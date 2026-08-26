import hashlib
import json
import sqlite3
import threading
import time
import uuid
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any

from app.decision_context import (
    CONTEXT_VERSION,
    DecisionContext,
    RuntimeConfigSnapshot,
    _CREDENTIAL_KEYS as DECISION_CONTEXT_CREDENTIAL_KEYS,
)
from app.models import (
    ObservationSignal,
    Signal,
    SimulatedOrder,
    decision_context_reference,
    decision_linked_storage_payload,
)
from app.order_profile import (
    PROFILE_SUMMARY_SCHEMA_VERSION,
    order_profile_algorithm_fingerprint,
    sample_from_entry_snapshot,
    summarize_profile_guard_materialization,
    summarize_order_samples_with_guard,
)
from app.stake_progression import TWO_STAGE_VERSION, StakeProgressionCredit
from app.storage_capacity import (
    OrdinaryAuditCapacityError,
    RebuildableAuxiliaryCapacityError,
    StorageCapacity,
    StorageWriteClass,
    capacity_from_connection,
    configure_max_page_count,
    ensure_write_allowed,
    raise_for_sqlite_write_error,
)
from app.storage_schema import migrate
from app.wave_state import WAVE_RUNTIME_VERSION, WaveSnapshot


ORDER_PAGE_SIZES = (10, 20, 30, 50, 100)
OBSERVATION_PROMOTE_SAMPLE = 30
OBSERVATION_WATCH_SAMPLE = 10
SIGNAL_AUDIT_VERSION = "SIGNAL_AUDIT_V2"
MAX_PROFILE_MATERIALIZATIONS_PER_SYMBOL = 32
MAX_PROFILE_PARAMETER_COMBINATIONS_PER_SYMBOL = 16
PROFILE_SUMMARY_LEASE_MS = 30_000
PROFILE_SUMMARY_MAX_CAS_RETRIES = 4
_ORDINARY_SIGNAL_DECISIONS = frozenset({"WAIT", "BELOW_THRESHOLD"})
_LOWERCASE_HEX = frozenset("0123456789abcdef")
_DECISION_CONTEXT_KEYS = {
    "decision_id",
    "context_version",
    "runtime_config_hash",
    "strategy_build_id",
    "symbol",
    "closed_kline_at_ms",
    "candidate_origin",
    "inputs",
    "decision_trace",
    "first_decisive_block",
    "final_decision",
    "final_reason",
    "open_allowed",
    "observation_allowed",
    "selected_order_terms",
}
_DECISION_OUTCOME_KEYS = {
    "decision_trace",
    "first_decisive_block",
    "final_decision",
    "final_reason",
    "open_allowed",
    "observation_allowed",
    "selected_order_terms",
}
_DECISION_CONTEXT_COLUMNS = (
    "symbol",
    "decision_id",
    "context_version",
    "runtime_config_hash",
    "strategy_build_id",
    "created_at_ms",
    "closed_kline_at_ms",
    "direction",
    "profile_key",
    "candidate_origin",
    "input_payload",
    "outcome_payload",
)


@dataclass(frozen=True)
class DecisionAudit:
    signal: Signal
    decision: str
    created_at_ms: int
    audit_context: Mapping[str, Any] | None = None
    event_kind: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "signal", deepcopy(self.signal))


def _compact_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


_LINKED_CONTEXT_COLUMNS = """
decision_contexts.decision_id as linked_decision_id,
decision_contexts.context_version as linked_context_version,
decision_contexts.runtime_config_hash as linked_runtime_config_hash,
decision_contexts.strategy_build_id as linked_strategy_build_id,
decision_contexts.symbol as linked_symbol,
decision_contexts.closed_kline_at_ms as linked_closed_kline_at_ms,
decision_contexts.candidate_origin as linked_candidate_origin,
decision_contexts.input_payload as linked_input_payload,
decision_contexts.outcome_payload as linked_outcome_payload
"""


def linked_decision_context_select_columns() -> str:
    """Return the stable SELECT projection used to hydrate linked decisions."""
    return _LINKED_CONTEXT_COLUMNS.strip()

_ORDER_LIFECYCLE_COLUMNS = """
orders.order_id as lifecycle_order_id,
orders.status as lifecycle_status,
orders.result as lifecycle_result,
orders.opened_at as lifecycle_opened_at,
orders.settled_at as lifecycle_settled_at,
orders.exit_price as lifecycle_exit_price,
orders.pnl as lifecycle_pnl
"""

_OBSERVATION_LIFECYCLE_COLUMNS = """
observation_signals.observation_key as lifecycle_observation_key,
observation_signals.status as lifecycle_status,
observation_signals.result as lifecycle_result,
observation_signals.opened_at as lifecycle_opened_at,
observation_signals.expires_at as lifecycle_expires_at,
observation_signals.settled_at as lifecycle_settled_at,
observation_signals.exit_price as lifecycle_exit_price,
observation_signals.pnl as lifecycle_pnl
"""

_MUTABLE_LIFECYCLE_FIELDS = {
    "status",
    "result",
    "exit_price",
    "settled_at",
    "pnl",
}

_OBSERVATION_CANONICAL_TIMEFRAME_SQL = "coalesce(json_extract(decision_contexts.input_payload, '$.identity.timeframe_minutes'), json_extract(decision_contexts.input_payload, '$.signal.timeframe_minutes'), observation_signals.timeframe_minutes)"
_OBSERVATION_CANONICAL_DIRECTION_SQL = "coalesce(nullif(json_extract(decision_contexts.input_payload, '$.identity.direction'), ''), nullif(json_extract(decision_contexts.input_payload, '$.signal.direction'), ''), observation_signals.direction)"
_OBSERVATION_CANONICAL_FAMILY_SQL = "coalesce(nullif(json_extract(decision_contexts.input_payload, '$.identity.strategy_family'), ''), nullif(json_extract(decision_contexts.input_payload, '$.signal.strategy_family'), ''), observation_signals.strategy_family)"
_OBSERVATION_CANONICAL_TAG_SQL = "coalesce(nullif(json_extract(decision_contexts.input_payload, '$.identity.strategy_tag'), ''), nullif(json_extract(decision_contexts.input_payload, '$.signal.strategy_tag'), ''), observation_signals.strategy_tag)"
_OBSERVATION_CANONICAL_SEGMENT_SQL = "coalesce(nullif(json_extract(decision_contexts.input_payload, '$.identity.threshold_segment'), ''), nullif(json_extract(decision_contexts.input_payload, '$.signal.threshold_segment'), ''), observation_signals.threshold_segment)"
_OBSERVATION_CANONICAL_PROFILE_SQL = (
    f"cast({_OBSERVATION_CANONICAL_TIMEFRAME_SQL} as text) || '|' || "
    f"{_OBSERVATION_CANONICAL_FAMILY_SQL} || '|' || "
    f"{_OBSERVATION_CANONICAL_TAG_SQL} || '|' || "
    f"upper({_OBSERVATION_CANONICAL_DIRECTION_SQL}) || '|' || "
    f"upper({_OBSERVATION_CANONICAL_SEGMENT_SQL})"
)

_OBSERVATION_CANONICAL_FILTER_SQL = {
    "direction": _OBSERVATION_CANONICAL_DIRECTION_SQL,
    "family": _OBSERVATION_CANONICAL_FAMILY_SQL,
    "tag": _OBSERVATION_CANONICAL_TAG_SQL,
    "segment": _OBSERVATION_CANONICAL_SEGMENT_SQL,
    "profile": _OBSERVATION_CANONICAL_PROFILE_SQL,
    "origin": "coalesce(nullif(decision_contexts.candidate_origin, ''), nullif(observation_signals.candidate_origin, ''), 'UNKNOWN')",
    "qualification_state": "coalesce(nullif(json_extract(decision_contexts.input_payload, '$.signal.adaptive_profile_state.qualification_state'), ''), nullif(observation_signals.qualification_state, ''), 'UNKNOWN')",
    "adaptive_state": "coalesce(nullif(json_extract(decision_contexts.input_payload, '$.signal.adaptive_profile_state.state'), ''), nullif(json_extract(decision_contexts.input_payload, '$.signal.adaptive_profile_state.status'), ''), nullif(observation_signals.adaptive_state, ''), 'UNKNOWN')",
    "entry_structure_state": "coalesce(nullif(json_extract(decision_contexts.input_payload, '$.entry_structure.entry_structure_state'), ''), nullif(observation_signals.entry_structure_state, ''), 'UNKNOWN')",
    "entry_structure_bias": "coalesce(nullif(json_extract(decision_contexts.input_payload, '$.entry_structure.entry_structure_bias'), ''), nullif(observation_signals.entry_structure_bias, ''), 'UNKNOWN')",
    "active_level_source": "coalesce(nullif(json_extract(decision_contexts.input_payload, '$.entry_structure.active_level_source'), ''), nullif(observation_signals.active_level_source, ''), 'UNKNOWN')",
}

_ORDER_CANONICAL_DIRECTION_SQL = "coalesce(nullif(decision_contexts.direction, ''), nullif(json_extract(decision_contexts.input_payload, '$.identity.direction'), ''), nullif(json_extract(decision_contexts.input_payload, '$.signal.direction'), ''), nullif(json_extract(orders.payload, '$.direction'), ''), '')"
_ORDER_CANONICAL_LEVEL_SQL = "coalesce(nullif(json_extract(decision_contexts.input_payload, '$.identity.level'), ''), nullif(json_extract(decision_contexts.input_payload, '$.signal.level'), ''), nullif(json_extract(orders.payload, '$.level'), ''), '')"
_ORDER_CANONICAL_SEGMENT_SQL = "coalesce(nullif(json_extract(decision_contexts.input_payload, '$.identity.threshold_segment'), ''), nullif(json_extract(decision_contexts.input_payload, '$.signal.threshold_segment'), ''), nullif(json_extract(orders.payload, '$.threshold_segment'), ''), '')"

_DASHBOARD_OMITTED_AUDIT_FIELDS = {
    "decision_inputs",
    "decision_trace",
    "first_decisive_block",
    "quality_score_inputs",
    "quality_score_components",
    "direction_pulse_shadow",
}


def _dashboard_payload(model: SimulatedOrder | ObservationSignal) -> dict[str, Any]:
    payload = model.to_dict()
    for field in _DASHBOARD_OMITTED_AUDIT_FIELDS:
        payload.pop(field, None)
    return payload


