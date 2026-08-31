import unittest

from app.models import Signal
from app.range_policy import RangePolicyConfig, evaluate_range_policy
from app.simulator import AccountSimulator


def signal(*, direction="LONG", wave_state="RANGE_MID", structure_state="NO_NEARBY_LEVEL", level_kind=""):
    return Signal(
        direction=direction,
        timeframe_minutes=10,
        level="B",
        reason="test",
        price=100.0,
        open_time=1_000,
        wave_state=wave_state,
        entry_structure_shadow={
            "entry_structure_state": structure_state,
            "active_level_kind": level_kind,
        },
    )


class RangePolicyTest(unittest.TestCase):
    def test_range_mid_allows_long_and_short_support(self):
        config = RangePolicyConfig(mode="SHADOW_ONLY")

        long_decision = evaluate_range_policy(signal(), config=config)
        short_decision = evaluate_range_policy(
            signal(direction="SHORT", structure_state="SUPPORT_REJECTED", level_kind="SUPPORT"),
            config=config,
        )

        self.assertEqual(long_decision["action"], "ALLOW")
        self.assertFalse(long_decision["would_block"])
        self.assertEqual(short_decision["action"], "ALLOW")
        self.assertFalse(short_decision["would_block"])

    def test_range_mid_short_without_support_is_shadow_restricted(self):
        decision = evaluate_range_policy(
            signal(direction="SHORT", structure_state="APPROACHING_RESISTANCE", level_kind="RESISTANCE"),
            config=RangePolicyConfig(mode="SHADOW_ONLY"),
        )

        self.assertEqual(decision["action"], "RESTRICT")
        self.assertTrue(decision["would_block"])
        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["reason_code"], "RANGE_MID_SHORT_NO_SUPPORT")

    def test_range_high_only_allows_rejection_or_no_nearby_level(self):
        config = RangePolicyConfig(mode="SHADOW_ONLY")

        rejected = evaluate_range_policy(
            signal(
                wave_state="RANGE_HIGH",
                structure_state="RESISTANCE_REJECTED",
                level_kind="RESISTANCE",
            ),
            config=config,
        )
        pending = evaluate_range_policy(
            signal(
                wave_state="RANGE_HIGH",
                structure_state="BREAKOUT_PENDING",
                level_kind="RESISTANCE",
            ),
            config=config,
        )

        self.assertEqual(rejected["action"], "ALLOW")
        self.assertEqual(pending["reason_code"], "RANGE_HIGH_STRUCTURE_RISK")
        self.assertTrue(pending["would_block"])
        self.assertTrue(pending["allowed"])

    def test_live_mode_turns_shadow_restriction_into_block(self):
        decision = evaluate_range_policy(
            signal(
                wave_state="RANGE_HIGH",
                structure_state="BREAKOUT_PENDING",
                level_kind="RESISTANCE",
            ),
            config=RangePolicyConfig(mode="LIVE"),
        )

        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["action"], "BLOCK")

    def test_non_range_state_is_unchanged(self):
        decision = evaluate_range_policy(
            signal(wave_state="UP_LEG", structure_state="BREAKOUT_PENDING", level_kind="RESISTANCE"),
            config=RangePolicyConfig(mode="LIVE"),
        )

        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["action"], "UNCHANGED")

    def test_simulator_persists_range_shadow_on_order(self):
        source = signal(direction="SHORT", structure_state="SUPPORT_REJECTED", level_kind="SUPPORT")
        source = Signal(**{**source.to_dict(), "range_policy_shadow": evaluate_range_policy(source)})

        order = AccountSimulator().open_order(source, entry_price=100.0, opened_at=1_000)

        self.assertEqual(order.range_policy_shadow["reason_code"], "RANGE_POLICY_ALLOWED")
        self.assertEqual(order.to_dict()["range_policy_shadow"]["wave_state"], "RANGE_MID")


if __name__ == "__main__":
    unittest.main()
