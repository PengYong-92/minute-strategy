from __future__ import annotations

import math
import unittest
from dataclasses import replace

from app.profile_admission import (
    PROFILE_ADMISSION_VERSION,
    ProfileAdmissionContext,
    ProfileAdmissionPolicy,
    baseline_policy,
    candidate_policy,
    evaluate_profile_admission,
    policy_grid,
    rank_admitted_candidates,
    select_admitted_candidate,
)


def context(**overrides) -> ProfileAdmissionContext:
    values = {
        "profile_key": "10|short_observe|generic_short_observe|SHORT|WD-08",
        "direction": "SHORT",
        "order_slot": "FIRST",
        "daily_selected": True,
        "qualification_state": "QUALIFIED",
        "daily_rank": 1,
        "daily_win_rate": 0.66,
        "adaptive_state": "ACTIVE",
        "adaptive_transition": "WATCH_TO_ACTIVE",
        "adaptive_evaluated_at": 1_800_000_000_000,
        "n12_sample_size": 12,
        "n12_wins": 7,
        "n20_sample_size": 20,
        "n20_ev": 1.0,
        "candidate_origin": "OBSERVATION",
        "candidate_ordinal": 1,
    }
    values.update(overrides)
    return ProfileAdmissionContext(**values)


class ProfileAdmissionTest(unittest.TestCase):
    def test_policy_hash_is_canonical_and_grid_is_unique(self):
        first = ProfileAdmissionPolicy(
            resident_allowed_states=("WATCH", "ACTIVE"),
            fast_directions=("SHORT", "SHORT"),
        )
        second = ProfileAdmissionPolicy(
            resident_allowed_states=("ACTIVE", "WATCH"),
            fast_directions=("SHORT",),
        )
        self.assertEqual(first.policy_hash, second.policy_hash)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(
            ProfileAdmissionPolicy(fast_n20_ev_min=-0.0).policy_hash,
            ProfileAdmissionPolicy(fast_n20_ev_min=0.0).policy_hash,
        )
        self.assertEqual(
            ProfileAdmissionPolicy(resident_daily_win_rate_floor=-0.0).policy_hash,
            ProfileAdmissionPolicy(resident_daily_win_rate_floor=0.0).policy_hash,
        )
        self.assertEqual(
            ProfileAdmissionPolicy(resident_short_daily_win_rate_floor=-0.0).policy_hash,
            ProfileAdmissionPolicy(resident_short_daily_win_rate_floor=0.0).policy_hash,
        )

        policies = policy_grid()
        self.assertEqual(len(policies), 32)
        self.assertEqual(len({item.policy_hash for item in policies}), 32)
        self.assertEqual(policies, policy_grid())

    def test_policy_rejects_invalid_numeric_values(self):
        with self.assertRaisesRegex(ValueError, "fast_n20_ev_min"):
            ProfileAdmissionPolicy(fast_n20_ev_min=math.inf)
        with self.assertRaisesRegex(ValueError, "fast_n12"):
            ProfileAdmissionPolicy(fast_n12_min_wins=9, fast_n12_max_wins=8)
        with self.assertRaisesRegex(ValueError, "resident_daily_win_rate_floor"):
            ProfileAdmissionPolicy(resident_daily_win_rate_floor=1.1)
        with self.assertRaisesRegex(ValueError, "resident_long_n12_max_wins"):
            ProfileAdmissionPolicy(resident_long_n12_max_wins=13)
        with self.assertRaisesRegex(ValueError, "resident_n12_max_wins"):
            ProfileAdmissionPolicy(resident_n12_max_wins=None)
        with self.assertRaisesRegex(ValueError, "resident_short_daily_win_rate_floor"):
            ProfileAdmissionPolicy(resident_short_daily_win_rate_floor=True)
        with self.assertRaisesRegex(ValueError, "fast_enabled"):
            ProfileAdmissionPolicy(fast_enabled=1)
        with self.assertRaisesRegex(ValueError, "fast_n20_ev_min"):
            ProfileAdmissionPolicy(fast_n20_ev_min=True)
        with self.assertRaisesRegex(ValueError, "fast_directions"):
            ProfileAdmissionPolicy(fast_directions=("SIDEWAYS",))

    def test_candidate_policy_is_the_automatic_search_candidate(self):
        policy = candidate_policy()
        self.assertEqual(policy.version, PROFILE_ADMISSION_VERSION)
        self.assertEqual(policy.resident_long_n12_max_wins, 7)
        self.assertEqual(policy.resident_short_n12_max_wins, 9)
        self.assertIsNone(policy.resident_daily_win_rate_floor)
        self.assertIsNone(policy.resident_long_daily_win_rate_floor)
        self.assertEqual(policy.resident_short_daily_win_rate_floor, 0.625)
        self.assertTrue(policy.fast_enabled)
        self.assertEqual(policy.fast_directions, ("SHORT",))
        self.assertEqual((policy.fast_n12_min_wins, policy.fast_n12_max_wins), (7, 8))

    def test_v1_baseline_and_candidate_have_golden_payloads_and_hashes(self):
        expected = {
            "baseline": (
                '{"fast_allow_progression":false,"fast_allow_second_order":false,'
                '"fast_allowed_states":["ACTIVE"],"fast_directions":["SHORT"],'
                '"fast_enabled":false,"fast_n12_max_wins":8,"fast_n12_min_wins":7,'
                '"fast_n20_ev_min":0.0,"resident_allowed_states":["ACTIVE","WARMUP","WATCH"],'
                '"resident_daily_win_rate_floor":null,"resident_long_daily_win_rate_floor":null,'
                '"resident_long_n12_max_wins":null,"resident_n12_max_wins":12,'
                '"resident_short_daily_win_rate_floor":null,"resident_short_n12_max_wins":null,'
                '"version":"PROFILE_ADMISSION_V1","watch_allow_first_order":true,'
                '"watch_allow_progression":false,"watch_allow_second_order":false}',
                "f9ddeaa9ecf86a236ea23e405048de8d605eb03a1a0dab4715ff4294c26d5c67",
            ),
            "candidate": (
                '{"fast_allow_progression":false,"fast_allow_second_order":false,'
                '"fast_allowed_states":["ACTIVE"],"fast_directions":["SHORT"],'
                '"fast_enabled":true,"fast_n12_max_wins":8,"fast_n12_min_wins":7,'
                '"fast_n20_ev_min":0.0,"resident_allowed_states":["ACTIVE","WATCH"],'
                '"resident_daily_win_rate_floor":null,"resident_long_daily_win_rate_floor":null,'
                '"resident_long_n12_max_wins":7,"resident_n12_max_wins":12,'
                '"resident_short_daily_win_rate_floor":0.625,"resident_short_n12_max_wins":9,'
                '"version":"PROFILE_ADMISSION_V1","watch_allow_first_order":true,'
                '"watch_allow_progression":false,"watch_allow_second_order":false}',
                "8ac0366d26a6f6e7cd0e69f887d73b6eeb8f7d548bc664caabc28f0ec66072bc",
            ),
        }
        for name, policy in (
            ("baseline", baseline_policy()),
            ("candidate", candidate_policy()),
        ):
            with self.subTest(name=name):
                payload, policy_hash = expected[name]
                self.assertEqual(policy.to_json(), payload)
                self.assertEqual(policy.policy_hash, policy_hash)

    def test_context_rejects_profile_direction_mismatch_and_impossible_windows(self):
        with self.assertRaisesRegex(ValueError, "profile_key direction"):
            context(
                profile_key="10|long|rebound|LONG|WD-08",
                direction="SHORT",
            )
        with self.assertRaisesRegex(ValueError, "profile_key"):
            context(profile_key=["not", "a", "key"])
        with self.assertRaisesRegex(ValueError, "n20_sample_size"):
            context(n12_sample_size=12, n12_wins=7, n20_sample_size=11)
        with self.assertRaisesRegex(ValueError, "mature"):
            context(adaptive_state="ACTIVE", n12_sample_size=11, n12_wins=7)
        with self.assertRaisesRegex(ValueError, "adaptive_transition"):
            context(adaptive_transition=["WATCH", "ACTIVE"])

    def test_resident_matrix_and_overheat(self):
        policy = candidate_policy()
        active = evaluate_profile_admission(context(), policy)
        self.assertTrue(active.allowed)
        self.assertEqual(active.channel, "RESIDENT")
        self.assertTrue(active.allow_second_order)
        self.assertTrue(active.allow_progression)

        watch = evaluate_profile_admission(
            context(adaptive_state="WATCH", adaptive_transition="ACTIVE_TO_WATCH"),
            policy,
        )
        self.assertTrue(watch.allowed)
        self.assertFalse(watch.allow_second_order)
        self.assertFalse(watch.allow_progression)

        watch_second = evaluate_profile_admission(
            context(
                adaptive_state="WATCH",
                adaptive_transition="ACTIVE_TO_WATCH",
                order_slot="SECOND",
            ),
            policy,
        )
        self.assertFalse(watch_second.allowed)
        self.assertEqual(watch_second.code, "WATCH_SECOND_ORDER_BLOCKED")

        for state in ("PAUSED", "WARMUP"):
            with self.subTest(state=state):
                overrides = {"adaptive_state": state}
                if state == "WARMUP":
                    overrides.update(
                        n12_sample_size=0,
                        n12_wins=0,
                        n20_sample_size=0,
                        n20_ev=0.0,
                    )
                decision = evaluate_profile_admission(context(**overrides), policy)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.channel, "NONE")

        overheated = evaluate_profile_admission(context(n12_wins=10), policy)
        self.assertFalse(overheated.allowed)
        self.assertEqual(overheated.code, "RESIDENT_N12_OVERHEATED")

        long_overheated = evaluate_profile_admission(
            context(
                profile_key="10|long|rebound|LONG|WD-08",
                direction="LONG",
                n12_wins=8,
            ),
            policy,
        )
        self.assertFalse(long_overheated.allowed)
        self.assertEqual(long_overheated.code, "RESIDENT_N12_OVERHEATED")

        unqualified = evaluate_profile_admission(
            context(qualification_state="NOT_QUALIFIED"),
            policy,
        )
        self.assertFalse(unqualified.allowed)
        self.assertEqual(unqualified.code, "RESIDENT_QUALIFICATION_BLOCKED")

    def test_fast_lane_only_admits_first_short_active_in_range(self):
        policy = candidate_policy()
        fast = evaluate_profile_admission(
            context(daily_selected=False, daily_rank=None),
            policy,
        )
        self.assertTrue(fast.allowed)
        self.assertEqual(fast.channel, "FAST")
        self.assertEqual(fast.code, "FAST_ADMITTED")
        self.assertFalse(fast.allow_second_order)
        self.assertFalse(fast.allow_progression)

        cases = (
            ("LONG", "ACTIVE", 7, 1.0, "FIRST", "FAST_DIRECTION_BLOCKED"),
            ("SHORT", "WATCH", 7, 1.0, "FIRST", "FAST_STATE_BLOCKED"),
            ("SHORT", "PAUSED", 7, 1.0, "FIRST", "ADAPTIVE_PAUSED"),
            ("SHORT", "ACTIVE", 6, 1.0, "FIRST", "FAST_N12_BLOCKED"),
            ("SHORT", "ACTIVE", 7, -0.01, "FIRST", "FAST_N20_EV_BLOCKED"),
            ("SHORT", "ACTIVE", 7, 1.0, "SECOND", "FAST_SECOND_ORDER_BLOCKED"),
        )
        for direction, state, wins, ev, slot, code in cases:
            with self.subTest(code=code):
                decision = evaluate_profile_admission(
                    context(
                        profile_key=(
                            "10|long_observe|generic_long_observe|LONG|WD-08"
                            if direction == "LONG"
                            else "10|short_observe|generic_short_observe|SHORT|WD-08"
                        ),
                        daily_selected=False,
                        daily_rank=None,
                        direction=direction,
                        adaptive_state=state,
                        n12_wins=wins,
                        n20_ev=ev,
                        order_slot=slot,
                    ),
                    policy,
                )
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.code, code)

        warmup = evaluate_profile_admission(
            context(
                daily_selected=False,
                daily_rank=None,
                adaptive_state="WARMUP",
                n12_sample_size=0,
                n12_wins=0,
                n20_sample_size=0,
                n20_ev=0.0,
            ),
            policy,
        )
        self.assertFalse(warmup.allowed)
        self.assertEqual(warmup.code, "FAST_STATE_BLOCKED")

        primary = evaluate_profile_admission(
            context(daily_selected=False, daily_rank=None, candidate_ordinal=0),
            policy,
        )
        self.assertFalse(primary.allowed)
        self.assertEqual(primary.code, "FAST_PRIMARY_BLOCKED")

        with self.assertRaisesRegex(ValueError, "fast_allowed_states"):
            replace(policy, fast_allowed_states=("ACTIVE", "WARMUP"))
        with self.assertRaisesRegex(ValueError, "FAST"):
            replace(policy, fast_allow_second_order=True)
        with self.assertRaisesRegex(ValueError, "WATCH"):
            replace(policy, watch_allow_progression=True)

    def test_default_without_fast_rejects_unselected_profile(self):
        decision = evaluate_profile_admission(
            context(daily_selected=False, daily_rank=None),
            ProfileAdmissionPolicy(),
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "DAILY_PROFILE_NOT_SELECTED")

        baseline_primary = evaluate_profile_admission(
            context(
                daily_selected=False,
                daily_rank=None,
                candidate_ordinal=0,
            ),
            ProfileAdmissionPolicy(),
        )
        self.assertFalse(baseline_primary.allowed)
        self.assertEqual(baseline_primary.code, "DAILY_PROFILE_NOT_SELECTED")

        baseline_warmup = evaluate_profile_admission(
            context(
                adaptive_state="WARMUP",
                n12_sample_size=0,
                n12_wins=0,
                n20_sample_size=0,
                n20_ev=0.0,
            ),
            baseline_policy(),
        )
        self.assertTrue(baseline_warmup.allowed)
        self.assertEqual(baseline_warmup.channel, "RESIDENT")

    def test_ranking_is_resident_then_fast_and_deterministic(self):
        policy = candidate_policy()
        fast = context(
            profile_key="10|short_observe|generic_short_observe|SHORT|WD-01",
            daily_selected=False,
            daily_rank=None,
            candidate_ordinal=1,
        )
        second_resident = context(
            profile_key="10|long|rebound|LONG|WD-03",
            direction="LONG",
            daily_rank=2,
            candidate_ordinal=2,
        )
        first_resident = replace(
            second_resident,
            profile_key="10|long|rebound|LONG|WD-02",
            daily_rank=1,
            candidate_ordinal=99,
        )
        ranked = rank_admitted_candidates(
            (fast, second_resident, first_resident),
            policy,
        )
        self.assertEqual(
            [item.context.profile_key for item in ranked],
            [first_resident.profile_key, second_resident.profile_key, fast.profile_key],
        )
        selected = select_admitted_candidate(
            (fast, second_resident, first_resident),
            policy,
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected.context.profile_key, first_resident.profile_key)


if __name__ == "__main__":
    unittest.main()
