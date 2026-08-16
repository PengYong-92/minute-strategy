import unittest

from app.direction_pulse_shadow import (
    DIRECTION_PULSE_SHADOW_VERSION,
    attach_candidate_shadow,
    evaluate_direction_pulse_shadow,
)
from app.models import ObservationSignal


MINUTE_MS = 60_000


def observation(
    index: int,
    result: str,
    *,
    direction: str = "SHORT",
    opened_at: int | None = None,
    settled_at: int | None = None,
) -> ObservationSignal:
    opened = index * 11 * MINUTE_MS if opened_at is None else opened_at
    expires = opened + 10 * MINUTE_MS
    return ObservationSignal(
        observation_key=f"{direction}|{index}|{opened}",
        strategy_family=f"{direction.lower()}_observe",
        strategy_tag=f"generic_{direction.lower()}_observe",
        direction=direction,
        timeframe_minutes=10,
        level="B",
        reason="test",
        entry_price=100.0,
        opened_at=opened,
        expires_at=expires,
        status="SETTLED",
        result=result,
        exit_price=101.0 if result == "WIN" else 99.0,
        settled_at=expires if settled_at is None else settled_at,
        pnl=8.0 if result == "WIN" else -10.0,
    )


def rows(results: str, *, direction: str = "SHORT") -> list[ObservationSignal]:
    return [
        observation(index, "WIN" if result == "W" else "LOSS", direction=direction)
        for index, result in enumerate(results)
    ]


class DirectionPulseShadowTests(unittest.TestCase):
    def evaluate(self, samples, current_time=None):
        cutoff = current_time
        if cutoff is None:
            cutoff = max(item.settled_at or 0 for item in samples) + MINUTE_MS
        return evaluate_direction_pulse_shadow(samples, current_time=cutoff)

    def test_reports_n12_and_n16_status_boundaries_by_direction(self):
        short = rows("WWWWWWLLLLLLWWWW")
        long = rows("WWLLWWWWWLLLLLLL", direction="LONG")

        snapshot = self.evaluate([*short, *long])

        self.assertEqual(snapshot["version"], DIRECTION_PULSE_SHADOW_VERSION)
        self.assertEqual(snapshot["mode"], "SHADOW_ONLY")
        self.assertEqual(snapshot["refresh_mode"], "SETTLEMENT_EVENT")
        self.assertEqual(snapshot["directions"]["SHORT"]["12"]["status"], "NORMAL")
        self.assertEqual(snapshot["directions"]["SHORT"]["16"]["status"], "NORMAL")
        self.assertEqual(snapshot["directions"]["LONG"]["12"]["status"], "WATCH")
        self.assertEqual(snapshot["directions"]["LONG"]["16"]["status"], "WATCH")
        self.assertEqual(snapshot["directions"]["LONG"]["12"]["hypothetical_action"], "BLOCK_SECOND")

    def test_degraded_and_warmup_boundaries_are_explicit(self):
        samples = rows("WWWWLLLLLLLL")

        snapshot = self.evaluate(samples)

        self.assertEqual(snapshot["directions"]["SHORT"]["12"]["status"], "DEGRADED")
        self.assertEqual(
            snapshot["directions"]["SHORT"]["12"]["hypothetical_action"],
            "BLOCK_DIRECTION",
        )
        self.assertEqual(snapshot["directions"]["SHORT"]["16"]["status"], "WARMUP")
        self.assertEqual(snapshot["directions"]["LONG"]["12"]["sample_size"], 0)

    def test_uses_only_settled_samples_available_at_evaluation_time(self):
        samples = rows("WWWWLLLLLLLL")
        future = observation(
            99,
            "WIN",
            opened_at=samples[-1].expires_at + MINUTE_MS,
            settled_at=samples[-1].expires_at + 20 * MINUTE_MS,
        )
        cutoff = samples[-1].expires_at + 10 * MINUTE_MS

        snapshot = self.evaluate([*samples, future], current_time=cutoff)

        window = snapshot["directions"]["SHORT"]["12"]
        self.assertEqual(window["sample_size"], 12)
        self.assertEqual(window["wins"], 4)
        self.assertLess(window["last_settled_at"], future.settled_at)

    def test_removes_overlapping_samples_across_profiles_at_direction_level(self):
        independent = rows("WWWWWWLLLLLL")
        overlaps = [
            observation(
                100 + index,
                "LOSS",
                opened_at=item.opened_at + MINUTE_MS,
                settled_at=item.expires_at + MINUTE_MS,
            )
            for index, item in enumerate(independent)
        ]

        snapshot = self.evaluate([*independent, *overlaps])

        window = snapshot["directions"]["SHORT"]["12"]
        self.assertEqual(window["sample_size"], 12)
        self.assertEqual(window["wins"], 6)
        self.assertEqual(window["losses"], 6)

    def test_candidate_context_records_slot_specific_hypothetical_blocks(self):
        snapshot = self.evaluate(rows("WWWWWLLLLLLLWWWW"))

        first = attach_candidate_shadow(snapshot, direction="SHORT", order_slot="FIRST")
        second = attach_candidate_shadow(snapshot, direction="SHORT", order_slot="SECOND")

        self.assertFalse(first["windows"]["12"]["would_block"])
        self.assertTrue(second["windows"]["12"]["would_block"])
        self.assertEqual(second["windows"]["12"]["status"], "WATCH")
        self.assertEqual(second["order_slot"], "SECOND")


if __name__ == "__main__":
    unittest.main()
