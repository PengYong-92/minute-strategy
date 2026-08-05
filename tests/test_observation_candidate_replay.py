import unittest

from app.models import Kline
from scripts.replay_observation_candidates import (
    RollingGateConfig,
    SegmentSelectionConfig,
    generate_observation_candidates,
    summarize_orders,
    walk_forward_segment_selection_replay,
    walk_forward_replay,
)


def kline(idx, close, open_price=None, high=None, low=None, volume=100):
    open_price = close if open_price is None else open_price
    high = max(open_price, close) if high is None else high
    low = min(open_price, close) if low is None else low
    return Kline(
        open_time=idx * 60_000,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        close_time=idx * 60_000 + 59_999,
    )


class ObservationCandidateReplayTest(unittest.TestCase):
    def test_summarize_orders_uses_event_contract_payoff(self):
        stats = summarize_orders(
            [
                {"entry_time": 1, "result": "WIN", "pnl": 8.0},
                {"entry_time": 2, "result": "LOSS", "pnl": -10.0},
            ]
        )

        self.assertEqual(stats["orders"], 2)
        self.assertEqual(stats["win_rate"], 0.5)
        self.assertEqual(stats["pnl"], -2.0)
        self.assertEqual(stats["break_even_win_rate"], 0.555556)

    def test_walk_forward_replay_uses_prior_history_only(self):
        orders = []
        for idx, result in enumerate(["WIN", "WIN", "LOSS"], start=1):
            orders.append(
                {
                    "strategy_tag": "s",
                    "direction": "LONG",
                    "threshold_segment": "WD-00",
                    "entry_time": idx * 60_000,
                    "result": result,
                    "pnl": 8.0 if result == "WIN" else -10.0,
                }
            )

        replay = walk_forward_replay(
            orders,
            RollingGateConfig(lookback_days=7, min_samples=2, min_win_rate=1.0, min_ev=8.0),
        )

        self.assertEqual(replay["traded"]["orders"], 1)
        self.assertEqual(replay["traded"]["pnl"], -10.0)
        self.assertEqual(replay["rejected"]["not_enough_samples"], 2)

    def test_walk_forward_segment_selection_trains_from_prior_period_only(self):
        orders = []
        for idx, result in enumerate(["WIN", "WIN", "LOSS", "LOSS"], start=1):
            orders.append(
                {
                    "strategy_tag": "s",
                    "direction": "LONG",
                    "threshold_segment": "WD-00",
                    "entry_time": idx * 86_400_000,
                    "result": result,
                    "pnl": 8.0 if result == "WIN" else -10.0,
                }
            )

        replay = walk_forward_segment_selection_replay(
            orders,
            SegmentSelectionConfig(
                lookback_days=2,
                update_days=1,
                min_samples=2,
                min_win_rate=1.0,
                min_ev=8.0,
                key_mode="tag_segment",
            ),
        )

        self.assertEqual(replay["traded"]["orders"], 1)
        self.assertEqual(replay["traded"]["pnl"], -10.0)
        self.assertGreaterEqual(replay["schedule_stats"]["updates"], 1)

    def test_generate_failed_low_candidate_on_synthetic_reclaim(self):
        klines = [kline(i, 100.0, high=101.0, low=98.9) for i in range(150)]
        for idx in range(150, 160):
            klines.append(kline(idx, 98.6, high=101.2, low=98.2))
        klines[-1] = kline(159, 98.90, open_price=98.4, high=99.0, low=97.8)
        klines.extend(kline(i, 100.0) for i in range(160, 180))

        candidates = generate_observation_candidates(klines)

        self.assertTrue(any(item["strategy_tag"] == "failed_low_120m_long_observe" for item in candidates))


if __name__ == "__main__":
    unittest.main()
