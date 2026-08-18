from __future__ import annotations

import math
import random
import time
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from unittest.mock import patch

import app.adaptive_profile_state as adaptive_module
from app.adaptive_profile_state import (
    ADAPTIVE_PROFILE_STATE_VERSION,
    AdaptiveProfileStateConfig,
    classify_profile_state,
    evaluate_adaptive_profile_state,
    independent_settled_samples,
    rebuild_adaptive_profile_states,
)
from app.daily_profile_selector import _independent_samples, profile_key
from app.models import ObservationSignal
from app.segments import threshold_segment


MINUTE_MS = 60_000
CUTOFF = 1_800_000_000_000
PROFILE_KEY = profile_key(
    10,
    "short_observe",
    "generic_short_observe",
    "SHORT",
    "WD-02",
)


def observation(
    key: str,
    result: str | None,
    opened_at: int,
    *,
    expires_at: int | None = None,
    settled_at: int | None = None,
    pnl: float | None = None,
    status: str = "SETTLED",
    family: str = "short_observe",
    tag: str = "generic_short_observe",
    direction: str = "SHORT",
    timeframe: int = 10,
    segment: str = "WD-02",
    decision_id: str = "",
) -> ObservationSignal:
    resolved_expires = opened_at + 10 * MINUTE_MS if expires_at is None else expires_at
    resolved_settled = resolved_expires if settled_at is None else settled_at
    resolved_pnl = (8.0 if result == "WIN" else -10.0) if pnl is None else pnl
    return ObservationSignal(
        observation_key=key,
        strategy_family=family,
        strategy_tag=tag,
        direction=direction,
        timeframe_minutes=timeframe,
        level="OBSERVE",
        reason="adaptive profile state fixture",
        entry_price=100.0,
        opened_at=opened_at,
        expires_at=resolved_expires,
        threshold_segment=segment,
        status=status,
        result=result,
        exit_price=101.0,
        settled_at=resolved_settled,
        pnl=resolved_pnl,
        decision_id=decision_id,
    )


def adaptive_rows(
    sample_size: int,
    n12_wins: int,
    *,
    n20_ev: float = 0.0,
    start: int = CUTOFF - 30 * 20 * MINUTE_MS,
) -> list[ObservationSignal]:
    tail_size = min(12, sample_size)
    if not 0 <= n12_wins <= tail_size:
        raise ValueError("n12_wins must fit the available N12 tail")
    results = ["LOSS"] * (sample_size - tail_size)
    results.extend(["WIN"] * n12_wins)
    results.extend(["LOSS"] * (tail_size - n12_wins))
    rows = [
        observation(
            f"row-{index:02d}",
            result,
            start + index * 20 * MINUTE_MS,
            decision_id=f"decision-{index:02d}",
        )
        for index, result in enumerate(results)
    ]
    if sample_size == 20:
        target_pnl = n20_ev * sample_size
        rows[-1].pnl += target_pnl - math.fsum(item.pnl for item in rows)
    return rows


def production_rows(sample_size: int) -> list[ObservationSignal]:
    start = CUTOFF - (sample_size + 2) * 20 * MINUTE_MS
    return [
        observation(
            f"production-{index:05d}",
            "WIN" if index % 2 == 0 else "LOSS",
            start + index * 20 * MINUTE_MS,
            decision_id=f"production-decision-{index:05d}",
        )
        for index in range(sample_size)
    ]


def delayed_production_rows(sample_size: int) -> list[ObservationSignal]:
    settlement_start = CUTOFF - sample_size - 1
    opened_start = settlement_start - (sample_size + 2) * 20 * MINUTE_MS
    rows = production_rows(sample_size)
    for index, item in enumerate(rows):
        item.opened_at = opened_start + index * 20 * MINUTE_MS
        item.expires_at = item.opened_at + 10 * MINUTE_MS
        item.settled_at = settlement_start + (sample_size - 1 - index)
    return rows


def all_overlap_rows(sample_size: int) -> list[ObservationSignal]:
    opened_at = CUTOFF - 40 * MINUTE_MS
    expires_at = opened_at + 10 * MINUTE_MS
    return [
        observation(
            f"all-overlap-{index:05d}",
            "WIN" if index % 2 == 0 else "LOSS",
            opened_at,
            expires_at=expires_at,
            settled_at=expires_at + index,
            decision_id=f"all-overlap-decision-{index:05d}",
        )
        for index in range(sample_size)
    ]


def duplicate_replacement_rows(sample_size: int) -> list[ObservationSignal]:
    settlement_start = CUTOFF - sample_size - 1
    opened_start = settlement_start - (sample_size + 2) * 20 * MINUTE_MS
    return [
        observation(
            "duplicate-heavy-observation",
            "WIN" if index % 2 == 0 else "LOSS",
            opened_start + index * 20 * MINUTE_MS,
            settled_at=settlement_start + (sample_size - 1 - index),
            decision_id="duplicate-heavy-decision",
        )
        for index in range(sample_size)
    ]


def same_observation_conflict_rows(sample_size: int) -> list[ObservationSignal]:
    settlement_start = CUTOFF - sample_size - 1
    opened_start = settlement_start - (sample_size + 2) * 20 * MINUTE_MS
    return [
        observation(
            "bound-observation",
            "WIN" if index % 2 == 0 else "LOSS",
            opened_start + (sample_size - 1 - index) * 20 * MINUTE_MS,
            settled_at=settlement_start + index,
            decision_id=f"conflicting-decision-{index:05d}",
        )
        for index in range(sample_size)
    ]


def cross_linked_conflict_rows(sample_size: int) -> list[ObservationSignal]:
    binding_count = sample_size // 2
    settlement_start = CUTOFF - sample_size - 1
    opened_start = settlement_start - (sample_size + 2) * 20 * MINUTE_MS
    rows = [
        observation(
            f"bound-observation-{index:05d}",
            "WIN" if index % 2 == 0 else "LOSS",
            opened_start + (binding_count + index) * 20 * MINUTE_MS,
            settled_at=settlement_start + index,
            decision_id=f"bound-decision-{index:05d}",
        )
        for index in range(binding_count)
    ]
    rows.extend(
        observation(
            f"bound-observation-{index:05d}",
            "WIN",
            opened_start + index * 20 * MINUTE_MS,
            settled_at=settlement_start + binding_count + index,
            decision_id=f"bound-decision-{(index + 1) % binding_count:05d}",
        )
        for index in range(sample_size - binding_count)
    )
    return rows