def _joined_decision_context(row: Mapping[str, Any]) -> dict[str, Any] | None:
    row = dict(row)
    decision_id = row.get("linked_decision_id")
    if decision_id is None:
        return None
    input_payload = row.get("linked_input_payload")
    outcome_payload = row.get("linked_outcome_payload")
    if not isinstance(input_payload, str) or not isinstance(outcome_payload, str):
        raise ValueError("linked decision context payload is missing")
    inputs = _parse_canonical_json(input_payload, "input_payload")
    outcome = _parse_canonical_json(outcome_payload, "outcome_payload")
    if not isinstance(inputs, dict) or not isinstance(outcome, dict):
        raise ValueError("linked decision context payload is malformed")
    try:
        context = DecisionContext(
            decision_id=str(decision_id),
            context_version=str(row.get("linked_context_version") or ""),
            runtime_config_hash=str(row.get("linked_runtime_config_hash") or ""),
            strategy_build_id=str(row.get("linked_strategy_build_id") or ""),
            symbol=str(row.get("linked_symbol") or ""),
            closed_kline_at_ms=int(row.get("linked_closed_kline_at_ms")),
            candidate_origin=str(row.get("linked_candidate_origin") or ""),
            inputs=inputs,
            decision_trace=outcome["decision_trace"],
            first_decisive_block=outcome["first_decisive_block"],
            final_decision=outcome["final_decision"],
            final_reason=outcome["final_reason"],
            open_allowed=outcome["open_allowed"],
            observation_allowed=outcome["observation_allowed"],
            selected_order_terms=outcome.get("selected_order_terms", {}),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("linked decision context is invalid") from error
    return context.to_dict()


def _require_matching_field(
    payload: Mapping[str, Any],
    field: str,
    expected: object,
) -> None:
    if field in payload and payload[field] != expected:
        raise ValueError(f"persisted {field} does not match decision context")


def _plain_entry_structure(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_entry_structure(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_entry_structure(item) for item in value]
    return value


def _canonical_entry_structure(inputs: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(inputs, Mapping):
        raise ValueError("linked decision structure inputs are malformed")
    signal = inputs.get("signal", {})
    if not isinstance(signal, Mapping):
        raise ValueError("linked decision signal structure is malformed")

    has_top_level = "entry_structure" in inputs
    has_signal_view = "entry_structure_shadow" in signal
    if has_top_level:
        top_level = inputs["entry_structure"]
        if not isinstance(top_level, Mapping):
            raise ValueError("linked decision entry structure must be an object")
        canonical = _plain_entry_structure(top_level)
        if not isinstance(canonical, dict):
            raise ValueError("linked decision entry structure must be an object")
        if has_signal_view:
            signal_view = signal["entry_structure_shadow"]
            if not isinstance(signal_view, Mapping):
                raise ValueError("linked signal entry structure must be an object")
            if canonical != _plain_entry_structure(signal_view):
                raise ValueError("linked decision entry structure views do not match")
        return canonical

    if not has_signal_view:
        return {}
    signal_view = signal["entry_structure_shadow"]
    if not isinstance(signal_view, Mapping):
        raise ValueError("legacy signal entry structure must be an object")
    canonical = _plain_entry_structure(signal_view)
    if not isinstance(canonical, dict):
        raise ValueError("legacy signal entry structure must be an object")
    return canonical


def _require_matching_entry_structure(
    value: object,
    expected: Mapping[str, Any],
    name: str,
) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} entry structure must be an object")
    if _plain_entry_structure(value) != _plain_entry_structure(expected):
        raise ValueError(f"{name} entry structure must match decision context")


def _canonical_model_fields(
    payload: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    inputs = context["inputs"]
    identity = inputs.get("identity")
    signal = inputs.get("signal", {})
    score = inputs.get("score", {})
    market = inputs.get("market", {})
    if not all(isinstance(item, Mapping) for item in (identity, signal, score, market)):
        raise ValueError("linked canonical model inputs are malformed")
    entry_structure = _canonical_entry_structure(inputs)

    is_order = "id" in payload
    is_observation = "observation_key" in payload
    if is_order == is_observation:
        raise ValueError("linked decision model type is ambiguous")
    model_type = SimulatedOrder if is_order else ObservationSignal
    model_fields = {item.name for item in fields(model_type)}
    canonical = {
        field: deepcopy(value)
        for field, value in signal.items()
        if field in model_fields and field not in _MUTABLE_LIFECYCLE_FIELDS
    }
    for field in (
        "direction",
        "profile_key",
        "strategy_family",
        "strategy_tag",
        "order_slot",
        "order_slot_scope",
        "timeframe_minutes",
        "threshold_segment",
        "level",
    ):
        if field in identity and field in model_fields:
            canonical[field] = deepcopy(identity[field])

    canonical.update(
        {
            "decision_id": context["decision_id"],
            "context_version": context["context_version"],
            "runtime_config_hash": context["runtime_config_hash"],
            "strategy_build_id": context["strategy_build_id"],
            "candidate_origin": context["candidate_origin"],
            "decision_inputs": deepcopy(inputs),
            "decision_trace": deepcopy(context["decision_trace"]),
            "first_decisive_block": context["first_decisive_block"],
            "quality_score": deepcopy(score.get("quality_score", 0.0)),
            "quality_score_version": deepcopy(
                score.get("quality_score_version", "")
            ),
            "quality_score_mode": deepcopy(score.get("quality_score_mode", "")),
            "quality_score_context": deepcopy(
                score.get("quality_score_context", "")
            ),
            "quality_score_components": deepcopy(
                score.get("quality_score_components", {})
            ),
            "quality_score_inputs": deepcopy(
                score.get("quality_score_inputs", {})
            ),
            "adaptive_profile_state": deepcopy(
                signal.get("adaptive_profile_state", {})
            ),
            "entry_structure_shadow": deepcopy(entry_structure),
        }
    )
    opened_at = (
        int(market["candidate_time_ms"])
        if "candidate_time_ms" in market
        else (
            int(context["closed_kline_at_ms"])
            if signal
            else None
        )
    )
    entry_price = market.get("entry_price", signal.get("price"))
    timeframe_minutes = canonical.get("timeframe_minutes")

    if is_order:
        terms = context.get("selected_order_terms")
        if not isinstance(terms, Mapping):
            raise ValueError("linked order selected order terms are malformed")
        term_fields = {
            "stake": "stake",
            "win_return": "win_return",
            "progression_step": "stake_progression_step",
            "progression_source_order_id": "stake_progression_source_order_id",
            "progression_version": "stake_progression_version",
            "expires_at": "expires_at",
            "timeframe_minutes": "timeframe_minutes",
            "order_slot": "order_slot",
            "order_slot_scope": "order_slot_scope",
            "direction": "direction",
            "entry_price": "entry_price",
        }
        for source, target in term_fields.items():
            if source in terms:
                canonical[target] = deepcopy(terms[source])
        if opened_at is not None:
            canonical["opened_at"] = opened_at
    else:
        if opened_at is not None:
            canonical["opened_at"] = opened_at
        if entry_price is not None:
            canonical["entry_price"] = float(entry_price)
        if opened_at is not None and timeframe_minutes is not None:
            canonical["expires_at"] = opened_at + int(timeframe_minutes) * 60_000
        if "edge" in score:
            canonical["edge"] = deepcopy(score["edge"])
        canonical["source_decision"] = context["final_decision"]
        canonical["observe_only"] = True
    return {key: value for key, value in canonical.items() if key in model_fields}


def _apply_lifecycle_row_authority(
    payload: dict[str, Any],
    row: Mapping[str, Any],
    *,
    has_context: bool,
) -> dict[str, Any]:
    row = dict(row)
    if "lifecycle_order_id" in row:
        if "id" in payload and int(payload["id"]) != int(row["lifecycle_order_id"]):
            raise ValueError("persisted id does not match orders row")
        payload["id"] = int(row["lifecycle_order_id"])
    elif "lifecycle_observation_key" in row:
        row_key = str(row["lifecycle_observation_key"])
        if "observation_key" in payload and str(payload["observation_key"]) != row_key:
            raise ValueError(
                "persisted observation_key does not match observation row"
            )
        payload["observation_key"] = row_key

    for field in ("opened_at", "expires_at"):
        row_field = f"lifecycle_{field}"
        if row_field not in row:
            continue
        row_value = int(row[row_field])
        if has_context and field in payload and int(payload[field]) != row_value:
            raise ValueError(f"persisted {field} does not match lifecycle row")
        payload[field] = row_value

    for field in _MUTABLE_LIFECYCLE_FIELDS:
        row_field = f"lifecycle_{field}"
        if row_field in row:
            payload[field] = deepcopy(row[row_field])
    return payload


def _hydrate_decision_linked_payload(
    payload: dict[str, Any],
    row: Mapping[str, Any],
    *,
    apply_lifecycle_authority: bool = True,
) -> dict[str, Any]:
    context = _joined_decision_context(row)
    marker = payload.get("decision_context_ref")
    if context is None:
        if marker is not None:
            raise ValueError("decision context reference cannot be resolved")
        if apply_lifecycle_authority:
            return _apply_lifecycle_row_authority(payload, row, has_context=False)
        return payload

    inputs = context["inputs"]
    identity = inputs.get("identity")
    if not isinstance(identity, dict):
        raise ValueError("linked decision identity is malformed")
    expected_reference = decision_context_reference(
        decision_id=context["decision_id"],
        context_version=context["context_version"],
        runtime_config_hash=context["runtime_config_hash"],
        strategy_build_id=context["strategy_build_id"],
        candidate_origin=context["candidate_origin"],
        identity=identity,
    )
    if marker is not None:
        if not isinstance(marker, Mapping) or dict(marker) != expected_reference:
            raise ValueError("decision context reference does not match persisted metadata")

    for field in (
        "decision_id",
        "context_version",
        "runtime_config_hash",
        "strategy_build_id",
        "candidate_origin",
    ):
        _require_matching_field(payload, field, context[field])

    identity_fields = (
        "direction",
        "profile_key",
        "strategy_family",
        "strategy_tag",
        "order_slot",
        "order_slot_scope",
        "timeframe_minutes",
        "threshold_segment",
        "level",
    )
    for field in identity_fields:
        if field in identity:
            _require_matching_field(payload, field, identity[field])

    canonical_fields = _canonical_model_fields(payload, context)
    for field, expected in canonical_fields.items():
        _require_matching_field(payload, field, expected)

    payload = dict(payload)
    payload.pop("decision_context_ref", None)
    for field, expected in canonical_fields.items():
        payload[field] = deepcopy(expected)
    if apply_lifecycle_authority:
        return _apply_lifecycle_row_authority(payload, row, has_context=True)
    return payload


def hydrate_decision_linked_payload(
    payload: dict[str, Any],
    row: Mapping[str, Any],
    *,
    apply_lifecycle_authority: bool = True,
) -> dict[str, Any]:
    """Hydrate a persisted model payload through the public storage contract."""
    return _hydrate_decision_linked_payload(
        payload,
        row,
        apply_lifecycle_authority=apply_lifecycle_authority,
    )


def _canonical_storage_projection(
    payload: dict[str, Any],
    row: Mapping[str, Any],
    *,
    apply_lifecycle_authority: bool,
) -> dict[str, Any]:
    hydrated = _hydrate_decision_linked_payload(
        payload,
        row,
        apply_lifecycle_authority=apply_lifecycle_authority,
    )
    if "id" in hydrated:
        model_type = SimulatedOrder
    elif "observation_key" in hydrated:
        model_type = ObservationSignal
    else:
        raise ValueError("decision-linked payload has no model identity")
    accepted = {item.name for item in fields(model_type)}
    values = {key: value for key, value in hydrated.items() if key in accepted}
    if model_type is SimulatedOrder and "calculated_threshold" not in values:
        values["calculated_threshold"] = float(values.get("threshold", 0.0))
    try:
        model = model_type(**values)
    except (TypeError, ValueError) as error:
        raise ValueError("decision-linked payload is malformed") from error
    return decision_linked_storage_payload(model)


def _decision_linked_validation_payload(
    model: SimulatedOrder | ObservationSignal,
    storage_payload: Mapping[str, Any],
) -> dict[str, Any]:
    payload = model.to_dict()
    if "decision_context_ref" in storage_payload:
        payload["decision_context_ref"] = deepcopy(
            storage_payload["decision_context_ref"]
        )
    return payload


def _profile_key_for_signal(signal: Signal) -> str:
    profile_key = str(signal.profile_key or "")
    if profile_key:
        return profile_key
    direction = str(signal.direction or signal.observe_direction or "").upper()
    return "|".join(
        [
            str(int(signal.timeframe_minutes or 0)),
            str(signal.strategy_family or "unknown"),
            str(signal.strategy_tag or "unknown"),
            direction,
            str(signal.threshold_segment or "GLOBAL").upper(),
        ]
    )


def _compact_result_sequence_guard(value: object) -> dict[str, object]:
    source = value if isinstance(value, Mapping) else {}
    return {
        key: source[key]
        for key in (
            "status",
            "code",
            "blocked",
            "scope",
            "direction",
            "consecutive_losses",
            "last_settled_at",
            "pause_until",
        )
        if key in source
    }


def _compact_wave_batch_guard(value: object) -> dict[str, object]:
    source = value if isinstance(value, Mapping) else {}
    return {
        key: source[key]
        for key in (
            "status",
            "code",
            "mode",
            "blocked",
            "allow_progression",
            "current_batch_id",
            "batch_orders",
            "batch_wins",
            "batch_losses",
            "failed_batches",
            "pause_until",
        )
        if key in source
    }


def _compact_profile_degradation_guard(value: object) -> dict[str, object]:
    source = value if isinstance(value, Mapping) else {}
    return {
        key: source[key]
        for key in (
            "status",
            "code",
            "blocked",
            "allow_progression",
            "profile_key",
            "daily_profile_version",
            "consecutive_losses",
            "last_loss_settled_at",
            "pause_until",
            "probe_order_id",
            "triggered_at",
        )
        if key in source
    }


def _compact_profile_health_guard(value: object) -> dict[str, object]:
    source = value if isinstance(value, Mapping) else {}
    return {
        key: source[key]
        for key in (
            "status",
            "code",
            "blocked",
            "direction",
            "evaluated_at",
            "next_evaluation_at",
            "sample_size",
            "wins",
            "losses",
            "win_rate",
            "pnl",
            "ev",
            "allow_second_order",
            "allow_progression",
        )
        if key in source
    }


def _compact_rolling_edge_guard(value: object) -> dict[str, object]:
    source = value if isinstance(value, Mapping) else {}
    return {
        key: source[key]
        for key in (
            "status",
            "code",
            "sample_size",
            "wins",
            "losses",
            "win_rate",
            "pnl",
            "ev",
            "blocked",
            "key",
            "edge",
            "threshold",
        )
        if key in source
    }


def _compact_time_period_guard(value: object) -> dict[str, object]:
    source = value if isinstance(value, Mapping) else {}
    return {
        key: source[key]
        for key in ("status", "code", "enabled", "blocked", "local_hour", "window")
        if key in source
    }


def _compact_profile_guard(value: object) -> dict[str, object]:
    source = value if isinstance(value, Mapping) else {}
    return {
        key: source[key]
        for key in (
            "status",
            "code",
            "enabled",
            "observe_only",
            "blocked",
            "hit_keys",
            "cache_status",
            "source_revision",
            "current_revision",
            "stale",
        )
        if key in source
    }


def _compact_direction_pulse(signal: Signal) -> dict[str, object]:
    source = (
        signal.direction_pulse_shadow
        if isinstance(signal.direction_pulse_shadow, Mapping)
        else {}
    )
    windows = source.get("windows")
    windows = windows if isinstance(windows, Mapping) else {}
    status_rank = {"UNKNOWN": 0, "WARMUP": 1, "NORMAL": 2, "WATCH": 3, "DEGRADED": 4}
    action_rank = {"ALLOW": 0, "BLOCK_SECOND": 1, "BLOCK_DIRECTION": 2}
    status = "UNKNOWN"
    code = "ALLOW"
    for item in windows.values():
        if not isinstance(item, Mapping):
            continue
        candidate_status = str(item.get("status") or "UNKNOWN").upper()
        candidate_code = str(
            item.get("code") or item.get("hypothetical_action") or "ALLOW"
        ).upper()
        if status_rank.get(candidate_status, 0) > status_rank.get(status, 0):
            status = candidate_status
        if action_rank.get(candidate_code, 0) > action_rank.get(code, 0):
            code = candidate_code
    return {
        "version": str(source.get("version") or ""),
        "mode": str(source.get("mode") or ""),
        "direction": str(source.get("direction") or signal.direction or "").upper(),
        "order_slot": str(source.get("order_slot") or signal.order_slot or ""),
        "evaluated_at": int(source.get("evaluated_at") or 0),
        "status": status,
        "code": code,
        "bias": code,
    }


def _signal_audit_guard_states(
    signal: Signal,
    audit_context: Mapping[str, object],
) -> dict[str, object]:
    return {
        "rolling_edge": _compact_rolling_edge_guard(
            audit_context.get("rolling_edge")
        ),
        "result_sequence": _compact_result_sequence_guard(
            audit_context.get("result_sequence_guard")
        ),
        "wave_batch": _compact_wave_batch_guard(
            audit_context.get("wave_batch_guard")
        ),
        "profile_degradation": _compact_profile_degradation_guard(
            audit_context.get("profile_degradation_guard")
        ),
        "profile_health": _compact_profile_health_guard(
            audit_context.get("profile_health_guard")
        ),
        "time_period": _compact_time_period_guard(
            audit_context.get("time_period_guard")
        ),
        "profile_guard": _compact_profile_guard(audit_context.get("profile_guard")),
        "wave_signal": {
            "mode": str(signal.wave_guard_mode or ""),
            "status": str(signal.wave_guard_status or "UNKNOWN"),
            "code": str(signal.wave_guard_status or "UNKNOWN"),
        },
    }


def _signal_audit_state_code(
    signal: Signal,
    audit_context: Mapping[str, object],
) -> dict[str, object]:
    adaptive = signal.adaptive_profile_state
    structure = signal.entry_structure_shadow
    guards = _signal_audit_guard_states(signal, audit_context)
    pulse = _compact_direction_pulse(signal)
    return {
        "guard_state_hash": hashlib.sha256(
            _compact_json({"guards": guards, "direction_pulse": pulse}).encode("utf-8")
        ).hexdigest(),
        "regime": str(signal.regime or ""),
        "risk_flags": str(signal.risk_flags or ""),
        "daily_profile_selected": bool(signal.daily_profile_selected),
        "daily_profile_version": str(signal.daily_profile_version or ""),
        "order_slot": str(signal.order_slot or ""),
        "order_slot_scope": str(signal.order_slot_scope or ""),
        "profile_health_status": str(signal.profile_health_status or ""),
        "adaptive_status": str(
            adaptive.get("status", "") if isinstance(adaptive, Mapping) else ""
        ),
        "structure_state": str(
            structure.get("entry_structure_state", structure.get("state", ""))
            if isinstance(structure, Mapping)
            else ""
        ),
        "structure_bias": str(
            structure.get("entry_structure_bias", structure.get("bias", ""))
            if isinstance(structure, Mapping)
            else ""
        ),
    }


def _signal_audit_payload(
    signal: Signal,
    decision: str,
    audit_context: Mapping[str, object],
    reason_code: str,
) -> dict[str, object]:
    structure = signal.entry_structure_shadow
    canonical_structure = (
        deepcopy(_plain_entry_structure(structure))
        if isinstance(structure, Mapping)
        else {}
    )
    adaptive = signal.adaptive_profile_state
    compact_adaptive = {}
    if isinstance(adaptive, Mapping):
        adaptive_keys = (
            "profile_key",
            "qualification_state",
            "qualification_version",
            "qualification_evaluated_at",
            "joint_failure_runs",
            "status",
            "reason",
            "evaluated_at",
            "n12",
            "n20",
        )
        compact_adaptive = {
            key: adaptive[key] for key in adaptive_keys if key in adaptive
        }
    return {
        "identity": {
            "decision_id": str(signal.decision_id or ""),
            "context_version": str(signal.context_version or ""),
            "runtime_config_hash": str(signal.runtime_config_hash or ""),
            "strategy_build_id": str(signal.strategy_build_id or ""),
            "candidate_origin": str(signal.candidate_origin or ""),
            "profile_key": _profile_key_for_signal(signal),
            "strategy_family": str(signal.strategy_family or "unknown"),
            "strategy_tag": str(signal.strategy_tag or "unknown"),
            "daily_profile_version": str(signal.daily_profile_version or "STATIC"),
            "order_slot": str(signal.order_slot or "UNKNOWN"),
        },
        "metrics": {
            "price": signal.price,
            "score": signal.score,
            "threshold": signal.threshold,
            "calculated_threshold": signal.calculated_threshold,
            "volume_ratio": signal.volume_ratio,
            "volume_threshold": signal.volume_threshold,
            "price_change_pct": signal.price_change_pct,
            "price_position": signal.price_position,
            "close_strength": signal.close_strength,
            "analysis_window_minutes": signal.analysis_window_minutes,
            "threshold_window_minutes": signal.threshold_window_minutes,
            "mtf_10m_bias": signal.mtf_10m_bias,
            "macd_histogram": signal.macd_histogram,
            "macd_histogram_delta": signal.macd_histogram_delta,
            "macd_histogram_threshold": signal.macd_histogram_threshold,
            "macd_delta_threshold": signal.macd_delta_threshold,
            "rsi": signal.rsi,
            "rsi_lower_threshold": signal.rsi_lower_threshold,
            "rsi_upper_threshold": signal.rsi_upper_threshold,
            "bollinger_position": signal.bollinger_position,
            "bollinger_width": signal.bollinger_width,
            "bollinger_lower_threshold": signal.bollinger_lower_threshold,
            "bollinger_upper_threshold": signal.bollinger_upper_threshold,
            "indicator_profile_segment": signal.indicator_profile_segment,
            "indicator_profile_sample_size": signal.indicator_profile_sample_size,
            "fear_greed_value": signal.fear_greed_value,
            "fear_greed_adjustment": signal.fear_greed_adjustment,
            "risk_flags": signal.risk_flags,
        },
        "profile": {
            "selected": bool(signal.daily_profile_selected),
            "session_allowed": bool(signal.session_allowed),
            "session_sample_size": signal.session_sample_size,
            "session_win_rate": signal.session_win_rate,
            "session_ev": signal.session_ev,
            "profile_health_status": str(signal.profile_health_status or ""),
            "profile_health_sample_size": signal.profile_health_sample_size,
            "profile_health_win_rate": signal.profile_health_win_rate,
            "profile_health_ev": signal.profile_health_ev,
            "adaptive": compact_adaptive,
        },
        "structure": canonical_structure,
        "guards": _signal_audit_guard_states(signal, audit_context),
        "direction_pulse": _compact_direction_pulse(signal),
        "state_code": _signal_audit_state_code(signal, audit_context),
        "decision": {
            "final": str(decision),
            "reason_code": reason_code,
            "first_decisive_block": str(signal.first_decisive_block or ""),
        },
    }


def _signal_audit_aggregation_key(
    symbol: str,
    signal: Signal,
    decision: str,
    created_at_ms: int,
    reason_code: str,
    audit_context: Mapping[str, object],
) -> str:
    identity = {
        "symbol": symbol,
        "bucket_10m": int(created_at_ms) // 600_000,
        "decision": decision,
        "reason_code": reason_code,
        "profile_key": _profile_key_for_signal(signal),
        "direction": str(signal.direction or signal.observe_direction or "").upper(),
        "context_version": str(signal.context_version or ""),
        "runtime_config_hash": str(signal.runtime_config_hash or ""),
        "first_decisive_block": str(signal.first_decisive_block or ""),
        "state": _signal_audit_state_code(signal, audit_context),
    }
    return hashlib.sha256(_compact_json(identity).encode("utf-8")).hexdigest()


def _parse_canonical_json(payload: object, name: str) -> object:
    if not isinstance(payload, str):
        raise ValueError(f"stored {name} must be a string")
    try:
        value = json.loads(payload)
        canonical_payload = _compact_json(value)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise ValueError(f"{name} must contain valid finite JSON") from error
    if canonical_payload != payload:
        raise ValueError(f"{name} must use canonical JSON encoding")
    return value


def _parse_runtime_config_payload(payload: object) -> object:
    value = _parse_canonical_json(payload, "canonical_payload")

    def reject_credentials(item: object) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                if key in DECISION_CONTEXT_CREDENTIAL_KEYS:
                    raise ValueError(
                        f"canonical_payload contains credential key: {key}"
                    )
                reject_credentials(nested)
        elif isinstance(item, list):
            for nested in item:
                reject_credentials(nested)

    reject_credentials(value)
    return value


def _require_string(
    value: object,
    name: str,
    *,
    non_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if non_empty and not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value


def _require_runtime_config_hash(value: object) -> str:
    digest = _require_string(value, "runtime_config_hash")
    if len(digest) != 64 or any(character not in _LOWERCASE_HEX for character in digest):
        raise ValueError("runtime_config_hash must be a 64-character lowercase hex digest")
    return digest


def _runtime_snapshot_values(
    snapshot: RuntimeConfigSnapshot,
) -> tuple[str, str, int]:
    if not isinstance(snapshot, RuntimeConfigSnapshot):
        raise TypeError("snapshot must be a RuntimeConfigSnapshot")
    runtime_config_hash = _require_runtime_config_hash(snapshot.hash)
    canonical_payload = _require_string(
        snapshot.canonical_payload,
        "canonical_payload",
    )
    _require_string(
        snapshot.strategy_build_id,
        "strategy_build_id",
        non_empty=True,
    )
    _parse_runtime_config_payload(canonical_payload)
    actual_hash = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
    if actual_hash != runtime_config_hash:
        raise ValueError("runtime_config_hash does not match canonical_payload")
    return runtime_config_hash, canonical_payload, len(canonical_payload.encode("utf-8"))


def _runtime_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    snapshot = dict(row)
    try:
        runtime_config_hash = _require_runtime_config_hash(
            snapshot["runtime_config_hash"]
        )
        context_version = _require_string(
            snapshot["context_version"],
            "context_version",
        )
        strategy_build_id = _require_string(
            snapshot["strategy_build_id"],
            "strategy_build_id",
            non_empty=True,
        )
        canonical_payload = _require_string(
            snapshot["canonical_payload"],
            "canonical_payload",
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("stored runtime configuration metadata is malformed") from error
    if context_version != CONTEXT_VERSION:
        raise ValueError("stored runtime configuration context version is unsupported")
    _parse_runtime_config_payload(canonical_payload)
    if hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest() != runtime_config_hash:
        raise ValueError("stored runtime configuration hash does not match its payload")
    payload_bytes = snapshot.get("payload_bytes")
    if type(payload_bytes) is not int or payload_bytes != len(
        canonical_payload.encode("utf-8")
    ):
        raise ValueError("stored runtime configuration payload byte count is invalid")
    created_at_ms = snapshot.get("created_at_ms")
    if type(created_at_ms) is not int or created_at_ms < 0:
        raise ValueError("stored runtime configuration timestamp is invalid")
    snapshot["strategy_build_id"] = strategy_build_id
    return snapshot


def _decision_context_values(context: DecisionContext) -> dict[str, Any]:
    if not isinstance(context, DecisionContext):
        raise TypeError("context must be a DecisionContext")
    payload = context.to_dict()
    if not isinstance(payload, dict) or set(payload) != _DECISION_CONTEXT_KEYS:
        raise ValueError("decision context has an invalid serialized shape")
    try:
        normalized_context = DecisionContext(**payload)
    except (TypeError, ValueError) as error:
        raise ValueError("decision context is invalid") from error
    normalized = normalized_context.to_dict()

    symbol = _require_string(normalized["symbol"], "symbol", non_empty=True)
    decision_id = _require_string(
        normalized["decision_id"],
        "decision_id",
        non_empty=True,
    )
    context_version = _require_string(
        normalized["context_version"],
        "context_version",
    )
    if context_version != CONTEXT_VERSION:
        raise ValueError("decision context version is unsupported")
    runtime_config_hash = _require_runtime_config_hash(
        normalized["runtime_config_hash"]
    )
    strategy_build_id = _require_string(
        normalized["strategy_build_id"],
        "strategy_build_id",
        non_empty=True,
    )
    candidate_origin = _require_string(
        normalized["candidate_origin"],
        "candidate_origin",
        non_empty=True,
    )
    closed_kline_at_ms = normalized["closed_kline_at_ms"]
    if type(closed_kline_at_ms) is not int or closed_kline_at_ms < 0:
        raise ValueError("closed_kline_at_ms must be a non-negative integer")

    inputs = normalized["inputs"]
    if not isinstance(inputs, dict):
        raise TypeError("decision context inputs must be an object")
    direction = ""
    profile_key = ""
    identity = inputs.get("identity")
    if isinstance(identity, Mapping):
        direction = _require_string(identity.get("direction", ""), "direction")
        profile_key = _require_string(identity.get("profile_key", ""), "profile_key")

    outcome = {
        "decision_trace": normalized["decision_trace"],
        "first_decisive_block": normalized["first_decisive_block"],
        "final_decision": normalized["final_decision"],
        "final_reason": normalized["final_reason"],
        "open_allowed": normalized["open_allowed"],
        "observation_allowed": normalized["observation_allowed"],
        "selected_order_terms": normalized["selected_order_terms"],
    }
    if not isinstance(outcome["decision_trace"], list):
        raise TypeError("decision_trace must be a list")

    return {
        "symbol": symbol,
        "decision_id": decision_id,
        "context_version": context_version,
        "runtime_config_hash": runtime_config_hash,
        "strategy_build_id": strategy_build_id,
        "created_at_ms": closed_kline_at_ms,
        "closed_kline_at_ms": closed_kline_at_ms,
        "direction": direction,
        "profile_key": profile_key,
        "candidate_origin": candidate_origin,
        "input_payload": _compact_json(inputs),
        "outcome_payload": _compact_json(outcome),
    }


def page_order_list(
    orders: list[SimulatedOrder],
    *,
    page: int = 1,
    page_size: int = 20,
    direction: str = "",
    level: str = "",
    segment: str = "",
    result: str = "",
) -> dict[str, Any]:
    ordered = sorted(orders, key=lambda item: (item.opened_at, item.id), reverse=True)
    filter_options = _order_filter_options(ordered)
    filters = {
        "direction": _clean_filter(direction),
        "level": _clean_filter(level),
        "segment": _clean_filter(segment),
        "result": _clean_filter(result),
    }
    filtered = [order for order in ordered if _order_matches(order, filters)]
    normalized_page_size = _normalize_page_size(page_size)
    total = len(filtered)
    total_pages = max(1, (total + normalized_page_size - 1) // normalized_page_size)
    normalized_page = min(max(1, int(page or 1)), total_pages)
    start = (normalized_page - 1) * normalized_page_size
    end = start + normalized_page_size
    return {
        "orders": [order.to_dict() for order in filtered[start:end]],
        "total": total,
        "page": normalized_page,
        "page_size": normalized_page_size,
        "total_pages": total_pages,
        "filters": filters,
        "filter_options": filter_options,
    }


def page_observation_list(
    observations: list[ObservationSignal],
    *,
    page: int = 1,
    page_size: int = 20,
    direction: str = "",
    family: str = "",
    tag: str = "",
    segment: str = "",
    result: str = "",
) -> dict[str, Any]:
    ordered = sorted(observations, key=lambda item: (item.opened_at, item.observation_key), reverse=True)
    filter_options = _observation_filter_options(ordered)
    filters = {
        "direction": _clean_filter(direction),
        "family": _clean_filter(family),
        "tag": _clean_filter(tag),
        "segment": _clean_filter(segment),
        "result": _clean_filter(result),
    }
    filtered = [observation for observation in ordered if _observation_matches(observation, filters)]
    normalized_page_size = _normalize_page_size(page_size)
    total = len(filtered)
    total_pages = max(1, (total + normalized_page_size - 1) // normalized_page_size)
    normalized_page = min(max(1, int(page or 1)), total_pages)
    start = (normalized_page - 1) * normalized_page_size
    end = start + normalized_page_size
    return {
        "observations": [observation.to_dict() for observation in filtered[start:end]],
        "total": total,
        "page": normalized_page,
        "page_size": normalized_page_size,
        "total_pages": total_pages,
        "filters": filters,
        "filter_options": filter_options,
    }


def summarize_observations(
    observations: list[ObservationSignal],
    *,
    group_limit: int = 50,
) -> dict[str, Any]:
    total = _empty_observation_stats()
    groups: dict[tuple[int, str, str, str, str], dict[str, Any]] = {}
    for observation in observations:
        _accumulate_observation_stats(total, observation)
        key = (
            observation.timeframe_minutes,
            observation.strategy_family,
            observation.strategy_tag,
            observation.direction,
            observation.threshold_segment,
        )
        group = groups.setdefault(
            key,
            _empty_observation_stats(
                timeframe_minutes=observation.timeframe_minutes,
                strategy_family=observation.strategy_family,
                strategy_tag=observation.strategy_tag,
                direction=observation.direction,
                threshold_segment=observation.threshold_segment,
            ),
        )
        _accumulate_observation_stats(group, observation)

    finalized_groups = [_finalize_observation_group(group) for group in groups.values()]
    finalized_groups.sort(
        key=lambda item: (
            _observation_action_rank(item["action"]),
            -item["settled"],
            -item["ev"],
            item["strategy_family"],
            item["strategy_tag"],
            item["direction"],
            item["threshold_segment"],
        )
    )
    action_counts: dict[str, int] = {}
    for group in finalized_groups:
        action_counts[group["action"]] = action_counts.get(group["action"], 0) + 1

    return {
        "total": _finalize_observation_stats(total),
        "groups": finalized_groups[:group_limit],
        "group_limit": group_limit,
        "action_counts": action_counts,
        "rules": {
            "promote_sample": OBSERVATION_PROMOTE_SAMPLE,
            "watch_sample": OBSERVATION_WATCH_SAMPLE,
            "promote_win_rate": 0.6,
            "promote_ev": 0.8,
            "block_win_rate": 0.5,
            "block_ev": -1.0,
        },
    }


def _summarize_signal_audit(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    def counts(field: str) -> list[dict[str, Any]]:
        grouped: dict[str, int] = {}
        for record in records:
            key = str(record.get(field) or "UNKNOWN")
            grouped[key] = grouped.get(key, 0) + int(record.get("occurrences", 1))
        return [
            {"key": key, "count": count}
            for key, count in sorted(grouped.items())
        ]

    contexts: dict[str, dict[str, int]] = {}
    for record in records:
        key = str(record.get("profile_context") or "UNKNOWN")
        group = contexts.setdefault(key, {"signals": 0, "blocked": 0, "opened": 0})
        occurrences = int(record.get("occurrences", 1))
        group["signals"] += occurrences
        decision = str(record.get("decision") or "")
        if decision == "OPENED":
            group["opened"] += occurrences
        elif decision.endswith("BLOCKED") or decision in {
            "HOLD_OPEN_ORDER",
            "HOLD_LONG_OPEN_ORDER",
            "HOLD_SHORT_OPEN_ORDER",
            "COOLDOWN",
            "DAILY_PROFILE_NOT_SELECTED",
            "SHORT_OBSERVE_ONLY",
        }:
            group["blocked"] += occurrences
    return {
        "sample_count": sum(int(record.get("occurrences", 1)) for record in records),
        "storage_rows": len(records),
        "by_decision": counts("decision"),
        "by_profile_dps_slot": [
            {"key": key, **values}
            for key, values in sorted(contexts.items())
        ],
        "by_result_sequence_status": counts("result_sequence_status"),
        "by_profile_degradation_status": counts("profile_degradation_status"),
        "by_wave_batch_status": counts("wave_batch_status"),
        "by_rolling_edge_status": counts("rolling_edge_status"),
    }


def _normalize_page_size(page_size: int) -> int:
    try:
        value = int(page_size)
    except (TypeError, ValueError):
        return 20
    return value if value in ORDER_PAGE_SIZES else 20


def _clean_filter(value: str | None) -> str:
    return str(value or "").strip().upper()


def _order_matches(order: SimulatedOrder, filters: dict[str, str]) -> bool:
    if filters["direction"] and order.direction.upper() != filters["direction"]:
        return False
    if filters["level"] and order.level.upper() != filters["level"]:
        return False
    if filters["segment"] and order.threshold_segment.upper() != filters["segment"]:
        return False
    if filters["result"]:
        if filters["result"] == "OPEN":
            return order.status.upper() == "OPEN"
        return str(order.result or "").upper() == filters["result"]
    return True


def _order_filter_options(orders: list[SimulatedOrder]) -> dict[str, list[str]]:
    result_order = {"OPEN": 0, "WIN": 1, "LOSS": 2}
    level_order = {"S": 0, "A": 1, "B": 2}
    results = {
        "OPEN" if order.status.upper() == "OPEN" else str(order.result or "").upper()
        for order in orders
    }
    results.discard("")
    return {
        "direction": sorted({order.direction.upper() for order in orders if order.direction}),
        "level": sorted({order.level.upper() for order in orders if order.level}, key=lambda item: level_order.get(item, 99)),
        "segment": sorted({order.threshold_segment.upper() for order in orders if order.threshold_segment}),
        "result": sorted(results, key=lambda item: result_order.get(item, 99)),
        "page_size": list(ORDER_PAGE_SIZES),
    }


def _observation_matches(observation: ObservationSignal, filters: dict[str, str]) -> bool:
    if filters["direction"] and observation.direction.upper() != filters["direction"]:
        return False
    if filters["family"] and observation.strategy_family.upper() != filters["family"]:
        return False
    if filters["tag"] and observation.strategy_tag.upper() != filters["tag"]:
        return False
    if filters["segment"] and observation.threshold_segment.upper() != filters["segment"]:
        return False
    if filters["result"]:
        if filters["result"] == "OPEN":
            return observation.status.upper() == "OPEN"
        return str(observation.result or "").upper() == filters["result"]
    return True


def _observation_filter_options(observations: list[ObservationSignal]) -> dict[str, list[str]]:
    result_order = {"OPEN": 0, "WIN": 1, "LOSS": 2}
    results = {
        "OPEN" if observation.status.upper() == "OPEN" else str(observation.result or "").upper()
        for observation in observations
    }
    results.discard("")
    return {
        "direction": sorted({observation.direction.upper() for observation in observations if observation.direction}),
        "family": sorted(
            {observation.strategy_family.upper() for observation in observations if observation.strategy_family}
        ),
        "tag": sorted({observation.strategy_tag.upper() for observation in observations if observation.strategy_tag}),
        "segment": sorted(
            {observation.threshold_segment.upper() for observation in observations if observation.threshold_segment}
        ),
        "result": sorted(results, key=lambda item: result_order.get(item, 99)),
        "page_size": list(ORDER_PAGE_SIZES),
    }


def _empty_observation_stats(
    *,
    timeframe_minutes: int | None = None,
    strategy_family: str = "",
    strategy_tag: str = "",
    direction: str = "",
    threshold_segment: str = "",
) -> dict[str, Any]:
    return {
        "timeframe_minutes": timeframe_minutes,
        "strategy_family": strategy_family,
        "strategy_tag": strategy_tag,
        "direction": direction,
        "threshold_segment": threshold_segment,
        "signals": 0,
        "open": 0,
        "settled": 0,
        "wins": 0,
        "losses": 0,
        "pnl": 0.0,
        "first_opened_at": None,
        "last_opened_at": None,
    }


def _accumulate_observation_stats(stats: dict[str, Any], observation: ObservationSignal) -> None:
    stats["signals"] += 1
    stats["first_opened_at"] = _min_optional(stats["first_opened_at"], observation.opened_at)
    stats["last_opened_at"] = _max_optional(stats["last_opened_at"], observation.opened_at)
    result = str(observation.result or "").upper()
    if observation.status.upper() == "OPEN" or result not in {"WIN", "LOSS"}:
        stats["open"] += 1
        return
    stats["settled"] += 1
    if result == "WIN":
        stats["wins"] += 1
    else:
        stats["losses"] += 1
    stats["pnl"] += _observation_result_pnl(observation)


def _finalize_observation_stats(stats: dict[str, Any]) -> dict[str, Any]:
    settled = stats["settled"]
    wins = stats["wins"]
    win_rate = wins / settled if settled else 0.0
    ev = stats["pnl"] / settled if settled else 0.0
    return {
        **stats,
        "win_rate": win_rate,
        "ev": ev,
        "pnl": round(stats["pnl"], 4),
    }


def _finalize_observation_group(stats: dict[str, Any]) -> dict[str, Any]:
    finalized = _finalize_observation_stats(stats)
    finalized["action"] = _observation_action(finalized)
    finalized["confidence"] = _observation_confidence(finalized["settled"])
    return finalized


def _observation_result_pnl(observation: ObservationSignal) -> float:
    if observation.pnl:
        return observation.pnl
    return 8.0 if observation.result == "WIN" else -10.0


def _observation_action(stats: dict[str, Any]) -> str:
    settled = stats["settled"]
    win_rate = stats["win_rate"]
    ev = stats["ev"]
    if settled < OBSERVATION_WATCH_SAMPLE:
        return "COLLECTING"
    if settled >= OBSERVATION_PROMOTE_SAMPLE and win_rate >= 0.6 and ev >= 0.8:
        return "PROMOTE_WATCH"
    if settled >= OBSERVATION_PROMOTE_SAMPLE and (win_rate < 0.5 or ev <= -1.0):
        return "BLOCK_WATCH"
    if win_rate >= 0.56 and ev > 0:
        return "WATCH_UPSIDE"
    if win_rate < 0.5 or ev < 0:
        return "WATCH_RISK"
    return "WATCH"


def _observation_confidence(settled: int) -> str:
    if settled >= 100:
        return "HIGH"
    if settled >= OBSERVATION_PROMOTE_SAMPLE:
        return "MEDIUM"
    return "LOW"


def _observation_action_rank(action: str) -> int:
    ranks = {
        "PROMOTE_WATCH": 0,
        "WATCH_UPSIDE": 1,
        "WATCH": 2,
        "WATCH_RISK": 3,
        "BLOCK_WATCH": 4,
        "COLLECTING": 5,
    }
    return ranks.get(action, 99)


def _min_optional(current: int | None, value: int) -> int:
    return value if current is None else min(current, value)


def _max_optional(current: int | None, value: int) -> int:
    return value if current is None else max(current, value)


class SQLiteMonitorStore:
    def __init__(
        self,
        path: str | Path,
        *,
        profile_summary_schema_version: int = PROFILE_SUMMARY_SCHEMA_VERSION,
        profile_algorithm_fingerprint: str | None = None,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.profile_summary_schema_version = max(
            1,
            int(profile_summary_schema_version),
        )
        self.profile_algorithm_fingerprint = str(
            profile_algorithm_fingerprint
            or order_profile_algorithm_fingerprint()
        ).strip()
        if not self.profile_algorithm_fingerprint:
            raise ValueError("profile_algorithm_fingerprint must not be empty")
        self._profile_summary_owner_id = uuid.uuid4().hex
        self._profile_summary_stop = threading.Event()
        self._profile_summary_lock = threading.RLock()
        self._profile_summary_dirty: set[str] = set()
        self._profile_summary_keys: set[tuple[str, int, str, int, int, int]] = set()
        self._profile_summary_futures: dict[
            tuple[str, int, str, int, int, int], Future
        ] = {}
        self._profile_summary_condition = threading.Condition(
            self._profile_summary_lock
        )
        self.profile_worker_thread_prefix = f"profile-summary-{id(self):x}"
        self._enable_concurrent_access()
        self._profile_summary_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=self.profile_worker_thread_prefix,
        )
        self._profile_summary_closed = False
        self._profile_summary_close_state = "OPEN"
        self._profile_summary_shutdown_in_progress = False
        self._init_schema()

    def _enable_concurrent_access(self) -> None:
        connection = sqlite3.connect(self.path, timeout=5.0)
        try:
            connection.execute("pragma busy_timeout = 5000")
            journal_mode = connection.execute(
                "pragma journal_mode = wal"
            ).fetchone()
            if not journal_mode or str(journal_mode[0]).lower() != "wal":
                raise sqlite3.OperationalError("failed to enable SQLite WAL mode")
            connection.execute("pragma synchronous = normal")
        finally:
            connection.close()

    def _maintain_profile_summary_after_commit(
        self,
        symbol: str,
        *,
        sample: Mapping[str, Any] | None = None,
        settlement: SimulatedOrder | None = None,
    ) -> None:
        try:
            self._refresh_profile_summary_cache(
                symbol,
                sample=sample,
                settlement=settlement,
            )
        except Exception:  # noqa: BLE001 - 事务已提交，缓存维护只能标记为陈旧。
            with self._profile_summary_lock:
                self._profile_summary_dirty.add(symbol.upper())

    def close(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._profile_summary_condition:
            if self._profile_summary_close_state == "CLOSED":
                return
            if self._profile_summary_close_state == "OPEN":
                self._profile_summary_close_state = "CLOSING"
                self._profile_summary_closed = True
                self._profile_summary_stop.set()
            futures = tuple(self._profile_summary_futures.values())

        # Future.cancel() runs callbacks synchronously, so it must not run while
        # iterating the live mapping or while holding the callback's lock.
        for future in futures:
            future.cancel()

        with self._profile_summary_condition:
            while self._profile_summary_futures:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._profile_summary_condition.wait(timeout=remaining)
            while self._profile_summary_shutdown_in_progress:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._profile_summary_condition.wait(timeout=remaining)
                if self._profile_summary_close_state == "CLOSED":
                    return
            if self._profile_summary_close_state == "CLOSED":
                return
            if deadline - time.monotonic() <= 0:
                return
            self._profile_summary_shutdown_in_progress = True

        try:
            self._profile_summary_executor.shutdown(
                wait=True,
                cancel_futures=True,
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._release_profile_summary_owner_leases(timeout=remaining)
        except Exception:
            with self._profile_summary_condition:
                self._profile_summary_shutdown_in_progress = False
                self._profile_summary_condition.notify_all()
            raise
        else:
            with self._profile_summary_condition:
                self._profile_summary_close_state = "CLOSED"
                self._profile_summary_shutdown_in_progress = False
                self._profile_summary_condition.notify_all()
        finally:
            with self._profile_summary_condition:
                if self._profile_summary_close_state != "CLOSED":
                    self._profile_summary_shutdown_in_progress = False
                    self._profile_summary_condition.notify_all()

    def _release_profile_summary_owner_leases(self, *, timeout: float) -> None:
        with self._connect(timeout=max(0.001, float(timeout))) as connection:
            connection.execute(
                "delete from profile_summary_leases where owner_id = ?",
                (self._profile_summary_owner_id,),
            )

    def wait_for_profile_summary_rebuilds(self, timeout: float | None = None) -> None:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._profile_summary_condition:
            while self._profile_summary_futures:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("profile summary rebuild did not drain")
                self._profile_summary_condition.wait(timeout=remaining)

    def storage_capacity(self) -> StorageCapacity:
        with self._connect() as connection:
            return capacity_from_connection(connection)

    @staticmethod
    def _after_bundle_step(_step: str) -> None:
        return None

    def _insert_runtime_config(
        self,
        connection: sqlite3.Connection,
        snapshot: RuntimeConfigSnapshot,
    ) -> None:
        runtime_config_hash, canonical_payload, payload_bytes = (
            _runtime_snapshot_values(snapshot)
        )
        # Build ID is first-seen config provenance; each decision stores its actual build.
        connection.execute(
            """
            insert or ignore into runtime_config_snapshots(
                runtime_config_hash, context_version, strategy_build_id,
                canonical_payload, payload_bytes, created_at_ms
            )
            values (?, ?, ?, ?, ?, strftime('%s','now') * 1000)
            """,
            (
                runtime_config_hash,
                CONTEXT_VERSION,
                snapshot.strategy_build_id,
                canonical_payload,
                payload_bytes,
            ),
        )
        row = connection.execute(
            """
            select runtime_config_hash, context_version, strategy_build_id,
                   canonical_payload, payload_bytes, created_at_ms
            from runtime_config_snapshots
            where runtime_config_hash = ?
            """,
            (runtime_config_hash,),
        ).fetchone()
        if row is None:
            raise ValueError("runtime configuration snapshot was not persisted")
        stored = _runtime_row_to_dict(row)
        if (
            stored["context_version"] != CONTEXT_VERSION
            or stored["canonical_payload"] != canonical_payload
            or stored["payload_bytes"] != payload_bytes
        ):
            raise ValueError("runtime configuration hash collides with stored data")

    def _insert_decision_context(
        self,
        connection: sqlite3.Connection,
        context: DecisionContext,
    ) -> None:
        values = _decision_context_values(context)
        runtime_row = connection.execute(
            """
            select runtime_config_hash, context_version, strategy_build_id,
                   canonical_payload, payload_bytes, created_at_ms
            from runtime_config_snapshots
            where runtime_config_hash = ?
            """,
            (values["runtime_config_hash"],),
        ).fetchone()
        if runtime_row is None:
            raise ValueError(
                "decision context requires a persisted V2 runtime configuration"
            )
        _runtime_row_to_dict(runtime_row)
        connection.execute(
            """
            insert or ignore into decision_contexts(
                symbol, decision_id, context_version, runtime_config_hash,
                strategy_build_id, created_at_ms, closed_kline_at_ms,
                direction, profile_key, candidate_origin, input_payload,
                outcome_payload
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(values[column] for column in _DECISION_CONTEXT_COLUMNS),
        )
        row = connection.execute(
            """
            select symbol, decision_id, context_version, runtime_config_hash,
                   strategy_build_id, created_at_ms, closed_kline_at_ms,
                   direction, profile_key, candidate_origin, input_payload,
                   outcome_payload
            from decision_contexts
            where symbol = ? and decision_id = ?
            """,
            (values["symbol"], values["decision_id"]),
        ).fetchone()
        if row is None or any(
            row[column] != values[column]
            for column in _DECISION_CONTEXT_COLUMNS
        ):
            raise ValueError("decision context conflicts with frozen stored data")

    @staticmethod
    def _validate_decision_metadata(
        item: object,
        context: DecisionContext,
        name: str,
    ) -> None:
        expected = {
            "decision_id": context.decision_id,
            "context_version": context.context_version,
            "runtime_config_hash": context.runtime_config_hash,
            "strategy_build_id": context.strategy_build_id,
            "candidate_origin": context.candidate_origin,
        }
        for field_name, expected_value in expected.items():
            if getattr(item, field_name, None) != expected_value:
                raise ValueError(
                    f"{name} {field_name} must match decision context"
                )

    @staticmethod
    def _validate_bundle_references(
        config: RuntimeConfigSnapshot,
        context: DecisionContext,
        audit: DecisionAudit,
        *,
        order: SimulatedOrder | None = None,
        entry_snapshot: Mapping[str, Any] | None = None,
        observation: ObservationSignal | None = None,
    ) -> None:
        if not isinstance(config, RuntimeConfigSnapshot):
            raise TypeError("config must be a RuntimeConfigSnapshot")
        if not isinstance(context, DecisionContext):
            raise TypeError("context must be a DecisionContext")
        if not isinstance(audit, DecisionAudit):
            raise TypeError("audit must be a DecisionAudit")
        if config.hash != context.runtime_config_hash:
            raise ValueError("config hash must match decision context")
        if config.strategy_build_id != context.strategy_build_id:
            raise ValueError("config build must match decision context")
        if context.symbol != context.symbol.upper():
            raise ValueError("decision context symbol must be uppercase")
        canonical_structure = _canonical_entry_structure(context.inputs)
        if not isinstance(audit.signal, Signal):
            raise TypeError("audit signal must be a Signal")
        SQLiteMonitorStore._validate_decision_metadata(
            audit.signal, context, "audit signal"
        )
        _require_matching_entry_structure(
            audit.signal.entry_structure_shadow,
            canonical_structure,
            "audit signal",
        )
        if str(audit.decision or "").upper() != context.final_decision.upper():
            raise ValueError("audit decision must match final decision")
        if type(audit.created_at_ms) is not int or audit.created_at_ms < 0:
            raise ValueError("audit created_at_ms must be a non-negative integer")
        if order is not None:
            if not isinstance(order, SimulatedOrder):
                raise TypeError("order must be a SimulatedOrder")
            SQLiteMonitorStore._validate_decision_metadata(order, context, "order")
            _require_matching_entry_structure(
                order.entry_structure_shadow,
                canonical_structure,
                "order",
            )
            if order.status != "OPEN":
                raise ValueError("open decision bundle requires an OPEN order")
            if order.direction != audit.signal.direction:
                raise ValueError("order direction must match audit signal")
        if observation is not None:
            if not isinstance(observation, ObservationSignal):
                raise TypeError("observation must be an ObservationSignal")
            SQLiteMonitorStore._validate_decision_metadata(
                observation, context, "observation"
            )
            _require_matching_entry_structure(
                observation.entry_structure_shadow,
                canonical_structure,
                "observation",
            )
            if observation.direction != audit.signal.direction:
                raise ValueError("observation direction must match audit signal")
        if entry_snapshot is not None:
            if not isinstance(entry_snapshot, Mapping):
                raise TypeError("entry_snapshot must be a mapping")
            snapshot_signal = entry_snapshot.get("signal")
            if not isinstance(snapshot_signal, Mapping):
                raise ValueError("entry_snapshot must contain signal metadata")
            if (
                canonical_structure
                and "entry_structure_shadow" not in snapshot_signal
            ):
                raise ValueError(
                    "entry_snapshot signal entry structure is required"
                )
            if "entry_structure_shadow" in snapshot_signal:
                _require_matching_entry_structure(
                    snapshot_signal["entry_structure_shadow"],
                    canonical_structure,
                    "entry_snapshot signal",
                )
            if "entry_structure_shadow" in entry_snapshot:
                _require_matching_entry_structure(
                    entry_snapshot["entry_structure_shadow"],
                    canonical_structure,
                    "entry_snapshot",
                )
            for field_name in (
                "decision_id",
                "context_version",
                "runtime_config_hash",
                "strategy_build_id",
                "candidate_origin",
            ):
                if snapshot_signal.get(field_name) != getattr(context, field_name):
                    raise ValueError(
                        f"entry_snapshot signal {field_name} must match decision context"
                    )

    def save_runtime_config_snapshot(
        self,
        snapshot: RuntimeConfigSnapshot,
    ) -> None:
        with self._connect() as connection:
            connection.execute("begin immediate")
            self._insert_runtime_config(connection, snapshot)

    def load_runtime_config_snapshot(
        self,
        runtime_config_hash: str,
    ) -> dict[str, Any] | None:
        normalized_hash = _require_runtime_config_hash(runtime_config_hash)
        with self._connect() as connection:
            row = connection.execute(
                """
                select runtime_config_hash, context_version, strategy_build_id,
                       canonical_payload, payload_bytes, created_at_ms
                from runtime_config_snapshots
                where runtime_config_hash = ?
                """,
                (normalized_hash,),
            ).fetchone()
        if row is None:
            return None
        return _runtime_row_to_dict(row)

    def save_decision_context(self, context: DecisionContext) -> None:
        with self._connect() as connection:
            connection.execute("begin immediate")
            self._insert_decision_context(connection, context)

    def load_decision_context(
        self,
        symbol: str,
        decision_id: str,
    ) -> dict[str, Any] | None:
        normalized_symbol = _require_string(symbol, "symbol", non_empty=True)
        normalized_decision_id = _require_string(
            decision_id,
            "decision_id",
            non_empty=True,
        )
        with self._connect() as connection:
            row = connection.execute(
                """
                select symbol, decision_id, context_version, runtime_config_hash,
                       strategy_build_id, created_at_ms, closed_kline_at_ms,
                       direction, profile_key, candidate_origin, input_payload,
                       outcome_payload
                from decision_contexts
                where symbol = ? and decision_id = ?
                """,
                (normalized_symbol, normalized_decision_id),
            ).fetchone()
            if row is None:
                return None
            runtime_row = connection.execute(
                """
                select runtime_config_hash, context_version, strategy_build_id,
                       canonical_payload, payload_bytes, created_at_ms
                from runtime_config_snapshots
                where runtime_config_hash = ?
                """,
                (row["runtime_config_hash"],),
            ).fetchone()
            if runtime_row is None:
                raise ValueError("stored decision context has no V2 runtime configuration")
            _runtime_row_to_dict(runtime_row)

        try:
            inputs = _parse_canonical_json(row["input_payload"], "input_payload")
            outcome = _parse_canonical_json(
                row["outcome_payload"],
                "outcome_payload",
            )
        except ValueError as error:
            raise ValueError("stored decision context JSON is malformed") from error
        if not isinstance(inputs, dict):
            raise ValueError("stored decision context inputs must be an object")
        signal_inputs = inputs.get("signal", {})
        has_structure_view = "entry_structure" in inputs or (
            isinstance(signal_inputs, Mapping)
            and "entry_structure_shadow" in signal_inputs
        )
        try:
            canonical_structure = _canonical_entry_structure(inputs)
        except ValueError as error:
            raise ValueError("stored decision context structure is malformed") from error
        legacy_outcome_keys = _DECISION_OUTCOME_KEYS - {"selected_order_terms"}
        if not isinstance(outcome, dict) or frozenset(outcome) not in {
            frozenset(_DECISION_OUTCOME_KEYS),
            frozenset(legacy_outcome_keys),
        }:
            raise ValueError("stored decision context outcome has an invalid shape")
        outcome.setdefault("selected_order_terms", {})
        if not isinstance(outcome["decision_trace"], list):
            raise ValueError("stored decision trace must be a list")

        try:
            restored_context = DecisionContext(
                decision_id=row["decision_id"],
                context_version=row["context_version"],
                runtime_config_hash=row["runtime_config_hash"],
                strategy_build_id=row["strategy_build_id"],
                symbol=row["symbol"],
                closed_kline_at_ms=row["closed_kline_at_ms"],
                candidate_origin=row["candidate_origin"],
                inputs=inputs,
                decision_trace=outcome["decision_trace"],
                first_decisive_block=outcome["first_decisive_block"],
                final_decision=outcome["final_decision"],
                final_reason=outcome["final_reason"],
                open_allowed=outcome["open_allowed"],
                observation_allowed=outcome["observation_allowed"],
                selected_order_terms=outcome["selected_order_terms"],
            )
            expected = _decision_context_values(restored_context)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("stored decision context is malformed") from error
        if any(row[column] != expected[column] for column in _DECISION_CONTEXT_COLUMNS):
            raise ValueError("stored decision context metadata does not match its payload")
        restored = restored_context.to_dict()
        if has_structure_view:
            restored["inputs"]["entry_structure"] = deepcopy(canonical_structure)
            restored["inputs"]["signal"]["entry_structure_shadow"] = deepcopy(
                canonical_structure
            )
        return restored

    def load_decision_context_for_candidate(
        self,
        symbol: str,
        *,
        closed_kline_at_ms: int,
        candidate_origin: str,
        profile_key: str,
        runtime_config_hash: str,
        strategy_build_id: str,
        candidate_identity: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        normalized_symbol = _require_string(symbol, "symbol", non_empty=True).upper()
        if not isinstance(candidate_identity, Mapping):
            raise TypeError("candidate_identity must be a mapping")
        expected_identity = dict(candidate_identity)
        required_identity_fields = {
            "candidate_origin",
            "candidate_ordinal",
            "direction",
            "profile_key",
            "strategy_family",
            "strategy_tag",
            "order_slot",
            "order_slot_scope",
            "timeframe_minutes",
            "threshold_segment",
        }
        if set(expected_identity) != required_identity_fields:
            raise ValueError("candidate_identity must contain the complete identity")
        if expected_identity["candidate_origin"] != str(candidate_origin):
            raise ValueError("candidate identity origin must match lookup origin")
        if expected_identity["profile_key"] != str(profile_key):
            raise ValueError("candidate identity profile must match lookup profile")
        with self._connect() as connection:
            rows = connection.execute(
                """
                select decision_id
                from decision_contexts
                where symbol = ? and closed_kline_at_ms = ?
                  and candidate_origin = ? and profile_key = ?
                  and direction = ?
                  and runtime_config_hash = ? and strategy_build_id = ?
                """,
                (
                    normalized_symbol,
                    int(closed_kline_at_ms),
                    str(candidate_origin),
                    str(profile_key),
                    str(expected_identity["direction"]),
                    str(runtime_config_hash),
                    str(strategy_build_id),
                ),
            ).fetchall()
        matches = []
        for row in rows:
            context = self.load_decision_context(
                normalized_symbol,
                row["decision_id"],
            )
            stored_identity = (
                context["inputs"].get("identity")
                if context is not None
                else None
            )
            comparable_identity = (
                stored_identity.get("candidate_identity", stored_identity)
                if isinstance(stored_identity, Mapping)
                else stored_identity
            )
            if context is not None and comparable_identity == expected_identity:
                matches.append(context)
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError("candidate identity maps to multiple decision contexts")
        return matches[0]

    def save_order(self, order: SimulatedOrder, symbol: str) -> None:
        with self._connect() as connection:
            self._upsert_order(connection, order, symbol)

    def save_wave_runtime(
        self,
        symbol: str,
        snapshot: WaveSnapshot,
        evaluated_at: int,
    ) -> None:
        payload = {
            "state": snapshot.state,
            "raw_state": snapshot.raw_state,
            "window": snapshot.window,
            "efficiency": snapshot.efficiency,
            "direction_ratio": snapshot.direction_ratio,
            "atr_strength": snapshot.atr_strength,
            "range_position": snapshot.range_position,
            "confirmations": snapshot.confirmations,
            "confirmed_at": snapshot.confirmed_at,
            "allowed_directions": list(snapshot.allowed_directions),
        }
        with self._connect() as connection:
            connection.execute(
                """
                insert into wave_runtime(symbol, version, evaluated_at, payload)
                values (?, ?, ?, ?)
                on conflict(symbol) do update set
                    version=excluded.version,
                    evaluated_at=excluded.evaluated_at,
                    payload=excluded.payload,
                    updated_at_ms=strftime('%s','now') * 1000
                """,
                (
                    symbol.upper(),
                    WAVE_RUNTIME_VERSION,
                    int(evaluated_at),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

    def load_wave_runtime(self, symbol: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                select version, evaluated_at, payload
                from wave_runtime
                where symbol = ?
                """,
                (symbol.upper(),),
            ).fetchone()
        if row is None or row["version"] != WAVE_RUNTIME_VERSION:
            return None
        try:
            payload = json.loads(row["payload"])
            snapshot = WaveSnapshot(
                state=str(payload["state"]),
                raw_state=str(payload["raw_state"]),
                window=int(payload["window"]),
                efficiency=float(payload["efficiency"]),
                direction_ratio=float(payload["direction_ratio"]),
                atr_strength=float(payload["atr_strength"]),
                range_position=float(payload["range_position"]),
                confirmations=int(payload["confirmations"]),
                confirmed_at=int(payload["confirmed_at"]),
                allowed_directions=tuple(
                    str(item) for item in payload["allowed_directions"]
                ),
            )
            evaluated_at = int(row["evaluated_at"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if evaluated_at <= 0:
            return None
        return {"evaluated_at": evaluated_at, "snapshot": snapshot}

    def _upsert_order(
        self,
        connection: sqlite3.Connection,
        order: SimulatedOrder,
        symbol: str,
    ) -> None:
        connection.execute(
            """
            insert into orders(
                symbol, order_id, status, result, opened_at, settled_at,
                exit_price, pnl, payload, decision_id, runtime_config_hash
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(symbol, order_id) do update set
                status=excluded.status,
                result=excluded.result,
                opened_at=excluded.opened_at,
                settled_at=excluded.settled_at,
                exit_price=excluded.exit_price,
                pnl=excluded.pnl,
                payload=excluded.payload,
                decision_id=excluded.decision_id,
                runtime_config_hash=excluded.runtime_config_hash,
                updated_at_ms=strftime('%s','now') * 1000
            """,
            (
                symbol.upper(),
                order.id,
                order.status,
                order.result,
                order.opened_at,
                order.settled_at,
                order.exit_price,
                order.pnl,
                json.dumps(decision_linked_storage_payload(order), ensure_ascii=False),
                order.decision_id or None,
                order.runtime_config_hash or None,
            ),
        )

    def _insert_open_order(
        self,
        connection: sqlite3.Connection,
        order: SimulatedOrder,
        symbol: str,
    ) -> None:
        normalized_symbol = symbol.upper()
        incoming = decision_linked_storage_payload(order)
        payload = json.dumps(incoming, ensure_ascii=False)
        bound_order = connection.execute(
            f"""
            select orders.order_id, orders.status, orders.result,
                   orders.opened_at, orders.settled_at, orders.exit_price,
                   orders.pnl, orders.payload, orders.decision_id,
                   orders.runtime_config_hash,
                   {_ORDER_LIFECYCLE_COLUMNS},
                   {_LINKED_CONTEXT_COLUMNS}
            from orders
            left join decision_contexts
              on decision_contexts.symbol = orders.symbol
             and decision_contexts.decision_id = orders.decision_id
            where orders.symbol = ? and orders.decision_id = ?
            """,
            (normalized_symbol, order.decision_id),
        ).fetchone()
        if bound_order is not None and bound_order["order_id"] != order.id:
            raise ValueError("decision is already bound to a different order")
        if bound_order is not None:
            try:
                persisted = json.loads(bound_order["payload"])
            except (TypeError, json.JSONDecodeError) as error:
                raise ValueError("stored order payload is malformed") from error
            persisted = _canonical_storage_projection(
                persisted,
                bound_order,
                apply_lifecycle_authority=True,
            )
            incoming = _canonical_storage_projection(
                incoming,
                bound_order,
                apply_lifecycle_authority=False,
            )
            mutable_fields = {"status", "result", "settled_at", "exit_price", "pnl"}
            persisted_frozen = {
                key: value for key, value in persisted.items() if key not in mutable_fields
            }
            incoming_frozen = {
                key: value for key, value in incoming.items() if key not in mutable_fields
            }
            if (
                persisted_frozen != incoming_frozen
                or bound_order["opened_at"] != order.opened_at
                or bound_order["decision_id"] != order.decision_id
                or bound_order["runtime_config_hash"] != order.runtime_config_hash
            ):
                raise ValueError("order id collides with different frozen decision data")
            if bound_order["status"] == "SETTLED" and order.status == "OPEN":
                return
            if (
                bound_order["status"] == order.status
                and bound_order["result"] == order.result
                and bound_order["settled_at"] == order.settled_at
                and bound_order["exit_price"] == order.exit_price
                and float(bound_order["pnl"]) == float(order.pnl)
                and persisted == incoming
            ):
                return
            raise ValueError("order id collides with different frozen decision data")
        connection.execute(
            """
            insert or ignore into orders(
                symbol, order_id, status, result, opened_at, settled_at,
                exit_price, pnl, payload, decision_id, runtime_config_hash
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_symbol,
                order.id,
                order.status,
                order.result,
                order.opened_at,
                order.settled_at,
                order.exit_price,
                order.pnl,
                payload,
                order.decision_id,
                order.runtime_config_hash,
            ),
        )
        row = connection.execute(
            """
            select status, result, opened_at, settled_at, exit_price, pnl,
                   payload, decision_id, runtime_config_hash
            from orders where symbol = ? and order_id = ?
            """,
            (normalized_symbol, order.id),
        ).fetchone()
        expected = (
            order.status,
            order.result,
            order.opened_at,
            order.settled_at,
            order.exit_price,
            order.pnl,
            payload,
            order.decision_id,
            order.runtime_config_hash,
        )
        if row is None or tuple(row) != expected:
            raise ValueError("order id collides with different frozen decision data")

    def _upsert_progression_credit(
        self,
        connection: sqlite3.Connection,
        symbol: str,
        credit: StakeProgressionCredit,
    ) -> None:
        connection.execute(
            """
            insert into stake_progression_credits(
                symbol, version, credit_id, source_order_id, status, created_at,
                consumed_order_id, consumed_at, direction
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(symbol, version, source_order_id) do update set
                credit_id=case
                    when stake_progression_credits.status = 'PENDING'
                    then excluded.credit_id
                    else stake_progression_credits.credit_id
                end,
                status=case
                    when stake_progression_credits.status = 'PENDING'
                    then excluded.status
                    else stake_progression_credits.status
                end,
                created_at=case
                    when stake_progression_credits.status = 'PENDING'
                    then excluded.created_at
                    else stake_progression_credits.created_at
                end,
                consumed_order_id=case
                    when stake_progression_credits.status = 'PENDING'
                    then excluded.consumed_order_id
                    else stake_progression_credits.consumed_order_id
                end,
                consumed_at=case
                    when stake_progression_credits.status = 'PENDING'
                    then excluded.consumed_at
                    else stake_progression_credits.consumed_at
                end,
                direction=case
                    when stake_progression_credits.status = 'PENDING'
                    then excluded.direction
                    else stake_progression_credits.direction
                end,
                updated_at_ms=strftime('%s','now') * 1000
            """,
            (
                symbol.upper(),
                credit.version,
                credit.credit_id,
                credit.source_order_id,
                credit.status,
                credit.created_at,
                credit.consumed_order_id,
                credit.consumed_at,
                credit.direction,
            ),
        )

    def save_stake_progression_credit(
        self,
        symbol: str,
        credit: StakeProgressionCredit,
    ) -> None:
        with self._connect() as connection:
            self._upsert_progression_credit(connection, symbol, credit)

    def cancel_stake_progression_credits(
        self,
        symbol: str,
        credits: Sequence[StakeProgressionCredit],
    ) -> None:
        normalized_symbol = symbol.upper()
        snapshots = list(credits)
        if not snapshots:
            return
        if any(credit.status != "CANCELLED" for credit in snapshots):
            raise ValueError("cancelled credit snapshots are required")
        keys = {(credit.version, credit.source_order_id) for credit in snapshots}
        if len(keys) != len(snapshots):
            raise ValueError("duplicate progression credit cancellation")

        with self._connect() as connection:
            for credit in snapshots:
                persisted = connection.execute(
                    """
                    select credit_id, status
                    from stake_progression_credits
                    where symbol = ? and version = ? and source_order_id = ?
                    """,
                    (normalized_symbol, credit.version, credit.source_order_id),
                ).fetchone()
                if persisted is None:
                    raise ValueError("progression credit to cancel does not exist")
                if persisted["credit_id"] != credit.credit_id:
                    raise ValueError("progression credit id conflicts with persisted state")
                if persisted["status"] == "CANCELLED":
                    continue
                if persisted["status"] != "PENDING":
                    raise ValueError("only pending progression credits can be cancelled")
                cursor = connection.execute(
                    """
                    update stake_progression_credits
                    set status = 'CANCELLED',
                        consumed_order_id = null,
                        consumed_at = null,
                        updated_at_ms = strftime('%s','now') * 1000
                    where symbol = ? and version = ? and source_order_id = ?
                      and credit_id = ? and status = 'PENDING'
                    """,
                    (
                        normalized_symbol,
                        credit.version,
                        credit.source_order_id,
                        credit.credit_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("progression credit cancellation lost a concurrent update")

    def load_stake_progression_credits(
        self,
        symbol: str,
        version: str = TWO_STAGE_VERSION,
    ) -> list[StakeProgressionCredit]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select source_order_id, created_at, credit_id, consumed_at,
                       consumed_order_id, version, status, direction
                from stake_progression_credits
                where symbol = ? and version = ?
                order by created_at, credit_id
                """,
                (symbol.upper(), version),
            ).fetchall()
        return [
            StakeProgressionCredit(
                source_order_id=row["source_order_id"],
                created_at=row["created_at"],
                credit_id=row["credit_id"],
                consumed_at=row["consumed_at"],
                consumed_order_id=row["consumed_order_id"],
                version=row["version"],
                status=row["status"],
                direction=row["direction"],
            )
            for row in rows
        ]

    def save_settled_order_with_credit(
        self,
        order: SimulatedOrder,
        symbol: str,
        credit: StakeProgressionCredit | None,
    ) -> None:
        self._validate_settlement_credit(order, credit)
        try:
            with self._connect() as connection:
                connection.execute("begin immediate")
                ensure_write_allowed(
                    capacity_from_connection(connection),
                    StorageWriteClass.CORE,
                )
                self._update_settled_order(connection, order, symbol)
                if credit is not None:
                    self._upsert_progression_credit(connection, symbol, credit)
                snapshot_changed = self._update_order_entry_snapshot_settlement(
                    connection,
                    order,
                    symbol,
                )
                if snapshot_changed:
                    revision = self._bump_profile_summary_revision(
                        connection,
                        symbol,
                    )
                    self._promote_profile_guard_settlement_branch(
                        connection,
                        symbol,
                        revision - 1,
                        revision,
                        order,
                    )
        except sqlite3.Error as error:
            raise_for_sqlite_write_error(error, StorageWriteClass.CORE)
        if snapshot_changed:
            self._maintain_profile_summary_after_commit(symbol)

    @staticmethod
    def _update_settled_order(
        connection: sqlite3.Connection,
        order: SimulatedOrder,
        symbol: str,
    ) -> None:
        if order.status != "SETTLED" or order.result not in {"WIN", "LOSS"}:
            raise ValueError("settlement requires a settled WIN or LOSS order")
        normalized_symbol = symbol.upper()
        row = connection.execute(
            f"""
            select orders.status, orders.result, orders.settled_at,
                   orders.payload, orders.decision_id, orders.runtime_config_hash,
                   {_ORDER_LIFECYCLE_COLUMNS},
                   {_LINKED_CONTEXT_COLUMNS}
            from orders
            left join decision_contexts
              on decision_contexts.symbol = orders.symbol
             and decision_contexts.decision_id = orders.decision_id
            where orders.symbol = ? and orders.order_id = ?
            """,
            (normalized_symbol, order.id),
        ).fetchone()
        if row is None:
            raise ValueError("settlement requires an existing open order")
        try:
            stored_payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("stored order payload is malformed") from error
        try:
            stored_payload = _canonical_storage_projection(
                stored_payload,
                row,
                apply_lifecycle_authority=True,
            )
        except ValueError as error:
            raise ValueError(
                f"settlement conflicts with frozen order identity: {error}"
            ) from error

        mutable_fields = {"status", "result", "settled_at", "exit_price", "pnl"}
        incoming_storage_payload = decision_linked_storage_payload(order)
        try:
            incoming_payload = _canonical_storage_projection(
                _decision_linked_validation_payload(
                    order,
                    incoming_storage_payload,
                ),
                row,
                apply_lifecycle_authority=False,
            )
        except ValueError as error:
            raise ValueError(
                f"settlement conflicts with frozen order identity: {error}"
            ) from error
        stored_frozen = {
            key: value
            for key, value in stored_payload.items()
            if key not in mutable_fields
        }
        requested_frozen = {
            key: value
            for key, value in incoming_payload.items()
            if key not in mutable_fields
        }
        if stored_frozen != requested_frozen:
            raise ValueError("settlement conflicts with frozen order identity")
        if (
            row["decision_id"] != (order.decision_id or None)
            or row["runtime_config_hash"] != (order.runtime_config_hash or None)
        ):
            raise ValueError("settlement conflicts with frozen order identity")

        expected_payload = json.dumps(incoming_storage_payload, ensure_ascii=False)
        if row["status"] == "SETTLED":
            if any(
                stored_payload.get(field) != incoming_payload.get(field)
                for field in mutable_fields
            ):
                raise ValueError("settlement conflicts with terminal order state")
            return
        if row["status"] != "OPEN":
            raise ValueError("only an open order can be settled")
        connection.execute(
            """
            update orders
            set status = ?, result = ?, settled_at = ?, exit_price = ?, pnl = ?,
                payload = ?,
                updated_at_ms = strftime('%s','now') * 1000
            where symbol = ? and order_id = ? and status = 'OPEN'
            """,
            (
                order.status,
                order.result,
                order.settled_at,
                order.exit_price,
                order.pnl,
                expected_payload,
                normalized_symbol,
                order.id,
            ),
        )

    def save_open_order_with_credit(
        self,
        order: SimulatedOrder,
        symbol: str,
        credit: StakeProgressionCredit | None,
    ) -> None:
        self._validate_open_credit(order, credit)
        with self._connect() as connection:
            self._upsert_order(connection, order, symbol)
            if credit is not None:
                self._upsert_progression_credit(connection, symbol, credit)
                persisted = connection.execute(
                    """
                    select credit_id, status, consumed_order_id, consumed_at
                    from stake_progression_credits
                    where symbol = ? and version = ? and source_order_id = ?
                    """,
                    (symbol.upper(), credit.version, credit.source_order_id),
                ).fetchone()
                if (
                    persisted is None
                    or persisted["status"] != "CONSUMED"
                    or persisted["consumed_order_id"] != credit.consumed_order_id
                    or persisted["consumed_at"] != credit.consumed_at
                    or persisted["credit_id"] != credit.credit_id
                ):
                    raise ValueError("credit consumption conflicts with persisted terminal state")

    @staticmethod
    def _validate_settlement_credit(
        order: SimulatedOrder,
        credit: StakeProgressionCredit | None,
    ) -> None:
        if credit is None:
            return
        if order.stake_progression_step != 1:
            raise ValueError("settlement credit requires a first-stage order")
        if credit.source_order_id != order.id:
            raise ValueError("settlement credit source_order_id must match order.id")
        if credit.status not in {"PENDING", "CANCELLED"}:
            raise ValueError("settlement credit must be PENDING or CANCELLED")
        if credit.version != order.stake_progression_version:
            raise ValueError("settlement credit version must match order version")
        if credit.direction and credit.direction != order.direction:
            raise ValueError("settlement credit direction must match order direction")

    @staticmethod
    def _validate_open_credit(
        order: SimulatedOrder,
        credit: StakeProgressionCredit | None,
    ) -> None:
        if credit is None:
            if order.stake_progression_step == 2:
                raise ValueError("second-stage order requires a consumed credit")
            return
        if order.stake_progression_step != 2:
            raise ValueError("consumed credit requires a second-stage order")
        if credit.status != "CONSUMED":
            raise ValueError("open-order credit must be CONSUMED")
        if credit.consumed_order_id != order.id:
            raise ValueError("credit consumed_order_id must match order.id")
        if credit.source_order_id != order.stake_progression_source_order_id:
            raise ValueError("credit source_order_id must match order source")
        if credit.version != order.stake_progression_version:
            raise ValueError("open-order credit version must match order version")
        if credit.direction and credit.direction != order.direction:
            raise ValueError("open-order credit direction must match order direction")

    def prepare_stake_progression(
        self,
        symbol: str,
        version: str,
        enabled: bool,
        activated_at: int,
    ) -> int:
        normalized_symbol = str(symbol).strip().upper()
        normalized_version = str(version).strip()
        if not normalized_symbol:
            raise ValueError("symbol must not be empty")
        if not normalized_version:
            raise ValueError("version must not be empty")
        try:
            requested_activation = int(activated_at)
        except (TypeError, ValueError) as error:
            raise ValueError("activated_at must be an integer") from error
        if requested_activation < 0:
            raise ValueError("activated_at must be >= 0")
        normalized_enabled = bool(enabled)

        with self._connect() as connection:
            runtime = connection.execute(
                """
                select version, activated_at, enabled
                from stake_progression_runtime
                where symbol = ?
                """,
                (normalized_symbol,),
            ).fetchone()
            should_cancel = False
            if runtime is None:
                actual_activation = requested_activation
                should_cancel = not normalized_enabled
            else:
                version_changed = runtime["version"] != normalized_version
                reenabled = not bool(runtime["enabled"]) and normalized_enabled
                disabling = bool(runtime["enabled"]) and not normalized_enabled
                should_cancel = version_changed or reenabled or disabling
                actual_activation = (
                    requested_activation
                    if version_changed or reenabled
                    else int(runtime["activated_at"])
                )

            if should_cancel:
                connection.execute(
                    """
                    update stake_progression_credits
                    set status = 'CANCELLED',
                        consumed_order_id = null,
                        consumed_at = null,
                        updated_at_ms = strftime('%s','now') * 1000
                    where symbol = ? and status = 'PENDING'
                    """,
                    (normalized_symbol,),
                )
            connection.execute(
                """
                insert into stake_progression_runtime(
                    symbol, version, activated_at, enabled
                )
                values (?, ?, ?, ?)
                on conflict(symbol) do update set
                    version=excluded.version,
                    activated_at=excluded.activated_at,
                    enabled=excluded.enabled,
                    updated_at_ms=strftime('%s','now') * 1000
                """,
                (
                    normalized_symbol,
                    normalized_version,
                    actual_activation,
                    int(normalized_enabled),
                ),
            )
        return actual_activation

    def load_orders(self, symbol: str) -> list[SimulatedOrder]:
        accepted = {field.name for field in fields(SimulatedOrder)}
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                select orders.payload,
                       {_ORDER_LIFECYCLE_COLUMNS},
                       {_LINKED_CONTEXT_COLUMNS}
                from orders
                left join decision_contexts
                  on decision_contexts.symbol = orders.symbol
                 and decision_contexts.decision_id = orders.decision_id
                where orders.symbol = ?
                order by orders.order_id
                """,
                (symbol.upper(),),
            ).fetchall()
        orders = []
        for row in rows:
            payload = _hydrate_decision_linked_payload(
                json.loads(row["payload"]),
                row,
            )
            clean_payload = {key: value for key, value in payload.items() if key in accepted}
            if "calculated_threshold" not in payload:
                clean_payload["calculated_threshold"] = float(
                    clean_payload.get("threshold", 0.0)
                )
            orders.append(SimulatedOrder(**clean_payload))
        return orders

    def save_observation(self, observation: ObservationSignal, symbol: str) -> None:
        self.save_observations((observation,), symbol)

    def save_observations(
        self,
        observations: Sequence[ObservationSignal],
        symbol: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("begin immediate")
            for observation in observations:
                existing = connection.execute(
                    "select payload from observation_signals "
                    "where symbol = ? and observation_key = ?",
                    (symbol.upper(), observation.observation_key),
                ).fetchone()
                if existing is None:
                    self._upsert_observation(connection, observation, symbol)
                else:
                    self._update_observation_settlement(
                        connection,
                        observation,
                        symbol,
                    )

    @staticmethod
    def _update_observation_settlement(
        connection: sqlite3.Connection,
        observation: ObservationSignal,
        symbol: str,
    ) -> None:
        normalized_symbol = symbol.upper()
        row = connection.execute(
            f"""
            select observation_signals.status, observation_signals.result,
                   observation_signals.settled_at, observation_signals.payload,
                   {_OBSERVATION_LIFECYCLE_COLUMNS},
                   {_LINKED_CONTEXT_COLUMNS}
            from observation_signals
            left join decision_contexts
              on decision_contexts.symbol = observation_signals.symbol
             and decision_contexts.decision_id = observation_signals.decision_id
            where observation_signals.symbol = ?
              and observation_signals.observation_key = ?
            """,
            (normalized_symbol, observation.observation_key),
        ).fetchone()
        if row is None:
            raise ValueError("observation settlement requires an existing observation")
        try:
            stored_payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("stored observation payload is malformed") from error
        try:
            stored_payload = _canonical_storage_projection(
                stored_payload,
                row,
                apply_lifecycle_authority=True,
            )
        except ValueError as error:
            raise ValueError(
                f"settlement conflicts with frozen observation data: {error}"
            ) from error

        mutable_fields = {"status", "result", "settled_at", "exit_price", "pnl"}
        incoming_storage_payload = decision_linked_storage_payload(observation)
        try:
            incoming_payload = _canonical_storage_projection(
                _decision_linked_validation_payload(
                    observation,
                    incoming_storage_payload,
                ),
                row,
                apply_lifecycle_authority=False,
            )
        except ValueError as error:
            raise ValueError(
                f"settlement conflicts with frozen observation data: {error}"
            ) from error
        stored_frozen = {
            key: value
            for key, value in stored_payload.items()
            if key not in mutable_fields
        }
        requested_frozen = {
            key: value
            for key, value in incoming_payload.items()
            if key not in mutable_fields
        }
        if stored_frozen != requested_frozen:
            raise ValueError("settlement conflicts with frozen observation data")

        expected_payload = json.dumps(incoming_storage_payload, ensure_ascii=False)
        if row["status"] == "SETTLED":
            if any(
                stored_payload.get(field) != incoming_payload.get(field)
                for field in mutable_fields
            ):
                raise ValueError("settlement conflicts with terminal observation state")
            return
        if row["status"] != "OPEN":
            raise ValueError("only an open observation can be settled")
        if observation.status == "OPEN":
            if stored_payload != incoming_payload:
                raise ValueError("open observation conflicts with frozen observation data")
            return
        if observation.status != "SETTLED" or observation.result not in {"WIN", "LOSS"}:
            raise ValueError("observation settlement requires SETTLED WIN or LOSS")
        connection.execute(
            """
            update observation_signals
            set status = ?, result = ?, settled_at = ?, exit_price = ?, pnl = ?,
                payload = ?,
                updated_at_ms = strftime('%s','now') * 1000
            where symbol = ? and observation_key = ? and status = 'OPEN'
            """,
            (
                observation.status,
                observation.result,
                observation.settled_at,
                observation.exit_price,
                observation.pnl,
                expected_payload,
                normalized_symbol,
                observation.observation_key,
            ),
        )

    @staticmethod
    def _observation_row_values(
        observation: ObservationSignal,
        symbol: str,
    ) -> dict[str, Any]:
        payload = decision_linked_storage_payload(observation)
        adaptive = (
            observation.adaptive_profile_state
            if isinstance(observation.adaptive_profile_state, Mapping)
            else {}
        )
        decision_inputs = observation.decision_inputs
        has_linked_inputs = bool(
            observation.decision_id
            and isinstance(decision_inputs, Mapping)
            and isinstance(decision_inputs.get("identity"), Mapping)
        )
        if has_linked_inputs:
            structure = _canonical_entry_structure(decision_inputs)
            _require_matching_entry_structure(
                observation.entry_structure_shadow,
                structure,
                "observation",
            )
        else:
            structure = (
                observation.entry_structure_shadow
                if isinstance(observation.entry_structure_shadow, Mapping)
                else {}
            )
        qualification_state = str(
            adaptive.get("qualification_state")
            or adaptive.get("qualification_status")
            or ""
        )
        adaptive_state = str(adaptive.get("state") or adaptive.get("status") or "")
        structure_state = str(
            structure.get("entry_structure_state") or structure.get("state") or ""
        )
        structure_bias = str(
            structure.get("entry_structure_bias") or structure.get("bias") or ""
        )
        active_level_source = str(structure.get("active_level_source") or "")
        return {
            "symbol": symbol.upper(),
            "observation_key": observation.observation_key,
            "status": observation.status,
            "result": observation.result,
            "direction": observation.direction,
            "strategy_family": observation.strategy_family,
            "strategy_tag": observation.strategy_tag,
            "timeframe_minutes": observation.timeframe_minutes,
            "threshold_segment": observation.threshold_segment,
            "opened_at": observation.opened_at,
            "expires_at": observation.expires_at,
            "settled_at": observation.settled_at,
            "exit_price": observation.exit_price,
            "pnl": observation.pnl,
            "payload": json.dumps(payload, ensure_ascii=False),
            "decision_id": observation.decision_id or None,
            "runtime_config_hash": observation.runtime_config_hash or None,
            "context_version": observation.context_version or None,
            "candidate_origin": observation.candidate_origin or None,
            "qualification_state": qualification_state or None,
            "adaptive_state": adaptive_state or None,
            "entry_structure_state": structure_state or None,
            "entry_structure_bias": structure_bias or None,
            "active_level_source": active_level_source or None,
        }

    @staticmethod
    def _upsert_observation(connection, observation: ObservationSignal, symbol: str) -> None:
        values = SQLiteMonitorStore._observation_row_values(observation, symbol)
        connection.execute(
            """
            insert into observation_signals(
                symbol, observation_key, status, result, direction,
                strategy_family, strategy_tag, timeframe_minutes,
                threshold_segment, opened_at, expires_at, settled_at, payload,
                exit_price, pnl,
                decision_id, runtime_config_hash, context_version, candidate_origin,
                qualification_state, adaptive_state, entry_structure_state,
                entry_structure_bias, active_level_source
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(symbol, observation_key) do update set
                status=excluded.status,
                result=excluded.result,
                direction=excluded.direction,
                strategy_family=excluded.strategy_family,
                strategy_tag=excluded.strategy_tag,
                timeframe_minutes=excluded.timeframe_minutes,
                threshold_segment=excluded.threshold_segment,
                opened_at=excluded.opened_at,
                expires_at=excluded.expires_at,
                settled_at=excluded.settled_at,
                exit_price=excluded.exit_price,
                pnl=excluded.pnl,
                payload=excluded.payload,
                decision_id=excluded.decision_id,
                runtime_config_hash=excluded.runtime_config_hash,
                context_version=excluded.context_version,
                candidate_origin=excluded.candidate_origin,
                qualification_state=excluded.qualification_state,
                adaptive_state=excluded.adaptive_state,
                entry_structure_state=excluded.entry_structure_state,
                entry_structure_bias=excluded.entry_structure_bias,
                active_level_source=excluded.active_level_source,
                updated_at_ms=strftime('%s','now') * 1000
            """,
            (
                values["symbol"],
                values["observation_key"],
                values["status"],
                values["result"],
                values["direction"],
                values["strategy_family"],
                values["strategy_tag"],
                values["timeframe_minutes"],
                values["threshold_segment"],
                values["opened_at"],
                values["expires_at"],
                values["settled_at"],
                values["payload"],
                values["exit_price"],
                values["pnl"],
                values["decision_id"],
                values["runtime_config_hash"],
                values["context_version"],
                values["candidate_origin"],
                values["qualification_state"],
                values["adaptive_state"],
                values["entry_structure_state"],
                values["entry_structure_bias"],
                values["active_level_source"],
            ),
        )

    def _insert_decision_observation(
        self,
        connection: sqlite3.Connection,
        observation: ObservationSignal,
        symbol: str,
    ) -> None:
        values = self._observation_row_values(observation, symbol)
        columns = tuple(values)
        selected_columns = ", ".join(
            f"observation_signals.{column} as {column}" for column in columns
        )
        if values["decision_id"]:
            existing = connection.execute(
                f"""
                select {selected_columns},
                       {_OBSERVATION_LIFECYCLE_COLUMNS},
                       {_LINKED_CONTEXT_COLUMNS}
                from observation_signals
                left join decision_contexts
                  on decision_contexts.symbol = observation_signals.symbol
                 and decision_contexts.decision_id = observation_signals.decision_id
                where observation_signals.symbol = ?
                  and observation_signals.decision_id = ?
                """,
                (values["symbol"], values["decision_id"]),
            ).fetchone()
            if (
                existing is not None
                and existing["observation_key"] != values["observation_key"]
            ):
                raise ValueError(
                    "decision observation is already bound to a different identity"
                )
        else:
            existing = connection.execute(
                f"select {selected_columns} from observation_signals "
                "where symbol = ? and observation_key = ?",
                (values["symbol"], values["observation_key"]),
            ).fetchone()
        if existing is not None:
            mutable_columns = {
                "status",
                "result",
                "settled_at",
                "exit_price",
                "pnl",
                "payload",
            }
            frozen_columns = tuple(
                column for column in columns if column not in mutable_columns
            )
            try:
                persisted_payload = json.loads(existing["payload"])
                requested_payload = json.loads(values["payload"])
            except (TypeError, json.JSONDecodeError) as error:
                raise ValueError("stored observation payload is malformed") from error
            persisted_payload = _canonical_storage_projection(
                persisted_payload,
                existing,
                apply_lifecycle_authority=True,
            )
            requested_payload = _canonical_storage_projection(
                requested_payload,
                existing,
                apply_lifecycle_authority=False,
            )
            mutable_payload_fields = {
                "status",
                "result",
                "settled_at",
                "exit_price",
                "pnl",
            }
            persisted_frozen_payload = {
                key: value
                for key, value in persisted_payload.items()
                if key not in mutable_payload_fields
            }
            requested_frozen_payload = {
                key: value
                for key, value in requested_payload.items()
                if key not in mutable_payload_fields
            }
            if (
                tuple(existing[column] for column in frozen_columns)
                != tuple(values[column] for column in frozen_columns)
                or persisted_frozen_payload != requested_frozen_payload
            ):
                raise ValueError(
                    "observation key collides with different frozen decision data"
                )
            if existing["status"] == "SETTLED" and values["status"] == "OPEN":
                return
            non_payload_columns = tuple(
                column for column in columns if column != "payload"
            )
            if (
                tuple(existing[column] for column in non_payload_columns)
                != tuple(values[column] for column in non_payload_columns)
                or persisted_payload != requested_payload
            ):
                raise ValueError(
                    "observation key collides with different frozen decision data"
                )
            return
        self._upsert_observation(connection, observation, values["symbol"])

    def load_observations(self, symbol: str, limit: int = 500) -> list[ObservationSignal]:
        accepted = {field.name for field in fields(ObservationSignal)}
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                select observation_signals.payload,
                       {_OBSERVATION_LIFECYCLE_COLUMNS},
                       {_LINKED_CONTEXT_COLUMNS}
                from observation_signals
                left join decision_contexts
                  on decision_contexts.symbol = observation_signals.symbol
                 and decision_contexts.decision_id = observation_signals.decision_id
                where observation_signals.symbol = ?
                order by observation_signals.opened_at desc
                limit ?
                """,
                (symbol.upper(), limit),
            ).fetchall()
        observations = []
        for row in rows:
            payload = _hydrate_decision_linked_payload(
                json.loads(row["payload"]),
                row,
            )
            clean_payload = {key: value for key, value in payload.items() if key in accepted}
            observations.append(ObservationSignal(**clean_payload))
        return observations

    @staticmethod
    def _compact_runtime_observation(
        observation: ObservationSignal,
    ) -> ObservationSignal:
        return ObservationSignal(
            observation_key=observation.observation_key,
            strategy_family=observation.strategy_family,
            strategy_tag=observation.strategy_tag,
            direction=observation.direction,
            timeframe_minutes=observation.timeframe_minutes,
            level=observation.level,
            reason="runtime history",
            entry_price=observation.entry_price,
            opened_at=observation.opened_at,
            expires_at=observation.expires_at,
            threshold_segment=observation.threshold_segment,
            status=observation.status,
            result=observation.result,
            exit_price=observation.exit_price,
            settled_at=observation.settled_at,
            pnl=observation.pnl,
            decision_id=observation.decision_id,
        )

    @staticmethod
    def _runtime_observations_from_rows(
        rows: Iterable[Mapping[str, Any]],
    ) -> list[ObservationSignal]:
        accepted = {field.name for field in fields(ObservationSignal)}
        restored = []
        for row in rows:
            payload = _hydrate_decision_linked_payload(
                json.loads(row["payload"]),
                row,
            )
            observation = ObservationSignal(
                **{key: value for key, value in payload.items() if key in accepted}
            )
            restored.append(
                observation
                if observation.status == "OPEN"
                else SQLiteMonitorStore._compact_runtime_observation(observation)
            )
        return restored

    def load_runtime_observations(
        self,
        symbol: str,
        *,
        limit: int = 5000,
    ) -> list[ObservationSignal]:
        normalized_symbol = symbol.upper()
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                select observation_signals.payload,
                       {_OBSERVATION_LIFECYCLE_COLUMNS},
                       {_LINKED_CONTEXT_COLUMNS}
                from observation_signals
                left join decision_contexts
                  on decision_contexts.symbol = observation_signals.symbol
                 and decision_contexts.decision_id = observation_signals.decision_id
                where observation_signals.symbol = ?
                order by observation_signals.opened_at desc
                limit ?
                """,
                (normalized_symbol, max(1, int(limit))),
            )
            return self._runtime_observations_from_rows(rows)

    def load_observations_for_profile(
        self,
        symbol: str,
        *,
        lookback_days: int = 7,
    ) -> list[ObservationSignal]:
        normalized_symbol = symbol.upper()
        with self._connect() as connection:
            latest = connection.execute(
                "select max(opened_at) as latest_opened_at from observation_signals where symbol = ?",
                (normalized_symbol,),
            ).fetchone()["latest_opened_at"]
            if latest is None:
                return []
            cutoff = int(latest) - (max(1, int(lookback_days)) + 1) * 86_400_000
            rows = connection.execute(
                f"""
                select observation_signals.payload,
                       {_OBSERVATION_LIFECYCLE_COLUMNS},
                       {_LINKED_CONTEXT_COLUMNS}
                from observation_signals
                left join decision_contexts
                  on decision_contexts.symbol = observation_signals.symbol
                 and decision_contexts.decision_id = observation_signals.decision_id
                where observation_signals.symbol = ?
                  and (observation_signals.status = 'OPEN'
                       or observation_signals.opened_at >= ?)
                order by observation_signals.opened_at desc
                """,
                (normalized_symbol, cutoff),
            )
            return self._runtime_observations_from_rows(rows)

    def load_adaptive_profile_observations(
        self,
        symbol: str,
        *,
        lookback_days: int = 15,
        evaluated_at: int,
        profile_keys: set[str] | tuple[str, ...] | list[str] | None = None,
    ) -> list[ObservationSignal]:
        days = int(lookback_days)
        if days < 15:
            raise ValueError("adaptive profile lookback must be at least 15 days")
        if isinstance(evaluated_at, bool) or not isinstance(evaluated_at, int):
            raise TypeError("evaluated_at must be an integer timestamp")
        if evaluated_at < 0:
            raise ValueError("evaluated_at must not be negative")
        normalized_keys = tuple(
            sorted(
                {
                    str(item).strip()
                    for item in (profile_keys or ())
                    if str(item).strip()
                }
            )
        )
        cutoff = evaluated_at - days * 86_400_000
        profile_sql = _OBSERVATION_CANONICAL_PROFILE_SQL
        profile_filter = ""
        parameters: list[Any] = [symbol.upper(), cutoff, evaluated_at]
        if normalized_keys:
            placeholders = ", ".join("?" for _item in normalized_keys)
            profile_filter = f"and {profile_sql} in ({placeholders})"
            parameters.extend(normalized_keys)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                select observation_signals.payload,
                       {_OBSERVATION_LIFECYCLE_COLUMNS},
                       {_LINKED_CONTEXT_COLUMNS}
                from observation_signals
                left join decision_contexts
                  on decision_contexts.symbol = observation_signals.symbol
                 and decision_contexts.decision_id = observation_signals.decision_id
                where observation_signals.symbol = ?
                  and observation_signals.status = 'SETTLED'
                  and observation_signals.settled_at >= ?
                  and observation_signals.settled_at < ?
                  {profile_filter}
                order by observation_signals.settled_at,
                         observation_signals.opened_at,
                         observation_signals.observation_key
                """,
                parameters,
            )
            return self._runtime_observations_from_rows(rows)

    def save_daily_profile_selection(self, symbol: str, snapshot: dict[str, Any]) -> None:
        payload = json.dumps(snapshot, ensure_ascii=False)
        evaluation_key = int(
            snapshot.get(
                "evaluation_key",
                snapshot.get("lookback_end", snapshot.get("evaluated_at", 0)),
            )
        )
        with self._connect() as connection:
            connection.execute(
                """
                insert into daily_profile_selections(
                    symbol, effective_from, effective_until, status, evaluated_at,
                    evaluation_key, payload
                )
                values (?, ?, ?, ?, ?, ?, ?)
                on conflict(symbol, effective_from) do update set
                    effective_until=excluded.effective_until,
                    status=excluded.status,
                    evaluated_at=excluded.evaluated_at,
                    evaluation_key=excluded.evaluation_key,
                    payload=excluded.payload,
                    updated_at_ms=strftime('%s','now') * 1000
                """,
                (
                    symbol.upper(),
                    int(snapshot["effective_from"]),
                    int(snapshot["effective_until"]),
                    str(snapshot.get("status", "READY")),
                    int(snapshot.get("evaluated_at", 0)),
                    evaluation_key,
                    payload,
                ),
            )

    def load_daily_profile_selection(self, symbol: str, effective_at_ms: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                select payload
                from daily_profile_selections
                where symbol = ? and effective_from <= ? and effective_until > ?
                order by effective_from desc
                limit 1
                """,
                (symbol.upper(), int(effective_at_ms), int(effective_at_ms)),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def load_latest_daily_profile_selection(self, symbol: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                select payload
                from daily_profile_selections
                where symbol = ?
                order by effective_from desc
                limit 1
                """,
                (symbol.upper(),),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def load_daily_profile_selection_as_of(
        self,
        symbol: str,
        evaluation_key: int,
        *,
        evaluated_at_ms: int | None = None,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                select payload
                from daily_profile_selections
                where symbol = ?
                  and evaluation_key <= ?
                  and (? is null or evaluated_at <= ?)
                order by evaluation_key desc, evaluated_at desc
                limit 1
                """,
                (
                    symbol.upper(),
                    int(evaluation_key),
                    evaluated_at_ms,
                    evaluated_at_ms,
                ),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def page_orders(
        self,
        symbol: str,
        *,
        page: int = 1,
        page_size: int = 20,
        direction: str = "",
        level: str = "",
        segment: str = "",
        result: str = "",
        dashboard: bool = False,
    ) -> dict[str, Any]:
        normalized_symbol = symbol.upper()
        filters = {
            "direction": _clean_filter(direction),
            "level": _clean_filter(level),
            "segment": _clean_filter(segment),
            "result": _clean_filter(result),
        }
        clauses = ["orders.symbol = ?"]
        parameters: list[object] = [normalized_symbol]
        for value, expression in (
            (filters["direction"], _ORDER_CANONICAL_DIRECTION_SQL),
            (filters["level"], _ORDER_CANONICAL_LEVEL_SQL),
            (filters["segment"], _ORDER_CANONICAL_SEGMENT_SQL),
        ):
            if value:
                clauses.append(f"upper({expression}) = ?")
                parameters.append(value)
        if filters["result"] == "OPEN":
            clauses.append("upper(orders.status) = 'OPEN'")
        elif filters["result"]:
            clauses.append("upper(coalesce(orders.result, '')) = ?")
            parameters.append(filters["result"])
        where_sql = " and ".join(clauses)
        normalized_page_size = _normalize_page_size(page_size)

        with self._connect() as connection:
            total = int(
                connection.execute(
                    f"""
                    select count(*)
                    from orders
                    left join decision_contexts
                      on decision_contexts.symbol = orders.symbol
                     and decision_contexts.decision_id = orders.decision_id
                    where {where_sql}
                    """,
                    parameters,
                ).fetchone()[0]
            )
            total_pages = max(
                1, (total + normalized_page_size - 1) // normalized_page_size
            )
            normalized_page = min(max(1, int(page or 1)), total_pages)
            offset = (normalized_page - 1) * normalized_page_size
            rows = connection.execute(
                f"""
                select orders.payload,
                       {_ORDER_LIFECYCLE_COLUMNS},
                       {_LINKED_CONTEXT_COLUMNS}
                from orders
                left join decision_contexts
                  on decision_contexts.symbol = orders.symbol
                 and decision_contexts.decision_id = orders.decision_id
                where {where_sql}
                order by orders.opened_at desc, orders.order_id desc
                limit ? offset ?
                """,
                [*parameters, normalized_page_size, offset],
            ).fetchall()
            filter_rows = connection.execute(
                f"""
                select distinct
                       upper({_ORDER_CANONICAL_DIRECTION_SQL}) as direction,
                       upper({_ORDER_CANONICAL_LEVEL_SQL}) as level,
                       upper({_ORDER_CANONICAL_SEGMENT_SQL}) as segment,
                       case
                           when upper(orders.status) = 'OPEN' then 'OPEN'
                           else upper(coalesce(orders.result, ''))
                       end as result
                from orders
                left join decision_contexts
                  on decision_contexts.symbol = orders.symbol
                 and decision_contexts.decision_id = orders.decision_id
                where orders.symbol = ?
                """,
                (normalized_symbol,),
            ).fetchall()

        accepted = {field.name for field in fields(SimulatedOrder)}
        orders = []
        for row in rows:
            payload = _hydrate_decision_linked_payload(
                json.loads(row["payload"]),
                row,
            )
            clean_payload = {
                key: value for key, value in payload.items() if key in accepted
            }
            if "calculated_threshold" not in payload:
                clean_payload["calculated_threshold"] = float(
                    clean_payload.get("threshold", 0.0)
                )
            order = SimulatedOrder(**clean_payload)
            orders.append(
                _dashboard_payload(order) if dashboard else order.to_dict()
            )

        result_order = {"OPEN": 0, "WIN": 1, "LOSS": 2}
        level_order = {"S": 0, "A": 1, "B": 2}
        filter_options = {
            name: {str(row[name]) for row in filter_rows if row[name]}
            for name in ("direction", "level", "segment", "result")
        }
        return {
            "orders": orders,
            "total": total,
            "page": normalized_page,
            "page_size": normalized_page_size,
            "total_pages": total_pages,
            "filters": filters,
            "filter_options": {
                "direction": sorted(filter_options["direction"]),
                "level": sorted(
                    filter_options["level"],
                    key=lambda item: level_order.get(item, 99),
                ),
                "segment": sorted(filter_options["segment"]),
                "result": sorted(
                    filter_options["result"],
                    key=lambda item: result_order.get(item, 99),
                ),
                "page_size": list(ORDER_PAGE_SIZES),
            },
        }

    def page_observations(
        self,
        symbol: str,
        *,
        page: int = 1,
        page_size: int = 20,
        direction: str = "",
        family: str = "",
        tag: str = "",
        segment: str = "",
        result: str = "",
        profile: str = "",
        origin: str = "",
        qualification_state: str = "",
        adaptive_state: str = "",
        entry_structure_state: str = "",
        entry_structure_bias: str = "",
        active_level_source: str = "",
        dashboard: bool = False,
    ) -> dict[str, Any]:
        normalized_symbol = symbol.upper()
        filters = {
            "direction": _clean_filter(direction),
            "family": _clean_filter(family),
            "tag": _clean_filter(tag),
            "segment": _clean_filter(segment),
            "result": _clean_filter(result),
            "profile": _clean_filter(profile),
            "origin": _clean_filter(origin),
            "qualification_state": _clean_filter(qualification_state),
            "adaptive_state": _clean_filter(adaptive_state),
            "entry_structure_state": _clean_filter(entry_structure_state),
            "entry_structure_bias": _clean_filter(entry_structure_bias),
            "active_level_source": _clean_filter(active_level_source),
        }
        clauses = ["observation_signals.symbol = ?"]
        parameters: list[object] = [normalized_symbol]
        for filter_name, expression in _OBSERVATION_CANONICAL_FILTER_SQL.items():
            if filters[filter_name]:
                clauses.append(f"upper({expression}) = ?")
                parameters.append(filters[filter_name])
        if filters["result"] == "OPEN":
            clauses.append("upper(observation_signals.status) = 'OPEN'")
        elif filters["result"]:
            clauses.append("upper(coalesce(observation_signals.result, '')) = ?")
            parameters.append(filters["result"])
        where_sql = " and ".join(clauses)
        normalized_page_size = _normalize_page_size(page_size)

        with self._connect() as connection:
            total = int(
                connection.execute(
                    f"""
                    select count(*)
                    from observation_signals
                    left join decision_contexts
                      on decision_contexts.symbol = observation_signals.symbol
                     and decision_contexts.decision_id = observation_signals.decision_id
                    where {where_sql}
                    """,
                    parameters,
                ).fetchone()[0]
            )
            total_pages = max(
                1, (total + normalized_page_size - 1) // normalized_page_size
            )
            normalized_page = min(max(1, int(page or 1)), total_pages)
            offset = (normalized_page - 1) * normalized_page_size
            rows = connection.execute(
                f"""
                select observation_signals.payload,
                       {_OBSERVATION_LIFECYCLE_COLUMNS},
                       {_LINKED_CONTEXT_COLUMNS}
                from observation_signals
                left join decision_contexts
                  on decision_contexts.symbol = observation_signals.symbol
                 and decision_contexts.decision_id = observation_signals.decision_id
                where {where_sql}
                order by observation_signals.opened_at desc,
                         observation_signals.observation_key desc
                limit ? offset ?
                """,
                [*parameters, normalized_page_size, offset],
            ).fetchall()
            filter_options = (
                self._observation_dashboard_filter_options_sql(
                    connection,
                    normalized_symbol,
                )
                if dashboard
                else self._observation_filter_options_sql(
                    connection,
                    normalized_symbol,
                )
            )

        accepted = {field.name for field in fields(ObservationSignal)}
        observations = []
        for row in rows:
            payload = _hydrate_decision_linked_payload(
                json.loads(row["payload"]),
                row,
            )
            clean_payload = {
                key: value for key, value in payload.items() if key in accepted
            }
            observation = ObservationSignal(**clean_payload)
            observations.append(
                _dashboard_payload(observation)
                if dashboard
                else observation.to_dict()
            )
        return {
            "observations": observations,
            "total": total,
            "page": normalized_page,
            "page_size": normalized_page_size,
            "total_pages": total_pages,
            "filters": filters,
            "filter_options": filter_options,
        }

    @staticmethod
    def _observation_dashboard_filter_options_sql(
        connection: sqlite3.Connection,
        symbol: str,
    ) -> dict[str, list[str] | list[int]]:
        columns = {
            "direction": "direction",
            "family": "strategy_family",
            "tag": "strategy_tag",
            "segment": "threshold_segment",
            "origin": "candidate_origin",
            "qualification_state": "qualification_state",
            "adaptive_state": "adaptive_state",
            "entry_structure_state": "entry_structure_state",
            "entry_structure_bias": "entry_structure_bias",
            "active_level_source": "active_level_source",
        }
        projections = [
            f"upper(coalesce(nullif({column}, ''), 'UNKNOWN')) as {name}"
            for name, column in columns.items()
        ]
        rows = connection.execute(
            f"""
            select distinct {', '.join(projections)},
                   case
                       when upper(status) = 'OPEN' then 'OPEN'
                       else upper(coalesce(result, ''))
                   end as result
            from observation_signals
            where symbol = ?
            """,
            (symbol,),
        ).fetchall()
        options = {
            name: sorted(
                {
                    str(row[name])
                    for row in rows
                    if row[name] is not None and str(row[name])
                }
            )
            for name in columns
        }
        result_order = {"OPEN": 0, "WIN": 1, "LOSS": 2}
        results = sorted(
            {str(row["result"]) for row in rows if row["result"]},
            key=lambda item: result_order.get(item, 99),
        )
        return options | {
            "profile": [],
            "result": results,
            "page_size": list(ORDER_PAGE_SIZES),
        }

    @staticmethod
    def _observation_filter_options_sql(
        connection: sqlite3.Connection,
        symbol: str,
    ) -> dict[str, list[str] | list[int]]:
        projections = [
            f"upper({expression}) as {name}"
            for name, expression in _OBSERVATION_CANONICAL_FILTER_SQL.items()
        ]
        projections.append(
            "case when upper(observation_signals.status) = 'OPEN' "
            "then 'OPEN' else upper(coalesce(observation_signals.result, '')) "
            "end as result"
        )
        rows = connection.execute(
            f"""
            select distinct {', '.join(projections)}
            from observation_signals
            left join decision_contexts
              on decision_contexts.symbol = observation_signals.symbol
             and decision_contexts.decision_id = observation_signals.decision_id
            where observation_signals.symbol = ?
            """,
            (symbol,),
        ).fetchall()

        options = {
            name: sorted(
                {
                    str(row[name])
                    for row in rows
                    if row[name] is not None and str(row[name])
                }
            )
            for name in _OBSERVATION_CANONICAL_FILTER_SQL
        }
        result_order = {"OPEN": 0, "WIN": 1, "LOSS": 2}
        results = sorted(
            {str(row["result"]) for row in rows if row["result"]},
            key=lambda item: result_order.get(item, 99),
        )
        return options | {
            "result": results,
            "page_size": list(ORDER_PAGE_SIZES),
        }

    def observation_summary(
        self,
        symbol: str,
        limit: int | None = None,
        *,
        window: str = "14d",
        group_limit: int = 50,
    ) -> dict[str, Any]:
        del limit  # Legacy row limits are accepted but no longer truncate statistics.
        normalized_window = self._normalize_observation_window(window)
        normalized_symbol = symbol.upper()
        with self._connect() as connection:
            anchor_row = connection.execute(
                "select max(opened_at) from observation_signals where symbol = ?",
                (normalized_symbol,),
            ).fetchone()
            anchor = anchor_row[0] if anchor_row else None
            cutoff = None
            if anchor is not None and normalized_window != "all":
                days = int(normalized_window[:-1])
                cutoff = int(anchor) - days * 86_400_000
            where_sql = "symbol = ?"
            parameters: list[object] = [normalized_symbol]
            if cutoff is not None:
                where_sql += " and opened_at >= ?"
                parameters.append(cutoff)

            total_row = connection.execute(
                self._observation_summary_sql(where_sql, grouped=False),
                parameters,
            ).fetchone()
            group_rows = connection.execute(
                self._observation_summary_sql(where_sql, grouped=True),
                parameters,
            ).fetchall()

        total = self._observation_stats_from_sql(total_row)
        groups = [
            _finalize_observation_group(self._observation_stats_from_sql(row))
            for row in group_rows
        ]
        groups.sort(
            key=lambda item: (
                _observation_action_rank(item["action"]),
                -item["settled"],
                -item["ev"],
                item["strategy_family"],
                item["strategy_tag"],
                item["direction"],
                item["threshold_segment"],
            )
        )
        action_counts: dict[str, int] = {}
        for group in groups:
            action_counts[group["action"]] = action_counts.get(group["action"], 0) + 1
        return {
            "total": _finalize_observation_stats(total),
            "groups": groups[: max(1, int(group_limit))],
            "group_limit": max(1, int(group_limit)),
            "action_counts": action_counts,
            "rules": {
                "promote_sample": OBSERVATION_PROMOTE_SAMPLE,
                "watch_sample": OBSERVATION_WATCH_SAMPLE,
                "promote_win_rate": 0.6,
                "promote_ev": 0.8,
                "block_win_rate": 0.5,
                "block_ev": -1.0,
            },
            "window": normalized_window,
            "cutoff": cutoff,
            "anchor": anchor,
        }

    @staticmethod
    def _normalize_observation_window(window: str | int) -> str:
        if isinstance(window, int):
            return "14d"
        normalized = str(window or "14d").strip().lower()
        if normalized not in {"7d", "14d", "30d", "all"}:
            raise ValueError("observation window must be 7d, 14d, 30d, or all")
        return normalized

    @staticmethod
    def _observation_summary_sql(where_sql: str, *, grouped: bool) -> str:
        identity = (
            "timeframe_minutes, strategy_family, strategy_tag, direction, "
            "threshold_segment, "
            if grouped
            else ""
        )
        group_by = (
            " group by timeframe_minutes, strategy_family, strategy_tag, "
            "direction, threshold_segment"
            if grouped
            else ""
        )
        settled = (
            "upper(status) != 'OPEN' and "
            "upper(coalesce(result, '')) in ('WIN', 'LOSS')"
        )
        pnl = (
            "case when "
            + settled
            + " then case "
            "when coalesce(cast(json_extract(payload, '$.pnl') as real), 0) != 0 "
            "then cast(json_extract(payload, '$.pnl') as real) "
            "when upper(result) = 'WIN' then 8.0 else -10.0 end else 0 end"
        )
        return f"""
            select {identity}
                   count(*) as signals,
                   sum(case when not ({settled}) then 1 else 0 end) as open,
                   sum(case when {settled} then 1 else 0 end) as settled,
                   sum(case when {settled} and upper(result) = 'WIN' then 1 else 0 end) as wins,
                   sum(case when {settled} and upper(result) = 'LOSS' then 1 else 0 end) as losses,
                   sum({pnl}) as pnl,
                   min(opened_at) as first_opened_at,
                   max(opened_at) as last_opened_at
            from observation_signals
            where {where_sql}{group_by}
        """

    @staticmethod
    def _observation_stats_from_sql(row: sqlite3.Row | None) -> dict[str, Any]:
        values = dict(row) if row is not None else {}
        return _empty_observation_stats(
            timeframe_minutes=values.get("timeframe_minutes"),
            strategy_family=str(values.get("strategy_family") or ""),
            strategy_tag=str(values.get("strategy_tag") or ""),
            direction=str(values.get("direction") or ""),
            threshold_segment=str(values.get("threshold_segment") or ""),
        ) | {
            "signals": int(values.get("signals") or 0),
            "open": int(values.get("open") or 0),
            "settled": int(values.get("settled") or 0),
            "wins": int(values.get("wins") or 0),
            "losses": int(values.get("losses") or 0),
            "pnl": float(values.get("pnl") or 0.0),
            "first_opened_at": values.get("first_opened_at"),
            "last_opened_at": values.get("last_opened_at"),
        }

    def order_profile_summary(
        self,
        symbol: str,
        limit: int = 5000,
        *,
        profile_guard_min_history: int = 15,
        profile_guard_min_group_size: int = 2,
    ) -> dict[str, Any]:
        return self.prepare_order_profile_summary(
            symbol,
            limit=limit,
            profile_guard_min_history=profile_guard_min_history,
            profile_guard_min_group_size=profile_guard_min_group_size,
        )

    def _profile_summary_key(
        self,
        symbol: str,
        limit: int,
        profile_guard_min_history: int,
        profile_guard_min_group_size: int,
    ) -> tuple[str, int, str, int, int, int]:
        return (
            symbol.upper(),
            self.profile_summary_schema_version,
            self.profile_algorithm_fingerprint,
            max(1, int(limit)),
            max(1, int(profile_guard_min_history)),
            max(1, int(profile_guard_min_group_size)),
        )

    def prepare_order_profile_summary(
        self,
        symbol: str,
        limit: int = 5000,
        *,
        profile_guard_min_history: int = 15,
        profile_guard_min_group_size: int = 2,
    ) -> dict[str, Any]:
        key = self._profile_summary_key(
            symbol,
            limit,
            profile_guard_min_history,
            profile_guard_min_group_size,
        )
        with self._profile_summary_lock:
            if self._profile_summary_closed:
                raise RuntimeError("profile summary worker is closed")
        self._remember_profile_summary_key(key)
        return self.profile_summary_snapshot(
            key[0],
            limit=key[3],
            profile_guard_min_history=key[4],
            profile_guard_min_group_size=key[5],
        )

    def profile_summary_snapshot(
        self,
        symbol: str,
        limit: int = 5000,
        *,
        profile_guard_min_history: int = 15,
        profile_guard_min_group_size: int = 2,
    ) -> dict[str, Any]:
        key = self._profile_summary_key(
            symbol,
            limit,
            profile_guard_min_history,
            profile_guard_min_group_size,
        )
        self._remember_profile_summary_key(key)
        current_revision, source_revision, payload = (
            self._read_profile_summary_materialization(key)
        )
        stale = source_revision is None or source_revision != current_revision
        if source_revision is None:
            summary = {}
            status = "PREPARING"
        else:
            summary = payload
            status = "STALE" if stale else "READY"
        summary.update(
            {
                "cache_status": status,
                "source_revision": source_revision,
                "current_revision": current_revision,
                "stale": stale,
            }
        )
        if stale:
            self._schedule_profile_summary_rebuild(key)
        return summary

    def _remember_profile_summary_key(
        self,
        key: tuple[str, int, str, int, int, int],
    ) -> None:
        with self._profile_summary_lock:
            if key in self._profile_summary_keys:
                return
            combinations = sum(
                1
                for existing in self._profile_summary_keys
                if existing[:3] == key[:3]
            )
            if combinations >= MAX_PROFILE_PARAMETER_COMBINATIONS_PER_SYMBOL:
                raise ValueError(
                    "profile summary parameter combinations exceed the per-symbol limit"
                )
            self._profile_summary_keys.add(key)

    def exact_order_profile_summary(
        self,
        symbol: str,
        limit: int = 5000,
        *,
        profile_guard_min_history: int = 15,
        profile_guard_min_group_size: int = 2,
    ) -> dict[str, Any]:
        key = self._profile_summary_key(
            symbol,
            limit,
            profile_guard_min_history,
            profile_guard_min_group_size,
        )
        self._remember_profile_summary_key(key)
        current_revision, source_revision, guard = (
            self._read_profile_guard_materialization(key)
        )
        if source_revision != current_revision:
            self._schedule_profile_summary_rebuild(key)
            with self._profile_summary_lock:
                pending = self._profile_summary_futures.get(key)
            if pending is not None:
                pending.result(timeout=30)
            current_revision, source_revision, guard = (
                self._read_profile_guard_materialization(key)
            )
        if source_revision != current_revision:
            raise RuntimeError("exact profile summary is not ready")
        return {
            "profile_guard": guard,
            "cache_status": "READY",
            "source_revision": source_revision,
            "current_revision": current_revision,
            "stale": False,
        }

    def profile_summary_revision(self, symbol: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "select revision from profile_summary_revisions where symbol = ?",
                (symbol.upper(),),
            ).fetchone()
        return 0 if row is None else int(row["revision"])

    @staticmethod
    def _bump_profile_summary_revision(
        connection: sqlite3.Connection,
        symbol: str,
    ) -> int:
        normalized_symbol = symbol.upper()
        connection.execute(
            """
            insert into profile_summary_revisions(symbol, revision)
            values (?, 1)
            on conflict(symbol) do update set
                revision = profile_summary_revisions.revision + 1,
                updated_at_ms = strftime('%s','now') * 1000
            """,
            (normalized_symbol,),
        )
        row = connection.execute(
            "select revision from profile_summary_revisions where symbol = ?",
            (normalized_symbol,),
        ).fetchone()
        return int(row["revision"])

    @staticmethod
    def _carry_profile_guard_materializations(
        connection: sqlite3.Connection,
        symbol: str,
        revision: int,
    ) -> None:
        previous_revision = int(revision) - 1
        if previous_revision < 0:
            return
        connection.execute(
            """
            update profile_guard_materializations
            set source_revision = ?,
                updated_at_ms = strftime('%s','now') * 1000
            where symbol = ? and source_revision = ?
              and (
                  select count(*) from order_entry_snapshots
                  where order_entry_snapshots.symbol = ?
              ) <= profile_guard_materializations.snapshot_limit
            """,
            (
                int(revision),
                symbol.upper(),
                previous_revision,
                symbol.upper(),
            ),
        )

    @staticmethod
    def _promote_profile_guard_settlement_branch(
        connection: sqlite3.Connection,
        symbol: str,
        base_revision: int,
        target_revision: int,
        order: SimulatedOrder,
    ) -> None:
        expected_pnl = (
            round(order.win_return - order.stake, 4)
            if order.result == "WIN"
            else -round(order.stake, 4)
        )
        if order.result not in {"WIN", "LOSS"} or order.pnl != expected_pnl:
            return
        connection.execute(
            """
            insert into profile_guard_materializations(
                symbol, summary_schema_version, algorithm_fingerprint,
                snapshot_limit, profile_guard_min_history,
                profile_guard_min_group_size, source_revision, payload
            )
            select symbol, summary_schema_version, algorithm_fingerprint,
                   snapshot_limit, profile_guard_min_history,
                   profile_guard_min_group_size, ?, payload
            from profile_guard_settlement_branches
            where symbol = ? and base_revision = ?
              and order_id = ? and result = ?
            on conflict(
                symbol, summary_schema_version, algorithm_fingerprint,
                snapshot_limit, profile_guard_min_history,
                profile_guard_min_group_size
            ) do update set
                source_revision = excluded.source_revision,
                payload = excluded.payload,
                updated_at_ms = strftime('%s','now') * 1000
            where profile_guard_materializations.source_revision <=
                  excluded.source_revision
            """,
            (
                int(target_revision),
                symbol.upper(),
                int(base_revision),
                order.id,
                order.result,
            ),
        )
        connection.execute(
            """
            insert into profile_guard_settlement_branches(
                symbol, summary_schema_version, algorithm_fingerprint,
                snapshot_limit, profile_guard_min_history,
                profile_guard_min_group_size, base_revision,
                order_id, result, payload
            )
            select symbol, summary_schema_version, algorithm_fingerprint,
                   snapshot_limit, profile_guard_min_history,
                   profile_guard_min_group_size, ?, pending_order_id,
                   pending_result, payload
            from profile_guard_settlement_successors
            where symbol = ? and base_revision = ?
              and settled_order_id = ? and settled_result = ?
            on conflict(
                symbol, summary_schema_version, algorithm_fingerprint,
                snapshot_limit, profile_guard_min_history,
                profile_guard_min_group_size, base_revision, order_id, result
            ) do update set
                payload = excluded.payload,
                updated_at_ms = strftime('%s','now') * 1000
            """,
            (
                int(target_revision),
                symbol.upper(),
                int(base_revision),
                order.id,
                order.result,
            ),
        )

    def _read_profile_summary_materialization(
        self,
        key: tuple[str, int, str, int, int, int],
    ) -> tuple[int, int | None, dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                """
                select coalesce(revisions.revision, 0) as current_revision,
                       materialized.source_revision, materialized.payload
                from (select ? as symbol) as requested
                left join profile_summary_revisions as revisions
                  on revisions.symbol = requested.symbol
                left join profile_summary_materializations as materialized
                  on materialized.symbol = requested.symbol
                 and materialized.summary_schema_version = ?
                 and materialized.algorithm_fingerprint = ?
                 and materialized.snapshot_limit = ?
                 and materialized.profile_guard_min_history = ?
                 and materialized.profile_guard_min_group_size = ?
                """,
                key,
            ).fetchone()
        current_revision = int(row["current_revision"])
        if row["source_revision"] is None:
            return current_revision, None, {}
        payload = json.loads(row["payload"])
        if not isinstance(payload, dict):
            raise ValueError("materialized profile summary must be an object")
        return current_revision, int(row["source_revision"]), payload

    def _read_profile_guard_materialization(
        self,
        key: tuple[str, int, str, int, int, int],
    ) -> tuple[int, int | None, dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                """
                select coalesce(revisions.revision, 0) as current_revision,
                       materialized.source_revision, materialized.payload
                from (select ? as symbol) as requested
                left join profile_summary_revisions as revisions
                  on revisions.symbol = requested.symbol
                left join profile_guard_materializations as materialized
                  on materialized.symbol = requested.symbol
                 and materialized.summary_schema_version = ?
                 and materialized.algorithm_fingerprint = ?
                 and materialized.snapshot_limit = ?
                 and materialized.profile_guard_min_history = ?
                 and materialized.profile_guard_min_group_size = ?
                """,
                key,
            ).fetchone()
        current_revision = int(row["current_revision"])
        if row["source_revision"] is None:
            return current_revision, None, {}
        payload = json.loads(row["payload"])
        if not isinstance(payload, dict):
            raise ValueError("materialized profile guard must be an object")
        return current_revision, int(row["source_revision"]), payload

    def _schedule_profile_summary_rebuild(
        self,
        key: tuple[str, int, str, int, int, int],
    ) -> None:
        with self._profile_summary_condition:
            if self._profile_summary_closed:
                raise RuntimeError("profile summary worker is closed")
            existing = self._profile_summary_futures.get(key)
            if existing is not None and not existing.done():
                return
            future = self._profile_summary_executor.submit(
                self._rebuild_profile_summary,
                key,
            )
            self._profile_summary_futures[key] = future
            future.add_done_callback(
                lambda completed, rebuild_key=key: (
                    self._profile_summary_rebuild_completed(rebuild_key, completed)
                )
            )

    def _profile_summary_rebuild_completed(
        self,
        key: tuple[str, int, str, int, int, int],
        future: Future,
    ) -> None:
        try:
            error = future.exception()
        except CancelledError:
            error = None
        completed_current_revision = (
            error is None
            and not future.cancelled()
            and future.result() is True
        )
        with self._profile_summary_condition:
            if self._profile_summary_futures.get(key) is future:
                self._profile_summary_futures.pop(key, None)
            if completed_current_revision:
                self._profile_summary_dirty.discard(key[0])
            else:
                self._profile_summary_dirty.add(key[0])
            self._profile_summary_condition.notify_all()

    def _rebuild_profile_summary(
        self,
        key: tuple[str, int, str, int, int, int],
    ) -> bool:
        for attempt in range(PROFILE_SUMMARY_MAX_CAS_RETRIES):
            if self._profile_summary_stop.is_set():
                return False
            revision, samples = self._profile_summary_rebuild_input(key)
            claimed = self._claim_profile_summary_lease(key, revision)
            if claimed is None:
                return False
            if not claimed:
                if self._wait_for_external_profile_summary(key, revision):
                    return True
                continue
            try:
                if self._profile_summary_stop.is_set():
                    return False
                summary = self._compute_profile_summary(key, samples)
                branches = self._compute_profile_guard_settlement_branches(
                    key,
                    samples,
                )
                successors = self._compute_profile_guard_settlement_successors(
                    key,
                    samples,
                )
                if self._profile_summary_stop.is_set():
                    return False
                if self._write_profile_summary_materialization(
                    key,
                    revision,
                    summary,
                    guard_branches=branches,
                    guard_successors=successors,
                ):
                    return True
            finally:
                self._release_profile_summary_lease(key, revision)
            self._profile_summary_stop.wait(min(0.05 * (2**attempt), 0.4))
        raise RuntimeError("profile summary rebuild exceeded CAS retry limit")

    def _claim_profile_summary_lease(
        self,
        key: tuple[str, int, str, int, int, int],
        revision: int,
    ) -> bool | None:
        now_ms = int(time.time() * 1000)
        try:
            with self._connect() as connection:
                connection.execute("begin immediate")
                ensure_write_allowed(
                    capacity_from_connection(connection),
                    StorageWriteClass.REBUILDABLE_AUXILIARY,
                )
                connection.execute(
                    "delete from profile_summary_leases where expires_at_ms <= ?",
                    (now_ms,),
                )
                cursor = connection.execute(
                    """
                    insert or ignore into profile_summary_leases(
                        symbol, summary_schema_version, algorithm_fingerprint,
                        snapshot_limit, profile_guard_min_history,
                        profile_guard_min_group_size, source_revision,
                        owner_id, expires_at_ms
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (*key, int(revision), self._profile_summary_owner_id, now_ms + PROFILE_SUMMARY_LEASE_MS),
                )
                return cursor.rowcount == 1
        except RebuildableAuxiliaryCapacityError:
            return None
        except sqlite3.Error as error:
            try:
                raise_for_sqlite_write_error(
                    error,
                    StorageWriteClass.REBUILDABLE_AUXILIARY,
                )
            except RebuildableAuxiliaryCapacityError:
                return None

    def _release_profile_summary_lease(
        self,
        key: tuple[str, int, str, int, int, int],
        revision: int,
    ) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    delete from profile_summary_leases
                    where symbol = ? and summary_schema_version = ?
                      and algorithm_fingerprint = ? and snapshot_limit = ?
                      and profile_guard_min_history = ?
                      and profile_guard_min_group_size = ?
                      and source_revision = ? and owner_id = ?
                    """,
                    (*key, int(revision), self._profile_summary_owner_id),
                )
        except (sqlite3.Error, RebuildableAuxiliaryCapacityError):
            return

    def _wait_for_external_profile_summary(
        self,
        key: tuple[str, int, str, int, int, int],
        revision: int,
    ) -> bool:
        deadline = time.monotonic() + PROFILE_SUMMARY_LEASE_MS / 1000
        while not self._profile_summary_stop.wait(0.05):
            current, source, _payload = self._read_profile_summary_materialization(key)
            if current != revision:
                return False
            if source == current:
                return True
            with self._connect() as connection:
                lease = connection.execute(
                    """
                    select expires_at_ms from profile_summary_leases
                    where symbol = ? and summary_schema_version = ?
                      and algorithm_fingerprint = ? and snapshot_limit = ?
                      and profile_guard_min_history = ?
                      and profile_guard_min_group_size = ?
                      and source_revision = ?
                    """,
                    (*key, int(revision)),
                ).fetchone()
            if (
                lease is None
                or int(lease["expires_at_ms"]) <= int(time.time() * 1000)
            ):
                current, source, _payload = self._read_profile_summary_materialization(
                    key
                )
                return current == revision and source == current
            if time.monotonic() >= deadline:
                return False
        return False

    def _profile_summary_rebuild_input(
        self,
        key: tuple[str, int, str, int, int, int],
    ) -> tuple[int, list[dict[str, Any]]]:
        with self._connect() as connection:
            connection.execute("begin")
            revision_row = connection.execute(
                "select revision from profile_summary_revisions where symbol = ?",
                (key[0],),
            ).fetchone()
            rows = connection.execute(
                """
                select * from order_entry_snapshots
                where symbol = ?
                order by opened_at desc, order_id desc
                limit ?
                """,
                (key[0], key[3]),
            ).fetchall()
        snapshots = self._profile_snapshot_rows(rows)
        samples = [
            sample_from_entry_snapshot(snapshot)
            for snapshot in reversed(snapshots)
        ]
        revision = 0 if revision_row is None else int(revision_row["revision"])
        return revision, samples

    @staticmethod
    def _compute_profile_summary(
        key: tuple[str, int, str, int, int, int],
        samples: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        return summarize_order_samples_with_guard(
            samples,
            profile_guard_min_history=key[4],
            profile_guard_min_group_size=key[5],
        )

    @staticmethod
    def _compute_profile_guard_settlement_branches(
        key: tuple[str, int, str, int, int, int],
        samples: Sequence[dict[str, Any]],
    ) -> list[tuple[int, str, dict[str, Any]]]:
        branches: list[tuple[int, str, dict[str, Any]]] = []
        for index, sample in enumerate(samples):
            if sample.get("result") in {"WIN", "LOSS"}:
                continue
            order_id = int(sample.get("order_id") or 0)
            if order_id <= 0:
                continue
            for result in ("WIN", "LOSS"):
                branch_samples = list(samples)
                branch_sample = dict(sample)
                branch_sample["result"] = result
                branch_sample["pnl"] = round(
                    float(branch_sample.get("win_return") or 0.0)
                    - float(branch_sample.get("stake") or 0.0),
                    4,
                ) if result == "WIN" else -round(
                    float(branch_sample.get("stake") or 0.0),
                    4,
                )
                branch_samples[index] = branch_sample
                branches.append(
                    (
                        order_id,
                        result,
                        summarize_profile_guard_materialization(
                            branch_samples,
                            min_history=key[4],
                            min_group_size=key[5],
                        ),
                    )
                )
        return branches

    @staticmethod
    def _profile_guard_branch_sample(
        sample: Mapping[str, Any],
        result: str,
    ) -> dict[str, Any]:
        branch_sample = dict(sample)
        branch_sample["result"] = result
        branch_sample["pnl"] = (
            round(
                float(branch_sample.get("win_return") or 0.0)
                - float(branch_sample.get("stake") or 0.0),
                4,
            )
            if result == "WIN"
            else -round(float(branch_sample.get("stake") or 0.0), 4)
        )
        return branch_sample

    @classmethod
    def _compute_profile_guard_settlement_successors(
        cls,
        key: tuple[str, int, str, int, int, int],
        samples: Sequence[dict[str, Any]],
    ) -> list[tuple[int, str, int, str, dict[str, Any]]]:
        open_samples = [
            (index, sample)
            for index, sample in enumerate(samples)
            if sample.get("result") not in {"WIN", "LOSS"}
            and int(sample.get("order_id") or 0) > 0
        ]
        if len(open_samples) != 2:
            return []
        (first_index, first), (second_index, second) = open_samples
        successors = []
        for first_result in ("WIN", "LOSS"):
            for second_result in ("WIN", "LOSS"):
                branch_samples = list(samples)
                branch_samples[first_index] = cls._profile_guard_branch_sample(
                    first,
                    first_result,
                )
                branch_samples[second_index] = cls._profile_guard_branch_sample(
                    second,
                    second_result,
                )
                guard = summarize_profile_guard_materialization(
                    branch_samples,
                    min_history=key[4],
                    min_group_size=key[5],
                )
                first_id = int(first.get("order_id") or 0)
                second_id = int(second.get("order_id") or 0)
                successors.extend(
                    (
                        (first_id, first_result, second_id, second_result, guard),
                        (second_id, second_result, first_id, first_result, guard),
                    )
                )
        return successors

    def _write_profile_summary_materialization(
        self,
        key: tuple[str, int, str, int, int, int],
        source_revision: int,
        summary: Mapping[str, Any],
        *,
        guard_branches: Sequence[tuple[int, str, Mapping[str, Any]]] = (),
        guard_successors: Sequence[
            tuple[int, str, int, str, Mapping[str, Any]]
        ] = (),
    ) -> bool:
        payload = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
        try:
            with self._connect() as connection:
                connection.execute("begin immediate")
                ensure_write_allowed(
                    capacity_from_connection(connection),
                    StorageWriteClass.REBUILDABLE_AUXILIARY,
                )
                revision_row = connection.execute(
                    "select revision from profile_summary_revisions where symbol = ?",
                    (key[0],),
                ).fetchone()
                current_revision = 0 if revision_row is None else int(revision_row["revision"])
                if current_revision != int(source_revision):
                    return False
                existing = connection.execute(
                    """
                    select source_revision from profile_summary_materializations
                    where symbol = ? and summary_schema_version = ?
                      and algorithm_fingerprint = ? and snapshot_limit = ?
                      and profile_guard_min_history = ?
                      and profile_guard_min_group_size = ?
                    """,
                    key,
                ).fetchone()
                if existing is not None and int(existing["source_revision"]) > source_revision:
                    return False
                connection.execute(
                    """
                    insert into profile_summary_materializations(
                        symbol, summary_schema_version, algorithm_fingerprint,
                        snapshot_limit, profile_guard_min_history,
                        profile_guard_min_group_size, source_revision, payload
                    ) values (?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(
                        symbol, summary_schema_version, algorithm_fingerprint,
                        snapshot_limit, profile_guard_min_history,
                        profile_guard_min_group_size
                    ) do update set
                        source_revision = excluded.source_revision,
                        payload = excluded.payload,
                        updated_at_ms = strftime('%s','now') * 1000
                    where excluded.source_revision >=
                          profile_summary_materializations.source_revision
                    """,
                    (*key, int(source_revision), payload),
                )
                guard_payload = json.dumps(
                    summary.get("profile_guard") or {},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                connection.execute(
                    """
                    insert into profile_guard_materializations(
                        symbol, summary_schema_version, algorithm_fingerprint,
                        snapshot_limit, profile_guard_min_history,
                        profile_guard_min_group_size, source_revision, payload
                    ) values (?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(
                        symbol, summary_schema_version, algorithm_fingerprint,
                        snapshot_limit, profile_guard_min_history,
                        profile_guard_min_group_size
                    ) do update set
                        source_revision = excluded.source_revision,
                        payload = excluded.payload,
                        updated_at_ms = strftime('%s','now') * 1000
                    where excluded.source_revision >=
                          profile_guard_materializations.source_revision
                    """,
                    (*key, int(source_revision), guard_payload),
                )
                connection.execute(
                    """
                    delete from profile_guard_settlement_branches
                    where symbol = ? and summary_schema_version = ?
                      and algorithm_fingerprint = ? and snapshot_limit = ?
                      and profile_guard_min_history = ?
                      and profile_guard_min_group_size = ?
                      and base_revision = ?
                    """,
                    (*key, int(source_revision)),
                )
                connection.executemany(
                    """
                    insert into profile_guard_settlement_branches(
                        symbol, summary_schema_version, algorithm_fingerprint,
                        snapshot_limit, profile_guard_min_history,
                        profile_guard_min_group_size, base_revision,
                        order_id, result, payload
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            *key,
                            int(source_revision),
                            int(order_id),
                            str(result),
                            json.dumps(
                                branch,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        )
                        for order_id, result, branch in guard_branches
                    ),
                )
                connection.execute(
                    """
                    delete from profile_guard_settlement_successors
                    where symbol = ? and summary_schema_version = ?
                      and algorithm_fingerprint = ? and snapshot_limit = ?
                      and profile_guard_min_history = ?
                      and profile_guard_min_group_size = ?
                      and base_revision = ?
                    """,
                    (*key, int(source_revision)),
                )
                connection.executemany(
                    """
                    insert into profile_guard_settlement_successors(
                        symbol, summary_schema_version, algorithm_fingerprint,
                        snapshot_limit, profile_guard_min_history,
                        profile_guard_min_group_size, base_revision,
                        settled_order_id, settled_result,
                        pending_order_id, pending_result, payload
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            *key,
                            int(source_revision),
                            int(settled_order_id),
                            str(settled_result),
                            int(pending_order_id),
                            str(pending_result),
                            json.dumps(
                                successor,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        )
                        for (
                            settled_order_id,
                            settled_result,
                            pending_order_id,
                            pending_result,
                            successor,
                        ) in guard_successors
                    ),
                )
                self._prune_profile_materializations(connection, key[0])
            return True
        except RebuildableAuxiliaryCapacityError:
            return False
        except sqlite3.Error as error:
            try:
                raise_for_sqlite_write_error(
                    error,
                    StorageWriteClass.REBUILDABLE_AUXILIARY,
                )
            except RebuildableAuxiliaryCapacityError:
                return False

    @staticmethod
    def _prune_profile_materializations(
        connection: sqlite3.Connection,
        symbol: str,
    ) -> None:
        connection.execute(
            """
            delete from profile_summary_materializations
            where symbol = ? and rowid not in (
                select rowid from profile_summary_materializations
                where symbol = ?
                order by updated_at_ms desc, rowid desc
                limit ?
            )
            """,
            (symbol, symbol, MAX_PROFILE_MATERIALIZATIONS_PER_SYMBOL),
        )
        connection.execute(
            """
            delete from profile_guard_materializations
            where symbol = ? and rowid not in (
                select rowid from profile_guard_materializations
                where symbol = ?
                order by updated_at_ms desc, rowid desc
                limit ?
            )
            """,
            (symbol, symbol, MAX_PROFILE_MATERIALIZATIONS_PER_SYMBOL),
        )
        connection.execute(
            """
            delete from profile_guard_settlement_branches
            where symbol = ? and rowid not in (
                select rowid from profile_guard_settlement_branches
                where symbol = ?
                order by base_revision desc, updated_at_ms desc, rowid desc
                limit ?
            )
            """,
            (symbol, symbol, MAX_PROFILE_MATERIALIZATIONS_PER_SYMBOL * 4),
        )
        connection.execute(
            """
            delete from profile_guard_settlement_successors
            where symbol = ? and rowid not in (
                select rowid from profile_guard_settlement_successors
                where symbol = ?
                order by base_revision desc, updated_at_ms desc, rowid desc
                limit ?
            )
            """,
            (symbol, symbol, MAX_PROFILE_MATERIALIZATIONS_PER_SYMBOL * 8),
        )

    def _refresh_profile_summary_cache(
        self,
        symbol: str,
        *,
        sample: Mapping[str, Any] | None = None,
        settlement: SimulatedOrder | None = None,
    ) -> None:
        normalized_symbol = symbol.upper()
        with self._profile_summary_lock:
            keys = tuple(
                key
                for key in self._profile_summary_keys
                if key[0] == normalized_symbol
            )
            self._profile_summary_dirty.add(normalized_symbol)
        for key in keys:
            self._schedule_profile_summary_rebuild(key)

    @staticmethod
    def _insert_individual_signal_audit(
        connection: sqlite3.Connection,
        symbol: str,
        audit: DecisionAudit,
    ) -> None:
        normalized_symbol = symbol.upper()
        normalized_decision = str(audit.decision or "UNKNOWN").upper()
        audit_context = dict(audit.audit_context or {})
        reason_code = str(
            audit_context.get("reason_code")
            or audit.signal.first_decisive_block
            or normalized_decision
        )
        event_kind = str(audit.event_kind or "").strip().upper()
        if not event_kind:
            if normalized_decision == "OPENED":
                event_kind = "ORDER_OPENED"
            elif normalized_decision.endswith("BLOCKED"):
                event_kind = "DECISIVE_BLOCK"
            else:
                event_kind = "DECISION"
        payload_json = json.dumps(
            _signal_audit_payload(
                audit.signal,
                normalized_decision,
                audit_context,
                reason_code,
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        values = (
            normalized_symbol,
            int(audit.created_at_ms),
            normalized_decision,
            audit.signal.direction,
            audit.signal.timeframe_minutes,
            audit.signal.threshold_segment,
            audit.signal.regime,
            audit.signal.score,
            audit.signal.threshold,
            audit.signal.reason,
            payload_json,
            SIGNAL_AUDIT_VERSION,
            audit.signal.decision_id,
            audit.signal.runtime_config_hash,
            event_kind,
            int(audit.created_at_ms),
            int(audit.created_at_ms),
            1,
            audit.signal.score,
            audit.signal.score,
            None,
        )
        existing = connection.execute(
            """
            select symbol, created_at_ms, decision, direction, timeframe_minutes,
                   threshold_segment, regime, score, threshold, reason, payload,
                   record_version, decision_id, runtime_config_hash, event_kind,
                   first_at_ms, last_at_ms, occurrences, score_min, score_max,
                   aggregation_key
            from signal_audit
            where symbol = ? and decision_id = ?
            """,
            (normalized_symbol, audit.signal.decision_id),
        ).fetchall()
        if existing:
            if len(existing) != 1 or tuple(existing[0]) != values:
                raise ValueError(
                    "decision audit collides with different frozen decision data"
                )
            return
        connection.execute(
            """
            insert into signal_audit(
                symbol, created_at_ms, decision, direction, timeframe_minutes,
                threshold_segment, regime, score, threshold, reason, payload,
                record_version, decision_id, runtime_config_hash, event_kind,
                first_at_ms, last_at_ms, occurrences, score_min, score_max,
                aggregation_key
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )

    def _apply_open_credit(
        self,
        connection: sqlite3.Connection,
        order: SimulatedOrder,
        symbol: str,
        credit: StakeProgressionCredit | None,
    ) -> None:
        self._validate_open_credit(order, credit)
        if credit is None:
            return
        self._upsert_progression_credit(connection, symbol, credit)
        persisted = connection.execute(
            """
            select credit_id, status, consumed_order_id, consumed_at
            from stake_progression_credits
            where symbol = ? and version = ? and source_order_id = ?
            """,
            (symbol.upper(), credit.version, credit.source_order_id),
        ).fetchone()
        if (
            persisted is None
            or persisted["status"] != "CONSUMED"
            or persisted["consumed_order_id"] != credit.consumed_order_id
            or persisted["consumed_at"] != credit.consumed_at
            or persisted["credit_id"] != credit.credit_id
        ):
            raise ValueError("credit consumption conflicts with persisted terminal state")

    def save_open_order_decision(
        self,
        *,
        config: RuntimeConfigSnapshot,
        context: DecisionContext,
        order: SimulatedOrder,
        credit: StakeProgressionCredit | None,
        entry_snapshot: Mapping[str, Any],
        audit: DecisionAudit,
        observation: ObservationSignal | None = None,
    ) -> bool:
        self._validate_bundle_references(
            config,
            context,
            audit,
            order=order,
            entry_snapshot=entry_snapshot,
            observation=observation,
        )
        if not context.open_allowed:
            raise ValueError("open decision bundle requires open_allowed")
        if observation is not None and not context.observation_allowed:
            raise ValueError("observation requires observation_allowed")
        try:
            with self._connect() as connection:
                connection.execute("begin immediate")
                ensure_write_allowed(
                    capacity_from_connection(connection),
                    StorageWriteClass.CORE,
                )
                existing_order = connection.execute(
                    """
                    select order_id from orders
                    where symbol = ? and decision_id = ?
                    """,
                    (context.symbol, context.decision_id),
                ).fetchone()
                self._insert_runtime_config(connection, config)
                self._after_bundle_step("config")
                self._insert_decision_context(connection, context)
                self._after_bundle_step("context")
                self._insert_open_order(connection, order, context.symbol)
                self._after_bundle_step("order")
                self._apply_open_credit(connection, order, context.symbol, credit)
                self._after_bundle_step("credit")
                snapshot_changed = self._insert_order_entry_snapshot(
                    connection,
                    order,
                    context.symbol,
                    entry_snapshot,
                )
                if snapshot_changed:
                    revision = self._bump_profile_summary_revision(
                        connection,
                        context.symbol,
                    )
                    self._carry_profile_guard_materializations(
                        connection,
                        context.symbol,
                        revision,
                    )
                self._after_bundle_step("entry_snapshot")
                self._insert_individual_signal_audit(
                    connection,
                    context.symbol,
                    audit,
                )
                self._after_bundle_step("audit")
                if observation is not None:
                    self._insert_decision_observation(
                        connection,
                        observation,
                        context.symbol,
                    )
                self._after_bundle_step("observation")
        except sqlite3.Error as error:
            raise_for_sqlite_write_error(error, StorageWriteClass.CORE)
        if snapshot_changed:
            self._maintain_profile_summary_after_commit(context.symbol)
        return existing_order is None

    def save_decision_bundle(
        self,
        *,
        config: RuntimeConfigSnapshot,
        context: DecisionContext,
        audit: DecisionAudit,
        observation: ObservationSignal | None = None,
    ) -> bool:
        self._validate_bundle_references(
            config,
            context,
            audit,
            observation=observation,
        )
        if observation is not None and not context.observation_allowed:
            raise ValueError("observation requires observation_allowed")
        try:
            with self._connect() as connection:
                connection.execute("begin immediate")
                ensure_write_allowed(
                    capacity_from_connection(connection),
                    StorageWriteClass.CORE,
                )
                existing_context = connection.execute(
                    """
                    select 1 from decision_contexts
                    where symbol = ? and decision_id = ?
                    """,
                    (context.symbol, context.decision_id),
                ).fetchone()
                self._insert_runtime_config(connection, config)
                self._after_bundle_step("config")
                self._insert_decision_context(connection, context)
                self._after_bundle_step("context")
                self._insert_individual_signal_audit(
                    connection,
                    context.symbol,
                    audit,
                )
                self._after_bundle_step("audit")
                if observation is not None:
                    self._insert_decision_observation(
                        connection,
                        observation,
                        context.symbol,
                    )
                self._after_bundle_step("observation")
        except sqlite3.Error as error:
            raise_for_sqlite_write_error(error, StorageWriteClass.CORE)
        return existing_context is None

    def save_signal(
        self,
        symbol: str,
        signal: Signal,
        decision: str,
        created_at_ms: int,
        audit_context: dict[str, Any] | None = None,
        *,
        has_formal_candidate: bool = False,
        force_independent: bool = False,
        event_kind: str | None = None,
    ) -> bool:
        normalized_symbol = symbol.upper()
        normalized_decision = str(decision or "UNKNOWN").upper()
        normalized_audit = dict(audit_context or {})
        ordinary_heartbeat = (
            normalized_decision in _ORDINARY_SIGNAL_DECISIONS
            and not bool(has_formal_candidate)
            and not bool(force_independent)
        )
        reason_code = str(
            normalized_audit.get("reason_code")
            or signal.first_decisive_block
            or normalized_decision
        )
        aggregation_key = (
            _signal_audit_aggregation_key(
                normalized_symbol,
                signal,
                normalized_decision,
                created_at_ms,
                reason_code,
                normalized_audit,
            )
            if ordinary_heartbeat
            else None
        )
        normalized_event_kind = str(event_kind or "").strip().upper()
        if normalized_event_kind:
            resolved_event_kind = normalized_event_kind
        elif normalized_decision == "OPENED":
            resolved_event_kind = "ORDER_OPENED"
        elif ordinary_heartbeat:
            resolved_event_kind = "HEARTBEAT"
        elif normalized_decision.endswith("BLOCKED"):
            resolved_event_kind = "DECISIVE_BLOCK"
        else:
            resolved_event_kind = "DECISION"
        payload = _signal_audit_payload(
            signal,
            normalized_decision,
            normalized_audit,
            reason_code,
        )
        write_class = (
            StorageWriteClass.ORDINARY_AUDIT
            if ordinary_heartbeat
            else StorageWriteClass.CORE
        )
        try:
            with self._connect() as connection:
                connection.execute("begin immediate")
                if ordinary_heartbeat:
                    previous = connection.execute(
                        """
                        select payload
                        from signal_audit
                        where symbol = ? and record_version = ?
                        order by coalesce(last_at_ms, created_at_ms) desc, id desc
                        limit 1
                        """,
                        (normalized_symbol, SIGNAL_AUDIT_VERSION),
                    ).fetchone()
                    if previous is not None:
                        try:
                            previous_payload = json.loads(previous["payload"])
                        except (TypeError, json.JSONDecodeError):
                            previous_payload = {}
                        previous_state = previous_payload.get("state_code")
                        if (
                            previous_state is not None
                            and previous_state != payload["state_code"]
                        ):
                            ordinary_heartbeat = False
                            aggregation_key = None
                            resolved_event_kind = "STATE_CHANGE"
                            write_class = StorageWriteClass.CORE
                capacity = capacity_from_connection(connection)
                if ordinary_heartbeat and not capacity.ordinary_audit_allowed:
                    return False
                if not ordinary_heartbeat:
                    ensure_write_allowed(capacity, write_class)
                connection.execute(
                    """
                    insert into signal_audit(
                        symbol, created_at_ms, decision, direction, timeframe_minutes,
                        threshold_segment, regime, score, threshold, reason, payload,
                        record_version, decision_id, runtime_config_hash, event_kind,
                        first_at_ms, last_at_ms, occurrences, score_min, score_max,
                        aggregation_key
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    on conflict(symbol, aggregation_key)
                    where aggregation_key is not null
                    do update set
                        created_at_ms=excluded.created_at_ms,
                        decision=excluded.decision,
                        direction=excluded.direction,
                        timeframe_minutes=excluded.timeframe_minutes,
                        threshold_segment=excluded.threshold_segment,
                        regime=excluded.regime,
                        score=excluded.score,
                        threshold=excluded.threshold,
                        reason=excluded.reason,
                        payload=excluded.payload,
                        record_version=excluded.record_version,
                        decision_id=excluded.decision_id,
                        runtime_config_hash=excluded.runtime_config_hash,
                        event_kind=excluded.event_kind,
                        last_at_ms=excluded.last_at_ms,
                        occurrences=signal_audit.occurrences + 1,
                        score_min=min(signal_audit.score_min, excluded.score_min),
                        score_max=max(signal_audit.score_max, excluded.score_max)
                    """,
                    (
                        normalized_symbol,
                        int(created_at_ms),
                        normalized_decision,
                        signal.direction,
                        signal.timeframe_minutes,
                        signal.threshold_segment,
                        signal.regime,
                        signal.score,
                        signal.threshold,
                        signal.reason,
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        SIGNAL_AUDIT_VERSION,
                        signal.decision_id or None,
                        signal.runtime_config_hash or None,
                        resolved_event_kind,
                        int(created_at_ms),
                        int(created_at_ms),
                        signal.score,
                        signal.score,
                        aggregation_key,
                    ),
                )
        except sqlite3.Error as error:
            try:
                raise_for_sqlite_write_error(error, write_class)
            except OrdinaryAuditCapacityError:
                return False
        return True

    def save_order_entry_snapshot(self, order: SimulatedOrder, symbol: str, entry_snapshot: dict[str, Any]) -> None:
        with self._connect() as connection:
            changed = self._insert_order_entry_snapshot(
                connection,
                order,
                symbol,
                entry_snapshot,
            )
            if changed:
                revision = self._bump_profile_summary_revision(connection, symbol)
                self._carry_profile_guard_materializations(
                    connection,
                    symbol,
                    revision,
                )
        if changed:
            self._maintain_profile_summary_after_commit(symbol)

    @staticmethod
    def _upsert_order_entry_snapshot(
        connection: sqlite3.Connection,
        order: SimulatedOrder,
        symbol: str,
        entry_snapshot: Mapping[str, Any],
    ) -> None:
        values = SQLiteMonitorStore._order_entry_snapshot_values(
            order,
            symbol,
            entry_snapshot,
        )
        connection.execute(
                """
                insert into order_entry_snapshots(
                    symbol, order_id, direction, timeframe_minutes, opened_at, expires_at,
                    entry_price, stake, win_return, stake_progression_step,
                    threshold_segment, regime, score, threshold, edge,
                    result, settled_at, exit_price, pnl, entry_payload,
                    decision_id, context_version, runtime_config_hash
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(symbol, order_id) do update set
                    direction=excluded.direction,
                    timeframe_minutes=excluded.timeframe_minutes,
                    opened_at=excluded.opened_at,
                    expires_at=excluded.expires_at,
                    entry_price=excluded.entry_price,
                    stake=excluded.stake,
                    win_return=excluded.win_return,
                    stake_progression_step=excluded.stake_progression_step,
                    threshold_segment=excluded.threshold_segment,
                    regime=excluded.regime,
                    score=excluded.score,
                    threshold=excluded.threshold,
                    edge=excluded.edge,
                    entry_payload=excluded.entry_payload,
                    decision_id=excluded.decision_id,
                    context_version=excluded.context_version,
                    runtime_config_hash=excluded.runtime_config_hash,
                    updated_at_ms=strftime('%s','now') * 1000
                """,
                (
                    values["symbol"],
                    values["order_id"],
                    values["direction"],
                    values["timeframe_minutes"],
                    values["opened_at"],
                    values["expires_at"],
                    values["entry_price"],
                    values["stake"],
                    values["win_return"],
                    values["stake_progression_step"],
                    values["threshold_segment"],
                    values["regime"],
                    values["score"],
                    values["threshold"],
                    values["edge"],
                    values["result"],
                    values["settled_at"],
                    values["exit_price"],
                    values["pnl"],
                    values["entry_payload"],
                    values["decision_id"],
                    values["context_version"],
                    values["runtime_config_hash"],
                ),
            )

    @staticmethod
    def _order_entry_snapshot_values(
        order: SimulatedOrder,
        symbol: str,
        entry_snapshot: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "symbol": symbol.upper(),
            "order_id": order.id,
            "direction": order.direction,
            "timeframe_minutes": order.timeframe_minutes,
            "opened_at": order.opened_at,
            "expires_at": order.expires_at,
            "entry_price": order.entry_price,
            "stake": order.stake,
            "win_return": order.win_return,
            "stake_progression_step": order.stake_progression_step,
            "threshold_segment": order.threshold_segment,
            "regime": order.regime,
            "score": order.score,
            "threshold": order.threshold,
            "edge": round(abs(order.score) - order.threshold, 4),
            "result": order.result,
            "settled_at": order.settled_at,
            "exit_price": order.exit_price,
            "pnl": order.pnl,
            "entry_payload": json.dumps(entry_snapshot, ensure_ascii=False),
            "decision_id": order.decision_id or None,
            "context_version": order.context_version or None,
            "runtime_config_hash": order.runtime_config_hash or None,
        }

    def _insert_order_entry_snapshot(
        self,
        connection: sqlite3.Connection,
        order: SimulatedOrder,
        symbol: str,
        entry_snapshot: Mapping[str, Any],
    ) -> bool:
        values = self._order_entry_snapshot_values(order, symbol, entry_snapshot)
        frozen_columns = (
            "symbol",
            "order_id",
            "direction",
            "timeframe_minutes",
            "opened_at",
            "expires_at",
            "entry_price",
            "stake",
            "win_return",
            "stake_progression_step",
            "threshold_segment",
            "regime",
            "score",
            "threshold",
            "edge",
            "entry_payload",
            "decision_id",
            "context_version",
            "runtime_config_hash",
        )
        existing = connection.execute(
            f"select {', '.join(frozen_columns)} from order_entry_snapshots "
            "where symbol = ? and order_id = ?",
            (values["symbol"], values["order_id"]),
        ).fetchone()
        if existing is not None:
            if tuple(existing) != tuple(values[column] for column in frozen_columns):
                raise ValueError(
                    "entry snapshot collides with different frozen decision data"
                )
            return False
        self._upsert_order_entry_snapshot(
            connection,
            order,
            values["symbol"],
            entry_snapshot,
        )
        return True

    @staticmethod
    def _update_order_entry_snapshot_settlement(
        connection: sqlite3.Connection,
        order: SimulatedOrder,
        symbol: str,
    ) -> bool:
        settlement_payload = {
            "decision_id": order.decision_id,
            "status": order.status,
            "result": order.result,
            "settled_at": order.settled_at,
            "exit_price": order.exit_price,
            "pnl": order.pnl,
        }
        if order.decision_id:
            where_sql = "symbol = ? and decision_id = ?"
            identity = (symbol.upper(), order.decision_id)
        else:
            where_sql = "symbol = ? and order_id = ? and decision_id is null"
            identity = (symbol.upper(), order.id)
        existing = connection.execute(
            f"select result, settled_at, exit_price, pnl, settlement_payload "
            f"from order_entry_snapshots where {where_sql}",
            identity,
        ).fetchone()
        expected = (
            order.result,
            order.settled_at,
            order.exit_price,
            order.pnl,
            json.dumps(settlement_payload, ensure_ascii=False),
        )
        if existing is not None and tuple(existing) == expected:
            return False
        cursor = connection.execute(
            f"""
            update order_entry_snapshots
            set result = ?,
                settled_at = ?,
                exit_price = ?,
                pnl = ?,
                settlement_payload = ?,
                updated_at_ms = strftime('%s','now') * 1000
            where {where_sql}
            """,
            (
                order.result,
                order.settled_at,
                order.exit_price,
                order.pnl,
                json.dumps(settlement_payload, ensure_ascii=False),
                *identity,
            ),
        )
        if order.decision_id and cursor.rowcount != 1:
            raise ValueError(
                "settlement must match exactly one entry snapshot by decision_id"
            )
        return cursor.rowcount > 0

    def update_order_entry_snapshot_settlement(self, order: SimulatedOrder, symbol: str) -> None:
        with self._connect() as connection:
            changed = self._update_order_entry_snapshot_settlement(
                connection,
                order,
                symbol,
            )
            if changed:
                revision = self._bump_profile_summary_revision(connection, symbol)
                self._promote_profile_guard_settlement_branch(
                    connection,
                    symbol,
                    revision - 1,
                    revision,
                    order,
                )
        if changed:
            self._maintain_profile_summary_after_commit(symbol)

    def load_order_entry_snapshots(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select *
                from order_entry_snapshots
                where symbol = ?
                order by opened_at desc, order_id desc
                limit ?
                """,
                (symbol.upper(), limit),
            ).fetchall()
        return self._profile_snapshot_rows(rows)

    @staticmethod
    def _profile_snapshot_rows(rows: Sequence[sqlite3.Row]) -> list[dict[str, Any]]:
        result = []
        for row in rows:
            item = dict(row)
            item["entry_payload"] = json.loads(item["entry_payload"])
            item["settlement_payload"] = json.loads(item["settlement_payload"]) if item["settlement_payload"] else None
            result.append(item)
        return result

    def load_recent_signals(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select symbol, created_at_ms, decision, direction, timeframe_minutes,
                       threshold_segment, regime, score, threshold, reason,
                       record_version, decision_id, runtime_config_hash, event_kind,
                       first_at_ms, last_at_ms, occurrences, score_min, score_max,
                       aggregation_key
                from signal_audit
                where symbol = ?
                order by coalesce(last_at_ms, created_at_ms) desc, id desc
                limit ?
                """,
                (symbol.upper(), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def signal_audit_summary(self, symbol: str, limit: int = 5000) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select created_at_ms, decision, direction, timeframe_minutes,
                       threshold_segment, payload, occurrences,
                       coalesce(last_at_ms, created_at_ms) as effective_at_ms
                from signal_audit
                where symbol = ?
                order by coalesce(last_at_ms, created_at_ms) desc, id desc
                limit ?
                """,
                (symbol.upper(), max(1, int(limit))),
            ).fetchall()
        records = []
        for row in reversed(rows):
            payload = json.loads(row["payload"])
            audit = payload.get("audit_context") or payload.get("guards") or {}
            identity = payload.get("identity") or {}
            profile_key = str(
                payload.get("profile_key")
                or identity.get("profile_key")
                or ""
            )
            if not profile_key or len(profile_key.split("|")) == 3:
                profile_key = "|".join(
                    [
                        str(int(row["timeframe_minutes"] or 0)),
                        str(
                            identity.get("strategy_family")
                            or payload.get("strategy_family")
                            or "unknown"
                        ),
                        str(
                            identity.get("strategy_tag")
                            or payload.get("strategy_tag")
                            or "unknown"
                        ),
                        str(row["direction"] or "").upper(),
                        str(row["threshold_segment"] or "GLOBAL").upper(),
                    ]
                )
            records.append(
                {
                    "decision": str(row["decision"] or "UNKNOWN"),
                    "profile_context": "|".join(
                        [
                            profile_key,
                            str(
                                payload.get("daily_profile_version")
                                or identity.get("daily_profile_version")
                                or "STATIC"
                            ),
                            str(
                                payload.get("order_slot")
                                or identity.get("order_slot")
                                or "UNKNOWN"
                            ),
                        ]
                    ),
                    "result_sequence_status": str(
                        (
                            audit.get("result_sequence_guard")
                            or audit.get("result_sequence")
                            or {}
                        ).get("status")
                        or "UNKNOWN"
                    ),
                    "profile_degradation_status": str(
                        (
                            audit.get("profile_degradation_guard")
                            or audit.get("profile_degradation")
                            or {}
                        ).get("status")
                        or "UNKNOWN"
                    ),
                    "wave_batch_status": str(
                        (
                            audit.get("wave_batch_guard")
                            or audit.get("wave_batch")
                            or {}
                        ).get("status")
                        or (
                            audit.get("wave_batch_guard")
                            or audit.get("wave_batch")
                            or {}
                        ).get("code")
                        or "UNKNOWN"
                    ),
                    "rolling_edge_status": str(
                        (audit.get("rolling_edge") or {}).get("status")
                        or "UNKNOWN"
                    ),
                    "occurrences": int(row["occurrences"] or 1),
                }
            )
        return _summarize_signal_audit(records)

    @contextmanager
    def _connect(self, *, timeout: float = 5.0) -> Iterator[sqlite3.Connection]:
        normalized_timeout = max(0.0, float(timeout))
        connection = sqlite3.connect(self.path, timeout=normalized_timeout)
        try:
            connection.execute(
                f"pragma busy_timeout = {int(normalized_timeout * 1000)}"
            )
            connection.execute("pragma synchronous = normal")
            configure_max_page_count(connection)
            connection.row_factory = sqlite3.Row
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                create table if not exists orders (
                    symbol text not null,
                    order_id integer not null,
                    status text not null,
                    result text,
                    opened_at integer not null,
                    settled_at integer,
                    exit_price real,
                    pnl real not null default 0.0,
                    payload text not null,
                    updated_at_ms integer not null default (strftime('%s','now') * 1000),
                    primary key(symbol, order_id)
                )
                """
            )
            connection.execute(
                """
                create table if not exists stake_progression_runtime (
                    symbol text primary key,
                    version text not null,
                    activated_at integer not null,
                    enabled integer not null check(enabled in (0, 1)),
                    updated_at_ms integer not null default (strftime('%s','now') * 1000)
                )
                """
            )
            connection.execute(
                """
                create table if not exists wave_runtime (
                    symbol text primary key,
                    version text not null,
                    evaluated_at integer not null,
                    payload text not null,
                    updated_at_ms integer not null default (strftime('%s','now') * 1000)
                )
                """
            )
            connection.execute(
                """
                create table if not exists stake_progression_credits (
                    symbol text not null,
                    version text not null,
                    credit_id text not null,
                    source_order_id integer not null,
                    status text not null check(status in ('PENDING', 'CONSUMED', 'CANCELLED')),
                    created_at integer not null,
                    consumed_order_id integer,
                    consumed_at integer,
                    direction text not null default '',
                    updated_at_ms integer not null default (strftime('%s','now') * 1000),
                    primary key(symbol, version, source_order_id),
                    unique(symbol, version, credit_id),
                    unique(symbol, version, consumed_order_id)
                )
                """
            )
            credit_columns = {
                row["name"]
                for row in connection.execute(
                    "pragma table_info(stake_progression_credits)"
                ).fetchall()
            }
            if "direction" not in credit_columns:
                connection.execute(
                    "alter table stake_progression_credits "
                    "add column direction text not null default ''"
                )
            connection.execute(
                """
                create table if not exists signal_audit (
                    id integer primary key autoincrement,
                    symbol text not null,
                    created_at_ms integer not null,
                    decision text not null,
                    direction text not null,
                    timeframe_minutes integer not null,
                    threshold_segment text not null,
                    regime text not null,
                    score real not null,
                    threshold real not null,
                    reason text not null,
                    payload text not null
                )
                """
            )
            connection.execute("create index if not exists idx_signal_audit_symbol_time on signal_audit(symbol, created_at_ms)")
            connection.execute(
                """
                create table if not exists observation_signals (
                    symbol text not null,
                    observation_key text not null,
                    status text not null,
                    result text,
                    direction text not null,
                    strategy_family text not null,
                    strategy_tag text not null,
                    timeframe_minutes integer not null,
                    threshold_segment text not null,
                    opened_at integer not null,
                    expires_at integer not null,
                    settled_at integer,
                    exit_price real,
                    pnl real not null default 0.0,
                    payload text not null,
                    created_at_ms integer not null default (strftime('%s','now') * 1000),
                    updated_at_ms integer not null default (strftime('%s','now') * 1000),
                    primary key(symbol, observation_key)
                )
                """
            )
            connection.execute(
                "create index if not exists idx_observation_signals_symbol_opened on observation_signals(symbol, opened_at)"
            )
            connection.execute(
                "create index if not exists idx_observation_signals_symbol_result on observation_signals(symbol, result)"
            )
            connection.execute(
                "create index if not exists idx_observation_signals_symbol_family on observation_signals(symbol, strategy_family)"
            )
            connection.execute(
                "create index if not exists "
                "idx_observation_signals_symbol_status_settled "
                "on observation_signals(symbol, status, settled_at)"
            )
            connection.execute(
                """
                create table if not exists daily_profile_selections (
                    symbol text not null,
                    effective_from integer not null,
                    effective_until integer not null,
                    status text not null,
                    evaluated_at integer not null,
                    evaluation_key integer,
                    payload text not null,
                    updated_at_ms integer not null default (strftime('%s','now') * 1000),
                    primary key(symbol, effective_from)
                )
                """
            )
            daily_profile_columns = {
                row["name"]
                for row in connection.execute(
                    "pragma table_info(daily_profile_selections)"
                ).fetchall()
            }
            if "evaluation_key" not in daily_profile_columns:
                connection.execute(
                    "alter table daily_profile_selections add column evaluation_key integer"
                )
            connection.execute(
                """
                update daily_profile_selections
                set evaluation_key = case
                    when json_valid(payload) then coalesce(
                        cast(json_extract(payload, '$.evaluation_key') as integer),
                        cast(json_extract(payload, '$.lookback_end') as integer),
                        evaluated_at
                    )
                    else evaluated_at
                end
                where evaluation_key is null
                """
            )
            connection.execute(
                "create index if not exists idx_daily_profile_selections_symbol_effective "
                "on daily_profile_selections(symbol, effective_from, effective_until)"
            )
            connection.execute(
                "create index if not exists idx_daily_profile_selections_symbol_evaluation "
                "on daily_profile_selections(symbol, evaluation_key, evaluated_at)"
            )
            connection.execute(
                """
                create table if not exists order_entry_snapshots (
                    symbol text not null,
                    order_id integer not null,
                    direction text not null,
                    timeframe_minutes integer not null,
                    opened_at integer not null,
                    expires_at integer not null,
                    entry_price real not null,
                    stake real not null,
                    win_return real not null,
                    stake_progression_step integer not null,
                    threshold_segment text not null,
                    regime text not null,
                    score real not null,
                    threshold real not null,
                    edge real not null,
                    result text,
                    settled_at integer,
                    exit_price real,
                    pnl real not null default 0.0,
                    entry_payload text not null,
                    settlement_payload text,
                    created_at_ms integer not null default (strftime('%s','now') * 1000),
                    updated_at_ms integer not null default (strftime('%s','now') * 1000),
                    primary key(symbol, order_id)
                )
                """
            )
            connection.execute(
                "create index if not exists idx_order_entry_snapshots_symbol_opened on order_entry_snapshots(symbol, opened_at)"
            )
            connection.execute(
                "create index if not exists idx_order_entry_snapshots_symbol_result on order_entry_snapshots(symbol, result)"
            )
            migrate(connection)
            duplicates = connection.execute(
                """
                select symbol, decision_id, count(*) as total
                from orders
                where decision_id is not null
                group by symbol, decision_id
                having count(*) > 1
                limit 1
                """
            ).fetchone()
            if duplicates is not None:
                raise ValueError(
                    "legacy orders contain duplicate decision identities; "
                    "repair is required before migration"
                )
            connection.execute(
                """
                create unique index if not exists ux_orders_symbol_decision_id
                on orders(symbol, decision_id)
                where decision_id is not null
                """
            )
