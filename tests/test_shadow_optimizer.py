import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.models import FearGreedContext, Kline, ObservationSignal, Signal
from app.shadow_models import MarketEvent
from app.shadow_models import ShadowEvaluationMetrics
from app.shadow_optimizer import ShadowLifecycleScheduler, ShadowOptimizer
from app.shadow_storage import ShadowSQLiteStore
from app.shadow_supervisor import ShadowSupervisor
from app.state import MonitorState


def kline(index: int, close: float | None = None) -> Kline:
    price = float(100 + index if close is None else close)
    return Kline(
        open_time=index * 60_000,
        open=price - 0.5,
        high=price + 1.0,
        low=price - 1.0,
        close=price,
        volume=100.0,
        close_time=(index + 1) * 60_000 - 1,
    )


def event(index: int, generation: int = 0) -> MarketEvent:
    return MarketEvent(
        symbol="BTCUSDT",
        generation=generation,
        kline=kline(index),
        fear_greed=FearGreedContext(
            value=35,
            classification="Fear",
            average_30d=42.0,
            trend="falling",
            updated_at_ms=(index + 1) * 60_000 - 1,
        ),
    )


def actionable(item: Kline, fear_greed: FearGreedContext) -> Signal:
    return Signal(
        direction="LONG",
        timeframe_minutes=10,
        level="S",
        reason="shadow-optimizer-test",
        price=item.close,
        open_time=item.open_time,
        score=90.0,
        threshold=10.0,
        calculated_threshold=10.0,
        threshold_segment="WD-00",
        session_allowed=True,
        fear_greed_value=fear_greed.value,
        fear_greed_classification=fear_greed.classification,
        fear_greed_trend=fear_greed.trend,
        strategy_family="test",
        strategy_tag="optimizer",
    )


class ShadowOptimizerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "monitor.shadow.sqlite3"
        source = MonitorState(
            "BTCUSDT",
            max_open_orders=2,
            max_open_long_orders=2,
            max_open_short_orders=2,
            min_order_gap_ms=0,
            enable_daily_profile_selector=False,
            enable_observation_profile_promotion=False,
            strategy_build_id="shadow-optimizer-test",
        )
        source.seed_klines([kline(index) for index in range(20)])
        self.seed = source.shadow_runtime_seed()
        source.close()

    def _optimizer(self, *, created_at_ms=1_000):
        optimizer = ShadowOptimizer(
            seed=self.seed,
            store=ShadowSQLiteStore(self.path),
            max_challengers=2,
            created_at_ms=created_at_ms,
        )
        self.addCleanup(optimizer.close)
        return optimizer

    def _process_actionable(self, optimizer, item):
        def choose(klines, fear_greed=None):
            return actionable(klines[-1], fear_greed)

        def analyze(klines, timeframe_minutes, fear_greed=None):
            return actionable(klines[-1], fear_greed)

        with (
            patch("app.state.choose_trade_signal", side_effect=choose),
            patch("app.state.analyze_volume_price", side_effect=analyze),
            patch("app.state.analyze_observation_signals", return_value=[]),
        ):
            return optimizer.process_event(item)

    def test_creates_champion_and_bounded_challengers_with_full_parameter_snapshots(self):
        optimizer = self._optimizer()

        self.assertEqual(len(optimizer.arm_ids), 3)
        experiment = ShadowSQLiteStore(self.path).load_experiment(optimizer.experiment_id)
        self.assertEqual(len(experiment["arms"]), 3)
        self.assertEqual(
            sum(item["role"] == "CHAMPION" for item in experiment["arms"]),
            1,
        )
        snapshots = ShadowSQLiteStore(self.path).list_parameter_snapshots()
        self.assertEqual(len(snapshots), 3)
        self.assertTrue(
            all("runtime_config" in item["payload"]["parameters"] for item in snapshots)
        )

    def test_event_persists_decision_order_runtime_and_cursor_for_every_arm(self):
        optimizer = self._optimizer()
        first = event(20)

        self.assertEqual(self._process_actionable(optimizer, first), len(optimizer.arm_ids))

        store = ShadowSQLiteStore(self.path)
        for arm_id in optimizer.arm_ids:
            recovery = store.load_recovery_state(arm_id)
            self.assertEqual(recovery["cursor"]["last_event_id"], first.event_id)
            self.assertEqual(len(recovery["orders"]), 1)
            self.assertEqual(recovery["runtime_state"]["cursor_event_id"], first.event_id)
            decisions = store.list_decisions(arm_id)
            self.assertEqual(decisions[0]["decision"], "OPENED")
            self.assertIn("decision_inputs", decisions[0]["detail"])
            self.assertIn("parameter_hash", decisions[0]["detail"])

    def test_same_analyzer_hash_computes_raw_candidate_frame_only_once(self):
        optimizer = self._optimizer()
        choose_calls = []
        analyze_calls = []

        def choose(klines, fear_greed=None):
            choose_calls.append(klines[-1].open_time)
            return actionable(klines[-1], fear_greed)

        def analyze(klines, timeframe_minutes, fear_greed=None):
            analyze_calls.append((klines[-1].open_time, timeframe_minutes))
            return actionable(klines[-1], fear_greed)

        with (
            patch("app.state.choose_trade_signal", side_effect=choose),
            patch("app.state.analyze_volume_price", side_effect=analyze),
            patch("app.state.analyze_observation_signals", return_value=[]),
        ):
            self.assertEqual(optimizer.process_event(event(20)), 3)

        self.assertEqual(len(choose_calls), 1)
        self.assertEqual(len(analyze_calls), 1)

    def test_first_event_never_persists_warmup_observations_as_experiment_samples(self):
        historical = ObservationSignal(
            observation_key="historical-seed-observation",
            strategy_family="long_observe",
            strategy_tag="generic_long_observe",
            direction="LONG",
            timeframe_minutes=10,
            level="B",
            reason="warmup only",
            entry_price=100.0,
            opened_at=1,
            expires_at=2,
            threshold_segment="WD-00",
            status="SETTLED",
            result="WIN",
            exit_price=101.0,
            settled_at=2,
            pnl=8.0,
        )
        self.seed = {**self.seed, "observations": (historical,)}
        optimizer = self._optimizer()

        self._process_actionable(optimizer, event(20))

        store = ShadowSQLiteStore(self.path)
        for arm_id in optimizer.arm_ids:
            stored_keys = {
                row["observation_key"]
                for row in store.load_recovery_state(arm_id)["observations"]
            }
            self.assertNotIn("historical-seed-observation", stored_keys)

    def test_restart_restores_warmup_observations_without_persisting_one_copy_per_arm(self):
        historical = ObservationSignal(
            observation_key="historical-seed-observation",
            strategy_family="long_observe",
            strategy_tag="generic_long_observe",
            direction="LONG",
            timeframe_minutes=10,
            level="B",
            reason="warmup only",
            entry_price=100.0,
            opened_at=1,
            expires_at=2,
            threshold_segment="WD-00",
            status="SETTLED",
            result="WIN",
            exit_price=101.0,
            settled_at=2,
            pnl=8.0,
        )
        self.seed = {**self.seed, "observations": (historical,)}
        optimizer = self._optimizer(created_at_ms=1_000)
        first = event(20)
        self._process_actionable(optimizer, first)
        experiment_id = optimizer.experiment_id
        arm_ids = optimizer.arm_ids
        optimizer.close()

        restarted = ShadowOptimizer(
            seed={
                **self.seed,
                "klines": (*self.seed["klines"], first.kline),
            },
            store=ShadowSQLiteStore(self.path),
            max_challengers=2,
            created_at_ms=9_999,
        )
        self.addCleanup(restarted.close)

        self.assertEqual(restarted.experiment_id, experiment_id)
        for arm_id in arm_ids:
            observations = restarted.runtime(arm_id).state(arm_id).observations
            self.assertIn(
                "historical-seed-observation",
                {item.observation_key for item in observations},
            )

    def test_restart_persists_lifecycle_updates_for_experiment_open_observations(self):
        optimizer = self._optimizer(created_at_ms=1_000)
        first = event(20)
        self._process_actionable(optimizer, first)
        second = event(21)
        arm_ids = optimizer.arm_ids
        experiment_observation = ObservationSignal(
            observation_key="experiment-open-observation",
            strategy_family="long_observe",
            strategy_tag="generic_long_observe",
            direction="LONG",
            timeframe_minutes=10,
            level="B",
            reason="experiment observation",
            entry_price=100.0,
            opened_at=first.kline.open_time,
            expires_at=event(22).kline.close_time,
            threshold_segment="WD-00",
        )
        for arm_id in arm_ids:
            optimizer.runtime(arm_id).state(arm_id).observations.append(
                replace(experiment_observation)
            )
        self._process_actionable(optimizer, second)
        optimizer.close()

        restarted = ShadowOptimizer(
            seed={
                **self.seed,
                "klines": (*self.seed["klines"], first.kline, second.kline),
                "observations": (experiment_observation,),
            },
            store=ShadowSQLiteStore(self.path),
            max_challengers=2,
            created_at_ms=9_999,
        )
        self.addCleanup(restarted.close)

        self._process_actionable(restarted, event(22))

        store = ShadowSQLiteStore(self.path)
        for arm_id in arm_ids:
            rows = {
                row["observation_key"]: row
                for row in store.load_recovery_state(arm_id)["observations"]
            }
            self.assertEqual(rows["experiment-open-observation"]["status"], "SETTLED")
            self.assertIsNotNone(rows["experiment-open-observation"]["settled_at"])

    def test_duplicate_is_idempotent_and_gap_freezes_each_arm_with_audit(self):
        optimizer = self._optimizer()
        first = event(20)
        self.assertEqual(optimizer.process_event(first), len(optimizer.arm_ids))
        self.assertEqual(optimizer.process_event(first), 0)

        self.assertEqual(optimizer.process_event(event(22)), 0)

        store = ShadowSQLiteStore(self.path)
        for arm_id in optimizer.arm_ids:
            recovery = store.load_recovery_state(arm_id)
            self.assertEqual(recovery["gaps"][-1]["event_id"], event(22).event_id)
            self.assertTrue(recovery["runtime_state"]["invalid"])

    def test_restart_resumes_same_experiment_and_exact_arm_state(self):
        optimizer = self._optimizer(created_at_ms=1_000)
        first = event(20)
        self.assertEqual(self._process_actionable(optimizer, first), len(optimizer.arm_ids))
        experiment_id = optimizer.experiment_id
        arm_ids = optimizer.arm_ids
        optimizer.close()

        restart_seed = {
            **self.seed,
            "klines": (*self.seed["klines"], first.kline),
        }
        restarted = ShadowOptimizer(
            seed=restart_seed,
            store=ShadowSQLiteStore(self.path),
            max_challengers=2,
            created_at_ms=9_999,
        )
        self.addCleanup(restarted.close)

        self.assertEqual(restarted.experiment_id, experiment_id)
        self.assertEqual(restarted.arm_ids, arm_ids)
        self.assertEqual(restarted.status()["last_event_id"], first.event_id)
        self.assertEqual(restarted.process_event(event(21)), len(arm_ids))
        for arm_id in arm_ids:
            self.assertEqual(len(restarted.runtime(arm_id).orders(arm_id)), 1)

    def test_strict_evaluation_selects_best_candidate_and_activation_is_audited(self):
        optimizer = self._optimizer()
        champion_id, first_candidate, second_candidate = optimizer.arm_ids

        class FakeRuntime:
            def __init__(self, metrics, daily):
                self._metrics = metrics
                self._daily = daily
                self.cursor_event_id = "event-final"
                self.invalid = False

            def evaluation_metrics(self, _arm_id):
                return self._metrics

            def daily_statistics(self, _arm_id):
                return self._daily

            def metrics_since(self, _arm_id, _since_ms):
                return self._metrics

            def orders(self, _arm_id):
                return ()

            def close(self):
                return None

        def metrics(wins):
            return ShadowEvaluationMetrics(
                complete_days=7,
                settled_orders=350,
                wins=wins,
                long_orders=175,
                long_wins=wins // 2,
                short_orders=175,
                short_wins=wins - wins // 2,
                qualified_win_rate_days=7,
                positive_ev_days=7,
                days_beating_champion=0,
                average_orders_per_day=50.0,
                worst_rolling_3d_win_rate=0.58,
                total_ev=1.0,
                total_pnl=350.0,
                max_drawdown=20.0,
                max_loss_streak=3,
            )

        champion_days = tuple(
            {"day": f"2026-08-{day:02d}", "win_rate": 0.60}
            for day in range(14, 21)
        )
        better_days = tuple(
            {"day": f"2026-08-{day:02d}", "win_rate": 0.63}
            for day in range(14, 21)
        )
        best_days = tuple(
            {"day": f"2026-08-{day:02d}", "win_rate": 0.65}
            for day in range(14, 21)
        )
        optimizer._runtimes = {
            champion_id: FakeRuntime(metrics(210), champion_days),
            first_candidate: FakeRuntime(metrics(221), better_days),
            second_candidate: FakeRuntime(metrics(228), best_days),
        }

        evaluation = optimizer.evaluate(1_776_643_800_000)

        self.assertEqual(evaluation["status"], "READY")
        self.assertEqual(evaluation["selected_arm_id"], second_candidate)
        request = optimizer.activate_pending(1_776_644_400_000)
        self.assertEqual(request["type"], "PROMOTION_REQUEST")
        self.assertEqual(request["to_arm_id"], second_candidate)
        self.assertEqual(request["from_arm_id"], champion_id)
        self.assertEqual(request["symbol"], "BTCUSDT")
        self.assertEqual(request["generation"], 0)
        experiment = ShadowSQLiteStore(self.path).load_experiment(
            optimizer.experiment_id
        )
        self.assertEqual(experiment["champion_arm_id"], champion_id)
        self.assertEqual(optimizer.status()["champion_arm_id"], champion_id)
        self.assertEqual(ShadowSQLiteStore(self.path).list_lifecycle_history(), [])

        self.assertTrue(
            optimizer.resolve_lifecycle_request(
                request["request_id"],
                {
                    "status": "APPLIED",
                    "request_id": request["request_id"],
                    "policy_hash": optimizer._definition_by_arm[
                        second_candidate
                    ].policy.policy_hash,
                    "applied_at_ms": 1_776_644_400_000,
                },
            )
        )
        experiment = ShadowSQLiteStore(self.path).load_experiment(
            optimizer.experiment_id
        )
        self.assertEqual(experiment["champion_arm_id"], second_candidate)
        self.assertEqual(optimizer.status()["champion_arm_id"], second_candidate)
        self.assertEqual(optimizer.status()["latest_evaluation"]["status"], "READY")
        history = ShadowSQLiteStore(self.path).list_lifecycle_history()
        self.assertEqual(history[-1]["event_type"], "PROMOTION")

        self.assertIsNone(
            ShadowSupervisor(
                seed_supplier=lambda: self.seed,
                database_path=self.path,
                enabled=True,
                max_challengers=2,
            ).restored_policy_request()
        )
        ShadowSQLiteStore(self.path).save_formal_policy_receipt(
            request,
            {
                "status": "APPLIED",
                "request_id": request["request_id"],
                "policy_hash": optimizer._definition_by_arm[
                    second_candidate
                ].policy.policy_hash,
                "applied_at_ms": 1_776_644_400_000,
            },
        )
        restarted_seed = {**self.seed, "context": ("BTCUSDT", 99)}
        restored = ShadowSupervisor(
            seed_supplier=lambda: restarted_seed,
            database_path=self.path,
            enabled=True,
            max_challengers=2,
        ).restored_policy_request()
        self.assertEqual(restored["type"], "RESTORE_CHAMPION_REQUEST")
        self.assertEqual(restored["policy"], request["policy"])
        self.assertEqual(
            restored["expected_policy_hash"],
            request["expected_policy_hash"],
        )

    def test_promoted_champion_rolls_back_on_forward_loss_streak(self):
        optimizer = self._optimizer()
        champion_id, candidate_id, _ = optimizer.arm_ids

        class FakeRuntime:
            def __init__(self, full_metrics, forward_metrics):
                self._full_metrics = full_metrics
                self._forward_metrics = forward_metrics
                self.cursor_event_id = "event-final"
                self.invalid = False

            def evaluation_metrics(self, _arm_id):
                return self._full_metrics

            def daily_statistics(self, _arm_id):
                return tuple(
                    {
                        "day": f"2026-08-{day:02d}",
                        "win_rate": (
                            0.65 if self._full_metrics.wins > 210 else 0.60
                        ),
                    }
                    for day in range(14, 21)
                )

            def metrics_since(self, _arm_id, _since_ms):
                return self._forward_metrics

            def orders(self, _arm_id):
                return ()

            def close(self):
                return None

        champion_metrics = ShadowEvaluationMetrics(
            complete_days=7,
            settled_orders=350,
            wins=210,
            long_orders=175,
            long_wins=105,
            short_orders=175,
            short_wins=105,
            qualified_win_rate_days=7,
            positive_ev_days=7,
            average_orders_per_day=50.0,
            worst_rolling_3d_win_rate=0.60,
            total_ev=1.0,
            total_pnl=350.0,
            max_drawdown=20.0,
            max_loss_streak=3,
        )
        candidate_metrics = replace(
            champion_metrics,
            wins=228,
            long_wins=114,
            short_wins=114,
        )
        healthy_forward = ShadowEvaluationMetrics(
            settled_orders=8,
            wins=5,
            long_orders=4,
            long_wins=3,
            short_orders=4,
            short_wins=2,
            total_pnl=10.0,
            max_drawdown=10.0,
            max_loss_streak=2,
        )
        losing_forward = ShadowEvaluationMetrics(
            settled_orders=8,
            wins=0,
            long_orders=4,
            long_wins=0,
            short_orders=4,
            short_wins=0,
            total_pnl=-80.0,
            max_drawdown=80.0,
            max_loss_streak=8,
            current_loss_streak=8,
        )
        optimizer._runtimes[champion_id] = FakeRuntime(
            champion_metrics,
            healthy_forward,
        )
        optimizer._runtimes[candidate_id] = FakeRuntime(
            candidate_metrics,
            losing_forward,
        )
        third_id = optimizer.arm_ids[2]
        optimizer._runtimes[third_id] = FakeRuntime(
            champion_metrics,
            healthy_forward,
        )
        evaluation = optimizer.evaluate(1_776_643_800_000)
        self.assertEqual(evaluation["selected_arm_id"], candidate_id)
        optimizer.activate_pending(1_776_644_400_000)
        promotion_request = optimizer.status()["pending_lifecycle"]["request"]
        optimizer.resolve_lifecycle_request(
            promotion_request["request_id"],
            {
                "status": "APPLIED",
                "request_id": promotion_request["request_id"],
                "policy_hash": optimizer._definition_by_arm[
                    candidate_id
                ].policy.policy_hash,
                "applied_at_ms": 1_776_644_400_000,
            },
        )

        request = optimizer.evaluate_rollback_if_needed(1_776_644_460_000)

        self.assertEqual(request["type"], "ROLLBACK_REQUEST")
        self.assertEqual(request["from_arm_id"], candidate_id)
        self.assertEqual(request["to_arm_id"], champion_id)
        self.assertIn("consecutive_losses", request["reasons"])
        self.assertEqual(optimizer.status()["champion_arm_id"], candidate_id)
        optimizer.resolve_lifecycle_request(
            request["request_id"],
            {
                "status": "APPLIED",
                "request_id": request["request_id"],
                "policy_hash": optimizer._definition_by_arm[
                    champion_id
                ].policy.policy_hash,
                "applied_at_ms": 1_776_644_460_000,
            },
        )
        self.assertEqual(optimizer.status()["champion_arm_id"], champion_id)
        history = ShadowSQLiteStore(self.path).list_lifecycle_history()
        self.assertEqual(history[-1]["event_type"], "ROLLBACK")

    def test_rejected_promotion_does_not_change_shadow_champion(self):
        optimizer = self._optimizer()
        champion_id, candidate_id, _ = optimizer.arm_ids
        optimizer._pending_promotion = {
            "arm_id": candidate_id,
            "from_arm_id": champion_id,
            "evaluated_at_ms": 10_000,
            "metrics": ShadowEvaluationMetrics().to_dict(),
        }

        request = optimizer.activate_pending(20_000)
        resolved = optimizer.resolve_lifecycle_request(
            request["request_id"],
            {"status": "REJECTED_STALE_CHAMPION", "request_id": request["request_id"]},
        )

        self.assertTrue(resolved)
        self.assertEqual(optimizer.status()["champion_arm_id"], champion_id)
        self.assertIsNone(optimizer.status()["pending_lifecycle"])
        self.assertEqual(ShadowSQLiteStore(self.path).list_lifecycle_history(), [])

    def test_seed_ahead_starts_a_new_forward_generation_instead_of_freezing(self):
        optimizer = self._optimizer(created_at_ms=1_000)
        old_experiment = optimizer.experiment_id
        optimizer.process_event(event(20))
        optimizer.close()
        advanced_seed = {
            **self.seed,
            "klines": (*self.seed["klines"], kline(20), kline(21)),
        }

        restarted = ShadowOptimizer(
            seed=advanced_seed,
            store=ShadowSQLiteStore(self.path),
            max_challengers=2,
            created_at_ms=2_000,
        )
        self.addCleanup(restarted.close)

        self.assertNotEqual(restarted.experiment_id, old_experiment)
        self.assertNotIn(old_experiment, restarted.experiment_id)
        self.assertEqual(restarted.process_event(event(22)), 3)
        old = ShadowSQLiteStore(self.path).load_experiment(old_experiment)
        self.assertEqual(old["status"], "SUPERSEDED_SEED_ADVANCE")

    def test_evaluation_reports_gate_failures_without_promotion(self):
        optimizer = self._optimizer()
        result = optimizer.evaluate(1_776_643_800_000)

        self.assertEqual(result["status"], "INSUFFICIENT_SAMPLE")
        self.assertIsNone(optimizer.activate_pending(1_776_644_400_000))
        self.assertEqual(ShadowSQLiteStore(self.path).list_lifecycle_history(), [])
