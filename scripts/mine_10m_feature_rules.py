#!/usr/bin/env python3
import argparse
import json
import sys
import time
from collections import defaultdict, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backtest import load_klines_from_zips
from app.models import Kline
from app.research_strategy import BarFeatures, compute_features, event_contract_pnl, summarize_trades


SYMBOL = "BTCUSDT"
HORIZON_MINUTES = 10
MIN_GAP_MINUTES = 10


@dataclass(frozen=True)
class Rule:
    name: str
    direction: str
    predicates: tuple[str, ...]


def build_predicates() -> dict[str, Callable[[BarFeatures], bool]]:
    return {
        "vol>=1.2": lambda item: item.vol_ratio_5 >= 1.2,
        "vol<=0.8": lambda item: item.vol_ratio_5 <= 0.8,
        "ret5z>=1.0": lambda item: item.ret_5_z >= 1.0,
        "ret5z>=1.5": lambda item: item.ret_5_z >= 1.5,
        "ret5z<=-1.0": lambda item: item.ret_5_z <= -1.0,
        "ret5z<=-1.5": lambda item: item.ret_5_z <= -1.5,
        "rsi>=60": lambda item: item.rsi_14 >= 60.0,
        "rsi>=70": lambda item: item.rsi_14 >= 70.0,
        "rsi<=40": lambda item: item.rsi_14 <= 40.0,
        "rsi<=30": lambda item: item.rsi_14 <= 30.0,
        "boll>=0.65": lambda item: item.boll_pos_20 >= 0.65,
        "boll>=0.8": lambda item: item.boll_pos_20 >= 0.8,
        "boll<=0.35": lambda item: item.boll_pos_20 <= 0.35,
        "boll<=0.2": lambda item: item.boll_pos_20 <= 0.2,
        "upper_rejection": lambda item: item.upper_rejection,
        "lower_reclaim": lambda item: item.lower_reclaim,
        "break_up20": lambda item: item.break_up_20,
        "break_down20": lambda item: item.break_down_20,
        "compression30": lambda item: item.compression_30,
        "trend>=0.04": lambda item: item.trend_strength >= 0.04,
        "trend<=-0.04": lambda item: item.trend_strength <= -0.04,
        "ret1>0": lambda item: item.ret_1 > 0.0,
        "ret1<0": lambda item: item.ret_1 < 0.0,
        "ret10>=0": lambda item: item.ret_10 >= 0.0,
        "ret10<=0": lambda item: item.ret_10 <= 0.0,
        "close_strength<=0.35": lambda item: item.close_strength <= 0.35,
        "close_strength>=0.65": lambda item: item.close_strength >= 0.65,
    }


def candidate_rules() -> list[Rule]:
    long_pairs = [
        ("ret5z<=-1.0", "rsi<=40"),
        ("ret5z<=-1.0", "boll<=0.35"),
        ("ret5z<=-1.5", "lower_reclaim"),
        ("boll<=0.35", "lower_reclaim"),
        ("break_down20", "lower_reclaim"),
        ("trend>=0.04", "ret1>0"),
        ("trend>=0.04", "ret10>=0"),
        ("compression30", "break_up20"),
        ("vol>=1.2", "ret1>0"),
        ("vol<=0.8", "ret1>0"),
    ]
    short_pairs = [
        ("ret5z>=1.0", "rsi>=60"),
        ("ret5z>=1.0", "boll>=0.65"),
        ("ret5z>=1.5", "upper_rejection"),
        ("boll>=0.65", "upper_rejection"),
        ("break_up20", "upper_rejection"),
        ("trend<=-0.04", "ret1<0"),
        ("trend<=-0.04", "ret10<=0"),
        ("compression30", "break_down20"),
        ("vol>=1.2", "ret1<0"),
        ("vol<=0.8", "ret1<0"),
    ]
    rules = []
    for direction, pairs in (("LONG", long_pairs), ("SHORT", short_pairs)):
        for pair in pairs:
            rules.append(Rule(f"{direction.lower()}__{'__'.join(pair)}", direction, pair))
    return rules


