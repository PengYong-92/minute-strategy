import dataclasses
import unittest

from app.models import FearGreedContext, Kline
from app.shadow_models import MarketEvent, ShadowEvaluationMetrics, ShadowParameterSnapshot


def closed_minute_kline(index: int = 1) -> Kline:
    open_time = index * 60_000
    return Kline(
        open_time=open_time,
        open=100.0,
        high=102.0,
        low=99.0,
        close=101.0,
        volume=12.5,
        close_time=open_time + 59_999,
    )


class MarketEventTest(unittest.TestCase):
    def test_event_id_is_stable_and_covers_only_market_identity(self):
        fear_greed = FearGreedContext(
            value=42,
            classification="Fear",
            average_30d=47.5,
            trend="falling",
            updated_at_ms=123_456,
            source="fixture",
        )
        left = MarketEvent(
            symbol="btcusdt",
            generation=7,
            kline=closed_minute_kline(),
            fear_greed=fear_greed,
        )
        right = MarketEvent(
            symbol="BTCUSDT",
            generation=7,
            kline=closed_minute_kline(),
            fear_greed=fear_greed,
        )

        self.assertEqual(left.symbol, "BTCUSDT")
        self.assertEqual(left.event_id, right.event_id)
        self.assertEqual(len(left.event_id), 64)
        self.assertNotEqual(
            left.event_id,
            dataclasses.replace(left, generation=8).event_id,
        )
        self.assertEqual(
            left.event_id,
            dataclasses.replace(
                left,
                fear_greed=dataclasses.replace(fear_greed, value=43),
            ).event_id,
        )
        self.assertNotEqual(
            left.to_dict()["fear_greed"],
            dataclasses.replace(
                left,
                fear_greed=dataclasses.replace(fear_greed, value=43),
            ).to_dict()["fear_greed"],
        )

    def test_event_requires_an_exactly_closed_one_minute_kline(self):
        invalid = dataclasses.replace(closed_minute_kline(), close_time=120_000)

        with self.assertRaisesRegex(ValueError, "closed 1-minute"):
            MarketEvent(
                symbol="BTCUSDT",
                generation=1,
                kline=invalid,
                fear_greed=FearGreedContext(value=50, classification="Neutral"),
            )

    def test_event_is_immutable(self):
        event = MarketEvent(
            symbol="BTCUSDT",
            generation=1,
            kline=closed_minute_kline(),
            fear_greed=FearGreedContext(value=50, classification="Neutral"),
        )

        with self.assertRaises(dataclasses.FrozenInstanceError):
            event.generation = 2


class ShadowParameterSnapshotTest(unittest.TestCase):
    def test_hash_is_canonical_and_snapshot_cannot_be_changed_through_input(self):
        source = {
            "limits": {"long": 2, "short": 3},
            "weights": [1.0, 2.0],
            "enabled": True,
        }
        left = ShadowParameterSnapshot(
            family="profile_admission",
            version="v1",
            parameters=source,
        )
        right = ShadowParameterSnapshot(
            family="profile_admission",
            version="v1",
            parameters={
                "enabled": True,
                "weights": (1.0, 2.0),
                "limits": {"short": 3, "long": 2},
            },
        )
        source["limits"]["long"] = 99
        source["weights"].append(3.0)

        self.assertEqual(left.parameter_hash, right.parameter_hash)
        self.assertEqual(left.parameters["limits"]["long"], 2)
        self.assertEqual(left.parameters["weights"], [1.0, 2.0])
        self.assertEqual(len(left.parameter_hash), 64)
        self.assertEqual(left.to_dict(), right.to_dict())

    def test_hash_covers_family_version_and_every_parameter(self):
        base = ShadowParameterSnapshot(
            family="profile_admission",
            version="v1",
            parameters={"threshold": 0.6},
        )

        self.assertNotEqual(
            base.parameter_hash,
            ShadowParameterSnapshot(
                family="quality_score",
                version="v1",
                parameters={"threshold": 0.6},
            ).parameter_hash,
        )
        self.assertNotEqual(
            base.parameter_hash,
            ShadowParameterSnapshot(
                family="profile_admission",
                version="v2",
                parameters={"threshold": 0.6},
            ).parameter_hash,
        )
        self.assertNotEqual(
            base.parameter_hash,
            ShadowParameterSnapshot(
                family="profile_admission",
                version="v1",
                parameters={"threshold": 0.6001},
            ).parameter_hash,
        )

    def test_snapshot_rejects_non_finite_and_non_string_key_values(self):
        with self.assertRaises(ValueError):
            ShadowParameterSnapshot(
                family="profile_admission",
                version="v1",
                parameters={"threshold": float("nan")},
            )
        with self.assertRaises(ValueError):
            ShadowParameterSnapshot(
                family="profile_admission",
                version="v1",
                parameters={1: "invalid"},
            )


class ShadowEvaluationMetricsTest(unittest.TestCase):
    def test_metrics_derive_directional_and_total_rates(self):
        metrics = ShadowEvaluationMetrics(
            complete_days=7,
            settled_orders=300,
            wins=180,
            long_orders=150,
            long_wins=90,
            short_orders=150,
            short_wins=90,
            qualified_win_rate_days=5,
            positive_ev_days=5,
            days_beating_champion=5,
            average_orders_per_day=42.857,
            worst_rolling_3d_win_rate=0.58,
            total_ev=120.0,
            total_pnl=96.0,
            max_drawdown=30.0,
            max_loss_streak=4,
            current_loss_streak=1,
        )

        self.assertEqual(metrics.win_rate, 0.6)
        self.assertEqual(metrics.long_win_rate, 0.6)
        self.assertEqual(metrics.short_win_rate, 0.6)
        self.assertEqual(metrics.minimum_direction_win_rate, 0.6)

    def test_metrics_reject_internally_inconsistent_counts(self):
        with self.assertRaises(ValueError):
            ShadowEvaluationMetrics(
                complete_days=7,
                settled_orders=10,
                wins=11,
                long_orders=5,
                long_wins=5,
                short_orders=5,
                short_wins=6,
            )


if __name__ == "__main__":
    unittest.main()
