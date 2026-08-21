import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.models import Kline
from app.shadow_supervisor import ShadowEventBatch, ShadowSupervisor
from app.state import MonitorState


def kline(index: int) -> Kline:
    return Kline(
        open_time=index * 60_000,
        open=100.0,
        high=102.0,
        low=99.0,
        close=101.0,
        volume=12.0,
        close_time=(index + 1) * 60_000 - 1,
    )


class FakeProcess:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.exitcode = None
        self.pid = 4321
        self.started = False
        self.terminated = False

    def start(self):
        self.started = True

    def is_alive(self):
        return self.started and self.exitcode is None and not self.terminated

    def join(self, timeout=None):
        self.join_timeout = timeout

    def terminate(self):
        self.terminated = True
        self.exitcode = -15


class FakeContext:
    def __init__(self):
        self.queues = []
        self.processes = []

    def Queue(self, maxsize=0):
        item = queue.Queue(maxsize=maxsize)
        self.queues.append(item)
        return item

    def Event(self):
        return threading.Event()

    def Process(self, **kwargs):
        process = FakeProcess(**kwargs)
        self.processes.append(process)
        return process


class FailingStartProcess(FakeProcess):
    def start(self):
        raise RuntimeError("spawn unavailable")


class FailingStartContext(FakeContext):
    def Process(self, **kwargs):
        process = FailingStartProcess(**kwargs)
        self.processes.append(process)
        return process


def seed(context=("BTCUSDT", 3)):
    return {
        "symbol": context[0],
        "context": context,
        "constructor": {},
        "klines": (),
        "observations": (),
        "daily_profile_selection": None,
        "profile_admission_policy": {},
        "runtime_config": {"hash": "runtime-hash"},
    }


