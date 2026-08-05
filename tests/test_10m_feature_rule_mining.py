import unittest

from scripts.mine_10m_feature_rules import summarize_trades, walk_forward_rule_guard


class TenMinuteFeatureRuleMiningTest(unittest.TestCase):
    def test_walk_forward_rule_guard_uses_prior_trades_only(self):
        trades = []
        for idx, result in enumerate(["WIN", "WIN", "LOSS"], start=1):
            trades.append(
                {
                    "entry_time": idx * 60_000,
                    "result": result,
                    "pnl": 8.0 if result == "WIN" else -10.0,
                }
            )

        replay = walk_forward_rule_guard(
            trades,
            start_ms=0,
            lookback_days=1,
            min_samples=2,
            min_win_rate=1.0,
            min_avg_pnl=8.0,
        )

        self.assertEqual(replay["traded"]["total_orders"], 1)
        self.assertEqual(replay["traded"]["balance"], -10.0)
        self.assertEqual(replay["rejected"]["not_enough_samples"], 2)

    def test_summarize_trades_keeps_event_contract_break_even(self):
        stats = summarize_trades(
            [
                {"entry_time": 1, "result": "WIN", "pnl": 8.0},
                {"entry_time": 2, "result": "LOSS", "pnl": -10.0},
            ]
        )

        self.assertEqual(stats["total_orders"], 2)
        self.assertEqual(stats["break_even_win_rate"], 0.5556)
        self.assertEqual(stats["balance"], -2.0)


if __name__ == "__main__":
    unittest.main()
