#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from app.result_sequence_guard import (
    ResultSequenceGuardConfig,
    evaluate_result_sequence_guard,
)
from scripts.analyze_monitor_db import load_order_samples


SHANGHAI = ZoneInfo("Asia/Shanghai")


def summarize(rows: Sequence[dict]) -> dict:
    wins = sum(item.get("result") == "WIN" for item in rows)
    count = len(rows)
    pnl = round(sum(8.0 if item.get("result") == "WIN" else -10.0 for item in rows), 4)
    return {
        "orders": count,
        "wins": wins,
        "losses": count - wins,
        "win_rate": round(wins / count, 6) if count else 0.0,
        "pnl": pnl,
        "ev": round(pnl / count, 6) if count else 0.0,
    }


def replay_guard(
    rows: Sequence[dict],
    *,
    config: ResultSequenceGuardConfig,
) -> dict:
    accepted: list[dict] = []
    blocked: list[dict] = []
    for raw in sorted(rows, key=lambda item: (int(item.get("opened_at") or 0), int(item.get("order_id") or 0))):
        item = dict(raw)
        decision = evaluate_result_sequence_guard(
            accepted,
            current_time=int(item.get("opened_at") or 0),
            direction=str(item.get("direction") or ""),
            config=config,
        )
        if decision.blocked:
            blocked.append(
                {
                    **item,
                    "guard_reason": decision.reason,
                    "guard_pause_until": decision.pause_until,
                }
            )
        else:
            accepted.append(item)
    return {
        "config": asdict(config.normalized()),
        "accepted": accepted,
        "blocked": blocked,
        "accepted_summary": summarize(accepted),
        "blocked_summary": summarize(blocked),
        "baseline_summary": summarize(list(rows)),
    }


def parameter_sweep(rows: Sequence[dict], *, holdout_days: int = 2) -> dict:
    settled = [dict(item) for item in rows if item.get("result") in {"WIN", "LOSS"}]
    days = sorted({_local_day(int(item.get("opened_at") or 0)) for item in settled})
    holdout_count = min(max(1, int(holdout_days)), max(1, len(days) - 1)) if len(days) > 1 else 0
    validation_days = set(days[-holdout_count:]) if holdout_count else set()
    training_days = set(days) - validation_days
    results = []
    for scope in ("GLOBAL", "DIRECTION"):
        for loss_streak in (2, 3):
            for cooldown_minutes in (10, 20, 30, 60):
                config = ResultSequenceGuardConfig(
                    loss_streak=loss_streak,
                    cooldown_minutes=cooldown_minutes,
                    scope=scope,
                )
                replay = replay_guard(settled, config=config)
                accepted_ids = {int(item.get("order_id") or 0) for item in replay["accepted"]}
                result = {
                    "name": f"{scope}_L{loss_streak}_C{cooldown_minutes}",
                    "config": replay["config"],
                    "all": _split_summary(settled, accepted_ids, None),
                    "training": _split_summary(settled, accepted_ids, training_days),
                    "validation": _split_summary(settled, accepted_ids, validation_days),
                }
                results.append(result)
    ranked = sorted(
        results,
        key=lambda item: (
            item["training"]["delta_pnl"],
            item["training"]["retention_rate"],
            item["validation"]["delta_pnl"],
            item["name"],
        ),
        reverse=True,
    )
    return {
        "days": days,
        "training_days": sorted(training_days),
        "validation_days": sorted(validation_days),
        "baseline": summarize(settled),
        "selected_by_training": ranked[0] if ranked else None,
        "results": ranked,
    }


def _split_summary(rows: Sequence[dict], accepted_ids: set[int], days: set[str] | None) -> dict:
    source = [
        item
        for item in rows
        if days is None or _local_day(int(item.get("opened_at") or 0)) in days
    ]
    accepted = [item for item in source if int(item.get("order_id") or 0) in accepted_ids]
    blocked = [item for item in source if int(item.get("order_id") or 0) not in accepted_ids]
    baseline = summarize(source)
    traded = summarize(accepted)
    return {
        "baseline": baseline,
        "traded": traded,
        "blocked": summarize(blocked),
        "delta_pnl": round(traded["pnl"] - baseline["pnl"], 4),
        "retention_rate": round(len(accepted) / len(source), 6) if source else 0.0,
    }


def _local_day(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=SHANGHAI).strftime("%Y-%m-%d")


def main() -> int:
    parser = argparse.ArgumentParser(description="严格因果的结算序列冷却守卫回放")
    parser.add_argument("--db-path", type=Path, required=True, help="SQLite 数据库路径")
    parser.add_argument("--symbol", default="BTCUSDT", help="交易对，默认: BTCUSDT")
    parser.add_argument("--holdout-days", type=int, default=2, help="末段验证天数，默认: 2")
    parser.add_argument("--report", type=Path, help="可选 JSON 报告路径")
    args = parser.parse_args()
    rows = load_order_samples(args.db_path, args.symbol)
    report = parameter_sweep(rows, holdout_days=args.holdout_days)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
