import unittest

from scripts.analyze_loss_precursors import (
    PrecursorConfig,
    analyze_loss_precursors,
    annotate_orders,
)


class LossPrecursorAnalysisTest(unittest.TestCase):
    def test_prior_warnings_use_only_previous_orders(self):
        samples = [
            _sample(1, "LOSS", -10.0, segment="WD-18", entry=100.0, exit_price=99.0),
            _sample(2, "LOSS", -10.0, segment="WD-18", entry=100.0, exit_price=99.0),
            _sample(3, "LOSS", -10.0, segment="WD-18", entry=100.0, exit_price=99.0),
            _sample(4, "WIN", 8.0, segment="WD-18", entry=100.0, exit_price=101.0),
        ]

        annotations = annotate_orders(samples, config=PrecursorConfig(lookback_days=7, min_samples=2))

        self.assertEqual(annotations[0]["prior_warning_keys"], [])
        self.assertEqual(annotations[1]["prior_warning_keys"], [])
        self.assertIn("DEGRADED:RISK:HIGH_RSI_REBOUND", annotations[2]["prior_warning_keys"])
        self.assertIn("LOSS_STREAK:RISK:HIGH_RSI_REBOUND>=2", annotations[2]["prior_warning_keys"])
        self.assertIn("DEGRADED:SEGMENT:WD-18", annotations[2]["prior_warning_keys"])
        self.assertIn("LOSS_STREAK:SEGMENT:WD-18>=2", annotations[2]["prior_warning_keys"])

    def test_report_separates_current_risk_from_prior_precursors(self):
        samples = [
            _sample(1, "LOSS", -10.0, segment="WD-18", entry=100.0, exit_price=99.0),
            _sample(2, "LOSS", -10.0, segment="WD-18", entry=100.0, exit_price=99.0),
            _sample(3, "LOSS", -10.0, segment="WD-18", entry=100.0, exit_price=99.0),
            _sample(4, "WIN", 8.0, segment="WD-12", entry=100.0, exit_price=101.0, rsi=34.0),
        ]

        report = analyze_loss_precursors(samples, config=PrecursorConfig(lookback_days=7, min_samples=2))

        warning_names = {item["key"] for item in report["warning_keys"]}
        replay_names = {item["name"] for item in report["replay_candidates"]}
        self.assertIn("RISK_NOW:HIGH_RSI_REBOUND", warning_names)
        self.assertIn("ANY_PRIOR_WARNING", replay_names)
        self.assertEqual(report["wrong_release"]["losses"], 3)
        self.assertEqual(report["wrong_release"]["prior_warned_losses"], 1)
        self.assertEqual(report["baseline"]["reverse_direction_fixed_stake"]["wins"], 3)


def _sample(
    order_id: int,
    result: str,
    pnl: float,
    *,
    segment: str,
    entry: float,
    exit_price: float,
    rsi: float = 46.0,
) -> dict:
    opened_at = 1_780_000_000_000 + order_id * 600_000
    return {
        "symbol": "BTCUSDT",
        "order_id": order_id,
        "direction": "LONG",
        "timeframe_minutes": 10,
        "threshold_segment": segment,
        "result": result,
        "pnl": pnl,
        "stake": 10.0,
        "stake_progression_step": 1,
        "opened_at": opened_at,
        "settled_at": opened_at + 600_000,
        "entry_price": entry,
        "exit_price": exit_price,
        "level": "A",
        "reason": "放量急跌反抽：synthetic",
        "reason_setup": "放量急跌反抽",
        "score": 85.0,
        "threshold": 70.0,
        "edge": 15.0,
        "volume_ratio": 2.1,
        "volume_threshold": 1.5,
        "price_change_pct": -0.0015,
        "price_position": 0.45,
        "rsi": rsi,
        "bollinger_position": 0.2,
        "mtf_10m_bias": 0.1,
        "mtf_30m_bias": 0.2,
        "regime": "FEAR_FALLING",
        "risk_flags": "",
        "fear_greed_value": 12,
        "fear_greed_trend": "falling",
        "profile_guard_shadow_status": "",
        "profile_guard_default_shadow_status": "",
    }


if __name__ == "__main__":
    unittest.main()