def rule_trades(
    rule: Rule,
    klines: Sequence[Kline],
    features: Sequence[BarFeatures],
    predicates: dict[str, Callable[[BarFeatures], bool]],
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
    horizon_minutes: int = HORIZON_MINUTES,
    min_gap_minutes: int = MIN_GAP_MINUTES,
) -> list[dict[str, Any]]:
    trades = []
    last_entry_time: int | None = None
    min_gap_ms = min_gap_minutes * 60_000
    funcs = [predicates[name] for name in rule.predicates]
    for item in features:
        if item.index < 260 or item.index + horizon_minutes >= len(klines):
            continue
        if start_ms is not None and item.close_time < start_ms:
            continue
        if end_ms is not None and item.close_time >= end_ms:
            continue
        if last_entry_time is not None and item.close_time - last_entry_time < min_gap_ms:
            continue
        if not all(func(item) for func in funcs):
            continue
        entry = klines[item.index]
        exit_bar = klines[item.index + horizon_minutes]
        result, pnl = event_contract_pnl(rule.direction, entry.close, exit_bar.close)
        trades.append(
            {
                "rule": rule.name,
                "direction": rule.direction,
                "entry_time": entry.close_time,
                "entry_time_utc": _iso(entry.close_time),
                "entry_price": entry.close,
                "exit_time": exit_bar.close_time,
                "exit_price": exit_bar.close,
                "result": result,
                "pnl": pnl,
                "beijing_bucket": item.beijing_bucket,
                "beijing_hour": item.beijing_hour,
                "utc_hour": item.utc_hour,
                "is_weekend": item.is_weekend,
                "ret_5_z": round(item.ret_5_z, 4),
                "rsi_14": round(item.rsi_14, 4),
                "boll_pos_20": round(item.boll_pos_20, 4),
                "vol_ratio_5": round(item.vol_ratio_5, 4),
            }
        )
        last_entry_time = item.close_time
    return trades


def evaluate_rules(
    klines: Sequence[Kline],
    features: Sequence[BarFeatures],
    *,
    train_start_ms: int,
    split_ms: int,
    end_ms: int,
) -> list[dict[str, Any]]:
    predicates = build_predicates()
    rows = []
    for rule in candidate_rules():
        train = rule_trades(rule, klines, features, predicates, start_ms=train_start_ms, end_ms=split_ms)
        test = rule_trades(rule, klines, features, predicates, start_ms=split_ms, end_ms=end_ms)
        all_trades = rule_trades(rule, klines, features, predicates, start_ms=train_start_ms, end_ms=end_ms)
        train_stats = summarize_trades(train)
        test_stats = summarize_trades(test)
        all_stats = summarize_trades(all_trades)
        rows.append(
            {
                "rule": rule.name,
                "direction": rule.direction,
                "predicates": list(rule.predicates),
                "train": train_stats,
                "test": test_stats,
                "all": all_stats,
                "test_by_bucket_top": _top_grouped(test, "beijing_bucket"),
                "walk_forward": walk_forward_rule_guard(all_trades, split_ms),
                "score": _score(train_stats, test_stats),
            }
        )
    return sorted(rows, key=lambda item: item["score"], reverse=True)


def walk_forward_rule_guard(
    trades: Sequence[dict[str, Any]],
    start_ms: int,
    *,
    lookback_days: int = 60,
    min_samples: int = 20,
    min_win_rate: float = 0.58,
    min_avg_pnl: float = 0.25,
) -> dict[str, Any]:
    history: deque[dict[str, Any]] = deque()
    traded = []
    lookback_ms = lookback_days * 86_400_000
    rejected = {"not_enough_samples": 0, "edge_degraded": 0}
    history_wins = 0
    history_pnl = 0.0
    for trade in sorted(trades, key=lambda item: item["entry_time"]):
        while history and history[0]["entry_time"] < trade["entry_time"] - lookback_ms:
            expired = history.popleft()
            if expired["result"] == "WIN":
                history_wins -= 1
            history_pnl -= float(expired["pnl"])
        sample_size = len(history)
        win_rate = history_wins / sample_size if sample_size else 0.0
        avg_pnl = history_pnl / sample_size if sample_size else 0.0
        allowed = (
            sample_size >= min_samples
            and win_rate >= min_win_rate
            and avg_pnl >= min_avg_pnl
        )
        if trade["entry_time"] >= start_ms:
            if allowed:
                enriched = dict(trade)
                enriched["rolling_sample_size"] = sample_size
                enriched["rolling_win_rate"] = round(win_rate, 4)
                enriched["rolling_avg_pnl"] = round(avg_pnl, 4)
                traded.append(enriched)
            elif sample_size < min_samples:
                rejected["not_enough_samples"] += 1
            else:
                rejected["edge_degraded"] += 1
        history.append(trade)
        if trade["result"] == "WIN":
            history_wins += 1
        history_pnl += float(trade["pnl"])
    return {
        "config": {
            "lookback_days": lookback_days,
            "min_samples": min_samples,
            "min_win_rate": min_win_rate,
            "min_avg_pnl": min_avg_pnl,
        },
        "traded": summarize_trades(traded),
        "rejected": rejected,
    }


