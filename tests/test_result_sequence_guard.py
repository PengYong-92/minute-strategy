import unittest

from app.result_sequence_guard import ResultSequenceGuardConfig, evaluate_result_sequence_guard
from scripts.replay_result_sequence_guard import replay_guard


def order(
    order_id: int,
    opened_at: int,
    settled_at: int,
    result: str,
    direction: str = "SHORT",
) -> dict:
    return {
        "order_id": order_id,
        "opened_at": opened_at,
        "settled_at": settled_at,
        "result": result,
        "direction": direction,
        "pnl": 8.0 if result == "WIN" else -10.0,
    }


class ResultSequenceGuardTest(unittest.TestCase):
    def test_defaults_match_selected_production_parameters(self):
        config = ResultSequenceGuardConfig().normalized()

        self.assertTrue(config.enabled)
        self.assertEqual(config.loss_streak, 3)
        self.assertEqual(config.cooldown_minutes, 20)
        self.assertEqual(config.scope, "DIRECTION")

    def test_future_settlement_cannot_trigger_guard(self):
        config = ResultSequenceGuardConfig(loss_streak=1, cooldown_minutes=20)

        decision = evaluate_result_sequence_guard(
            [order(1, 0, 600_000, "LOSS")],
            current_time=120_000,
            direction="SHORT",
            config=config,
        )

        self.assertFalse(decision.blocked)
        self.assertEqual(decision.consecutive_losses, 0)

    def test_two_settled_losses_pause_until_latest_loss_plus_cooldown(self):
        config = ResultSequenceGuardConfig(loss_streak=2, cooldown_minutes=20)
        history = [
            order(1, 0, 600_000, "LOSS"),
            order(2, 120_000, 720_000, "LOSS"),
        ]

        blocked = evaluate_result_sequence_guard(
            history,
            current_time=721_000,
            direction="SHORT",
            config=config,
        )
        resumed = evaluate_result_sequence_guard(
            history,
            current_time=1_921_000,
            direction="SHORT",
            config=config,
        )

        self.assertTrue(blocked.blocked)
        self.assertEqual(blocked.pause_until, 1_920_000)
        self.assertFalse(resumed.blocked)

    def test_direction_scope_only_counts_matching_direction(self):
        history = [
            order(1, 0, 600_000, "LOSS", "LONG"),
            order(2, 120_000, 720_000, "LOSS", "LONG"),
        ]
        config = ResultSequenceGuardConfig(
            loss_streak=2,
            cooldown_minutes=20,
            scope="DIRECTION",
        )

        long_decision = evaluate_result_sequence_guard(
            history,
            current_time=721_000,
            direction="LONG",
            config=config,
        )
        short_decision = evaluate_result_sequence_guard(
            history,
            current_time=721_000,
            direction="SHORT",
            config=config,
        )

        self.assertTrue(long_decision.blocked)
        self.assertFalse(short_decision.blocked)

    def test_replay_does_not_learn_from_blocked_order_outcomes(self):
        config = ResultSequenceGuardConfig(loss_streak=2, cooldown_minutes=30)
        rows = [
            order(1, 0, 10, "LOSS"),
            order(2, 20, 30, "LOSS"),
            order(3, 40, 50, "WIN"),
            order(4, 60, 70, "WIN"),
        ]

        replay = replay_guard(rows, config=config)

        self.assertEqual([item["order_id"] for item in replay["accepted"]], [1, 2])
        self.assertEqual([item["order_id"] for item in replay["blocked"]], [3, 4])


if __name__ == "__main__":
    unittest.main()
