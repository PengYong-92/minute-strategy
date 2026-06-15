import unittest

from app.backtest import BacktestConfig
from app.models import Kline
from scripts.walk_forward_edge_cycle import Candidate
from scripts.walk_forward_stake_sweep import StakePolicy, evaluate_stake_policy, rank_stake_result


def kline(idx: int, close: float) -> Kline:
    return Kline(
        open_time=idx * 60_000,
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
        volume=100,
        close_time=idx * 60_000 + 59_999,
    )


def candidate(idx: int) -> Candidate:
    entry = idx * 60_000 + 59_999
    return Candidate(
        entry_index=idx,
        exit_index=idx + 1,
        entry_time=entry,
        expires_at=entry + 60_000,
        direction="LONG",
        segment="WD-12",
        setup="放量急跌反抽",
        reason="放量急跌反抽：synthetic",
        score=80.0,
        threshold=70.0,
        edge=10.0,
        stake_key="LONG|WD-12",
    )


class StakePolicySweepTest(unittest.TestCase):
    def test_evaluate_stake_policy_caps_progression_stake(self):
        klines = [kline(0, 100), kline(1, 101), kline(2, 102), kline(3, 103), kline(4, 104)]
        candidates = [candidate(0), candidate(1), candidate(2), candidate(3)]
        policy = StakePolicy(name="cap_18", enable_progression=True, max_orders=3, max_stake=18.0, loss_cooldown_orders=0)

        result = evaluate_stake_policy(
            klines,
            candidates,
            start_ms=0,
            end_ms=klines[-1].close_time,
            policy=policy,
            base_config=BacktestConfig(
                warmup_minutes=0,
                min_order_gap_minutes=0,
                enable_rolling_edge_guard=False,
                enable_stake_progression=True,
            ),
        )

        self.assertEqual([order["stake"] for order in result["orders"]], [10.0, 18.0, 18.0, 10.0])
        self.assertEqual(result["policy"], policy.__dict__)

    def test_rank_stake_result_penalizes_drawdown(self):
        low_drawdown = {
            "summary": {"balance": 100.0, "fixed_balance": 80.0},
            "risk": {"max_drawdown": -20.0, "max_loss_streak": 2},
        }
        high_drawdown = {
            "summary": {"balance": 100.0, "fixed_balance": 80.0},
            "risk": {"max_drawdown": -200.0, "max_loss_streak": 2},
        }

        self.assertGreater(rank_stake_result(low_drawdown), rank_stake_result(high_drawdown))


if __name__ == "__main__":
    unittest.main()
