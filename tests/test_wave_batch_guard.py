import unittest

from app.models import SimulatedOrder
from app.wave_batch_guard import WaveBatchGuardConfig, evaluate_wave_batch_guard


MINUTE = 60_000


def order(
    order_id: int,
    batch_id: str,
    opened_minute: int,
    *,
    result: str | None = None,
    guard_mode: str = "NORMAL",
) -> SimulatedOrder:
    settled = result in {"WIN", "LOSS"}
    return SimulatedOrder(
        id=order_id,
        direction="LONG",
        timeframe_minutes=10,
        level="A",
        reason="wave batch",
        entry_price=100.0,
        opened_at=opened_minute * MINUTE,
        expires_at=(opened_minute + 10) * MINUTE,
        status="SETTLED" if settled else "OPEN",
        result=result,
        settled_at=(opened_minute + 10) * MINUTE if settled else None,
        pnl=8.0 if result == "WIN" else -10.0 if result == "LOSS" else 0.0,
        wave_batch_id=batch_id,
        wave_state="UP_LEG",
        wave_guard_mode=guard_mode,
    )


def failed_batch(start_id: int, batch_id: str, opened_minute: int) -> list[SimulatedOrder]:
    return [
        order(start_id, batch_id, opened_minute, result="LOSS"),
        order(start_id + 1, batch_id, opened_minute + 2, result="LOSS"),
    ]


class WaveBatchGuardTest(unittest.TestCase):
    def setUp(self):
        self.config = WaveBatchGuardConfig()

    def test_empty_new_batch_is_allowed(self):
        decision = evaluate_wave_batch_guard([], 20 * MINUTE, "wave-new", self.config)

        self.assertFalse(decision.blocked)
        self.assertEqual(decision.mode, "NORMAL")
        self.assertTrue(decision.allow_progression)

    def test_legacy_orders_without_batch_id_are_not_grouped_together(self):
        legacy_loss = order(1, "", 0, result="LOSS")

        decision = evaluate_wave_batch_guard([legacy_loss], 11 * MINUTE, "", self.config)

        self.assertFalse(decision.blocked)
        self.assertEqual(decision.mode, "NORMAL")

    def test_first_settled_loss_locks_current_batch_without_refill(self):
        orders = [order(1, "wave-a", 0, result="LOSS")]

        decision = evaluate_wave_batch_guard(orders, 11 * MINUTE, "wave-a", self.config)

        self.assertTrue(decision.blocked)
        self.assertEqual(decision.code, "WAVE_BATCH_LOSS_LOCKED")
        self.assertEqual((decision.batch_wins, decision.batch_losses), (0, 1))

    def test_current_batch_never_opens_more_than_two_orders(self):
        orders = [order(1, "wave-a", 0), order(2, "wave-a", 2)]

        decision = evaluate_wave_batch_guard(orders, 3 * MINUTE, "wave-a", self.config)

        self.assertTrue(decision.blocked)
        self.assertEqual(decision.code, "WAVE_BATCH_FULL")

    def test_single_failed_batch_allows_a_new_confirmed_wave(self):
        decision = evaluate_wave_batch_guard(
            failed_batch(1, "wave-a", 0),
            20 * MINUTE,
            "wave-b",
            self.config,
        )

        self.assertFalse(decision.blocked)
        self.assertEqual(decision.failed_batches, 1)
        self.assertEqual(decision.mode, "NORMAL")

    def test_two_failed_batches_within_hour_trigger_global_cooldown(self):
        orders = failed_batch(1, "wave-a", 0) + failed_batch(3, "wave-b", 30)

        decision = evaluate_wave_batch_guard(orders, 50 * MINUTE, "wave-c", self.config)

        self.assertTrue(decision.blocked)
        self.assertEqual(decision.code, "WAVE_GLOBAL_COOLDOWN")
        self.assertEqual(decision.mode, "COOLDOWN")
        self.assertEqual(decision.pause_until, 102 * MINUTE)

    def test_cooldown_expiry_allows_exactly_one_fixed_recovery_order(self):
        failed = failed_batch(1, "wave-a", 0) + failed_batch(3, "wave-b", 30)
        ready = evaluate_wave_batch_guard(failed, 103 * MINUTE, "wave-c", self.config)

        self.assertFalse(ready.blocked)
        self.assertEqual(ready.mode, "RECOVERY")
        self.assertFalse(ready.allow_progression)

        recovery_open = order(5, "wave-c", 103, guard_mode="RECOVERY")
        pending = evaluate_wave_batch_guard(
            [*failed, recovery_open], 104 * MINUTE, "wave-c", self.config
        )

        self.assertTrue(pending.blocked)
        self.assertEqual(pending.code, "WAVE_RECOVERY_PENDING")

    def test_recovery_win_unlocks_and_recovery_loss_rearms_cooldown(self):
        failed = failed_batch(1, "wave-a", 0) + failed_batch(3, "wave-b", 30)
        recovery_win = order(5, "wave-c", 103, result="WIN", guard_mode="RECOVERY")
        recovered = evaluate_wave_batch_guard(
            [*failed, recovery_win], 114 * MINUTE, "wave-d", self.config
        )

        self.assertFalse(recovered.blocked)
        self.assertEqual(recovered.mode, "NORMAL")

        recovery_loss = order(5, "wave-c", 103, result="LOSS", guard_mode="RECOVERY")
        rearmed = evaluate_wave_batch_guard(
            [*failed, recovery_loss], 150 * MINUTE, "wave-d", self.config
        )
        retry = evaluate_wave_batch_guard(
            [*failed, recovery_loss], 174 * MINUTE, "wave-d", self.config
        )

        self.assertTrue(rearmed.blocked)
        self.assertEqual(rearmed.pause_until, 173 * MINUTE)
        self.assertEqual(retry.mode, "RECOVERY")
        self.assertFalse(retry.blocked)

    def test_same_persisted_orders_rebuild_identical_decision(self):
        orders = failed_batch(1, "wave-a", 0) + failed_batch(3, "wave-b", 30)

        first = evaluate_wave_batch_guard(orders, 50 * MINUTE, "wave-c", self.config)
        second = evaluate_wave_batch_guard(list(reversed(orders)), 50 * MINUTE, "wave-c", self.config)

        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
