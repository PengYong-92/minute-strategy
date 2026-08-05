import json
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from itertools import combinations
from typing import Any

from app.models import Signal


BASE_STAKE = 10.0
WIN_PAYOUT_RATE = 0.8
STAKE_MULTIPLIER = 1.8
MAX_STAKE_STEP = 3
SHADOW_GUARD_MIN_OBSERVED = 20
SHADOW_GUARD_MIN_BLOCKED = 8
SHADOW_GUARD_BLOCK_EV = -1.0
SHADOW_GUARD_BLOCK_WIN_RATE = 0.5
SHADOW_GUARD_PASS_EV = 0.0
KEY_SUBSET_DELTA_PNL_BAND = 30.0
KEY_SUBSET_VALIDATION_DELTA_PNL_BAND = 30.0
KEY_SUBSET_MIN_BLOCK_COVERAGE = 0.6
GUARD_COMPARE_MIN_OBSERVED = 20
GUARD_COMPARE_MIN_DIFF = 4


def sample_from_entry_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    payload = _get(snapshot, "entry_payload", {}) or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    signal = payload.get("signal") or {}
    fear_greed = payload.get("fear_greed") or {}
    guard_shadow = payload.get("profile_guard_shadow") or {}
    default_guard_shadow = payload.get("profile_guard_default_shadow") or {}
    guard_policy = payload.get("profile_guard_selection_policy") or {}
    reason = str(signal.get("reason") or "")
    return {
        "symbol": _get(snapshot, "symbol", ""),
        "order_id": _get(snapshot, "order_id", 0),
        "direction": _get(snapshot, "direction", ""),
        "timeframe_minutes": _get(snapshot, "timeframe_minutes", 0),
        "threshold_segment": _get(snapshot, "threshold_segment", ""),
        "result": _get(snapshot, "result"),
        "pnl": _float(_get(snapshot, "pnl")),
        "stake": _float(_get(snapshot, "stake")),
        "stake_progression_step": int(_get(snapshot, "stake_progression_step", 1) or 1),
        "opened_at": int(_get(snapshot, "opened_at", 0) or 0),
        "settled_at": _get(snapshot, "settled_at"),
        "entry_price": _get(snapshot, "entry_price"),
        "exit_price": _get(snapshot, "exit_price"),
        "level": signal.get("level") or "",
        "reason": reason,
        "reason_setup": reason.split("：", 1)[0].split(";", 1)[0].strip() or "UNKNOWN",
        "score": _float(signal.get("score")),
        "threshold": _float(signal.get("threshold")),
        "edge": _float(_get(snapshot, "edge")),
        "volume_ratio": _float(signal.get("volume_ratio")),
        "volume_threshold": _float(signal.get("volume_threshold")),
        "price_change_pct": _float(signal.get("price_change_pct")),
        "price_position": _float(signal.get("price_position")),
        "rsi": _float(signal.get("rsi")),
        "bollinger_position": _float(signal.get("bollinger_position")),
        "mtf_10m_bias": _float(signal.get("mtf_10m_bias")),
        "mtf_30m_bias": _float(signal.get("mtf_30m_bias")),
        "regime": signal.get("regime") or _get(snapshot, "regime", "") or "",
        "risk_flags": signal.get("risk_flags") or "",
        "fear_greed_value": fear_greed.get("value"),
        "fear_greed_trend": fear_greed.get("trend") or "",
        "profile_guard_shadow_status": guard_shadow.get("status", ""),
        "profile_guard_shadow_hit_keys": guard_shadow.get("hit_keys") or [],
        "profile_guard_shadow_active_keys": guard_shadow.get("active_keys") or [],
        "profile_guard_shadow_min_history": int(guard_shadow.get("min_history", 0) or 0),
        "profile_guard_shadow_min_group_size": int(guard_shadow.get("min_group_size", 0) or 0),
        "profile_guard_default_shadow_status": default_guard_shadow.get("status", ""),
        "profile_guard_default_shadow_hit_keys": default_guard_shadow.get("hit_keys") or [],
        "profile_guard_selection_policy_name": guard_policy.get("name", ""),
        "profile_guard_selection_policy_reason": guard_policy.get("reason", ""),
        "profile_guard_selection_policy_selected_keys": guard_policy.get("selected_keys") or [],
        "profile_guard_selection_policy_score_best_keys": guard_policy.get("score_best_keys") or [],
    }


def summarize_order_samples(samples: Sequence[dict[str, Any]], *, min_group_size: int = 2) -> dict[str, Any]:
    return summarize_order_samples_with_guard(
        samples,
        min_group_size=min_group_size,
        profile_guard_min_history=15,
        profile_guard_min_group_size=min_group_size,
    )


def summarize_order_samples_with_guard(
    samples: Sequence[dict[str, Any]],
    *,
    min_group_size: int = 2,
    profile_guard_min_history: int = 15,
    profile_guard_min_group_size: int | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    guard_group_size = max(1, int(profile_guard_min_group_size or min_group_size))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sample_count": len(samples),
        "range": _sample_range(samples),
        "total": _summary(samples),
        "by_segment": _group_summaries(samples, "threshold_segment", min_group_size=min_group_size),
        "by_level": _group_summaries(samples, "level", min_group_size=1),
        "by_stake_step": _group_summaries(samples, "stake_progression_step", min_group_size=1),
        "by_regime": _group_summaries(samples, "regime", min_group_size=1),
        "by_fear_greed_trend": _group_summaries(samples, "fear_greed_trend", min_group_size=1),
        "by_reason": _group_summaries(samples, "reason_setup", min_group_size=1),
        "feature_bins": _feature_bins(samples, min_group_size=min_group_size),
        "risk_hints": _risk_hints(samples),
        "profile_guard": evaluate_profile_guard(
            samples,
            min_history=max(1, int(profile_guard_min_history)),
            min_group_size=guard_group_size,
        ),
        "profile_guard_shadow": _profile_guard_shadow_summary(samples),
        "profile_guard_policy": _profile_guard_policy_summary(samples),
        "profile_guard_shadow_compare": _profile_guard_shadow_compare(samples),
        "elapsed_seconds": round(time.perf_counter() - started, 4),
    }


