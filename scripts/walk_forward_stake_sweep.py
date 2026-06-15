#!/usr/bin/env python3
import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backtest import BacktestConfig, load_klines_from_zips
from app.models import Kline
from scripts.walk_forward_edge_cycle import (
    DEFAULT_EDGE,
    Candidate,
    _compact_replay,
    _group,
    _iso,
    _load_or_precompute_candidates,
    _parse_date,
    _risk,
    _settle,
    _summary,
    _worker_count,
)
from scripts.walk_forward_guard_sweep import STATIC_TWO_YEAR_LONG_MAP


@dataclass(frozen=True)
class StakePolicy:
    name: str
    enable_progression: bool
    max_orders: int = 1
    max_stake: float | None = None
    loss_cooldown_orders: int = 0


def evaluate_stake_policy(
    klines: Sequence[Kline],
    candidates: Sequence[Candidate],
    start_ms: int,
    end_ms: int,
    policy: StakePolicy,
    base_config: BacktestConfig,
) -> dict:
    config = BacktestConfig(
        warmup_minutes=base_config.warmup_minutes,
        max_open_orders=base_config.max_open_orders,
        min_order_gap_minutes=base_config.min_order_gap_minutes,
        strategy_history_limit=base_config.strategy_history_limit,
        stake=base_config.stake,
        win_return=base_config.win_return,
        enable_stake_progression=policy.enable_progression,
        stake_progression_max_orders=policy.max_orders,
        enable_rolling_edge_guard=base_config.enable_rolling_edge_guard,
        rolling_edge_lookback_days=base_config.rolling_edge_lookback_days,
        rolling_edge_min_samples=base_config.rolling_edge_min_samples,
        rolling_edge_min_win_rate=base_config.rolling_edge_min_win_rate,
        rolling_edge_min_ev=base_config.rolling_edge_min_ev,
    )
    replay = _simulate_stake_policy(klines, candidates, start_ms, end_ms, config, policy)
    return {
        "policy": asdict(policy),
        "summary": replay["summary"],
        "risk": replay["risk"],
        "by_direction": replay["by_direction"],
        "by_direction_segment": replay["by_direction_segment"],
        "orders": replay["orders"],
        "rejected": replay["rejected"],
        "score": rank_stake_result(replay),
    }


