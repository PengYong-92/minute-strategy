#!/usr/bin/env python3
import argparse
import json
import sys
import time
from collections import defaultdict, deque
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backtest import load_klines_from_zips
from app.indicators import build_technical_context
from app.models import Kline
from app.segments import threshold_segment


SYMBOL = "BTCUSDT"
HORIZON_MINUTES = 10
BREAK_EVEN_WIN_RATE = 10.0 / 18.0
DAY_MS = 86_400_000
RESEARCH_FIXED_SEGMENTS = {
    "failed_high_120m_short_observe": {"WD-14", "WE-15", "WD-18", "WE-06", "WD-21"},
    "failed_low_120m_long_observe": {"WD-08", "WE-09"},
}


@dataclass(frozen=True)
class RollingGateConfig:
    lookback_days: int
    min_samples: int
    min_win_rate: float
    min_ev: float
    key_mode: str = "tag_direction_segment"


@dataclass(frozen=True)
class SegmentSelectionConfig:
    lookback_days: int
    update_days: int
    min_samples: int
    min_win_rate: float
    min_ev: float
    key_mode: str = "tag_segment"


def generate_observation_candidates(
    klines: Sequence[Kline],
    *,
    horizon_minutes: int = HORIZON_MINUTES,
    cooldown_minutes: int = HORIZON_MINUTES,
    enforce_live_segments: bool = False,
) -> list[dict[str, Any]]:
    highs = [item.high for item in klines]
    lows = [item.low for item in klines]
    high_120 = _rolling_extreme(highs, 120, max)
    low_120 = _rolling_extreme(lows, 120, min)
    high_10 = _rolling_extreme(highs, 10, max)
    low_10 = _rolling_extreme(lows, 10, min)

    candidates: list[dict[str, Any]] = []
    last_entry_by_key: dict[tuple[str, str], int] = {}
    cooldown_ms = cooldown_minutes * 60_000
    start_index = 129
    end_index = len(klines) - horizon_minutes
    for index in range(start_index, end_index):
        prior_high = high_120[index - 10]
        prior_low = low_120[index - 10]
        recent_high = high_10[index]
        recent_low = low_10[index]
        if prior_high is None or prior_low is None or recent_high is None or recent_low is None:
            continue

        latest = klines[index]
        candle_strength = _candle_close_strength(latest)
        maybe_failed_high = recent_high > prior_high and latest.close <= prior_high and candle_strength <= 0.45
        maybe_failed_low = recent_low < prior_low and latest.close >= prior_low and candle_strength >= 0.55
        if not maybe_failed_high and not maybe_failed_low:
            continue

        recent = klines[index - 9 : index + 1]
        technical = build_technical_context(klines[max(0, index - 249) : index + 1])
        close_strength = _range_close_strength(recent, latest.close)
        rows = []
        if (
            maybe_failed_high
            and technical.bollinger_position >= 0.50
            and technical.macd_histogram < 0.0
        ):
            rows.append(
                _candidate_order(
                    klines,
                    index,
                    horizon_minutes,
                    strategy_tag="failed_high_120m_short_observe",
                    strategy_family="failed_breakout",
                    direction="SHORT",
                    reason="冲高失败SHORT观察：10分钟窗口突破120分钟高点后收回，BOLL>=0.5且MACD柱<0",
                    features={
                        "prior_high": prior_high,
                        "recent_high": recent_high,
                        "candle_strength": candle_strength,
                        "close_strength": close_strength,
                        "bollinger_position": technical.bollinger_position,
                        "macd_histogram": technical.macd_histogram,
                    },
                )
            )
        if (
            maybe_failed_low
            and technical.bollinger_position <= 0.35
            and close_strength <= 0.35
        ):
            rows.append(
                _candidate_order(
                    klines,
                    index,
                    horizon_minutes,
                    strategy_tag="failed_low_120m_long_observe",
                    strategy_family="failed_breakout",
                    direction="LONG",
                    reason="破低收回LONG观察：10分钟窗口跌破120分钟低点后收回，BOLL<=0.35且10m收盘偏弱",
                    features={
                        "prior_low": prior_low,
                        "recent_low": recent_low,
                        "candle_strength": candle_strength,
                        "close_strength": close_strength,
                        "bollinger_position": technical.bollinger_position,
                        "macd_histogram": technical.macd_histogram,
                    },
                )
            )

        for row in rows:
            if enforce_live_segments and row["threshold_segment"] not in RESEARCH_FIXED_SEGMENTS.get(
                row["strategy_tag"], set()
            ):
                continue
            key = (row["strategy_tag"], row["direction"])
            last_entry = last_entry_by_key.get(key)
            if last_entry is not None and row["entry_time"] - last_entry < cooldown_ms:
                continue
            candidates.append(row)
            last_entry_by_key[key] = row["entry_time"]
    return candidates


