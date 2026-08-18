from __future__ import annotations

import math
import re
from bisect import bisect_left
from collections import Counter, deque
from dataclasses import dataclass
from typing import Sequence

from app.daily_profile_selector import _independent_samples, profile_key as build_profile_key
from app.models import ObservationSignal


ADAPTIVE_PROFILE_STATE_VERSION = "ADAPTIVE_PROFILE_STATE_V1"
_PROFILE_SEGMENT = re.compile(r"(?:WD|WE)-(?:0[0-9]|1[0-9]|2[0-3])\Z")
_KNOWN_STATES = {"WARMUP", "ACTIVE", "WATCH", "PAUSED"}


@dataclass(frozen=True)
class AdaptiveProfileStateConfig:
    warmup_samples: int = 12
    active_n12_wins: int = 7
    paused_n12_max_wins: int = 5
    full_window_samples: int = 20

    def __post_init__(self) -> None:
        values = {
            "warmup_samples": self.warmup_samples,
            "active_n12_wins": self.active_n12_wins,
            "paused_n12_max_wins": self.paused_n12_max_wins,
            "full_window_samples": self.full_window_samples,
        }
        for name, value in values.items():
            if type(value) is not int:
                raise TypeError(f"{name} must be an integer")
        if self.warmup_samples <= 0:
            raise ValueError("warmup_samples must be positive")
        if not 1 <= self.active_n12_wins <= self.warmup_samples:
            raise ValueError("active_n12_wins must fit the warmup window")
        if not 0 <= self.paused_n12_max_wins < self.active_n12_wins:
            raise ValueError("paused_n12_max_wins must be below active_n12_wins")
        if self.full_window_samples <= self.warmup_samples:
            raise ValueError("full_window_samples must be longer than the warmup window")


@dataclass
class _ProfileReplay:
    samples: deque[ObservationSignal]
    interval_index: _IntervalOverlapIndex
    candidates: list[ObservationSignal]
    selected_counts: Counter[tuple]
    seen_observation_keys: set[str]
    seen_decision_ids: set[str]
    previous_status: str | None = None


class _IntervalOverlapIndex:
    def __init__(self, candidates: Sequence[ObservationSignal]) -> None:
        self._opened_at = sorted({item.opened_at for item in candidates})
        size = 1
        while size < len(self._opened_at):
            size *= 2
        self._size = size
        self._max_expires = [-1] * (2 * size)

    def overlaps(self, item: ObservationSignal) -> bool:
        right = bisect_left(self._opened_at, item.expires_at)
        left = self._size
        right += self._size
        max_expires = -1
        while left < right:
            if left % 2:
                max_expires = max(max_expires, self._max_expires[left])
                left += 1
            if right % 2:
                right -= 1
                max_expires = max(max_expires, self._max_expires[right])
            left //= 2
            right //= 2
        return max_expires > item.opened_at

    def add(self, item: ObservationSignal) -> None:
        position = self._size + bisect_left(self._opened_at, item.opened_at)
        self._max_expires[position] = max(
            self._max_expires[position],
            item.expires_at,
        )
        position //= 2
        while position:
            self._max_expires[position] = max(
                self._max_expires[2 * position],
                self._max_expires[2 * position + 1],
            )
            position //= 2


def evaluate_adaptive_profile_state(
    observations: Sequence[ObservationSignal],
    profile_key: str,
    evaluated_at: int,
    previous: str | dict | None = None,
    config: AdaptiveProfileStateConfig | None = None,
) -> dict:
    resolved = _resolve_config(config)
    cutoff = _validated_evaluated_at(evaluated_at)
    _validate_profile_key(profile_key)
    samples = independent_settled_samples(observations, profile_key, cutoff)
    return classify_profile_state(
        samples,
        profile_key,
        cutoff,
        previous=previous,
        config=resolved,
    )


