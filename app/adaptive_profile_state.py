from __future__ import annotations

import math
import re
from bisect import bisect_left, bisect_right
from collections import deque
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
    selection: _IncrementalCanonicalSelection
    previous_status: str | None = None


@dataclass(frozen=True)
class _ReplayCandidate:
    event: ObservationSignal
    order_key: tuple
    serial: int


@dataclass(frozen=True)
class _SelectionUpdate:
    changed: bool
    append_only: ObservationSignal | None = None


class _IdentityBindings:
    def __init__(self) -> None:
        self._decisions_by_observation: dict[str, tuple[str, str]] = {}
        self._observations_by_decision: dict[str, tuple[str, str]] = {}

    def accept(self, profile_key: str, event: ObservationSignal) -> bool:
        observation_identity = event.observation_key
        decision_identity = event.decision_id
        if (
            observation_identity
            and observation_identity in self._decisions_by_observation
            and self._decisions_by_observation[observation_identity]
            != (decision_identity, profile_key)
        ):
            return False
        if (
            decision_identity
            and decision_identity in self._observations_by_decision
            and self._observations_by_decision[decision_identity]
            != (observation_identity, profile_key)
        ):
            return False
        if observation_identity:
            self._decisions_by_observation.setdefault(
                observation_identity,
                (decision_identity, profile_key),
            )
        if decision_identity:
            self._observations_by_decision.setdefault(
                decision_identity,
                (observation_identity, profile_key),
            )
        return True


class _IncrementalCanonicalSelection:
    def __init__(self) -> None:
        self._serial = 0
        self._identity_selected: list[_ReplayCandidate] = []
        self._identity_keys: list[tuple] = []
        self._identity_opened_at: list[int] = []
        self._interval_selected: list[_ReplayCandidate] = []
        self._interval_keys: list[tuple] = []
        self._observation_owners: dict[str, _ReplayCandidate] = {}
        self._decision_owners: dict[str, _ReplayCandidate] = {}

    @property
    def selected_events(self) -> list[ObservationSignal]:
        return [candidate.event for candidate in self._interval_selected]

    def add(self, event: ObservationSignal) -> _SelectionUpdate:
        candidate = _ReplayCandidate(
            event=event,
            order_key=(*_opened_sort_key(event), self._serial),
            serial=self._serial,
        )
        self._serial += 1

        observation_owner = (
            self._observation_owners.get(event.observation_key)
            if event.observation_key
            else None
        )
        decision_owner = (
            self._decision_owners.get(event.decision_id)
            if event.decision_id
            else None
        )
        owners = [owner for owner in (observation_owner, decision_owner) if owner]
        if any(owner.order_key < candidate.order_key for owner in owners):
            return _SelectionUpdate(False)

        if not owners:
            self._insert_identity_candidate(candidate)
            return self._add_unique_interval_candidate(candidate)

        owner = owners[0]
        exact_owner_replacement = (
            all(item is owner for item in owners)
            and owner.event.observation_key == event.observation_key
            and owner.event.decision_id == event.decision_id
        )
        if not exact_owner_replacement:
            return _SelectionUpdate(False)
        before = self._canonical_signature()
        self._replace_identity_candidate(owner, candidate)
        self._repair_interval_suffix(
            min(owner.order_key, candidate.order_key),
            convergence_after=owner.order_key,
        )
        return _SelectionUpdate(self._canonical_signature() != before)

    def _insert_identity_candidate(self, candidate: _ReplayCandidate) -> None:
        position = bisect_right(self._identity_keys, candidate.order_key)
        self._identity_keys.insert(position, candidate.order_key)
        self._identity_opened_at.insert(position, candidate.event.opened_at)
        self._identity_selected.insert(position, candidate)
        self._set_owner(candidate)

    def _replace_identity_candidate(
        self,
        owner: _ReplayCandidate,
        candidate: _ReplayCandidate,
    ) -> None:
        position = bisect_left(self._identity_keys, owner.order_key)
        self._identity_keys.pop(position)
        self._identity_opened_at.pop(position)
        self._identity_selected.pop(position)
        self._insert_identity_candidate(candidate)

    def _set_owner(self, candidate: _ReplayCandidate) -> None:
        if candidate.event.observation_key:
            self._observation_owners[candidate.event.observation_key] = candidate
        if candidate.event.decision_id:
            self._decision_owners[candidate.event.decision_id] = candidate

    def _add_unique_interval_candidate(
        self,
        candidate: _ReplayCandidate,
    ) -> _SelectionUpdate:
        successor = bisect_left(self._interval_keys, candidate.order_key)
        if successor:
            predecessor = self._interval_selected[successor - 1]
            if candidate.event.opened_at < predecessor.event.expires_at:
                return _SelectionUpdate(False)
        if (
            successor == len(self._interval_selected)
            or candidate.event.expires_at
            <= self._interval_selected[successor].event.opened_at
        ):
            self._interval_keys.insert(successor, candidate.order_key)
            self._interval_selected.insert(successor, candidate)
            return _SelectionUpdate(True, append_only=candidate.event)

        before = self._canonical_signature()
        self._repair_interval_suffix(
            candidate.order_key,
            convergence_after=candidate.order_key,
        )
        return _SelectionUpdate(self._canonical_signature() != before)

    def _repair_interval_suffix(
        self,
        affected_key: tuple,
        *,
        convergence_after: tuple | None = None,
    ) -> None:
        old_selected = self._interval_selected
        old_keys = self._interval_keys
        prefix_end = bisect_left(old_keys, affected_key)
        selected = old_selected[:prefix_end]
        next_independent_at = selected[-1].event.expires_at if selected else 0
        old_expires = [candidate.event.expires_at for candidate in old_selected]
        identity_start = bisect_left(self._identity_keys, affected_key)
        identity_position = identity_start

        while identity_position < len(self._identity_selected):
            identity_position = bisect_left(
                self._identity_opened_at,
                next_independent_at,
                identity_position,
            )
            if identity_position >= len(self._identity_selected):
                break
            candidate = self._identity_selected[identity_position]
            selected.append(candidate)
            next_independent_at = candidate.event.expires_at
            old_position = bisect_left(old_expires, next_independent_at)
            if (
                convergence_after is not None
                and old_position < len(old_selected)
                and old_expires[old_position] == next_independent_at
                and old_selected[old_position].order_key >= convergence_after
                and old_selected[old_position].order_key >= candidate.order_key
            ):
                selected.extend(old_selected[old_position + 1 :])
                break
            identity_position += 1

        self._interval_selected = selected
        self._interval_keys = [candidate.order_key for candidate in selected]

    def _canonical_signature(self) -> tuple:
        return tuple(
            _observation_signature(candidate.event)
            for candidate in self._interval_selected
        )


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
    prepared_events = []
    for item in observations:
        prepared = _prepare_event(item, cutoff)
        if prepared is not None:
            prepared_events.append(prepared)
    candidates = [
        event
        for key, event in _causally_bound_prepared_events(prepared_events)
        if key == profile_key
    ]

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


