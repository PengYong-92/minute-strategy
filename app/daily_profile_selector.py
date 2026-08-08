from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from typing import Sequence
from zoneinfo import ZoneInfo

from app.models import ObservationSignal


SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class DailyProfileSelectorConfig:
    lookback_days: int = 7
    min_samples: int = 20
    weekend_min_samples: int = 10
    min_win_rate: float = 0.60
    min_ev: float = 0.0
    exit_win_rate: float = 0.60
    exit_ev: float = 0.0
    degraded_runs_to_exit: int = 1
    max_active_profiles: int = 0
    evaluation_hour: int = 7
    evaluation_minute: int = 50
    activation_hour: int = 8
    activation_minute: int = 0

    def normalized(self) -> "DailyProfileSelectorConfig":
        return DailyProfileSelectorConfig(
            lookback_days=max(1, int(self.lookback_days)),
            min_samples=max(1, int(self.min_samples)),
            weekend_min_samples=max(1, int(self.weekend_min_samples)),
            min_win_rate=min(1.0, max(0.0, float(self.min_win_rate))),
            min_ev=float(self.min_ev),
            exit_win_rate=min(1.0, max(0.0, float(self.exit_win_rate))),
            exit_ev=float(self.exit_ev),
            degraded_runs_to_exit=max(1, int(self.degraded_runs_to_exit)),
            max_active_profiles=max(0, int(self.max_active_profiles)),
            evaluation_hour=min(23, max(0, int(self.evaluation_hour))),
            evaluation_minute=min(59, max(0, int(self.evaluation_minute))),
            activation_hour=min(23, max(0, int(self.activation_hour))),
            activation_minute=min(59, max(0, int(self.activation_minute))),
        )


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
    window = selection_window(
        evaluated_at_ms,
        lookback_days=resolved.lookback_days,
        evaluation_hour=resolved.evaluation_hour,
        evaluation_minute=resolved.evaluation_minute,
        activation_hour=resolved.activation_hour,
        activation_minute=resolved.activation_minute,
    )
    grouped: dict[str, list[ObservationSignal]] = {}
    for item in observations:
        if (
            item.status != "SETTLED"
            or item.result not in {"WIN", "LOSS"}
            or item.settled_at is None
            or item.opened_at < window["lookback_start"]
            or item.opened_at >= window["lookback_end"]
            or item.settled_at >= window["lookback_end"]
        ):
            continue
        key = profile_key(
            item.timeframe_minutes,
            item.strategy_family,
            item.strategy_tag,
            item.direction,
            item.threshold_segment,
        )
        grouped.setdefault(key, []).append(item)

    previous_by_key = {
        str(item.get("key", "")): item
        for item in (previous_snapshot or {}).get("selected_profiles", [])
        if item.get("key")
    }
    for key in previous_by_key:
        grouped.setdefault(key, [])

    candidates = [
        _candidate_summary(key, rows, previous_by_key.get(key), resolved)
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
            and item["selection_state"] in {"SELECTED", "RETAINED", "RETAINED_DEGRADED"}
        ):
            item["selection_state"] = "RANKED_OUT"
            item["selection_reason"] = f"合格画像超过上限 {resolved.max_active_profiles}，本日未启用"

    candidates.sort(key=_candidate_sort_key)
    selected_profiles = [item for item in candidates if item["key"] in selected_keys]
    effective_local = datetime.fromtimestamp(window["effective_from"] / 1000, tz=SHANGHAI)
    return {
        "version": f"DPS-{effective_local.strftime('%Y%m%d-%H%M')}",
        "status": "READY",
        "evaluated_at": int(evaluated_at_ms),
        **window,
        "config": asdict(resolved),
        "candidates": candidates,
        "selected_profiles": selected_profiles,
        "selected_count": len(selected_profiles),
        "reason": (
            f"启用 {len(selected_profiles)} 个画像"
            if selected_profiles
            else "最近7天没有画像达到启用条件"
        ),
    }


def _candidate_summary(
    key: str,
    rows: Sequence[ObservationSignal],
    previous: dict | None,
    config: DailyProfileSelectorConfig,
) -> dict:
    samples = []
    next_independent_at = 0
    for item in sorted(rows, key=lambda row: (row.opened_at, row.observation_key)):
        if item.opened_at < next_independent_at:
            continue
        samples.append(item)
        next_independent_at = item.expires_at

    parts = key.split("|", 4)
    if len(parts) != 5:
        parts = ["0", "unknown", "unknown", "", "GLOBAL"]
    sample_size = len(samples)
    wins = sum(1 for item in samples if item.result == "WIN")
    pnl = round(sum(float(item.pnl) for item in samples), 4)
    win_rate = wins / sample_size if sample_size else 0.0
    ev = round(pnl / sample_size, 4) if sample_size else 0.0
    min_samples = (
        config.weekend_min_samples
        if parts[4].upper().startswith("WE-")
        else config.min_samples
    )
    entry_qualified = (
        sample_size >= min_samples
        and win_rate >= config.min_win_rate
        and ev >= config.min_ev
    )

    degraded_runs = 0
    selected = False
    if previous is not None:
        degraded = (
            sample_size < min_samples
            or win_rate < config.exit_win_rate
            or ev <= config.exit_ev
        )
        degraded_runs = int(previous.get("degraded_runs", 0)) + 1 if degraded else 0
        if degraded and degraded_runs >= config.degraded_runs_to_exit:
            state = "DEGRADED_EXIT"
            reason = f"连续 {degraded_runs} 次低于退出条件"
        elif degraded:
            selected = True
            state = "RETAINED_DEGRADED"
            reason = f"本次退化，保留观察 {degraded_runs}/{config.degraded_runs_to_exit}"
        else:
            selected = True
            state = "RETAINED"
            reason = "历史启用画像仍高于退出条件"
    elif entry_qualified:
        selected = True
        state = "SELECTED"
        reason = "达到新增画像启用条件"
    elif sample_size < min_samples:
        state = "INSUFFICIENT_SAMPLES"
        reason = f"独立样本 {sample_size} < {min_samples}"
    elif win_rate < config.min_win_rate:
        state = "LOW_WIN_RATE"
        reason = f"胜率 {win_rate:.2%} < {config.min_win_rate:.2%}"
    else:
        state = "LOW_EV"
        reason = f"EV {ev:.2f}U < {config.min_ev:.2f}U"

    return {
        "key": key,
        "timeframe_minutes": int(parts[0]),
        "strategy_family": parts[1],
        "strategy_tag": parts[2],
        "direction": parts[3],
        "threshold_segment": parts[4],
        "sample_size": sample_size,
        "min_samples_required": min_samples,
        "wins": wins,
        "losses": sample_size - wins,
        "win_rate": round(win_rate, 6),
        "pnl": pnl,
        "ev": ev,
        "degraded_runs": degraded_runs,
        "selection_state": state,
        "selection_reason": reason,
        "_selected": selected,
    }


def _candidate_sort_key(item: dict) -> tuple:
    return (-item["win_rate"], -item["ev"], -item["sample_size"], item["key"])


def _timestamp_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)
