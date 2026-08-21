import json
import unittest
from unittest.mock import patch

from app.models import FearGreedContext, Kline, Signal
from app.profile_admission import baseline_policy, candidate_policy
from app.shadow_models import MarketEvent
from app.shadow_runtime import FrozenFearGreedProvider, ShadowRuntime
from app.state import MonitorState


MINUTE_MS = 60_000


def kline(index: int, close: float | None = None) -> Kline:
    price = float(100 + index if close is None else close)
    return Kline(
        open_time=index * MINUTE_MS,
        open=price - 0.5,
        high=price + 1.0,
        low=price - 1.0,
        close=price,
        volume=100.0,
        close_time=(index + 1) * MINUTE_MS - 1,
    )


def fear(value: int, trend: str = "flat") -> FearGreedContext:
    return FearGreedContext(
        value=value,
        classification="Neutral",
        average_30d=50.0,
        trend=trend,
        updated_at_ms=123,
    )


def actionable_signal(item: Kline, context: FearGreedContext) -> Signal:
    return Signal(
        direction="LONG",
        timeframe_minutes=10,
        level="S",
        reason="shadow-runtime-test",
        price=item.close,
        open_time=item.open_time,
        score=90.0,
        threshold=10.0,
        calculated_threshold=10.0,
        threshold_segment="WD-00",
        session_allowed=True,
        fear_greed_value=context.value,
        fear_greed_classification=context.classification,
        fear_greed_trend=context.trend,
        strategy_family="test",
        strategy_tag="complete-state",
    )


