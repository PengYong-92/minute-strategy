import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.time_period_guard import TimePeriodGuardConfig, evaluate_time_period_guard


SHANGHAI = ZoneInfo("Asia/Shanghai")


def timestamp_ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=SHANGHAI).timestamp() * 1000)


class TimePeriodGuardTests(unittest.TestCase):
    def test_blocks_from_noon_until_six_pm_in_shanghai(self):
        config = TimePeriodGuardConfig(enabled=True)

        self.assertFalse(
            evaluate_time_period_guard(
                timestamp_ms("2026-08-14 11:59:59"), config
            ).blocked
        )
        self.assertTrue(
            evaluate_time_period_guard(
                timestamp_ms("2026-08-14 12:00:00"), config
            ).blocked
        )
        self.assertTrue(
            evaluate_time_period_guard(
                timestamp_ms("2026-08-14 17:59:59"), config
            ).blocked
        )
        self.assertFalse(
            evaluate_time_period_guard(
                timestamp_ms("2026-08-14 18:00:00"), config
            ).blocked
        )

    def test_disabled_guard_never_blocks(self):
        decision = evaluate_time_period_guard(
            timestamp_ms("2026-08-14 15:00:00"),
            TimePeriodGuardConfig(enabled=False),
        )

        self.assertFalse(decision.blocked)
        self.assertEqual(decision.code, "DISABLED")


if __name__ == "__main__":
    unittest.main()
