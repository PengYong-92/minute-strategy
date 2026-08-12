import unittest

from app.models import Signal
from app.quality_score import (
    QUALITY_SCORE_MODE,
    QUALITY_SCORE_VERSION,
    attach_shadow_quality_score,
)


def candidate(**overrides) -> Signal:
    values = {
        "direction": "LONG",
        "timeframe_minutes": 10,
        "level": "B",
        "reason": "影子评分测试",
        "price": 100.0,
        "open_time": 1_000,
        "score": 0.0,
        "threshold": 78.5,
        "daily_profile_selected": True,
        "session_sample_size": 24,
        "session_win_rate": 0.66,
        "session_ev": 2.0,
        "volume_ratio": 1.2,
        "price_change_pct": 0.001,
        "price_position": 0.35,
        "close_strength": 0.7,
        "macd_histogram": 1.0,
        "macd_histogram_delta": 0.5,
        "rsi": 35.0,
        "bollinger_position": 0.3,
        "wave_state": "UP_LEG",
        "wave_efficiency": 0.8,
        "wave_direction_ratio": 0.8,
        "wave_atr_strength": 4.5,
    }
    values.update(overrides)
    return Signal(**values)


class ShadowQualityScoreTest(unittest.TestCase):
    def test_shadow_score_does_not_change_existing_order_decision_fields(self):
        signal = candidate()

        scored = attach_shadow_quality_score(signal, open_order_count=0)

        self.assertEqual(scored.direction, signal.direction)
        self.assertEqual(scored.score, signal.score)
        self.assertEqual(scored.threshold, signal.threshold)
        self.assertEqual(scored.daily_profile_selected, signal.daily_profile_selected)
        self.assertEqual(scored.actionable, signal.actionable)
        self.assertEqual(scored.quality_score_mode, QUALITY_SCORE_MODE)
        self.assertEqual(scored.quality_score_version, QUALITY_SCORE_VERSION)
        self.assertEqual(scored.quality_score_context, "LONG_FIRST")
        self.assertGreaterEqual(scored.quality_score, 0.0)
        self.assertLessEqual(scored.quality_score, 100.0)
        self.assertEqual(
            set(scored.quality_score_components),
            {
                "profile",
                "volume",
                "price_change",
                "price_position",
                "close_strength",
                "macd",
                "rsi",
                "bollinger",
                "wave_state",
                "wave_quality",
            },
        )

    def test_second_order_context_scores_weak_long_below_strong_long(self):
        strong = attach_shadow_quality_score(candidate(), open_order_count=1)
        weak = attach_shadow_quality_score(
            candidate(
                volume_ratio=1.7,
                price_change_pct=-0.001,
                price_position=0.75,
                close_strength=0.3,
                macd_histogram=-1.0,
                macd_histogram_delta=-0.5,
                rsi=47.0,
                bollinger_position=0.7,
                wave_state="TURN_UP",
                wave_efficiency=0.1,
                wave_direction_ratio=0.3,
                wave_atr_strength=0.5,
            ),
            open_order_count=1,
        )

        self.assertEqual(strong.quality_score_context, "LONG_SECOND")
        self.assertEqual(weak.quality_score_context, "LONG_SECOND")
        self.assertGreater(strong.quality_score, weak.quality_score)
        self.assertTrue(strong.actionable)
        self.assertTrue(weak.actionable)


if __name__ == "__main__":
    unittest.main()