class ShadowLifecycleSchedulerTest(unittest.TestCase):
    def test_daily_tasks_run_once_and_publish_activation_request(self):
        class FakeOptimizer:
            def __init__(self):
                self.compactions = []
                self.evaluations = []
                self.activations = []

            def compact(self, timestamp):
                self.compactions.append(timestamp)
                return {"decisions": 2}

            def evaluate(self, timestamp):
                self.evaluations.append(timestamp)
                return {"status": "READY", "evaluated_at_ms": timestamp}

            def activate_pending(self, timestamp):
                self.activations.append(timestamp)
                return {"type": "PROMOTION_REQUEST", "effective_at_ms": timestamp}

        tz = timezone(timedelta(hours=8))

        def timestamp(hour, minute):
            return int(datetime(2026, 8, 21, hour, minute, tzinfo=tz).timestamp() * 1000)

        optimizer = FakeOptimizer()
        scheduler = ShadowLifecycleScheduler()

        self.assertEqual(scheduler.advance(optimizer, timestamp(7, 44)), ())
        first = scheduler.advance(optimizer, timestamp(7, 50))
        duplicate = scheduler.advance(optimizer, timestamp(7, 59))
        activation = scheduler.advance(optimizer, timestamp(8, 0))

        self.assertEqual(len(optimizer.compactions), 1)
        self.assertEqual(len(optimizer.evaluations), 1)
        self.assertEqual(len(optimizer.activations), 1)
        self.assertEqual(first[0]["type"], "EVALUATION")
        self.assertEqual(duplicate, ())
        self.assertEqual(activation[0]["type"], "PROMOTION_REQUEST")


if __name__ == "__main__":
    unittest.main()
