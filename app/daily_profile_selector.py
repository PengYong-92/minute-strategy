from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
from typing import Sequence
from zoneinfo import ZoneInfo

from app.models import ObservationSignal


SHANGHAI = ZoneInfo("Asia/Shanghai")
QUALIFICATION_VERSION = "DAILY_PROFILE_QUALIFICATION_V2"


@dataclass(frozen=True)
class DailyProfileSelectorConfig:
    lookback_days: int = 7
    min_samples: int = 20
    weekend_min_samples: int = 10
    min_win_rate: float = 0.60
    min_ev: float = 0.0
    exit_win_rate: float = 0.60
    exit_ev: float = 0.0
    degraded_runs_to_exit: int = 2
    max_active_profiles: int = 0
    evaluation_hour: int = 7
    evaluation_minute: int = 50
    activation_hour: int = 8
    activation_minute: int = 0
    stable_lookback_days: int = 14
    joint_failures_to_exit: int | None = None
    joint_failures_source: str = field(default="", init=False)

    def normalized(self) -> "DailyProfileSelectorConfig":
        lookback_days = int(self.lookback_days)
        stable_lookback_days = int(self.stable_lookback_days)
        degraded_runs_to_exit = int(self.degraded_runs_to_exit)
        if lookback_days <= 0:
            raise ValueError("lookback_days must be positive")
        if stable_lookback_days <= 0:
            raise ValueError("stable_lookback_days must be positive")
        if stable_lookback_days < lookback_days:
            raise ValueError("stable_lookback_days must not be shorter than lookback_days")
        if degraded_runs_to_exit <= 0:
            raise ValueError("degraded_runs_to_exit must be positive")

        min_win_rate = float(self.min_win_rate)
        exit_win_rate = float(self.exit_win_rate)
        if not math.isfinite(min_win_rate) or not 0.0 <= min_win_rate <= 1.0:
            raise ValueError("min_win_rate must be between 0 and 1")
        if not math.isfinite(exit_win_rate) or not 0.0 <= exit_win_rate <= 1.0:
            raise ValueError("exit_win_rate must be between 0 and 1")
        min_ev = float(self.min_ev)
        exit_ev = float(self.exit_ev)
        if not math.isfinite(min_ev) or not math.isfinite(exit_ev):
            raise ValueError("EV thresholds must be finite")

        if self.joint_failures_to_exit is None:
            effective_failures = degraded_runs_to_exit
            source = (
                self.joint_failures_source
                or ("default" if degraded_runs_to_exit == 2 else "degraded_runs_to_exit")
            )
        else:
            effective_failures = int(self.joint_failures_to_exit)
            source = self.joint_failures_source or "joint_failures_to_exit"
        if effective_failures <= 0:
            raise ValueError("joint_failures_to_exit must be positive")

        normalized = DailyProfileSelectorConfig(
            lookback_days=lookback_days,
            min_samples=max(1, int(self.min_samples)),
            weekend_min_samples=max(1, int(self.weekend_min_samples)),
            min_win_rate=min_win_rate,
            min_ev=min_ev,
            exit_win_rate=exit_win_rate,
            exit_ev=exit_ev,
            degraded_runs_to_exit=degraded_runs_to_exit,
            max_active_profiles=max(0, int(self.max_active_profiles)),
            evaluation_hour=min(23, max(0, int(self.evaluation_hour))),
            evaluation_minute=min(59, max(0, int(self.evaluation_minute))),
            activation_hour=min(23, max(0, int(self.activation_hour))),
            activation_minute=min(59, max(0, int(self.activation_minute))),
            stable_lookback_days=stable_lookback_days,
            joint_failures_to_exit=effective_failures,
        )
        object.__setattr__(normalized, "joint_failures_source", source)
        return normalized


def profile_key(
    timeframe_minutes: int,
    strategy_family: str,
    strategy_tag: str,
    direction: str,
    threshold_segment: str,
) -> str:
    return "|".join(
        [
            str(int(timeframe_minutes)),
            str(strategy_family or "unknown"),
            str(strategy_tag or "unknown"),
            str(direction or "").upper(),
            str(threshold_segment or "GLOBAL").upper(),
        ]
    )


