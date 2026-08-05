#!/usr/bin/env python3
from __future__ import annotations

import argparse
import heapq
import json
from bisect import bisect_left
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

from app.backtest import load_klines_from_zips
from app.market_sequence import (
    MarketSequenceConfig,
    SequenceTrainingRow,
    build_snapshot_from_rows,
    build_state_feature_series,
    selection_window,
)
from app.models import Kline


SHANGHAI = ZoneInfo("Asia/Shanghai")


def summarize_trades(trades: Sequence[dict]) -> dict:
    wins = sum(item.get("result") == "WIN" for item in trades)
    pnl = round(sum(float(item.get("pnl", 0.0)) for item in trades), 4)
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    loss_streak = 0
    max_loss_streak = 0
    for item in sorted(trades, key=lambda row: (int(row.get("settled_at", 0)), int(row.get("entry_time", 0)))):
        equity += float(item.get("pnl", 0.0))
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)
        if item.get("result") == "LOSS":
            loss_streak += 1
            max_loss_streak = max(max_loss_streak, loss_streak)
        else:
            loss_streak = 0
    count = len(trades)
    return {
        "orders": count,
        "wins": wins,
        "losses": count - wins,
        "win_rate": round(wins / count, 6) if count else 0.0,
        "pnl": pnl,
        "ev": round(pnl / count, 6) if count else 0.0,
        "max_drawdown": round(max_drawdown, 4),
        "max_loss_streak": max_loss_streak,
    }


def apply_exposure_limit(
    candidates: Sequence[dict],
    *,
    max_open_orders: int,
    min_gap_minutes: float,
) -> list[dict]:
    accepted = []
    open_expiries: list[int] = []
    last_entry: int | None = None
    min_gap_ms = round(max(0.0, float(min_gap_minutes)) * 60_000)
    for candidate in sorted(candidates, key=lambda item: (int(item["entry_time"]), str(item.get("state_key", "")))):
        entry_time = int(candidate["entry_time"])
        while open_expiries and open_expiries[0] <= entry_time:
            heapq.heappop(open_expiries)
        if len(open_expiries) >= max(1, int(max_open_orders)):
            continue
        if last_entry is not None and entry_time - last_entry < min_gap_ms:
            continue
        accepted.append(dict(candidate))
        heapq.heappush(open_expiries, int(candidate["expires_at"]))
        last_entry = entry_time
    return accepted


def split_train_holdout_days(days: Sequence[str], holdout_ratio: float = 0.20) -> tuple[list[str], list[str]]:
    ordered = sorted(set(days))
    if len(ordered) < 2:
        return ordered, []
    holdout_count = max(1, round(len(ordered) * min(0.5, max(0.05, holdout_ratio))))
    holdout_count = min(len(ordered) - 1, holdout_count)
    return ordered[:-holdout_count], ordered[-holdout_count:]


def select_parameter_from_training(results: Sequence[dict], *, train_days: set[str]) -> dict:
    ranked = []
    for result in results:
        trades = [
            trade
            for day, rows in result.get("by_day", {}).items()
            if day in train_days
            for trade in rows
        ]
        summary = summarize_trades(trades)
        ranked.append(({**result, "training_summary": summary}, summary))
    if not ranked:
        raise ValueError("parameter results cannot be empty")
    return max(
        ranked,
        key=lambda item: (
            item[1]["pnl"],
            item[1]["ev"],
            item[1]["max_drawdown"],
            item[1]["orders"],
            item[0].get("name", ""),
        ),
    )[0]


