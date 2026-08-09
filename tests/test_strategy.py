import unittest

from app.models import FearGreedContext, Kline, Signal
from app.strategy import (
    _failed_breakout_observation,
    _is_extreme_drop_reclaim,
    _risk_flags,
    _session_edge,
    analyze_observation_signals,
    analyze_volume_price,
    choose_best_candidate,
    choose_trade_signal,
    max_trade_edge_for,
)


def kline(idx, close, volume, open_price=None, high=None, low=None):
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


def baseline_klines():
    return [kline(660 + i, 100 + i * 0.05, 100) for i in range(80)]


def fear_falling_mid_drop_klines(drop_total=0.6, volume=125):
    klines = []
    for offset in range(120):
        idx = 600 + offset
        if offset == 0:
            close = 104.0
            high = 104.2
            low = 90.0
        elif offset == 1:
            close = 108.0
            high = 112.0
            low = 107.8
        else:
            close = 106.0 + (offset - 60) * 0.005 + (0.1 if offset % 2 else -0.1)
            high = close + 0.25
            low = close - 0.25
        klines.append(kline(idx, close, 100, open_price=close - 0.01, high=high, low=low))
    for offset in range(10):
        idx = 720 + offset
        close = 107.0 - drop_total * (offset / 9.0)
        open_price = 107.0 - drop_total * ((offset - 1) / 9.0) if offset else 107.05
        klines.append(
            kline(
                idx,
                close,
                volume,
                open_price=open_price,
                high=max(open_price, close) + 0.08,
                low=min(open_price, close) - 0.08,
            )
        )
    return klines


def neutral_mid_klines():
    klines = []
    for offset in range(120):
        close = 100.0 + (0.02 if offset % 2 else -0.02)
        high = 110.0 if offset == 0 else close + 0.1
        low = 90.0 if offset == 0 else close - 0.1
        klines.append(
            kline(
                1200 + offset,
                close,
                100.0,
                open_price=close,
                high=high,
                low=low,
            )
        )
    return klines