def risk_hint_keys_for_sample(sample: Mapping[str, Any]) -> list[str]:
    return [name for name, predicate in _risk_hint_rules() if predicate(sample)]


def sample_from_signal(signal: Signal) -> dict[str, Any]:
    reason = signal.reason or ""
    direction = signal.observe_direction or signal.direction
    return {
        "symbol": "",
        "order_id": 0,
        "direction": direction,
        "timeframe_minutes": signal.timeframe_minutes,
        "threshold_segment": signal.threshold_segment,
        "result": None,
        "pnl": 0.0,
        "stake": BASE_STAKE,
        "stake_progression_step": 1,
        "opened_at": signal.open_time,
        "settled_at": None,
        "entry_price": signal.price,
        "exit_price": None,
        "level": signal.level,
        "reason": reason,
        "reason_setup": reason.split("：", 1)[0].split(";", 1)[0].strip() or "UNKNOWN",
        "score": signal.score,
        "threshold": signal.threshold,
        "edge": abs(signal.score) - signal.threshold,
        "volume_ratio": signal.volume_ratio,
        "volume_threshold": signal.volume_threshold,
        "price_change_pct": signal.price_change_pct,
        "price_position": signal.price_position,
        "rsi": signal.rsi,
        "bollinger_position": signal.bollinger_position,
        "mtf_10m_bias": signal.mtf_10m_bias,
        "mtf_30m_bias": signal.mtf_30m_bias,
        "regime": signal.regime,
        "risk_flags": signal.risk_flags,
        "fear_greed_value": signal.fear_greed_value,
        "fear_greed_trend": signal.fear_greed_trend,
    }


def profile_guard_shadow(
    signal: Signal | None,
    profile: Mapping[str, Any] | None,
    *,
    use_recommended: bool = True,
) -> dict[str, Any]:
    if signal is None:
        return _empty_profile_guard_shadow("NO_SIGNAL")
    if not profile:
        return _empty_profile_guard_shadow("NO_PROFILE")

    guard = profile.get("profile_guard") or {}
    guard_variant = (
        guard.get("recommended_key_subset") if use_recommended else guard.get("walk_forward_combined")
    ) or guard.get("recommended_walk_forward") or guard.get("walk_forward_combined") or {}
    variant_name = (
        "recommended_key_subset"
        if use_recommended and guard.get("recommended_key_subset")
        else ("walk_forward_combined" if not use_recommended and guard.get("walk_forward_combined") else guard_variant.get("name", ""))
    )
    active_keys = set(guard_variant.get("final_active_keys") or guard_variant.get("risk_keys") or [])
    sample_keys = risk_hint_keys_for_sample(sample_from_signal(signal))
    hit_keys = sorted(active_keys.intersection(sample_keys))
    return {
        "observe_only": True,
        "variant": variant_name,
        "status": "WOULD_BLOCK" if hit_keys else "PASS",
        "sample_keys": sample_keys,
        "active_keys": sorted(active_keys),
        "hit_keys": hit_keys,
        "min_history": guard_variant.get("min_history", 0),
        "min_group_size": guard_variant.get("min_group_size", 0),
        "selection_policy": guard_variant.get("selection_policy") or {},
        "recommended": {
            "traded_orders": (guard_variant.get("traded") or {}).get("orders", 0),
            "traded_win_rate": (guard_variant.get("traded") or {}).get("win_rate", 0.0),
            "traded_ev": (guard_variant.get("traded") or {}).get("ev", 0.0),
            "traded_pnl": (guard_variant.get("traded") or {}).get("pnl", 0.0),
            "blocked_orders": (guard_variant.get("blocked") or {}).get("orders", 0),
            "delta_pnl": guard_variant.get("delta_pnl", 0.0),
        },
    }


def _profile_guard_shadow_summary(samples: Sequence[dict[str, Any]]) -> dict[str, Any]:
    blocked = [
        item
        for item in samples
        if item.get("profile_guard_shadow_status") == "WOULD_BLOCK"
        and item.get("result") in {"WIN", "LOSS"}
    ]
    passed = [
        item
        for item in samples
        if item.get("profile_guard_shadow_status") == "PASS"
        and item.get("result") in {"WIN", "LOSS"}
    ]
    observed = [
        item
        for item in samples
        if item.get("profile_guard_shadow_status") in {"WOULD_BLOCK", "PASS"}
        and item.get("result") in {"WIN", "LOSS"}
    ]
    observed_summary = _summary(observed)
    blocked_summary = _summary(blocked)
    pass_summary = _summary(passed)
    return {
        "observed": observed_summary,
        "would_block": blocked_summary,
        "pass": pass_summary,
        "hit_keys": _shadow_hit_key_summary(blocked),
        "coverage": round(len(observed) / len(samples), 6) if samples else 0.0,
        "upgrade": _profile_guard_upgrade_action(observed_summary, blocked_summary, pass_summary),
    }


