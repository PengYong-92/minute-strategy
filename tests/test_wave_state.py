import unittest

from app.models import Kline
from app.wave_state import WaveSnapshot, analyze_wave, rebuild_wave


def bars(closes: list[float], *, start: int = 0, wick: float = 0.2) -> list[Kline]:
    result = []
    for index, close in enumerate(closes):
        previous = closes[index - 1] if index else close
        open_price = previous
        result.append(
            Kline(
                open_time=start + index * 60_000,
                open=open_price,
                high=max(open_price, close) + wick,
                low=min(open_price, close) - wick,
                close=close,
                volume=100.0,
                close_time=start + (index + 1) * 60_000,
            )
        )
    return result


class WaveStateTest(unittest.TestCase):
    def test_up_leg_requires_two_closed_minute_confirmations(self):
        first = analyze_wave(bars([100.0] * 12 + [100.2, 100.5, 100.9, 101.3, 101.8, 102.3, 102.9, 103.5]))

        self.assertEqual(first.raw_state, "UP_LEG")
        self.assertEqual(first.state, "TURN_UP")
        self.assertEqual(first.confirmations, 1)
        self.assertEqual(first.allowed_directions, ())

        second = analyze_wave(
            bars([100.0] * 12 + [100.2, 100.5, 100.9, 101.3, 101.8, 102.3, 102.9, 103.5, 104.1]),
            previous=first,
        )

        self.assertEqual(second.state, "UP_LEG")
        self.assertEqual(second.confirmations, 2)
        self.assertEqual(second.allowed_directions, ("LONG",))
        self.assertGreater(second.confirmed_at, 0)

    def test_down_leg_allows_only_short_after_confirmation(self):
        first = analyze_wave(bars([105.0] * 12 + [104.8, 104.4, 104.0, 103.5, 103.0, 102.4, 101.8, 101.1]))
        second = analyze_wave(
            bars([105.0] * 12 + [104.8, 104.4, 104.0, 103.5, 103.0, 102.4, 101.8, 101.1, 100.5]),
            previous=first,
        )

        self.assertEqual(second.raw_state, "DOWN_LEG")
        self.assertEqual(second.state, "DOWN_LEG")
        self.assertEqual(second.allowed_directions, ("SHORT",))

    def test_range_location_maps_high_to_short_low_to_long_and_middle_to_wait(self):
        high = analyze_wave(bars([100.0] * 12 + [99.0, 101.0, 99.2, 101.1, 99.4, 101.2, 100.0, 101.0]))
        low = analyze_wave(bars([100.0] * 12 + [99.0, 101.0, 99.2, 101.1, 99.4, 101.2, 100.0, 99.1]))
        middle = analyze_wave(bars([100.0] * 12 + [99.0, 101.0, 99.2, 101.1, 99.4, 101.2, 99.8, 100.1]))

        self.assertEqual((high.state, high.allowed_directions), ("RANGE_HIGH", ("SHORT",)))
        self.assertEqual((low.state, low.allowed_directions), ("RANGE_LOW", ("LONG",)))
        self.assertEqual((middle.state, middle.allowed_directions), ("RANGE_MID", ()))

    def test_small_move_is_range_even_when_closes_are_monotonic(self):
        snapshot = analyze_wave(
            bars([100.0] * 12 + [100.00, 100.02, 100.04, 100.06, 100.08, 100.10, 100.12, 100.14], wick=0.5)
        )

        self.assertNotIn(snapshot.raw_state, {"UP_LEG", "DOWN_LEG"})
        self.assertLess(snapshot.atr_strength, 0.5)

    def test_direction_change_stays_blocked_until_second_confirmation(self):
        confirmed_up = WaveSnapshot(
            state="UP_LEG",
            raw_state="UP_LEG",
            window=8,
            efficiency=0.9,
            direction_ratio=1.0,
            atr_strength=3.0,
            range_position=0.9,
            confirmations=2,
            confirmed_at=1_000,
            allowed_directions=("LONG",),
        )
        falling = bars([105.0] * 12 + [104.8, 104.4, 104.0, 103.5, 103.0, 102.4, 101.8, 101.1])

        first = analyze_wave(falling, previous=confirmed_up)
        second = analyze_wave(falling + bars([100.5], start=len(falling) * 60_000), previous=first)

        self.assertEqual((first.state, first.confirmations), ("TURN_DOWN", 1))
        self.assertEqual(first.allowed_directions, ())
        self.assertEqual((second.state, second.confirmations), ("DOWN_LEG", 2))
        self.assertEqual(second.allowed_directions, ("SHORT",))

    def test_insufficient_history_is_unknown(self):
        snapshot = analyze_wave(bars([100.0] * 10))

        self.assertEqual(snapshot.state, "UNKNOWN")
        self.assertEqual(snapshot.allowed_directions, ())

    def test_future_bars_do_not_change_historical_snapshot(self):
        history = bars([100.0] * 12 + [100.2, 100.5, 100.9, 101.3, 101.8, 102.3, 102.9, 103.5])
        before = analyze_wave(history)
        _ = analyze_wave(history + bars([90.0, 89.0], start=len(history) * 60_000), previous=before)
        after = analyze_wave(history)

        self.assertEqual(before, after)

    def test_rebuild_matches_incremental_wave_anchor_after_restart(self):
        history = bars([100.0] * 12 + [100.2, 100.5, 100.9, 101.3, 101.8, 102.3, 102.9, 103.5, 104.1])
        incremental = analyze_wave(())
        for end in range(15, len(history) + 1):
            incremental = analyze_wave(history[:end], previous=incremental)

        rebuilt = rebuild_wave(history)

        self.assertEqual(rebuilt, incremental)
        self.assertEqual(rebuilt.state, "UP_LEG")
        self.assertEqual(rebuilt.confirmed_at, incremental.confirmed_at)


if __name__ == "__main__":
    unittest.main()
