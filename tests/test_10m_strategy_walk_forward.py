import unittest

from scripts.research_10m_strategy_coarse_replay import Candidate, Kline
from scripts.research_10m_strategy_walk_forward import (
    EdgeGateConfig,
    ObservedHistory,
    threshold_segment,
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


def candidate(idx, strategy="s", direction="LONG"):
    price = 100.0
    return Candidate(
        strategy=strategy,
        family="unit",
        direction=direction,
        entry_index=idx,
        entry_time=idx * 60_000 + 59_999,
        entry_price=price,
        params={"window": 3},
    )


class TenMinuteStrategyWalkForwardTest(unittest.TestCase):
    def test_threshold_segment_uses_utc_weekday_hour(self):
        self.assertEqual(threshold_segment(1714521600000), "WD-00")

    def test_observed_history_uses_only_prior_outcomes(self):
        history = ObservedHistory()
        config = EdgeGateConfig(lookback_days=7, min_samples=2, min_win_rate=0.6, min_ev=0.5)
        current = {"entry_time": 10 * 60_000, "strategy": "s", "segment": "WD-00"}

        self.assertFalse(history.allowed(current, config)["allowed"])
        history.add({"entry_time": 0, "strategy": "s", "segment": "WD-00", "result": "WIN", "pnl": 8.0})
        history.add({"entry_time": 60_000, "strategy": "s", "segment": "WD-00", "result": "WIN", "pnl": 8.0})

        gate = history.allowed(current, config)

        self.assertTrue(gate["allowed"])
        self.assertEqual(gate["sample_size"], 2)
        self.assertEqual(gate["wins"], 2)

    def test_walk_forward_blocks_when_prior_edge_is_degraded(self):
        klines = [kline(i, 100 - i) for i in range(40)]
        candidates = [candidate(5), candidate(16), candidate(27)]
        config = EdgeGateConfig(lookback_days=7, min_samples=2, min_win_rate=0.6, min_ev=0.5)

        result = walk_forward_replay(candidates, klines, config)

        self.assertEqual(result["observed"]["orders"], 3)
        self.assertEqual(result["traded"]["orders"], 2)
        self.assertEqual(result["rejected"]["edge_degraded"], 1)


if __name__ == "__main__":
    unittest.main()
