from __future__ import annotations

import math
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

from app.adaptive_profile_state import (
    ADAPTIVE_PROFILE_STATE_VERSION,
    AdaptiveProfileStateConfig,
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

    def test_unknown_previous_is_stateless_and_legacy_degraded_is_paused(self):
        rows = adaptive_rows(20, 7, n20_ev=0.0)
        unknown = evaluate_adaptive_profile_state(rows, PROFILE_KEY, CUTOFF, "OLD_UNKNOWN")
        legacy = evaluate_adaptive_profile_state(
            rows,
            PROFILE_KEY,
            CUTOFF,
            {"status": "DEGRADED"},
        )

        self.assertEqual(unknown["status"], "ACTIVE")
        self.assertIsNone(unknown["previous"])
        self.assertEqual(legacy["status"], "WATCH")
        self.assertEqual(legacy["previous"], "PAUSED")
        self.assertEqual(legacy["transition"], "PAUSED->WATCH")

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