class ShadowSupervisorTest(unittest.TestCase):
    def test_spawn_failure_is_reported_and_stop_remains_safe(self):
        supervisor = ShadowSupervisor(
            seed_supplier=seed,
            database_path="shadow.sqlite3",
            process_context=FailingStartContext(),
        )

        with self.assertRaisesRegex(RuntimeError, "spawn unavailable"):
            supervisor.start()
        self.assertEqual(supervisor.status()["status"], "FAILED")

        supervisor.stop()
        self.assertEqual(supervisor.status()["status"], "STOPPED")

    def test_real_spawn_worker_advances_shadow_cursor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = MonitorState(
                "BTCUSDT",
                enable_daily_profile_selector=False,
                enable_observation_profile_promotion=False,
                strategy_build_id="shadow-process-smoke",
            )
            self.addCleanup(source.close)
            source.seed_klines([kline(index) for index in range(20)])
            supervisor = ShadowSupervisor(
                seed_supplier=source.shadow_runtime_seed,
                database_path=Path(temp_dir) / "monitor.shadow.sqlite3",
                max_challengers=1,
            )
            try:
                supervisor.start()

                deadline = time.monotonic() + 8.0
                status = supervisor.status()
                while status.get("status") == "STARTING" and time.monotonic() < deadline:
                    time.sleep(0.05)
                    status = supervisor.status()
                self.assertEqual(status.get("status"), "RUNNING", status)

                self.assertTrue(
                    supervisor.try_publish(
                        context=("BTCUSDT", 0),
                        klines=(kline(20),),
                        fear_greed=None,
                    )
                )
                while not status.get("last_event_id") and time.monotonic() < deadline:
                    time.sleep(0.05)
                    status = supervisor.status()

                self.assertTrue(status.get("last_event_id"), status)
                self.assertEqual(status.get("arms"), 2)
            finally:
                supervisor.stop()

    def test_try_publish_builds_closed_minute_events_without_waiting(self):
        context = FakeContext()
        supervisor = ShadowSupervisor(
            seed_supplier=seed,
            database_path="shadow.sqlite3",
            queue_size=2,
            process_context=context,
        )
        supervisor.start()

        published = supervisor.try_publish(
            context=("BTCUSDT", 3),
            klines=(kline(1), kline(2)),
            fear_greed={
                "value": 37,
                "classification": "Fear",
                "average_30d": 44.0,
                "trend": "falling",
                "updated_at_ms": 100,
                "source": "fixture",
            },
        )

        self.assertTrue(published)
        batch = context.queues[0].get_nowait()
        self.assertIsInstance(batch, ShadowEventBatch)
        self.assertFalse(batch.reset)
        self.assertEqual(batch.context, ("BTCUSDT", 3))
        self.assertEqual([event.kline for event in batch.events], [kline(1), kline(2)])
        self.assertEqual(batch.events[0].fear_greed.value, 37)

    def test_full_queue_returns_false_and_never_replaces_existing_batch(self):
        context = FakeContext()
        supervisor = ShadowSupervisor(
            seed_supplier=seed,
            database_path="shadow.sqlite3",
            queue_size=1,
            process_context=context,
        )
        supervisor.start()
        self.assertTrue(
            supervisor.try_publish(
                context=("BTCUSDT", 3),
                klines=(kline(1),),
                fear_greed=None,
            )
        )

        self.assertFalse(
            supervisor.try_publish(
                context=("BTCUSDT", 3),
                klines=(kline(2),),
                fear_greed=None,
            )
        )
        retained = context.queues[0].get_nowait()
        self.assertEqual(retained.events[0].kline, kline(1))

    def test_symbol_generation_change_attaches_atomic_reset_seed(self):
        context = FakeContext()
        current_seed = seed()
        supervisor = ShadowSupervisor(
            seed_supplier=lambda: current_seed,
            database_path="shadow.sqlite3",
            process_context=context,
        )
        supervisor.start()
        current_seed = seed(("ETHUSDT", 4))

        self.assertTrue(
            supervisor.try_publish(
                context=("ETHUSDT", 4),
                klines=(kline(3),),
                fear_greed=None,
            )
        )

        batch = context.queues[0].get_nowait()
        self.assertTrue(batch.reset)
        self.assertEqual(batch.seed["context"], ("ETHUSDT", 4))

    def test_status_reports_worker_updates_and_unexpected_exit(self):
        context = FakeContext()
        supervisor = ShadowSupervisor(
            seed_supplier=seed,
            database_path="shadow.sqlite3",
            process_context=context,
        )
        supervisor.start()
        context.queues[1].put_nowait(
            {"status": "RUNNING", "last_event_id": "event-1", "arms": 8}
        )

        self.assertEqual(supervisor.status()["status"], "RUNNING")
        self.assertEqual(supervisor.status()["last_event_id"], "event-1")

        context.processes[0].exitcode = 7
        failed = supervisor.status()
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["exit_code"], 7)

    def test_disabled_supervisor_is_a_noop(self):
        context = FakeContext()
        supervisor = ShadowSupervisor(
            seed_supplier=seed,
            database_path="shadow.sqlite3",
            enabled=False,
            process_context=context,
        )

        supervisor.start()

        self.assertEqual(context.processes, [])
        self.assertTrue(
            supervisor.try_publish(
                context=("BTCUSDT", 3),
                klines=(kline(1),),
                fear_greed=None,
            )
        )
        self.assertEqual(supervisor.status()["status"], "DISABLED")

    def test_lifecycle_requests_are_applied_by_background_status_consumer(self):
        context = FakeContext()
        handled = []
        supervisor = ShadowSupervisor(
            seed_supplier=seed,
            database_path="shadow.sqlite3",
            process_context=context,
            lifecycle_handler=lambda request: handled.append(request) or {
                "status": "REJECTED_STALE_CHAMPION",
                "request_id": request["request_id"],
            },
        )
        self.addCleanup(supervisor.stop)
        supervisor.start()
        context.queues[2].put_nowait(
            {
                "type": "PROMOTION_REQUEST",
                "request_id": "promotion-1",
                "policy": {},
            }
        )

        deadline = time.monotonic() + 1.0
        while not handled and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertEqual(handled[0]["request_id"], "promotion-1")
        self.assertEqual(
            supervisor.status()["last_lifecycle_action"]["result"]["status"],
            "REJECTED_STALE_CHAMPION",
        )
        lifecycle_result = context.queues[3].get(timeout=1.0)
        self.assertEqual(lifecycle_result["request_id"], "promotion-1")
        self.assertEqual(lifecycle_result["status"], "REJECTED_STALE_CHAMPION")

    def test_formal_receipt_ack_carries_authoritative_runtime_seed(self):
        context = FakeContext()
        supervisor = ShadowSupervisor(
            seed_supplier=seed,
            database_path="shadow.sqlite3",
            process_context=context,
        )
        self.addCleanup(supervisor.stop)
        supervisor.start()
        request = {"request_id": "promotion-applied"}
        result = {"status": "APPLIED", "request_id": "promotion-applied"}

        with patch(
            "app.shadow_storage.ShadowSQLiteStore.save_formal_policy_receipt"
        ) as save:
            supervisor.record_formal_policy_receipt(request, result)

        save.assert_called_once_with(request, result)
        lifecycle_result = context.queues[3].get(timeout=1.0)
        self.assertEqual(lifecycle_result["request_id"], "promotion-applied")
        self.assertEqual(lifecycle_result["formal_seed"]["context"], ("BTCUSDT", 3))


if __name__ == "__main__":
    unittest.main()
