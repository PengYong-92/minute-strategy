import unittest

from scripts.replay_market_sequence import (
    apply_exposure_limit,
    select_parameter_from_training,
    split_train_holdout_days,
    summarize_trades,
)


def candidate(index: int, result: str = "WIN") -> dict:
    entry_time = index * 120_000
    return {
        "entry_time": entry_time,
        "expires_at": entry_time + 600_000,
        "settled_at": entry_time + 600_000,
        "direction": "LONG",
        "result": result,
        "pnl": 8.0 if result == "WIN" else -10.0,
        "state_key": "DOWN|2",
        "day": "2026-08-01",
    }


class MarketSequenceReplayTest(unittest.TestCase):
    def test_exposure_limit_supports_one_two_and_five_overlapping_orders(self):
        candidates = [candidate(index) for index in range(10)]

        one = apply_exposure_limit(candidates, max_open_orders=1, min_gap_minutes=2)
        two = apply_exposure_limit(candidates, max_open_orders=2, min_gap_minutes=2)
        five = apply_exposure_limit(candidates, max_open_orders=5, min_gap_minutes=2)

        self.assertEqual(len(one), 2)
        self.assertEqual(len(two), 4)
        self.assertEqual(len(five), 10)

    def test_holdout_dates_are_not_used_to_select_parameters(self):
        results = [
            {
                "name": "training-winner",
                "by_day": {
                    "2026-07-30": [candidate(0, "WIN"), candidate(1, "WIN")],
                    "2026-08-01": [candidate(2, "LOSS") for _ in range(20)],
                },
            },
            {
                "name": "holdout-winner",
                "by_day": {
                    "2026-07-30": [candidate(0, "WIN")],
                    "2026-08-01": [candidate(2, "WIN") for _ in range(20)],
                },
            },
        ]

        selected = select_parameter_from_training(results, train_days={"2026-07-30"})

        self.assertEqual(selected["name"], "training-winner")

    def test_time_split_reserves_latest_twenty_percent_of_days(self):
        days = [f"2026-07-{day:02d}" for day in range(1, 11)]

        train, holdout = split_train_holdout_days(days, holdout_ratio=0.20)

        self.assertEqual(train, days[:8])
        self.assertEqual(holdout, days[8:])

    def test_summary_uses_event_contract_payout_and_drawdown(self):
        trades = [candidate(0, "WIN"), candidate(1, "LOSS"), candidate(2, "LOSS")]

        summary = summarize_trades(trades)

        self.assertEqual(summary["orders"], 3)
        self.assertEqual(summary["wins"], 1)
        self.assertEqual(summary["pnl"], -12.0)
        self.assertEqual(summary["max_drawdown"], -20.0)


if __name__ == "__main__":
    unittest.main()