def walk_forward_replay(orders: Sequence[dict[str, Any]], config: RollingGateConfig) -> dict[str, Any]:
    history: dict[str, deque[tuple[int, str, float]]] = defaultdict(deque)
    traded = []
    rejected = {"not_enough_samples": 0, "edge_degraded": 0}
    lookback_ms = config.lookback_days * DAY_MS
    for order in sorted(orders, key=lambda item: item["entry_time"]):
        key = _rolling_key(order, config.key_mode)
        bucket = history[key]
        while bucket and bucket[0][0] < order["entry_time"] - lookback_ms:
            bucket.popleft()
        gate = _history_gate(bucket, config)
        enriched = dict(order)
        enriched["rolling_key"] = key
        enriched["rolling_sample_size"] = len(bucket)
        enriched["rolling_win_rate"] = gate["win_rate"]
        enriched["rolling_ev"] = gate["ev"]
        if gate["allowed"]:
            traded.append(enriched)
        elif len(bucket) < config.min_samples:
            rejected["not_enough_samples"] += 1
        else:
            rejected["edge_degraded"] += 1
        bucket.append((order["entry_time"], order["result"], order["pnl"]))
    return {
        "config": asdict(config),
        "traded": summarize_orders(traded),
        "traded_by_strategy": group_summaries(traded, lambda item: item["strategy_tag"]),
        "traded_by_segment": group_summaries(traded, lambda item: item["threshold_segment"]),
        "rejected": rejected,
    }


def walk_forward_segment_selection_replay(
    orders: Sequence[dict[str, Any]],
    config: SegmentSelectionConfig,
) -> dict[str, Any]:
    ordered = sorted(orders, key=lambda item: item["entry_time"])
    if not ordered:
        return {
            "config": asdict(config),
            "traded": summarize_orders([]),
            "traded_by_strategy": [],
            "traded_by_segment": [],
            "rejected": {"not_selected": 0},
            "schedule_stats": {"updates": 0, "avg_selected": 0.0, "max_selected": 0},
            "schedule_tail": [],
        }

    traded = []
    rejected = {"not_selected": 0}
    schedule = []
    selected_keys: set[str] = set()
    lookback_ms = config.lookback_days * DAY_MS
    update_ms = max(1, config.update_days) * DAY_MS
    next_update = ordered[0]["entry_time"]

    for order in ordered:
        while order["entry_time"] >= next_update:
            selected_keys = _train_segment_selection(ordered, next_update - lookback_ms, next_update, config)
            schedule.append(
                {
                    "effective_at": next_update,
                    "effective_at_utc": _iso(next_update),
                    "selected": sorted(selected_keys),
                    "selected_count": len(selected_keys),
                }
            )
            next_update += update_ms

        key = _selection_key(order, config.key_mode)
        if key in selected_keys:
            enriched = dict(order)
            enriched["selection_key"] = key
            enriched["selection_config"] = asdict(config)
            traded.append(enriched)
        else:
            rejected["not_selected"] += 1

    selected_counts = [item["selected_count"] for item in schedule]
    return {
        "config": asdict(config),
        "traded": summarize_orders(traded),
        "traded_by_strategy": group_summaries(traded, lambda item: item["strategy_tag"]),
        "traded_by_segment": group_summaries(traded, lambda item: item["threshold_segment"]),
        "rejected": rejected,
        "schedule_stats": {
            "updates": len(schedule),
            "avg_selected": round(sum(selected_counts) / len(selected_counts), 4) if selected_counts else 0.0,
            "max_selected": max(selected_counts) if selected_counts else 0,
        },
        "schedule_tail": schedule[-8:],
    }