def classify_profile_state(
    samples: Sequence[ObservationSignal],
    profile_key_value: str,
    evaluated_at: int,
    *,
    previous: str | dict | None = None,
    config: AdaptiveProfileStateConfig | None = None,
) -> dict:
    resolved = _resolve_config(config)
    cutoff = _validated_evaluated_at(evaluated_at)
    _validate_profile_key(profile_key_value)
    recent = sorted(samples, key=_settlement_sort_key)[-resolved.full_window_samples :]
    n12_rows = recent[-resolved.warmup_samples :]
    n12 = _summary(n12_rows)
    n20 = _summary(recent)
    previous_status, previous_ignored_reason = _resolve_previous(
        previous,
        profile_key_value,
        cutoff,
    )

    mature = n12["sample_size"] >= resolved.warmup_samples
    full = n20["sample_size"] >= resolved.full_window_samples
    active = bool(
        mature
        and n12["wins"] >= resolved.active_n12_wins
        and (not full or n20["_raw_ev"] >= 0.0)
    )
    paused = bool(
        mature
        and n12["wins"] <= resolved.paused_n12_max_wins
        and full
        and n20["_raw_ev"] < 0.0
    )

    if not mature:
        status = "WARMUP"
        reason = (
            f"N12 independent samples {n12['sample_size']} "
            f"< {resolved.warmup_samples}"
        )
    elif previous_status == "PAUSED":
        if paused:
            status = "PAUSED"
            reason = "PAUSED conditions remain satisfied"
        else:
            status = "WATCH"
            reason = "PAUSED recovery is limited to WATCH for one transition"
    elif active:
        status = "ACTIVE"
        reason = "N12 wins and N20 EV satisfy ACTIVE conditions"
    elif paused:
        status = "PAUSED"
        reason = "N12 wins are at most the pause limit and full N20 EV is negative"
    else:
        status = "WATCH"
        reason = "Mature profile satisfies neither ACTIVE nor PAUSED conditions"
    if previous_ignored_reason:
        reason = f"{reason}; {previous_ignored_reason}"

    _remove_raw_metrics(n12)
    _remove_raw_metrics(n20)
    return {
        "version": ADAPTIVE_PROFILE_STATE_VERSION,
        "status": status,
        "reason": reason,
        "evaluated_at": cutoff,
        "profile_key": profile_key_value,
        "n12": n12,
        "n20": n20,
        "previous": previous_status,
        "transition": f"{previous_status or 'NONE'}->{status}",
        "previous_ignored_reason": previous_ignored_reason,
    }


def independent_settled_samples(
    observations: Sequence[ObservationSignal],
    profile_key: str,
    evaluated_at: int,
) -> list[ObservationSignal]:
    cutoff = _validated_evaluated_at(evaluated_at)
    _validate_profile_key(profile_key)
    candidates = []
    for item in observations:
        prepared = _prepare_event(item, cutoff)
        if prepared is not None and prepared[0] == profile_key:
            candidates.append(item)

    return _canonical_independent_samples(candidates, cutoff)


def _canonical_independent_samples(
    candidates: Sequence[ObservationSignal],
    evaluated_at: int,
) -> list[ObservationSignal]:
    ordered = sorted(candidates, key=_opened_sort_key)

    deduplicated = []
    seen_observation_keys: set[str] = set()
    seen_decision_ids: set[str] = set()
    for item in ordered:
        observation_identity = item.observation_key
        decision_identity = item.decision_id
        if observation_identity and observation_identity in seen_observation_keys:
            continue
        if decision_identity and decision_identity in seen_decision_ids:
            continue
        if observation_identity:
            seen_observation_keys.add(observation_identity)
        if decision_identity:
            seen_decision_ids.add(decision_identity)
        deduplicated.append(item)

    # The daily selector owns the opened/expires overlap boundary: touching
    # intervals are independent, while opened_at < previous expires_at overlaps.
    return _independent_samples(deduplicated, -(2**63), evaluated_at)


