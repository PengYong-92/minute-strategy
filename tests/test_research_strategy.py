import unittest

from app.models import Kline
from app.research_strategy import (
    BarFeatures,
    CandidateSignal,
    MagicianParams,
    apply_min_gap,
    event_contract_pnl,
    magician_signals,
    rolling_observation_guard_trades,
    summarize_trades,
)


def kline(idx, close):
    return Kline(
        open_time=idx * 60_000,
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
        volume=100,
        close_time=idx * 60_000 + 59_999,
    )


def feature(idx):
    return BarFeatures(
        index=idx,
        close_time=idx * 60_000 + 59_999,
        close=100.0 + idx,
        utc_hour=0,
        beijing_hour=8,
        beijing_bucket="BJT-08:00",
        is_weekend=False,
        ret_1=0.0,
        ret_3=0.0,
        ret_5=0.0,
        ret_10=0.0,
        ret_20=0.0,
        ret_5_z=0.0,
        range_30_pct=0.0,
        vol_ratio_5=1.0,
        rsi_14=50.0,
        boll_pos_20=0.5,
        ema20=100.0,
        ema60=100.0,
        trend_strength=0.0,
        close_strength=0.5,
        upper_rejection=False,
        lower_reclaim=False,
        break_up_20=False,
        break_down_20=False,
        compression_30=False,
    )


def feature_with(idx, **overrides):
    values = feature(idx).__dict__ | overrides
    return BarFeatures(**values)


class ResearchStrategyTest(unittest.TestCase):
    def test_event_contract_pnl_uses_10_to_win_8_and_flat_counts_loss(self):
        self.assertEqual(event_contract_pnl("LONG", 100.0, 101.0), ("WIN", 8.0))
        self.assertEqual(event_contract_pnl("SHORT", 100.0, 99.0), ("WIN", 8.0))
        self.assertEqual(event_contract_pnl("LONG", 100.0, 100.0), ("LOSS", -10.0))
        self.assertEqual(event_contract_pnl("SHORT", 100.0, 100.0), ("LOSS", -10.0))

    def test_apply_min_gap_keeps_non_overlapping_10_minute_entries(self):
        signals = [
            CandidateSignal(index=10, direction="LONG", family="trend", score=5.0, reason="a"),
            CandidateSignal(index=15, direction="SHORT", family="reversal", score=6.0, reason="b"),
            CandidateSignal(index=20, direction="LONG", family="trend", score=7.0, reason="c"),
        ]
        kept = apply_min_gap(signals, [kline(i, 100 + i) for i in range(40)], gap_minutes=10)

        self.assertEqual([item.index for item in kept], [10, 20])

    def test_summarize_trades_reports_break_even_and_drawdown(self):
        trades = [
            {
                "direction": "LONG",
                "entry_time": 0,
                "entry_price": 100.0,
                "exit_price": 101.0,
                "result": "WIN",
                "pnl": 8.0,
            },
            {
                "direction": "LONG",
                "entry_time": 60_000,
                "entry_price": 101.0,
                "exit_price": 100.0,
                "result": "LOSS",
                "pnl": -10.0,
            },
        ]

        stats = summarize_trades(trades)

        self.assertEqual(stats["total_orders"], 2)
        self.assertEqual(stats["wins"], 1)
        self.assertEqual(stats["losses"], 1)
        self.assertEqual(stats["win_rate"], 0.5)
        self.assertEqual(stats["balance"], -2.0)
        self.assertEqual(stats["break_even_win_rate"], 0.5556)
        self.assertEqual(stats["max_drawdown"], -10.0)

    def test_bar_features_session_bucket_uses_beijing_time(self):
        features = BarFeatures(
            index=0,
            close_time=0,
            close=100.0,
            utc_hour=0,
            beijing_hour=8,
            beijing_bucket="BJT-08:00",
            is_weekend=False,
            ret_1=0.0,
            ret_3=0.0,
            ret_5=0.0,
            ret_10=0.0,
            ret_20=0.0,
            ret_5_z=0.0,
            range_30_pct=0.0,
            vol_ratio_5=1.0,
            rsi_14=50.0,
            boll_pos_20=0.5,
            ema20=100.0,
            ema60=100.0,
            trend_strength=0.0,
            close_strength=0.5,
            upper_rejection=False,
            lower_reclaim=False,
            break_up_20=False,
            break_down_20=False,
            compression_30=False,
        )

        self.assertEqual(features.beijing_bucket, "BJT-08:00")

    def test_rolling_observation_guard_uses_only_prior_signal_outcomes(self):
        klines = [kline(i, 100 + i) for i in range(10)]
        features = [feature(i) for i in range(10)]
        signals = [
            CandidateSignal(index=0, direction="LONG", family="reversal", score=1.0, reason="a"),
            CandidateSignal(index=2, direction="LONG", family="reversal", score=1.0, reason="b"),
            CandidateSignal(index=4, direction="LONG", family="reversal", score=1.0, reason="c"),
            CandidateSignal(index=6, direction="LONG", family="reversal", score=1.0, reason="d"),
        ]

        trades = rolling_observation_guard_trades(
            klines,
            features,
            signals,
            start_ms=0,
            horizon_minutes=1,
            min_gap_minutes=0,
            min_samples=2,
            lookback_days=1,
            min_win_rate=1.0,
            min_avg_pnl=8.0,
            key_mode="family_hour",
        )

        self.assertEqual([trade["index"] for trade in trades], [4, 6])

    def test_magician_signals_emit_vcp_breakout_long_at_low_risk_pivot(self):
        features = [feature(i) for i in range(260)]
        features.append(
            feature_with(
                260,
                close=105.0,
                ema20=104.0,
                ema60=101.0,
                trend_strength=0.08,
                ret_1=0.05,
                ret_5_z=0.8,
                vol_ratio_5=1.4,
                rsi_14=61.0,
                boll_pos_20=0.72,
                break_up_20=True,
                compression_30=True,
            )
        )

        signals = magician_signals(features, MagicianParams())

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].direction, "LONG")
        self.assertEqual(signals[0].family, "magician_vcp")

    def test_magician_signals_do_not_chase_overextended_breakout(self):
        features = [feature(i) for i in range(260)]
        features.append(
            feature_with(
                260,
                close=105.0,
                ema20=104.0,
                ema60=101.0,
                trend_strength=0.08,
                ret_1=0.05,
                ret_5_z=3.2,
                vol_ratio_5=2.5,
                rsi_14=83.0,
                boll_pos_20=1.08,
                break_up_20=True,
                compression_30=True,
            )
        )

        signals = magician_signals(features, MagicianParams(max_abs_z=1.8))

        self.assertEqual(signals, [])

    def test_magician_signals_emit_pullback_restart_short(self):
        features = [feature(i) for i in range(260)]
        features.append(
            feature_with(
                260,
                close=95.0,
                ema20=96.0,
                ema60=100.0,
                trend_strength=-0.09,
                ret_1=-0.04,
                ret_3=-0.08,
                ret_5_z=-0.7,
                vol_ratio_5=1.2,
                rsi_14=42.0,
                boll_pos_20=0.35,
                break_down_20=False,
                compression_30=False,
            )
        )

        signals = magician_signals(features, MagicianParams())

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].direction, "SHORT")
        self.assertEqual(signals[0].family, "magician_pullback")


if __name__ == "__main__":
    unittest.main()
