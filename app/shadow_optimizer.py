from __future__ import annotations

import hashlib
import json
import queue
import time
from copy import deepcopy
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from app.models import ObservationSignal, SimulatedOrder
from app.shadow_candidates import ShadowArmDefinition, build_profile_admission_arms
from app.shadow_lifecycle import (
    ChallengerEvaluation,
    evaluate_promotion,
    evaluate_rollback,
    rank_eligible_challengers,
)
from app.shadow_models import MarketEvent
from app.shadow_runtime import ShadowRuntime
from app.shadow_storage import ShadowSQLiteStore


PARAMETER_FAMILY = "PROFILE_ADMISSION"
SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))
OBSERVATION_DETAIL_RETENTION_MS = 30 * 86_400_000
OLD_CHAMPION_RETENTION_MS = 14 * 86_400_000


class ShadowLifecycleScheduler:
    def __init__(self) -> None:
        self._compacted_day = ""
        self._evaluated_day = ""
        self._activated_day = ""

    def advance(
        self,
        optimizer: "ShadowOptimizer",
        current_time_ms: int,
    ) -> tuple[dict[str, object], ...]:
        timestamp = int(current_time_ms)
        local = datetime.fromtimestamp(timestamp / 1000, SHANGHAI_TIMEZONE)
        day = local.date().isoformat()
        minute = local.hour * 60 + local.minute
        messages: list[dict[str, object]] = []
        if minute >= 7 * 60 + 45 and self._compacted_day != day:
            optimizer.compact(timestamp)
            self._compacted_day = day
        if minute >= 7 * 60 + 50 and self._evaluated_day != day:
            evaluation = optimizer.evaluate(timestamp)
            self._evaluated_day = day
            messages.append({"type": "EVALUATION", **evaluation})
        if minute >= 8 * 60 and self._activated_day != day:
            request = optimizer.activate_pending(timestamp)
            self._activated_day = day
            if request is not None:
                messages.append(request)
        return tuple(messages)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _stable_id(prefix: str, value: object, length: int = 24) -> str:
    digest = hashlib.sha256(_canonical_json(value).encode("ascii")).hexdigest()
    return f"{prefix}-{digest[:length]}"


def _model_payload(model_type, detail, core: Mapping[str, object]):
    payload = deepcopy(detail) if isinstance(detail, dict) else {}
    payload.update(core)
    allowed = {item.name for item in fields(model_type) if item.init}
    return {key: value for key, value in payload.items() if key in allowed}