def rebuild_adaptive_profile_states(
    observations: Sequence[ObservationSignal],
    evaluated_at: int,
    config: AdaptiveProfileStateConfig | None = None,
) -> dict[str, dict]:
    resolved = _resolve_config(config)
    cutoff = _validated_evaluated_at(evaluated_at)
    events: list[tuple[str, ObservationSignal]] = []
    for item in observations:
        prepared = _prepare_event(item, cutoff)
        if prepared is not None:
            events.append(prepared)
    events.sort(key=_prepared_event_sort_key)

    candidates_by_profile: dict[str, list[ObservationSignal]] = {}
    for key, event in events:
        candidates_by_profile.setdefault(key, []).append(event)

    states: dict[str, dict] = {}
    profiles = {
        key: _ProfileReplay(
            samples=deque(maxlen=resolved.full_window_samples),
            interval_index=_IntervalOverlapIndex(candidates),
            candidates=[],
            selected_counts=Counter(),
            seen_observation_keys=set(),
            seen_decision_ids=set(),
        )
        for key, candidates in candidates_by_profile.items()
    }

    for key, event in events:
        replay = profiles[key]
        observation_identity = event.observation_key
        decision_identity = event.decision_id
        identity_conflict = bool(
            (observation_identity and observation_identity in replay.seen_observation_keys)
            or (decision_identity and decision_identity in replay.seen_decision_ids)
        )
        interval_conflict = replay.interval_index.overlaps(event)

        replay.candidates.append(event)
        replay.interval_index.add(event)
        if observation_identity:
            replay.seen_observation_keys.add(observation_identity)
        if decision_identity:
            replay.seen_decision_ids.add(decision_identity)

        if identity_conflict or interval_conflict:
            selected = _canonical_independent_samples(
                replay.candidates,
                int(event.settled_at) + 1,
            )
            selected_counts = Counter(_observation_signature(item) for item in selected)
            if selected_counts == replay.selected_counts:
                continue
            replay.selected_counts = selected_counts
            replay.samples = deque(
                sorted(selected, key=_settlement_sort_key)[
                    -resolved.full_window_samples :
                ],
                maxlen=resolved.full_window_samples,
            )
        else:
            replay.selected_counts[_observation_signature(event)] += 1
            replay.samples.append(event)

        event_cutoff = int(event.settled_at) + 1
        states[key] = classify_profile_state(
            replay.samples,
            key,
            event_cutoff,
            previous=replay.previous_status,
            config=resolved,
        )
        replay.previous_status = states[key]["status"]

    for state in states.values():
        state["evaluated_at"] = cutoff
    return states


def _observation_signature(item: ObservationSignal) -> tuple:
    return (
        item.observation_key,
        item.decision_id,
        item.opened_at,
        item.expires_at,
        item.settled_at,
        item.result,
        float(item.pnl).hex(),
    )


def _resolve_config(config: AdaptiveProfileStateConfig | None) -> AdaptiveProfileStateConfig:
    if config is None:
        return AdaptiveProfileStateConfig()
    if not isinstance(config, AdaptiveProfileStateConfig):
        raise TypeError("config must be an AdaptiveProfileStateConfig")
    return config


def _validated_evaluated_at(value: int) -> int:
    if type(value) is not int:
        raise TypeError("evaluated_at must be an integer timestamp")
    if value < 0:
        raise ValueError("evaluated_at must not be negative")
    return value


def _validate_profile_key(value: str) -> None:
    if not isinstance(value, str):
        raise TypeError("profile_key must be a string")
    parts = value.split("|")
    if len(parts) != 5:
        raise ValueError("profile_key must contain five complete components")
    timeframe, family, tag, direction, segment = parts
    if timeframe != "10" or not family or not tag:
        raise ValueError("profile_key must identify a complete 10-minute profile")
    if direction not in {"LONG", "SHORT"} or _PROFILE_SEGMENT.fullmatch(segment) is None:
        raise ValueError("profile_key must contain LONG/SHORT and a WD/WE-hour segment")
    if build_profile_key(10, family, tag, direction, segment) != value:
        raise ValueError("profile_key is not canonical")


def _eligible_observation(item: ObservationSignal, evaluated_at: int) -> bool:
    if not isinstance(item, ObservationSignal):
        return False
    timestamps = (item.opened_at, item.expires_at, item.settled_at)
    if any(type(value) is not int for value in timestamps):
        return False
    if (
        item.status != "SETTLED"
        or item.result not in {"WIN", "LOSS"}
        or item.opened_at < 0
        or not item.opened_at < item.expires_at <= item.settled_at
        or item.opened_at >= evaluated_at
        or item.settled_at >= evaluated_at
        or not isinstance(item.observation_key, str)
        or not isinstance(item.decision_id, str)
    ):
        return False
    if isinstance(item.pnl, bool):
        return False
    try:
        return math.isfinite(float(item.pnl))
    except (TypeError, ValueError, OverflowError):
        return False


