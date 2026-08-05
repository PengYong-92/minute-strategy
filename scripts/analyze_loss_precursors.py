#!/usr/bin/env python3
import argparse
import json
import sys
from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from app.order_profile import BASE_STAKE, STAKE_MULTIPLIER, WIN_PAYOUT_RATE, risk_hint_keys_for_sample
from scripts.analyze_monitor_db import DEFAULT_DB_PATH, DEFAULT_REPORT_DIR, load_order_samples


DAY_MS = 86_400_000
MAX_STAKE_STEP = 3


@dataclass(frozen=True)
class PrecursorConfig:
    lookback_days: int = 7
    min_samples: int = 5
    degraded_win_rate: float = 0.5
    degraded_ev: float = 0.0
    global_loss_streak: int = 2
    key_loss_streak: int = 2
    top: int = 12


def analyze_loss_precursors(
    samples: Sequence[Mapping[str, Any]],
    *,
    config: PrecursorConfig | None = None,
) -> dict[str, Any]:
    config = config or PrecursorConfig()
    settled = _settled_samples(samples)
    annotations = annotate_orders(settled, config=config)
    warning_keys = _summarize_warning_keys(annotations)
    replay_candidates = _guard_replay_candidates(annotations, warning_keys)
    reverse_candidates = _reverse_direction_candidates(warning_keys)
    losses = [item for item in annotations if item["result"] == "LOSS"]
    wins = [item for item in annotations if item["result"] == "WIN"]
    warned_losses = [item for item in losses if item["warning_keys"]]
    warned_wins = [item for item in wins if item["warning_keys"]]
    prior_warned_losses = [item for item in losses if item["prior_warning_keys"]]
    prior_warned_wins = [item for item in wins if item["prior_warning_keys"]]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": asdict(config),
        "sample_count": len(settled),
        "range": _sample_range(settled),
        "baseline": {
            "recorded": _summary(settled),
            "rolling_recomputed": _rolling_summary(settled),
            "reverse_direction_fixed_stake": _reverse_summary(annotations),
        },
        "by_direction": _group_summaries(settled, lambda item: str(item.get("direction") or "UNKNOWN")),
        "by_reason_setup": _group_summaries(settled, lambda item: str(item.get("reason_setup") or "UNKNOWN")),
        "by_segment": _group_summaries(settled, lambda item: str(item.get("threshold_segment") or "UNKNOWN")),
        "by_prior_global_loss_streak": _group_summaries(
            annotations,
            lambda item: _streak_bucket(int(item["prior_global_loss_streak"])),
        ),
        "warning_keys": warning_keys[: config.top],
        "replay_candidates": replay_candidates[: config.top],
        "reverse_direction_candidates": reverse_candidates[: config.top],
        "wrong_release": {
            "losses": len(losses),
            "warned_losses": len(warned_losses),
            "warned_loss_rate": round(len(warned_losses) / len(losses), 6) if losses else 0.0,
            "prior_warned_losses": len(prior_warned_losses),
            "prior_warned_loss_rate": round(len(prior_warned_losses) / len(losses), 6) if losses else 0.0,
            "unwarned_losses": len(losses) - len(warned_losses),
            "warned_loss_pnl": round(sum(float(item.get("pnl") or 0.0) for item in warned_losses), 4),
            "prior_warned_loss_pnl": round(sum(float(item.get("pnl") or 0.0) for item in prior_warned_losses), 4),
            "top": _compact_orders(warned_losses[-10:]),
        },
        "wrong_block": {
            "wins": len(wins),
            "warned_wins": len(warned_wins),
            "warned_win_rate": round(len(warned_wins) / len(wins), 6) if wins else 0.0,
            "prior_warned_wins": len(prior_warned_wins),
            "prior_warned_win_rate": round(len(prior_warned_wins) / len(wins), 6) if wins else 0.0,
            "warned_win_pnl": round(sum(float(item.get("pnl") or 0.0) for item in warned_wins), 4),
            "prior_warned_win_pnl": round(sum(float(item.get("pnl") or 0.0) for item in prior_warned_wins), 4),
            "top": _compact_orders(warned_wins[-10:]),
        },
        "tail": _compact_orders(annotations[-10:]),
    }