def rank_stake_result(result: dict) -> float:
    summary = result["summary"]
    risk = result["risk"]
    fixed_balance = summary.get("fixed_balance", 0.0)
    return (
        summary["balance"]
        + fixed_balance * 0.15
        - abs(risk["max_drawdown"]) * 0.45
        - risk["max_loss_streak"] * 25.0
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sweep stake progression policies using cached candidate signals.")
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--cache", default=str(ROOT / "reports" / "walk_forward_candidates_20260521.json"))
    parser.add_argument("--report", default=str(ROOT / "reports" / "walk_forward_stake_sweep_20260521.json"))
    parser.add_argument("--start", default="2024-05-18")
    parser.add_argument("--end", default="2026-05-17")
    parser.add_argument("--eval-start", default="2025-05-18")
    parser.add_argument("--workers", default="auto")
    args = parser.parse_args(argv)

    started = time.perf_counter()
    start_ms = _parse_date(args.start)
    end_ms = _parse_date(args.end, end_of_day=True)
    eval_start_ms = _parse_date(args.eval_start)
    data_dir = Path(args.data_dir)
    zip_paths = sorted(data_dir.glob("BTCUSDT-1m-*.zip"))
    klines = [item for item in load_klines_from_zips(zip_paths) if start_ms <= item.close_time <= end_ms]
    close_times = [item.close_time for item in klines]
    candidates = _load_or_precompute_candidates(Path(args.cache), klines, close_times, _worker_count(args.workers))
    candidates = [item for item in candidates if eval_start_ms <= item.entry_time <= end_ms]

    base_config = BacktestConfig(enable_rolling_edge_guard=True, enable_stake_progression=True)
    policies = [
        StakePolicy("fixed_10u", False, 1, None, 0),
        StakePolicy("progression_2x", True, 2, None, 0),
        StakePolicy("progression_3x", True, 3, None, 0),
        StakePolicy("progression_3x_cap18", True, 3, 18.0, 0),
        StakePolicy("progression_3x_cap24", True, 3, 24.0, 0),
        StakePolicy("progression_3x_cap32_4", True, 3, 32.4, 0),
        StakePolicy("progression_3x_loss_cooldown_1", True, 3, None, 1),
        StakePolicy("progression_3x_loss_cooldown_2", True, 3, None, 2),
        StakePolicy("progression_2x_cap18_loss_cooldown_1", True, 2, 18.0, 1),
    ]
    results = []
    for policy in policies:
        print(f"evaluating {policy}")
        results.append(evaluate_stake_policy(klines, candidates, eval_start_ms, end_ms, policy, base_config))
    results.sort(key=lambda item: item["score"], reverse=True)
    report = {
        "coverage": {
            "data_start": _iso(start_ms),
            "data_end": _iso(end_ms),
            "eval_start": _iso(eval_start_ms),
            "eval_end": _iso(end_ms),
            "klines": len(klines),
            "candidate_signals": len(candidates),
            "edge_map": STATIC_TWO_YEAR_LONG_MAP,
            "guard": {
                "lookback_days": base_config.rolling_edge_lookback_days,
                "min_samples": base_config.rolling_edge_min_samples,
                "min_win_rate": base_config.rolling_edge_min_win_rate,
                "min_ev": base_config.rolling_edge_min_ev,
            },
            "method": "fixed static edge map and selected rolling guard; sweep stake progression policies.",
        },
        "ranked_stake_results": results,
        "elapsed_seconds": round(time.perf_counter() - started, 4),
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved {report_path}")
    print(json.dumps(_printable_summary(report), ensure_ascii=False, indent=2))
    return 0


def _simulate_stake_policy(
    klines: Sequence[Kline],
    candidates: Sequence[Candidate],
    start_ms: int,
    end_ms: int,
    config: BacktestConfig,
    policy: StakePolicy,
) -> dict:
    orders: list[dict] = []
    open_orders: list[dict] = []
    rejected: dict[str, int] = {}
    last_order_time: int | None = None
    balance = 0.0
    next_stake = config.stake
    stake_progression_step = 1
    cooldown_remaining = 0

    from scripts.walk_forward_guard_sweep import _rolling_edge_degraded_for_guard

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
            next_stake, stake_progression_step, cooldown_remaining = _next_stake_after_policy_settlement(
                order, config, policy, cooldown_remaining
            )
            open_orders.remove(order)

        if candidate.expires_at > end_ms:
            continue
        if candidate.edge >= _static_edge_provider(candidate):
            rejected["edge_cap"] = rejected.get("edge_cap", 0) + 1
            continue
        if len(open_orders) >= config.max_open_orders:
            rejected["max_open_orders"] = rejected.get("max_open_orders", 0) + 1
            continue
        if last_order_time is not None and candidate.entry_time - last_order_time < config.min_order_gap_minutes * 60_000:
            rejected["min_order_gap"] = rejected.get("min_order_gap", 0) + 1
            continue
        if config.enable_rolling_edge_guard and _rolling_edge_degraded_for_guard(orders, candidate, config):
            rejected["rolling_edge_degraded"] = rejected.get("rolling_edge_degraded", 0) + 1
            continue

        stake = round(next_stake, 4)
        win_return = _win_return_for_policy_stake(stake, config)
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
            next_stake, stake_progression_step, cooldown_remaining = _next_stake_after_policy_settlement(
                order, config, policy, cooldown_remaining
            )
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


def _next_stake_after_policy_settlement(
    order: dict,
    config: BacktestConfig,
    policy: StakePolicy,
    cooldown_remaining: int,
) -> tuple[float, int, int]:
    if not policy.enable_progression:
        return config.stake, 1, 0
    if order.get("result") != "WIN":
        return config.stake, 1, max(0, int(policy.loss_cooldown_orders))
    if cooldown_remaining > 0:
        return config.stake, 1, cooldown_remaining - 1
    current_step = int(order.get("stake_progression_step", 1) or 1)
    if current_step >= max(1, int(policy.max_orders)):
        return config.stake, 1, 0
    next_stake = float(order.get("win_return", config.win_return))
    if policy.max_stake is not None:
        next_stake = min(next_stake, float(policy.max_stake))
    return next_stake, current_step + 1, 0


def _win_return_for_policy_stake(stake: float, config: BacktestConfig) -> float:
    if config.stake <= 0:
        return round(config.win_return, 4)
    return round(stake * (config.win_return / config.stake), 4)


def _static_edge_provider(candidate: Candidate) -> float:
    if candidate.direction == "SHORT":
        return DEFAULT_EDGE
    return STATIC_TWO_YEAR_LONG_MAP.get(candidate.segment, DEFAULT_EDGE)


def _printable_summary(report: dict) -> dict:
    return {
        "coverage": report["coverage"],
        "top_stake_results": [
            {
                "policy": item["policy"],
                "summary": item["summary"],
                "risk": item["risk"],
                "rejected": item["rejected"],
                "score": round(item["score"], 4),
            }
            for item in report["ranked_stake_results"]
        ],
    }


if __name__ == "__main__":
    raise SystemExit(main())
