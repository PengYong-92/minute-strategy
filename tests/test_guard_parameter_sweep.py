import unittest

from app.backtest import BacktestConfig
from app.models import Kline
from scripts.walk_forward_edge_cycle import Candidate
from scripts.walk_forward_guard_sweep import GuardSweepConfig, evaluate_guard_config, rank_guard_result


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


def candidate(idx: int, segment: str = "WD-12", reason: str = "放量急跌反抽：synthetic") -> Candidate:
    entry = idx * 60_000 + 59_999
    return Candidate(
        entry_index=idx,
        exit_index=idx + 1,
        entry_time=entry,
        expires_at=entry + 60_000,
        direction="LONG",
        segment=segment,
        setup="放量急跌反抽",
        reason=reason,
        score=80.0,
        threshold=70.0,
        edge=10.0,
        stake_key=f"LONG|{segment}",
    )


class GuardParameterSweepTest(unittest.TestCase):
    def test_evaluate_guard_config_reports_summary_and_rejections(self):
        klines = [kline(0, 100), kline(1, 99), kline(2, 98), kline(3, 97), kline(4, 96), kline(5, 95)]
        candidates = [candidate(0), candidate(1), candidate(2), candidate(3)]
        config = GuardSweepConfig(lookback_days=1, min_samples=2, min_win_rate=0.5556, min_ev=0.0)

        result = evaluate_guard_config(
            klines,
            candidates,
            start_ms=0,
            end_ms=klines[-1].close_time,
            guard=config,
            base_config=BacktestConfig(
                warmup_minutes=0,
                min_order_gap_minutes=0,
                enable_rolling_edge_guard=True,
                enable_stake_progression=False,
            ),
        )

        self.assertEqual(result["guard"], config.__dict__)
        self.assertEqual(result["summary"]["total_orders"], 2)
        self.assertEqual(result["summary"]["losses"], 2)
        self.assertEqual(result["rejected"]["rolling_edge_degraded"], 2)

    def test_rank_guard_result_penalizes_drawdown_and_rejections(self):
        stronger = {
            "summary": {"balance": 120.0},
            "risk": {"max_drawdown": -50.0, "max_loss_streak": 2},
            "rejected": {"rolling_edge_degraded": 5},
        }
        weaker = {
            "summary": {"balance": 120.0},
            "risk": {"max_drawdown": -200.0, "max_loss_streak": 5},
            "rejected": {"rolling_edge_degraded": 50},
        }

        self.assertGreater(rank_guard_result(stronger), rank_guard_result(weaker))


if __name__ == "__main__":
    unittest.main()