def _profile_guard_upgrade_action(
    observed: Mapping[str, Any],
    blocked: Mapping[str, Any],
    passed: Mapping[str, Any],
) -> dict[str, Any]:
    if int(observed.get("orders") or 0) < SHADOW_GUARD_MIN_OBSERVED:
        return {
            "action": "COLLECTING",
            "reason": f"影子样本 {observed.get('orders', 0)} < {SHADOW_GUARD_MIN_OBSERVED}，继续采样",
            "confidence": "LOW",
        }
    if int(blocked.get("orders") or 0) < SHADOW_GUARD_MIN_BLOCKED:
        return {
            "action": "COLLECTING",
            "reason": f"影子拦截样本 {blocked.get('orders', 0)} < {SHADOW_GUARD_MIN_BLOCKED}，继续采样",
            "confidence": "LOW",
        }

    block_bad = (
        float(blocked.get("ev") or 0.0) <= SHADOW_GUARD_BLOCK_EV
        and float(blocked.get("win_rate") or 0.0) < SHADOW_GUARD_BLOCK_WIN_RATE
    )
    pass_ok = int(passed.get("orders") or 0) == 0 or float(passed.get("ev") or 0.0) >= SHADOW_GUARD_PASS_EV
    if block_bad and pass_ok:
        return {
            "action": "READY_TO_BLOCK",
            "reason": (
                f"影子拦截组 {blocked.get('orders', 0)} 单 EV {blocked.get('ev', 0.0):.2f} "
                f"且胜率 {blocked.get('win_rate', 0.0):.2%}，放行组 EV {passed.get('ev', 0.0):.2f}"
            ),
            "confidence": "MEDIUM" if int(blocked.get("orders") or 0) < 30 else "HIGH",
        }
    if float(blocked.get("ev") or 0.0) > 0.0 or float(blocked.get("win_rate") or 0.0) >= 0.56:
        return {
            "action": "KEEP_OBSERVING",
            "reason": (
                f"影子拦截组暂未劣化：EV {blocked.get('ev', 0.0):.2f} "
                f"胜率 {blocked.get('win_rate', 0.0):.2%}"
            ),
            "confidence": "MEDIUM",
        }
    return {
        "action": "WATCH",
        "reason": "影子拦截组偏弱但证据不足，继续观察",
        "confidence": "MEDIUM",
    }


def evaluate_profile_guard(
    samples: Sequence[dict[str, Any]],
    *,
    min_history: int = 10,
    min_group_size: int = 2,
) -> dict[str, Any]:
    settled = _settled_samples(samples)
    baseline = _replay_summary(settled)
    final_hint_keys = [item["key"] for item in _risk_hints(settled)]
    per_hint = [
        _guard_variant(settled, [key], f"static_{key}", baseline=baseline)
        for key in final_hint_keys
    ]
    per_hint.sort(key=lambda item: (item["delta_pnl"], item["blocked"]["orders"]), reverse=True)
    sweep = sweep_profile_guard(settled, baseline=baseline)
    default_walk_forward = _walk_forward_guard(
        settled,
        min_history=max(1, int(min_history)),
        min_group_size=max(1, int(min_group_size)),
        baseline=baseline,
    )
    recommended = sweep.get("best") or default_walk_forward
    key_subset_sweep = sweep_profile_guard_key_subsets(
        settled,
        candidate_keys=final_hint_keys,
        min_history=int(recommended.get("min_history") or default_walk_forward["min_history"]),
        min_group_size=int(recommended.get("min_group_size") or default_walk_forward["min_group_size"]),
        baseline=baseline,
    )
    replay_upgrade = _profile_guard_upgrade_action(
        baseline,
        default_walk_forward.get("blocked") or {},
        default_walk_forward.get("traded") or {},
    )
    return {
        "method": (
            "Uses settled order-entry snapshots. Static variants are diagnostic; "
            "walk-forward variant only uses prior samples and assumes blocked signals are still observed."
        ),
        "baseline": baseline,
        "risk_keys": final_hint_keys,
        "static_combined": _guard_variant(settled, final_hint_keys, "static_combined", baseline=baseline),
        "static_by_hint": per_hint,
        "walk_forward_combined": default_walk_forward,
        "recommended_walk_forward": recommended,
        "recommended_key_subset": key_subset_sweep.get("stable_best") or key_subset_sweep.get("best"),
        "key_subset_sweep": key_subset_sweep,
        "replay_upgrade": replay_upgrade,
        "walk_forward_sweep": sweep,
    }