def run_mining(data_dir: Path, report_dir: Path, start: str, split: str, end: str) -> dict[str, Any]:
    started = time.perf_counter()
    zip_paths = sorted(data_dir.glob(f"{SYMBOL}-1m-*.zip"))
    klines = load_klines_from_zips(zip_paths)
    start_ms = _parse_date(start)
    split_ms = _parse_date(split)
    end_ms = _parse_date(end, end_of_day=True)
    klines = [item for item in klines if start_ms <= item.close_time <= end_ms]
    features = compute_features(klines)
    rows = evaluate_rules(
        klines,
        features,
        train_start_ms=start_ms,
        split_ms=split_ms,
        end_ms=end_ms,
    )
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbol": SYMBOL,
        "from": _iso(klines[0].close_time) if klines else "",
        "split": _iso(split_ms),
        "to": _iso(klines[-1].close_time) if klines else "",
        "klines": len(klines),
        "method": "Mine simple 10m feature rules on train period and verify on later out-of-sample period.",
        "rules_tested": len(rows),
        "top": rows[:20],
        "all": rows,
        "elapsed_seconds": round(time.perf_counter() - started, 4),
    }
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"10m_feature_rule_mining_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def print_summary(report: dict[str, Any]) -> None:
    print("=== 10分钟特征规则挖掘 ===")
    print(f"数据: {report['from']} -> {report['to']} split={report['split']} klines={report['klines']}")
    print(f"规则数: {report['rules_tested']} 报告: {report['report_path']}")
    print("\nTop 12:")
    for item in report["top"][:12]:
        test = item["test"]
        wf = item["walk_forward"]["traded"]
        print(
            f"- {item['rule']} {item['direction']} "
            f"train={item['train']['total_orders']}/{item['train']['win_rate']:.2%}/{item['train']['balance']:.1f} "
            f"test={test['total_orders']}/{test['win_rate']:.2%}/{test['balance']:.1f}/ev{test['avg_pnl']:.2f} "
            f"wf={wf['total_orders']}/{wf['win_rate']:.2%}/{wf['balance']:.1f}/ev{wf['avg_pnl']:.2f}"
        )


def _top_grouped(trades: Sequence[dict[str, Any]], key: str, limit: int = 8) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        groups[str(trade[key])].append(trade)
    rows = []
    for name, items in groups.items():
        stats = summarize_trades(items)
        stats["key"] = name
        rows.append(stats)
    return sorted(rows, key=lambda item: item["balance"], reverse=True)[:limit]


def _score(train: dict[str, Any], test: dict[str, Any]) -> float:
    if train["total_orders"] < 100 or test["total_orders"] < 40:
        return -1_000_000 + train["total_orders"] + test["total_orders"]
    if train["avg_pnl"] <= 0 or test["avg_pnl"] <= 0:
        return -100_000 + test["balance"]
    return (
        test["balance"]
        + test["avg_pnl"] * 200.0
        + (test["win_rate"] - 10.0 / 18.0) * 300.0
        - abs(test["max_drawdown"]) * 0.08
        - test["max_loss_streak"] * 4.0
    )


def _parse_date(value: str, *, end_of_day: bool = False) -> int:
    dt = datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    ms = int(dt.timestamp() * 1000)
    return ms + 86_400_000 - 1 if end_of_day and len(value) == 10 else ms


def _iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).isoformat()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mine simple 10m feature rules with out-of-sample validation.")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--report-dir", type=Path, default=ROOT / "reports")
    parser.add_argument("--start", default="2024-06-18")
    parser.add_argument("--split", default="2025-06-18")
    parser.add_argument("--end", default="2026-06-17")
    args = parser.parse_args(argv)

    report = run_mining(args.data_dir, args.report_dir, args.start, args.split, args.end)
    print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
