import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.models import ObservationSignal
from app.profile_admission import baseline_policy, candidate_policy
from app.shadow_storage import (
    COMPACT_ONLY_BYTES,
    CORE_RESERVE_BYTES,
    HARD_LIMIT_BYTES,
    WARNING_BYTES,
    ShadowSQLiteStore,
    ShadowStorageCapacity,
    ShadowStorageConflictError,
    classify_shadow_capacity,
)


def arm(arm_id: str, parameter_hash: str, role: str = "CHALLENGER") -> dict:
    return {
        "arm_id": arm_id,
        "parameter_hash": parameter_hash,
        "role": role,
        "status": "RUNNING",
        "effective_from_ms": 1_000,
    }


def open_order(order_id: int = 1) -> dict:
    return {
        "order_id": order_id,
        "decision_event_id": f"event-{order_id}",
        "direction": "LONG",
        "status": "OPEN",
        "opened_at_ms": 2_000,
        "expires_at_ms": 602_000,
        "entry_price": 100.0,
        "stake": 10.0,
        "detail": {"score": 81.5, "threshold": 78.0},
    }


def open_observation(observation_key: str = "observation-1") -> dict:
    return {
        "observation_key": observation_key,
        "decision_event_id": "event-1",
        "strategy_family": "generic_short_observe",
        "strategy_tag": "short_observe",
        "direction": "SHORT",
        "timeframe_minutes": 10,
        "level": "B",
        "reason": "WD-08 candidate",
        "entry_price": 100.0,
        "opened_at": 2_000,
        "expires_at": 602_000,
        "threshold_segment": "WD-08",
        "status": "OPEN",
        "result": None,
        "exit_price": None,
        "settled_at": None,
        "pnl": 0.0,
        "profile_key": "10|generic_short_observe|short_observe|SHORT|WD-08",
        "detail": {
            "score": 81.5,
            "threshold": 78.0,
            "edge": 2.0,
            "regime": "FEAR_FLAT",
            "decision_id": "decision-1",
        },
    }


class ShadowSQLiteStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "monitor.shadow.sqlite3"
        self.store = ShadowSQLiteStore(self.path)
        self.store.save_parameter_snapshot(
            parameter_hash="p-champion",
            analyzer_hash="a1",
            parameter_family="PROFILE_ADMISSION",
            payload={
                "parameters": {
                    "profile_admission_policy": baseline_policy().to_dict(),
                }
            },
            created_at_ms=100,
        )
        self.store.save_parameter_snapshot(
            parameter_hash="p-challenger",
            analyzer_hash="a1",
            parameter_family="PROFILE_ADMISSION",
            payload={
                "parameters": {
                    "profile_admission_policy": candidate_policy().to_dict(),
                }
            },
            created_at_ms=101,
        )
        self.store.create_experiment(
            experiment_id="exp-1",
            symbol="BTCUSDT",
            parameter_family="PROFILE_ADMISSION",
            generation=1,
            created_at_ms=200,
            arms=(
                arm("champion", "p-champion", "CHAMPION"),
                arm("challenger", "p-challenger"),
            ),
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_parameter_snapshot_is_idempotent_but_hash_conflict_is_rejected(self):
        self.store.save_parameter_snapshot(
            parameter_hash="p-champion",
            analyzer_hash="a1",
            parameter_family="PROFILE_ADMISSION",
            payload={
                "parameters": {
                    "profile_admission_policy": baseline_policy().to_dict(),
                }
            },
            created_at_ms=999,
        )
        with self.assertRaises(ShadowStorageConflictError):
            self.store.save_parameter_snapshot(
                parameter_hash="p-champion",
                analyzer_hash="a1",
                parameter_family="PROFILE_ADMISSION",
                payload={
                    "parameters": {
                        "profile_admission_policy": candidate_policy().to_dict(),
                    }
                },
                created_at_ms=999,
            )
        self.assertEqual(len(self.store.list_parameter_snapshots()), 2)

    def test_formal_policy_receipt_is_validated_idempotent_and_immutable(self):
        request = {
            "request_id": "promotion-1",
            "type": "PROMOTION_REQUEST",
            "experiment_id": "exp-1",
            "from_arm_id": "challenger",
            "to_arm_id": "champion",
            "parameter_hash": "p-champion",
            "symbol": "BTCUSDT",
            "generation": 1,
            "policy": baseline_policy().to_dict(),
        }
        result = {
            "status": "APPLIED",
            "request_id": "promotion-1",
            "policy_hash": baseline_policy().policy_hash,
            "applied_at_ms": 4_000,
        }

        self.store.save_formal_policy_receipt(request, result)
        self.store.save_formal_policy_receipt(request, result)

        receipt = self.store.load_latest_formal_policy_receipt("BTCUSDT")
        self.assertEqual(receipt["request_id"], "promotion-1")
        self.assertEqual(receipt["action_type"], "PROMOTION")
        self.assertEqual(receipt["parameter_hash"], "p-champion")
        self.assertEqual(receipt["policy"], baseline_policy().to_dict())

        with self.assertRaises(ShadowStorageConflictError):
            self.store.save_formal_policy_receipt(
                request,
                {**result, "applied_at_ms": 4_001},
            )

    def test_formal_policy_receipt_rejects_non_applied_result(self):
        with self.assertRaises(ValueError):
            self.store.save_formal_policy_receipt(
                {
                    "request_id": "promotion-rejected",
                    "type": "PROMOTION_REQUEST",
                    "experiment_id": "exp-1",
                    "to_arm_id": "champion",
                    "parameter_hash": "p-champion",
                    "symbol": "BTCUSDT",
                    "generation": 1,
                    "policy": baseline_policy().to_dict(),
                },
                {"status": "WAITING_OPEN_ORDERS"},
            )

    def test_create_experiment_and_arms_is_atomic(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create_experiment(
                experiment_id="exp-bad",
                symbol="ETHUSDT",
                parameter_family="PROFILE_ADMISSION",
                generation=2,
                created_at_ms=300,
                arms=(arm("duplicate", "p-champion"), arm("duplicate", "p-challenger")),
            )
        self.assertIsNone(self.store.load_experiment("exp-bad"))

    def test_experiment_and_arm_ids_are_idempotent_only_for_identical_definition(self):
        self.store.create_experiment(
            experiment_id="exp-1",
            symbol="BTCUSDT",
            parameter_family="PROFILE_ADMISSION",
            generation=1,
            created_at_ms=200,
            arms=(
                arm("champion", "p-champion", "CHAMPION"),
                arm("challenger", "p-challenger"),
            ),
        )
        self.assertEqual(len(self.store.load_experiment("exp-1")["arms"]), 2)

        with self.assertRaises(ShadowStorageConflictError):
            self.store.create_experiment(
                experiment_id="exp-2",
                symbol="ETHUSDT",
                parameter_family="PROFILE_ADMISSION",
                generation=2,
                created_at_ms=300,
                arms=(arm("challenger", "p-challenger"),),
            )
        self.assertIsNone(self.store.load_experiment("exp-2"))

    def test_find_latest_running_experiment_returns_descending_arms(self):
        self.store.create_experiment(
            experiment_id="exp-new-running",
            symbol="BTCUSDT",
            parameter_family="PROFILE_ADMISSION",
            generation=2,
            created_at_ms=400,
            arms=(
                {
                    **arm("newer-arm", "p-challenger"),
                    "created_at_ms": 420,
                },
                {
                    **arm("older-arm", "p-champion", "CHAMPION"),
                    "created_at_ms": 410,
                },
            ),
        )
        self.store.create_experiment(
            experiment_id="exp-later-completed",
            symbol="BTCUSDT",
            parameter_family="PROFILE_ADMISSION",
            generation=3,
            created_at_ms=500,
            status="COMPLETED",
            arms=(arm("completed-arm", "p-champion", "CHAMPION"),),
        )

        experiment = self.store.find_latest_running_experiment(
            "BTCUSDT", "PROFILE_ADMISSION"
        )
        self.assertEqual(experiment["experiment_id"], "exp-new-running")
        self.assertEqual(
            [item["arm_id"] for item in experiment["arms"]],
            ["newer-arm", "older-arm"],
        )
        self.assertIsNone(
            self.store.find_latest_running_experiment(
                "ETHUSDT", "PROFILE_ADMISSION"
            )
        )

    def test_event_bundle_is_atomic_and_recoverable(self):
        self.store.save_event_bundle(
            arm_id="challenger",
            event_id="event-1",
            closed_at_ms=2_000,
            decisions=(
                {
                    "decision": "OPEN",
                    "direction": "LONG",
                    "profile_key": "WD-08",
                    "detail": {"score": 81.5},
                    "terminal_at_ms": None,
                },
            ),
            orders=(open_order(),),
            observations=(open_observation(),),
            runtime_state={"loss_streak": 0, "next_order_id": 2},
            daily_rollup={"day": "2026-08-21", "orders": 1, "wins": 0},
        )

        recovery = self.store.load_recovery_state("challenger")
        self.assertEqual(recovery["cursor"]["last_event_id"], "event-1")
        self.assertEqual(recovery["cursor"]["last_closed_at_ms"], 2_000)
        self.assertEqual(recovery["runtime_state"]["next_order_id"], 2)
        self.assertEqual([row["order_id"] for row in recovery["orders"]], [1])
        self.assertEqual(
            [row["observation_key"] for row in recovery["observations"]],
            ["observation-1"],
        )
        restored = recovery["observations"][0]
        model_payload = {
            key: restored[key]
            for key in (
                "observation_key",
                "strategy_family",
                "strategy_tag",
                "direction",
                "timeframe_minutes",
                "level",
                "reason",
                "entry_price",
                "opened_at",
                "expires_at",
                "threshold_segment",
                "status",
                "result",
                "exit_price",
                "settled_at",
                "pnl",
                "profile_key",
            )
        }
        model_payload.update(restored["detail"])
        rebuilt = ObservationSignal(**model_payload)
        self.assertEqual(rebuilt.observation_key, "observation-1")
        self.assertEqual(rebuilt.decision_id, "decision-1")
        self.assertEqual(recovery["daily_rollups"][0]["orders"], 1)

    def test_shadow_orders_use_true_sqlite_pagination(self):
        for order_id in range(1, 4):
            self.store.save_event_bundle(
                arm_id="challenger",
                event_id=f"event-page-{order_id}",
                closed_at_ms=order_id * 60_000,
                orders=(open_order(order_id),),
                runtime_state={"next_order_id": order_id + 1},
            )

        first = self.store.page_orders("challenger", page=1, page_size=2)
        second = self.store.page_orders("challenger", page=2, page_size=2)

        self.assertEqual(first["total"], 3)
        self.assertEqual(first["total_pages"], 2)
        self.assertEqual([row["order_id"] for row in first["orders"]], [3, 2])
        self.assertEqual([row["order_id"] for row in second["orders"]], [1])

    def test_observation_lifecycle_is_idempotent_and_rejects_conflicts(self):
        opened = open_observation()
        self.store.save_event_bundle(
            arm_id="challenger",
            event_id="event-observation-open",
            closed_at_ms=2_000,
            observations=(opened,),
        )
        settled = {
            **opened,
            "status": "SETTLED",
            "result": "WIN",
            "exit_price": 99.0,
            "settled_at": 602_000,
            "pnl": 8.0,
        }
        self.store.save_event_bundle(
            arm_id="challenger",
            event_id="event-observation-settle",
            closed_at_ms=602_000,
            observations=(settled,),
        )
        self.store.save_event_bundle(
            arm_id="challenger",
            event_id="event-observation-settle-replay",
            closed_at_ms=603_000,
            observations=(settled,),
        )

        observation = self.store.list_observations("challenger")[0]
        self.assertEqual(observation["status"], "SETTLED")
        self.assertEqual(observation["result"], "WIN")
        self.assertEqual(observation["detail"]["decision_id"], "decision-1")

        with self.assertRaises(ShadowStorageConflictError):
            self.store.save_event_bundle(
                arm_id="challenger",
                event_id="event-observation-conflict",
                closed_at_ms=604_000,
                observations=({**settled, "result": "LOSS", "pnl": -10.0},),
            )
        with self.assertRaises(ShadowStorageConflictError):
            self.store.save_event_bundle(
                arm_id="challenger",
                event_id="event-observation-reopen",
                closed_at_ms=605_000,
                observations=(opened,),
            )
        self.assertEqual(
            self.store.load_recovery_state("challenger")["cursor"]["last_event_id"],
            "event-observation-settle-replay",
        )

    def test_invalid_observation_lifecycle_rolls_back_entire_bundle(self):
        with self.assertRaises(ValueError):
            self.store.save_event_bundle(
                arm_id="challenger",
                event_id="event-invalid-observation",
                closed_at_ms=2_000,
                decisions=({"decision": "OPEN", "detail": {}},),
                orders=(open_order(),),
                observations=(
                    {
                        **open_observation(),
                        "result": "WIN",
                        "settled_at": 2_000,
                    },
                ),
                runtime_state={"next_order_id": 2},
            )
        recovery = self.store.load_recovery_state("challenger")
        self.assertIsNone(recovery["cursor"])
        self.assertEqual(recovery["orders"], [])
        self.assertEqual(recovery["observations"], [])
        self.assertEqual(self.store.list_decisions("challenger"), [])

    def test_failure_after_observations_rolls_back_observations_and_cursor(self):
        def fail_after_observations(step: str) -> None:
            if step == "observations":
                raise RuntimeError("injected observation failure")

        self.store._bundle_step_hook = fail_after_observations
        with self.assertRaisesRegex(RuntimeError, "injected observation failure"):
            self.store.save_event_bundle(
                arm_id="challenger",
                event_id="event-observation-failure",
                closed_at_ms=2_000,
                orders=(open_order(),),
                observations=(open_observation(),),
            )
        recovery = self.store.load_recovery_state("challenger")
        self.assertIsNone(recovery["cursor"])
        self.assertEqual(recovery["orders"], [])
        self.assertEqual(recovery["observations"], [])

    def test_same_arm_event_is_idempotent_and_conflicting_replay_is_rejected(self):
        kwargs = dict(
            arm_id="challenger",
            event_id="event-1",
            closed_at_ms=2_000,
            decisions=({"decision": "WAIT", "detail": {"reason": "LOW_SCORE"}},),
            runtime_state={"loss_streak": 0},
        )
        self.store.save_event_bundle(**kwargs)
        self.store.save_event_bundle(**kwargs)
        self.assertEqual(len(self.store.list_decisions("challenger")), 1)

        with self.assertRaises(ShadowStorageConflictError):
            self.store.save_event_bundle(
                **{**kwargs, "runtime_state": {"loss_streak": 1}}
            )
        self.assertEqual(
            self.store.load_recovery_state("challenger")["runtime_state"],
            {"loss_streak": 0},
        )

    def test_bundle_failure_does_not_advance_cursor_or_leave_partial_rows(self):
        def fail_after_orders(step: str) -> None:
            if step == "orders":
                raise RuntimeError("injected failure")

        self.store._bundle_step_hook = fail_after_orders
        with self.assertRaisesRegex(RuntimeError, "injected failure"):
            self.store.save_event_bundle(
                arm_id="challenger",
                event_id="event-2",
                closed_at_ms=3_000,
                decisions=({"decision": "OPEN", "detail": {}},),
                orders=(open_order(2),),
                runtime_state={"next_order_id": 3},
            )
        recovery = self.store.load_recovery_state("challenger")
        self.assertIsNone(recovery["cursor"])
        self.assertEqual(recovery["orders"], [])
        self.assertEqual(self.store.list_decisions("challenger"), [])

    def test_gap_is_audited_and_recovered_with_cursor_atomically(self):
        self.store.save_event_bundle(
            arm_id="challenger",
            event_id="event-gap",
            closed_at_ms=4_000,
            gap={"reason": "QUEUE_FULL", "detected_at_ms": 4_100},
            runtime_state={"status": "FROZEN"},
        )
        recovery = self.store.load_recovery_state("challenger")
        self.assertEqual(recovery["cursor"]["gap_count"], 1)
        self.assertEqual(recovery["gaps"][0]["event_id"], "event-gap")
        self.assertEqual(recovery["gaps"][0]["reason"], "QUEUE_FULL")

    def test_evaluation_and_promotion_history_commit_as_one_bundle(self):
        self.store.save_evaluation_bundle(
            evaluation_id="eval-1",
            experiment_id="exp-1",
            arm_id="challenger",
            evaluated_at_ms=10_000,
            decision="PROMOTE",
            metrics={"orders": 330, "win_rate": 0.62},
            lifecycle_event={
                "history_id": "promotion-1",
                "event_type": "PROMOTION",
                "from_arm_id": "champion",
                "to_arm_id": "challenger",
                "effective_at_ms": 11_000,
                "reason": "ALL_GATES_PASSED",
                "evidence": {"evaluation_id": "eval-1"},
            },
            arm_status="PROMOTED",
        )
        experiment = self.store.load_experiment("exp-1")
        self.assertEqual(experiment["arms"][1]["status"], "PROMOTED")
        self.assertEqual(experiment["champion_arm_id"], "challenger")
        self.assertEqual(self.store.list_evaluations("challenger")[0]["decision"], "PROMOTE")
        self.assertEqual(self.store.list_lifecycle_history()[0]["event_type"], "PROMOTION")

    def test_evaluation_bundle_with_unknown_arm_rolls_back(self):
        with self.assertRaises(ShadowStorageConflictError):
            self.store.save_evaluation_bundle(
                evaluation_id="eval-unknown",
                experiment_id="exp-1",
                arm_id="missing-arm",
                evaluated_at_ms=10_000,
                decision="INVALID",
                metrics={"orders": 0},
                arm_status="INVALID",
            )
        self.assertEqual(self.store.list_evaluations("missing-arm"), [])

    def test_evaluation_idempotency_covers_lifecycle_action(self):
        base = dict(
            evaluation_id="eval-stable",
            experiment_id="exp-1",
            arm_id="challenger",
            evaluated_at_ms=10_000,
            decision="ELIGIBLE",
            metrics={"orders": 330, "win_rate": 0.62},
        )
        self.store.save_evaluation_bundle(**base)
        self.store.save_evaluation_bundle(**base)
        with self.assertRaises(ShadowStorageConflictError):
            self.store.save_evaluation_bundle(
                **base,
                lifecycle_event={
                    "history_id": "late-promotion",
                    "event_type": "PROMOTION",
                    "from_arm_id": "champion",
                    "to_arm_id": "challenger",
                    "effective_at_ms": 11_000,
                    "reason": "CHANGED_AFTER_COMMIT",
                    "evidence": {},
                },
            )
        self.assertEqual(self.store.list_lifecycle_history(), [])

    def test_order_lifecycle_fields_must_be_consistent(self):
        invalid_open = {
            **open_order(),
            "result": "WIN",
            "settled_at_ms": 3_000,
            "exit_price": 101.0,
            "pnl": 8.0,
        }
        with self.assertRaises(ValueError):
            self.store.save_event_bundle(
                arm_id="challenger",
                event_id="event-invalid-open",
                closed_at_ms=2_000,
                orders=(invalid_open,),
            )
        invalid_settled = {**open_order(), "status": "SETTLED"}
        with self.assertRaises(ValueError):
            self.store.save_event_bundle(
                arm_id="challenger",
                event_id="event-invalid-settled",
                closed_at_ms=2_000,
                orders=(invalid_settled,),
            )
        self.assertIsNone(self.store.load_recovery_state("challenger")["cursor"])

    def test_recovery_keeps_settled_orders_and_runtime_audit(self):
        order = open_order()
        self.store.save_event_bundle(
            arm_id="challenger",
            event_id="event-1",
            closed_at_ms=2_000,
            decisions=({"decision": "OPEN", "detail": {"score": 81.5}},),
            orders=(order,),
            runtime_state={"next_order_id": 2},
        )
        settled = {
            **order,
            "status": "SETTLED",
            "result": "WIN",
            "settled_at_ms": 602_000,
            "exit_price": 101.0,
            "pnl": 8.0,
        }
        self.store.save_event_bundle(
            arm_id="challenger",
            event_id="event-2",
            closed_at_ms=602_000,
            orders=(settled,),
            runtime_state={"next_order_id": 2, "loss_streak": 0},
        )
        recovery = self.store.load_recovery_state("challenger")
        self.assertEqual(len(recovery["orders"]), 1)
        self.assertEqual(recovery["orders"][0]["status"], "SETTLED")
        self.assertEqual(recovery["runtime_state"]["next_order_id"], 2)
        self.assertEqual(self.store.list_orders("challenger")[0]["result"], "WIN")

    def test_compaction_only_removes_terminal_detail_and_preserves_core_records(self):
        self.store.save_event_bundle(
            arm_id="challenger",
            event_id="event-1",
            closed_at_ms=2_000,
            decisions=(
                {
                    "decision": "WAIT",
                    "direction": "LONG",
                    "profile_key": "WD-01",
                    "detail": {
                        "large": "x" * 50,
                        "score": 63.5,
                        "threshold": 71.0,
                        "first_decisive_block": "BELOW_THRESHOLD",
                    },
                    "terminal_at_ms": 2_000,
                },
                {
                    "decision": "BLOCKED",
                    "retain_at_capacity": True,
                    "detail": {"reason": "RESULT_SEQUENCE"},
                    "terminal_at_ms": 2_000,
                },
            ),
            orders=(open_order(),),
            observations=(open_observation(),),
            runtime_state={"next_order_id": 2},
        )
        settled = {
            **open_order(),
            "status": "SETTLED",
            "result": "LOSS",
            "settled_at_ms": 602_000,
            "exit_price": 99.0,
            "pnl": -10.0,
        }
        self.store.save_event_bundle(
            arm_id="challenger",
            event_id="event-2",
            closed_at_ms=602_000,
            orders=(settled, open_order(2)),
            observations=(
                {
                    **open_observation(),
                    "status": "SETTLED",
                    "result": "LOSS",
                    "exit_price": 101.0,
                    "settled_at": 602_000,
                    "pnl": -10.0,
                },
                open_observation("observation-2"),
            ),
            gap={"reason": "SOURCE_GAP", "detected_at_ms": 602_100},
            runtime_state={"next_order_id": 3},
        )
        result = self.store.compact_terminal_details(before_ms=700_000, limit=100)
        decisions = self.store.list_decisions("challenger")
        orders = self.store.list_orders("challenger")
        observations = self.store.list_observations("challenger")
        decision_rollups = self.store.list_decision_rollups("challenger")
        recovery = self.store.load_recovery_state("challenger")

        self.assertEqual(result, {"decisions": 1, "observations": 1, "orders": 0})
        self.assertIsNone(decisions[0]["detail"])
        self.assertEqual(decisions[1]["detail"], {"reason": "RESULT_SEQUENCE"})
        self.assertIsNotNone(orders[0]["detail"])
        self.assertIsNotNone(orders[1]["detail"])
        self.assertIsNone(observations[0]["detail"])
        self.assertIsNotNone(observations[1]["detail"])
        self.assertEqual([row["order_id"] for row in recovery["orders"]], [1, 2])
        self.assertEqual(
            [row["observation_key"] for row in recovery["observations"]],
            ["observation-1", "observation-2"],
        )
        self.assertEqual(len(recovery["gaps"]), 1)
        self.assertEqual(len(self.store.list_lifecycle_history()), 0)
        self.assertEqual(
            decision_rollups,
            [
                {
                    "arm_id": "challenger",
                    "trading_day": "1970-01-01",
                    "decision": "WAIT",
                    "direction": "LONG",
                    "profile_key": "WD-01",
                    "first_decisive_block": "BELOW_THRESHOLD",
                    "occurrences": 1,
                    "minimum_score": 63.5,
                    "maximum_score": 63.5,
                    "minimum_threshold": 71.0,
                    "maximum_threshold": 71.0,
                    "updated_at_ms": 700_000,
                }
            ],
        )

        self.store.save_event_bundle(
            arm_id="challenger",
            event_id="event-3",
            closed_at_ms=603_000,
            decisions=(
                {
                    "decision": "WAIT",
                    "direction": "LONG",
                    "profile_key": "WD-01",
                    "detail": {
                        "score": 64.0,
                        "threshold": 72.0,
                        "first_decisive_block": "BELOW_THRESHOLD",
                    },
                    "terminal_at_ms": 603_000,
                },
            ),
            runtime_state={"next_order_id": 3},
        )
        self.store.compact_terminal_details(
            before_ms=700_000,
            limit=100,
            compacted_at_ms=701_000,
        )
        merged_rollup = self.store.list_decision_rollups("challenger")[0]
        self.assertEqual(merged_rollup["occurrences"], 2)
        self.assertEqual(merged_rollup["minimum_score"], 63.5)
        self.assertEqual(merged_rollup["maximum_score"], 64.0)
        self.assertEqual(merged_rollup["minimum_threshold"], 71.0)
        self.assertEqual(merged_rollup["maximum_threshold"], 72.0)
        self.assertEqual(merged_rollup["updated_at_ms"], 701_000)

    def test_capacity_boundaries_and_database_page_cap_are_exact(self):
        self.assertEqual(HARD_LIMIT_BYTES, 5 * 1024**3)
        self.assertEqual(WARNING_BYTES, 4 * 1024**3)
        self.assertEqual(COMPACT_ONLY_BYTES, int(4.5 * 1024**3))
        self.assertEqual(CORE_RESERVE_BYTES, 512 * 1024**2)
        cases = (
            (WARNING_BYTES - 1, "NORMAL"),
            (WARNING_BYTES, "WARNING"),
            (COMPACT_ONLY_BYTES, "COMPACT_ONLY"),
            (HARD_LIMIT_BYTES, "HARD_LIMIT"),
        )
        for size, expected in cases:
            self.assertEqual(classify_shadow_capacity(size), expected)

        with self.store._connect() as connection:
            page_size = connection.execute("pragma page_size").fetchone()[0]
            max_pages = connection.execute("pragma max_page_count").fetchone()[0]
            auto_vacuum = connection.execute("pragma auto_vacuum").fetchone()[0]
        self.assertEqual(max_pages, HARD_LIMIT_BYTES // page_size)
        self.assertEqual(auto_vacuum, 2)

    def test_hard_limit_keeps_core_ledger_writable_and_allows_compaction(self):
        self.store.save_event_bundle(
            arm_id="challenger",
            event_id="event-terminal",
            closed_at_ms=2_000,
            decisions=(
                {"decision": "WAIT", "detail": {"large": "x" * 50}, "terminal_at_ms": 2_000},
            ),
        )
        hard_limit = ShadowStorageCapacity(
            status="HARD_LIMIT",
            database_bytes=HARD_LIMIT_BYTES,
        )
        with mock.patch.object(
            self.store,
            "_capacity_from_connection",
            return_value=hard_limit,
        ):
            self.store.save_event_bundle(
                arm_id="challenger",
                event_id="event-core-only",
                closed_at_ms=3_000,
            )
            self.assertEqual(
                self.store.compact_terminal_details(before_ms=3_000),
                {"decisions": 1, "observations": 0, "orders": 0},
            )
        self.assertIsNone(self.store.list_decisions("challenger")[0]["detail"])
        self.assertEqual(
            self.store.load_recovery_state("challenger")["cursor"]["last_event_id"],
            "event-core-only",
        )

    def test_compact_only_stops_routine_wait_details_but_keeps_decisive_events(self):
        compact_only = ShadowStorageCapacity(
            status="COMPACT_ONLY",
            database_bytes=COMPACT_ONLY_BYTES,
        )
        with mock.patch.object(
            self.store,
            "_capacity_from_connection",
            return_value=compact_only,
        ):
            self.store.save_event_bundle(
                arm_id="challenger",
                event_id="event-compact",
                closed_at_ms=8_000,
                decisions=(
                    {
                        "decision": "WAIT",
                        "detail": {"large": "x" * 100},
                        "terminal_at_ms": 8_000,
                    },
                    {
                        "decision": "BLOCKED",
                        "retain_at_capacity": True,
                        "detail": {"reason": "RESULT_SEQUENCE"},
                        "terminal_at_ms": 8_000,
                    },
                ),
                orders=(open_order(3),),
                runtime_state={"status": "RUNNING"},
                daily_rollup={"day": "2026-08-21", "orders": 1},
            )

        decisions = self.store.list_decisions("challenger")
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["decision"], "BLOCKED")
        self.assertEqual(decisions[0]["detail"], {"reason": "RESULT_SEQUENCE"})
        self.assertEqual(self.store.list_orders("challenger")[0]["order_id"], 3)
        self.assertEqual(
            self.store.load_recovery_state("challenger")["cursor"]["last_event_id"],
            "event-compact",
        )


if __name__ == "__main__":
    unittest.main()
