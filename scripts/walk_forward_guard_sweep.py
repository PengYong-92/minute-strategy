#!/usr/bin/env python3
import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backtest import BacktestConfig, load_klines_from_zips
from app.models import Kline
from app.rolling_edge import RollingEdgeConfig, rolling_edge_snapshot, should_degrade
from scripts.walk_forward_edge_cycle import (
    DEFAULT_EDGE,
    MS_PER_DAY,
    Candidate,
    _compact_replay,
    _iso,
    _load_or_precompute_candidates,
    _parse_date,
    _simulate,
    _worker_count,
)


@dataclass(frozen=True)
class GuardSweepConfig:
    lookback_days: int
    min_samples: int
    min_win_rate: float
    min_ev: float


STATIC_TWO_YEAR_LONG_MAP = {
    "WD-00": 16.0,
    "WD-08": 26.0,
    "WD-20": 14.0,
    "WD-22": 25.0,
    "WE-02": 24.0,
    "WE-03": 33.0,
    "WE-08": 25.0,
    "WE-17": 36.0,
}


def evaluate_guard_config(
    klines: Sequence[Kline],
    candidates: Sequence[Candidate],
    start_ms: int,
    end_ms: int,
    guard: GuardSweepConfig,
    base_config: BacktestConfig,
) -> dict:
    config = BacktestConfig(
        warmup_minutes=base_config.warmup_minutes,
        max_open_orders=base_config.max_open_orders,
        min_order_gap_minutes=base_config.min_order_gap_minutes,
        strategy_history_limit=base_config.strategy_history_limit,
        stake=base_config.stake,
        win_return=base_config.win_return,
        enable_stake_progression=base_config.enable_stake_progression,
        stake_progression_max_orders=base_config.stake_progression_max_orders,
        enable_rolling_edge_guard=True,
        rolling_edge_lookback_days=guard.lookback_days,
        rolling_edge_min_samples=guard.min_samples,
        rolling_edge_min_win_rate=guard.min_win_rate,
        rolling_edge_min_ev=guard.min_ev,
    )
    replay = _simulate_with_rejections(
        klines,
        candidates,
        start_ms,
        end_ms,
        config,
    )
    return {
        "guard": asdict(guard),
        "summary": replay["summary"],
        "risk": replay["risk"],
        "by_direction": replay["by_direction"],
        "by_direction_segment": replay["by_direction_segment"],
        "rejected": replay["rejected"],
        "score": rank_guard_result(replay),
    }


