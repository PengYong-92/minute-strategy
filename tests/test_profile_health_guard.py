import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.daily_profile_selector import profile_key
from app.models import ObservationSignal
from app.profile_health_guard import (
    ProfileHealthGuardConfig,
    evaluate_profile_health_guard,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
MINUTE_MS = 60_000


def timestamp_ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=SHANGHAI).timestamp() * 1000)


def selected_profile(direction: str, segment: str) -> dict:
    family = f"{direction.lower()}_observe"
    tag = f"generic_{direction.lower()}_observe"
    return {
        "key": profile_key(10, family, tag, direction, segment),
        "direction": direction,
        "threshold_segment": segment,
    }


def observation(
    opened_at: int,
    *,
    direction: str = "LONG",
    segment: str = "WD-00",
    result: str = "WIN",
    settled: bool = True,
) -> ObservationSignal:
    family = f"{direction.lower()}_observe"
    tag = f"generic_{direction.lower()}_observe"
    expires_at = opened_at + 10 * MINUTE_MS
    return ObservationSignal(
        observation_key=f"{opened_at}|10|{direction}|{tag}",
        strategy_family=family,
        strategy_tag=tag,
        direction=direction,
        timeframe_minutes=10,
        level="B",
        reason="test",
        entry_price=100.0,
        opened_at=opened_at,
        expires_at=expires_at,
        threshold_segment=segment,
        status="SETTLED" if settled else "OPEN",
        result=result if settled else None,
        settled_at=expires_at if settled else None,
        pnl=8.0 if result == "WIN" else -10.0,
    )


def result_rows(
    evaluation_at: int,
    results: str,
    *,
    direction: str = "LONG",
    segment: str = "WD-00",
) -> list[ObservationSignal]:
    start = evaluation_at - len(results) * 11 * MINUTE_MS
    return [
        observation(
            start + index * 11 * MINUTE_MS,
            direction=direction,
            segment=segment,
            result="WIN" if result == "W" else "LOSS",
        )
        for index, result in enumerate(results)
    ]


class ProfileHealthGuardTests(unittest.TestCase):
    def setUp(self):
        self.current_time = timestamp_ms("2026-08-15 10:30:00")
        self.evaluation_at = timestamp_ms("2026-08-15 08:00:00")
        self.selected = [selected_profile("LONG", "WD-00")]

    def evaluate(self, rows, *, direction="LONG", selected=None, enabled=True):
        return evaluate_profile_health_guard(
            rows,
            current_time=self.current_time,
            direction=direction,
            selected_profiles=self.selected if selected is None else selected,
            config=ProfileHealthGuardConfig(enabled=enabled),
        )

    def test_uses_latest_four_hour_boundary_and_previous_24_hours(self):
        rows = result_rows(self.evaluation_at, "WWWWWWLLLLLL")
        rows.extend(
            [
                observation(timestamp_ms("2026-08-14 07:30:00"), result="LOSS"),
                observation(timestamp_ms("2026-08-15 08:01:00"), result="LOSS"),
                observation(
                    timestamp_ms("2026-08-15 07:40:00"),
                    result="LOSS",
                    settled=False,
                ),
            ]
        )

        decision = self.evaluate(rows)

        self.assertEqual(decision.evaluated_at, self.evaluation_at)
        self.assertEqual(decision.next_evaluation_at, timestamp_ms("2026-08-15 12:00:00"))
        self.assertEqual(decision.lookback_start, timestamp_ms("2026-08-14 08:00:00"))
        self.assertEqual(decision.sample_size, 12)
        self.assertEqual(decision.wins, 6)
        self.assertEqual(decision.losses, 6)

    def test_requires_twelve_independent_samples_per_exact_profile(self):
        rows = result_rows(self.evaluation_at, "WWWWWWWWWWW")
        overlapping = observation(rows[-1].opened_at + MINUTE_MS, result="WIN")
        rows.append(overlapping)

        decision = self.evaluate(rows)

        self.assertEqual(decision.status, "WARMUP")
        self.assertEqual(decision.sample_size, 11)
        self.assertFalse(decision.blocked)

    def test_healthy_direction_preserves_second_order_and_progression(self):
        decision = self.evaluate(result_rows(self.evaluation_at, "WWWWWWWLLLLL"))

        self.assertEqual(decision.status, "HEALTHY")
        self.assertAlmostEqual(decision.win_rate, 7 / 12, places=6)
        self.assertEqual(decision.ev, 0.5)
        self.assertFalse(decision.blocked)
        self.assertTrue(decision.allow_second_order)
        self.assertTrue(decision.allow_progression)

    def test_watch_direction_allows_only_base_first_order(self):
        decision = self.evaluate(result_rows(self.evaluation_at, "WWWWWWLLLLLL"))

        self.assertEqual(decision.status, "WATCH")
        self.assertEqual(decision.win_rate, 0.5)
        self.assertEqual(decision.ev, -1.0)
        self.assertFalse(decision.blocked)
        self.assertFalse(decision.allow_second_order)
        self.assertFalse(decision.allow_progression)

    def test_degraded_direction_is_blocked_until_next_evaluation(self):
        decision = self.evaluate(result_rows(self.evaluation_at, "WWWWWLLLLLLL"))

        self.assertEqual(decision.status, "DEGRADED")
        self.assertTrue(decision.blocked)
        self.assertFalse(decision.allow_second_order)
        self.assertFalse(decision.allow_progression)
        self.assertEqual(decision.next_evaluation_at, timestamp_ms("2026-08-15 12:00:00"))

    def test_directions_and_selected_profile_keys_are_isolated(self):
        selected = [
            selected_profile("LONG", "WD-00"),
            selected_profile("SHORT", "WD-01"),
        ]
        rows = result_rows(self.evaluation_at, "WWWWWLLLLLLL")
        rows.extend(
            result_rows(
                self.evaluation_at,
                "WWWWWWWWLLLL",
                direction="SHORT",
                segment="WD-01",
            )
        )
        rows.extend(
            result_rows(
                self.evaluation_at,
                "LLLLLLLLLLLL",
                direction="LONG",
                segment="WD-02",
            )
        )

        long_decision = self.evaluate(rows, selected=selected)
        short_decision = self.evaluate(rows, direction="SHORT", selected=selected)

        self.assertEqual(long_decision.status, "DEGRADED")
        self.assertEqual(long_decision.sample_size, 12)
        self.assertEqual(short_decision.status, "HEALTHY")
        self.assertEqual(short_decision.sample_size, 12)

    def test_disabled_or_unselected_direction_is_not_applicable(self):
        rows = result_rows(self.evaluation_at, "LLLLLLLLLLLL")

        disabled = self.evaluate(rows, enabled=False)
        unselected = self.evaluate(rows, direction="SHORT")

        self.assertEqual(disabled.status, "DISABLED")
        self.assertFalse(disabled.blocked)
        self.assertEqual(unselected.status, "NOT_APPLICABLE")
        self.assertFalse(unselected.blocked)


if __name__ == "__main__":
    unittest.main()
