from __future__ import annotations

import multiprocessing
import queue
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from app.models import FearGreedContext, Kline
from app.shadow_models import MarketEvent


@dataclass(frozen=True)
class ShadowEventBatch:
    context: tuple[str, int]
    events: tuple[MarketEvent, ...]
    reset: bool = False
    seed: dict[str, object] | None = None


def _shadow_worker_entry(
    event_queue,
    status_queue,
    lifecycle_queue,
    lifecycle_result_queue,
    stop_event,
    seed: dict[str, object],
    database_path: str,
    max_challengers: int,
) -> None:
    from app.shadow_optimizer import run_shadow_optimizer_worker

    run_shadow_optimizer_worker(
        event_queue=event_queue,
        status_queue=status_queue,
        lifecycle_queue=lifecycle_queue,
        lifecycle_result_queue=lifecycle_result_queue,
        stop_event=stop_event,
        seed=seed,
        database_path=database_path,
        max_challengers=max_challengers,
    )


class ShadowSupervisor:
    def __init__(
        self,
        *,
        seed_supplier: Callable[[], dict[str, object]],
        database_path: str | Path,
        enabled: bool = True,
        queue_size: int = 120,
        max_challengers: int = 7,
        process_context=None,
        worker_target=None,
        lifecycle_handler: Callable[[dict[str, object]], dict[str, object]] | None = None,
    ) -> None:
        self.seed_supplier = seed_supplier
        self.database_path = str(database_path)
        self.enabled = bool(enabled)
        self.queue_size = max(1, int(queue_size))
        self.max_challengers = min(7, max(1, int(max_challengers)))
        self._context_factory = process_context or multiprocessing.get_context("spawn")
        self._worker_target = worker_target or _shadow_worker_entry
        self._lifecycle_handler = lifecycle_handler
        self._event_queue = None
        self._status_queue = None
        self._lifecycle_queue = None
        self._lifecycle_result_queue = None
        self._stop_event = None
        self._process = None
        self._symbol_context: tuple[str, int] | None = None
        self._status: dict[str, object] = {"status": "DISABLED"}
        self._status_stop = threading.Event()
        self._status_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if not self.enabled:
                self._status = {"status": "DISABLED"}
                return
            if self._process is not None and self._process.is_alive():
                return
            seed = self.seed_supplier()
            self._symbol_context = self._normalize_context(seed.get("context"))
            self._event_queue = self._context_factory.Queue(maxsize=self.queue_size)
            self._status_queue = self._context_factory.Queue(maxsize=64)
            self._lifecycle_queue = self._context_factory.Queue(maxsize=16)
            self._lifecycle_result_queue = self._context_factory.Queue(maxsize=64)
            self._stop_event = self._context_factory.Event()
            self._process = self._context_factory.Process(
                target=self._worker_target,
                args=(
                    self._event_queue,
                    self._status_queue,
                    self._lifecycle_queue,
                    self._lifecycle_result_queue,
                    self._stop_event,
                    seed,
                    self.database_path,
                    self.max_challengers,
                ),
                name="strategy-shadow-optimizer",
                daemon=True,
            )
            try:
                self._process.start()
            except Exception as exc:
                self._status = {
                    "status": "FAILED",
                    "context": list(self._symbol_context),
                    "error": f"{type(exc).__name__}: {exc}",
                }
                self._process = None
                raise
            self._status = {
                "status": "STARTING",
                "pid": self._process.pid,
                "context": list(self._symbol_context),
            }
            if self._lifecycle_handler is not None:
                self._status_stop.clear()
                self._status_thread = threading.Thread(
                    target=self._consume_status_loop,
                    name="shadow-optimizer-status",
                    daemon=True,
                )
                self._status_thread.start()

    def try_publish(
        self,
        *,
        context: tuple[str, int],
        klines: Sequence[Kline],
        fear_greed: dict[str, object] | None,
    ) -> bool:
        if not self.enabled:
            return True
        normalized_context = self._normalize_context(context)
        with self._lock:
            process = self._process
            event_queue = self._event_queue
            current_context = self._symbol_context
        if process is None or event_queue is None or not process.is_alive():
            return False
        if not klines:
            return True
        frozen_fear_greed = self._fear_greed_context(
            fear_greed,
            default_updated_at=int(klines[-1].close_time),
        )
        events = tuple(
            MarketEvent(
                symbol=normalized_context[0],
                generation=normalized_context[1],
                kline=item,
                fear_greed=frozen_fear_greed,
            )
            for item in klines
        )
        reset = normalized_context != current_context
        reset_seed = None
        if reset:
            reset_seed = self.seed_supplier()
            if self._normalize_context(reset_seed.get("context")) != normalized_context:
                return False
        batch = ShadowEventBatch(
            context=normalized_context,
            events=events,
            reset=reset,
            seed=reset_seed,
        )
        try:
            event_queue.put_nowait(batch)
        except queue.Full:
            return False
        if reset:
            with self._lock:
                self._symbol_context = normalized_context
        return True

    def status(self) -> dict[str, object]:
        if not self.enabled:
            return {"status": "DISABLED"}
        with self._lock:
            status_queue = self._status_queue
            lifecycle_queue = self._lifecycle_queue
            process = self._process
            status = deepcopy(self._status)
        if lifecycle_queue is not None:
            self._drain_lifecycle_queue(lifecycle_queue)
        if status_queue is not None:
            while True:
                try:
                    update = status_queue.get_nowait()
                except queue.Empty:
                    break
                if isinstance(update, dict):
                    self._handle_status_update(update)
            with self._lock:
                status = deepcopy(self._status)
        if process is not None and not process.is_alive() and process.exitcode is not None:
            if status.get("status") != "STOPPED":
                status = {
                    **status,
                    "status": "FAILED",
                    "exit_code": process.exitcode,
                }
        return status

    def stop(self, timeout: float = 5.0) -> None:
        self._status_stop.set()
        with self._lock:
            stop_event = self._stop_event
            process = self._process
            status_thread = self._status_thread
        if stop_event is not None:
            stop_event.set()
        if process is not None:
            process.join(timeout=max(0.0, float(timeout)))
            if process.is_alive():
                process.terminate()
                process.join(timeout=1.0)
        with self._lock:
            self._status = {**self._status, "status": "STOPPED"}
        if status_thread is not None and status_thread is not threading.current_thread():
            status_thread.join(timeout=1.0)
        for channel in (
            self._event_queue,
            self._status_queue,
            self._lifecycle_queue,
            self._lifecycle_result_queue,
        ):
            closer = getattr(channel, "close", None)
            if callable(closer):
                closer()

    def experiment_summary(self) -> dict[str, object]:
        status = self.status()
        experiment_id = str(status.get("experiment_id") or "")
        if not experiment_id:
            return {"status": status.get("status", "DISABLED"), "experiment": None}
        from app.shadow_storage import ShadowSQLiteStore

        store = ShadowSQLiteStore(self.database_path)
        experiment = store.load_experiment(experiment_id)
        if experiment is None:
            return {"status": status.get("status", "UNKNOWN"), "experiment": None}
        snapshots = {
            row["parameter_hash"]: row
            for row in store.list_parameter_snapshots()
        }
        arms = []
        for arm in experiment["arms"]:
            evaluations = store.list_evaluations(str(arm["arm_id"]))
            arms.append(
                {
                    **arm,
                    "parameters": deepcopy(
                        snapshots.get(str(arm["parameter_hash"]), {}).get("payload")
                    ),
                    "latest_evaluation": (
                        evaluations[-1] if evaluations else None
                    ),
                }
            )
        return {
            "status": status.get("status", "UNKNOWN"),
            "experiment": {**experiment, "arms": arms},
            "lifecycle": [
                row
                for row in store.list_lifecycle_history()
                if row["experiment_id"] == experiment_id
            ][-20:],
            "capacity": store.storage_capacity().__dict__,
        }

    def restored_policy_request(self) -> dict[str, object] | None:
        if not self.enabled:
            return None
        from app.shadow_candidates import build_profile_admission_arms
        from app.shadow_storage import ShadowSQLiteStore

        seed = self.seed_supplier()
        context = self._normalize_context(seed.get("context"))
        definitions = build_profile_admission_arms(
            seed,
            max_challengers=self.max_challengers,
        )
        receipt = ShadowSQLiteStore(
            self.database_path
        ).load_latest_formal_policy_receipt(context[0])
        if receipt is None:
            return None
        definition_by_hash = {
            definition.parameter_hash: definition for definition in definitions
        }
        target = definition_by_hash.get(str(receipt["parameter_hash"]))
        current = definitions[0]
        if (
            target is None
            or str(receipt["analyzer_hash"]) != target.analyzer_hash
            or str(receipt["policy_hash"]) != target.policy.policy_hash
            or receipt["policy"] != target.policy.to_dict()
            or target.policy.policy_hash == current.policy.policy_hash
        ):
            return None
        return {
            "type": "RESTORE_CHAMPION_REQUEST",
            "request_id": (
                f"restore:{receipt['receipt_id']}:"
                f"{time.time_ns()}"
            ),
            "experiment_id": str(receipt["experiment_id"]),
            "symbol": context[0],
            "generation": context[1],
            "from_arm_id": "",
            "to_arm_id": str(receipt["to_arm_id"]),
            "expected_policy_hash": current.policy.policy_hash,
            "policy": target.policy.to_dict(),
            "policy_hash": target.policy.policy_hash,
            "parameter_hash": target.parameter_hash,
            "effective_at_ms": int(time.time() * 1000),
        }

    def record_formal_policy_receipt(
        self,
        request: dict[str, object],
        result: dict[str, object],
    ) -> None:
        from app.shadow_storage import ShadowSQLiteStore

        ShadowSQLiteStore(self.database_path).save_formal_policy_receipt(
            request,
            result,
        )
        self._publish_lifecycle_result(
            {
                **deepcopy(result),
                "formal_seed": self.seed_supplier(),
            }
        )

    def page_orders(
        self,
        *,
        arm_id: str,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, object]:
        if not str(arm_id).strip():
            return {
                "arm_id": "",
                "page": 1,
                "page_size": min(100, max(1, int(page_size))),
                "total": 0,
                "total_pages": 1,
                "orders": [],
            }
        from app.shadow_storage import ShadowSQLiteStore

        return ShadowSQLiteStore(self.database_path).page_orders(
            str(arm_id),
            page=page,
            page_size=page_size,
        )

    def _consume_status_loop(self) -> None:
        while not self._status_stop.is_set():
            with self._lock:
                status_queue = self._status_queue
                lifecycle_queue = self._lifecycle_queue
            if lifecycle_queue is not None:
                self._drain_lifecycle_queue(lifecycle_queue)
            if status_queue is None:
                return
            try:
                update = status_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if isinstance(update, dict):
                self._handle_status_update(update)

    def _drain_lifecycle_queue(self, lifecycle_queue) -> None:
        while True:
            try:
                update = lifecycle_queue.get_nowait()
            except queue.Empty:
                return
            if isinstance(update, dict):
                self._handle_status_update(update)

    def _handle_status_update(self, update: dict[str, object]) -> None:
        message_type = str(update.get("type") or "")
        if message_type == "EVALUATION":
            with self._lock:
                self._status = {
                    **self._status,
                    "latest_evaluation": deepcopy(update),
                }
            return
        if message_type in {"PROMOTION_REQUEST", "ROLLBACK_REQUEST"}:
            if self._lifecycle_handler is None:
                result = {"status": "NO_HANDLER"}
            else:
                try:
                    result = self._lifecycle_handler(deepcopy(update))
                except Exception as exc:  # noqa: BLE001 - 生命周期失败不得结束状态线程。
                    result = {
                        "status": "FAILED",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
            result = {
                **deepcopy(result),
                "request_id": str(
                    result.get("request_id") or update.get("request_id") or ""
                ),
            }
            if str(result.get("status") or "").upper() not in {
                "APPLIED",
                "APPLIED_AT_DEADLINE",
                "WAITING_OPEN_ORDERS",
            }:
                self._publish_lifecycle_result(result)
            with self._lock:
                self._status = {
                    **self._status,
                    "last_lifecycle_action": {
                        "request": deepcopy(update),
                        "result": deepcopy(result),
                    },
                }
            return
        with self._lock:
            lifecycle = self._status.get("last_lifecycle_action")
            self._status = deepcopy(update)
            if lifecycle is not None:
                self._status["last_lifecycle_action"] = deepcopy(lifecycle)

    def _publish_lifecycle_result(self, result: dict[str, object]) -> None:
        with self._lock:
            channel = self._lifecycle_result_queue
        if channel is None:
            return
        channel.put(deepcopy(result), timeout=1.0)

    @staticmethod
    def _normalize_context(value) -> tuple[str, int]:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError("shadow symbol context must contain symbol and generation")
        symbol = str(value[0]).strip().upper()
        generation = value[1]
        if not symbol or type(generation) is not int or generation < 0:
            raise ValueError("invalid shadow symbol context")
        return symbol, generation

    @staticmethod
    def _fear_greed_context(
        snapshot: dict[str, object] | None,
        *,
        default_updated_at: int,
    ) -> FearGreedContext:
        payload = snapshot or {}
        return FearGreedContext(
            value=int(payload.get("value", 50) or 50),
            classification=str(payload.get("classification", "Neutral") or "Neutral"),
            average_30d=float(payload.get("average_30d", 50.0) or 0.0),
            trend=str(payload.get("trend", "unknown") or "unknown"),
            updated_at_ms=int(payload.get("updated_at_ms", default_updated_at) or default_updated_at),
            source=str(payload.get("source", "neutral") or "neutral"),
        )