def _shadow_hit_key_summary(samples: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        for key in sample.get("profile_guard_shadow_hit_keys") or []:
            groups[str(key)].append(sample)
    summaries = [_summary(rows, key) for key, rows in groups.items()]
    return sorted(summaries, key=lambda item: (item["ev"], item["win_rate"], -item["orders"]))


def _profile_guard_policy_summary(samples: Sequence[dict[str, Any]]) -> dict[str, Any]:
    policy_groups = _group_summaries(
        [sample for sample in samples if sample.get("profile_guard_selection_policy_name")],
        "profile_guard_selection_policy_name",
        min_group_size=1,
    )
    selected_key_groups = _group_by_list_field(samples, "profile_guard_selection_policy_selected_keys")
    return {
        "by_policy": policy_groups,
        "by_selected_key": selected_key_groups,
    }


def _profile_guard_shadow_compare(samples: Sequence[dict[str, Any]]) -> dict[str, Any]:
    settled = [sample for sample in samples if sample.get("result") in {"WIN", "LOSS"}]
    observed = [
        sample
        for sample in settled
        if sample.get("profile_guard_shadow_status") in {"WOULD_BLOCK", "PASS"}
        and sample.get("profile_guard_default_shadow_status") in {"WOULD_BLOCK", "PASS"}
    ]
    buckets = {
        "BOTH_BLOCK": [],
        "RECOMMENDED_ONLY_BLOCK": [],
        "DEFAULT_ONLY_BLOCK": [],
        "BOTH_PASS": [],
    }
    for sample in observed:
        recommended_blocks = sample.get("profile_guard_shadow_status") == "WOULD_BLOCK"
        default_blocks = sample.get("profile_guard_default_shadow_status") == "WOULD_BLOCK"
        if recommended_blocks and default_blocks:
            buckets["BOTH_BLOCK"].append(sample)
        elif recommended_blocks:
            buckets["RECOMMENDED_ONLY_BLOCK"].append(sample)
        elif default_blocks:
            buckets["DEFAULT_ONLY_BLOCK"].append(sample)
        else:
            buckets["BOTH_PASS"].append(sample)
    by_bucket = [
        _summary(rows, key)
        for key, rows in buckets.items()
    ]
    recommended_block = _summary([sample for sample in observed if sample.get("profile_guard_shadow_status") == "WOULD_BLOCK"])
    default_block = _summary([sample for sample in observed if sample.get("profile_guard_default_shadow_status") == "WOULD_BLOCK"])
    recommended_pass_default_block = _summary(buckets["DEFAULT_ONLY_BLOCK"])
    recommended_block_default_pass = _summary(buckets["RECOMMENDED_ONLY_BLOCK"])
    compare = {
        "observed": _summary(observed),
        "coverage": round(len(observed) / len(settled), 6) if settled else 0.0,
        "by_bucket": by_bucket,
        "recommended_block": recommended_block,
        "default_block": default_block,
        "recommended_pass_default_block": recommended_pass_default_block,
        "recommended_block_default_pass": recommended_block_default_pass,
    }
    compare["upgrade"] = _guard_compare_upgrade_action(compare)
    return compare


def _guard_compare_upgrade_action(compare: Mapping[str, Any]) -> dict[str, Any]:
    observed = compare.get("observed") or {}
    recommended_only = compare.get("recommended_block_default_pass") or {}
    default_only = compare.get("recommended_pass_default_block") or {}
    recommended_block = compare.get("recommended_block") or {}
    default_block = compare.get("default_block") or {}
    observed_orders = int(observed.get("orders") or 0)
    diff_orders = int(recommended_only.get("orders") or 0) + int(default_only.get("orders") or 0)
    if observed_orders < GUARD_COMPARE_MIN_OBSERVED:
        return {
            "action": "COLLECTING",
            "confidence": "LOW",
            "reason": f"守卫对照样本 {observed_orders} < {GUARD_COMPARE_MIN_OBSERVED}，继续采样",
        }
    if diff_orders < GUARD_COMPARE_MIN_DIFF:
        return {
            "action": "COLLECTING",
            "confidence": "LOW",
            "reason": f"推荐/默认差异样本 {diff_orders} < {GUARD_COMPARE_MIN_DIFF}，继续采样",
        }

    recommended_extra_good = (
        int(recommended_only.get("orders") or 0) == 0
        or float(recommended_only.get("ev") or 0.0) <= -1.0
    )
    default_extra_bad = (
        int(default_only.get("orders") or 0) == 0
        or float(default_only.get("ev") or 0.0) >= 0.0
    )
    recommended_no_worse = float(recommended_block.get("ev") or 0.0) <= float(default_block.get("ev") or 0.0) + 0.5
    if recommended_extra_good and default_extra_bad and recommended_no_worse:
        return {
            "action": "PROMOTE_RECOMMENDED_GUARD",
            "confidence": "MEDIUM" if observed_orders < 40 else "HIGH",
            "reason": (
                f"推荐额外拦截 EV {recommended_only.get('ev', 0.0):.2f}，"
                f"默认额外拦截 EV {default_only.get('ev', 0.0):.2f}，推荐守卫可升级"
            ),
        }
    if float(recommended_block.get("ev") or 0.0) > float(default_block.get("ev") or 0.0) + 1.0:
        return {
            "action": "KEEP_DEFAULT_GUARD",
            "confidence": "MEDIUM",
            "reason": (
                f"推荐拦截组 EV {recommended_block.get('ev', 0.0):.2f} "
                f"弱于默认拦截组 EV {default_block.get('ev', 0.0):.2f}"
            ),
        }
    return {
        "action": "KEEP_OBSERVING",
        "confidence": "MEDIUM",
        "reason": "推荐/默认守卫差异尚未形成稳定优势",
    }


def _empty_profile_guard_shadow(status: str) -> dict[str, Any]:
    return {
        "observe_only": True,
        "variant": "",
        "status": status,
        "sample_keys": [],
        "active_keys": [],
        "hit_keys": [],
        "min_history": 0,
        "min_group_size": 0,
        "selection_policy": {},
        "recommended": {},
    }


def sweep_profile_guard(
    samples: Sequence[dict[str, Any]],
    *,
    min_history_values: Sequence[int] | None = None,
    min_group_size_values: Sequence[int] | None = None,
    baseline: dict[str, Any] | None = None,
    top: int = 12,
) -> dict[str, Any]:
    settled = _settled_samples(samples)
    baseline = baseline or _replay_summary(settled)
    history_values = tuple(min_history_values or (5, 8, 10, 12, 15, 20, 25, 30))
    group_values = tuple(min_group_size_values or (2, 3, 4, 5, 8, 10))
    results = []
    for min_history in history_values:
        for min_group_size in group_values:
            if min_group_size > min_history:
                continue
            variant = _walk_forward_guard(
                settled,
                min_history=max(1, int(min_history)),
                min_group_size=max(1, int(min_group_size)),
                baseline=baseline,
            )
            variant["score"] = _profile_guard_score(variant, baseline)
            results.append(variant)
    results.sort(
        key=lambda item: (
            item["score"],
            item["traded"]["pnl"],
            item["traded"]["win_rate"],
            -item["blocked"]["orders"],
        ),
        reverse=True,
    )
    best = results[0] if results else None
    return {
        "tested": len(results),
        "top": results[: max(1, int(top))],
        "best": best,
        "baseline": baseline,
        "parameter_space": {
            "min_history": list(history_values),
            "min_group_size": list(group_values),
        },
        "score_note": "score favors positive pnl/EV/win-rate, penalizes blocking almost everything or trading too few orders.",
    }


def sweep_profile_guard_key_subsets(
    samples: Sequence[dict[str, Any]],
    *,
    candidate_keys: Sequence[str] | None = None,
    min_history: int = 15,
    min_group_size: int = 2,
    baseline: dict[str, Any] | None = None,
    top: int = 12,
) -> dict[str, Any]:
    settled = _settled_samples(samples)
    baseline = baseline or _replay_summary(settled)
    keys = sorted(dict.fromkeys(candidate_keys or [item["key"] for item in _risk_hints(settled)]))
    validation_start = _validation_start_index(len(settled), max(1, int(min_history)))
    training_samples = settled[:validation_start]
    validation_samples = settled[validation_start:]
    training_baseline = _replay_summary(training_samples)
    validation_baseline = _replay_summary(validation_samples)
    results = []
    for size in range(1, len(keys) + 1):
        for key_subset in combinations(keys, size):
            training_variant = _walk_forward_guard(
                training_samples,
                min_history=max(1, int(min_history)),
                min_group_size=max(1, int(min_group_size)),
                baseline=training_baseline,
                allowed_keys=key_subset,
                name="train_key_subset",
            )
            variant = _walk_forward_guard(
                settled,
                min_history=max(1, int(min_history)),
                min_group_size=max(1, int(min_group_size)),
                baseline=baseline,
                allowed_keys=key_subset,
                name="walk_forward_key_subset",
            )
            variant["candidate_risk_keys"] = list(key_subset)
            variant["training"] = _training_for_key_subset(
                training_variant,
                baseline=training_baseline,
                min_history=max(1, int(min_history)),
                min_group_size=max(1, int(min_group_size)),
            )
            variant["validation"] = _validation_for_key_subset(
                validation_samples,
                key_subset,
                baseline=validation_baseline,
                min_history=max(1, int(min_history)),
                min_group_size=max(1, int(min_group_size)),
            )
            variant["training_score"] = _profile_guard_score(training_variant, training_baseline)
            variant["score"] = _profile_guard_score(variant, baseline)
            variant["stability_score"] = _profile_guard_stability_score(variant, baseline)
            results.append(variant)
    results.sort(
        key=lambda item: (
            item["stability_score"],
            1 if (item.get("validation") or {}).get("stable") else 0,
            item["score"],
            item["traded"]["pnl"],
            item["traded"]["win_rate"],
            -len(item.get("candidate_risk_keys") or []),
            -item["blocked"]["orders"],
        ),
        reverse=True,
    )
    best = results[0] if results else None
    stable_results = [
        item
        for item in results
        if (item.get("training") or {}).get("stable") and (item.get("validation") or {}).get("stable")
    ]
    score_best = stable_results[0] if stable_results else None
    stable_best, selection_policy = _select_stable_key_subset(stable_results)
    if stable_best:
        stable_best["selection_policy"] = selection_policy
    elif best:
        best["selection_policy"] = selection_policy
    return {
        "tested": len(results),
        "top": results[: max(1, int(top))],
        "best": best,
        "score_best": score_best,
        "stable_best": stable_best,
        "stable_top": stable_results[: max(1, int(top))],
        "baseline": baseline,
        "candidate_keys": keys,
        "min_history": max(1, int(min_history)),
        "min_group_size": max(1, int(min_group_size)),
        "validation": {
            "start_index": validation_start,
            "training_orders": len(training_samples),
            "orders": len(validation_samples),
            "baseline": validation_baseline,
        },
        "selection_policy": selection_policy,
        "score_note": "diagnostic only; stable_best requires train+tail validation and applies a pnl stability band.",
    }


def _summary(samples: Sequence[dict[str, Any]], key: str | None = None) -> dict[str, Any]:
    orders = len(samples)
    wins = sum(1 for item in samples if item["result"] == "WIN")
    pnl = round(sum(float(item["pnl"]) for item in samples), 4)
    return {
        "key": key,
        "orders": orders,
        "wins": wins,
        "losses": orders - wins,
        "win_rate": round(wins / orders, 6) if orders else 0.0,
        "pnl": pnl,
        "ev": round(pnl / orders, 6) if orders else 0.0,
    }


def _group_summaries(samples: Sequence[dict[str, Any]], field: str, *, min_group_size: int) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in samples:
        groups[str(item.get(field) or "UNKNOWN")].append(item)
    summaries = [_summary(rows, key) for key, rows in groups.items() if len(rows) >= min_group_size]
    return sorted(summaries, key=lambda item: (item["ev"], item["win_rate"], -item["orders"]))


def _group_by_list_field(samples: Sequence[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        for key in sample.get(field) or []:
            groups[str(key)].append(sample)
    summaries = [_summary(rows, key) for key, rows in groups.items()]
    return sorted(summaries, key=lambda item: (item["ev"], item["win_rate"], -item["orders"], item["key"] or ""))


def _feature_bins(samples: Sequence[dict[str, Any]], *, min_group_size: int) -> dict[str, list[dict[str, Any]]]:
    cutoffs = {
        "volume_ratio": [1.5, 2.0, 3.0, 4.0, 6.0, 10.0],
        "price_position": [0.0, 0.2, 0.35, 0.5, 0.65, 0.8, 1.1],
        "price_change_pct": [-0.02, -0.006, -0.004, -0.003, -0.002, -0.001, 0.01],
        "rsi": [0.0, 25.0, 30.0, 35.0, 40.0, 45.0, 50.0, 60.0, 100.0],
        "bollinger_position": [-2.0, -0.5, 0.0, 0.2, 0.35, 0.5, 0.8, 2.0],
        "mtf_10m_bias": [-5.0, -2.0, -1.0, 0.0, 1.0, 2.0, 5.0],
        "mtf_30m_bias": [-5.0, -2.0, -1.0, 0.0, 1.0, 2.0, 5.0],
        "edge": [0.0, 10.0, 15.0, 20.0, 25.0, 30.0, 100.0],
    }
    return {
        field: _group_by_binner(
            samples,
            lambda item, field=field, cuts=cuts: _bin_value(item.get(field), cuts),
            min_group_size,
        )
        for field, cuts in cutoffs.items()
    }


def _risk_hints(samples: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    hints = []
    for name, predicate in _risk_hint_rules():
        rows = [item for item in samples if predicate(item)]
        if not rows:
            continue
        summary = _summary(rows, name)
        if summary["orders"] >= 2 and (summary["ev"] < 0 or summary["win_rate"] < 0.5):
            hints.append(summary)
    return sorted(hints, key=lambda item: (item["ev"], item["win_rate"], -item["orders"]))


def _risk_hint_rules():
    return (
        ("LEVEL_A_REBOUND", lambda item: item["reason_setup"] == "放量急跌反抽" and item["level"] == "A"),
        ("WEAK_SEGMENT_WD00_WD18_WD22", lambda item: item["threshold_segment"] in {"WD-00", "WD-18", "WD-22"}),
        ("MID_POSITION_REBOUND", lambda item: 0.35 <= item["price_position"] < 0.65),
        ("SHALLOW_DROP_REBOUND", lambda item: -0.002 <= item["price_change_pct"] < -0.001),
        ("HIGH_RSI_REBOUND", lambda item: item["rsi"] >= 45.0),
        ("DUAL_UP_BIAS_REBOUND", lambda item: item["mtf_10m_bias"] >= 0.0 and item["mtf_30m_bias"] >= 0.0),
    )


def _settled_samples(samples: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        [item for item in samples if item.get("result") in {"WIN", "LOSS"}],
        key=lambda item: (int(item.get("opened_at") or 0), int(item.get("order_id") or 0)),
    )


def _guard_variant(
    samples: Sequence[dict[str, Any]],
    risk_keys: Sequence[str],
    name: str,
    *,
    baseline: dict[str, Any],
) -> dict[str, Any]:
    key_set = set(risk_keys)
    blocked = [item for item in samples if key_set.intersection(risk_hint_keys_for_sample(item))]
    blocked_ids = {id(item) for item in blocked}
    traded = [item for item in samples if id(item) not in blocked_ids]
    traded_summary = _replay_summary(traded)
    return {
        "name": name,
        "risk_keys": sorted(key_set),
        "traded": traded_summary,
        "blocked": _summary(blocked),
        "blocked_actual_pnl": round(sum(float(item.get("pnl") or 0.0) for item in blocked), 4),
        "delta_pnl": round(traded_summary["pnl"] - baseline["pnl"], 4),
        "rejected_by_key": _rejected_by_key(blocked, key_set),
    }


def _walk_forward_guard(
    samples: Sequence[dict[str, Any]],
    *,
    min_history: int,
    min_group_size: int,
    baseline: dict[str, Any],
    allowed_keys: Sequence[str] | None = None,
    name: str = "walk_forward_combined",
) -> dict[str, Any]:
    history: list[dict[str, Any]] = []
    traded: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    blocked_records: list[dict[str, Any]] = []
    key_stats: dict[str, dict[str, Any]] = {}
    allowed_key_set = set(allowed_keys) if allowed_keys is not None else None

    for sample in samples:
        active_keys: set[str] = set()
        if len(history) >= min_history:
            active_keys = {
                key
                for key, stats in key_stats.items()
                if _is_weak_key_stats(stats, min_group_size=min_group_size)
            }
            if allowed_key_set is not None:
                active_keys = active_keys.intersection(allowed_key_set)

        sample_keys = risk_hint_keys_for_sample(sample)
        hit_keys = sorted(active_keys.intersection(sample_keys))
        if hit_keys:
            blocked.append(sample)
            blocked_records.append(
                {
                    "order_id": sample.get("order_id"),
                    "opened_at": sample.get("opened_at"),
                    "result": sample.get("result"),
                    "pnl": sample.get("pnl"),
                    "risk_keys": hit_keys,
                }
            )
        else:
            traded.append(sample)

        history.append(sample)
        _accumulate_key_stats(key_stats, sample, sample_keys)

    traded_summary = _replay_summary(traded)
    active_keys = sorted({key for item in blocked_records for key in item["risk_keys"]})
    final_active_keys = {
        key
        for key, stats in key_stats.items()
        if _is_weak_key_stats(stats, min_group_size=min_group_size)
    }
    if allowed_key_set is not None:
        final_active_keys = final_active_keys.intersection(allowed_key_set)
    return {
        "name": name,
        "min_history": min_history,
        "min_group_size": min_group_size,
        "allowed_risk_keys": sorted(allowed_key_set) if allowed_key_set is not None else [],
        "risk_keys": active_keys,
        "final_active_keys": sorted(final_active_keys),
        "traded": traded_summary,
        "blocked": _summary(blocked),
        "blocked_actual_pnl": round(sum(float(item.get("pnl") or 0.0) for item in blocked), 4),
        "delta_pnl": round(traded_summary["pnl"] - baseline["pnl"], 4),
        "rejected_by_key": _rejected_by_key(blocked, active_keys),
        "blocked_key_contribution": _blocked_key_contribution(blocked_records),
        "blocked_records": blocked_records[-20:],
    }


def _profile_guard_score(variant: Mapping[str, Any], baseline: Mapping[str, Any]) -> float:
    traded = variant.get("traded") or {}
    blocked = variant.get("blocked") or {}
    total_orders = max(1, int(baseline.get("orders") or 0))
    traded_orders = int(traded.get("orders") or 0)
    blocked_orders = int(blocked.get("orders") or 0)
    trade_ratio = traded_orders / total_orders
    block_ratio = blocked_orders / total_orders
    pnl = float(traded.get("pnl") or 0.0)
    delta = float(variant.get("delta_pnl") or 0.0)
    ev = float(traded.get("ev") or 0.0)
    win_rate = float(traded.get("win_rate") or 0.0)

    score = pnl + delta * 0.25 + ev * 4.0 + (win_rate - 0.55) * 80.0
    if traded_orders < max(8, total_orders * 0.25):
        score -= 80.0
    if block_ratio > 0.7:
        score -= (block_ratio - 0.7) * 120.0
    if trade_ratio < 0.35:
        score -= (0.35 - trade_ratio) * 120.0
    return round(score, 4)


def _profile_guard_stability_score(variant: Mapping[str, Any], baseline: Mapping[str, Any]) -> float:
    training = variant.get("training") or {}
    validation = variant.get("validation") or {}
    score = float(variant.get("score") or 0.0)
    training_delta = float(training.get("delta_pnl") or 0.0)
    validation_delta = float(validation.get("delta_pnl") or 0.0)
    validation_ev = float(validation.get("traded_ev") or 0.0)
    blocked_orders = int((variant.get("blocked") or {}).get("orders") or 0)
    validation_orders = int(validation.get("orders") or 0)
    stable = bool(training.get("stable") and validation.get("stable"))
    stable_bonus = 55.0 if stable else -55.0
    coverage_bonus = min(20.0, blocked_orders * 1.5 + validation_orders * 0.6)
    return round(score + training_delta * 0.8 + validation_delta * 1.8 + validation_ev * 6.0 + stable_bonus + coverage_bonus, 4)


def _select_stable_key_subset(stable_results: Sequence[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if not stable_results:
        return None, {
            "name": "NO_STABLE_SUBSET",
            "reason": "没有同时通过训练段和验证段的候选",
            "delta_pnl_band": KEY_SUBSET_DELTA_PNL_BAND,
            "validation_delta_pnl_band": KEY_SUBSET_VALIDATION_DELTA_PNL_BAND,
            "min_block_coverage": KEY_SUBSET_MIN_BLOCK_COVERAGE,
        }
    score_best = stable_results[0]
    score_best_pnl = float((score_best.get("traded") or {}).get("pnl") or 0.0)
    score_best_validation_delta = float((score_best.get("validation") or {}).get("delta_pnl") or 0.0)
    score_best_blocked = max(1, int((score_best.get("blocked") or {}).get("orders") or 0))
    eligible = []
    for item in stable_results:
        item_pnl = float((item.get("traded") or {}).get("pnl") or 0.0)
        item_validation_delta = float((item.get("validation") or {}).get("delta_pnl") or 0.0)
        item_blocked = int((item.get("blocked") or {}).get("orders") or 0)
        if score_best_pnl - item_pnl > KEY_SUBSET_DELTA_PNL_BAND:
            continue
        if score_best_validation_delta - item_validation_delta > KEY_SUBSET_VALIDATION_DELTA_PNL_BAND:
            continue
        if item_blocked / score_best_blocked < KEY_SUBSET_MIN_BLOCK_COVERAGE:
            continue
        eligible.append(item)

    def sort_key(item: Mapping[str, Any]) -> tuple:
        return (
            len(item.get("candidate_risk_keys") or []),
            -float((item.get("traded") or {}).get("pnl") or 0.0),
            -float((item.get("validation") or {}).get("delta_pnl") or 0.0),
            -int((item.get("blocked") or {}).get("orders") or 0),
            -float(item.get("stability_score") or 0.0),
        )

    selected = sorted(eligible or [score_best], key=sort_key)[0]
    selected_keys = selected.get("candidate_risk_keys") or []
    score_best_keys = score_best.get("candidate_risk_keys") or []
    reason = (
        "收益位于稳定带内，选择更少key的稳定组合"
        if list(selected_keys) != list(score_best_keys)
        else "最高稳定分组合已满足稳定带"
    )
    return selected, {
        "name": "STABILITY_BAND",
        "reason": reason,
        "delta_pnl_band": KEY_SUBSET_DELTA_PNL_BAND,
        "validation_delta_pnl_band": KEY_SUBSET_VALIDATION_DELTA_PNL_BAND,
        "min_block_coverage": KEY_SUBSET_MIN_BLOCK_COVERAGE,
        "score_best_keys": list(score_best_keys),
        "selected_keys": list(selected_keys),
        "eligible": len(eligible),
        "stable_candidates": len(stable_results),
    }


def _training_for_key_subset(
    variant: Mapping[str, Any],
    *,
    baseline: Mapping[str, Any],
    min_history: int,
    min_group_size: int,
) -> dict[str, Any]:
    traded = variant.get("traded") or {}
    blocked = variant.get("blocked") or {}
    orders = int(baseline.get("orders") or 0)
    blocked_orders = int(blocked.get("orders") or 0)
    stable = (
        orders >= max(min_history, 4)
        and blocked_orders >= min_group_size
        and float(variant.get("delta_pnl") or 0.0) > 0.0
        and float(traded.get("ev") or 0.0) >= float(baseline.get("ev") or 0.0)
        and float(blocked.get("ev") or 0.0) < 0.0
    )
    if stable:
        reason = "训练段改善基准"
    elif blocked_orders == 0:
        reason = "训练段未命中该子集"
    elif float(variant.get("delta_pnl") or 0.0) <= 0.0:
        reason = "训练段未改善基准"
    else:
        reason = "训练段证据不足"
    return {
        "orders": orders,
        "baseline_pnl": baseline.get("pnl", 0.0),
        "baseline_ev": baseline.get("ev", 0.0),
        "traded_orders": traded.get("orders", 0),
        "traded_win_rate": traded.get("win_rate", 0.0),
        "traded_pnl": traded.get("pnl", 0.0),
        "traded_ev": traded.get("ev", 0.0),
        "blocked_orders": blocked_orders,
        "blocked_ev": blocked.get("ev", 0.0),
        "blocked_pnl": blocked.get("pnl", 0.0),
        "delta_pnl": variant.get("delta_pnl", 0.0),
        "stable": stable,
        "reason": reason,
    }


def _validation_for_key_subset(
    validation_samples: Sequence[dict[str, Any]],
    key_subset: Sequence[str],
    *,
    baseline: Mapping[str, Any],
    min_history: int,
    min_group_size: int,
) -> dict[str, Any]:
    if not validation_samples:
        return {
            "orders": 0,
            "stable": False,
            "reason": "验证段无样本",
            "delta_pnl": 0.0,
            "traded_ev": 0.0,
        }
    variant = _guard_variant(validation_samples, key_subset, "tail_validation_key_subset", baseline=dict(baseline))
    traded = variant.get("traded") or {}
    blocked = variant.get("blocked") or {}
    orders = int(baseline.get("orders") or 0)
    blocked_orders = int(blocked.get("orders") or 0)
    stable = (
        orders >= max(4, min_history // 2)
        and blocked_orders >= min(2, max(1, min_group_size))
        and float(variant.get("delta_pnl") or 0.0) > 0.0
        and float(traded.get("ev") or 0.0) >= float(baseline.get("ev") or 0.0)
        and float(blocked.get("ev") or 0.0) < 0.0
    )
    if stable:
        reason = "训练候选在验证段继续改善"
    elif blocked_orders == 0:
        reason = "验证段未命中该子集"
    elif float(variant.get("delta_pnl") or 0.0) <= 0.0:
        reason = "验证段未改善基准"
    else:
        reason = "验证段证据不足"
    return {
        "orders": orders,
        "baseline_pnl": baseline.get("pnl", 0.0),
        "baseline_ev": baseline.get("ev", 0.0),
        "traded_orders": traded.get("orders", 0),
        "traded_win_rate": traded.get("win_rate", 0.0),
        "traded_pnl": traded.get("pnl", 0.0),
        "traded_ev": traded.get("ev", 0.0),
        "blocked_orders": blocked_orders,
        "blocked_ev": blocked.get("ev", 0.0),
        "blocked_pnl": blocked.get("pnl", 0.0),
        "delta_pnl": variant.get("delta_pnl", 0.0),
        "stable": stable,
        "reason": reason,
    }


def _validation_start_index(total_orders: int, min_history: int) -> int:
    if total_orders <= 0:
        return 0
    tail = max(6, min(24, total_orders // 3))
    start = max(min_history, total_orders - tail)
    return min(total_orders, start)


def _accumulate_key_stats(stats_by_key: dict[str, dict[str, Any]], sample: Mapping[str, Any], keys: Sequence[str]) -> None:
    for key in keys:
        stats = stats_by_key.setdefault(key, {"orders": 0, "wins": 0, "pnl": 0.0})
        stats["orders"] += 1
        if sample.get("result") == "WIN":
            stats["wins"] += 1
        stats["pnl"] += float(sample.get("pnl") or 0.0)


def _is_weak_key_stats(stats: Mapping[str, Any], *, min_group_size: int) -> bool:
    orders = int(stats.get("orders") or 0)
    if orders < min_group_size:
        return False
    wins = int(stats.get("wins") or 0)
    pnl = float(stats.get("pnl") or 0.0)
    win_rate = wins / orders if orders else 0.0
    ev = pnl / orders if orders else 0.0
    return ev < 0.0 or win_rate < 0.5


def _replay_summary(samples: Sequence[dict[str, Any]]) -> dict[str, Any]:
    orders = len(samples)
    wins = 0
    pnl = 0.0
    stake_step = 1
    total_staked = 0.0

    for item in samples:
        stake = round(BASE_STAKE * (STAKE_MULTIPLIER ** (stake_step - 1)), 4)
        total_staked += stake
        if item["result"] == "WIN":
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


def _rejected_by_key(samples: Sequence[dict[str, Any]], risk_keys: Sequence[str]) -> dict[str, int]:
    key_set = set(risk_keys)
    counts: dict[str, int] = {}
    for item in samples:
        for key in key_set.intersection(risk_hint_keys_for_sample(item)):
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _blocked_key_contribution(blocked_records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in blocked_records:
        for key in record.get("risk_keys") or []:
            groups[str(key)].append(record)
    summaries = [_summary(rows, key) for key, rows in groups.items()]
    return sorted(summaries, key=lambda item: (item["ev"], item["win_rate"], -item["orders"], item["key"] or ""))


def _group_by_binner(
    samples: Sequence[dict[str, Any]],
    binner,
    min_group_size: int,
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in samples:
        groups[binner(item)].append(item)
    return sorted(
        (_summary(rows, key) for key, rows in groups.items() if len(rows) >= min_group_size),
        key=lambda item: (item["ev"], item["win_rate"], -item["orders"]),
    )


def _bin_value(value: Any, cutoffs: Sequence[float]) -> str:
    if value is None:
        return "NA"
    value = float(value)
    for start, end in zip(cutoffs, cutoffs[1:]):
        if start <= value < end:
            return f"[{start:g},{end:g})"
    return f">={cutoffs[-1]:g}"


def _sample_range(samples: Sequence[dict[str, Any]]) -> dict[str, str | None]:
    if not samples:
        return {"from": None, "to": None}
    return {
        "from": _iso(min(item["opened_at"] for item in samples)),
        "to": _iso(max(item["settled_at"] or item["opened_at"] for item in samples)),
    }


def _iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _get(mapping: Mapping[str, Any], key: str, default: Any = None) -> Any:
    try:
        return mapping.get(key, default)
    except AttributeError:
        try:
            return mapping[key]
        except (KeyError, IndexError):
            return default