def rank_guard_result(result: dict) -> float:
    summary = result["summary"]
    risk = result["risk"]
    rejected = result.get("rejected", {})
    return (
        summary["balance"]
        - abs(risk["max_drawdown"]) * 0.35
        - risk["max_loss_streak"] * 22.0
        - rejected.get("rolling_edge_degraded", 0) * 0.35
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sweep rolling edge guard parameters using cached candidate signals.")
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--cache", default=str(ROOT / "reports" / "walk_forward_candidates_20260521.json"))
    parser.add_argument("--report", default=str(ROOT / "reports" / "walk_forward_guard_sweep_20260521.json"))
    parser.add_argument("--start", default="2024-05-18")
    parser.add_argument("--end", default="2026-05-17")
    parser.add_argument("--eval-start", default="2025-05-18")
    parser.add_argument("--workers", default="auto")
    args = parser.parse_args(argv)

    started = time.perf_counter()
    data_dir = Path(args.data_dir)
    start_ms = _parse_date(args.start)
    end_ms = _parse_date(args.end, end_of_day=True)
    eval_start_ms = _parse_date(args.eval_start)

    zip_paths = sorted(data_dir.glob("BTCUSDT-1m-*.zip"))
    klines = [item for item in load_klines_from_zips(zip_paths) if start_ms <= item.close_time <= end_ms]
    close_times = [item.close_time for item in klines]
    candidates = _load_or_precompute_candidates(Path(args.cache), klines, close_times, _worker_count(args.workers))
    candidates = [item for item in candidates if eval_start_ms <= item.entry_time <= end_ms]

    base_config = BacktestConfig(enable_rolling_edge_guard=True, enable_stake_progression=True)
    baseline_guard = GuardSweepConfig(
        lookback_days=base_config.rolling_edge_lookback_days,
        min_samples=base_config.rolling_edge_min_samples,
        min_win_rate=base_config.rolling_edge_min_win_rate,
        min_ev=base_config.rolling_edge_min_ev,
    )

    configs = [
        GuardSweepConfig(lookback, samples, win_rate, ev)
        for lookback in (60, 90, 120, 180)
        for samples in (10, 15, 20, 30)
        for win_rate in (0.5556, 0.58, 0.60)
        for ev in (0.0, 0.2, 0.5, 1.0)
    ]
    results = []
    for index, guard in enumerate(configs, start=1):
        if index == 1 or index % 25 == 0 or index == len(configs):
            print(f"evaluating guard {index}/{len(configs)} {guard}")
        results.append(evaluate_guard_config(klines, candidates, eval_start_ms, end_ms, guard, base_config))
    results.sort(key=lambda item: item["score"], reverse=True)

    no_guard = _simulate(
        klines,
        candidates,
        eval_start_ms,
        end_ms,
        _static_edge_provider,
        BacktestConfig(enable_rolling_edge_guard=False, enable_stake_progression=True),
    )
    current_guard = evaluate_guard_config(klines, candidates, eval_start_ms, end_ms, baseline_guard, base_config)

    report = {
        "coverage": {
            "data_start": _iso(start_ms),
            "data_end": _iso(end_ms),
            "eval_start": _iso(eval_start_ms),
            "eval_end": _iso(end_ms),
            "klines": len(klines),
            "candidate_signals": len(candidates),
            "edge_map": STATIC_TWO_YEAR_LONG_MAP,
            "method": "fixed current static edge map; sweep only rolling-edge guard parameters.",
        },
        "no_guard": _compact_replay(no_guard),
        "current_guard": current_guard,
        "ranked_guard_results": results,
        "elapsed_seconds": round(time.perf_counter() - started, 4),
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved {report_path}")
    print(json.dumps(_printable_summary(report), ensure_ascii=False, indent=2))
    return 0


def _simulate_with_rejections(
    klines: Sequence[Kline],
    candidates: Sequence[Candidate],
    start_ms: int,
    end_ms: int,
    config: BacktestConfig,
) -> dict:
    orders: list[dict] = []
    open_orders: list[dict] = []
    rejected: dict[str, int] = {}
    last_order_time: int | None = None
    balance = 0.0
    next_stake = config.stake
    stake_progression_step = 1

    from scripts.walk_forward_edge_cycle import (
        _group,
        _next_stake_after_settlement,
        _risk,
        _settle,
        _summary,
        _win_return_for_stake,
    )

    for candidate in candidates:
        if candidate.entry_time < start_ms:
            continue
        if candidate.entry_time > end_ms:
            break

        for order in list(open_orders):
            if order["expires_at"] > candidate.entry_time:
                continue
            _settle(order, klines[order["exit_index"]], config)
            balance = round(balance + order["pnl"], 4)
            next_stake, stake_progression_step = _next_stake_after_settlement(order, config)
            open_orders.remove(order)

        if candidate.expires_at > end_ms:
            continue
        if candidate.edge >= _static_edge_provider(candidate, candidate.entry_time):
            rejected["edge_cap"] = rejected.get("edge_cap", 0) + 1
            continue
        if len(open_orders) >= config.max_open_orders:
            rejected["max_open_orders"] = rejected.get("max_open_orders", 0) + 1
            continue
        if last_order_time is not None and candidate.entry_time - last_order_time < config.min_order_gap_minutes * 60_000:
            rejected["min_order_gap"] = rejected.get("min_order_gap", 0) + 1
            continue
        if _rolling_edge_degraded_for_guard(orders, candidate, config):
            rejected["rolling_edge_degraded"] = rejected.get("rolling_edge_degraded", 0) + 1
            continue

        stake = round(next_stake, 4)
        win_return = _win_return_for_stake(stake, config)
        order = {
            "id": len(orders) + 1,
            "direction": candidate.direction,
            "timeframe_minutes": 10,
            "level": "A",
            "reason": candidate.reason,
            "score": candidate.score,
            "threshold": candidate.threshold,
            "threshold_segment": candidate.segment,
            "stake_key": candidate.stake_key,
            "stake": stake,
            "win_return": win_return,
            "stake_progression_step": stake_progression_step,
            "entry_price": klines[candidate.entry_index].close,
            "entry_time": candidate.entry_time,
            "expires_at": candidate.expires_at,
            "exit_index": candidate.exit_index,
            "exit_price": None,
            "exit_time": None,
            "result": None,
            "pnl": 0.0,
        }
        orders.append(order)
        open_orders.append(order)
        last_order_time = candidate.entry_time

    for order in list(open_orders):
        if order["expires_at"] <= end_ms:
            _settle(order, klines[order["exit_index"]], config)
            balance = round(balance + order["pnl"], 4)
            next_stake, stake_progression_step = _next_stake_after_settlement(order, config)
            open_orders.remove(order)

    settled = [order for order in orders if order["result"]]
    return {
        "summary": _summary(settled, balance),
        "risk": _risk(settled),
        "by_direction": _group(settled, "direction"),
        "by_direction_segment": _group(settled, "stake_key"),
        "orders": settled,
        "rejected": rejected,
    }


def _rolling_edge_degraded_for_guard(orders: Sequence[dict], candidate: Candidate, config: BacktestConfig) -> bool:
    edge_config = RollingEdgeConfig(
        lookback_days=config.rolling_edge_lookback_days,
        min_samples=config.rolling_edge_min_samples,
        min_win_rate=config.rolling_edge_min_win_rate,
        min_ev=config.rolling_edge_min_ev,
    )
    current_item = {
        "entry_time": candidate.entry_time,
        "timeframe_minutes": 10,
        "threshold_segment": candidate.segment,
        "reason": candidate.reason,
    }
    snapshot = rolling_edge_snapshot(orders, current_item, edge_config)
    return should_degrade(snapshot, edge_config)


def _static_edge_provider(candidate: Candidate, _current_time: int) -> float:
    if candidate.direction == "SHORT":
        return DEFAULT_EDGE
    return STATIC_TWO_YEAR_LONG_MAP.get(candidate.segment, DEFAULT_EDGE)


def _printable_summary(report: dict) -> dict:
    return {
        "coverage": report["coverage"],
        "no_guard": {
            "summary": report["no_guard"]["summary"],
            "risk": report["no_guard"]["risk"],
        },
        "current_guard": {
            "guard": report["current_guard"]["guard"],
            "summary": report["current_guard"]["summary"],
            "risk": report["current_guard"]["risk"],
            "rejected": report["current_guard"]["rejected"],
        },
        "top_guard_results": [
            {
                "guard": item["guard"],
                "summary": item["summary"],
                "risk": item["risk"],
                "rejected": item["rejected"],
                "score": round(item["score"], 4),
            }
            for item in report["ranked_guard_results"][:10]
        ],
    }


if __name__ == "__main__":
    raise SystemExit(main())
