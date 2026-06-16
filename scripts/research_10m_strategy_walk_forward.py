import argparse
import json
import sys
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.research_10m_strategy_coarse_replay import (
    DEFAULT_DATA_DIR,
    DEFAULT_REPORT_DIR,
    HORIZON_MINUTES,
    SYMBOL,
    Candidate,
    Kline,
    _dt,
    event_contract_pnl,
    failed_breakout_candidates,
    load_klines_from_zips,
    replay_candidates,
    reversal_candidates,
    rolling_bollinger_position,
    rolling_mean,
    rolling_rsi,
    settle_candidate,
    summarize_strategy,
)


DAY_MS = 24 * 60 * 60 * 1000


@dataclass(frozen=True)
class EdgeGateConfig:
    lookback_days: int = 60
    min_samples: int = 5
    min_win_rate: float = 0.62
    min_ev: float = 0.5


FOCUS_STRATEGIES = {
    "drop_reclaim_10m_80bps_rsi45_boll0.35",
    "drop_reclaim_10m_80bps_rsi40_boll0.35",
    "drop_reclaim_10m_80bps_rsi30_boll0.1",
    "failed_low_120m_10bps_vol2.0",
    "failed_low_120m_10bps_vol1.5",
    "failed_high_60m_20bps_vol1.5",
}


