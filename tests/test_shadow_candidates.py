import json
import unittest

from app.profile_admission import baseline_policy, candidate_policy
from app.shadow_candidates import build_profile_admission_arms


def runtime_payload(admission_policy=None, *, threshold=None):
    policy = admission_policy or baseline_policy()
    payload = {
        "strategy_build_id": "build-1",
        "order_policy": {
            "max_open_orders": 2,
            "max_open_long_orders": 1,
            "max_open_short_orders": 2,
            "min_order_gap_ms": 120_000,
        },
        "stake": {"amount": 10.0, "win_return": 18.0},
        "guards": {"result_sequence": {"enabled": True}},
        "profiles": {
            "admission": {
                "enabled": policy.fast_enabled,
                "policy": policy.to_dict(),
                "policy_hash": policy.policy_hash,
            }
        },
        "trade_score_threshold": threshold,
    }
    return {
        "hash": "formal-runtime-hash",
        "canonical_payload": json.dumps(payload, sort_keys=True, separators=(",", ":")),
        "strategy_build_id": "build-1",
    }


def seed(policy=None, *, threshold=None):
    selected = policy or baseline_policy()
    return {
        "symbol": "BTCUSDT",
        "context": ("BTCUSDT", 3),
        "profile_admission_policy": selected.to_dict(),
        "runtime_config": runtime_payload(selected, threshold=threshold),
    }


class ShadowCandidateGenerationTest(unittest.TestCase):
    def test_generation_contains_champion_and_at_most_seven_unique_challengers(self):
        arms = build_profile_admission_arms(seed(), max_challengers=7)

        self.assertEqual(len(arms), 8)
        self.assertEqual(arms[0].role, "CHAMPION")
        self.assertTrue(all(item.role == "CHALLENGER" for item in arms[1:]))
        self.assertEqual(len({item.parameter_hash for item in arms}), 8)
        self.assertEqual(arms[0].policy.policy_hash, baseline_policy().policy_hash)

    def test_every_snapshot_contains_complete_runtime_and_only_admission_varies(self):
        arms = build_profile_admission_arms(seed(), max_challengers=3)

        self.assertEqual(len(arms), 4)
        analyzer_hashes = {item.analyzer_hash for item in arms}
        self.assertEqual(len(analyzer_hashes), 1)
        for item in arms:
            parameters = item.parameters.parameters
            runtime = parameters["runtime_config"]
            self.assertEqual(runtime["order_policy"]["max_open_orders"], 2)
            self.assertEqual(runtime["stake"]["amount"], 10.0)
            self.assertEqual(runtime["guards"]["result_sequence"]["enabled"], True)
            self.assertEqual(
                runtime["profiles"]["admission"]["policy_hash"],
                item.policy.policy_hash,
            )

    def test_generation_is_deterministic_and_excludes_current_policy_duplicate(self):
        first = build_profile_admission_arms(seed(candidate_policy()), max_challengers=7)
        second = build_profile_admission_arms(seed(candidate_policy()), max_challengers=7)

        self.assertEqual(
            [item.parameter_hash for item in first],
            [item.parameter_hash for item in second],
        )
        self.assertEqual(
            sum(item.policy.policy_hash == candidate_policy().policy_hash for item in first),
            1,
        )

    def test_analyzer_hash_ignores_admission_but_changes_for_scoring_configuration(self):
        baseline = build_profile_admission_arms(seed(baseline_policy()), max_challengers=1)
        changed_admission = build_profile_admission_arms(
            seed(candidate_policy()),
            max_challengers=1,
        )
        changed_score = build_profile_admission_arms(
            seed(baseline_policy(), threshold=75.0),
            max_challengers=1,
        )

        self.assertEqual(baseline[0].analyzer_hash, changed_admission[0].analyzer_hash)
        self.assertNotEqual(baseline[0].analyzer_hash, changed_score[0].analyzer_hash)


if __name__ == "__main__":
    unittest.main()