def prepare_replay_data(
    klines: Sequence[Kline],
    config: MarketSequenceConfig | None = None,
) -> dict:
    resolved = (config or MarketSequenceConfig(key_mode="move_run_volume_rsi")).normalized()
    ordered = sorted(klines, key=lambda item: item.close_time)
    close_by_time = {item.close_time: item for item in ordered}
    feature_config = replace_config(resolved, key_mode="move_run_volume_rsi")
    feature_series = build_state_feature_series(ordered, config=feature_config)
    training_rows = {mode: [] for mode in ("move_run", "move_run_volume", "move_run_volume_rsi")}
    points = []
    windows = {}
    for index, item in enumerate(ordered):
        features = feature_series[index]
        if features is None:
            continue
        expires_at = item.close_time + resolved.horizon_minutes * 60_000
        exit_kline = close_by_time.get(expires_at)
        if exit_kline is None:
            continue
        keys = {
            "move_run": f"{features['move']}|{features['run_bucket']}",
            "move_run_volume": f"{features['move']}|{features['run_bucket']}|{features['volume_bucket']}",
            "move_run_volume_rsi": features["state_key"],
        }
        won_long = exit_kline.close > item.close
        outcome = "UP" if won_long else "DOWN"
        if (item.close_time + 1) % (resolved.training_stride_minutes * 60_000) == 0:
            for mode, key in keys.items():
                training_rows[mode].append(
                    SequenceTrainingRow(
                        entry_time=item.close_time,
                        settled_at=expires_at,
                        state_key=key,
                        outcome=outcome,
                    )
                )
        if ((item.close_time + 1) // 60_000) % resolved.entry_stride_minutes != 0:
            continue
        target = selection_window(item.close_time, resolved)
        effective_from = int(target["effective_from"])
        windows[effective_from] = target
        points.append(
            {
                "entry_time": item.close_time,
                "expires_at": expires_at,
                "entry_price": item.close,
                "exit_price": exit_kline.close,
                "outcome": outcome,
                "keys": keys,
                "effective_from": effective_from,
                "day": local_day(item.close_time),
            }
        )
    return {
        "ordered": ordered,
        "training_rows": training_rows,
        "training_entry_times": {
            mode: [row.entry_time for row in rows]
            for mode, rows in training_rows.items()
        },
        "points": points,
        "windows": windows,
    }


def generate_candidates(
    klines: Sequence[Kline],
    config: MarketSequenceConfig,
    *,
    prepared: dict | None = None,
) -> dict:
    resolved = config.normalized()
    prepared = prepared or prepare_replay_data(klines, resolved)
    source_rows = prepared["training_rows"][resolved.key_mode]
    entry_times = prepared["training_entry_times"][resolved.key_mode]
    snapshots: dict[int, dict] = {}
    for effective_from, target in sorted(prepared["windows"].items()):
        left = bisect_left(entry_times, int(target["lookback_start"]))
        upper_entry = int(target["lookback_end"]) - resolved.horizon_minutes * 60_000
        right = bisect_left(entry_times, upper_entry)
        snapshots[effective_from] = {
            **build_snapshot_from_rows(
                source_rows[left:right],
                evaluated_at=int(target["lookback_end"]),
                effective_from=effective_from,
                effective_until=int(target["effective_until"]),
                config=resolved,
            ),
            "lookback_start": int(target["lookback_start"]),
            "lookback_end": int(target["lookback_end"]),
        }

    candidates = []
    for point in prepared["points"]:
        snapshot = snapshots[point["effective_from"]]
        key = point["keys"][resolved.key_mode]
        selected_state = snapshot.get("selected_states", {}).get(key)
        if not selected_state or selected_state["direction"] not in {"LONG", "SHORT"}:
            continue
        won = point["outcome"] == ("UP" if selected_state["direction"] == "LONG" else "DOWN")
        candidates.append(
            {
                "entry_time": point["entry_time"],
                "expires_at": point["expires_at"],
                "settled_at": point["expires_at"],
                "entry_price": point["entry_price"],
                "exit_price": point["exit_price"],
                "direction": selected_state["direction"],
                "result": "WIN" if won else "LOSS",
                "pnl": 8.0 if won else -10.0,
                "state_key": key,
                "sample_size": int(selected_state["sample_size"]),
                "profile_win_rate": float(selected_state["win_rate"]),
                "profile_ev": float(selected_state["ev"]),
                "snapshot_version": snapshot["version"],
                "day": point["day"],
            }
        )
    return {"candidates": candidates, "snapshots": list(snapshots.values())}


def replay_config(
    klines: Sequence[Kline],
    config: MarketSequenceConfig,
    *,
    prepared: dict | None = None,
) -> dict:
    generated = generate_candidates(klines, config, prepared=prepared)
    candidates = generated["candidates"]
    exposures = {}
    for max_open in (1, 2, 5):
        trades = apply_exposure_limit(candidates, max_open_orders=max_open, min_gap_minutes=2)
        exposures[str(max_open)] = result_view(trades)
    five = exposures["5"]["trades"]
    return {
        "name": config_name(config),
        "config": asdict(config.normalized()),
        "candidate_summary": summarize_trades(candidates),
        "exposures": exposures,
        "by_day": _group_rows(five, lambda item: item["day"]),
        "snapshots": generated["snapshots"],
    }


def parameter_sweep(
    klines: Sequence[Kline],
    configs: Sequence[MarketSequenceConfig],
    *,
    holdout_ratio: float = 0.20,
) -> dict:
    days = sorted({local_day(item.close_time) for item in klines})
    train_days, holdout_days = split_train_holdout_days(days, holdout_ratio)
    prepared = prepare_replay_data(klines, configs[0] if configs else None)
    results = [replay_config(klines, config, prepared=prepared) for config in configs]
    selected = select_parameter_from_training(results, train_days=set(train_days))
    selected_trades = selected["exposures"]["5"]["trades"]
    training = [item for item in selected_trades if item["day"] in set(train_days)]
    holdout = [item for item in selected_trades if item["day"] in set(holdout_days)]
    compact_results = []
    for result in results:
        trades = result["exposures"]["5"]["trades"]
        compact_results.append(
            {
                "name": result["name"],
                "config": result["config"],
                "training": summarize_trades([item for item in trades if item["day"] in set(train_days)]),
                "holdout": summarize_trades([item for item in trades if item["day"] in set(holdout_days)]),
            }
        )
    compact_results.sort(key=lambda item: (-item["training"]["pnl"], -item["training"]["ev"], item["name"]))
    return {
        "coverage": {
            "klines": len(klines),
            "first": ordered_time(klines, first=True),
            "last": ordered_time(klines, first=False),
            "train_days": train_days,
            "holdout_days": holdout_days,
            "rules": "10m horizon; +8U/-10U; daily 07:50 evaluation; 08:00 activation; no future labels",
        },
        "selected": {
            "name": selected["name"],
            "config": selected["config"],
            "training": summarize_trades(training),
            "holdout": summarize_trades(holdout),
            "exposures": {
                key: {name: value for name, value in view.items() if name != "trades"}
                for key, view in selected["exposures"].items()
            },
            "by_direction": _summary_groups(selected_trades, lambda item: item["direction"]),
            "by_state": _summary_groups(selected_trades, lambda item: item["state_key"]),
            "by_day": _summary_groups(selected_trades, lambda item: item["day"]),
        },
        "parameter_results": compact_results,
    }


def result_view(trades: Sequence[dict]) -> dict:
    return {
        **summarize_trades(trades),
        "by_direction": _summary_groups(trades, lambda item: item["direction"]),
        "by_state": _summary_groups(trades, lambda item: item["state_key"]),
        "by_day": _summary_groups(trades, lambda item: item["day"]),
        "trades": list(trades),
    }


def default_configs() -> list[MarketSequenceConfig]:
    return [
        MarketSequenceConfig(key_mode=key_mode, min_samples=min_samples, min_win_rate=min_win_rate)
        for key_mode in ("move_run", "move_run_volume", "move_run_volume_rsi")
        for min_samples in (12, 20, 30)
        for min_win_rate in (0.58, 0.60, 0.62)
    ]


def config_name(config: MarketSequenceConfig) -> str:
    resolved = config.normalized()
    return f"{resolved.key_mode}_n{resolved.min_samples}_p{resolved.min_win_rate:.2f}"


def replace_config(config: MarketSequenceConfig, **changes) -> MarketSequenceConfig:
    values = asdict(config)
    values.update(changes)
    return MarketSequenceConfig(**values)


def local_day(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=SHANGHAI).strftime("%Y-%m-%d")


def ordered_time(klines: Sequence[Kline], *, first: bool) -> str:
    if not klines:
        return ""
    item = min(klines, key=lambda row: row.close_time) if first else max(klines, key=lambda row: row.close_time)
    return datetime.fromtimestamp(item.close_time / 1000, tz=SHANGHAI).isoformat()


def _group_rows(rows: Iterable[dict], key) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(str(key(row)), []).append(row)
    return groups


def _summary_groups(rows: Iterable[dict], key) -> dict[str, dict]:
    return {name: summarize_trades(group) for name, group in sorted(_group_rows(rows, key).items())}


def main() -> int:
    parser = argparse.ArgumentParser(description="10分钟市场序列策略严格滚动回放")
    parser.add_argument("--data-dir", type=Path, required=True, help="包含 Binance 1分钟K线zip的目录")
    parser.add_argument("--report", type=Path, required=True, help="详细JSON报告输出路径")
    parser.add_argument("--holdout-ratio", type=float, default=0.20, help="末段留出日期比例，默认: 0.20")
    args = parser.parse_args()
    paths = sorted(args.data_dir.glob("BTCUSDT-1m-*.zip"))
    if not paths:
        raise SystemExit(f"没有找到K线zip: {args.data_dir}")
    klines = load_klines_from_zips(paths)
    report = parameter_sweep(klines, default_configs(), holdout_ratio=args.holdout_ratio)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"coverage": report["coverage"], "selected": report["selected"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
