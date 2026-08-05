#!/usr/bin/env python3
import argparse
import json
import math
import sys
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.daily_profile_selector import (
    SHANGHAI,
    DailyProfileSelectorConfig,
    build_daily_selection,
    profile_key,
    selection_window,
)
from app.models import ObservationSignal
from scripts.analyze_observations_db import load_observations


DAY_MS = 86_400_000


def replay_daily_profile_selection(
    observations: Sequence[ObservationSignal],
    config: DailyProfileSelectorConfig,
    *,
    require_full_lookback: bool = True,
    max_open_orders: int = 5,
    min_order_gap_ms: int = 2 * 60_000,
) -> dict[str, Any]:
    settled = sorted(
        (
            item
            for item in observations
            if item.status == "SETTLED"
            and item.result in {"WIN", "LOSS"}
            and item.settled_at is not None
        ),
        key=lambda item: (item.opened_at, item.observation_key),
    )
    if not settled:
        return _empty_replay(
            config,
            max_open_orders=max_open_orders,
            min_order_gap_ms=min_order_gap_ms,
        )

    snapshots = _build_schedule(settled, config, require_full_lookback=require_full_lookback)
    trades: list[dict[str, Any]] = []
    rejections = {
        "profile_not_selected": 0,
        "hold_open_order": 0,
        "cooldown": 0,
        "three_loss_pause": 0,
    }
    eligible_events = 0
    last_order_opened_at: int | None = None
    open_expiries: list[int] = []
    max_open_orders = max(1, int(max_open_orders))
    min_order_gap_ms = max(0, int(min_order_gap_ms))

    for snapshot in snapshots:
        grouped: dict[int, list[ObservationSignal]] = defaultdict(list)
        for item in settled:
            if item.opened_at < snapshot["effective_from"]:
                continue
            if item.opened_at >= snapshot["effective_until"]:
                break
            grouped[item.opened_at].append(item)

        selected_profiles = snapshot.get("selected_profiles") or []
        for opened_at, rows in sorted(grouped.items()):
            eligible_events += 1
            by_key = {
                _observation_profile_key(item): item
                for item in sorted(rows, key=lambda row: row.observation_key)
            }
            chosen = next(
                (by_key[item["key"]] for item in selected_profiles if item["key"] in by_key),
                None,
            )
            if chosen is None:
                rejections["profile_not_selected"] += 1
                continue
            open_expiries = [expires_at for expires_at in open_expiries if expires_at > opened_at]
            if len(open_expiries) >= max_open_orders:
                rejections["hold_open_order"] += 1
                continue
            if last_order_opened_at is not None and opened_at - last_order_opened_at < min_order_gap_ms:
                rejections["cooldown"] += 1
                continue
            if _has_three_segment_losses(trades, chosen, opened_at):
                rejections["three_loss_pause"] += 1
                continue

            selected = next(item for item in selected_profiles if item["key"] == _observation_profile_key(chosen))
            trades.append(
                {
                    "opened_at": chosen.opened_at,
                    "settled_at": chosen.settled_at,
                    "expires_at": chosen.expires_at,
                    "direction": chosen.direction,
                    "threshold_segment": chosen.threshold_segment,
                    "strategy_family": chosen.strategy_family,
                    "strategy_tag": chosen.strategy_tag,
                    "profile_key": selected["key"],
                    "training_samples": selected["sample_size"],
                    "training_win_rate": selected["win_rate"],
                    "training_ev": selected["ev"],
                    "result": chosen.result,
                    "pnl": float(chosen.pnl),
                }
            )
            last_order_opened_at = opened_at
            open_expiries.append(chosen.expires_at)

    compact_schedule = [_compact_snapshot(item) for item in snapshots]
    return {
        "config": config.normalized().__dict__,
        "execution": {
            "max_open_orders": max_open_orders,
            "min_order_gap_ms": min_order_gap_ms,
        },
        "data": {
            "settled_observations": len(settled),
            "first_observation": _iso(settled[0].opened_at),
            "last_observation": _iso(settled[-1].opened_at),
            "require_full_lookback": require_full_lookback,
            "out_of_sample_from": _iso(snapshots[0]["effective_from"]) if snapshots else None,
            "out_of_sample_until": _iso(min(snapshots[-1]["effective_until"], settled[-1].opened_at + 1))
            if snapshots
            else None,
        },
        "schedule": compact_schedule,
        "schedule_stats": _schedule_stats(compact_schedule),
        "eligible_events": eligible_events,
        "rejections": rejections,
        "trades": summarize_trades(trades),
        "by_direction": _group_summaries(trades, "direction"),
        "by_profile": _group_summaries(trades, "profile_key"),
        "by_day": _group_summaries(trades, "day"),
        "trade_rows": trades,
        "leakage_violations": _count_leakage_violations(settled, snapshots),
    }


