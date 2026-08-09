import unittest
from dataclasses import replace

from app.models import Kline, Signal, SimulatedOrder
from app.order_policy import OrderPolicy


def kline(idx, close=100.0):
    return Kline(
        open_time=idx * 60_000,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=100,
        close_time=idx * 60_000 + 59_999,
    )


def signal(direction="LONG", score=82.0, threshold=70.0, segment="WD-12"):
    return Signal(
        direction=direction,
        timeframe_minutes=10,
        level="A",
        reason="policy",
        price=100.0,
        open_time=1_000,
        score=score,
        threshold=threshold,
        threshold_segment=segment,
        session_allowed=True,
        session_edge_min=10.0,
    )


class OrderPolicyTest(unittest.TestCase):
    def test_default_policy_allows_second_open_order_and_blocks_third(self):
        policy = OrderPolicy()
        latest = kline(20)
        open_orders = [
            SimulatedOrder(
                id=idx + 1,
                direction="LONG",
                timeframe_minutes=10,
                level="A",
                reason="open",
                entry_price=100.0,
                opened_at=latest.close_time - (idx + 2) * 120_000,
                expires_at=latest.close_time + 600_000,
                threshold_segment="WD-12",
            )
            for idx in range(1)
        ]

        second = policy.evaluate(
            signal(),
            latest,
            open_orders,
            latest.close_time - 120_000,
            set(),
        )
        third = policy.evaluate(
            signal(),
            latest,
            [*open_orders, replace(open_orders[-1], id=2)],
            latest.close_time - 120_000,
            set(),
        )

        self.assertTrue(second.open_allowed)
        self.assertEqual(third.code, "HOLD_OPEN_ORDER")

    def test_default_policy_keeps_two_minute_entry_gap(self):
        policy = OrderPolicy()
        latest = kline(20)

        gate = policy.evaluate(signal(), latest, [], latest.close_time - 60_000, set())

        self.assertFalse(gate.open_allowed)
        self.assertEqual(gate.code, "COOLDOWN")

    def test_allows_actionable_signal_without_open_orders(self):
        policy = OrderPolicy(max_open_orders=1, min_order_gap_ms=600_000)

        gate = policy.evaluate(signal(), kline(20), [], None, set())

        self.assertTrue(gate.open_allowed)
        self.assertEqual(gate.code, "OPENED")
        self.assertEqual(gate.signal_key, (1_000, 10, "LONG"))

    def test_blocks_duplicate_signal_key(self):
        policy = OrderPolicy(max_open_orders=1, min_order_gap_ms=600_000)
        opened_keys = {(1_000, 10, "LONG")}

        gate = policy.evaluate(signal(), kline(20), [], None, opened_keys)

        self.assertFalse(gate.open_allowed)
        self.assertEqual(gate.code, "DUPLICATE_SIGNAL")

    def test_segment_losses_do_not_block_mechanical_admission(self):
        policy = OrderPolicy(max_open_orders=1, min_order_gap_ms=600_000)
        losses = [
            SimulatedOrder(
                id=idx + 1,
                direction="LONG",
                timeframe_minutes=10,
                level="A",
                reason="loss",
                entry_price=100.0,
                opened_at=1_800_000 + idx * 600_000,
                expires_at=2_400_000 + idx * 600_000,
                threshold_segment="WD-00",
                status="SETTLED",
                result="LOSS",
                exit_price=99.0,
                settled_at=2_400_000 + idx * 600_000,
                pnl=-10.0,
            )
            for idx in range(3)
        ]

        gate = policy.evaluate(signal(segment="WD-00"), kline(70), losses, None, set())

        self.assertTrue(gate.open_allowed)
        self.assertEqual(gate.code, "OPENED")

    def test_wait_decision_keeps_wd00_overheat_edge_tight(self):
        decision = OrderPolicy.wait_decision(signal(score=86.0, threshold=70.0, segment="WD-00"))

        self.assertEqual(decision, "OVERHEATED")

    def test_wait_decision_uses_revenue_first_baseline_edge_for_non_wd00(self):
        below_baseline = OrderPolicy.wait_decision(signal(score=99.0, threshold=70.0, segment="WD-12"))
        at_baseline = OrderPolicy.wait_decision(signal(score=100.0, threshold=70.0, segment="WD-12"))

        self.assertEqual(below_baseline, "EDGE_TOO_SMALL")
        self.assertEqual(at_baseline, "OVERHEATED")

    def test_wait_decision_uses_segment_specific_relaxed_edge(self):
        below_segment_edge = OrderPolicy.wait_decision(signal(score=105.0, threshold=70.0, segment="WE-17"))
        at_segment_edge = OrderPolicy.wait_decision(signal(score=106.0, threshold=70.0, segment="WE-17"))

        self.assertEqual(below_segment_edge, "EDGE_TOO_SMALL")
        self.assertEqual(at_segment_edge, "OVERHEATED")


if __name__ == "__main__":
    unittest.main()