def annotate_orders(
    samples: Sequence[Mapping[str, Any]],
    *,
    config: PrecursorConfig | None = None,
) -> list[dict[str, Any]]:
    config = config or PrecursorConfig()
    lookback_ms = max(1, config.lookback_days) * DAY_MS
    history_by_key: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    loss_streak_by_key: dict[str, int] = defaultdict(int)
    prior_global_loss_streak = 0
    annotations: list[dict[str, Any]] = []

    for raw_sample in _settled_samples(samples):
        sample = dict(raw_sample)
        opened_at = int(sample.get("opened_at") or 0)
        risk_keys = risk_hint_keys_for_sample(sample)
        profile_keys = _profile_shadow_keys(sample)
        feature_keys = _feature_keys(sample, risk_keys)
        prior_warning_keys: list[str] = []
        current_risk_warning_keys: list[str] = []
        warning_details: list[dict[str, Any]] = []

        if prior_global_loss_streak >= config.global_loss_streak:
            prior_warning_keys.append(f"LOSS_STREAK:GLOBAL>={config.global_loss_streak}")
            warning_details.append(
                {
                    "key": prior_warning_keys[-1],
                    "orders": prior_global_loss_streak,
                    "scope": "GLOBAL",
                    "type": "LOSS_STREAK",
                }
            )

        for key in feature_keys:
            _trim_history(history_by_key[key], opened_at - lookback_ms)
            prior_rows = list(history_by_key[key])
            prior_stats = _summary(prior_rows, key)
            if loss_streak_by_key[key] >= config.key_loss_streak:
                prior_warning_keys.append(f"LOSS_STREAK:{key}>={config.key_loss_streak}")
                warning_details.append(
                    {
                        "key": prior_warning_keys[-1],
                        "orders": loss_streak_by_key[key],
                        "scope": key,
                        "type": "LOSS_STREAK",
                    }
                )
            if _is_degraded(prior_stats, config):
                prior_warning_keys.append(f"DEGRADED:{key}")
                warning_details.append(
                    {
                        "key": prior_warning_keys[-1],
                        "scope": key,
                        "type": "DEGRADED",
                        "orders": prior_stats["orders"],
                        "win_rate": prior_stats["win_rate"],
                        "ev": prior_stats["ev"],
                        "pnl": prior_stats["pnl"],
                    }
                )

        for risk_key in risk_keys:
            current_risk_warning_keys.append(f"RISK_NOW:{risk_key}")
            warning_details.append({"key": current_risk_warning_keys[-1], "scope": risk_key, "type": "RISK_NOW"})

        for profile_key in profile_keys:
            prior_warning_keys.append(profile_key)
            warning_details.append({"key": profile_key, "scope": profile_key, "type": "PROFILE_SHADOW"})

        result = str(sample.get("result") or "")
        warning_keys = sorted(set(prior_warning_keys + current_risk_warning_keys))
        annotated = {
            **sample,
            "risk_hint_keys": risk_keys,
            "feature_keys": feature_keys,
            "prior_warning_keys": sorted(set(prior_warning_keys)),
            "current_risk_warning_keys": sorted(set(current_risk_warning_keys)),
            "warning_keys": warning_keys,
            "warning_details": warning_details,
            "prior_global_loss_streak": prior_global_loss_streak,
            "reverse_result": _reverse_result(sample),
            "reverse_pnl": _fixed_pnl(_reverse_result(sample)),
        }
        annotations.append(annotated)

        for key in feature_keys:
            history_by_key[key].append(annotated)
            if result == "LOSS":
                loss_streak_by_key[key] += 1
            else:
                loss_streak_by_key[key] = 0
        prior_global_loss_streak = prior_global_loss_streak + 1 if result == "LOSS" else 0

    return annotations