def selection_window(
    current_time_ms: int,
    *,
    lookback_days: int = 7,
    evaluation_hour: int = 7,
    evaluation_minute: int = 50,
    activation_hour: int = 8,
    activation_minute: int = 0,
) -> dict:
    current = datetime.fromtimestamp(current_time_ms / 1000, tz=SHANGHAI)
    evaluation_clock = time(evaluation_hour, evaluation_minute)
    evaluation_date = current.date()
    if current.timetz().replace(tzinfo=None) < evaluation_clock:
        evaluation_date -= timedelta(days=1)

    lookback_end_dt = datetime.combine(evaluation_date, evaluation_clock, tzinfo=SHANGHAI)
    effective_from_dt = datetime.combine(
        evaluation_date,
        time(activation_hour, activation_minute),
        tzinfo=SHANGHAI,
    )
    effective_until_dt = effective_from_dt + timedelta(days=1)
    return {
        "lookback_start": _timestamp_ms(lookback_end_dt - timedelta(days=max(1, int(lookback_days)))),
        "lookback_end": _timestamp_ms(lookback_end_dt),
        "effective_from": _timestamp_ms(effective_from_dt),
        "effective_until": _timestamp_ms(effective_until_dt),
    }


def build_daily_selection(
    observations: Sequence[ObservationSignal],
    evaluated_at_ms: int,
    *,
    config: DailyProfileSelectorConfig | None = None,
    previous_snapshot: dict | None = None,
) -> dict:
    resolved = (config or DailyProfileSelectorConfig()).normalized()
    fast_window = _selection_window_for(evaluated_at_ms, resolved.lookback_days, resolved)
    stable_window = _selection_window_for(
        evaluated_at_ms,
        resolved.stable_lookback_days,
        resolved,
    )

    grouped: dict[str, list[ObservationSignal]] = {}
    for item in observations:
        if not _eligible_for_window(item, stable_window):
            continue
        key = profile_key(
            item.timeframe_minutes,
            item.strategy_family,
            item.strategy_tag,
            item.direction,
            item.threshold_segment,
        )
        grouped.setdefault(key, []).append(item)

    previous_snapshot = previous_snapshot or {}
    previous_by_key = _previous_candidates(previous_snapshot)
    for key in previous_by_key:
        grouped.setdefault(key, [])

    evaluation_key = fast_window["lookback_end"]
    previous_evaluation_key = previous_snapshot.get(
        "evaluation_key",
        previous_snapshot.get("lookback_end"),
    )
    same_evaluation_day = previous_evaluation_key == evaluation_key
    candidates = [
        _candidate_summary(
            key,
            rows,
            previous_by_key.get(key),
            resolved,
            fast_window,
            stable_window,
            evaluation_key=evaluation_key,
            same_evaluation_day=same_evaluation_day,
        )
        for key, rows in grouped.items()
    ]
    selected = [item for item in candidates if item.pop("_selected")]
    selected.sort(key=_candidate_sort_key)
    limited = selected if resolved.max_active_profiles == 0 else selected[: resolved.max_active_profiles]
    selected_keys = {item["key"] for item in limited}
    for item in candidates:
        if item["key"] in selected_keys:
            continue
        if (
            resolved.max_active_profiles > 0
            and item["selection_state"] in {"SELECTED", "RETAINED", "QUALIFICATION_WATCH"}
        ):
            item["selection_state"] = "RANKED_OUT"
            item["selection_reason"] = f"合格画像超过上限 {resolved.max_active_profiles}，本日未启用"
            item["reason"] = item["selection_reason"]

    candidates.sort(key=_candidate_sort_key)
    selected_profiles = [item for item in candidates if item["key"] in selected_keys]
    effective_local = datetime.fromtimestamp(fast_window["effective_from"] / 1000, tz=SHANGHAI)
    config_snapshot = asdict(resolved)
    config_snapshot.update(
        {
            "effective_joint_failures_to_exit": resolved.joint_failures_to_exit,
            "retention_min_win_rate": resolved.exit_win_rate,
            "retention_min_ev": resolved.exit_ev,
        }
    )
    return {
        "version": f"DPS-{effective_local.strftime('%Y%m%d-%H%M')}",
        "status": "READY",
        "evaluated_at": int(evaluated_at_ms),
        "evaluation_key": evaluation_key,
        **fast_window,
        "fast_7d": _window_metadata(fast_window, resolved.lookback_days),
        "stable_14d": _window_metadata(stable_window, resolved.stable_lookback_days),
        "config": config_snapshot,
        "candidates": candidates,
        "selected_profiles": selected_profiles,
        "selected_count": len(selected_profiles),
        "reason": (
            f"启用 {len(selected_profiles)} 个画像"
            if selected_profiles
            else "最近7天没有画像达到启用条件"
        ),
    }


def _selection_window_for(
    evaluated_at_ms: int,
    lookback_days: int,
    config: DailyProfileSelectorConfig,
) -> dict:
    return selection_window(
        evaluated_at_ms,
        lookback_days=lookback_days,
        evaluation_hour=config.evaluation_hour,
        evaluation_minute=config.evaluation_minute,
        activation_hour=config.activation_hour,
        activation_minute=config.activation_minute,
    )