def _prepare_event(
    item: ObservationSignal,
    evaluated_at: int,
) -> tuple[str, ObservationSignal] | None:
    if not _eligible_observation(item, evaluated_at):
        return None
    key = _safe_observation_profile_key(item)
    return None if key is None else (key, item)


def _safe_observation_profile_key(item: ObservationSignal) -> str | None:
    if type(item.timeframe_minutes) is not int or item.timeframe_minutes != 10:
        return None
    if not isinstance(item.strategy_family, str) or not item.strategy_family:
        return None
    if not isinstance(item.strategy_tag, str) or not item.strategy_tag:
        return None
    if item.direction not in {"LONG", "SHORT"}:
        return None
    if not isinstance(item.threshold_segment, str):
        return None
    if _PROFILE_SEGMENT.fullmatch(item.threshold_segment) is None:
        return None
    key = build_profile_key(
        item.timeframe_minutes,
        item.strategy_family,
        item.strategy_tag,
        item.direction,
        item.threshold_segment,
    )
    try:
        _validate_profile_key(key)
    except (TypeError, ValueError):
        return None
    return key


def _resolve_previous(
    previous: str | dict | None,
    target_profile_key: str,
    evaluated_at: int,
) -> tuple[str | None, str]:
    if previous is None:
        return None, ""
    if isinstance(previous, str):
        if previous in _KNOWN_STATES:
            return previous, ""
        return None, "PREVIOUS_STATUS_INVALID"
    if not isinstance(previous, dict):
        return None, "PREVIOUS_FORMAT_INVALID"
    if previous.get("version") != ADAPTIVE_PROFILE_STATE_VERSION:
        return None, "PREVIOUS_VERSION_INCOMPATIBLE"
    if previous.get("profile_key") != target_profile_key:
        return None, "PREVIOUS_PROFILE_KEY_MISMATCH"
    previous_evaluated_at = previous.get("evaluated_at")
    if type(previous_evaluated_at) is not int or previous_evaluated_at < 0:
        return None, "PREVIOUS_EVALUATED_AT_INVALID"
    if previous_evaluated_at >= evaluated_at:
        return None, "PREVIOUS_EVALUATED_AT_NOT_PAST"
    previous_status = previous.get("status")
    if previous_status not in _KNOWN_STATES:
        return None, "PREVIOUS_STATUS_INVALID"
    return previous_status, ""


def _summary(rows: Sequence[ObservationSignal]) -> dict:
    sample_size = len(rows)
    wins = sum(1 for item in rows if item.result == "WIN")
    raw_pnl = math.fsum(float(item.pnl) for item in rows)
    raw_win_rate = wins / sample_size if sample_size else 0.0
    raw_ev = raw_pnl / sample_size if sample_size else 0.0
    return {
        "sample_size": sample_size,
        "wins": wins,
        "losses": sample_size - wins,
        "win_rate": _display_number(raw_win_rate, 6),
        "pnl": _display_number(raw_pnl, 4),
        "ev": _display_number(raw_ev, 4),
        "_raw_ev": raw_ev,
    }


def _remove_raw_metrics(summary: dict) -> None:
    summary.pop("_raw_ev", None)


def _display_number(value: float, digits: int) -> float:
    displayed = round(value, digits)
    return 0.0 if displayed == 0 else displayed


def _opened_sort_key(item: ObservationSignal) -> tuple:
    return (
        item.opened_at,
        item.observation_key,
        item.decision_id,
        item.expires_at,
        int(item.settled_at),
        str(item.result),
        float(item.pnl),
    )


def _settlement_sort_key(item: ObservationSignal) -> tuple:
    return (
        int(item.settled_at),
        item.opened_at,
        item.observation_key,
        item.decision_id,
        item.expires_at,
        str(item.result),
        float(item.pnl),
    )


def _prepared_event_sort_key(prepared: tuple[str, ObservationSignal]) -> tuple:
    key, item = prepared
    return (
        item.settled_at,
        key,
        item.opened_at,
        item.observation_key,
        item.decision_id,
        item.expires_at,
        item.result,
        float(item.pnl).hex(),
    )