def print_summary(report: Mapping[str, Any]) -> None:
    config = report["config"]
    baseline = report["baseline"]
    recorded = baseline["recorded"]
    rolling = baseline["rolling_recomputed"]
    reverse = baseline["reverse_direction_fixed_stake"]
    print("=== 连续亏损 / 错放前兆分析 ===")
    print(
        f"样本: {report['sample_count']} "
        f"区间: {report['range']['from'] or '-'} -> {report['range']['to'] or '-'}"
    )
    print(
        f"配置: lookback={config['lookback_days']}天 min_samples={config['min_samples']} "
        f"degraded_win<{config['degraded_win_rate']:.2%} degraded_ev<{config['degraded_ev']:.2f}"
    )
    print(
        f"实盘记录: orders={recorded['orders']} win={recorded['win_rate']:.2%} "
        f"pnl={recorded['pnl']:.2f} ev={recorded['ev']:.2f}"
    )
    print(
        f"滚单重算: orders={rolling['orders']} win={rolling['win_rate']:.2%} "
        f"pnl={rolling['pnl']:.2f} ev={rolling['ev']:.2f} roi={rolling['roi']:.2%}"
    )
    print(
        f"同点位反向固定金额: orders={reverse['orders']} win={reverse['win_rate']:.2%} "
        f"pnl={reverse['pnl']:.2f} ev={reverse['ev']:.2f}"
    )

    print("\n方向 / 原因:")
    for item in report["by_direction"]:
        _print_basic(f"- 方向 {item['key']}", item)
    for item in report["by_reason_setup"][:6]:
        _print_basic(f"- 原因 {item['key']}", item)

    print("\n前置连续亏损分桶:")
    for item in report["by_prior_global_loss_streak"]:
        _print_basic(f"- prior_global_loss_streak {item['key']}", item)

    print("\n前兆Top:")
    for item in report["warning_keys"]:
        print(
            f"- {item['key']}: orders={item['orders']} win={item['win_rate']:.2%} "
            f"ev={item['ev']:.2f} pnl={item['pnl']:.2f} "
            f"loss_capture={item['loss_capture']:.2%} block_delta={item['block_delta_pnl']:+.2f} "
            f"reverse_win={item['reverse_win_rate']:.2%} reverse_ev={item['reverse_ev']:.2f}"
        )

    print("\n候选拦截回放:")
    for item in report["replay_candidates"]:
        traded = item["traded"]
        blocked = item["blocked_recorded"]
        print(
            f"- {item['name']}: traded={traded['orders']} win={traded['win_rate']:.2%} "
            f"pnl={traded['pnl']:.2f} ev={traded['ev']:.2f} "
            f"blocked={blocked['orders']} blocked_pnl={blocked['pnl']:.2f} "
            f"delta={item['delta_pnl']:+.2f}"
        )

    print("\n同点位反向观察候选:")
    for item in report["reverse_direction_candidates"]:
        print(
            f"- {item['key']}: orders={item['orders']} actual_ev={item['ev']:.2f} "
            f"actual_pnl={item['pnl']:.2f} reverse_win={item['reverse_win_rate']:.2%} "
            f"reverse_ev={item['reverse_ev']:.2f} reverse_pnl={item['reverse_pnl']:.2f}"
        )

    wrong_release = report["wrong_release"]
    wrong_block = report["wrong_block"]
    print("\n错放 / 错杀概览:")
    print(
        f"- 错放候选: loss={wrong_release['losses']} warned_loss={wrong_release['warned_losses']} "
        f"coverage={wrong_release['warned_loss_rate']:.2%} warned_loss_pnl={wrong_release['warned_loss_pnl']:.2f} "
        f"prior_warned={wrong_release['prior_warned_losses']} "
        f"prior_coverage={wrong_release['prior_warned_loss_rate']:.2%} "
        f"prior_pnl={wrong_release['prior_warned_loss_pnl']:.2f}"
    )
    print(
        f"- 错杀风险: win={wrong_block['wins']} warned_win={wrong_block['warned_wins']} "
        f"coverage={wrong_block['warned_win_rate']:.2%} warned_win_pnl={wrong_block['warned_win_pnl']:.2f} "
        f"prior_warned={wrong_block['prior_warned_wins']} "
        f"prior_coverage={wrong_block['prior_warned_win_rate']:.2%} "
        f"prior_pnl={wrong_block['prior_warned_win_pnl']:.2f}"
    )


