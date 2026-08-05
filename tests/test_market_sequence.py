import unittest

from app.market_sequence import (
    MarketSequenceConfig,
    SequenceTrainingRow,
    build_daily_snapshot,
    build_state_feature_series,
    build_snapshot_from_rows,
    build_training_rows,
    decide_current_state,
    run_bucket,
    state_features_at_index,
)
from app.models import Kline


def minute_kline(index: int, close: float, volume: float = 100.0) -> Kline:
    return Kline(
        open_time=index * 60_000,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=volume,
        close_time=(index + 1) * 60_000 - 1,
    )


class MarketSequenceTest(unittest.TestCase):
    def test_run_bucket_keeps_non_monotonic_sequence_states_separate(self):
        self.assertEqual(run_bucket(1), "1")
        self.assertEqual(run_bucket(2), "2")
        self.assertEqual(run_bucket(3), "3-4")
        self.assertEqual(run_bucket(4), "3-4")
        self.assertEqual(run_bucket(5), "5+")
        self.assertEqual(run_bucket(20), "5+")

    def test_training_rows_are_non_overlapping_and_settled_before_cutoff(self):
        klines = [minute_kline(index, 100.0 + index) for index in range(80)]
        config = MarketSequenceConfig(
            lookback_days=1,
            training_stride_minutes=10,
            run_step_minutes=2,
            min_samples=1,
            min_win_rate=0.5,
        )

        rows = build_training_rows(
            klines,
            lookback_start=0,
            lookback_end=70 * 60_000,
            config=config,
        )

        self.assertGreater(len(rows), 1)
        self.assertTrue(all(row.entry_time % (10 * 60_000) == 10 * 60_000 - 1 for row in rows))
        self.assertTrue(all(row.settled_at < 70 * 60_000 for row in rows))
        self.assertTrue(all(right.entry_time - left.entry_time >= 10 * 60_000 for left, right in zip(rows, rows[1:])))

    def test_snapshot_selects_majority_direction_and_rejects_tie(self):
        rows = [
            SequenceTrainingRow(
                entry_time=index * 600_000,
                settled_at=index * 600_000 + 600_000,
                state_key="DOWN|2",
                outcome="UP" if index < 6 else "DOWN",
            )
            for index in range(10)
        ]
        tied = [
            SequenceTrainingRow(
                entry_time=(index + 10) * 600_000,
                settled_at=(index + 11) * 600_000,
                state_key="UP|1",
                outcome="UP" if index < 2 else "DOWN",
            )
            for index in range(4)
        ]
        config = MarketSequenceConfig(min_samples=4, min_win_rate=0.60, min_ev=0.0)

        snapshot = build_snapshot_from_rows(
            [*rows, *tied],
            evaluated_at=100_000_000,
            effective_from=100_000_000,
            effective_until=200_000_000,
            config=config,
        )

        self.assertEqual(snapshot["selected_states"]["DOWN|2"]["direction"], "LONG")
        self.assertEqual(snapshot["selected_states"]["DOWN|2"]["sample_size"], 10)
        self.assertAlmostEqual(snapshot["selected_states"]["DOWN|2"]["win_rate"], 0.6)
        self.assertNotIn("UP|1", snapshot["selected_states"])
        self.assertEqual(snapshot["states"]["UP|1"]["selection_state"], "TIE")

    def test_daily_snapshot_excludes_outcome_settled_at_cutoff(self):
        klines = [minute_kline(index, 100.0 + index) for index in range(180)]
        config = MarketSequenceConfig(
            lookback_days=1,
            training_stride_minutes=10,
            min_samples=1,
            min_win_rate=0.5,
            evaluation_hour=0,
            evaluation_minute=0,
            activation_hour=0,
            activation_minute=10,
        )

        snapshot = build_daily_snapshot(klines, 180 * 60_000, config=config)

        self.assertTrue(all(item["latest_settled_at"] < snapshot["lookback_end"] for item in snapshot["states"].values()))

    def test_current_decision_requires_two_minute_alignment_and_selected_state(self):
        closes = [100.0] * 12 + [99.0, 98.0, 97.0, 96.0, 95.0, 94.0, 93.0, 92.0, 91.0, 90.0]
        klines = [minute_kline(index, close) for index, close in enumerate(closes)]
        config = MarketSequenceConfig(entry_stride_minutes=2, key_mode="move_run")
        aligned_time = klines[-1].close_time
        probe = decide_current_state(klines, {}, current_time=aligned_time, config=config)
        selected = {
            probe["state_key"]: {
                "direction": "LONG",
                "sample_size": 20,
                "win_rate": 0.65,
                "ev": 1.7,
            }
        }

        opened = decide_current_state(klines, selected, current_time=aligned_time, config=config)
        unaligned = decide_current_state(klines[:-1], selected, current_time=klines[-2].close_time, config=config)

        self.assertEqual(opened["direction"], "LONG")
        self.assertTrue(opened["selected"])
        self.assertEqual(unaligned["direction"], "WAIT")
        self.assertEqual(unaligned["reason"], "ENTRY_NOT_ALIGNED")

    def test_sorted_index_feature_api_matches_current_decision_state(self):
        closes = [100.0 + index for index in range(30)]
        klines = [minute_kline(index, close) for index, close in enumerate(closes)]
        config = MarketSequenceConfig(key_mode="move_run_volume_rsi")

        feature = state_features_at_index(klines, len(klines) - 1, config=config)
        decision = decide_current_state(
            klines,
            {},
            current_time=klines[-1].close_time,
            config=config,
        )

        self.assertEqual(feature["state_key"], decision["state_key"])
        self.assertEqual(feature["move"], "UP")

    def test_batch_feature_series_matches_index_calculation(self):
        klines = [
            minute_kline(index, 100.0 + ((index % 9) - 4) * 0.3 + index * 0.02, 80.0 + index % 17)
            for index in range(320)
        ]
        config = MarketSequenceConfig(key_mode="move_run_volume_rsi")

        series = build_state_feature_series(klines, config=config)

        for index in (20, 40, 100, 319):
            expected = state_features_at_index(klines, index, config=config)
            self.assertEqual(series[index], expected)


if __name__ == "__main__":
    unittest.main()