def _causally_bound_prepared_events(
    events: Sequence[tuple[str, ObservationSignal]],
) -> list[tuple[str, ObservationSignal]]:
    bindings = _IdentityBindings()
    accepted = []
    for key, event in sorted(events, key=_prepared_event_sort_key):
        if bindings.accept(key, event):
            accepted.append((key, event))
    return accepted


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
    events = _causally_bound_prepared_events(events)

    states: dict[str, dict] = {}
    profiles: dict[str, _ProfileReplay] = {}

    for key, event in events:
        replay = profiles.get(key)
        if replay is None:
            replay = _ProfileReplay(
                samples=deque(maxlen=resolved.full_window_samples),
                selection=_IncrementalCanonicalSelection(),
            )
            profiles[key] = replay

        update = replay.selection.add(event)
        if not update.changed:
            continue
        if update.append_only is not None:
            replay.samples.append(update.append_only)
        else:
            replay.samples = deque(
                sorted(replay.selection.selected_events, key=_settlement_sort_key)[
                    -resolved.full_window_samples :
                ],
                maxlen=resolved.full_window_samples,
            )

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


def _causal_observation_sort_key(item: ObservationSignal) -> tuple:
    return (
        item.settled_at,
        item.opened_at,
        item.observation_key,
        item.decision_id,
        item.expires_at,
        item.result,
        float(item.pnl).hex(),
    )


def _prepared_event_sort_key(prepared: tuple[str, ObservationSignal]) -> tuple:
    key, item = prepared
    causal_key = _causal_observation_sort_key(item)
    return (
        causal_key[0],
        key,
        *causal_key[1:],
    )