def _eligible_for_window(item: ObservationSignal, window: dict) -> bool:
    return bool(
        item.status == "SETTLED"
        and item.result in {"WIN", "LOSS"}
        and item.settled_at is not None
        and item.opened_at >= window["lookback_start"]
        and item.opened_at < window["lookback_end"]
        and item.settled_at < window["lookback_end"]
    )


def _previous_candidates(previous_snapshot: dict) -> dict[str, dict]:
    previous_by_key = {
        str(item.get("key", "")): dict(item)
        for item in previous_snapshot.get("candidates", [])
        if item.get("key")
    }
    selected_keys = set()
    for item in previous_snapshot.get("selected_profiles", []):
        key = str(item.get("key", ""))
        if not key:
            continue
        selected_keys.add(key)
        previous_by_key[key] = {**previous_by_key.get(key, {}), **item}
    for key, item in previous_by_key.items():
        item["_previously_selected"] = key in selected_keys
    return previous_by_key


def _candidate_summary(
    key: str,
    rows: Sequence[ObservationSignal],
    previous: dict | None,
    config: DailyProfileSelectorConfig,
    fast_window: dict,
    stable_window: dict,
    *,
    evaluation_key: int,
    same_evaluation_day: bool,
) -> dict:
    parts = key.split("|", 4)
    if len(parts) != 5:
        parts = ["0", "unknown", "unknown", "", "GLOBAL"]
    min_samples = (
        config.weekend_min_samples
        if parts[4].upper().startswith("WE-")
        else config.min_samples
    )
    fast = _window_summary(rows, fast_window, config.lookback_days, min_samples, config)
    stable = _window_summary(
        rows,
        stable_window,
        config.stable_lookback_days,
        min_samples,
        config,
    )

    joint_failure_runs = 0
    selected = False
    previously_selected = bool(previous and previous.get("_previously_selected"))
    candidate_same_evaluation = bool(
        previous
        and previous.get("evaluation_key", previous.get("lookback_end")) == evaluation_key
    )
    same_evaluation_day = same_evaluation_day or candidate_same_evaluation
    migration_state = str((previous or {}).get("migration_state", ""))
    migration_evaluation_key = (previous or {}).get("migration_evaluation_key")
    legacy_selected = previously_selected and not any(
        field in previous
        for field in (
            "fast_7d",
            "stable_14d",
            "qualification_state",
            "joint_failure_runs",
        )
    )
    same_day_migration = bool(
        previously_selected
        and migration_state == "LEGACY_SELECTED_MIGRATED"
        and migration_evaluation_key == evaluation_key
    )
    if legacy_selected or same_day_migration:
        selected = True
        qualification_state = "QUALIFIED"
        state = "RETAINED"
        reason = "旧版已启用画像迁移为双窗口合格状态"
        migration_state = "LEGACY_SELECTED_MIGRATED"
        migration_evaluation_key = evaluation_key
    elif previously_selected:
        if fast["retention_qualified"] or stable["retention_qualified"]:
            selected = True
            qualification_state = "QUALIFIED"
            state = "RETAINED"
            reason = "7天或14天窗口仍达到保留条件"
        else:
            previous_runs = _non_negative_int(previous.get("joint_failure_runs", 0))
            joint_failure_runs = previous_runs if same_evaluation_day else previous_runs + 1
            if joint_failure_runs >= config.joint_failures_to_exit:
                qualification_state = "DEGRADED_EXIT"
                state = "DEGRADED_EXIT"
                reason = f"连续 {joint_failure_runs} 次7天与14天窗口均未达标"
            else:
                selected = True
                qualification_state = "QUALIFICATION_WATCH"
                state = "QUALIFICATION_WATCH"
                reason = (
                    f"7天与14天窗口均未达标，保留观察 "
                    f"{joint_failure_runs}/{config.joint_failures_to_exit}"
                )
    elif fast["qualified"]:
        selected = True
        qualification_state = "QUALIFIED"
        state = "SELECTED"
        reason = "7天快速窗口达到新增画像启用条件"
    elif previous and previous.get("qualification_state") == "DEGRADED_EXIT":
        qualification_state = "DEGRADED_EXIT"
        joint_failure_runs = _non_negative_int(previous.get("joint_failure_runs", 0))
        state = "DEGRADED_EXIT"
        reason = "画像仍未重新达到7天快速启用条件"
    else:
        qualification_state = "NOT_QUALIFIED"
        state, reason = _entry_failure(fast, config)

    return {
        "key": key,
        "timeframe_minutes": int(parts[0]),
        "strategy_family": parts[1],
        "strategy_tag": parts[2],
        "direction": parts[3],
        "threshold_segment": parts[4],
        "evaluation_key": evaluation_key,
        "migration_state": migration_state,
        "migration_evaluation_key": migration_evaluation_key,
        "fast_7d": fast,
        "stable_14d": stable,
        "sample_size": fast["sample_size"],
        "min_samples_required": fast["min_samples_required"],
        "wins": fast["wins"],
        "losses": fast["losses"],
        "win_rate": fast["win_rate"],
        "pnl": fast["pnl"],
        "ev": fast["ev"],
        "qualification_state": qualification_state,
        "joint_failure_runs": joint_failure_runs,
        "degraded_runs": joint_failure_runs,
        "selection_state": state,
        "selection_reason": reason,
        "reason": reason,
        "version": QUALIFICATION_VERSION,
        "_selected": selected,
    }