def write_report(report: Mapping[str, Any], report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"loss_precursors_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _guard_replay_candidates(
    annotations: Sequence[Mapping[str, Any]],
    warning_keys: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    candidates: list[tuple[str, Callable[[Mapping[str, Any]], bool]]] = [
        ("ANY_WARNING", lambda item: bool(item.get("warning_keys"))),
        ("ANY_PRIOR_WARNING", lambda item: bool(item.get("prior_warning_keys"))),
        (
            "ANY_PRIOR_DEGRADED_OR_STREAK",
            lambda item: any(str(key).startswith(("DEGRADED:", "LOSS_STREAK:")) for key in item.get("warning_keys") or []),
        ),
        ("ANY_RISK_NOW", lambda item: any(str(key).startswith("RISK_NOW:") for key in item.get("warning_keys") or [])),
        (
            "ANY_PROFILE_SHADOW_BLOCK",
            lambda item: any(str(key).startswith("PROFILE_SHADOW:") for key in item.get("warning_keys") or []),
        ),
    ]
    for item in warning_keys[:8]:
        key = str(item["key"])
        candidates.append((key, lambda row, key=key: key in (row.get("warning_keys") or [])))

    baseline = _rolling_summary(annotations)
    replays: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name, predicate in candidates:
        if name in seen:
            continue
        seen.add(name)
        blocked = [item for item in annotations if predicate(item)]
        if not blocked:
            continue
        blocked_ids = {id(item) for item in blocked}
        traded = [item for item in annotations if id(item) not in blocked_ids]
        traded_summary = _rolling_summary(traded)
        blocked_summary = _summary(blocked)
        replays.append(
            {
                "name": name,
                "traded": traded_summary,
                "blocked_recorded": blocked_summary,
                "blocked_reverse": _reverse_summary(blocked),
                "delta_pnl": round(traded_summary["pnl"] - baseline["pnl"], 4),
            }
        )
    return sorted(replays, key=lambda item: (item["delta_pnl"], -item["blocked_recorded"]["orders"]), reverse=True)


def _summarize_warning_keys(annotations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    baseline = _summary(annotations)
    total_losses = max(1, baseline["losses"])
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in annotations:
        for key in item.get("warning_keys") or []:
            groups[str(key)].append(item)

    summaries = []
    for key, rows in groups.items():
        stats = _summary(rows, key)
        reverse = _reverse_summary(rows)
        stats.update(
            {
                "loss_lift": round((stats["losses"] / stats["orders"] if stats["orders"] else 0.0) - (baseline["losses"] / baseline["orders"] if baseline["orders"] else 0.0), 6),
                "loss_capture": round(stats["losses"] / total_losses, 6),
                "block_delta_pnl": round(-stats["pnl"], 4),
                "reverse_wins": reverse["wins"],
                "reverse_win_rate": reverse["win_rate"],
                "reverse_pnl": reverse["pnl"],
                "reverse_ev": reverse["ev"],
            }
        )
        summaries.append(stats)
    return sorted(
        summaries,
        key=lambda item: (item["block_delta_pnl"], item["loss_capture"], -item["orders"], item["key"]),
        reverse=True,
    )


def _reverse_direction_candidates(warning_keys: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    candidates = [
        dict(item)
        for item in warning_keys
        if item["orders"] >= 2 and item["ev"] < 0.0 and item["reverse_ev"] > 0.0 and item["reverse_win_rate"] >= 0.55
    ]
    return sorted(candidates, key=lambda item: (item["reverse_ev"], item["block_delta_pnl"], item["orders"]), reverse=True)


def _feature_keys(sample: Mapping[str, Any], risk_keys: Sequence[str]) -> list[str]:
    keys = [
        f"DIRECTION:{sample.get('direction') or 'UNKNOWN'}",
        f"SEGMENT:{sample.get('threshold_segment') or 'UNKNOWN'}",
        f"SETUP:{sample.get('reason_setup') or 'UNKNOWN'}",
        f"LEVEL:{sample.get('level') or 'UNKNOWN'}",
        f"TIMEFRAME:{sample.get('timeframe_minutes') or 'UNKNOWN'}",
    ]
    regime = sample.get("regime")
    if regime:
        keys.append(f"REGIME:{regime}")
    for risk_key in risk_keys:
        keys.append(f"RISK:{risk_key}")
    return keys


def _profile_shadow_keys(sample: Mapping[str, Any]) -> list[str]:
    keys = []
    if sample.get("profile_guard_shadow_status") == "WOULD_BLOCK":
        keys.append("PROFILE_SHADOW:RECOMMENDED_WOULD_BLOCK")
    if sample.get("profile_guard_default_shadow_status") == "WOULD_BLOCK":
        keys.append("PROFILE_SHADOW:DEFAULT_WOULD_BLOCK")
    return keys


def _is_degraded(stats: Mapping[str, Any], config: PrecursorConfig) -> bool:
    return (
        int(stats.get("orders") or 0) >= config.min_samples
        and (
            float(stats.get("win_rate") or 0.0) < config.degraded_win_rate
            or float(stats.get("ev") or 0.0) < config.degraded_ev
        )
    )


def _trim_history(rows: deque[dict[str, Any]], min_opened_at: int) -> None:
    while rows and int(rows[0].get("opened_at") or 0) < min_opened_at:
        rows.popleft()


def _reverse_result(sample: Mapping[str, Any]) -> str:
    entry = sample.get("entry_price")
    exit_price = sample.get("exit_price")
    try:
        entry_value = float(entry)
        exit_value = float(exit_price)
    except (TypeError, ValueError):
        return "UNKNOWN"
    direction = str(sample.get("direction") or "").upper()
    if direction == "LONG":
        return "WIN" if exit_value < entry_value else "LOSS"
    if direction == "SHORT":
        return "WIN" if exit_value > entry_value else "LOSS"
    return "UNKNOWN"


def _fixed_pnl(result: str) -> float:
    if result == "WIN":
        return round(BASE_STAKE * WIN_PAYOUT_RATE, 4)
    if result == "LOSS":
        return -BASE_STAKE
    return 0.0


def _settled_samples(samples: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        [dict(item) for item in samples if item.get("result") in {"WIN", "LOSS"}],
        key=lambda item: (int(item.get("opened_at") or 0), int(item.get("order_id") or 0)),
    )


def _summary(samples: Sequence[Mapping[str, Any]], key: str | None = None) -> dict[str, Any]:
    orders = len(samples)
    wins = sum(1 for item in samples if item.get("result") == "WIN")
    pnl = round(sum(float(item.get("pnl") or 0.0) for item in samples), 4)
    return {
        "key": key,
        "orders": orders,
        "wins": wins,
        "losses": orders - wins,
        "win_rate": round(wins / orders, 6) if orders else 0.0,
        "pnl": pnl,
        "ev": round(pnl / orders, 6) if orders else 0.0,
    }


def _rolling_summary(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    orders = len(samples)
    wins = 0
    pnl = 0.0
    stake_step = 1
    total_staked = 0.0
    for item in samples:
        stake = round(BASE_STAKE * (STAKE_MULTIPLIER ** (stake_step - 1)), 4)
        total_staked += stake
        if item.get("result") == "WIN":
            wins += 1
            pnl += round(stake * WIN_PAYOUT_RATE, 4)
            stake_step = min(stake_step + 1, MAX_STAKE_STEP)
        else:
            pnl -= stake
            stake_step = 1
    pnl = round(pnl, 4)
    return {
        "orders": orders,
        "wins": wins,
        "losses": orders - wins,
        "win_rate": round(wins / orders, 6) if orders else 0.0,
        "pnl": pnl,
        "ev": round(pnl / orders, 6) if orders else 0.0,
        "total_staked": round(total_staked, 4),
        "roi": round(pnl / total_staked, 6) if total_staked else 0.0,
    }


def _reverse_summary(annotations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    orders = len(annotations)
    wins = sum(1 for item in annotations if item.get("reverse_result") == "WIN")
    pnl = round(sum(float(item.get("reverse_pnl") or 0.0) for item in annotations), 4)
    return {
        "orders": orders,
        "wins": wins,
        "losses": orders - wins,
        "win_rate": round(wins / orders, 6) if orders else 0.0,
        "pnl": pnl,
        "ev": round(pnl / orders, 6) if orders else 0.0,
    }


def _group_summaries(
    samples: Sequence[Mapping[str, Any]],
    key_func: Callable[[Mapping[str, Any]], str],
) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in samples:
        groups[key_func(item)].append(item)
    return sorted((_summary(rows, key) for key, rows in groups.items()), key=lambda item: (item["ev"], item["win_rate"], -item["orders"]))


def _sample_range(samples: Sequence[Mapping[str, Any]]) -> dict[str, str | None]:
    if not samples:
        return {"from": None, "to": None}
    return {
        "from": _iso(min(int(item.get("opened_at") or 0) for item in samples)),
        "to": _iso(max(int(item.get("settled_at") or item.get("opened_at") or 0) for item in samples)),
    }


def _iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()


def _streak_bucket(value: int) -> str:
    if value >= 3:
        return ">=3"
    return str(value)


def _compact_orders(samples: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "order_id": item.get("order_id"),
            "direction": item.get("direction"),
            "segment": item.get("threshold_segment"),
            "result": item.get("result"),
            "pnl": item.get("pnl"),
            "opened_at": item.get("opened_at"),
            "prior_global_loss_streak": item.get("prior_global_loss_streak"),
            "risk_hint_keys": item.get("risk_hint_keys") or [],
            "prior_warning_keys": item.get("prior_warning_keys") or [],
            "warning_keys": item.get("warning_keys") or [],
        }
        for item in samples
    ]


def _print_basic(label: str, item: Mapping[str, Any]) -> None:
    print(
        f"{label}: orders={item.get('orders', 0)} win={item.get('win_rate', 0.0):.2%} "
        f"ev={item.get('ev', 0.0):.2f} pnl={item.get('pnl', 0.0):.2f}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze live order loss precursors without changing trading logic.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--min-samples", type=int, default=5)
    parser.add_argument("--degraded-win-rate", type=float, default=0.5)
    parser.add_argument("--degraded-ev", type=float, default=0.0)
    parser.add_argument("--global-loss-streak", type=int, default=2)
    parser.add_argument("--key-loss-streak", type=int, default=2)
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)

    config = PrecursorConfig(
        lookback_days=max(1, args.lookback_days),
        min_samples=max(1, args.min_samples),
        degraded_win_rate=args.degraded_win_rate,
        degraded_ev=args.degraded_ev,
        global_loss_streak=max(1, args.global_loss_streak),
        key_loss_streak=max(1, args.key_loss_streak),
        top=max(1, args.top),
    )
    samples = load_order_samples(args.db_path, args.symbol)
    report = analyze_loss_precursors(samples, config=config)
    if not args.no_write:
        report["report_path"] = str(write_report(report, args.report_dir))
    print_summary(report)
    if report.get("report_path"):
        print(f"\n报告: {report['report_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
