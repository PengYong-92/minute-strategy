import threading
import unittest

from app.models import FearGreedContext, Kline, Signal
from app.profile_admission import baseline_policy, candidate_policy
from app.state import MonitorState


def kline(index: int, close: float = 100.0) -> Kline:
    return Kline(
        open_time=index * 60_000,
        open=close - 1.0,
        high=close + 1.0,
        low=close - 2.0,
        close=close,
        volume=10.0,
        close_time=(index + 1) * 60_000 - 1,
    )


class ShadowStateBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.state = MonitorState("BTCUSDT", strategy_build_id="test-build")
        self.addCleanup(self.state.close)

    def test_runtime_seed_contains_complete_constructor_and_detached_history(self):
        self.state.seed_klines([kline(0), kline(1)])

        seed = self.state.shadow_runtime_seed()

        self.assertEqual(seed["symbol"], "BTCUSDT")
        self.assertEqual(seed["context"], ("BTCUSDT", 0))
        self.assertEqual(seed["constructor"]["max_open_orders"], 2)
        self.assertEqual(seed["constructor"]["max_open_long_orders"], 1)
        self.assertEqual(seed["constructor"]["max_open_short_orders"], 2)
        self.assertEqual(seed["constructor"]["strategy_build_id"], "test-build")
        self.assertEqual(seed["klines"], (kline(0), kline(1)))
        self.assertEqual(seed["profile_admission_policy"]["version"], "PROFILE_ADMISSION_V1")
        self.assertIn("canonical_payload", seed["runtime_config"])

        seed["constructor"]["max_open_orders"] = 99
        self.assertEqual(self.state.order_policy.max_open_orders, 2)

    def test_market_context_is_frozen_and_rejects_stale_symbol_generation(self):
        self.state.fear_greed = FearGreedContext(
            value=31,
            classification="Fear",
            average_30d=40.0,
            trend="falling",
            updated_at_ms=123,
        )

        snapshot = self.state.shadow_market_context(expected_context=("BTCUSDT", 0))
        stale = self.state.shadow_market_context(expected_context=("ETHUSDT", 1))

        self.assertEqual(snapshot["value"], 31)
        self.assertEqual(snapshot["trend"], "falling")
        self.assertIsNone(stale)

    def test_profile_admission_promotion_is_atomic_and_changes_runtime_hash(self):
        before = self.state.shadow_runtime_seed()["runtime_config"]["hash"]

        applied = self.state.apply_profile_admission_policy(candidate_policy())

        after = self.state.shadow_runtime_seed()["runtime_config"]["hash"]
        self.assertTrue(applied)
        self.assertEqual(
            self.state.profile_admission_policy.policy_hash,
            candidate_policy().policy_hash,
        )
        self.assertNotEqual(before, after)

    def test_shadow_promotion_request_waits_for_open_orders_but_has_ten_minute_deadline(self):
        recorded = []
        completed = threading.Event()

        def record(request, result):
            recorded.append((request, result))
            completed.set()

        self.state.attach_shadow_policy_audit_sink(record)
        self.state.simulator.open_order(
            Signal(
                direction="LONG",
                timeframe_minutes=10,
                level="B",
                reason="pending-policy-test",
                price=100.0,
                open_time=1_000,
                score=80.0,
                threshold=70.0,
                calculated_threshold=70.0,
                threshold_segment="WD-00",
            ),
            100.0,
            1_000,
        )
        request = {
            "request_id": "promotion-1",
            "type": "PROMOTION_REQUEST",
            "experiment_id": "experiment-1",
            "from_arm_id": "champion-arm",
            "to_arm_id": "challenger-arm",
            "parameter_hash": "parameter-1",
            "symbol": "BTCUSDT",
            "generation": 0,
            "policy": candidate_policy().to_dict(),
            "expected_policy_hash": baseline_policy().policy_hash,
            "effective_at_ms": 10_000,
        }

        queued = self.state.request_shadow_policy_change(request)
        before_deadline = self.state.apply_pending_shadow_policy_change(609_999)
        at_deadline = self.state.apply_pending_shadow_policy_change(610_000)

        self.assertEqual(queued["status"], "WAITING_OPEN_ORDERS")
        self.assertEqual(before_deadline["status"], "WAITING_OPEN_ORDERS")
        self.assertEqual(at_deadline["status"], "APPLIED_AT_DEADLINE")
        self.assertEqual(
            self.state.profile_admission_policy.policy_hash,
            candidate_policy().policy_hash,
        )
        self.assertTrue(completed.wait(1.0))
        self.assertEqual(recorded[0][0]["experiment_id"], "experiment-1")
        self.assertEqual(recorded[0][0]["parameter_hash"], "parameter-1")
        self.assertEqual(recorded[0][1]["status"], "APPLIED_AT_DEADLINE")

    def test_formal_policy_audit_sink_does_not_block_immediate_application(self):
        sink_started = threading.Event()
        sink_release = threading.Event()

        def record(_request, _result):
            sink_started.set()
            sink_release.wait(1.0)

        self.state.attach_shadow_policy_audit_sink(record)
        started = threading.Event()

        def apply():
            self.state.request_shadow_policy_change(
                {
                    "request_id": "promotion-async",
                    "type": "PROMOTION_REQUEST",
                    "experiment_id": "experiment-1",
                    "to_arm_id": "challenger-arm",
                    "parameter_hash": "parameter-1",
                    "symbol": "BTCUSDT",
                    "generation": 0,
                    "policy": candidate_policy().to_dict(),
                    "expected_policy_hash": baseline_policy().policy_hash,
                    "effective_at_ms": 10_000,
                }
            )
            started.set()

        thread = threading.Thread(target=apply)
        thread.start()
        self.assertTrue(started.wait(0.5))
        self.assertTrue(sink_started.wait(0.5))
        sink_release.set()
        thread.join(1.0)

    def test_lifecycle_request_for_previous_symbol_context_is_rejected(self):
        self.state.reset_symbol("ETHUSDT")

        result = self.state.request_shadow_policy_change(
            {
                "request_id": "stale-symbol-request",
                "type": "PROMOTION_REQUEST",
                "experiment_id": "experiment-btc",
                "to_arm_id": "challenger-arm",
                "parameter_hash": "parameter-1",
                "symbol": "BTCUSDT",
                "generation": 0,
                "policy": candidate_policy().to_dict(),
                "expected_policy_hash": baseline_policy().policy_hash,
                "effective_at_ms": 10_000,
            }
        )

        self.assertEqual(result["status"], "REJECTED_CONTEXT")
        self.assertEqual(self.state.symbol, "ETHUSDT")
        self.assertEqual(
            self.state.profile_admission_policy.policy_hash,
            baseline_policy().policy_hash,
        )

    def test_symbol_change_cancels_pending_shadow_policy_change(self):
        self.state.simulator.open_order(
            Signal(
                direction="LONG",
                timeframe_minutes=10,
                level="B",
                reason="pending-policy-test",
                price=100.0,
                open_time=1_000,
                score=80.0,
                threshold=70.0,
                calculated_threshold=70.0,
                threshold_segment="WD-00",
            ),
            100.0,
            1_000,
        )
        self.state.request_shadow_policy_change(
            {
                "request_id": "promotion-before-switch",
                "policy": candidate_policy().to_dict(),
                "expected_policy_hash": baseline_policy().policy_hash,
                "symbol": "BTCUSDT",
                "generation": 0,
                "effective_at_ms": 10_000,
            }
        )

        self.state.reset_symbol("ETHUSDT")
        result = self.state.apply_pending_shadow_policy_change(700_000)

        self.assertEqual(result["status"], "CANCELLED_SYMBOL_CHANGE")
        self.assertEqual(
            self.state.profile_admission_policy.policy_hash,
            baseline_policy().policy_hash,
        )

    def test_shadow_gap_does_not_change_formal_last_error(self):
        self.state.record_shadow_event_gap(
            "queue full",
            expected_context=("BTCUSDT", 0),
        )

        self.assertIsNone(self.state.last_error)
        self.assertEqual(self.state.shadow_optimizer_status()["gap_count"], 1)
        self.assertEqual(self.state.shadow_optimizer_status()["last_gap"], "queue full")

    def test_state_snapshot_exposes_lightweight_shadow_status(self):
        class StatusProvider:
            @staticmethod
            def status():
                return {"status": "RUNNING", "arms": 8, "last_event_id": "event-1"}

        self.state.attach_shadow_status_provider(StatusProvider())

        status = self.state.snapshot()["shadow_optimizer"]

        self.assertEqual(status["status"], "RUNNING")
        self.assertEqual(status["arms"], 8)
        self.assertEqual(status["last_event_id"], "event-1")


if __name__ == "__main__":
    unittest.main()