def summarize_trades(trades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(trades, key=lambda item: item["opened_at"])
    wins = sum(1 for item in ordered if item["result"] == "WIN")
    losses = len(ordered) - wins
    pnl = round(sum(float(item["pnl"]) for item in ordered), 4)
    win_rate = wins / len(ordered) if ordered else 0.0
    low, high = _wilson_interval(wins, len(ordered))
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    max_loss_streak = 0
    loss_streak = 0
    for item in ordered:
        equity += float(item["pnl"])
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        if item["result"] == "LOSS":
            loss_streak += 1
            max_loss_streak = max(max_loss_streak, loss_streak)
        else:
            loss_streak = 0
    return {
        "orders": len(ordered),
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 6),
        "win_rate_ci95": [round(low, 6), round(high, 6)],
        "pnl": pnl,
        "ev": round(pnl / len(ordered), 4) if ordered else 0.0,
        "max_drawdown": round(max_drawdown, 4),
        "max_loss_streak": max_loss_streak,
        "first_trade": _iso(ordered[0]["opened_at"]) if ordered else None,
        "last_trade": _iso(ordered[-1]["opened_at"]) if ordered else None,
    }


def _build_schedule(
    observations: Sequence[ObservationSignal],
    config: DailyProfileSelectorConfig,
    *,
    require_full_lookback: bool,
) -> list[dict[str, Any]]:
    first_opened_at = observations[0].opened_at
    last_opened_at = observations[-1].opened_at
    current_date = datetime.fromtimestamp(first_opened_at / 1000, tz=SHANGHAI).date()
    end_date = datetime.fromtimestamp(last_opened_at / 1000, tz=SHANGHAI).date()
    previous = None
    snapshots = []
    while current_date <= end_date:
        evaluated_at = int(
            datetime.combine(
                current_date,
                time(config.evaluation_hour, config.evaluation_minute),
                tzinfo=SHANGHAI,
            ).timestamp()
            * 1000
        )
        window = selection_window(
            evaluated_at,
            lookback_days=config.lookback_days,
            evaluation_hour=config.evaluation_hour,
            evaluation_minute=config.evaluation_minute,
            activation_hour=config.activation_hour,
            activation_minute=config.activation_minute,
        )
        full_lookback = first_opened_at <= window["lookback_start"]
        if (
            (full_lookback or not require_full_lookback)
            and window["effective_from"] <= last_opened_at
        ):
            snapshot = build_daily_selection(
                observations,
                evaluated_at,
                config=config,
                previous_snapshot=previous,
            )
            snapshots.append(snapshot)
            previous = snapshot
        current_date += timedelta(days=1)
    return snapshots


def _compact_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": snapshot["version"],
        "evaluated_at": snapshot["evaluated_at"],
        "evaluated_at_local": _iso(snapshot["evaluated_at"]),
        "lookback_start": snapshot["lookback_start"],
        "lookback_end": snapshot["lookback_end"],
        "effective_from": snapshot["effective_from"],
        "effective_until": snapshot["effective_until"],
        "effective_from_local": _iso(snapshot["effective_from"]),
        "selected_count": snapshot["selected_count"],
        "selected_profiles": [
            {
                key: item[key]
                for key in (
                    "key",
                    "direction",
                    "threshold_segment",
                    "sample_size",
                    "wins",
                    "losses",
                    "win_rate",
                    "pnl",
                    "ev",
                    "selection_state",
                )
            }
            for item in snapshot.get("selected_profiles") or []
        ],
    }


def _schedule_stats(schedule: Sequence[dict[str, Any]]) -> dict[str, Any]:
    counts = [item["selected_count"] for item in schedule]
    activated = 0
    removed = 0
    previous: set[str] = set()
    for item in schedule:
        current = {profile["key"] for profile in item["selected_profiles"]}
        activated += len(current - previous)
        removed += len(previous - current)
        previous = current
    return {
        "evaluations": len(schedule),
        "average_selected": round(sum(counts) / len(counts), 4) if counts else 0.0,
        "min_selected": min(counts) if counts else 0,
        "max_selected": max(counts) if counts else 0,
        "activations": activated,
        "removals": removed,
    }