class StrategyTest(unittest.TestCase):
    def test_generic_short_profile_identity_is_stable(self):
        klines = neutral_mid_klines()
        latest = klines[-1]
        klines[-1] = Kline(
            open_time=latest.open_time,
            open=100.1,
            high=101.0,
            low=99.95,
            close=100.0,
            volume=100.0,
            close_time=latest.close_time,
        )

        signal = analyze_volume_price(klines, timeframe_minutes=10)

        self.assertEqual(signal.timeframe_minutes, 10)
        self.assertEqual(signal.direction, "WAIT")
        self.assertFalse(signal.actionable)
        self.assertEqual(
            (signal.strategy_family, signal.strategy_tag, signal.observe_direction),
            ("short_observe", "generic_short_observe", "SHORT"),
        )
        self.assertEqual(
            signal.profile_key,
            f"short_observe|SHORT|{signal.threshold_segment}",
        )

    def test_generic_long_profile_identity_is_stable(self):
        klines = neutral_mid_klines()
        latest = klines[-1]
        klines[-1] = Kline(
            open_time=latest.open_time,
            open=99.9,
            high=100.05,
            low=99.0,
            close=100.0,
            volume=100.0,
            close_time=latest.close_time,
        )

        signal = analyze_volume_price(klines, timeframe_minutes=10)

        self.assertEqual(signal.timeframe_minutes, 10)
        self.assertEqual(signal.direction, "WAIT")
        self.assertFalse(signal.actionable)
        self.assertEqual(
            (signal.strategy_family, signal.strategy_tag, signal.observe_direction),
            ("long_observe", "generic_long_observe", "LONG"),
        )
        self.assertEqual(
            signal.profile_key,
            f"long_observe|LONG|{signal.threshold_segment}",
        )

    def test_volume_price_analysis_rejects_non_10m_timeframes(self):
        with self.assertRaisesRegex(ValueError, "only 10-minute analysis is supported"):
            analyze_volume_price(neutral_mid_klines(), timeframe_minutes=30)

    def test_wd00_keeps_tight_overheat_edge_after_recent_replay(self):
        self.assertEqual(max_trade_edge_for(10, "WD-00", "LONG"), 16.0)

    def test_other_10m_sessions_use_revenue_first_baseline_overheat_edge(self):
        self.assertEqual(max_trade_edge_for(10, "WD-12", "LONG"), 30.0)

    def test_profitable_time_canvases_use_their_own_backtested_edges(self):
        self.assertEqual(max_trade_edge_for(10, "WD-20", "LONG"), 14.0)
        self.assertEqual(max_trade_edge_for(10, "WE-02", "LONG"), 24.0)
        self.assertEqual(max_trade_edge_for(10, "WE-17", "LONG"), 36.0)

    def test_short_setups_use_revenue_first_baseline_overheat_edge(self):
        self.assertEqual(max_trade_edge_for(10, "WD-21", "SHORT"), 30.0)

    def test_database_derived_short_observation_segments_are_not_live_enabled(self):
        for segment in ("WD-00", "WD-08", "WD-12", "WD-18", "WD-20"):
            with self.subTest(segment=segment):
                self.assertIsNone(_session_edge(10, segment, "SHORT"))

    def test_normal_volume_down_short_extension_is_limited_to_wd02_wd23(self):
        from app import strategy

        override = getattr(strategy, "_normal_down_short_override_reason", None)

        self.assertIsNotNone(override, "strategy must expose the database-derived normal-down SHORT override")
        reason = "量平价跌：MACD/RSI确认弱势延续，动态评分偏空"
        for segment in ("WD-02", "WD-23"):
            with self.subTest(segment=segment):
                note = override(
                    "SHORT",
                    reason,
                    timeframe_minutes=10,
                    threshold_segment=segment,
                    score_abs=65.0,
                    threshold=90.0,
                )
                self.assertIsNotNone(note)
                self.assertIn("量平价跌SHORT扩展", note)
                deep_note = override(
                    "SHORT",
                    reason,
                    timeframe_minutes=10,
                    threshold_segment=segment,
                    score_abs=30.0,
                    threshold=90.0,
                )
                self.assertIsNotNone(deep_note)

        self.assertIsNone(
            override(
                "SHORT",
                reason,
                timeframe_minutes=10,
                threshold_segment="WD-18",
                score_abs=65.0,
                threshold=90.0,
            )
        )
        self.assertIsNone(
            override(
                "SHORT",
                "高位放量下跌：MACD/RSI/BOLL确认卖压，动态评分偏空",
                timeframe_minutes=10,
                threshold_segment="WD-02",
                score_abs=65.0,
                threshold=90.0,
            )
        )

    def test_one_year_weak_sessions_are_not_hard_disabled(self):
        self.assertIsNotNone(_session_edge(10, "WD-00", "LONG"))
        self.assertIsNotNone(_session_edge(10, "WD-08", "LONG"))

    def test_one_year_positive_sessions_remain_allowed(self):
        self.assertIsNotNone(_session_edge(10, "WD-20", "LONG"))
        self.assertIsNotNone(_session_edge(10, "WD-18", "LONG"))

    def test_low_position_high_volume_rise_is_observation_only_after_psychology_review(self):
        klines = baseline_klines()
        start = klines[-1].open_time // 60_000 + 1
        for offset in range(10):
            close = 97.0 + offset * 0.35
            klines.append(
                kline(start + offset, close, 240, open_price=close - 0.25, high=close + 0.15, low=close - 0.35)
            )

        signal = analyze_volume_price(klines, timeframe_minutes=10)

        self.assertEqual(signal.direction, "WAIT")
        self.assertEqual(signal.timeframe_minutes, 10)
        self.assertIn("低位放量上涨", signal.reason)
        self.assertIn("仅预警观察", signal.reason)

    def test_high_position_high_volume_stall_is_observation_only_after_backtest_review(self):
        klines = [kline(i, 100 + i * 0.1, 100) for i in range(80)]
        for offset in range(30):
            close = 110.0 + (0.04 if offset % 2 else -0.04)
            klines.append(
                kline(80 + offset, close, 300, open_price=110.0, high=111.5, low=109.7)
            )

        signal = analyze_volume_price(klines, timeframe_minutes=10)

        self.assertEqual(signal.direction, "WAIT")
        self.assertEqual(signal.timeframe_minutes, 10)
        self.assertIn("高位放量滞涨", signal.reason)
        self.assertIn("不开单", signal.reason)

    def test_high_position_high_volume_drop_opens_short_when_bearish_indicators_confirm(self):
        klines = []
        for offset in range(260):
            idx = 960 + offset
            close = 105.0 + offset * 0.015
            low = 100.0 if offset == 0 else close - 0.2
            klines.append(kline(idx, close, 100, open_price=close - 0.01, high=close + 0.2, low=low))
        for offset in range(40):
            idx = 1220 + offset
            close = 109.0 + (offset % 3) * 0.02 if offset < 34 else 108.8 + (offset - 34) * 0.3
            klines.append(kline(idx, close, 100, open_price=close - 0.02, high=112.2, low=close - 0.2))
        start = klines[-1].open_time // 60_000 + 1
        price = 111.2
        for offset, step in enumerate([-1, -1, 1, -1, -1, 1, -1, -1, -1, -1]):
            idx = start + offset
            open_price = price
            price += step * 0.15
            close = price
            klines.append(kline(idx, close, 170, open_price=open_price, high=max(open_price, close) + 0.04, low=min(open_price, close) - 0.08))

        signal = analyze_volume_price(
            klines,
            timeframe_minutes=10,
            fear_greed=FearGreedContext(value=28, classification="Fear", average_30d=21.0, trend="rising"),
        )

        self.assertEqual(signal.threshold_segment, "WD-21")
        self.assertEqual(signal.direction, "SHORT")
        self.assertIn("高位放量下跌", signal.reason)
        self.assertIn("MACD", signal.reason)
        self.assertLess(signal.macd_histogram, 0)
        self.assertGreater(signal.rsi, 35)
        self.assertLessEqual(signal.bollinger_position, 0.85)

    def test_strict_rebound_risk_blocks_long_without_opening_short(self):
        klines = fear_falling_mid_drop_klines()
        signal = analyze_volume_price(
            klines,
            timeframe_minutes=10,
            fear_greed=FearGreedContext(value=18, classification="Extreme Fear", average_30d=37.0, trend="falling"),
        )

        self.assertEqual(signal.threshold_segment, "WD-12")
        self.assertEqual(signal.direction, "WAIT")
        self.assertFalse(signal.actionable)
        self.assertIn("趋势过滤禁多", signal.reason)
        self.assertIn("STRICT", signal.reason)
        self.assertIn("TREND_STRICT_WAIT", signal.risk_flags)
        self.assertGreaterEqual(signal.price_position, 0.35)
        self.assertGreaterEqual(signal.rsi, 45.0)
        self.assertGreaterEqual(signal.bollinger_position, 0.35)
        self.assertTrue(signal.macd_histogram < 0 or signal.macd_histogram_delta < 0)

    def test_broad_only_rebound_risk_switches_to_actionable_short(self):
        klines = fear_falling_mid_drop_klines(drop_total=0.86)
        signal = analyze_volume_price(
            klines,
            timeframe_minutes=10,
            fear_greed=FearGreedContext(value=28, classification="Fear", average_30d=37.0, trend="falling"),
        )

        self.assertEqual(signal.threshold_segment, "WD-12")
        self.assertEqual(signal.direction, "SHORT")
        self.assertTrue(signal.actionable)
        self.assertTrue(signal.session_allowed)
        self.assertIn("趋势候选顺势SHORT", signal.reason)
        self.assertIn("BROAD_ONLY", signal.reason)
        self.assertIn("TREND_BROAD_SHORT", signal.risk_flags)
        self.assertNotIn("TREND_STRICT_WAIT", signal.risk_flags)
        self.assertLess(signal.rsi, 45.0)
        self.assertGreater(signal.mtf_10m_bias, 0.0)
        self.assertGreater(signal.mtf_30m_bias, 0.0)

    def test_observation_signals_do_not_include_unproven_drop_reclaim_mirror_short(self):
        klines = fear_falling_mid_drop_klines(drop_total=1.0)
        primary = analyze_volume_price(
            klines,
            timeframe_minutes=10,
            fear_greed=FearGreedContext(value=28, classification="Fear", average_30d=37.0, trend="falling"),
        )
        observations = analyze_observation_signals(
            klines,
            timeframe_minutes=10,
            fear_greed=FearGreedContext(value=28, classification="Fear", average_30d=37.0, trend="falling"),
        )

        self.assertEqual(primary.direction, "WAIT")
        self.assertIn("极端过热", primary.reason)
        self.assertFalse(any(item.strategy_tag == "drop_reclaim_mirror_short_observe" for item in observations))

    def test_failed_breakout_observation_stays_disabled_after_walk_forward_review(self):
        history = [kline(820 + i, 100.0, 100, high=105.0, low=95.0) for i in range(120)]
        recent = [
            kline(940 + i, 100.0, 100, open_price=100.0, high=104.0, low=99.0)
            for i in range(9)
        ]
        latest = kline(949, 104.8, 100, open_price=105.2, high=106.0, low=104.5)
        recent.append(latest)
        primary = Signal(
            direction="WAIT",
            timeframe_minutes=10,
            level="A",
            reason="观察",
            price=latest.close,
            open_time=latest.open_time,
            threshold_segment="WD-12",
            score=0.0,
            threshold=0.0,
            bollinger_position=0.55,
            macd_histogram=-1.0,
            close_strength=0.2,
        )

        observation = _failed_breakout_observation(primary, history, recent, latest)

        self.assertIsNone(observation)

    def test_extreme_fear_raises_short_threshold(self):
        klines = []
        for offset in range(260):
            idx = 960 + offset
            close = 105.0 + offset * 0.015
            low = 100.0 if offset == 0 else close - 0.2
            klines.append(kline(idx, close, 100, open_price=close - 0.01, high=close + 0.2, low=low))
        for offset in range(40):
            idx = 1220 + offset
            close = 109.0 + (offset % 3) * 0.02 if offset < 34 else 108.8 + (offset - 34) * 0.3
            klines.append(kline(idx, close, 100, open_price=close - 0.02, high=112.2, low=close - 0.2))
        start = klines[-1].open_time // 60_000 + 1
        price = 111.2
        for offset, step in enumerate([-1, -1, 1, -1, -1, 1, -1, -1, -1, -1]):
            idx = start + offset
            open_price = price
            price += step * 0.15
            close = price
            klines.append(kline(idx, close, 170, open_price=open_price, high=max(open_price, close) + 0.04, low=min(open_price, close) - 0.08))

        neutral = analyze_volume_price(klines, timeframe_minutes=10)
        fearful = analyze_volume_price(
            klines,
            timeframe_minutes=10,
            fear_greed=FearGreedContext(value=18, classification="Extreme Fear", average_30d=37.0, trend="falling"),
        )

        self.assertEqual(neutral.direction, "SHORT")
        self.assertEqual(fearful.direction, "SHORT")
        self.assertGreater(fearful.threshold, neutral.threshold)
        self.assertGreater(fearful.fear_greed_adjustment, 0.0)
        self.assertEqual(fearful.regime, "FEAR_FALLING")

    def test_extreme_greed_raises_long_threshold(self):
        klines = [
            kline(i, 100 + (0.2 if i % 2 else -0.2), 100 + (30 if i % 3 == 0 else 0))
            for i in range(360, 480)
        ]
        for offset in range(10):
            idx = 480 + offset
            open_price = 100.0 - offset * 0.2
            close = open_price - 0.15
            klines.append(kline(idx, close, 160, open_price=open_price, high=open_price + 0.05, low=close - 0.1))

        neutral = analyze_volume_price(klines, timeframe_minutes=10)
        greedy = analyze_volume_price(
            klines,
            timeframe_minutes=10,
            fear_greed=FearGreedContext(value=84, classification="Extreme Greed", average_30d=62.0, trend="rising"),
        )

        self.assertEqual(neutral.direction, "LONG")
        self.assertGreater(greedy.threshold, neutral.threshold)
        self.assertGreater(greedy.fear_greed_adjustment, 0.0)

    def test_choose_best_candidate_ignores_non_live_candidates(self):
        ten = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="10m",
            price=100.0,
            open_time=0,
            score=82.0,
            threshold=70.0,
            session_allowed=True,
            session_sample_size=30,
            session_win_rate=0.68,
            session_ev=2.0,
        )
        non_live = Signal(
            direction="LONG",
            timeframe_minutes=15,
            level="A",
            reason="non-live",
            price=100.0,
            open_time=0,
            score=83.0,
            threshold=70.0,
            session_allowed=True,
            session_sample_size=30,
            session_win_rate=0.68,
            session_ev=2.1,
        )
        superior_non_live = Signal(
            direction="LONG",
            timeframe_minutes=15,
            level="A",
            reason="non-live strong",
            price=100.0,
            open_time=0,
            score=92.0,
            threshold=70.0,
            session_allowed=True,
            session_sample_size=60,
            session_win_rate=0.76,
            session_ev=4.2,
        )

        self.assertEqual(choose_best_candidate([ten, non_live]).timeframe_minutes, 10)
        self.assertEqual(choose_best_candidate([ten, superior_non_live]).timeframe_minutes, 10)

    def test_high_position_high_volume_drop_waits_when_short_indicators_do_not_confirm(self):
        klines = [kline(660 + i, 100.0, 100, high=108.0, low=99.5) for i in range(80)]
        start = klines[-1].open_time // 60_000 + 1
        for offset in range(10):
            open_price = 108.40 - offset * 0.02
            close = open_price - 0.02
            klines.append(kline(start + offset, close, 130, open_price=open_price, high=open_price + 0.01, low=close - 0.01))

        signal = analyze_volume_price(klines, timeframe_minutes=10)

        self.assertEqual(signal.direction, "WAIT")
        self.assertIn("高位放量下跌", signal.reason)
        self.assertIn("SHORT确认不足", signal.reason)

    def test_mid_position_high_volume_drop_generates_rebound_long_after_backtest_review(self):
        klines = [
            kline(i, 100 + (0.2 if i % 2 else -0.2), 100 + (30 if i % 3 == 0 else 0))
            for i in range(360, 480)
        ]
        for offset in range(10):
            idx = 480 + offset
            open_price = 100.0 - offset * 0.2
            close = open_price - 0.15
            klines.append(kline(idx, close, 160, open_price=open_price, high=open_price + 0.05, low=close - 0.1))

        signal = analyze_volume_price(klines, timeframe_minutes=10)

        self.assertEqual(signal.direction, "LONG")
        self.assertIn("放量急跌反抽", signal.reason)
        self.assertIn(signal.strategy_family, {"reversal", "drop_reclaim"})
        self.assertTrue(signal.strategy_tag.startswith("drop_reclaim"))
        self.assertEqual(signal.observe_direction, "LONG")
        self.assertGreaterEqual(signal.price_position, 0.0)
        self.assertLessEqual(signal.price_position, 1.0)

    def test_extreme_drop_reclaim_identity_uses_brainstorm_replay_thresholds(self):
        self.assertTrue(
            _is_extreme_drop_reclaim(
                price_change_pct=-0.012,
                volume_ratio=1.5,
                rsi=30.0,
                bollinger_position=0.5,
                has_lower_reclaim=True,
            )
        )
        self.assertTrue(
            _is_extreme_drop_reclaim(
                price_change_pct=-0.012,
                volume_ratio=1.5,
                rsi=45.0,
                bollinger_position=0.1,
                has_lower_reclaim=True,
            )
        )
        self.assertFalse(
            _is_extreme_drop_reclaim(
                price_change_pct=-0.011,
                volume_ratio=1.5,
                rsi=30.0,
                bollinger_position=0.1,
                has_lower_reclaim=True,
            )
        )

    def test_database_sample_hint_flags_are_observability_only(self):
        flags = _risk_flags(
            "FEAR_FALLING",
            0.0,
            "放量急跌反抽：回测显示急跌后后续窗口更偏反弹，动态评分偏多",
            raw_direction="LONG",
            threshold_segment="WD-18",
            level="A",
            price_position=0.45,
            price_change_pct=-0.0015,
            rsi=46.0,
            mtf_10m_bias=0.1,
            mtf_30m_bias=0.2,
        )

        self.assertIn("SAMPLE_WEAK_LEVEL_A_REBOUND", flags)
        self.assertIn("SAMPLE_WEAK_SEGMENT_WD-18", flags)
        self.assertIn("SAMPLE_WEAK_MID_POSITION_REBOUND", flags)
        self.assertIn("SAMPLE_WEAK_SHALLOW_DROP_REBOUND", flags)
        self.assertIn("SAMPLE_WEAK_HIGH_RSI_REBOUND", flags)
        self.assertIn("SAMPLE_WEAK_DUAL_UP_BIAS_REBOUND", flags)

    def test_rebound_long_guard_blocks_recently_bad_live_pattern(self):
        from app import strategy

        guard = getattr(strategy, "_long_rebound_guard_reason", None)

        self.assertIsNotNone(guard, "strategy must expose the database-derived rebound guard")
        reason = guard(
            "LONG",
            "放量急跌反抽：回测显示急跌后后续窗口更偏反弹，动态评分偏多",
            edge=17.9,
            rsi=45.0,
            bollinger_position=0.35,
            volume_ratio=4.0,
            mtf_10m_bias=1.0,
        )

        self.assertIsNotNone(reason)
        self.assertIn("急跌反抽过滤", reason)
        self.assertIn("RSI", reason)
        self.assertIn("BOLL", reason)
        self.assertIn("边际", reason)
        self.assertIn("量比", reason)
        self.assertIn("10m偏向", reason)

    def test_rebound_long_waits_when_score_edge_is_chase_zone(self):
        klines = [
            kline(i, 100 + (0.2 if i % 2 else -0.2), 100 + (30 if i % 3 == 0 else 0))
            for i in range(1320, 1440)
        ]
        for offset in range(10):
            idx = 1440 + offset
            open_price = 100.0 - offset * 0.2
            close = open_price - 0.15
            klines.append(kline(idx, close, 160, open_price=open_price, high=open_price + 0.05, low=close - 0.1))

        signal = analyze_volume_price(klines, timeframe_minutes=10)

        self.assertEqual(signal.direction, "WAIT")
        self.assertIn("极端过热", signal.reason)

    def test_extreme_score_edge_waits_after_backtest_review(self):
        klines = [kline(i, 100 + (0.02 if i % 2 else -0.02), 100) for i in range(600, 720)]
        for offset in range(10):
            idx = 720 + offset
            open_price = 100.0 - offset * 0.4
            close = open_price - 0.35
            klines.append(kline(idx, close, 260, open_price=open_price, high=open_price + 0.05, low=close - 0.1))

        signal = analyze_volume_price(klines, timeframe_minutes=10)

        self.assertEqual(signal.direction, "WAIT")
        self.assertIn("极端过热", signal.reason)

    def test_low_position_volume_reclaim_is_observation_only_after_backtest_review(self):
        klines = baseline_klines()
        start = klines[-1].open_time // 60_000 + 1
        for offset in range(10):
            idx = start + offset
            open_price = 97.0 - offset * 0.2
            close = open_price - 0.05
            low = close - 0.8
            if offset == 9:
                close = open_price + 0.15
            klines.append(kline(idx, close, 240, open_price=open_price, high=open_price + 0.2, low=low))

        signal = analyze_volume_price(klines, timeframe_minutes=10)

        self.assertEqual(signal.direction, "WAIT")
        self.assertIn("低位放量承接", signal.reason)
        self.assertIn("仅预警观察", signal.reason)

    def test_low_position_low_volume_fall_waits(self):
        klines = baseline_klines()
        start = klines[-1].open_time // 60_000 + 1
        klines.extend(
            [
                kline(start, 97.5, 70, open_price=97.8, high=97.9, low=97.4),
                kline(start + 1, 97.2, 65, open_price=97.5, high=97.6, low=97.1),
                kline(start + 2, 96.9, 60, open_price=97.2, high=97.3, low=96.8),
                kline(start + 3, 96.6, 55, open_price=96.9, high=97.0, low=96.5),
                kline(start + 4, 96.3, 50, open_price=96.6, high=96.7, low=96.2),
            ]
        )

        signal = analyze_volume_price(klines, timeframe_minutes=10)

        self.assertEqual(signal.direction, "WAIT")
        self.assertIn("低位缩量下跌", signal.reason)

    def test_dynamic_threshold_is_higher_in_noisy_market(self):
        quiet = [kline(i, 100 + (0.03 if i % 2 else -0.03), 100) for i in range(80)]
        quiet.extend(
            [
                kline(80, 98.8, 180, open_price=98.2, high=98.9, low=98.1),
                kline(81, 99.4, 190, open_price=98.8, high=99.5, low=98.7),
                kline(82, 100.0, 200, open_price=99.4, high=100.1, low=99.3),
                kline(83, 100.6, 210, open_price=100.0, high=100.7, low=99.9),
                kline(84, 101.2, 220, open_price=100.6, high=101.3, low=100.5),
            ]
        )
        noisy = [
            kline(i, 100 + (1.2 if i % 2 else -1.2), 100 + (40 if i % 3 == 0 else 0))
            for i in range(80)
        ]
        noisy.extend(quiet[-5:])

        quiet_signal = analyze_volume_price(quiet, timeframe_minutes=10)
        noisy_signal = analyze_volume_price(noisy, timeframe_minutes=10)

        self.assertGreater(noisy_signal.threshold, quiet_signal.threshold)

    def test_volume_ratio_uses_same_utc_hour_baseline(self):
        klines = []
        idx = 0
        for _day in range(8):
            for hour in range(24):
                volume = 1000 if hour == 12 else 100
                for _minute in range(60):
                    klines.append(kline(idx, 100.0, volume))
                    idx += 1
        for _hour in range(12):
            for _minute in range(60):
                klines.append(kline(idx, 100.0, 100))
                idx += 1
        for _minute in range(10):
            klines.append(kline(idx, 100.0, 1000))
            idx += 1

        signal = analyze_volume_price(klines, timeframe_minutes=10)

        self.assertEqual(signal.threshold_segment, "WD-12")
        self.assertLess(signal.volume_ratio, 1.5)
        self.assertIn("信号不足", signal.reason)

    def test_indicator_profile_uses_same_utc_hour_baseline(self):
        klines = []
        idx = 0
        for _day in range(8):
            for hour in range(24):
                for minute in range(60):
                    if hour == 21:
                        close = 100.0 + (minute % 20) * 0.08
                    else:
                        close = 100.0 + (minute % 10) * 0.01
                    klines.append(kline(idx, close, 100, open_price=close - 0.01, high=close + 0.08, low=close - 0.08))
                    idx += 1
        while ((idx // 60) % 24) != 21:
            klines.append(kline(idx, 100.0, 100, high=100.1, low=99.9))
            idx += 1
        for minute in range(10):
            close = 101.0 - minute * 0.05
            klines.append(kline(idx, close, 180, open_price=close + 0.03, high=close + 0.1, low=close - 0.1))
            idx += 1

        signal = analyze_volume_price(klines, timeframe_minutes=10)

        self.assertEqual(signal.threshold_segment, "WD-21")
        self.assertGreater(signal.indicator_profile_sample_size, 0)
        self.assertEqual(signal.indicator_profile_segment, "WD-21")
        self.assertNotEqual(signal.rsi_lower_threshold, 35.0)
        self.assertNotEqual(signal.bollinger_lower_threshold, 0.35)

    def test_weekend_volume_uses_weekend_hour_baseline(self):
        klines = []
        idx = 0
        for day in range(14):
            is_weekend = day % 7 in {2, 3}
            for hour in range(24):
                volume = 100 if is_weekend and hour == 12 else 1000 if hour == 12 else 80
                for _minute in range(60):
                    klines.append(kline(idx, 100.0, volume))
                    idx += 1
        # 移动到下一个周六 12:00 UTC。
        while ((idx // 1440) % 7) != 2 or ((idx // 60) % 24) != 12:
            klines.append(kline(idx, 100.0, 80))
            idx += 1
        for _minute in range(10):
            klines.append(kline(idx, 100.0, 100))
            idx += 1

        signal = analyze_volume_price(klines, timeframe_minutes=10)

        self.assertEqual(signal.threshold_segment, "WE-12")
        self.assertLess(signal.volume_ratio, 1.5)

    def test_unprofitable_utc_hour_blocks_otherwise_actionable_signal(self):
        klines = [kline(i, 100 + (0.2 if i % 2 else -0.2), 100 + (30 if i % 3 == 0 else 0)) for i in range(830)]
        for offset in range(10):
            idx = 830 + offset
            open_price = 100.0 - offset * 0.2
            close = open_price - 0.15
            klines.append(kline(idx, close, 160, open_price=open_price, high=open_price + 0.05, low=close - 0.1))

        signal = analyze_volume_price(klines, timeframe_minutes=10)

        self.assertEqual(signal.threshold_segment, "WD-13")
        self.assertEqual(signal.direction, "WAIT")
        self.assertIn("时段", signal.reason)

    def test_signal_exposes_timeframe_specific_session_edge_fields(self):
        klines = [
            kline(i, 100 + (0.2 if i % 2 else -0.2), 100 + (30 if i % 3 == 0 else 0))
            for i in range(360, 480)
        ]
        for offset in range(10):
            idx = 480 + offset
            open_price = 100.0 - offset * 0.2
            close = open_price - 0.15
            klines.append(kline(idx, close, 160, open_price=open_price, high=open_price + 0.05, low=close - 0.1))

        signal = analyze_volume_price(klines, timeframe_minutes=10)

        self.assertEqual(signal.threshold_segment, "WD-08")
        self.assertTrue(signal.session_allowed)
        self.assertGreater(signal.session_sample_size, 0)
        self.assertGreater(signal.session_win_rate, 0.0)
        self.assertGreater(signal.session_ev, 0.0)
        self.assertGreater(signal.session_edge_min, 0.0)

    def test_signal_exposes_aggregated_10m_and_30m_bias(self):
        klines = [kline(i, 100 + i * 0.03, 100) for i in range(120)]
        for offset in range(30):
            idx = 120 + offset
            close = 104.0 + offset * 0.15
            klines.append(kline(idx, close, 150, open_price=close - 0.1, high=close + 0.1, low=close - 0.2))

        signal = analyze_volume_price(klines, timeframe_minutes=10)

        self.assertGreater(signal.mtf_10m_bias, 0)
        self.assertGreater(signal.mtf_30m_bias, 0)

    def test_choose_trade_signal_returns_one_duration_above_threshold(self):
        klines = [
            kline(i, 100 + (0.2 if i % 2 else -0.2), 100 + (30 if i % 3 == 0 else 0))
            for i in range(360, 480)
        ]
        for offset in range(10):
            idx = 480 + offset
            open_price = 100.0 - offset * 0.2
            close = open_price - 0.15
            klines.append(kline(idx, close, 160, open_price=open_price, high=open_price + 0.05, low=close - 0.1))

        signal = choose_trade_signal(klines)

        self.assertEqual(signal.direction, "LONG")
        self.assertEqual(signal.timeframe_minutes, 10)
        self.assertGreaterEqual(abs(signal.score), signal.threshold)

    def test_choose_trade_signal_preserves_session_block_reason_when_score_passes_threshold(self):
        klines = [kline(i, 100 + (0.2 if i % 2 else -0.2), 100 + (30 if i % 3 == 0 else 0)) for i in range(830)]
        for offset in range(10):
            idx = 830 + offset
            open_price = 100.0 - offset * 0.2
            close = open_price - 0.15
            klines.append(kline(idx, close, 220, open_price=open_price, high=open_price + 0.05, low=close - 0.1))

        signal = choose_trade_signal(klines)

        self.assertEqual(signal.direction, "WAIT")
        self.assertGreaterEqual(abs(signal.score), signal.threshold)
        self.assertIn("时段", signal.reason)
        self.assertNotIn("< 阈值", signal.reason)

    def test_10m_signal_uses_previous_10_closed_minutes_as_analysis_window(self):
        klines = [kline(i, 100.0, 100) for i in range(80)]
        for offset in range(10):
            idx = 80 + offset
            open_price = 100.0 + offset
            close = 101.0 + offset
            if offset == 9:
                close = 110.0
            klines.append(kline(idx, close, 200, open_price=open_price, high=max(open_price, close), low=open_price))

        signal = analyze_volume_price(klines, timeframe_minutes=10)

        self.assertEqual(signal.analysis_window_minutes, 10)
        self.assertEqual(signal.threshold_window_minutes, 10)
        self.assertAlmostEqual(signal.price_change_pct, 0.10, places=4)

if __name__ == "__main__":
    unittest.main()
