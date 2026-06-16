import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.research_10m_strategy_coarse_replay import (
    Candidate,
    Kline,
    event_contract_pnl,
    load_klines_from_zips,
    settle_candidate,
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


class TenMinuteStrategyCoarseReplayTest(unittest.TestCase):
    def test_load_klines_from_zips_deduplicates_and_sorts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first = Path(temp_dir) / "first.zip"
            second = Path(temp_dir) / "second.zip"
            with zipfile.ZipFile(first, "w") as archive:
                archive.writestr(
                    "first.csv",
                    "120000,100,101,99,100,1,179999\n60000,100,101,99,99,1,119999\n",
                )
            with zipfile.ZipFile(second, "w") as archive:
                archive.writestr(
                    "second.csv",
                    "120000,100,101,99,101,1,179999\n180000,101,102,100,102,1,239999\n",
                )

            klines = load_klines_from_zips([first, second])

        self.assertEqual([item.open_time for item in klines], [60_000, 120_000, 180_000])
        self.assertEqual(klines[1].close, 101.0)

    def test_event_contract_pnl_uses_10u_18u_event_payout(self):
        self.assertEqual(event_contract_pnl("LONG", 100, 101), ("WIN", 8.0))
        self.assertEqual(event_contract_pnl("LONG", 100, 100), ("LOSS", -10.0))
        self.assertEqual(event_contract_pnl("SHORT", 100, 99), ("WIN", 8.0))
        self.assertEqual(event_contract_pnl("SHORT", 100, 100), ("LOSS", -10.0))

    def test_settle_candidate_uses_10_minute_horizon(self):
        klines = [kline(i, 100 + i) for i in range(20)]
        candidate = Candidate(
            strategy="synthetic",
            family="unit",
            direction="LONG",
            entry_index=5,
            entry_time=klines[5].close_time,
            entry_price=klines[5].close,
            params={"window": 5},
        )

        order = settle_candidate(candidate, klines)

        self.assertEqual(order["result"], "WIN")
        self.assertEqual(order["exit_time"], klines[15].close_time)
        self.assertEqual(order["pnl"], 8.0)

    def test_generates_momentum_candidates_on_synthetic_up_move(self):
        from scripts.research_10m_strategy_coarse_replay import generate_candidates

        klines = [kline(i, 100 + i * 0.1, volume=100) for i in range(150)]
        candidates = generate_candidates(klines)
        names = {item.strategy for item in candidates}

        self.assertIn("momentum_3m_long_0bps", names)
        self.assertIn("momentum_5m_long_0bps", names)

    def test_generates_reversal_long_after_drop_and_reclaim(self):
        from scripts.research_10m_strategy_coarse_replay import generate_candidates

        klines = [kline(i, 100.0, volume=100) for i in range(130)]
        for idx in range(130, 140):
            klines.append(kline(idx, 100 - (idx - 129) * 0.2, volume=180))
        klines.append(kline(140, 98.2, open_price=98.0, high=98.3, low=97.5, volume=220))
        klines.extend(kline(i, 98.2, volume=100) for i in range(141, 160))

        candidates = generate_candidates(klines)

        self.assertTrue(any(item.family == "reversal" and item.direction == "LONG" for item in candidates))

    def test_generates_failed_breakout_short_after_high_reclaim_failure(self):
        from scripts.research_10m_strategy_coarse_replay import generate_candidates

        klines = [kline(i, 100.0 + (i % 5) * 0.01, high=100.2, low=99.8, volume=100) for i in range(130)]
        klines.append(kline(130, 100.05, open_price=100.4, high=100.8, low=100.0, volume=220))
        klines.extend(kline(i, 100.0, volume=100) for i in range(131, 150))

        candidates = generate_candidates(klines)

        self.assertTrue(any(item.family == "failed_breakout" and item.direction == "SHORT" for item in candidates))

    def test_replay_applies_one_open_order_and_10_minute_gap(self):
        from scripts.research_10m_strategy_coarse_replay import replay_candidates

        klines = [kline(i, 100 + i) for i in range(40)]
        candidates = [
            Candidate("s1", "unit", "LONG", 5, klines[5].close_time, klines[5].close, {}),
            Candidate("s1", "unit", "LONG", 6, klines[6].close_time, klines[6].close, {}),
            Candidate("s1", "unit", "LONG", 16, klines[16].close_time, klines[16].close, {}),
        ]

        orders = replay_candidates(candidates, klines, enforce_cooldown=True)

        self.assertEqual(len(orders), 2)
        self.assertEqual([order["entry_time"] for order in orders], [klines[5].close_time, klines[16].close_time])

    def test_summarize_strategy_reports_risk_and_recent_windows(self):
        from scripts.research_10m_strategy_coarse_replay import summarize_strategy

        orders = [
            {
                "strategy": "s",
                "family": "unit",
                "direction": "LONG",
                "entry_time": 1714521600000,
                "result": "WIN",
                "pnl": 8.0,
                "params": {},
            },
            {
                "strategy": "s",
                "family": "unit",
                "direction": "LONG",
                "entry_time": 1714522200000,
                "result": "LOSS",
                "pnl": -10.0,
                "params": {},
            },
            {
                "strategy": "s",
                "family": "unit",
                "direction": "LONG",
                "entry_time": 1714522800000,
                "result": "LOSS",
                "pnl": -10.0,
                "params": {},
            },
        ]

        summary = summarize_strategy("s", orders)

        self.assertEqual(summary["orders"], 3)
        self.assertEqual(summary["wins"], 1)
        self.assertEqual(summary["max_loss_streak"], 2)
        self.assertLess(summary["max_drawdown"], 0)
        self.assertIn("by_month", summary)
        self.assertIn("recent_6m", summary)
        self.assertIn("recent_3m", summary)


if __name__ == "__main__":
    unittest.main()