def _window_summary(
    rows: Sequence[ObservationSignal],
    window: dict,
    lookback_days: int,
    min_samples: int,
    config: DailyProfileSelectorConfig,
) -> dict:
    samples = _independent_samples(rows, window["lookback_start"], window["lookback_end"])
    sample_size = len(samples)
    wins = sum(1 for item in samples if item.result == "WIN")
    raw_pnl = math.fsum(float(item.pnl) for item in samples)
    raw_win_rate = wins / sample_size if sample_size else 0.0
    raw_ev = raw_pnl / sample_size if sample_size else 0.0
    entry_win_rate_qualified = raw_win_rate >= config.min_win_rate
    entry_ev_qualified = raw_ev >= config.min_ev
    qualified = (
        sample_size >= min_samples
        and entry_win_rate_qualified
        and entry_ev_qualified
    )
    retention_qualified = (
        sample_size >= min_samples
        and raw_win_rate >= config.exit_win_rate
        and raw_ev >= config.exit_ev
    )
    return {
        "lookback_days": lookback_days,
        "lookback_start": window["lookback_start"],
        "lookback_end": window["lookback_end"],
        "sample_size": sample_size,
        "min_samples_required": min_samples,
        "wins": wins,
        "losses": sample_size - wins,
        "win_rate": _display_number(raw_win_rate, 6),
        "pnl": _display_number(raw_pnl, 4),
        "ev": _display_number(raw_ev, 4),
        "qualified": qualified,
        "win_rate_qualified": entry_win_rate_qualified,
        "ev_qualified": entry_ev_qualified,
        "retention_qualified": retention_qualified,
    }


def _independent_samples(
    rows: Sequence[ObservationSignal],
    lookback_start: int,
    lookback_end: int,
) -> list[ObservationSignal]:
    samples = []
    seen_observation_keys = set()
    next_independent_at = 0
    for item in sorted(rows, key=lambda row: (row.opened_at, row.observation_key)):
        if (
            item.opened_at < lookback_start
            or item.opened_at >= lookback_end
            or item.settled_at is None
            or item.settled_at >= lookback_end
        ):
            continue
        identity = str(item.observation_key or "")
        if identity and identity in seen_observation_keys:
            continue
        if identity:
            seen_observation_keys.add(identity)
        if item.opened_at < next_independent_at:
            continue
        samples.append(item)
        next_independent_at = item.expires_at
    return samples


def _entry_failure(
    fast: dict,
    config: DailyProfileSelectorConfig,
) -> tuple[str, str]:
    if fast["sample_size"] < fast["min_samples_required"]:
        return (
            "INSUFFICIENT_SAMPLES",
            f"独立样本 {fast['sample_size']} < {fast['min_samples_required']}",
        )
    if not fast["win_rate_qualified"]:
        return (
            "LOW_WIN_RATE",
            f"胜率 {fast['win_rate']:.2%} < {config.min_win_rate:.2%}",
        )
    return "LOW_EV", f"EV {fast['ev']:.2f}U < {config.min_ev:.2f}U"


def _window_metadata(window: dict, lookback_days: int) -> dict:
    return {
        "lookback_days": lookback_days,
        "lookback_start": window["lookback_start"],
        "lookback_end": window["lookback_end"],
    }


def _non_negative_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _display_number(value: float, digits: int) -> float:
    displayed = round(value, digits)
    return 0.0 if displayed == 0 else displayed


def _candidate_sort_key(item: dict) -> tuple:
    return (-item["win_rate"], -item["ev"], -item["sample_size"], item["key"])


def _timestamp_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)