def _group_summaries(trades: Sequence[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in trades:
        key = (
            datetime.fromtimestamp(item["opened_at"] / 1000, tz=SHANGHAI).strftime("%Y-%m-%d")
            if field == "day"
            else str(item[field])
        )
        groups[key].append(item)
    rows = [{field: key, **summarize_trades(values)} for key, values in groups.items()]
    return sorted(rows, key=lambda item: item[field])


def _has_three_segment_losses(
    trades: Sequence[dict[str, Any]],
    candidate: ObservationSignal,
    opened_at: int,
) -> bool:
    day = opened_at // DAY_MS
    matching = [
        item
        for item in trades
        if item["settled_at"] is not None
        and item["settled_at"] <= opened_at
        and item["settled_at"] // DAY_MS == day
        and item["threshold_segment"] == candidate.threshold_segment
    ]
    consecutive = 0
    for item in sorted(matching, key=lambda row: row["settled_at"], reverse=True):
        if item["result"] != "LOSS":
            break
        consecutive += 1
    return consecutive >= 3


def _count_leakage_violations(
    observations: Sequence[ObservationSignal],
    snapshots: Sequence[dict[str, Any]],
) -> int:
    violations = 0
    for snapshot in snapshots:
        for selected in snapshot.get("selected_profiles") or []:
            rows = [
                item
                for item in observations
                if _observation_profile_key(item) == selected["key"]
                and snapshot["lookback_start"] <= item.opened_at < snapshot["lookback_end"]
                and item.settled_at is not None
                and item.settled_at < snapshot["lookback_end"]
            ]
            samples = []
            next_independent_at = 0
            for item in sorted(rows, key=lambda row: (row.opened_at, row.observation_key)):
                if item.opened_at < next_independent_at:
                    continue
                samples.append(item)
                next_independent_at = item.expires_at
            wins = sum(1 for item in samples if item.result == "WIN")
            pnl = round(sum(float(item.pnl) for item in samples), 4)
            if (
                len(samples) != selected["sample_size"]
                or wins != selected["wins"]
                or pnl != selected["pnl"]
            ):
                violations += 1
    return violations


def _observation_profile_key(item: ObservationSignal) -> str:
    return profile_key(
        item.timeframe_minutes,
        item.strategy_family,
        item.strategy_tag,
        item.direction,
        item.threshold_segment,
    )


def _wilson_interval(wins: int, count: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if count <= 0:
        return 0.0, 0.0
    ratio = wins / count
    denominator = 1.0 + z * z / count
    center = (ratio + z * z / (2 * count)) / denominator
    margin = z * math.sqrt(ratio * (1 - ratio) / count + z * z / (4 * count * count)) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def _iso(timestamp_ms: int | None) -> str | None:
    if timestamp_ms is None:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=SHANGHAI).isoformat(timespec="seconds")


def _empty_replay(
    config: DailyProfileSelectorConfig,
    *,
    max_open_orders: int,
    min_order_gap_ms: int,
) -> dict[str, Any]:
    return {
        "config": config.normalized().__dict__,
        "execution": {
            "max_open_orders": max(1, int(max_open_orders)),
            "min_order_gap_ms": max(0, int(min_order_gap_ms)),
        },
        "data": {"settled_observations": 0},
        "schedule": [],
        "schedule_stats": _schedule_stats([]),
        "eligible_events": 0,
        "rejections": {},
        "trades": summarize_trades([]),
        "by_direction": [],
        "by_profile": [],
        "by_day": [],
        "trade_rows": [],
        "leakage_violations": 0,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="严格走前回放每日观察画像选择器")
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--min-samples", type=int, default=20)
    parser.add_argument("--min-win-rate", type=float, default=0.60)
    parser.add_argument("--min-ev", type=float, default=0.0)
    parser.add_argument("--max-open-orders", type=int, default=5)
    parser.add_argument("--min-order-gap-minutes", type=float, default=2.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-partial-lookback", action="store_true")
    args = parser.parse_args(argv)
    config = DailyProfileSelectorConfig(
        min_samples=args.min_samples,
        min_win_rate=args.min_win_rate,
        min_ev=args.min_ev,
        exit_win_rate=args.min_win_rate,
        exit_ev=args.min_ev,
        degraded_runs_to_exit=1,
        max_active_profiles=0,
    )
    result = replay_daily_profile_selection(
        load_observations(args.db_path, args.symbol),
        config,
        require_full_lookback=not args.allow_partial_lookback,
        max_open_orders=args.max_open_orders,
        min_order_gap_ms=round(args.min_order_gap_minutes * 60_000),
    )
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