def nested_overlap_rows(sample_size: int) -> list[ObservationSignal]:
    settlement_start = CUTOFF - sample_size - 1
    opened_start = settlement_start - 10 * MINUTE_MS - sample_size
    return [
        observation(
            f"nested-overlap-{index:05d}",
            "WIN" if index % 2 == 0 else "LOSS",
            opened_start + (sample_size - 1 - index),
            settled_at=settlement_start + index,
            decision_id=f"nested-overlap-decision-{index:05d}",
        )
        for index in range(sample_size)
    ]


def state_trace_item(key: str, result: dict) -> tuple:
    return (
        key,
        result["status"],
        result["previous"],
        result["transition"],
        result["n12"],
        result["n20"],
    )


def progressive_reference_rebuild(
    rows: list[ObservationSignal],
    evaluated_at: int,
) -> tuple[dict[str, dict], list[tuple]]:
    events = sorted(
        rows,
        key=lambda item: (
            item.settled_at,
            profile_key(
                item.timeframe_minutes,
                item.strategy_family,
                item.strategy_tag,
                item.direction,
                item.threshold_segment,
            ),
            item.opened_at,
            item.observation_key,
            item.decision_id,
            item.expires_at,
            item.result,
            float(item.pnl).hex(),
        ),
    )
    prefix: list[ObservationSignal] = []
    signatures: dict[str, tuple] = {}
    states: dict[str, dict] = {}
    trace = []
    for event in events:
        key = profile_key(
            event.timeframe_minutes,
            event.strategy_family,
            event.strategy_tag,
            event.direction,
            event.threshold_segment,
        )
        prefix.append(event)
        event_cutoff = event.settled_at + 1
        samples = independent_settled_samples(prefix, key, event_cutoff)
        signature = tuple(
            (
                item.observation_key,
                item.decision_id,
                item.opened_at,
                item.expires_at,
                item.settled_at,
                item.result,
                item.pnl,
            )
            for item in samples
        )
        if signature == signatures.get(key, ()):
            continue
        signatures[key] = signature
        result = classify_profile_state(
            samples,
            key,
            event_cutoff,
            previous=states.get(key, {}).get("status"),
        )
        states[key] = result
        trace.append(state_trace_item(key, result))
    for state in states.values():
        state["evaluated_at"] = evaluated_at
    return states, trace


