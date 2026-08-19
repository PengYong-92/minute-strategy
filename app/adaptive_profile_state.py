from __future__ import annotations

import math
import re
from bisect import bisect_left
from collections import deque
from copy import deepcopy
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
    selection: _IncrementalCanonicalSelection
    previous_status: str | None = None


@dataclass(frozen=True)
class _ReplayCandidate:
    event: ObservationSignal
    order_key: tuple
    settlement_key: tuple
    serial: int


@dataclass(frozen=True)
class _SelectionUpdate:
    changed: bool


class _ChunkedSortedIndex:
    _TARGET_BLOCK_SIZE = 192

    def __init__(self) -> None:
        self._blocks: list[list[tuple[tuple, _ReplayCandidate]]] = []
        self._max_keys: list[tuple] = []

    def insert(self, key: tuple, value: _ReplayCandidate) -> None:
        if not self._blocks:
            self._blocks.append([(key, value)])
            self._max_keys.append(key)
            return

        block_index = bisect_left(self._max_keys, key)
        if block_index == len(self._blocks):
            block_index -= 1
        block = self._blocks[block_index]
        position = self._block_lower_bound(block, key)
        if position < len(block) and block[position][0] == key:
            raise ValueError("sorted index keys must be unique")
        block.insert(position, (key, value))
        self._max_keys[block_index] = block[-1][0]
        if len(block) > self._TARGET_BLOCK_SIZE * 2:
            midpoint = len(block) // 2
            right = block[midpoint:]
            del block[midpoint:]
            self._blocks.insert(block_index + 1, right)
            self._max_keys[block_index] = block[-1][0]
            self._max_keys.insert(block_index + 1, right[-1][0])

    def remove(self, key: tuple) -> _ReplayCandidate | None:
        location = self._lower_bound_location(key)
        if location is None:
            return None
        block_index, position = location
        block = self._blocks[block_index]
        if block[position][0] != key:
            return None
        _, value = block.pop(position)
        if not block:
            self._blocks.pop(block_index)
            self._max_keys.pop(block_index)
            return value
        self._max_keys[block_index] = block[-1][0]
        self._merge_small_block(block_index)
        return value

    def lower_bound(self, key: tuple) -> _ReplayCandidate | None:
        location = self._lower_bound_location(key)
        if location is None:
            return None
        block_index, position = location
        return self._blocks[block_index][position][1]

    def predecessor(self, key: tuple) -> _ReplayCandidate | None:
        if not self._blocks:
            return None
        block_index = bisect_left(self._max_keys, key)
        if block_index == len(self._blocks):
            return self._blocks[-1][-1][1]
        block = self._blocks[block_index]
        position = self._block_lower_bound(block, key)
        if position:
            return block[position - 1][1]
        if block_index:
            return self._blocks[block_index - 1][-1][1]
        return None

    def replace_range(
        self,
        start_key: tuple,
        end_key: tuple | None,
        values: Sequence[_ReplayCandidate],
    ) -> list[_ReplayCandidate]:
        removed = []
        while True:
            candidate = self.lower_bound(start_key)
            if candidate is None or (
                end_key is not None and candidate.order_key >= end_key
            ):
                break
            removed_candidate = self.remove(candidate.order_key)
            if removed_candidate is not None:
                removed.append(removed_candidate)
        for candidate in values:
            self.insert(candidate.order_key, candidate)
        return removed

    def tail_values(self, limit: int) -> list[_ReplayCandidate]:
        if limit <= 0:
            return []
        tail = []
        remaining = limit
        for block in reversed(self._blocks):
            take = min(remaining, len(block))
            tail.extend(value for _, value in reversed(block[-take:]))
            remaining -= take
            if remaining == 0:
                break
        tail.reverse()
        return tail

    def _lower_bound_location(self, key: tuple) -> tuple[int, int] | None:
        block_index = bisect_left(self._max_keys, key)
        if block_index == len(self._blocks):
            return None
        block = self._blocks[block_index]
        return block_index, self._block_lower_bound(block, key)

    @staticmethod
    def _block_lower_bound(
        block: Sequence[tuple[tuple, _ReplayCandidate]],
        key: tuple,
    ) -> int:
        keys = [entry[0] for entry in block]
        return bisect_left(keys, key)

    def _merge_small_block(self, block_index: int) -> None:
        minimum = self._TARGET_BLOCK_SIZE // 2
        block = self._blocks[block_index]
        if len(block) >= minimum or len(self._blocks) == 1:
            return
        if block_index and (
            len(self._blocks[block_index - 1]) + len(block)
            <= self._TARGET_BLOCK_SIZE * 2
        ):
            left = self._blocks[block_index - 1]
            left.extend(block)
            self._blocks.pop(block_index)
            self._max_keys[block_index - 1] = left[-1][0]
            self._max_keys.pop(block_index)
            return
        if block_index + 1 < len(self._blocks) and (
            len(block) + len(self._blocks[block_index + 1])
            <= self._TARGET_BLOCK_SIZE * 2
        ):
            right = self._blocks.pop(block_index + 1)
            block.extend(right)
            self._max_keys[block_index] = block[-1][0]
            self._max_keys.pop(block_index + 1)


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
        self._identity_by_opened = _ChunkedSortedIndex()
        self._canonical_by_opened = _ChunkedSortedIndex()
        self._canonical_by_settlement = _ChunkedSortedIndex()
        self._canonical_serials: set[int] = set()
        self._observation_owners: dict[str, _ReplayCandidate] = {}
        self._decision_owners: dict[str, _ReplayCandidate] = {}

    def latest_selected_events(self, limit: int) -> list[ObservationSignal]:
        return [
            candidate.event
            for candidate in self._canonical_by_settlement.tail_values(limit)
        ]

    def add(self, event: ObservationSignal) -> _SelectionUpdate:
        candidate = _ReplayCandidate(
            event=event,
            order_key=(*_opened_sort_key(event), self._serial),
            settlement_key=(*_settlement_sort_key(event), self._serial),
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
            return _SelectionUpdate(self._repair_interval_selection(candidate.order_key))

        owner = owners[0]
        exact_owner_replacement = (
            all(item is owner for item in owners)
            and owner.event.observation_key == event.observation_key
            and owner.event.decision_id == event.decision_id
        )
        if not exact_owner_replacement:
            return _SelectionUpdate(False)
        affected_key = min(owner.order_key, candidate.order_key)
        convergence_after = (
            owner.order_key if owner.serial in self._canonical_serials else None
        )
        self._replace_identity_candidate(owner, candidate)
        return _SelectionUpdate(
            self._repair_interval_selection(
                affected_key,
                convergence_after=convergence_after,
            )
        )

    def _insert_identity_candidate(self, candidate: _ReplayCandidate) -> None:
        self._identity_by_opened.insert(candidate.order_key, candidate)
        self._set_owner(candidate)

    def _replace_identity_candidate(
        self,
        owner: _ReplayCandidate,
        candidate: _ReplayCandidate,
    ) -> None:
        self._identity_by_opened.remove(owner.order_key)
        self._insert_identity_candidate(candidate)

    def _set_owner(self, candidate: _ReplayCandidate) -> None:
        if candidate.event.observation_key:
            self._observation_owners[candidate.event.observation_key] = candidate
        if candidate.event.decision_id:
            self._decision_owners[candidate.event.decision_id] = candidate

    def _repair_interval_selection(
        self,
        affected_key: tuple,
        *,
        convergence_after: tuple | None = None,
    ) -> bool:
        predecessor = self._canonical_by_opened.predecessor(affected_key)
        next_independent_at = predecessor.event.expires_at if predecessor else 0
        search_key = max(affected_key, (next_independent_at,))
        selected = []
        convergence_key = None

        while True:
            candidate = self._identity_by_opened.lower_bound(search_key)
            if candidate is None:
                break
            # Reusing the same selected node preserves the remaining greedy chain.
            if (
                candidate.serial in self._canonical_serials
                and (
                    convergence_after is None
                    or candidate.order_key > convergence_after
                )
            ):
                convergence_key = candidate.order_key
                break
            selected.append(candidate)
            next_independent_at = candidate.event.expires_at
            search_key = (next_independent_at,)

        removed = self._canonical_by_opened.replace_range(
            affected_key,
            convergence_key,
            selected,
        )
        for candidate in removed:
            self._canonical_serials.remove(candidate.serial)
            self._canonical_by_settlement.remove(candidate.settlement_key)
        for candidate in selected:
            self._canonical_serials.add(candidate.serial)
            self._canonical_by_settlement.insert(candidate.settlement_key, candidate)
        return bool(removed or selected)


class _MonotonicWindowIndex:
    """Index greedy interval successors for monotonic opened-time events."""

    def __init__(self) -> None:
        self.events: list[ObservationSignal] = []
        self.opened_at: list[int] = []
        self._successors: dict[int, int] = {}
        self._jumps: dict[tuple[int, int], int] = {}
        self.work_units = 0

    def append(self, event: ObservationSignal) -> bool:
        monotonic = not self.events or (
            _opened_sort_key(self.events[-1]) <= _opened_sort_key(event)
        )
        self.events.append(event)
        self.opened_at.append(int(event.opened_at))
        self.work_units += 1
        return monotonic

    def selected_tail(
        self,
        start: int,
        end: int,
        limit: int,
    ) -> tuple[list[ObservationSignal], int]:
        if not 0 <= start <= end < len(self.events):
            raise ValueError("active monotonic window indices are invalid")
        current = start
        selected_count = 1
        max_power = max(0, (end - start + 1).bit_length() - 1)
        for power in range(max_power, -1, -1):
            target = self._jump(current, power)
            if target is not None and target <= end:
                current = target
                selected_count += 1 << power

        tail_count = min(limit, selected_count)
        skip = selected_count - tail_count
        current = start
        power = 0
        while skip:
            if skip & 1:
                target = self._jump(current, power)
                if target is None or target > end:
                    raise RuntimeError("monotonic interval jump index is inconsistent")
                current = target
            skip >>= 1
            power += 1

        tail = [self.events[current]]
        while len(tail) < tail_count:
            target = self._successor(current)
            if target is None or target > end:
                raise RuntimeError("monotonic interval tail is incomplete")
            current = target
            tail.append(self.events[current])
        self.work_units += len(tail)
        return tail, selected_count

    def _successor(self, index: int) -> int | None:
        cached = self._successors.get(index)
        if cached is not None:
            return cached
        self.work_units += 1
        target = bisect_left(
            self.opened_at,
            int(self.events[index].expires_at),
            index + 1,
        )
        if target == len(self.events):
            return None
        self._successors[index] = target
        return target

    def _jump(self, index: int, power: int) -> int | None:
        if power == 0:
            return self._successor(index)
        if index + (1 << power) >= len(self.events):
            return None
        cache_key = (index, power)
        cached = self._jumps.get(cache_key)
        if cached is not None:
            return cached
        midpoint = self._jump(index, power - 1)
        if midpoint is None:
            return None
        target = self._jump(midpoint, power - 1)
        if target is None:
            return None
        self._jumps[cache_key] = target
        self.work_units += 1
        return target


class AdaptiveProfileWindowReplay:
    """Replay causally ordered events over an exact settlement-time window."""

    def __init__(
        self,
        profile_key: str,
        *,
        lookback_ms: int,
        config: AdaptiveProfileStateConfig | None = None,
    ) -> None:
        _validate_profile_key(profile_key)
        if type(lookback_ms) is not int or lookback_ms <= 0:
            raise ValueError("lookback_ms must be a positive integer")
        self.profile_key = profile_key
        self.lookback_ms = lookback_ms
        self.config = _resolve_config(config)
        self._events: deque[ObservationSignal] = deque()
        self._index = _MonotonicWindowIndex()
        self._active_start = 0
        self._opened_monotonic = True
        self._observation_counts: dict[str, int] = {}
        self._decision_counts: dict[str, int] = {}
        self._duplicate_identities = 0
        self._bindings = _IdentityBindings()
        self._replay = _ProfileReplay(selection=_IncrementalCanonicalSelection())
        self._incremental_window_valid = True
        self._state: dict | None = None
        self._last_event_key: tuple | None = None
        self._last_evaluated_at: int | None = None
        self.workload = {
            "incremental_adds": 0,
            "window_rebuilds": 0,
            "window_rebuild_input_rows": 0,
            "max_window_events": 0,
            "fast_path_events": 0,
            "algorithm_work_units": 0,
            "bounded_work_units": 0,
            "index_compactions": 0,
            "index_compaction_input_rows": 0,
            "retained_index_events": 0,
        }

    def advance(
        self,
        event: ObservationSignal,
        evaluated_at: int,
    ) -> dict:
        cutoff = _validated_evaluated_at(evaluated_at)
        prepared = _prepare_event(event, cutoff)
        if prepared is None or prepared[0] != self.profile_key:
            raise ValueError("event is not eligible for this adaptive profile window")
        event_key = _prepared_event_sort_key(prepared)
        if self._last_event_key is not None and event_key < self._last_event_key:
            raise ValueError("events must advance in causal settlement order")

        window_start = cutoff - self.lookback_ms
        if int(event.settled_at) < window_start:
            raise ValueError("event is outside the adaptive lookback window")
        self._expire_before(window_start)

        self._events.append(event)
        self._opened_monotonic = (
            self._index.append(event) and self._opened_monotonic
        )
        self.workload["retained_index_events"] = len(self._index.events)
        self._add_identity(event.observation_key, self._observation_counts)
        self._add_identity(event.decision_id, self._decision_counts)
        self._last_event_key = event_key
        self._last_evaluated_at = cutoff
        self.workload["incremental_adds"] += 1
        self.workload["bounded_work_units"] += (
            self.config.full_window_samples
            + len(self._index.events).bit_length()
            + 1
        )
        self.workload["max_window_events"] = max(
            self.workload["max_window_events"],
            len(self._events),
        )
        if self._incremental_window_valid:
            self._apply_incremental(prepared)
            self.workload["algorithm_work_units"] += (
                min(len(self._events), self.config.full_window_samples) + 1
            )
            return self._copy_state_at(cutoff)
        return self._evaluate_window(cutoff)

    def state_at(self, evaluated_at: int) -> dict:
        """Return the exact state after expiring rows outside the window."""
        cutoff = _validated_evaluated_at(evaluated_at)
        if self._last_evaluated_at is not None and cutoff < self._last_evaluated_at:
            raise ValueError("evaluated_at must advance monotonically")
        self._expire_before(cutoff - self.lookback_ms)
        self._last_evaluated_at = cutoff
        return self._evaluate_window(cutoff)

    def _expire_before(self, window_start: int) -> None:
        removed = False
        while self._events and int(self._events[0].settled_at) < window_start:
            expired = self._events.popleft()
            self._remove_identity(expired.observation_key, self._observation_counts)
            self._remove_identity(expired.decision_id, self._decision_counts)
            self._active_start += 1
            removed = True
        if removed:
            self._incremental_window_valid = False
            self._compact_index_if_geometric()

    def _compact_index_if_geometric(self) -> None:
        active_count = len(self._events)
        if self._active_start == 0:
            return
        if active_count and self._active_start < active_count:
            return
        compacted = _MonotonicWindowIndex()
        monotonic = True
        for event in self._events:
            monotonic = compacted.append(event) and monotonic
        self._index = compacted
        self._active_start = 0
        self._opened_monotonic = monotonic
        self.workload["index_compactions"] += 1
        self.workload["index_compaction_input_rows"] += active_count
        self.workload["algorithm_work_units"] += active_count
        self.workload["bounded_work_units"] += active_count
        self.workload["retained_index_events"] = active_count

    def _apply_incremental(
        self,
        prepared: tuple[str, ObservationSignal],
    ) -> None:
        key, event = prepared
        if not self._bindings.accept(key, event):
            return
        state = _advance_profile_replay(
            self._replay,
            key,
            event,
            self.config,
        )
        if state is not None:
            self._state = state

    def _add_identity(self, identity: str, counts: dict[str, int]) -> None:
        if not identity:
            return
        previous = counts.get(identity, 0)
        counts[identity] = previous + 1
        if previous == 1:
            self._duplicate_identities += 1

    def _remove_identity(self, identity: str, counts: dict[str, int]) -> None:
        if not identity:
            return
        previous = counts[identity]
        if previous == 2:
            self._duplicate_identities -= 1
        if previous == 1:
            del counts[identity]
        else:
            counts[identity] = previous - 1

    def _evaluate_window(self, evaluated_at: int) -> dict:
        if not self._events:
            self._state = evaluate_adaptive_profile_state(
                (),
                self.profile_key,
                evaluated_at,
                config=self.config,
            )
        elif self._opened_monotonic and self._duplicate_identities == 0:
            before_work = self._index.work_units
            tail, selected_count = self._index.selected_tail(
                self._active_start,
                len(self._index.events) - 1,
                self.config.full_window_samples + 2,
            )
            self._state = _state_from_monotonic_selected_tail(
                tail,
                selected_count,
                self.profile_key,
                evaluated_at,
                self.config,
            )
            self.workload["fast_path_events"] += 1
            self.workload["algorithm_work_units"] += (
                self._index.work_units - before_work
            )
        else:
            self._rebuild_window(evaluated_at)
        return self._copy_state_at(evaluated_at)

    def _copy_state_at(self, evaluated_at: int) -> dict:
        if self._state is None:
            self._state = evaluate_adaptive_profile_state(
                (),
                self.profile_key,
                evaluated_at,
                config=self.config,
            )
        state = dict(self._state)
        state["evaluated_at"] = evaluated_at
        self._state = state
        return deepcopy(state)

    def _rebuild_window(self, evaluated_at: int) -> None:
        self.workload["window_rebuilds"] += 1
        self.workload["window_rebuild_input_rows"] += len(self._events)
        self.workload["algorithm_work_units"] += len(self._events)
        rebuilt = rebuild_adaptive_profile_states(
            list(self._events),
            evaluated_at,
            config=self.config,
        )
        self._state = rebuilt.get(self.profile_key)
        if self._state is None:
            self._state = evaluate_adaptive_profile_state(
                (),
                self.profile_key,
                evaluated_at,
                config=self.config,
            )


@dataclass
class _GlobalWindowEntry:
    profile_key: str
    event: ObservationSignal
    accepted: bool


class AdaptiveGlobalProfileWindowReplay:
    """Replay a global identity-bound window across every adaptive profile."""

    _PROFILE_WORKLOAD_KEYS = (
        "window_rebuilds",
        "window_rebuild_input_rows",
        "fast_path_events",
        "algorithm_work_units",
        "bounded_work_units",
    )

    def __init__(
        self,
        *,
        lookback_ms: int,
        config: AdaptiveProfileStateConfig | None = None,
    ) -> None:
        if type(lookback_ms) is not int or lookback_ms <= 0:
            raise ValueError("lookback_ms must be a positive integer")
        self.lookback_ms = lookback_ms
        self.config = _resolve_config(config)
        self._events: deque[_GlobalWindowEntry] = deque()
        self._observation_claims: dict[str, int] = {}
        self._decision_claims: dict[str, int] = {}
        self._observation_bindings: dict[str, tuple[tuple[str, str], int]] = {}
        self._decision_bindings: dict[str, tuple[tuple[str, str], int]] = {}
        self._trackers: dict[str, AdaptiveProfileWindowReplay] = {}
        self._retired_profile_workload = {
            key: 0 for key in self._PROFILE_WORKLOAD_KEYS
        }
        self._max_profile_window_events = 0
        self._last_event_key: tuple | None = None
        self._last_evaluated_at: int | None = None
        self.workload = {
            "events": 0,
            "global_identity_rebuilds": 0,
            "global_identity_rebuild_input_rows": 0,
            "max_global_window_events": 0,
        }

    def advance(
        self,
        event: ObservationSignal,
        evaluated_at: int,
    ) -> dict:
        cutoff = _validated_evaluated_at(evaluated_at)
        prepared = _prepare_event(event, cutoff)
        if prepared is None:
            raise ValueError("event is not eligible for the adaptive global window")
        key, prepared_event = prepared
        event_key = _prepared_event_sort_key(prepared)
        if self._last_event_key is not None and event_key < self._last_event_key:
            raise ValueError("events must advance in causal settlement order")
        if self._last_evaluated_at is not None and cutoff < self._last_evaluated_at:
            raise ValueError("evaluated_at must advance monotonically")
        window_start = cutoff - self.lookback_ms
        if int(prepared_event.settled_at) < window_start:
            raise ValueError("event is outside the adaptive lookback window")

        if self._expire_before(window_start):
            self._rebuild_active_window()
        self._increment_claim(
            prepared_event.observation_key,
            self._observation_claims,
        )
        self._increment_claim(
            prepared_event.decision_id,
            self._decision_claims,
        )
        accepted = self._accept(key, prepared_event)
        self._events.append(_GlobalWindowEntry(key, prepared_event, accepted))
        self._last_event_key = event_key
        self._last_evaluated_at = cutoff
        self.workload["events"] += 1
        self.workload["max_global_window_events"] = max(
            self.workload["max_global_window_events"],
            len(self._events),
        )

        tracker = self._trackers.get(key)
        if accepted:
            if tracker is None:
                tracker = AdaptiveProfileWindowReplay(
                    key,
                    lookback_ms=self.lookback_ms,
                    config=self.config,
                )
                self._trackers[key] = tracker
            state = tracker.advance(prepared_event, cutoff)
        elif tracker is None:
            state = evaluate_adaptive_profile_state(
                (),
                key,
                cutoff,
                config=self.config,
            )
        else:
            state = tracker.state_at(cutoff)
        self._record_profile_window_maximum()
        return state

    def workload_report(self) -> dict[str, int]:
        totals = dict(self._retired_profile_workload)
        for tracker in self._trackers.values():
            for key in self._PROFILE_WORKLOAD_KEYS:
                totals[key] += int(tracker.workload[key])
        totals.update(self.workload)
        totals["incremental_adds"] = int(self.workload["events"])
        totals["max_window_events"] = self._max_profile_window_events
        return totals

    def _expire_before(self, window_start: int) -> bool:
        released_observations: set[str] = set()
        released_decisions: set[str] = set()
        while self._events and int(self._events[0].event.settled_at) < window_start:
            entry = self._events.popleft()
            event = entry.event
            self._decrement_claim(event.observation_key, self._observation_claims)
            self._decrement_claim(event.decision_id, self._decision_claims)
            if not entry.accepted:
                continue
            if self._release_binding(
                event.observation_key,
                self._observation_bindings,
            ):
                released_observations.add(event.observation_key)
            if self._release_binding(
                event.decision_id,
                self._decision_bindings,
            ):
                released_decisions.add(event.decision_id)
        return any(
            identity in self._observation_claims
            for identity in released_observations
        ) or any(
            identity in self._decision_claims
            for identity in released_decisions
        )

    def _accept(self, key: str, event: ObservationSignal) -> bool:
        observation_binding = (event.decision_id, key)
        decision_binding = (event.observation_key, key)
        if not self._binding_matches(
            event.observation_key,
            observation_binding,
            self._observation_bindings,
        ):
            return False
        if not self._binding_matches(
            event.decision_id,
            decision_binding,
            self._decision_bindings,
        ):
            return False
        self._retain_binding(
            event.observation_key,
            observation_binding,
            self._observation_bindings,
        )
        self._retain_binding(
            event.decision_id,
            decision_binding,
            self._decision_bindings,
        )
        return True

    @staticmethod
    def _binding_matches(
        identity: str,
        binding: tuple[str, str],
        bindings: dict[str, tuple[tuple[str, str], int]],
    ) -> bool:
        return (
            not identity
            or identity not in bindings
            or bindings[identity][0] == binding
        )

    @staticmethod
    def _retain_binding(
        identity: str,
        binding: tuple[str, str],
        bindings: dict[str, tuple[tuple[str, str], int]],
    ) -> None:
        if not identity:
            return
        current = bindings.get(identity)
        bindings[identity] = (binding, 1 if current is None else current[1] + 1)

    @staticmethod
    def _release_binding(
        identity: str,
        bindings: dict[str, tuple[tuple[str, str], int]],
    ) -> bool:
        if not identity:
            return False
        binding, count = bindings[identity]
        if count > 1:
            bindings[identity] = (binding, count - 1)
            return False
        del bindings[identity]
        return True

    @staticmethod
    def _increment_claim(identity: str, claims: dict[str, int]) -> None:
        if identity:
            claims[identity] = claims.get(identity, 0) + 1

    @staticmethod
    def _decrement_claim(identity: str, claims: dict[str, int]) -> None:
        if not identity:
            return
        count = claims[identity]
        if count == 1:
            del claims[identity]
        else:
            claims[identity] = count - 1

    def _rebuild_active_window(self) -> None:
        self.workload["global_identity_rebuilds"] += 1
        self.workload["global_identity_rebuild_input_rows"] += len(self._events)
        self._observation_bindings = {}
        self._decision_bindings = {}
        self._retire_trackers()
        for entry in self._events:
            entry.accepted = self._accept(entry.profile_key, entry.event)
            if not entry.accepted:
                continue
            tracker = self._trackers.get(entry.profile_key)
            if tracker is None:
                tracker = AdaptiveProfileWindowReplay(
                    entry.profile_key,
                    lookback_ms=self.lookback_ms,
                    config=self.config,
                )
                self._trackers[entry.profile_key] = tracker
            tracker.advance(entry.event, int(entry.event.settled_at) + 1)
        self._record_profile_window_maximum()

    def _retire_trackers(self) -> None:
        for tracker in self._trackers.values():
            for key in self._PROFILE_WORKLOAD_KEYS:
                self._retired_profile_workload[key] += int(tracker.workload[key])
        self._trackers = {}

    def _record_profile_window_maximum(self) -> None:
        current_maximum = max(
            (
                int(tracker.workload["max_window_events"])
                for tracker in self._trackers.values()
            ),
            default=0,
        )
        self._max_profile_window_events = max(
            self._max_profile_window_events,
            current_maximum,
        )


def _state_from_monotonic_selected_tail(
    tail: Sequence[ObservationSignal],
    selected_count: int,
    profile_key: str,
    evaluated_at: int,
    config: AdaptiveProfileStateConfig,
) -> dict:
    if not tail or selected_count <= 0:
        return evaluate_adaptive_profile_state(
            (),
            profile_key,
            evaluated_at,
            config=config,
        )

    if selected_count == len(tail):
        previous = None
        state = None
        samples = []
        for event in tail:
            samples.append(event)
            state = classify_profile_state(
                samples[-config.full_window_samples :],
                profile_key,
                evaluated_at,
                previous=previous,
                config=config,
            )
            previous = state["status"]
        return state

    if len(tail) != config.full_window_samples + 2:
        raise RuntimeError("adaptive monotonic tail does not cover final transitions")
    antepenultimate_index = len(tail) - 3
    antepenultimate = classify_profile_state(
        tail[
            antepenultimate_index - config.full_window_samples + 1
            : antepenultimate_index + 1
        ],
        profile_key,
        evaluated_at,
        config=config,
    )
    penultimate_index = len(tail) - 2
    penultimate = classify_profile_state(
        tail[
            penultimate_index - config.full_window_samples + 1
            : penultimate_index + 1
        ],
        profile_key,
        evaluated_at,
        previous=antepenultimate["status"],
        config=config,
    )
    return classify_profile_state(
        tail[-config.full_window_samples :],
        profile_key,
        evaluated_at,
        previous=penultimate["status"],
        config=config,
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


def _advance_profile_replay(
    replay: _ProfileReplay,
    key: str,
    event: ObservationSignal,
    config: AdaptiveProfileStateConfig,
) -> dict | None:
    update = replay.selection.add(event)
    if not update.changed:
        return None
    samples = replay.selection.latest_selected_events(config.full_window_samples)
    state = classify_profile_state(
        samples,
        key,
        int(event.settled_at) + 1,
        previous=replay.previous_status,
        config=config,
    )
    replay.previous_status = state["status"]
    return state


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
                selection=_IncrementalCanonicalSelection(),
            )
            profiles[key] = replay

        state = _advance_profile_replay(replay, key, event, resolved)
        if state is not None:
            states[key] = state

    for state in states.values():
        state["evaluated_at"] = cutoff
    return states


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


def adaptive_replay_event_sort_key(item: ObservationSignal) -> tuple:
    """Return the production causal order for global adaptive replay."""
    if not isinstance(item, ObservationSignal):
        raise TypeError("adaptive replay events must be ObservationSignal values")
    key = _safe_observation_profile_key(item) or ""
    causal_key = _causal_observation_sort_key(item)
    return (
        causal_key[0],
        key,
        *causal_key[1:],
    )


def _prepared_event_sort_key(prepared: tuple[str, ObservationSignal]) -> tuple:
    _key, item = prepared
    return adaptive_replay_event_sort_key(item)