def threshold_segment(timestamp_ms: int) -> str:
    hour = (timestamp_ms // 3_600_000) % 24
    day = timestamp_ms // 86_400_000
    weekday = (day + 3) % 7
    day_type = "WE" if weekday >= 5 else "WD"
    return f"{day_type}-{hour:02d}"


class ObservedHistory:
    def __init__(self) -> None:
        self._orders: list[dict] = []

    def add(self, order: dict) -> None:
        self._orders.append(order)

    def allowed(self, current: dict, config: EdgeGateConfig) -> dict:
        cutoff = current["entry_time"] - config.lookback_days * DAY_MS
        prior = [
            order
            for order in self._orders
            if cutoff <= order["entry_time"] < current["entry_time"]
            and order["strategy"] == current["strategy"]
            and order["segment"] == current["segment"]
        ]
        wins = sum(1 for order in prior if order["result"] == "WIN")
        pnl = round(sum(order["pnl"] for order in prior), 4)
        sample_size = len(prior)
        win_rate = wins / sample_size if sample_size else 0.0
        ev = pnl / sample_size if sample_size else 0.0
        has_sample = sample_size >= config.min_samples
        allowed = has_sample and win_rate >= config.min_win_rate and ev > config.min_ev
        return {
            "allowed": allowed,
            "sample_size": sample_size,
            "wins": wins,
            "losses": sample_size - wins,
            "win_rate": round(win_rate, 6),
            "pnl": pnl,
            "ev": round(ev, 6),
        }


def generate_focus_candidates(klines: Sequence[Kline]) -> list[Candidate]:
    close_values = [item.close for item in klines]
    volumes = [item.volume for item in klines]
    rsi_values = rolling_rsi(close_values, 14)
    boll_positions = rolling_bollinger_position(close_values, 20)
    volume_means = rolling_mean(volumes, 60)
    candidates: list[Candidate] = []
    for index in range(120, len(klines) - HORIZON_MINUTES):
        for candidate in reversal_candidates(klines, index, rsi_values, boll_positions):
            if candidate.strategy in FOCUS_STRATEGIES:
                candidates.append(candidate)
        for candidate in failed_breakout_candidates(klines, index, volume_means, boll_positions):
            if candidate.strategy in FOCUS_STRATEGIES:
                candidates.append(candidate)
    return candidates


def add_observation_fields(order: dict) -> dict:
    enriched = dict(order)
    enriched["segment"] = threshold_segment(order["entry_time"])
    enriched["month"] = _dt(order["entry_time"]).strftime("%Y-%m")
    enriched["utc_hour"] = f"{_dt(order['entry_time']).hour:02d}"
    return enriched


def observation_orders(candidates: Sequence[Candidate], klines: Sequence[Kline]) -> list[dict]:
    raw_orders = replay_candidates(candidates, klines, enforce_cooldown=True)
    return [add_observation_fields(order) for order in raw_orders]


def walk_forward_replay(
    candidates: Sequence[Candidate],
    klines: Sequence[Kline],
    config: EdgeGateConfig,
) -> dict:
    observed_history = ObservedHistory()
    traded_orders: list[dict] = []
    observed_orders: list[dict] = []
    rejected = {"edge_degraded": 0, "not_enough_samples": 0}
    last_trade_time_by_strategy: dict[str, int] = {}

    for candidate in sorted(candidates, key=lambda item: (item.entry_time, item.strategy)):
        if candidate.entry_index + HORIZON_MINUTES >= len(klines):
            continue
        raw_order = settle_candidate(candidate, klines)
        observed = add_observation_fields(raw_order)
        observed_orders.append(observed)
        gate = observed_history.allowed(observed, config)
        if gate["sample_size"] >= config.min_samples and not gate["allowed"]:
            rejected["edge_degraded"] += 1
        else:
            last_trade = last_trade_time_by_strategy.get(candidate.strategy)
            if last_trade is None or candidate.entry_time - last_trade >= HORIZON_MINUTES * 60_000:
                traded = dict(observed)
                traded["gate"] = gate
                traded_orders.append(traded)
                last_trade_time_by_strategy[candidate.strategy] = candidate.entry_time
        observed_history.add(observed)

    return {
        "observed": _portfolio_summary(observed_orders),
        "traded": _portfolio_summary(traded_orders),
        "observed_by_strategy": _summaries_by_strategy(observed_orders),
        "traded_by_strategy": _summaries_by_strategy(traded_orders),
        "rejected": rejected,
        "orders": traded_orders,
    }


def rolling_profile_comparison(observed_orders: Sequence[dict]) -> dict:
    results: dict[str, dict] = {}
    for days in [7, 30]:
        config = EdgeGateConfig(lookback_days=days, min_samples=1, min_win_rate=0.0, min_ev=-999.0)
        history = ObservedHistory()
        snapshots = []
        for order in sorted(observed_orders, key=lambda item: item["entry_time"]):
            gate = history.allowed(order, config)
            if gate["sample_size"]:
                snapshots.append({**gate, "strategy": order["strategy"], "segment": order["segment"], "entry_time": order["entry_time"]})
            history.add(order)
        results[f"{days}d"] = {
            "snapshots": len(snapshots),
            "avg_sample_size": _avg(item["sample_size"] for item in snapshots),
            "avg_win_rate": _avg(item["win_rate"] for item in snapshots),
            "avg_ev": _avg(item["ev"] for item in snapshots),
            "positive_edge_ratio": _avg(1.0 if item["win_rate"] > 0.5556 and item["ev"] > 0 else 0.0 for item in snapshots),
        }
    return results


def _portfolio_summary(orders: Sequence[dict]) -> dict:
    if not orders:
        return summarize_strategy("portfolio", [])
    summary = summarize_strategy("portfolio", orders)
    summary["by_strategy"] = _summaries_by_strategy(orders)
    summary["by_segment"] = _group_compact(orders, "segment")
    return summary


def _summaries_by_strategy(orders: Sequence[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for order in orders:
        groups[order["strategy"]].append(order)
    return sorted((summarize_strategy(name, rows) for name, rows in groups.items()), key=lambda item: item["pnl"], reverse=True)


def _group_compact(orders: Sequence[dict], key: str) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for order in orders:
        groups[str(order.get(key, ""))].append(order)
    return {name: _compact(rows) for name, rows in sorted(groups.items())}


def _compact(orders: Sequence[dict]) -> dict:
    wins = sum(1 for order in orders if order["result"] == "WIN")
    pnl = round(sum(order["pnl"] for order in orders), 4)
    return {
        "orders": len(orders),
        "wins": wins,
        "losses": len(orders) - wins,
        "win_rate": round(wins / len(orders), 6) if orders else 0.0,
        "pnl": pnl,
    }


def _avg(values) -> float:
    items = list(values)
    return round(sum(items) / len(items), 6) if items else 0.0


def run_walk_forward(data_dir: Path = DEFAULT_DATA_DIR, report_dir: Path = DEFAULT_REPORT_DIR, limit_files: int | None = None) -> dict:
    zip_paths = sorted(data_dir.glob(f"{SYMBOL}-1m-*.zip"))
    if limit_files:
        zip_paths = zip_paths[-limit_files:]
    started = time.perf_counter()
    klines = load_klines_from_zips(zip_paths)
    candidates = generate_focus_candidates(klines)
    observed = observation_orders(candidates, klines)
    configs = {
        "60d_5_62_ev05": EdgeGateConfig(60, 5, 0.62, 0.5),
        "30d_5_60_ev0": EdgeGateConfig(30, 5, 0.60, 0.0),
        "7d_3_58_ev0": EdgeGateConfig(7, 3, 0.58, 0.0),
    }
    walk_forward = {name: walk_forward_replay(candidates, klines, config) for name, config in configs.items()}
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbol": SYMBOL,
        "data_files": len(zip_paths),
        "from": _dt(klines[0].open_time).isoformat() if klines else "",
        "to": _dt(klines[-1].open_time).isoformat() if klines else "",
        "klines": len(klines),
        "focus_strategies": sorted(FOCUS_STRATEGIES),
        "candidate_signals": len(candidates),
        "observed": _portfolio_summary(observed),
        "observed_by_strategy": _summaries_by_strategy(observed),
        "profiles": rolling_profile_comparison(observed),
        "walk_forward": walk_forward,
        "elapsed_seconds": round(time.perf_counter() - started, 4),
    }
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"10m_strategy_walk_forward_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def print_summary(report: dict) -> None:
    print("=== 10分钟重点策略滚动样本外验证 ===")
    print(f"数据: {report['from']} -> {report['to']} files={report['data_files']} klines={report['klines']}")
    print(f"候选信号: {report['candidate_signals']} 报告: {report['report_path']}")
    obs = report["observed"]
    print(f"观察全集: orders={obs['orders']} win={obs['win_rate']:.2%} pnl={obs['pnl']:.2f} mdd={obs['max_drawdown']:.2f}")
    for name, result in report["walk_forward"].items():
        traded = result["traded"]
        print(
            f"{name}: orders={traded['orders']} win={traded['win_rate']:.2%} pnl={traded['pnl']:.2f} "
            f"mdd={traded['max_drawdown']:.2f} rejected={result['rejected']}"
        )
    print("\n观察策略明细:")
    for item in report["observed_by_strategy"]:
        print(f"{item['strategy']} {item['direction']} orders={item['orders']} win={item['win_rate']:.2%} pnl={item['pnl']:.2f}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Walk-forward validation for selected 10m event-contract research strategies.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--limit-files", type=int, default=None)
    args = parser.parse_args(argv)
    report = run_walk_forward(data_dir=args.data_dir, report_dir=args.report_dir, limit_files=args.limit_files)
    print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