class AdaptiveProfileStateTest(unittest.TestCase):
    def test_config_is_frozen_and_strictly_validated(self):
        config = AdaptiveProfileStateConfig()
        self.assertEqual(
            (
                config.warmup_samples,
                config.active_n12_wins,
                config.paused_n12_max_wins,
                config.full_window_samples,
            ),
            (12, 7, 5, 20),
        )
        with self.assertRaises(FrozenInstanceError):
            config.warmup_samples = 10

        invalid = [
            {"warmup_samples": True},
            {"warmup_samples": 0},
            {"active_n12_wins": 13},
            {"paused_n12_max_wins": -1},
            {"paused_n12_max_wins": 7},
            {"full_window_samples": 11},
            {"full_window_samples": 12},
        ]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises((TypeError, ValueError)):
                AdaptiveProfileStateConfig(**values)

    def test_adaptive_state_matrix_and_output_contract(self):
        cases = [
            (11, 6, 0.0, None, "WARMUP"),
            (12, 7, 0.0, None, "ACTIVE"),
            (20, 7, 0.1, None, "ACTIVE"),
            (20, 6, -0.1, None, "WATCH"),
            (20, 5, -0.1, None, "PAUSED"),
            (20, 6, -0.1, "PAUSED", "WATCH"),
            (20, 5, 0.0, "PAUSED", "WATCH"),
            (20, 7, 0.0, "PAUSED", "WATCH"),
            (20, 7, 0.0, "WATCH", "ACTIVE"),
        ]
        for samples, wins, ev, previous, expected in cases:
            with self.subTest(expected=expected, previous=previous, wins=wins, ev=ev):
                result = evaluate_adaptive_profile_state(
                    adaptive_rows(samples, wins, n20_ev=ev),
                    PROFILE_KEY,
                    CUTOFF,
                    previous,
                )
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["version"], ADAPTIVE_PROFILE_STATE_VERSION)
                self.assertEqual(result["evaluated_at"], CUTOFF)
                self.assertEqual(result["profile_key"], PROFILE_KEY)
                self.assertEqual(result["previous"], previous)
                self.assertEqual(result["transition"], f"{previous or 'NONE'}->{expected}")
                self.assertTrue(result["reason"])
                self.assertEqual(result["n12"]["sample_size"], min(12, samples))
                self.assertEqual(result["n20"]["sample_size"], min(20, samples))

    def test_public_evaluator_accepts_planned_profile_key_keyword(self):
        result = evaluate_adaptive_profile_state(
            [],
            profile_key=PROFILE_KEY,
            evaluated_at=CUTOFF,
        )

        self.assertEqual(result["profile_key"], PROFILE_KEY)

    def test_n12_five_six_seven_and_raw_n20_ev_boundaries(self):
        cases = [
            (5, -1e-12, "PAUSED"),
            (6, -1e-12, "WATCH"),
            (7, -1e-12, "WATCH"),
            (5, 0.0, "WATCH"),
            (6, 0.0, "WATCH"),
            (7, 0.0, "ACTIVE"),
        ]
        for wins, ev, expected in cases:
            with self.subTest(wins=wins, ev=ev):
                result = evaluate_adaptive_profile_state(
                    adaptive_rows(20, wins, n20_ev=ev),
                    PROFILE_KEY,
                    CUTOFF,
                )
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["n20"]["ev"], 0.0)

    def test_state_excludes_overlap_future_open_invalid_duplicates_and_other_keys(self):
        start = CUTOFF - 8 * 60 * MINUTE_MS
        valid = observation("valid", "WIN", start, decision_id="decision-valid")
        rows = [
            valid,
            observation("overlap", "WIN", start + 5 * MINUTE_MS),
            observation("valid", "WIN", start + 20 * MINUTE_MS),
            observation("duplicate-decision", "WIN", start + 40 * MINUTE_MS, decision_id="decision-valid"),
            observation("other-key", "WIN", start + 60 * MINUTE_MS, tag="other"),
            observation("at-cutoff", "WIN", start + 80 * MINUTE_MS, settled_at=CUTOFF),
            observation("future", "WIN", start + 100 * MINUTE_MS, settled_at=CUTOFF + 1),
            observation("open", None, start + 120 * MINUTE_MS, status="OPEN"),
            observation("invalid-result", "VOID", start + 140 * MINUTE_MS),
            observation("invalid-pnl", "WIN", start + 160 * MINUTE_MS, pnl=float("nan")),
        ]

        result = evaluate_adaptive_profile_state(rows, PROFILE_KEY, CUTOFF)

        self.assertEqual(result["n12"]["sample_size"], 1)
        self.assertEqual(result["n12"]["wins"], 1)
        self.assertEqual(
            [item.observation_key for item in independent_settled_samples(rows, PROFILE_KEY, CUTOFF)],
            ["valid"],
        )

    def test_overlap_selection_reuses_daily_selector_interval_rule(self):
        start = CUTOFF - 4 * 60 * MINUTE_MS
        rows = [
            observation("first", "WIN", start),
            observation("overlap", "LOSS", start + 5 * MINUTE_MS),
            observation("boundary", "WIN", start + 10 * MINUTE_MS),
        ]

        adaptive = independent_settled_samples(rows, PROFILE_KEY, CUTOFF)
        daily = _independent_samples(rows, 0, CUTOFF)

        self.assertEqual(
            [item.observation_key for item in adaptive],
            [item.observation_key for item in daily],
        )
        self.assertEqual([item.observation_key for item in adaptive], ["first", "boundary"])

    def test_profile_key_is_strict_complete_and_recomputed_from_observation_fields(self):
        valid_keys = [
            PROFILE_KEY,
            profile_key(10, "family", "tag", "LONG", "WE-23"),
        ]
        for key in valid_keys:
            evaluate_adaptive_profile_state([], key, CUTOFF)

        invalid_keys = [
            "short_observe|SHORT|WD-02",
            "5|family|tag|LONG|WD-02",
            "10|family|tag|SIDEWAYS|WD-02",
            "10|family|tag|LONG|GLOBAL",
            "10|family|tag|long|wd-02",
            "10|family|tag|LONG|WE-24",
        ]
        for key in invalid_keys:
            with self.subTest(key=key), self.assertRaises(ValueError):
                evaluate_adaptive_profile_state([], key, CUTOFF)

        row = observation("stale-profile-field", "WIN", CUTOFF - 20 * MINUTE_MS)
        row.profile_key = profile_key(10, "other", "other", "LONG", "WE-23")
        self.assertEqual(
            evaluate_adaptive_profile_state([row], PROFILE_KEY, CUTOFF)["n12"]["sample_size"],
            1,
        )

    def test_previous_accepts_legacy_four_state_string_and_valid_structured_state(self):
        rows = adaptive_rows(20, 7, n20_ev=0.0)
        legacy = evaluate_adaptive_profile_state(rows, PROFILE_KEY, CUTOFF, "PAUSED")
        structured = evaluate_adaptive_profile_state(
            rows,
            PROFILE_KEY,
            CUTOFF,
            {
                "version": ADAPTIVE_PROFILE_STATE_VERSION,
                "status": "PAUSED",
                "profile_key": PROFILE_KEY,
                "evaluated_at": CUTOFF - 1,
            },
        )

        self.assertEqual(legacy["status"], "WATCH")
        self.assertEqual(legacy["previous"], "PAUSED")
        self.assertEqual(legacy["transition"], "PAUSED->WATCH")
        self.assertEqual(structured["status"], "WATCH")
        self.assertEqual(structured["previous"], "PAUSED")
        self.assertEqual(structured["previous_ignored_reason"], "")

    def test_structured_previous_future_equal_other_key_invalid_and_unknown_are_ignored(self):
        rows = adaptive_rows(20, 7, n20_ev=0.0)
        valid = {
            "version": ADAPTIVE_PROFILE_STATE_VERSION,
            "status": "PAUSED",
            "profile_key": PROFILE_KEY,
            "evaluated_at": CUTOFF - 1,
        }
        cases = [
            ({**valid, "evaluated_at": CUTOFF + 1}, "PREVIOUS_EVALUATED_AT_NOT_PAST"),
            ({**valid, "evaluated_at": CUTOFF}, "PREVIOUS_EVALUATED_AT_NOT_PAST"),
            ({**valid, "evaluated_at": "bad"}, "PREVIOUS_EVALUATED_AT_INVALID"),
            ({**valid, "evaluated_at": True}, "PREVIOUS_EVALUATED_AT_INVALID"),
            ({**valid, "profile_key": profile_key(10, "other", "tag", "LONG", "WE-23")}, "PREVIOUS_PROFILE_KEY_MISMATCH"),
            ({**valid, "version": "ADAPTIVE_PROFILE_STATE_V0"}, "PREVIOUS_VERSION_INCOMPATIBLE"),
            ({**valid, "status": "DEGRADED"}, "PREVIOUS_STATUS_INVALID"),
            ({**valid, "status": "active"}, "PREVIOUS_STATUS_INVALID"),
            ("DEGRADED", "PREVIOUS_STATUS_INVALID"),
            ("OLD_UNKNOWN", "PREVIOUS_STATUS_INVALID"),
        ]
        for previous, ignored_reason in cases:
            with self.subTest(previous=previous):
                result = evaluate_adaptive_profile_state(
                    rows,
                    PROFILE_KEY,
                    CUTOFF,
                    previous,
                )
                self.assertEqual(result["status"], "ACTIVE")
                self.assertIsNone(result["previous"])
                self.assertEqual(result["previous_ignored_reason"], ignored_reason)
                self.assertIn(ignored_reason, result["reason"])

    def test_malformed_profiles_and_time_invariant_violations_are_skipped_before_identity(self):
        start = CUTOFF - 10 * 60 * MINUTE_MS
        valid = observation(
            "shared-key",
            "WIN",
            start + 8 * MINUTE_MS,
            decision_id="shared-decision",
        )
        malformed_profile_rows = []
        for index, (field, value) in enumerate(
            [
                ("timeframe_minutes", None),
                ("timeframe_minutes", "10"),
                ("timeframe_minutes", True),
                ("strategy_family", None),
                ("strategy_family", ""),
                ("strategy_family", "bad|family"),
                ("strategy_tag", None),
                ("strategy_tag", ""),
                ("strategy_tag", "bad|tag"),
                ("direction", None),
                ("direction", "SIDEWAYS"),
                ("threshold_segment", None),
                ("threshold_segment", "WE-24"),
            ]
        ):
            item = observation(
                f"malformed-profile-{index}",
                "LOSS",
                start - (index + 1) * 20 * MINUTE_MS,
            )
            setattr(item, field, value)
            malformed_profile_rows.append(item)

        invalid_shared = observation(
            "shared-key",
            "LOSS",
            start,
            expires_at=start + 10 * MINUTE_MS,
            settled_at=start + 5 * MINUTE_MS,
            decision_id="shared-decision",
        )
        invalid_times = [
            invalid_shared,
            observation("future-expiry", "LOSS", start, expires_at=CUTOFF + 1, settled_at=CUTOFF - 1),
            observation("settled-before-open", "LOSS", start, settled_at=start - 1),
            observation("settled-before-expiry", "LOSS", start, settled_at=start + 5 * MINUTE_MS),
            observation("equal-cutoff", "LOSS", start, settled_at=CUTOFF),
        ]
        non_integer_time = observation("bad-time", "LOSS", start)
        non_integer_time.opened_at = float("nan")
        bool_time = observation("bool-time", "LOSS", start)
        bool_time.expires_at = True
        rows = malformed_profile_rows + invalid_times + [non_integer_time, bool_time, valid]
        original = repr(rows)

        samples = independent_settled_samples(rows, PROFILE_KEY, CUTOFF)
        rebuilt = rebuild_adaptive_profile_states(rows, CUTOFF)

        self.assertEqual([item.observation_key for item in samples], ["shared-key"])
        self.assertEqual(samples[0].result, "WIN")
        self.assertEqual(set(rebuilt), {PROFILE_KEY})
        self.assertEqual(rebuilt[PROFILE_KEY]["n12"]["sample_size"], 1)
        self.assertEqual(rebuilt[PROFILE_KEY]["n12"]["wins"], 1)
        self.assertEqual(repr(rows), original)

    def test_rebuild_filters_once_and_never_rescans_complete_prefixes(self):
        scan_counts = []
        for sample_size in (50, 100, 200):
            rows = production_rows(sample_size)
            with patch.object(
                adaptive_module,
                "_eligible_observation",
                wraps=adaptive_module._eligible_observation,
            ) as eligibility:
                with patch.object(
                    adaptive_module,
                    "independent_settled_samples",
                    side_effect=AssertionError("rebuild must not rescan prefixes"),
                ):
                    rebuilt = rebuild_adaptive_profile_states(rows, CUTOFF)
            scan_counts.append(eligibility.call_count)
            self.assertEqual(rebuilt[PROFILE_KEY]["n20"]["sample_size"], 20)

        self.assertEqual(scan_counts, [50, 100, 200])

    def test_rebuild_matches_quadratic_reference_after_every_independent_event(self):
        rows = production_rows(32)
        other_key_rows = [
            observation(
                f"other-{index:02d}",
                "WIN" if index % 3 else "LOSS",
                rows[index * 3].opened_at,
                family="other-family",
                tag="other-tag",
                direction="LONG",
                segment="WE-23",
                decision_id=f"other-decision-{index:02d}",
            )
            for index in range(10)
        ]
        overlap = observation(
            "overlap-reference",
            "WIN",
            rows[4].opened_at + 5 * MINUTE_MS,
            decision_id="overlap-reference-decision",
        )
        duplicate = observation(
            rows[8].observation_key,
            "WIN",
            rows[20].opened_at,
            decision_id=rows[8].decision_id,
        )
        mixed = list(reversed(rows + other_key_rows + [overlap, duplicate]))
        expected, expected_trace = progressive_reference_rebuild(mixed, CUTOFF)
        actual_trace = []
        real_classify = adaptive_module.classify_profile_state

        def recording_classify(*args, **kwargs):
            result = real_classify(*args, **kwargs)
            actual_trace.append(state_trace_item(args[1], result))
            return result

        with patch.object(
            adaptive_module,
            "classify_profile_state",
            side_effect=recording_classify,
        ):
            actual = rebuild_adaptive_profile_states(mixed, CUTOFF)

        self.assertEqual(actual_trace, expected_trace)
        self.assertEqual(actual, expected)

    def test_rebuild_includes_delayed_settlement_and_matches_public_state(self):
        start = CUTOFF - 60 * 20 * MINUTE_MS
        results = ["LOSS"] * 9 + ["WIN"] * 5 + ["LOSS"] * 6
        rows = [
            observation(
                f"causal-{index:02d}",
                result,
                start + index * 20 * MINUTE_MS,
                decision_id=f"causal-decision-{index:02d}",
            )
            for index, result in enumerate(results)
        ]
        delayed = observation(
            "delayed-win",
            "WIN",
            start - 20 * MINUTE_MS,
            settled_at=rows[-1].settled_at + MINUTE_MS,
            decision_id="delayed-win-decision",
        )
        mixed = list(reversed(rows + [delayed]))

        before_delayed = rebuild_adaptive_profile_states(mixed, delayed.settled_at)
        rebuilt = rebuild_adaptive_profile_states(mixed, CUTOFF)
        public = evaluate_adaptive_profile_state(
            mixed,
            PROFILE_KEY,
            CUTOFF,
            previous="PAUSED",
        )

        self.assertEqual(before_delayed[PROFILE_KEY]["status"], "PAUSED")
        self.assertEqual(rebuilt[PROFILE_KEY]["status"], "WATCH")
        self.assertEqual(rebuilt[PROFILE_KEY]["previous"], "PAUSED")
        self.assertEqual(rebuilt[PROFILE_KEY]["transition"], "PAUSED->WATCH")
        self.assertEqual(rebuilt[PROFILE_KEY]["n12"], public["n12"])
        self.assertEqual(rebuilt[PROFILE_KEY]["n20"], public["n20"])

    def test_rebuild_delayed_duplicate_at_n20_boundary_matches_reference(self):
        start = CUTOFF - 60 * 20 * MINUTE_MS
        results = ["LOSS"] * 8 + ["WIN"] * 6 + ["LOSS"] * 5 + ["WIN"]
        rows = [
            observation(
                f"boundary-{index:02d}",
                result,
                start + index * 20 * MINUTE_MS,
                pnl=0.0,
                decision_id=f"boundary-decision-{index:02d}",
            )
            for index, result in enumerate(results)
        ]
        delayed = observation(
            rows[-1].observation_key,
            "LOSS",
            start - 20 * MINUTE_MS,
            settled_at=rows[-1].settled_at + MINUTE_MS,
            pnl=0.0,
            decision_id=rows[-1].decision_id,
        )
        mixed = list(reversed(rows + [delayed]))

        expected, _ = progressive_reference_rebuild(mixed, CUTOFF)
        actual = rebuild_adaptive_profile_states(mixed, CUTOFF)

        self.assertEqual(expected[PROFILE_KEY]["status"], "WATCH")
        self.assertEqual(expected[PROFILE_KEY]["n12"]["wins"], 6)
        self.assertEqual(actual, expected)

    def test_rebuild_same_time_overlap_uses_public_observation_key_tie_break(self):
        opened_at = CUTOFF - 40 * MINUTE_MS
        settled_at = opened_at + 10 * MINUTE_MS
        rows = [
            observation(
                "z-short-win",
                "WIN",
                opened_at,
                expires_at=opened_at + 5 * MINUTE_MS,
                settled_at=settled_at,
                decision_id="z-short-decision",
            ),
            observation(
                "a-long-loss",
                "LOSS",
                opened_at,
                expires_at=opened_at + 10 * MINUTE_MS,
                settled_at=settled_at,
                decision_id="a-long-decision",
            ),
        ]

        expected, _ = progressive_reference_rebuild(rows, CUTOFF)
        actual = rebuild_adaptive_profile_states(list(reversed(rows)), CUTOFF)
        public = independent_settled_samples(rows, PROFILE_KEY, CUTOFF)

        self.assertEqual([item.observation_key for item in public], ["a-long-loss"])
        self.assertEqual(actual, expected)
        self.assertEqual(actual[PROFILE_KEY]["n12"]["wins"], 0)

    def test_rebuild_random_legal_delayed_duplicate_overlap_matches_reference(self):
        for seed in range(20):
            rng = random.Random(seed)
            start = CUTOFF - 3_000 * MINUTE_MS
            rows = []
            for index in range(28):
                opened_at = start + index * 20 * MINUTE_MS
                expires_at = opened_at + 10 * MINUTE_MS
                rows.append(
                    observation(
                        f"random-{seed:02d}-{index:02d}",
                        rng.choice(("WIN", "LOSS")),
                        opened_at,
                        settled_at=max(
                            expires_at,
                            CUTOFF - rng.randint(1, 300) * MINUTE_MS,
                        ),
                        decision_id=f"random-decision-{seed:02d}-{index:02d}",
                    )
                )
            for duplicate_index in range(5):
                target = rows[rng.randrange(4, len(rows) - 4)]
                opened_at = target.opened_at - rng.randint(1, 9) * MINUTE_MS
                rows.append(
                    observation(
                        target.observation_key,
                        rng.choice(("WIN", "LOSS")),
                        opened_at,
                        settled_at=CUTOFF - rng.randint(1, 120) * MINUTE_MS,
                        decision_id=target.decision_id,
                    )
                )
            for overlap_index in range(5):
                target = rows[rng.randrange(2, 26)]
                opened_at = target.opened_at + rng.randint(1, 9) * MINUTE_MS
                rows.append(
                    observation(
                        f"overlap-{seed:02d}-{overlap_index:02d}",
                        rng.choice(("WIN", "LOSS")),
                        opened_at,
                        settled_at=CUTOFF - rng.randint(1, 120) * MINUTE_MS,
                        decision_id=f"overlap-decision-{seed:02d}-{overlap_index:02d}",
                    )
                )
            rng.shuffle(rows)

            expected, expected_trace = progressive_reference_rebuild(rows, CUTOFF)
            actual_trace = []
            real_classify = adaptive_module.classify_profile_state

            def recording_classify(*args, **kwargs):
                result = real_classify(*args, **kwargs)
                actual_trace.append(state_trace_item(args[1], result))
                return result

            with patch.object(
                adaptive_module,
                "classify_profile_state",
                side_effect=recording_classify,
            ):
                actual = rebuild_adaptive_profile_states(rows, CUTOFF)

            with self.subTest(seed=seed):
                self.assertEqual(actual_trace, expected_trace)
                self.assertEqual(actual, expected)

    def test_cross_linked_identity_conflicts_are_rejected_without_rebinding(self):
        start = CUTOFF - 100 * MINUTE_MS
        initially_selected = observation(
            "shared-observation",
            "LOSS",
            start + 20 * MINUTE_MS,
            settled_at=start + 40 * MINUTE_MS,
            decision_id="shared-decision",
        )
        released_successor = observation(
            "released-observation",
            "WIN",
            start + 40 * MINUTE_MS,
            settled_at=start + 60 * MINUTE_MS,
            decision_id="shared-decision",
        )
        conflicting_observation = observation(
            "shared-observation",
            "WIN",
            start,
            settled_at=start + 80 * MINUTE_MS,
            decision_id="first-decision",
        )
        exact_pair = observation(
            "shared-observation",
            "WIN",
            start - 20 * MINUTE_MS,
            settled_at=start + 90 * MINUTE_MS,
            decision_id="shared-decision",
        )
        rows = [
            released_successor,
            exact_pair,
            conflicting_observation,
            initially_selected,
        ]
        expected, expected_trace = progressive_reference_rebuild(rows, CUTOFF)
        public = independent_settled_samples(rows, PROFILE_KEY, CUTOFF)
        actual_trace = []
        real_classify = adaptive_module.classify_profile_state

        def recording_classify(*args, **kwargs):
            result = real_classify(*args, **kwargs)
            actual_trace.append(state_trace_item(args[1], result))
            return result

        with patch.object(
            adaptive_module,
            "classify_profile_state",
            side_effect=recording_classify,
        ):
            actual = rebuild_adaptive_profile_states(rows, CUTOFF)

        self.assertEqual([item.decision_id for item in public], ["shared-decision"])
        self.assertEqual(public[0].result, "WIN")
        self.assertEqual(len(expected_trace), 2)
        self.assertEqual(expected[PROFILE_KEY]["n12"]["sample_size"], 1)
        self.assertEqual(expected[PROFILE_KEY]["n12"]["wins"], 1)
        self.assertEqual(actual_trace, expected_trace)
        self.assertEqual(actual, expected)

    def test_partial_identity_conflict_does_not_release_old_decision(self):
        start = CUTOFF - 100 * MINUTE_MS
        delayed = observation(
            "shared-observation",
            "WIN",
            start,
            settled_at=start + 80 * MINUTE_MS,
            decision_id="",
        )
        old_owner = observation(
            "shared-observation",
            "LOSS",
            start + 20 * MINUTE_MS,
            settled_at=start + 40 * MINUTE_MS,
            decision_id="released-decision",
        )
        released_successor = observation(
            "released-observation",
            "WIN",
            start + 40 * MINUTE_MS,
            settled_at=start + 60 * MINUTE_MS,
            decision_id="released-decision",
        )
        rows = [released_successor, delayed, old_owner]
        expected, expected_trace = progressive_reference_rebuild(rows, CUTOFF)
        actual_trace = []
        real_classify = adaptive_module.classify_profile_state

        def recording_classify(*args, **kwargs):
            result = real_classify(*args, **kwargs)
            actual_trace.append(state_trace_item(args[1], result))
            return result

        with patch.object(
            adaptive_module,
            "classify_profile_state",
            side_effect=recording_classify,
        ):
            actual = rebuild_adaptive_profile_states(rows, CUTOFF)

        self.assertEqual(expected[PROFILE_KEY]["n12"]["sample_size"], 1)
        self.assertEqual(expected[PROFILE_KEY]["n12"]["wins"], 0)
        self.assertEqual(actual_trace, expected_trace)
        self.assertEqual(actual, expected)

    def test_same_time_binding_conflict_uses_deterministic_causal_tie_break(self):
        settled_at = CUTOFF - 1
        earlier = observation(
            "same-binding-observation",
            "LOSS",
            CUTOFF - 40 * MINUTE_MS,
            settled_at=settled_at,
            decision_id="z-earlier-decision",
        )
        later = observation(
            "same-binding-observation",
            "WIN",
            CUTOFF - 20 * MINUTE_MS,
            settled_at=settled_at,
            decision_id="a-later-decision",
        )

        first = rebuild_adaptive_profile_states([later, earlier], CUTOFF)
        second = rebuild_adaptive_profile_states([earlier, later], CUTOFF)
        public = independent_settled_samples([later, earlier], PROFILE_KEY, CUTOFF)

        self.assertEqual([item.decision_id for item in public], ["z-earlier-decision"])
        self.assertEqual(first, second)
        self.assertEqual(first[PROFILE_KEY]["n12"]["wins"], 0)

    def test_global_binding_rejects_cross_profile_conflicts_and_profile_drift(self):
        start = CUTOFF - 200 * MINUTE_MS
        other_values = {
            "family": "aaa_family",
            "tag": "aaa_tag",
            "direction": "LONG",
            "segment": "WE-03",
        }
        other_key = profile_key(10, "aaa_family", "aaa_tag", "LONG", "WE-03")
        rows = [
            observation(
                "shared-global-observation",
                "LOSS",
                start,
                settled_at=start + 20 * MINUTE_MS,
                decision_id="global-owner-decision",
                **other_values,
            ),
            observation(
                "shared-global-observation",
                "WIN",
                start + 20 * MINUTE_MS,
                settled_at=start + 40 * MINUTE_MS,
                decision_id="conflicting-target-decision",
            ),
            observation(
                "drifting-global-observation",
                "LOSS",
                start + 40 * MINUTE_MS,
                settled_at=start + 60 * MINUTE_MS,
                decision_id="drifting-global-decision",
                **other_values,
            ),
            observation(
                "drifting-global-observation",
                "WIN",
                start + 60 * MINUTE_MS,
                settled_at=start + 80 * MINUTE_MS,
                decision_id="drifting-global-decision",
            ),
            observation(
                "valid-target-observation",
                "WIN",
                start + 80 * MINUTE_MS,
                settled_at=start + 100 * MINUTE_MS,
                decision_id="valid-target-decision",
            ),
        ]
        expected, expected_trace = progressive_reference_rebuild(rows, CUTOFF)
        actual_trace = []
        real_classify = adaptive_module.classify_profile_state

        def recording_classify(*args, **kwargs):
            result = real_classify(*args, **kwargs)
            actual_trace.append(state_trace_item(args[1], result))
            return result

        with patch.object(
            adaptive_module,
            "classify_profile_state",
            side_effect=recording_classify,
        ):
            actual = rebuild_adaptive_profile_states(list(reversed(rows)), CUTOFF)
        public = independent_settled_samples(
            list(reversed(rows)),
            PROFILE_KEY,
            CUTOFF,
        )

        self.assertEqual([item.observation_key for item in public], ["valid-target-observation"])
        self.assertEqual(set(actual), {other_key, PROFILE_KEY})
        self.assertEqual(actual_trace, expected_trace)
        self.assertEqual(actual, expected)

    def test_global_binding_same_time_is_deterministic_and_malformed_does_not_bind(self):
        opened_at = CUTOFF - 60 * MINUTE_MS
        settled_at = CUTOFF - 1
        malformed = observation(
            "malformed-global-observation",
            "LOSS",
            opened_at - 40 * MINUTE_MS,
            settled_at=settled_at - 1,
            family="",
            decision_id="malformed-global-decision",
        )
        valid_after_malformed = observation(
            "malformed-global-observation",
            "WIN",
            opened_at - 20 * MINUTE_MS,
            settled_at=settled_at,
            decision_id="malformed-global-decision",
        )
        other = observation(
            "same-time-global-observation",
            "LOSS",
            opened_at,
            settled_at=settled_at,
            family="aaa_family",
            tag="aaa_tag",
            direction="LONG",
            segment="WE-03",
            decision_id="aaa-global-decision",
        )
        target_conflict = observation(
            "same-time-global-observation",
            "WIN",
            opened_at,
            settled_at=settled_at,
            decision_id="target-global-decision",
        )
        rows = [target_conflict, valid_after_malformed, other, malformed]

        first = rebuild_adaptive_profile_states(rows, CUTOFF)
        second = rebuild_adaptive_profile_states(list(reversed(rows)), CUTOFF)
        public = independent_settled_samples(rows, PROFILE_KEY, CUTOFF)

        self.assertEqual(
            [item.observation_key for item in public],
            ["malformed-global-observation"],
        )
        self.assertEqual(first, second)
        self.assertEqual(first[PROFILE_KEY]["n12"]["wins"], 1)

    def test_rebuild_progressive_overlap_replacement_preserves_transition_history(self):
        start = CUTOFF - 60 * 20 * MINUTE_MS
        results = ["LOSS"] * 8 + ["WIN"] * 5 + ["LOSS"] * 7
        rows = [
            observation(
                f"replacement-{index:02d}",
                result,
                start + index * 20 * MINUTE_MS,
                pnl=0.0 if result == "WIN" else -1.0,
                decision_id=f"replacement-decision-{index:02d}",
            )
            for index, result in enumerate(results)
        ]
        rows[-1].observation_key = "z-replaced-loss"
        delayed = observation(
            "a-delayed-win",
            "WIN",
            rows[-1].opened_at,
            expires_at=rows[-1].expires_at,
            settled_at=rows[-1].settled_at + MINUTE_MS,
            pnl=0.0,
            decision_id="a-delayed-win-decision",
        )
        mixed = list(reversed(rows + [delayed]))

        expected, _ = progressive_reference_rebuild(mixed, CUTOFF)
        actual = rebuild_adaptive_profile_states(mixed, CUTOFF)

        self.assertEqual(expected[PROFILE_KEY]["status"], "WATCH")
        self.assertEqual(expected[PROFILE_KEY]["previous"], "PAUSED")
        self.assertEqual(expected[PROFILE_KEY]["transition"], "PAUSED->WATCH")
        self.assertEqual(expected[PROFILE_KEY]["n12"]["wins"], 6)
        self.assertEqual(actual, expected)

    def test_rebuild_handles_ten_thousand_events_with_one_linear_filter_pass(self):
        rows = delayed_production_rows(10_000)
        started = time.perf_counter()
        with patch.object(
            adaptive_module,
            "_eligible_observation",
            wraps=adaptive_module._eligible_observation,
        ) as eligibility:
            rebuilt = rebuild_adaptive_profile_states(rows, CUTOFF)
        elapsed = time.perf_counter() - started

        self.assertEqual(eligibility.call_count, 10_000)
        self.assertEqual(rebuilt[PROFILE_KEY]["n20"]["sample_size"], 20)
        self.assertLess(elapsed, 5.0)

    def test_rebuild_ten_thousand_all_overlap_never_rescans_prefix(self):
        rows = all_overlap_rows(10_000)
        started = time.perf_counter()
        with patch.object(
            adaptive_module,
            "_canonical_independent_samples",
            side_effect=AssertionError("rebuild must maintain canonical selection"),
        ):
            with patch.object(
                adaptive_module,
                "classify_profile_state",
                wraps=adaptive_module.classify_profile_state,
            ) as classify:
                rebuilt = rebuild_adaptive_profile_states(rows, CUTOFF)
        elapsed = time.perf_counter() - started

        self.assertEqual(classify.call_count, 1)
        self.assertEqual(rebuilt[PROFILE_KEY]["n20"]["sample_size"], 1)
        self.assertLess(elapsed, 5.0)

    def test_rebuild_ten_thousand_duplicate_replacements_never_rescan_prefix(self):
        rows = duplicate_replacement_rows(10_000)
        started = time.perf_counter()
        with patch.object(
            adaptive_module,
            "_canonical_independent_samples",
            side_effect=AssertionError("rebuild must maintain canonical selection"),
        ):
            with patch.object(
                adaptive_module,
                "classify_profile_state",
                wraps=adaptive_module.classify_profile_state,
            ) as classify:
                rebuilt = rebuild_adaptive_profile_states(rows, CUTOFF)
        elapsed = time.perf_counter() - started

        self.assertEqual(classify.call_count, 10_000)
        self.assertEqual(rebuilt[PROFILE_KEY]["n20"]["sample_size"], 1)
        self.assertEqual(rebuilt[PROFILE_KEY]["n20"]["wins"], 1)
        self.assertLess(elapsed, 5.0)

    def test_rebuild_ten_thousand_same_observation_conflicts_are_linear(self):
        rows = same_observation_conflict_rows(10_000)
        started = time.perf_counter()
        real_add = adaptive_module._IncrementalCanonicalSelection.add

        def recording_add(selection, event):
            return real_add(selection, event)

        with patch.object(
            adaptive_module._IncrementalCanonicalSelection,
            "add",
            autospec=True,
            side_effect=recording_add,
        ) as add:
            with patch.object(
                adaptive_module,
                "classify_profile_state",
                wraps=adaptive_module.classify_profile_state,
            ) as classify:
                rebuilt = rebuild_adaptive_profile_states(rows, CUTOFF)
        elapsed = time.perf_counter() - started

        self.assertEqual(add.call_count, 1)
        self.assertEqual(classify.call_count, 1)
        self.assertEqual(rebuilt[PROFILE_KEY]["n20"]["sample_size"], 1)
        self.assertLess(elapsed, 5.0)

    def test_rebuild_ten_thousand_cross_linked_conflicts_are_linear(self):
        rows = cross_linked_conflict_rows(10_000)
        started = time.perf_counter()
        real_add = adaptive_module._IncrementalCanonicalSelection.add

        def recording_add(selection, event):
            return real_add(selection, event)

        with patch.object(
            adaptive_module._IncrementalCanonicalSelection,
            "add",
            autospec=True,
            side_effect=recording_add,
        ) as add:
            rebuilt = rebuild_adaptive_profile_states(rows, CUTOFF)
        elapsed = time.perf_counter() - started

        self.assertEqual(add.call_count, 5_000)
        self.assertEqual(rebuilt[PROFILE_KEY]["n20"]["sample_size"], 20)
        self.assertLess(elapsed, 5.0)

    def test_rebuild_nested_overlaps_use_successor_jumps(self):
        elapsed_by_size = {}
        for sample_size in (1_000, 2_000, 4_000, 10_000):
            rows = nested_overlap_rows(sample_size)
            started = time.perf_counter()
            with patch.object(
                adaptive_module,
                "_canonical_independent_samples",
                side_effect=AssertionError("rebuild must not scan canonical prefixes"),
            ):
                with patch.object(
                    adaptive_module,
                    "classify_profile_state",
                    wraps=adaptive_module.classify_profile_state,
                ) as classify:
                    rebuilt = rebuild_adaptive_profile_states(rows, CUTOFF)
            elapsed_by_size[sample_size] = time.perf_counter() - started
            self.assertEqual(classify.call_count, sample_size)
            self.assertEqual(rebuilt[PROFILE_KEY]["n20"]["sample_size"], 1)

        self.assertLess(elapsed_by_size[10_000], 1.0)
        self.assertLess(
            elapsed_by_size[10_000],
            elapsed_by_size[2_000] * 8 + 0.1,
        )

    def test_rebuild_replays_paused_then_watch_without_future_leak(self):
        start = CUTOFF - 30 * 20 * MINUTE_MS
        results = ["LOSS"] * 9 + ["WIN"] * 5 + ["LOSS"] * 6 + ["WIN"]
        rows = [
            observation(
                f"rebuild-{index:02d}",
                result,
                start + index * 20 * MINUTE_MS,
                decision_id=f"rebuild-decision-{index:02d}",
            )
            for index, result in enumerate(results)
        ]

        before_last = rebuild_adaptive_profile_states(rows, rows[-1].settled_at)
        rebuilt = rebuild_adaptive_profile_states(rows, CUTOFF)

        self.assertEqual(before_last[PROFILE_KEY]["status"], "PAUSED")
        self.assertEqual(rebuilt[PROFILE_KEY]["status"], "WATCH")
        self.assertEqual(rebuilt[PROFILE_KEY]["previous"], "PAUSED")
        self.assertEqual(rebuilt[PROFILE_KEY]["transition"], "PAUSED->WATCH")
        self.assertEqual(rebuilt[PROFILE_KEY]["evaluated_at"], CUTOFF)

    def test_rebuild_is_stable_for_input_order_same_timestamp_and_duplicate_identity(self):
        rows = adaptive_rows(20, 5, n20_ev=-0.1)
        same_settlement = CUTOFF - 1
        for item in rows:
            item.settled_at = same_settlement
        duplicate_key = observation(
            rows[-1].observation_key,
            "WIN",
            rows[-1].opened_at + 40 * MINUTE_MS,
            settled_at=same_settlement,
            decision_id="new-decision",
        )
        duplicate_decision = observation(
            "new-key",
            "WIN",
            rows[-1].opened_at + 60 * MINUTE_MS,
            settled_at=same_settlement,
            decision_id=rows[-1].decision_id,
        )
        with_duplicates = rows + [duplicate_key, duplicate_decision]

        forward = rebuild_adaptive_profile_states(with_duplicates, CUTOFF)
        reverse = rebuild_adaptive_profile_states(list(reversed(with_duplicates)), CUTOFF)

        self.assertEqual(forward, reverse)
        self.assertEqual(forward[PROFILE_KEY]["n20"]["sample_size"], 20)
        self.assertEqual(forward[PROFILE_KEY]["status"], "PAUSED")

    def test_utc_segment_key_remains_exact(self):
        opened_at = int(datetime(2026, 8, 15, 23, 0, tzinfo=timezone.utc).timestamp() * 1000)
        segment = threshold_segment(opened_at)
        key = profile_key(10, "family", "tag", "LONG", segment)
        row = observation(
            "utc-weekend",
            "WIN",
            opened_at,
            family="family",
            tag="tag",
            direction="LONG",
            segment=segment,
        )

        result = evaluate_adaptive_profile_state([row], key, opened_at + 20 * MINUTE_MS)

        self.assertEqual(segment, "WE-23")
        self.assertEqual(result["profile_key"], "10|family|tag|LONG|WE-23")
        self.assertEqual(result["n12"]["sample_size"], 1)


if __name__ == "__main__":
    unittest.main()
