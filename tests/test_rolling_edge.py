import unittest

from app.rolling_edge import RollingEdgeConfig, rolling_edge_snapshot, setup_key, should_degrade


def order(idx, result, pnl, segment="WD-12", timeframe=10, reason="放量急跌反抽：动态评分偏多"):
    return {
        "entry_time": idx * 60_000,
        "timeframe_minutes": timeframe,
        "threshold_segment": segment,
        "reason": reason,
        "result": result,
        "pnl": pnl,
    }


class RollingEdgeTest(unittest.TestCase):
    def test_default_guard_config_uses_backtested_parameters(self):
        config = RollingEdgeConfig()

        self.assertEqual(config.lookback_days, 60)
        self.assertEqual(config.min_samples, 5)
        self.assertEqual(config.min_win_rate, 0.62)
        self.assertEqual(config.min_ev, 0.5)

    def test_setup_key_includes_timeframe_segment_and_setup_name(self):
        self.assertEqual(setup_key(order(1, "WIN", 8.0)), "10|WD-12|放量急跌反抽")

    def test_snapshot_uses_only_prior_orders_in_lookback_window(self):
        config = RollingEdgeConfig(lookback_days=1, min_samples=2, min_win_rate=0.5556, min_ev=0.0)
        current = order(100, "WIN", 8.0)
        orders = [
            order(-2000, "LOSS", -10.0),
            order(90, "LOSS", -10.0),
            order(95, "WIN", 8.0),
            order(100, "LOSS", -10.0),
            order(101, "LOSS", -10.0),
            order(96, "LOSS", -10.0, segment="WD-13"),
        ]

        snapshot = rolling_edge_snapshot(orders, current, config)

        self.assertEqual(snapshot.sample_size, 2)
        self.assertEqual(snapshot.wins, 1)
        self.assertEqual(snapshot.losses, 1)
        self.assertEqual(snapshot.pnl, -2.0)

    def test_degrades_when_prior_same_setup_fails_break_even(self):
        config = RollingEdgeConfig(lookback_days=1, min_samples=3, min_win_rate=0.5556, min_ev=0.0)
        current = order(100, "WIN", 8.0)
        orders = [
            order(90, "LOSS", -10.0),
            order(91, "LOSS", -10.0),
            order(92, "WIN", 8.0),
        ]

        snapshot = rolling_edge_snapshot(orders, current, config)

        self.assertTrue(should_degrade(snapshot, config))
        self.assertEqual(snapshot.win_rate, 0.3333)
        self.assertEqual(snapshot.ev, -4.0)

    def test_does_not_degrade_before_minimum_sample_size(self):
        config = RollingEdgeConfig(lookback_days=1, min_samples=4, min_win_rate=0.5556, min_ev=0.0)
        current = order(100, "WIN", 8.0)
        orders = [
            order(90, "LOSS", -10.0),
            order(91, "LOSS", -10.0),
            order(92, "WIN", 8.0),
        ]

        snapshot = rolling_edge_snapshot(orders, current, config)

        self.assertFalse(should_degrade(snapshot, config))


if __name__ == "__main__":
    unittest.main()