class ShadowRuntimeTest(unittest.TestCase):
    def setUp(self):
        source = MonitorState(
            "BTCUSDT",
            max_open_orders=2,
            max_open_long_orders=2,
            max_open_short_orders=2,
            min_order_gap_ms=0,
            enable_observation_profile_promotion=False,
            strategy_build_id="shadow-runtime-test",
        )
        source.seed_klines([kline(index) for index in range(20)])
        self.seed = source.shadow_runtime_seed()
        source.close()
        self.runtime = ShadowRuntime.from_seed(
            self.seed,
            generation=7,
            policies={
                "champion": baseline_policy(),
                "challenger": candidate_policy(),
            },
        )
        self.addCleanup(self.runtime.close)

    def _event(self, index: int, *, fear_value: int = 50) -> MarketEvent:
        return MarketEvent(
            symbol="BTCUSDT",
            generation=7,
            kline=kline(index),
            fear_greed=fear(fear_value),
        )

    def _process_with_actionable_signal(self, event: MarketEvent) -> bool:
        def choose(klines, fear_greed=None):
            return actionable_signal(klines[-1], fear_greed)

        def analyze(klines, timeframe_minutes, fear_greed=None):
            return actionable_signal(klines[-1], fear_greed)

        with (
            patch("app.state.choose_trade_signal", side_effect=choose),
            patch("app.state.analyze_volume_price", side_effect=analyze),
            patch("app.state.analyze_observation_signals", return_value=[]),
        ):
            return self.runtime.process(event)

    def test_frozen_provider_returns_detached_current_snapshot(self):
        provider = FrozenFearGreedProvider(fear(21, "falling"))

        first = provider.get_context()
        provider.update(fear(73, "rising"))

        self.assertEqual(first.value, 21)
        self.assertEqual(provider.get_context().value, 73)
        self.assertIsNot(first, provider.get_context())

    def test_two_policies_own_isolated_complete_monitor_states(self):
        event = self._event(20, fear_value=33)

        self.assertTrue(self._process_with_actionable_signal(event))

        champion = self.runtime.state("champion")
        challenger = self.runtime.state("challenger")
        self.assertIsNot(champion, challenger)
        self.assertEqual(
            champion.profile_admission_policy.policy_hash,
            baseline_policy().policy_hash,
        )
        self.assertEqual(
            challenger.profile_admission_policy.policy_hash,
            candidate_policy().policy_hash,
        )
        self.assertEqual(len(champion.simulator.orders), 1)
        self.assertEqual(len(challenger.simulator.orders), 1)
        self.assertIsNot(champion.simulator.orders[0], challenger.simulator.orders[0])
        champion.simulator.orders[0].reason = "changed-only-in-champion"
        self.assertEqual(challenger.simulator.orders[0].reason, "shadow-runtime-test")
        self.assertIsNot(champion.observations, challenger.observations)
        self.assertIsNot(champion.simulator.stake_progression, challenger.simulator.stake_progression)

    def test_duplicate_event_is_idempotent(self):
        event = self._event(20)
        self.assertTrue(self._process_with_actionable_signal(event))

        self.assertFalse(self._process_with_actionable_signal(event))

        self.assertEqual(len(self.runtime.orders("champion")), 1)
        self.assertEqual(self.runtime.cursor_event_id, event.event_id)
        self.assertFalse(self.runtime.invalid)

    def test_minute_gap_freezes_generation_without_future_backfill(self):
        self.assertTrue(self.runtime.process(self._event(20)))
        missing_minute = self._event(22)

        self.assertFalse(self.runtime.process(missing_minute))
        self.assertTrue(self.runtime.invalid)
        self.assertIn("minute gap", self.runtime.invalid_reason)
        self.assertEqual(self.runtime.cursor_event_id, self._event(20).event_id)
        self.assertFalse(self.runtime.process(self._event(21)))
        self.assertEqual(self.runtime.orders("champion"), ())

    def test_first_live_event_anchors_generation_even_after_warmup_gap(self):
        delayed = self._event(40)

        self.assertTrue(self.runtime.process(delayed))

        self.assertFalse(self.runtime.invalid)
        self.assertEqual(self.runtime.cursor_event_id, delayed.event_id)

    def test_event_fear_greed_snapshot_is_used_by_every_complete_state(self):
        event = self._event(20, fear_value=17)
        observed_values = []

        def choose(klines, fear_greed=None):
            observed_values.append(fear_greed.value)
            return actionable_signal(klines[-1], fear_greed)

        def analyze(klines, timeframe_minutes, fear_greed=None):
            return actionable_signal(klines[-1], fear_greed)

        with (
            patch("app.state.choose_trade_signal", side_effect=choose),
            patch("app.state.analyze_volume_price", side_effect=analyze),
            patch("app.state.analyze_observation_signals", return_value=[]),
        ):
            self.assertTrue(self.runtime.process(event))

        self.assertEqual(observed_values, [17])
        self.assertEqual(self.runtime.state("champion").fear_greed.value, 17)
        self.assertEqual(self.runtime.state("challenger").fear_greed.value, 17)

    def test_checkpoint_is_json_serializable_and_contains_independent_runtime_state(self):
        self.assertTrue(self._process_with_actionable_signal(self._event(20)))

        checkpoint = self.runtime.checkpoint()
        encoded = json.dumps(checkpoint, sort_keys=True)

        self.assertIn('"cursor_event_id"', encoded)
        self.assertIn('"orders"', encoded)
        self.assertIn('"observations"', encoded)
        self.assertIn('"credits"', encoded)
        self.assertIn('"wave"', encoded)
        self.assertIn('"daily_profile_selection"', encoded)
        self.assertEqual(set(checkpoint["arms"]), {"champion", "challenger"})

    def test_checkpoint_restores_orders_state_and_exact_event_cursor(self):
        first_event = self._event(20)
        self.assertTrue(self._process_with_actionable_signal(first_event))
        checkpoint = self.runtime.checkpoint()
        restored_seed = {**self.seed, "klines": (*self.seed["klines"], first_event.kline)}

        restored = ShadowRuntime.from_checkpoint(
            restored_seed,
            checkpoint=checkpoint,
            policies={
                "champion": baseline_policy(),
                "challenger": candidate_policy(),
            },
        )
        self.addCleanup(restored.close)

        self.assertEqual(restored.cursor_event_id, first_event.event_id)
        self.assertEqual(restored.orders("champion"), self.runtime.orders("champion"))
        self.assertFalse(restored.process(first_event))
        self.assertFalse(restored.invalid)
        self.assertTrue(restored.process(self._event(21)))

    def test_persistence_state_omits_large_records_and_restores_from_separate_rows(self):
        first_event = self._event(20)
        self.assertTrue(self._process_with_actionable_signal(first_event))
        compact = self.runtime.persistence_state("champion")
        arm_state = compact["arm"]

        self.assertNotIn("orders", arm_state)
        self.assertNotIn("observations", arm_state)
        self.assertNotIn("adaptive_profile_states", arm_state)
        self.assertNotIn("direction_pulse_shadow", arm_state)
        json.dumps(compact, sort_keys=True)

        restored_seed = {**self.seed, "klines": (*self.seed["klines"], first_event.kline)}
        restored = ShadowRuntime.from_persistence(
            restored_seed,
            arm_id="champion",
            policy=baseline_policy(),
            effective_from_ms=self.seed["klines"][-1].close_time + 1,
            runtime_state=compact,
            orders=[item.to_dict() for item in self.runtime.orders("champion")],
            observations=[
                item.to_dict()
                for item in self.runtime.state("champion").observations
            ],
        )
        self.addCleanup(restored.close)

        self.assertEqual(restored.cursor_event_id, first_event.event_id)
        self.assertEqual(restored.orders("champion"), self.runtime.orders("champion"))

    def test_restore_with_seed_ahead_of_committed_cursor_freezes_generation(self):
        first_event = self._event(20)
        self.assertTrue(self.runtime.process(first_event))
        checkpoint = self.runtime.checkpoint()
        future_seed = {
            **self.seed,
            "klines": (*self.seed["klines"], first_event.kline, kline(21)),
        }

        restored = ShadowRuntime.from_checkpoint(
            future_seed,
            checkpoint=checkpoint,
            policies={
                "champion": baseline_policy(),
                "challenger": candidate_policy(),
            },
        )
        self.addCleanup(restored.close)

        self.assertTrue(restored.invalid)
        self.assertIn("seed advanced beyond committed cursor", restored.invalid_reason)

    def test_batch_is_analyzed_once_at_final_minute_like_formal_runtime(self):
        events = (self._event(20), self._event(21), self._event(22))
        analyzed_lengths = []

        def choose(klines, fear_greed=None):
            analyzed_lengths.append(len(klines))
            return actionable_signal(klines[-1], fear_greed)

        def analyze(klines, timeframe_minutes, fear_greed=None):
            return choose(klines, fear_greed)

        with (
            patch("app.state.choose_trade_signal", side_effect=choose),
            patch("app.state.analyze_volume_price", side_effect=analyze),
            patch("app.state.analyze_observation_signals", return_value=[]),
        ):
            self.assertTrue(
                self.runtime.process_batch(events),
                self.runtime.invalid_reason,
            )

        self.assertEqual(analyzed_lengths, [23, 23])
        self.assertEqual(self.runtime.cursor_event_id, events[-1].event_id)
        self.assertEqual(len(self.runtime.orders("champion")), 1)

    def test_orders_daily_statistics_and_metrics_are_exposed(self):
        self.assertTrue(self._process_with_actionable_signal(self._event(20)))

        self.assertEqual(len(self.runtime.orders("champion")), 1)
        self.assertEqual(self.runtime.daily_statistics("champion"), ())
        metrics = self.runtime.evaluation_metrics("champion")
        self.assertEqual(metrics.complete_days, 0)
        self.assertEqual(metrics.settled_orders, 0)

    def test_same_price_settlement_uses_monitor_simulator_loss_rule(self):
        self.assertTrue(self._process_with_actionable_signal(self._event(20)))
        entry_price = self.runtime.orders("champion")[0].entry_price
        for index in range(21, 30):
            self.assertTrue(self._process_with_actionable_signal(self._event(index)))
        self.assertTrue(
            self._process_with_actionable_signal(
                MarketEvent(
                    symbol="BTCUSDT",
                    generation=7,
                    kline=kline(30, close=entry_price),
                    fear_greed=fear(50),
                )
            )
        )

        first = self.runtime.orders("champion")[0]
        self.assertEqual(first.status, "SETTLED")
        self.assertEqual(first.exit_price, first.entry_price)
        self.assertEqual(first.result, "LOSS")

    def test_symbol_and_generation_are_rejected_before_state_mutation(self):
        wrong_symbol = MarketEvent(
            symbol="ETHUSDT",
            generation=7,
            kline=kline(20),
            fear_greed=fear(50),
        )
        wrong_generation = MarketEvent(
            symbol="BTCUSDT",
            generation=8,
            kline=kline(20),
            fear_greed=fear(50),
        )

        with self.assertRaisesRegex(ValueError, "symbol"):
            self.runtime.process(wrong_symbol)
        with self.assertRaisesRegex(ValueError, "generation"):
            self.runtime.process(wrong_generation)
        self.assertFalse(self.runtime.invalid)
        self.assertEqual(self.runtime.orders("champion"), ())

    def test_close_is_idempotent_and_stops_processing(self):
        champion = self.runtime.state("champion")
        challenger = self.runtime.state("challenger")

        self.runtime.close()
        self.runtime.close()

        self.assertTrue(self.runtime.closed)
        self.assertTrue(champion._storage_executor._shutdown)
        self.assertTrue(challenger._storage_executor._shutdown)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.runtime.process(self._event(20))

    def test_stop_is_an_explicit_close_alias(self):
        self.runtime.stop()

        self.assertTrue(self.runtime.closed)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.runtime.process(self._event(20))


if __name__ == "__main__":
    unittest.main()