def _train_segment_selection(
    orders: Sequence[dict[str, Any]],
    train_start: int,
    train_end: int,
    config: SegmentSelectionConfig,
) -> set[str]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for order in orders:
        if order["entry_time"] < train_start:
            continue
        if order["entry_time"] >= train_end:
            break
        groups[_selection_key(order, config.key_mode)].append(order)

    selected = set()
    for key, items in groups.items():
        stats = summarize_orders(items)
        if (
            stats["orders"] >= config.min_samples
            and stats["win_rate"] >= config.min_win_rate
            and stats["ev"] >= config.min_ev
        ):
            selected.add(key)
    return selected


def summarize_orders(orders: Sequence[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(orders, key=lambda item: item["entry_time"])
    wins = [item for item in ordered if item["result"] == "WIN"]
    pnl = round(sum(float(item["pnl"]) for item in ordered), 4)
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    loss_streak = 0
    max_loss_streak = 0
    win_streak = 0
    max_win_streak = 0
    for item in ordered:
        equity = round(equity + float(item["pnl"]), 4)
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)
        if item["result"] == "LOSS":
            loss_streak += 1
            win_streak = 0
        else:
            win_streak += 1
            loss_streak = 0
        max_loss_streak = max(max_loss_streak, loss_streak)
        max_win_streak = max(max_win_streak, win_streak)
    return {
        "orders": len(ordered),
        "wins": len(wins),
        "losses": len(ordered) - len(wins),
        "win_rate": round(len(wins) / len(ordered), 6) if ordered else 0.0,
        "pnl": pnl,
        "ev": round(pnl / len(ordered), 6) if ordered else 0.0,
        "roi": round(pnl / (len(ordered) * 10.0), 6) if ordered else 0.0,
        "break_even_win_rate": round(BREAK_EVEN_WIN_RATE, 6),
        "max_drawdown": round(max_drawdown, 4),
        "max_loss_streak": max_loss_streak,
        "max_win_streak": max_win_streak,
    }


def group_summaries(
    orders: Sequence[dict[str, Any]],
    key_func: Callable[[dict[str, Any]], str],
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for order in orders:
        groups[str(key_func(order))].append(order)
    rows = []
    for key, items in groups.items():
        row = {"key": key}
        row.update(summarize_orders(items))
        rows.append(row)
    rows.sort(key=lambda item: (item["pnl"], item["orders"], item["win_rate"]), reverse=True)
    return rows if limit is None else rows[:limit]


def run_replay(
    data_dir: Path,
    report_dir: Path,
    *,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    zip_paths = sorted(data_dir.glob(f"{SYMBOL}-1m-*.zip"))
    klines = load_klines_from_zips(zip_paths)
    start_ms = _parse_date(start) if start else None
    end_ms = _parse_date(end, end_of_day=True) if end else None
    if start_ms is not None or end_ms is not None:
        klines = [
            item
            for item in klines
            if (start_ms is None or item.close_time >= start_ms)
            and (end_ms is None or item.close_time <= end_ms)
        ]
    all_orders = generate_observation_candidates(klines)
    orders = [
        order
        for order in all_orders
        if order["threshold_segment"] in RESEARCH_FIXED_SEGMENTS.get(order["strategy_tag"], set())
    ]
    gate_configs = [
        RollingGateConfig(7, 10, 0.58, 0.0, "tag_direction"),
        RollingGateConfig(14, 10, 0.58, 0.0, "tag_direction"),
        RollingGateConfig(30, 20, 0.58, 0.5, "tag_direction"),
        RollingGateConfig(60, 30, 0.58, 0.5, "tag_direction"),
        RollingGateConfig(14, 10, 0.58, 0.0, "tag"),
        RollingGateConfig(30, 20, 0.58, 0.5, "tag"),
        RollingGateConfig(14, 10, 0.58, 0.0, "direction"),
        RollingGateConfig(30, 20, 0.58, 0.5, "direction"),
        RollingGateConfig(14, 10, 0.58, 0.0, "tag_direction_segment"),
        RollingGateConfig(30, 20, 0.58, 0.5, "tag_direction_segment"),
    ]
    selection_configs = [
        SegmentSelectionConfig(90, 14, 20, 0.58, 0.5, "tag_segment"),
        SegmentSelectionConfig(180, 30, 30, 0.58, 0.5, "tag_segment"),
        SegmentSelectionConfig(365, 30, 40, 0.58, 0.5, "tag_segment"),
        SegmentSelectionConfig(180, 30, 50, 0.57, 0.2, "segment"),
        SegmentSelectionConfig(365, 30, 80, 0.57, 0.2, "segment"),
    ]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbol": SYMBOL,
        "data_files": len(zip_paths),
        "from": _iso(klines[0].close_time) if klines else "",
        "to": _iso(klines[-1].close_time) if klines else "",
        "klines": len(klines),
        "method": (
            "Replay only the current research observation candidates: "
            "failed_high_120m_short_observe and failed_low_120m_long_observe. "
            "Orders are non-overlapping per tag/direction and settle after 10 minutes. "
            "The fixed segment subset is research-only and is not enabled in live strategy unless "
            "walk-forward selection is positive."
        ),
        "all_candidates": summarize_orders(all_orders),
        "observed": summarize_orders(orders),
        "observed_by_strategy": group_summaries(orders, lambda item: item["strategy_tag"]),
        "observed_by_direction": group_summaries(orders, lambda item: item["direction"]),
        "observed_by_segment": group_summaries(orders, lambda item: item["threshold_segment"], limit=30),
        "observed_by_month": group_summaries(orders, lambda item: _month(item["entry_time"])),
        "walk_forward": { _gate_name(config): walk_forward_replay(orders, config) for config in gate_configs },
        "walk_forward_segment_selection": {
            _selection_name(config): walk_forward_segment_selection_replay(all_orders, config)
            for config in selection_configs
        },
        "elapsed_seconds": round(time.perf_counter() - started, 4),
    }
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"observation_candidates_replay_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def print_summary(report: dict[str, Any]) -> None:
    all_candidates = report.get("all_candidates") or {}
    observed = report["observed"]
    print("=== 10分钟观察候选长周期回放 ===")
    print(f"数据: {report['from']} -> {report['to']} files={report['data_files']} klines={report['klines']}")
    if all_candidates:
        print(
            f"全候选: orders={all_candidates['orders']} win={all_candidates['win_rate']:.2%} "
            f"pnl={all_candidates['pnl']:.2f} ev={all_candidates['ev']:.2f}"
        )
    print(
        f"研究固定白名单子集: orders={observed['orders']} win={observed['win_rate']:.2%} "
        f"pnl={observed['pnl']:.2f} ev={observed['ev']:.2f} mdd={observed['max_drawdown']:.2f}"
    )
    print("\n按策略:")
    for item in report["observed_by_strategy"]:
        print(
            f"- {item['key']}: orders={item['orders']} win={item['win_rate']:.2%} "
            f"pnl={item['pnl']:.2f} ev={item['ev']:.2f} mdd={item['max_drawdown']:.2f}"
        )
    print("\n滚动观察守卫:")
    for name, result in report["walk_forward"].items():
        traded = result["traded"]
        print(
            f"- {name}: orders={traded['orders']} win={traded['win_rate']:.2%} "
            f"pnl={traded['pnl']:.2f} ev={traded['ev']:.2f} rejected={result['rejected']}"
        )
    print("\n滚动时段选择（全候选池，非固定白名单）:")
    for name, result in report.get("walk_forward_segment_selection", {}).items():
        traded = result["traded"]
        schedule = result.get("schedule_stats") or {}
        print(
            f"- {name}: orders={traded['orders']} win={traded['win_rate']:.2%} "
            f"pnl={traded['pnl']:.2f} ev={traded['ev']:.2f} "
            f"updates={schedule.get('updates', 0)} avg_keys={schedule.get('avg_selected', 0.0):.2f} "
            f"rejected={result['rejected']}"
        )
    print(f"\n报告: {report['report_path']}")


def _candidate_order(
    klines: Sequence[Kline],
    index: int,
    horizon_minutes: int,
    *,
    strategy_tag: str,
    strategy_family: str,
    direction: str,
    reason: str,
    features: dict[str, float],
) -> dict[str, Any]:
    entry = klines[index]
    exit_bar = klines[index + horizon_minutes]
    result, pnl = _event_pnl(direction, entry.close, exit_bar.close)
    return {
        "strategy_family": strategy_family,
        "strategy_tag": strategy_tag,
        "direction": direction,
        "timeframe_minutes": horizon_minutes,
        "threshold_segment": threshold_segment(entry.close_time),
        "entry_index": index,
        "exit_index": index + horizon_minutes,
        "entry_time": entry.close_time,
        "entry_time_utc": _iso(entry.close_time),
        "exit_time": exit_bar.close_time,
        "entry_price": entry.close,
        "exit_price": exit_bar.close,
        "result": result,
        "pnl": pnl,
        "reason": reason,
        "features": {key: round(value, 6) for key, value in features.items()},
    }


def _event_pnl(direction: str, entry_price: float, exit_price: float) -> tuple[str, float]:
    if direction == "SHORT":
        won = exit_price < entry_price
    else:
        won = exit_price > entry_price
    return ("WIN", 8.0) if won else ("LOSS", -10.0)


def _rolling_extreme(values: Sequence[float], window_size: int, func: Callable[[Sequence[float]], float]) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    for index in range(window_size - 1, len(values)):
        result[index] = func(values[index - window_size + 1 : index + 1])
    return result


def _candle_close_strength(kline: Kline) -> float:
    candle_range = kline.high - kline.low
    if candle_range <= 0:
        return 0.5
    return max(0.0, min(1.0, (kline.close - kline.low) / candle_range))


def _range_close_strength(recent: Sequence[Kline], close: float) -> float:
    high = max(item.high for item in recent)
    low = min(item.low for item in recent)
    width = high - low
    if width <= 0:
        return 0.5
    return max(0.0, min(1.0, (close - low) / width))


def _rolling_key(order: dict[str, Any], key_mode: str) -> str:
    if key_mode == "tag":
        return str(order["strategy_tag"])
    if key_mode == "tag_direction":
        return f"{order['strategy_tag']}|{order['direction']}"
    if key_mode == "direction":
        return str(order["direction"])
    if key_mode == "tag_direction_segment":
        return f"{order['strategy_tag']}|{order['direction']}|{order['threshold_segment']}"
    raise ValueError(f"unknown key_mode: {key_mode}")


def _selection_key(order: dict[str, Any], key_mode: str) -> str:
    if key_mode == "segment":
        return str(order["threshold_segment"])
    if key_mode == "tag_segment":
        return f"{order['strategy_tag']}|{order['threshold_segment']}"
    if key_mode == "tag_direction_segment":
        return f"{order['strategy_tag']}|{order['direction']}|{order['threshold_segment']}"
    raise ValueError(f"unknown key_mode: {key_mode}")


def _history_gate(history: Sequence[tuple[int, str, float]], config: RollingGateConfig) -> dict[str, Any]:
    wins = sum(1 for _time, result, _pnl in history if result == "WIN")
    pnl = sum(pnl for _time, _result, pnl in history)
    orders = len(history)
    win_rate = wins / orders if orders else 0.0
    ev = pnl / orders if orders else 0.0
    return {
        "orders": orders,
        "wins": wins,
        "win_rate": round(win_rate, 6),
        "ev": round(ev, 6),
        "allowed": orders >= config.min_samples and win_rate >= config.min_win_rate and ev >= config.min_ev,
    }


def _gate_name(config: RollingGateConfig) -> str:
    return f"{config.lookback_days}d_{config.min_samples}_{int(config.min_win_rate * 100)}_ev{config.min_ev:g}_{config.key_mode}"


def _selection_name(config: SegmentSelectionConfig) -> str:
    return (
        f"{config.lookback_days}d_update{config.update_days}d_"
        f"{config.min_samples}_{int(config.min_win_rate * 100)}_ev{config.min_ev:g}_{config.key_mode}"
    )


def _parse_date(value: str | None, *, end_of_day: bool = False) -> int:
    if not value:
        raise ValueError("date is required")
    dt = datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    ms = int(dt.timestamp() * 1000)
    return ms + DAY_MS - 1 if end_of_day and len(value) == 10 else ms


def _iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).isoformat()


def _month(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).strftime("%Y-%m")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay current 10m research observation candidates.")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--report-dir", type=Path, default=ROOT / "reports")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    args = parser.parse_args(argv)

    report = run_replay(args.data_dir, args.report_dir, start=args.start, end=args.end)
    print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
