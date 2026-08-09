import unittest
from types import SimpleNamespace

from app.profile_degradation_guard import (
    MINUTE_MS,
    PROFILE_DEGRADATION_LOSS_STREAK,
    ProfileDegradationGuardConfig,
    evaluate_profile_degradation_guard,
)


def order(
    order_id: object,
    result: str | None,
    settled_at: object,
    *,
    profile: str = "p1",
    version: str = "v1",
    status: str = "SETTLED",
    probe: object = False,
    triggered_at: object = 0,
    opened_at: object = None,
) -> SimpleNamespace:
    resolved_opened_at = (
        opened_at
        if opened_at is not None
        else max(0, settled_at - 10 * MINUTE_MS)
        if isinstance(settled_at, int)
        else 0
    )
    return SimpleNamespace(
        id=order_id,
        status=status,
        result=result,
        settled_at=settled_at,
        opened_at=resolved_opened_at,
        profile_key=profile,
        daily_profile_version=version,
        profile_degradation_probe=probe,
        profile_degradation_triggered_at=triggered_at,
    )


def three_losses() -> list[SimpleNamespace]:
    return [
        order(1, "LOSS", 2 * MINUTE_MS),
        order(2, "LOSS", 6 * MINUTE_MS),
        order(3, "LOSS", 10 * MINUTE_MS),
    ]