class ShadowOptimizer:
    def __init__(
        self,
        *,
        seed: Mapping[str, object],
        store: ShadowSQLiteStore,
        max_challengers: int = 7,
        created_at_ms: int | None = None,
    ) -> None:
        self.seed = deepcopy(dict(seed))
        self.store = store
        self.created_at_ms = int(
            time.time() * 1000 if created_at_ms is None else created_at_ms
        )
        self.definitions = build_profile_admission_arms(
            self.seed,
            max_challengers=max_challengers,
        )
        self.symbol, self.generation = self._seed_context(self.seed)
        self.experiment_id = ""
        self._arm_ids: tuple[str, ...] = ()
        self._definition_by_arm: dict[str, ShadowArmDefinition] = {}
        self._runtimes: dict[str, ShadowRuntime] = {}
        self._order_signatures: dict[str, dict[int, tuple]] = {}
        self._observation_signatures: dict[str, dict[str, tuple]] = {}
        self._order_event_ids: dict[str, dict[int, str]] = {}
        self._observation_event_ids: dict[str, dict[str, str]] = {}
        self._failed_arms: dict[str, str] = {}
        self._retired_arms: set[str] = set()
        self._champion_arm_id = ""
        self._pending_promotion: dict[str, object] | None = None
        self._pending_lifecycle: dict[str, object] | None = None
        self._latest_evaluation: dict[str, object] | None = None
        self._lifecycle_requests: list[dict[str, object]] = []
        self._closed = False
        self._initialize()

    @staticmethod
    def _seed_context(seed: Mapping[str, object]) -> tuple[str, int]:
        context = seed.get("context")
        if not isinstance(context, (list, tuple)) or len(context) != 2:
            raise ValueError("shadow seed requires symbol context")
        symbol = str(context[0]).strip().upper()
        generation = context[1]
        if not symbol or type(generation) is not int or generation < 0:
            raise ValueError("invalid shadow seed context")
        return symbol, generation

    @property
    def arm_ids(self) -> tuple[str, ...]:
        return self._arm_ids

    def runtime(self, arm_id: str) -> ShadowRuntime:
        return self._runtimes[str(arm_id)]

    def _initialize(self) -> None:
        for definition in self.definitions:
            self.store.save_parameter_snapshot(
                parameter_hash=definition.parameter_hash,
                analyzer_hash=definition.analyzer_hash,
                parameter_family=PARAMETER_FAMILY,
                payload=definition.parameters.to_dict(),
                created_at_ms=self.created_at_ms,
            )

        existing = self.store.find_latest_running_experiment(
            self.symbol,
            PARAMETER_FAMILY,
        )
        expected_hashes = {item.parameter_hash for item in self.definitions}
        seed_klines = tuple(self.seed.get("klines") or ())
        latest_seed_close = (
            int(seed_klines[-1].close_time) if seed_klines else None
        )
        cursor_bounds = (
            self.store.experiment_cursor_bounds(str(existing["experiment_id"]))
            if existing is not None
            else {"cursor_count": 0, "minimum_closed_at_ms": None}
        )
        seed_ahead = bool(
            latest_seed_close is not None
            and int(cursor_bounds["cursor_count"] or 0) > 0
            and cursor_bounds["minimum_closed_at_ms"] is not None
            and latest_seed_close > int(cursor_bounds["minimum_closed_at_ms"])
        )
        reusable = bool(
            existing
            and int(existing["generation"]) == self.generation
            and {item["parameter_hash"] for item in existing["arms"]}
            == expected_hashes
            and not seed_ahead
        )
        if reusable:
            self.experiment_id = str(existing["experiment_id"])
            rows_by_hash = {
                str(row["parameter_hash"]): row for row in existing["arms"]
            }
            ordered_rows = [
                rows_by_hash[definition.parameter_hash]
                for definition in self.definitions
            ]
            self._champion_arm_id = str(existing["champion_arm_id"])
        else:
            if existing is not None and seed_ahead:
                self.store.mark_experiment_status(
                    str(existing["experiment_id"]),
                    status="SUPERSEDED_SEED_ADVANCE",
                    completed_at_ms=self.created_at_ms,
                )
            self.experiment_id = _stable_id(
                "shadow-profile",
                {
                    "symbol": self.symbol,
                    "generation": self.generation,
                    "created_at_ms": self.created_at_ms,
                    "runtime_hash": self.seed["runtime_config"]["hash"],
                    "supersedes": (
                        str(existing["experiment_id"])
                        if existing is not None and seed_ahead
                        else None
                    ),
                    "parameters": [
                        item.parameter_hash for item in self.definitions
                    ],
                },
            )
            effective_from = self._effective_from(self.seed, self.created_at_ms)
            ordered_rows = []
            arms = []
            for definition in self.definitions:
                arm_id = (
                    f"{self.experiment_id}:"
                    f"{definition.role.lower()}:{definition.parameter_hash[:12]}"
                )
                row = {
                    "arm_id": arm_id,
                    "parameter_hash": definition.parameter_hash,
                    "parent_arm_id": None,
                    "role": definition.role,
                    "status": "RUNNING",
                    "effective_from_ms": effective_from,
                    "created_at_ms": self.created_at_ms,
                }
                arms.append(row)
                ordered_rows.append(row)
            champion_arm_id = next(
                row["arm_id"] for row in arms if row["role"] == "CHAMPION"
            )
            self.store.create_experiment(
                experiment_id=self.experiment_id,
                symbol=self.symbol,
                parameter_family=PARAMETER_FAMILY,
                generation=self.generation,
                created_at_ms=self.created_at_ms,
                started_at_ms=self.created_at_ms,
                arms=arms,
                champion_arm_id=champion_arm_id,
            )
            self._champion_arm_id = champion_arm_id

        self._arm_ids = tuple(str(row["arm_id"]) for row in ordered_rows)
        self._retired_arms = {
            str(row["arm_id"])
            for row in ordered_rows
            if str(row.get("status", "")).upper() == "RETIRED"
        }
        if self._champion_arm_id not in self._arm_ids:
            raise ValueError("shadow experiment champion arm is invalid")
        self._definition_by_arm = {
            str(row["arm_id"]): definition
            for row, definition in zip(ordered_rows, self.definitions, strict=True)
        }
        try:
            for arm_id in self._arm_ids:
                definition = self._definition_by_arm[arm_id]
                recovery = self.store.load_recovery_state(arm_id)
                runtime_state = recovery.get("runtime_state")
                if runtime_state is None:
                    runtime = ShadowRuntime.from_seed(
                        self.seed,
                        generation=self.generation,
                        policies={arm_id: definition.policy},
                    )
                else:
                    runtime = ShadowRuntime.from_persistence(
                        self.seed,
                        arm_id=arm_id,
                        policy=definition.policy,
                        runtime_state=runtime_state,
                        orders=[self._restore_order(row) for row in recovery["orders"]],
                        observations=[
                            self._restore_observation(row)
                            for row in recovery["observations"]
                        ],
                    )
                self._runtimes[arm_id] = runtime
                self._prime_signatures(arm_id, recovery)
        except Exception:
            self.close()
            raise

    @staticmethod
    def _effective_from(seed: Mapping[str, object], fallback: int) -> int:
        klines = seed.get("klines") or ()
        return int(klines[-1].close_time + 1) if klines else int(fallback)

    @staticmethod
    def _restore_order(row: Mapping[str, object]) -> dict[str, object]:
        stake = float(row["stake"])
        return _model_payload(
            SimulatedOrder,
            row.get("detail"),
            {
                "id": int(row["order_id"]),
                "direction": str(row["direction"]),
                "timeframe_minutes": int(
                    (row.get("detail") or {}).get("timeframe_minutes", 10)
                ),
                "level": str((row.get("detail") or {}).get("level", "B")),
                "reason": str(
                    (row.get("detail") or {}).get("reason", "shadow restored")
                ),
                "entry_price": float(row["entry_price"]),
                "opened_at": int(row["opened_at_ms"]),
                "expires_at": int(row["expires_at_ms"]),
                "stake": stake,
                "win_return": float(
                    (row.get("detail") or {}).get("win_return", stake * 1.8)
                ),
                "status": str(row["status"]),
                "result": row.get("result"),
                "exit_price": row.get("exit_price"),
                "settled_at": row.get("settled_at_ms"),
                "pnl": float(row.get("pnl", 0.0)),
            },
        )

    @staticmethod
    def _restore_observation(row: Mapping[str, object]) -> dict[str, object]:
        return _model_payload(
            ObservationSignal,
            row.get("detail"),
            {
                key: row.get(key)
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
            },
        )

    def _prime_signatures(
        self,
        arm_id: str,
        recovery: Mapping[str, object],
    ) -> None:
        self._order_signatures[arm_id] = {
            int(row["order_id"]): self._stored_order_signature(row)
            for row in recovery["orders"]
        }
        self._observation_signatures[arm_id] = {
            str(row["observation_key"]): self._stored_observation_signature(row)
            for row in recovery["observations"]
        }
        self._order_event_ids[arm_id] = {
            int(row["order_id"]): str(row["decision_event_id"])
            for row in recovery["orders"]
        }
        self._observation_event_ids[arm_id] = {
            str(row["observation_key"]): str(row["decision_event_id"])
            for row in recovery["observations"]
        }

    @staticmethod
    def _stored_order_signature(row: Mapping[str, object]) -> tuple:
        return (
            str(row["status"]),
            row.get("result"),
            row.get("settled_at_ms"),
            row.get("exit_price"),
            float(row.get("pnl", 0.0)),
        )

    @staticmethod
    def _stored_observation_signature(row: Mapping[str, object]) -> tuple:
        return (
            str(row["status"]),
            row.get("result"),
            row.get("settled_at"),
            row.get("exit_price"),
            float(row.get("pnl", 0.0)),
        )

    def process_event(self, event: MarketEvent) -> int:
        return self.process_batch((event,))

    def process_batch(self, events) -> int:
        if self._closed:
            raise RuntimeError("shadow optimizer is closed")
        batch = tuple(events)
        if not batch:
            return 0
        final_event = batch[-1]
        active_arm_ids = tuple(
            arm_id
            for arm_id in self._arm_ids
            if arm_id not in self._failed_arms and arm_id not in self._retired_arms
        )
        if not active_arm_ids:
            return 0
        analysis_frames: dict[str, dict[str, object]] = {}
        for arm_id in active_arm_ids:
            analyzer_hash = self._definition_by_arm[arm_id].analyzer_hash
            if analyzer_hash in analysis_frames:
                continue
            try:
                analysis_frames[analyzer_hash] = self._runtimes[
                    arm_id
                ].build_analysis_frame(batch)
            except Exception as exc:  # noqa: BLE001 - 只冻结同分析器参数组。
                reason = f"shadow shared analyzer failed: {exc}"
                for related_arm_id in active_arm_ids:
                    if (
                        self._definition_by_arm[related_arm_id].analyzer_hash
                        == analyzer_hash
                    ):
                        self._runtimes[related_arm_id].freeze(reason)
                        self._failed_arms[related_arm_id] = reason
        persisted = 0
        for arm_id in self._arm_ids:
            if arm_id in self._failed_arms or arm_id in self._retired_arms:
                continue
            runtime = self._runtimes[arm_id]
            prior_cursor = runtime.cursor_event_id
            try:
                processed = runtime.process_batch(
                    batch,
                    analysis_frame=analysis_frames[
                        self._definition_by_arm[arm_id].analyzer_hash
                    ],
                )
                if not processed:
                    if runtime.invalid and final_event.event_id != prior_cursor:
                        self._persist_gap(
                            arm_id,
                            final_event,
                            runtime.invalid_reason,
                        )
                    continue
                self._persist_event(arm_id, final_event)
                persisted += 1
            except Exception as exc:  # noqa: BLE001 - 只冻结受影响的影子参数组。
                reason = f"shadow arm persistence failed: {exc}"
                runtime.freeze(reason)
                self._failed_arms[arm_id] = reason
        request = self.evaluate_rollback_if_needed(final_event.kline.close_time)
        if request is not None:
            self._lifecycle_requests.append(request)
        return persisted

    def drain_lifecycle_requests(self) -> tuple[dict[str, object], ...]:
        requests = tuple(deepcopy(self._lifecycle_requests))
        self._lifecycle_requests.clear()
        return requests

    @staticmethod
    def _days_beating_champion(
        candidate_days,
        champion_days,
    ) -> int:
        champion_by_day = {
            str(row["day"]): float(row["win_rate"])
            for row in champion_days
        }
        return sum(
            str(row["day"]) in champion_by_day
            and float(row["win_rate"])
            > champion_by_day[str(row["day"])]
            for row in candidate_days
        )

    def evaluate(self, evaluated_at_ms: int) -> dict[str, object]:
        timestamp = int(evaluated_at_ms)
        if self._closed:
            raise RuntimeError("shadow optimizer is closed")
        champion_runtime = self._runtimes[self._champion_arm_id]
        champion_metrics = champion_runtime.evaluation_metrics(
            self._champion_arm_id
        )
        champion_days = champion_runtime.daily_statistics(self._champion_arm_id)
        self.store.save_evaluation_bundle(
            evaluation_id=_stable_id(
                "shadow-eval",
                {
                    "experiment": self.experiment_id,
                    "arm": self._champion_arm_id,
                    "at": timestamp,
                },
            ),
            experiment_id=self.experiment_id,
            arm_id=self._champion_arm_id,
            evaluated_at_ms=timestamp,
            decision="CONTROL",
            metrics=champion_metrics.to_dict(),
        )

        evaluations = []
        decisions = {}
        for arm_id in self._arm_ids:
            if (
                arm_id == self._champion_arm_id
                or arm_id in self._failed_arms
                or arm_id in self._retired_arms
            ):
                continue
            runtime = self._runtimes[arm_id]
            metrics = runtime.evaluation_metrics(arm_id)
            metrics = replace(
                metrics,
                days_beating_champion=self._days_beating_champion(
                    runtime.daily_statistics(arm_id),
                    champion_days,
                ),
            )
            definition = self._definition_by_arm[arm_id]
            promotion = evaluate_promotion(metrics, champion_metrics)
            decision = "ELIGIBLE" if promotion.eligible else "REJECTED"
            decisions[arm_id] = {
                "decision": decision,
                "failures": list(promotion.failures),
                "metrics": metrics.to_dict(),
            }
            self.store.save_evaluation_bundle(
                evaluation_id=_stable_id(
                    "shadow-eval",
                    {
                        "experiment": self.experiment_id,
                        "arm": arm_id,
                        "at": timestamp,
                    },
                ),
                experiment_id=self.experiment_id,
                arm_id=arm_id,
                evaluated_at_ms=timestamp,
                decision=decision,
                metrics={
                    **metrics.to_dict(),
                    "gate_failures": list(promotion.failures),
                    "required_average_orders_per_day": (
                        promotion.required_average_orders_per_day
                    ),
                },
            )
            evaluations.append(
                ChallengerEvaluation(
                    parameters=definition.parameters,
                    metrics=metrics,
                    complexity=definition.complexity,
                )
            )

        recent_promotion = next(
            (
                row
                for row in reversed(self.store.list_lifecycle_history())
                if row["experiment_id"] == self.experiment_id
                and row["event_type"] == "PROMOTION"
                and int(row["effective_at_ms"]) > timestamp - 7 * 86_400_000
            ),
            None,
        )
        ranked = rank_eligible_challengers(evaluations, champion_metrics)
        ranked_by_hash = {
            item.parameters.parameter_hash: item for item in ranked
        }
        selected_arm_id = next(
            (
                arm_id
                for arm_id in self._arm_ids
                if self._definition_by_arm[arm_id].parameter_hash
                in ranked_by_hash
            ),
            None,
        )
        if ranked:
            best_hash = ranked[0].parameters.parameter_hash
            selected_arm_id = next(
                arm_id
                for arm_id in self._arm_ids
                if self._definition_by_arm[arm_id].parameter_hash == best_hash
            )

        if recent_promotion is not None:
            status = "PROMOTION_COOLDOWN"
            selected_arm_id = None
            self._pending_promotion = None
        elif selected_arm_id is not None:
            status = "READY"
            selected = decisions[selected_arm_id]
            self._pending_promotion = {
                "arm_id": selected_arm_id,
                "from_arm_id": self._champion_arm_id,
                "evaluated_at_ms": timestamp,
                "metrics": deepcopy(selected["metrics"]),
            }
        else:
            status = (
                "INSUFFICIENT_SAMPLE"
                if champion_metrics.complete_days < 7
                or champion_metrics.settled_orders < 300
                else "NO_ELIGIBLE_CHALLENGER"
            )
            self._pending_promotion = None

        result = {
            "status": status,
            "evaluated_at_ms": timestamp,
            "champion_arm_id": self._champion_arm_id,
            "selected_arm_id": selected_arm_id,
            "champion_metrics": champion_metrics.to_dict(),
            "challengers": decisions,
        }
        self._latest_evaluation = deepcopy(result)
        return result

    def activate_pending(self, effective_at_ms: int) -> dict[str, object] | None:
        pending = self._pending_promotion
        if pending is None or self._pending_lifecycle is not None:
            return None
        target_arm_id = str(pending["arm_id"])
        source_arm_id = str(pending["from_arm_id"])
        timestamp = int(effective_at_ms)
        target = self._definition_by_arm[target_arm_id]
        source = self._definition_by_arm[source_arm_id]
        history_id = _stable_id(
            "shadow-promotion",
            {
                "experiment": self.experiment_id,
                "from": source_arm_id,
                "to": target_arm_id,
                "at": timestamp,
            },
        )
        evaluation_id = _stable_id(
                "shadow-activation-eval",
                {"history": history_id, "arm": target_arm_id},
            )
        lifecycle_event = {
                "history_id": history_id,
                "event_type": "PROMOTION",
                "from_arm_id": source_arm_id,
                "to_arm_id": target_arm_id,
                "effective_at_ms": timestamp,
                "reason": "ALL_FORWARD_GATES_PASSED",
                "evidence": {
                    "evaluated_at_ms": pending["evaluated_at_ms"],
                    "metrics": deepcopy(pending["metrics"]),
                },
            }
        request = {
            "type": "PROMOTION_REQUEST",
            "request_id": history_id,
            "experiment_id": self.experiment_id,
            "symbol": self.symbol,
            "generation": self.generation,
            "from_arm_id": source_arm_id,
            "to_arm_id": target_arm_id,
            "expected_policy_hash": source.policy.policy_hash,
            "policy": target.policy.to_dict(),
            "policy_hash": target.policy.policy_hash,
            "parameter_hash": target.parameter_hash,
            "effective_at_ms": timestamp,
        }
        self._pending_lifecycle = {
            "action": "PROMOTION",
            "request": deepcopy(request),
            "evaluation_id": evaluation_id,
            "arm_id": target_arm_id,
            "decision": "PROMOTE",
            "metrics": deepcopy(pending["metrics"]),
            "lifecycle_event": lifecycle_event,
            "arm_status": "PROMOTED",
            "source_arm_id": source_arm_id,
            "target_arm_id": target_arm_id,
        }
        self._pending_promotion = None
        return request

    def resolve_lifecycle_request(
        self,
        request_id: str,
        result: Mapping[str, object],
    ) -> bool:
        pending = self._pending_lifecycle
        if pending is None:
            return False
        request = pending["request"]
        if str(request.get("request_id")) != str(request_id):
            return False
        status = str(result.get("status") or "").upper()
        if status == "WAITING_OPEN_ORDERS":
            return False
        if status in {"APPLIED", "APPLIED_AT_DEADLINE"}:
            expected_policy_hash = str(request.get("policy_hash") or "")
            if str(result.get("policy_hash") or "") != expected_policy_hash:
                raise ValueError("formal lifecycle ACK policy hash mismatch")
            applied_at_ms = int(result.get("applied_at_ms") or 0)
            if applied_at_ms <= 0:
                raise ValueError("formal lifecycle ACK requires applied_at_ms")
            lifecycle_event = {
                **deepcopy(pending["lifecycle_event"]),
                "effective_at_ms": applied_at_ms,
            }
            self.store.save_evaluation_bundle(
                evaluation_id=str(pending["evaluation_id"]),
                experiment_id=self.experiment_id,
                arm_id=str(pending["arm_id"]),
                evaluated_at_ms=applied_at_ms,
                decision=str(pending["decision"]),
                metrics=deepcopy(pending["metrics"]),
                lifecycle_event=lifecycle_event,
                arm_status=str(pending["arm_status"]),
            )
            source_arm_id = str(pending["source_arm_id"])
            target_arm_id = str(pending["target_arm_id"])
            self._champion_arm_id = target_arm_id
            if pending["action"] == "ROLLBACK":
                self._retired_arms.add(source_arm_id)
        self._pending_lifecycle = None
        return True

    def evaluate_rollback_if_needed(
        self,
        evaluated_at_ms: int,
    ) -> dict[str, object] | None:
        timestamp = int(evaluated_at_ms)
        if self._pending_lifecycle is not None:
            return None
        history = [
            row
            for row in self.store.list_lifecycle_history()
            if row["experiment_id"] == self.experiment_id
        ]
        if not history:
            return None
        latest = history[-1]
        if (
            latest["event_type"] != "PROMOTION"
            or latest.get("to_arm_id") != self._champion_arm_id
        ):
            return None
        promoted_at = int(latest["effective_at_ms"])
        if timestamp < promoted_at or timestamp > promoted_at + OLD_CHAMPION_RETENTION_MS:
            return None
        old_arm_id = str(latest.get("from_arm_id") or "")
        if old_arm_id not in self._runtimes:
            return None
        current_metrics = self._runtimes[self._champion_arm_id].metrics_since(
            self._champion_arm_id,
            promoted_at,
        )
        old_metrics = self._runtimes[old_arm_id].metrics_since(
            old_arm_id,
            promoted_at,
        )
        rollback = evaluate_rollback(current_metrics, old_metrics)
        if not rollback.should_rollback:
            return None
        failed_arm_id = self._champion_arm_id
        failed_definition = self._definition_by_arm[failed_arm_id]
        old_definition = self._definition_by_arm[old_arm_id]
        history_id = _stable_id(
            "shadow-rollback",
            {
                "experiment": self.experiment_id,
                "from": failed_arm_id,
                "to": old_arm_id,
                "at": timestamp,
            },
        )
        evaluation_id = _stable_id(
                "shadow-rollback-eval",
                {"history": history_id, "arm": failed_arm_id},
            )
        lifecycle_event = {
                "history_id": history_id,
                "event_type": "ROLLBACK",
                "from_arm_id": failed_arm_id,
                "to_arm_id": old_arm_id,
                "effective_at_ms": timestamp,
                "reason": ",".join(rollback.reasons),
                "evidence": {
                    "promoted_at_ms": promoted_at,
                    "current_metrics": current_metrics.to_dict(),
                    "old_champion_metrics": old_metrics.to_dict(),
                },
            }
        self._pending_promotion = None
        request = {
            "type": "ROLLBACK_REQUEST",
            "request_id": history_id,
            "experiment_id": self.experiment_id,
            "symbol": self.symbol,
            "generation": self.generation,
            "from_arm_id": failed_arm_id,
            "to_arm_id": old_arm_id,
            "expected_policy_hash": failed_definition.policy.policy_hash,
            "policy": old_definition.policy.to_dict(),
            "policy_hash": old_definition.policy.policy_hash,
            "parameter_hash": old_definition.parameter_hash,
            "effective_at_ms": timestamp,
            "reasons": list(rollback.reasons),
        }
        self._pending_lifecycle = {
            "action": "ROLLBACK",
            "request": deepcopy(request),
            "evaluation_id": evaluation_id,
            "arm_id": failed_arm_id,
            "decision": "ROLLBACK",
            "metrics": current_metrics.to_dict(),
            "lifecycle_event": lifecycle_event,
            "arm_status": "RETIRED",
            "source_arm_id": failed_arm_id,
            "target_arm_id": old_arm_id,
        }
        return request

    def _persist_event(self, arm_id: str, event: MarketEvent) -> None:
        runtime = self._runtimes[arm_id]
        state = runtime.state(arm_id)
        definition = self._definition_by_arm[arm_id]
        orders = self._changed_orders(arm_id, state.simulator.orders, event.event_id)
        observations = self._changed_observations(
            arm_id,
            state.observations,
            event.event_id,
        )
        signal = state.selected_signal
        decision = {
            "decision": str(state.order_decision),
            "direction": str(signal.direction if signal else ""),
            "profile_key": str(signal.profile_key if signal else ""),
            "terminal_at_ms": int(event.kline.close_time),
            "retain_at_capacity": bool(
                state.order_decision in {"OPEN", "OPENED", "STORAGE_ERROR"}
                or (
                    signal is not None
                    and signal.first_decisive_block
                    not in {"", "SCORE", "SESSION"}
                )
            ),
            "detail": {
                "parameter_hash": definition.parameter_hash,
                "analyzer_hash": definition.analyzer_hash,
                "policy_hash": definition.policy.policy_hash,
                "runtime_config_hash": (
                    str(signal.runtime_config_hash) if signal else ""
                ),
                "decision_id": str(signal.decision_id) if signal else "",
                "score": float(signal.score) if signal else 0.0,
                "threshold": float(signal.threshold) if signal else 0.0,
                "calculated_threshold": (
                    float(signal.calculated_threshold) if signal else 0.0
                ),
                "decision_inputs": (
                    deepcopy(signal.decision_inputs) if signal else {}
                ),
                "decision_trace": (
                    deepcopy(signal.decision_trace) if signal else []
                ),
                "first_decisive_block": (
                    str(signal.first_decisive_block) if signal else ""
                ),
            },
        }
        self.store.save_event_bundle(
            arm_id=arm_id,
            event_id=event.event_id,
            closed_at_ms=event.kline.close_time,
            decisions=(decision,),
            orders=orders,
            observations=observations,
            runtime_state=runtime.persistence_state(arm_id),
            daily_rollup=self._daily_rollup(runtime, arm_id, event),
            state_version=1,
        )

    def _persist_gap(self, arm_id: str, event: MarketEvent, reason: str) -> None:
        runtime = self._runtimes[arm_id]
        self.store.save_event_bundle(
            arm_id=arm_id,
            event_id=event.event_id,
            closed_at_ms=event.kline.close_time,
            decisions=(
                {
                    "decision": "GAP",
                    "terminal_at_ms": event.kline.close_time,
                    "retain_at_capacity": True,
                    "detail": {"reason": str(reason)},
                },
            ),
            runtime_state=runtime.persistence_state(arm_id),
            gap={"reason": str(reason), "detected_at_ms": event.kline.close_time},
            state_version=1,
        )

    def _changed_orders(
        self,
        arm_id: str,
        orders,
        event_id: str,
    ) -> tuple[dict[str, object], ...]:
        result = []
        signatures = self._order_signatures[arm_id]
        event_ids = self._order_event_ids[arm_id]
        for order in orders:
            signature = (
                order.status,
                order.result,
                order.settled_at,
                order.exit_price,
                float(order.pnl),
            )
            if signatures.get(order.id) == signature:
                continue
            decision_event_id = event_ids.setdefault(order.id, event_id)
            result.append(
                {
                    "order_id": order.id,
                    "decision_event_id": decision_event_id,
                    "direction": order.direction,
                    "status": order.status,
                    "result": order.result,
                    "opened_at_ms": order.opened_at,
                    "expires_at_ms": order.expires_at,
                    "settled_at_ms": order.settled_at,
                    "entry_price": order.entry_price,
                    "exit_price": order.exit_price,
                    "stake": order.stake,
                    "pnl": order.pnl,
                    "detail": order.to_dict(),
                }
            )
            signatures[order.id] = signature
        return tuple(result)

    def _changed_observations(
        self,
        arm_id: str,
        observations,
        event_id: str,
    ) -> tuple[dict[str, object], ...]:
        result = []
        signatures = self._observation_signatures[arm_id]
        event_ids = self._observation_event_ids[arm_id]
        lifecycle_fields = {"status", "result", "exit_price", "settled_at", "pnl"}
        core_fields = {
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
            "profile_key",
        }
        for observation in observations:
            signature = (
                observation.status,
                observation.result,
                observation.settled_at,
                observation.exit_price,
                float(observation.pnl),
            )
            if signatures.get(observation.observation_key) == signature:
                continue
            decision_event_id = event_ids.setdefault(
                observation.observation_key,
                event_id,
            )
            payload = observation.to_dict()
            detail = {
                key: value
                for key, value in payload.items()
                if key not in lifecycle_fields and key not in core_fields
            }
            result.append(
                {
                    "observation_key": observation.observation_key,
                    "decision_event_id": decision_event_id,
                    "strategy_family": observation.strategy_family,
                    "strategy_tag": observation.strategy_tag,
                    "direction": observation.direction,
                    "timeframe_minutes": observation.timeframe_minutes,
                    "level": observation.level,
                    "reason": observation.reason,
                    "entry_price": observation.entry_price,
                    "opened_at": observation.opened_at,
                    "expires_at": observation.expires_at,
                    "threshold_segment": observation.threshold_segment,
                    "status": observation.status,
                    "result": observation.result,
                    "exit_price": observation.exit_price,
                    "settled_at": observation.settled_at,
                    "pnl": observation.pnl,
                    "profile_key": observation.profile_key,
                    "detail": detail,
                }
            )
            signatures[observation.observation_key] = signature
        return tuple(result)

    @staticmethod
    def _daily_rollup(
        runtime: ShadowRuntime,
        arm_id: str,
        event: MarketEvent,
    ) -> dict[str, object] | None:
        day = datetime.fromtimestamp(
            event.kline.open_time / 1000,
            SHANGHAI_TIMEZONE,
        ).date().isoformat()
        return next(
            (row for row in runtime.daily_statistics(arm_id) if row["day"] == day),
            None,
        )

    def status(self) -> dict[str, object]:
        champion_id = self._champion_arm_id
        champion = self._runtimes[champion_id]
        metrics = champion.evaluation_metrics(champion_id)
        capacity = self.store.storage_capacity()
        return {
            "status": "RUNNING" if not self._closed else "STOPPED",
            "experiment_id": self.experiment_id,
            "symbol": self.symbol,
            "generation": self.generation,
            "arms": len(self._arm_ids),
            "active_arms": (
                len(self._arm_ids)
                - len(self._failed_arms)
                - len(self._retired_arms - set(self._failed_arms))
            ),
            "failed_arms": deepcopy(self._failed_arms),
            "retired_arms": sorted(self._retired_arms),
            "champion_arm_id": champion_id,
            "champion_parameter_hash": self._definition_by_arm[
                champion_id
            ].parameter_hash,
            "last_event_id": champion.cursor_event_id,
            "complete_days": metrics.complete_days,
            "settled_orders": metrics.settled_orders,
            "win_rate": metrics.win_rate,
            "long_win_rate": metrics.long_win_rate,
            "short_win_rate": metrics.short_win_rate,
            "average_orders_per_day": metrics.average_orders_per_day,
            "capacity_status": capacity.status,
            "database_bytes": capacity.database_bytes,
            "latest_evaluation": deepcopy(self._latest_evaluation),
            "pending_promotion": deepcopy(self._pending_promotion),
            "pending_lifecycle": deepcopy(self._pending_lifecycle),
            "next_evaluation": "07:50 Asia/Shanghai",
        }

    def compact(self, current_time_ms: int) -> dict[str, int]:
        return self.store.compact_terminal_details(
            before_ms=int(current_time_ms) - OBSERVATION_DETAIL_RETENTION_MS,
            compacted_at_ms=int(current_time_ms),
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        failures = []
        for runtime in self._runtimes.values():
            try:
                runtime.close()
            except Exception as exc:  # noqa: BLE001 - close every arm runtime.
                failures.append(exc)
        if failures:
            raise failures[0]


def _publish_status(status_queue, payload: dict[str, object]) -> None:
    try:
        status_queue.put_nowait(payload)
        return
    except queue.Full:
        pass
    try:
        status_queue.get_nowait()
    except queue.Empty:
        return
    try:
        status_queue.put_nowait(payload)
    except queue.Full:
        return


def _publish_lifecycle(lifecycle_queue, payload: dict[str, object]) -> None:
    lifecycle_queue.put(payload, timeout=1.0)


def run_shadow_optimizer_worker(
    *,
    event_queue,
    status_queue,
    lifecycle_queue,
    lifecycle_result_queue,
    stop_event,
    seed: Mapping[str, object],
    database_path: str | Path,
    max_challengers: int,
) -> None:
    optimizer = None
    scheduler = ShadowLifecycleScheduler()
    try:
        optimizer = ShadowOptimizer(
            seed=seed,
            store=ShadowSQLiteStore(database_path),
            max_challengers=max_challengers,
        )
        _publish_status(status_queue, optimizer.status())
        while not stop_event.is_set():
            while True:
                try:
                    lifecycle_result = lifecycle_result_queue.get_nowait()
                except queue.Empty:
                    break
                if isinstance(lifecycle_result, Mapping):
                    resolved = optimizer.resolve_lifecycle_request(
                        str(lifecycle_result.get("request_id") or ""),
                        lifecycle_result,
                    )
                    if (
                        not resolved
                        and str(lifecycle_result.get("status") or "").upper()
                        in {"APPLIED", "APPLIED_AT_DEADLINE"}
                        and isinstance(
                            lifecycle_result.get("formal_seed"),
                            Mapping,
                        )
                    ):
                        optimizer.close()
                        optimizer = ShadowOptimizer(
                            seed=lifecycle_result["formal_seed"],
                            store=ShadowSQLiteStore(database_path),
                            max_challengers=max_challengers,
                        )
                        scheduler = ShadowLifecycleScheduler()
            try:
                batch = event_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if getattr(batch, "reset", False):
                optimizer.close()
                optimizer = ShadowOptimizer(
                    seed=batch.seed,
                    store=ShadowSQLiteStore(database_path),
                    max_challengers=max_challengers,
                )
                scheduler = ShadowLifecycleScheduler()
            events = tuple(getattr(batch, "events", ()))
            if events:
                optimizer.process_batch(events)
                for message in optimizer.drain_lifecycle_requests():
                    _publish_lifecycle(lifecycle_queue, message)
                for message in scheduler.advance(
                    optimizer,
                    events[-1].kline.close_time,
                ):
                    if message.get("type") in {
                        "PROMOTION_REQUEST",
                        "ROLLBACK_REQUEST",
                    }:
                        _publish_lifecycle(lifecycle_queue, message)
                    else:
                        _publish_status(status_queue, message)
            _publish_status(status_queue, optimizer.status())
        _publish_status(status_queue, {**optimizer.status(), "status": "STOPPED"})
    except Exception as exc:  # noqa: BLE001 - 子进程失败只通过状态通道报告。
        _publish_status(
            status_queue,
            {"status": "FAILED", "error": f"{type(exc).__name__}: {exc}"},
        )
        raise
    finally:
        if optimizer is not None:
            optimizer.close()
