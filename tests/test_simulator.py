import unittest

from app.models import Signal
from app.simulator import AccountSimulator


def signal(direction="LONG", timeframe_minutes=10):
    return Signal(
        direction=direction,
        timeframe_minutes=timeframe_minutes,
        level="A",
        reason="test",
        price=100.0,
        open_time=0,
        threshold_segment="WD-12",
        session_allowed=True,
        session_sample_size=37,
        session_win_rate=0.6757,
        session_ev=2.1622,
        session_edge_min=10.0,
    )


class SimulatorTest(unittest.TestCase):
    def test_long_order_wins_when_expiry_price_is_higher(self):
        simulator = AccountSimulator()
        order = simulator.open_order(signal("LONG"), entry_price=100.0, opened_at=0)

        simulator.settle_expired_orders(current_time=10 * 60_000, current_price=100.1)

        self.assertEqual(order.status, "SETTLED")
        self.assertEqual(order.result, "WIN")
        self.assertEqual(order.exit_price, 100.1)
        self.assertEqual(order.pnl, 8.0)
        self.assertEqual(simulator.balance, 8.0)
        self.assertEqual(order.threshold_segment, "WD-12")
        self.assertEqual(order.session_win_rate, 0.6757)

    def test_long_order_loses_when_expiry_price_is_not_higher(self):
        simulator = AccountSimulator()
        order = simulator.open_order(signal("LONG"), entry_price=100.0, opened_at=0)

        simulator.settle_expired_orders(current_time=10 * 60_000, current_price=100.0)

        self.assertEqual(order.result, "LOSS")
        self.assertEqual(order.pnl, -10.0)
        self.assertEqual(simulator.balance, -10.0)

    def test_short_order_wins_when_expiry_price_is_lower(self):
        simulator = AccountSimulator()
        order = simulator.open_order(signal("SHORT"), entry_price=100.0, opened_at=0)

        simulator.settle_expired_orders(current_time=10 * 60_000, current_price=99.9)

        self.assertEqual(order.result, "WIN")
        self.assertEqual(order.pnl, 8.0)
        self.assertEqual(simulator.balance, 8.0)

    def test_short_order_loses_when_expiry_price_is_not_lower(self):
        simulator = AccountSimulator()
        order = simulator.open_order(signal("SHORT"), entry_price=100.0, opened_at=0)

        simulator.settle_expired_orders(current_time=10 * 60_000, current_price=100.0)

        self.assertEqual(order.result, "LOSS")
        self.assertEqual(order.pnl, -10.0)
        self.assertEqual(simulator.balance, -10.0)

    def test_three_order_stake_progression_uses_prior_win_return_and_resets(self):
        simulator = AccountSimulator(enable_stake_progression=True, stake_progression_max_orders=3)
        first = simulator.open_order(signal("LONG", timeframe_minutes=1), entry_price=100.0, opened_at=0)
        simulator.settle_expired_orders(current_time=60_000, current_price=101.0)
        second = simulator.open_order(signal("LONG", timeframe_minutes=1), entry_price=101.0, opened_at=60_000)
        simulator.settle_expired_orders(current_time=120_000, current_price=102.0)
        third = simulator.open_order(signal("LONG", timeframe_minutes=1), entry_price=102.0, opened_at=120_000)
        simulator.settle_expired_orders(current_time=180_000, current_price=103.0)
        fourth = simulator.open_order(signal("LONG", timeframe_minutes=1), entry_price=103.0, opened_at=180_000)

        self.assertEqual([first.stake, second.stake, third.stake, fourth.stake], [10.0, 18.0, 32.4, 10.0])
        self.assertEqual([first.win_return, second.win_return, third.win_return, fourth.win_return], [18.0, 32.4, 58.32, 18.0])
        self.assertEqual([first.stake_progression_step, second.stake_progression_step, third.stake_progression_step, fourth.stake_progression_step], [1, 2, 3, 1])
        self.assertEqual(simulator.balance, 48.32)

    def test_stake_progression_resets_after_loss(self):
        simulator = AccountSimulator(enable_stake_progression=True)
        first = simulator.open_order(signal("LONG", timeframe_minutes=1), entry_price=100.0, opened_at=0)
        simulator.settle_expired_orders(current_time=60_000, current_price=101.0)
        second = simulator.open_order(signal("LONG", timeframe_minutes=1), entry_price=101.0, opened_at=60_000)
        simulator.settle_expired_orders(current_time=120_000, current_price=100.0)
        third = simulator.open_order(signal("LONG", timeframe_minutes=1), entry_price=100.0, opened_at=120_000)

        self.assertEqual([first.stake, second.stake, third.stake], [10.0, 18.0, 10.0])
        self.assertEqual([first.result, second.result], ["WIN", "LOSS"])
        self.assertEqual([first.pnl, second.pnl], [8.0, -18.0])


if __name__ == "__main__":
    unittest.main()