class ProfileDegradationGuardTest(unittest.TestCase):
    def evaluate(
        self,
        orders: list[SimpleNamespace],
        current_time: int,
        *,
        profile: str = "p1",
        version: str = "v1",
        cooldown_minutes: int = 60,
    ):
        return evaluate_profile_degradation_guard(
            orders,
            current_time=current_time,
            profile_key=profile,
            daily_profile_version=version,
            config=ProfileDegradationGuardConfig(
                cooldown_minutes=cooldown_minutes
            ),
        )

    def test_three_trailing_losses_enter_full_cooldown(self):
        decision = self.evaluate(three_losses(), 11 * MINUTE_MS)

        self.assertEqual(PROFILE_DEGRADATION_LOSS_STREAK, 3)
        self.assertEqual(decision.status, "COOLDOWN")
        self.assertTrue(decision.blocked)
        self.assertFalse(decision.allow_progression)
        self.assertEqual(decision.consecutive_losses, 3)
        self.assertEqual(decision.last_loss_settled_at, 10 * MINUTE_MS)
        self.assertEqual(decision.triggered_at, 10 * MINUTE_MS)
        self.assertEqual(decision.pause_until, 70 * MINUTE_MS)
        self.assertEqual(decision.reason, "画像连续三笔亏损，进入冷却")

    def test_exact_cooldown_boundary_is_recovery_ready(self):
        decision = self.evaluate(three_losses(), 70 * MINUTE_MS)

        self.assertEqual(decision.status, "RECOVERY_READY")
        self.assertFalse(decision.blocked)
        self.assertFalse(decision.allow_progression)
        self.assertEqual(decision.pause_until, 70 * MINUTE_MS)
        self.assertEqual(decision.triggered_at, 10 * MINUTE_MS)
        self.assertEqual(decision.reason, "画像冷却结束，允许一笔基础金额试探单")

    def test_open_probe_for_same_trigger_is_recovery_pending(self):
        probe = order(
            4,
            None,
            None,
            status="OPEN",
            probe=True,
            triggered_at=10 * MINUTE_MS,
            opened_at=70 * MINUTE_MS,
        )

        decision = self.evaluate([*three_losses(), probe], 71 * MINUTE_MS)

        self.assertEqual(decision.status, "RECOVERY_PENDING")
        self.assertTrue(decision.blocked)
        self.assertFalse(decision.allow_progression)
        self.assertEqual(decision.probe_order_id, 4)
        self.assertEqual(decision.triggered_at, 10 * MINUTE_MS)
        self.assertEqual(decision.reason, "画像恢复试探单尚未结算")

    def test_probe_win_returns_to_normal(self):
        probe = order(
            4,
            "WIN",
            80 * MINUTE_MS,
            probe=True,
            triggered_at=10 * MINUTE_MS,
        )

        decision = self.evaluate([*three_losses(), probe], 81 * MINUTE_MS)

        self.assertEqual(decision.status, "NORMAL")
        self.assertFalse(decision.blocked)
        self.assertTrue(decision.allow_progression)
        self.assertEqual(decision.consecutive_losses, 0)
        self.assertEqual(decision.triggered_at, 0)
        self.assertEqual(decision.reason, "画像连续亏损未达到三笔")

    def test_probe_loss_restarts_full_cooldown_from_probe_settlement(self):
        probe = order(
            4,
            "LOSS",
            80 * MINUTE_MS,
            probe=True,
            triggered_at=10 * MINUTE_MS,
        )

        decision = self.evaluate([*three_losses(), probe], 81 * MINUTE_MS)

        self.assertEqual(decision.status, "COOLDOWN")
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.consecutive_losses, 4)
        self.assertEqual(decision.last_loss_settled_at, 80 * MINUTE_MS)
        self.assertEqual(decision.triggered_at, 80 * MINUTE_MS)
        self.assertEqual(decision.pause_until, 140 * MINUTE_MS)

    def test_zero_and_negative_cooldown_are_disabled(self):
        self.assertEqual(
            ProfileDegradationGuardConfig(-10).normalized().cooldown_minutes,
            0,
        )

        for cooldown_minutes in (0, -10):
            with self.subTest(cooldown_minutes=cooldown_minutes):
                decision = self.evaluate(
                    three_losses(),
                    11 * MINUTE_MS,
                    cooldown_minutes=cooldown_minutes,
                )

                self.assertEqual(decision.status, "DISABLED")
                self.assertFalse(decision.blocked)
                self.assertTrue(decision.allow_progression)
                self.assertEqual(decision.reason, "画像退化守卫已关闭")

    def test_fewer_than_three_trailing_losses_remain_normal(self):
        decision = self.evaluate(three_losses()[:2], 11 * MINUTE_MS)

        self.assertEqual(decision.status, "NORMAL")
        self.assertFalse(decision.blocked)
        self.assertTrue(decision.allow_progression)
        self.assertEqual(decision.consecutive_losses, 2)

    def test_win_breaks_streak_and_equal_settlement_uses_id_boundary(self):
        history = [
            order(1, "LOSS", 2 * MINUTE_MS),
            order(2, "LOSS", 4 * MINUTE_MS),
            order(4, "LOSS", 6 * MINUTE_MS),
            order(3, "WIN", 6 * MINUTE_MS),
        ]

        decision = self.evaluate(history, 7 * MINUTE_MS)

        self.assertEqual(decision.status, "NORMAL")
        self.assertEqual(decision.consecutive_losses, 1)
        self.assertEqual(decision.last_loss_settled_at, 6 * MINUTE_MS)

    def test_other_scope_future_and_invalid_orders_do_not_affect_streak(self):
        history = [
            order(1, "LOSS", 2 * MINUTE_MS),
            order(2, "LOSS", 4 * MINUTE_MS),
            order(3, "LOSS", 5 * MINUTE_MS, profile="p2"),
            order(4, "LOSS", 5 * MINUTE_MS, version="v2"),
            order(5, "LOSS", 20 * MINUTE_MS),
            order(6, "LOSS", 5 * MINUTE_MS, status="OPEN"),
            order(7, "DRAW", 5 * MINUTE_MS),
            order(8, "LOSS", None),
        ]

        decision = self.evaluate(history, 10 * MINUTE_MS)

        self.assertEqual(decision.status, "NORMAL")
        self.assertEqual(decision.consecutive_losses, 2)

    def test_malformed_settled_order_numeric_fields_are_ignored(self):
        valid = three_losses()[:2]

        for field, value in (("settled_at", "invalid"), ("id", "invalid")):
            with self.subTest(field=field):
                malformed = order(3, "LOSS", 5 * MINUTE_MS)
                setattr(malformed, field, value)

                decision = self.evaluate([*valid, malformed], 10 * MINUTE_MS)

                self.assertEqual(decision.status, "NORMAL")
                self.assertEqual(decision.consecutive_losses, 2)

    def test_malformed_open_probe_numeric_fields_are_ignored(self):
        for field, value in (
            ("profile_degradation_triggered_at", "invalid"),
            ("opened_at", "invalid"),
        ):
            with self.subTest(field=field):
                malformed = order(
                    4,
                    None,
                    None,
                    status="OPEN",
                    probe=True,
                    triggered_at=10 * MINUTE_MS,
                    opened_at=70 * MINUTE_MS,
                )
                setattr(malformed, field, value)

                decision = self.evaluate(
                    [*three_losses(), malformed],
                    71 * MINUTE_MS,
                )

                self.assertEqual(decision.status, "RECOVERY_READY")
                self.assertEqual(decision.probe_order_id, 0)

    def test_probe_marker_requires_literal_boolean_true(self):
        for marker in (1, "false", "true"):
            with self.subTest(marker=marker):
                unmarked = order(
                    4,
                    None,
                    None,
                    status="OPEN",
                    probe=marker,
                    triggered_at=10 * MINUTE_MS,
                    opened_at=70 * MINUTE_MS,
                )

                decision = self.evaluate(
                    [*three_losses(), unmarked],
                    71 * MINUTE_MS,
                )

                self.assertEqual(decision.status, "RECOVERY_READY")
                self.assertEqual(decision.probe_order_id, 0)

    def test_open_probe_requires_matching_trigger_marker_and_scope(self):
        nonmatching = [
            order(
                4,
                None,
                None,
                status="OPEN",
                probe=True,
                triggered_at=9 * MINUTE_MS,
                opened_at=70 * MINUTE_MS,
            ),
            order(
                5,
                None,
                None,
                status="OPEN",
                probe=False,
                triggered_at=10 * MINUTE_MS,
                opened_at=70 * MINUTE_MS,
            ),
            order(
                6,
                None,
                None,
                status="OPEN",
                probe=True,
                triggered_at=10 * MINUTE_MS,
                opened_at=70 * MINUTE_MS,
                profile="p2",
            ),
            order(
                7,
                None,
                None,
                status="OPEN",
                probe=True,
                triggered_at=10 * MINUTE_MS,
                opened_at=70 * MINUTE_MS,
                version="v2",
            ),
        ]

        decision = self.evaluate(
            [*three_losses(), *nonmatching],
            71 * MINUTE_MS,
        )

        self.assertEqual(decision.status, "RECOVERY_READY")
        self.assertEqual(decision.probe_order_id, 0)

    def test_zero_settlement_timestamp_is_nonempty_and_counted(self):
        history = [
            order(1, "LOSS", 0),
            order(2, "LOSS", 1),
            order(3, "LOSS", 2),
        ]

        decision = self.evaluate(history, 3)

        self.assertEqual(decision.status, "COOLDOWN")
        self.assertEqual(decision.consecutive_losses, 3)
        self.assertEqual(decision.last_loss_settled_at, 2)

    def test_shuffled_input_rebuilds_identical_decision(self):
        history = [
            order(1, "WIN", 1 * MINUTE_MS),
            *three_losses(),
            order(9, "LOSS", 9 * MINUTE_MS, profile="p2"),
        ]

        ordered = self.evaluate(history, 11 * MINUTE_MS)
        shuffled = self.evaluate(
            [history[3], history[1], history[4], history[0], history[2]],
            11 * MINUTE_MS,
        )

        self.assertEqual(shuffled, ordered)

    def test_missing_profile_or_version_is_not_applicable(self):
        for profile, version in (("", "v1"), ("p1", ""), (None, "v1"), ("p1", None)):
            with self.subTest(profile=profile, version=version):
                decision = evaluate_profile_degradation_guard(
                    three_losses(),
                    current_time=11 * MINUTE_MS,
                    profile_key=profile,
                    daily_profile_version=version,
                )

                self.assertEqual(decision.status, "NOT_APPLICABLE")
                self.assertFalse(decision.blocked)
                self.assertTrue(decision.allow_progression)
                self.assertEqual(decision.reason, "画像键或每日画像版本不完整")


if __name__ == "__main__":
    unittest.main()
