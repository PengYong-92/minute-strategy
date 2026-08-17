import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from types import MappingProxyType


CONTEXT_VERSION = "DECISION_CONTEXT_V2"

_CREDENTIAL_KEYS = {
    "api_key",
    "api_secret",
    "webhook_token",
    "webhook_url",
}
_TRACE_KEYS = {
    "decisive_values",
    "reason_code",
    "result",
    "stage",
}


def _canonicalize(value: object, *, exclude_credentials: bool = False) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        value = {item.name: getattr(value, item.name) for item in fields(value)}

    if isinstance(value, Mapping):
        normalized = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("mapping keys must be strings")
            if exclude_credentials and key in _CREDENTIAL_KEYS:
                continue
            normalized[key] = _canonicalize(
                item,
                exclude_credentials=exclude_credentials,
            )
        return normalized

    if isinstance(value, (list, tuple)):
        return [
            _canonicalize(item, exclude_credentials=exclude_credentials)
            for item in value
        ]

    if isinstance(value, (set, frozenset)):
        normalized = [
            _canonicalize(item, exclude_credentials=exclude_credentials)
            for item in value
        ]
        return sorted(normalized, key=_canonical_json)

    if value is None or isinstance(value, (bool, int, float, str)):
        return value

    raise TypeError(f"unsupported value type: {type(value).__name__}")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _require_string(value: object, name: str, *, non_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if non_empty and not value:
        raise ValueError(f"{name} must not be empty")
    return value


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name} must be a bool")
    return value


def _normalize_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    normalized = _canonicalize(value)
    _canonical_json(normalized)
    frozen = _freeze(normalized)
    if not isinstance(frozen, Mapping):
        raise TypeError(f"{name} must be a mapping")
    return frozen


def _normalize_trace_record(record: object) -> Mapping[str, object]:
    if not isinstance(record, Mapping):
        raise TypeError("decision trace records must be mappings")
    if set(record) != _TRACE_KEYS:
        raise ValueError("decision trace records must contain the canonical fields")

    stage = _require_string(record["stage"], "stage", non_empty=True)
    result = _require_string(record["result"], "result", non_empty=True)
    reason_code = _require_string(record["reason_code"], "reason_code")
    decisive_values = _canonicalize(record["decisive_values"])
    _canonical_json(decisive_values)
    frozen = _freeze(
        {
            "stage": stage,
            "result": result,
            "decisive_values": decisive_values,
            "reason_code": reason_code,
        }
    )
    if not isinstance(frozen, Mapping):
        raise TypeError("decision trace records must be mappings")
    return frozen


@dataclass(frozen=True)
class RuntimeConfigSnapshot:
    hash: str
    canonical_payload: str
    strategy_build_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "hash": self.hash,
            "canonical_payload": self.canonical_payload,
            "strategy_build_id": self.strategy_build_id,
        }


@dataclass(frozen=True)
class DecisionContext:
    decision_id: str
    context_version: str
    runtime_config_hash: str
    strategy_build_id: str
    symbol: str
    closed_kline_at_ms: int
    candidate_origin: str
    inputs: Mapping[str, object]
    decision_trace: tuple[Mapping[str, object], ...]
    first_decisive_block: str
    final_decision: str
    final_reason: str
    open_allowed: bool
    observation_allowed: bool

    def __post_init__(self) -> None:
        for name in (
            "decision_id",
            "context_version",
            "runtime_config_hash",
            "strategy_build_id",
            "symbol",
            "candidate_origin",
        ):
            _require_string(getattr(self, name), name)
        if type(self.closed_kline_at_ms) is not int:
            raise TypeError("closed_kline_at_ms must be an int")

        normalized_inputs = _normalize_mapping(self.inputs, "inputs")
        if not isinstance(self.decision_trace, (list, tuple)):
            raise TypeError("decision_trace must be a list or tuple")
        normalized_trace = tuple(
            _normalize_trace_record(record) for record in self.decision_trace
        )
        first_decisive_block = _require_string(
            self.first_decisive_block,
            "first_decisive_block",
        )
        final_decision = _require_string(
            self.final_decision,
            "final_decision",
            non_empty=True,
        )
        final_reason = _require_string(self.final_reason, "final_reason")
        open_allowed = _require_bool(self.open_allowed, "open_allowed")
        observation_allowed = _require_bool(
            self.observation_allowed,
            "observation_allowed",
        )

        expected_first_block = next(
            (
                record["stage"]
                for record in normalized_trace
                if record["result"] == "BLOCK"
            ),
            "",
        )
        if first_decisive_block != expected_first_block:
            raise ValueError("first_decisive_block must match the first BLOCK stage")

        object.__setattr__(self, "inputs", normalized_inputs)
        object.__setattr__(self, "decision_trace", normalized_trace)
        object.__setattr__(self, "first_decisive_block", first_decisive_block)
        object.__setattr__(self, "final_decision", final_decision)
        object.__setattr__(self, "final_reason", final_reason)
        object.__setattr__(self, "open_allowed", open_allowed)
        object.__setattr__(self, "observation_allowed", observation_allowed)
        _canonical_json(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "context_version": self.context_version,
            "runtime_config_hash": self.runtime_config_hash,
            "strategy_build_id": self.strategy_build_id,
            "symbol": self.symbol,
            "closed_kline_at_ms": self.closed_kline_at_ms,
            "candidate_origin": self.candidate_origin,
            "inputs": _thaw(self.inputs),
            "decision_trace": _thaw(self.decision_trace),
            "first_decisive_block": self.first_decisive_block,
            "final_decision": self.final_decision,
            "final_reason": self.final_reason,
            "open_allowed": self.open_allowed,
            "observation_allowed": self.observation_allowed,
        }


def runtime_config_snapshot(
    config: Mapping[str, object],
    strategy_build_id: str = "UNKNOWN",
) -> RuntimeConfigSnapshot:
    normalized = _canonicalize(config, exclude_credentials=True)
    if not isinstance(normalized, dict):
        raise TypeError("config must be a mapping")
    canonical_payload = _canonical_json(normalized)
    config_hash = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
    return RuntimeConfigSnapshot(
        hash=config_hash,
        canonical_payload=canonical_payload,
        strategy_build_id=strategy_build_id,
    )


class DecisionContextBuilder:
    def __init__(
        self,
        *,
        decision_id: str,
        symbol: str,
        closed_kline_at_ms: int,
        candidate_origin: str,
        runtime_config_hash: str,
        strategy_build_id: str,
    ) -> None:
        self._decision_id = decision_id
        self._symbol = symbol
        self._closed_kline_at_ms = closed_kline_at_ms
        self._candidate_origin = candidate_origin
        self._runtime_config_hash = runtime_config_hash
        self._strategy_build_id = strategy_build_id
        self._inputs = None
        self._decision_trace = []
        self._first_decisive_block = ""
        self._finished = False

    @classmethod
    def new(
        cls,
        symbol: str,
        closed_kline_at_ms: int,
        candidate_origin: str,
        runtime_config_hash: str,
        *,
        strategy_build_id: str = "UNKNOWN",
        profile_key: str = "",
        candidate_ordinal: int = 0,
    ) -> "DecisionContextBuilder":
        normalized_symbol = symbol.upper()
        identity_payload = _canonical_json(
            {
                "candidate_ordinal": candidate_ordinal,
                "candidate_origin": candidate_origin,
                "closed_kline_at_ms": closed_kline_at_ms,
                "profile_key": profile_key,
                "runtime_config_hash": runtime_config_hash,
                "strategy_build_id": strategy_build_id,
                "symbol": normalized_symbol,
            }
        )
        decision_id = hashlib.sha256(identity_payload.encode("utf-8")).hexdigest()
        return cls(
            decision_id=decision_id,
            symbol=normalized_symbol,
            closed_kline_at_ms=closed_kline_at_ms,
            candidate_origin=candidate_origin,
            runtime_config_hash=runtime_config_hash,
            strategy_build_id=strategy_build_id,
        )

    def capture_inputs(self, mapping: Mapping[str, object]) -> None:
        if self._finished or self._inputs is not None:
            raise RuntimeError("inputs may be captured exactly once")
        self._inputs = _normalize_mapping(mapping, "inputs")

    def trace(
        self,
        stage: str,
        result: str,
        decisive_values: object = None,
        reason_code: str = "",
    ) -> None:
        if self._finished:
            raise RuntimeError("decision context is already finished")
        if self._inputs is None:
            raise RuntimeError("inputs must be captured before tracing")
        record = _normalize_trace_record(
            {
                "stage": stage,
                "result": result,
                "decisive_values": decisive_values,
                "reason_code": reason_code,
            }
        )
        self._decision_trace.append(record)
        if result == "BLOCK" and not self._first_decisive_block:
            self._first_decisive_block = stage

    def finish(
        self,
        final_decision: str,
        final_reason: str,
        open_allowed: bool,
        observation_allowed: bool,
    ) -> DecisionContext:
        if self._finished:
            raise RuntimeError("decision context may be finished exactly once")
        if self._inputs is None:
            raise RuntimeError("inputs must be captured before finish")
        final_decision = _require_string(
            final_decision,
            "final_decision",
            non_empty=True,
        )
        final_reason = _require_string(final_reason, "final_reason")
        open_allowed = _require_bool(open_allowed, "open_allowed")
        observation_allowed = _require_bool(
            observation_allowed,
            "observation_allowed",
        )
        context = DecisionContext(
            decision_id=self._decision_id,
            context_version=CONTEXT_VERSION,
            runtime_config_hash=self._runtime_config_hash,
            strategy_build_id=self._strategy_build_id,
            symbol=self._symbol,
            closed_kline_at_ms=self._closed_kline_at_ms,
            candidate_origin=self._candidate_origin,
            inputs=self._inputs,
            decision_trace=tuple(self._decision_trace),
            first_decisive_block=self._first_decisive_block,
            final_decision=final_decision,
            final_reason=final_reason,
            open_allowed=open_allowed,
            observation_allowed=observation_allowed,
        )
        self._finished = True
        return context


__all__ = [
    "CONTEXT_VERSION",
    "DecisionContext",
    "DecisionContextBuilder",
    "RuntimeConfigSnapshot",
    "runtime_config_snapshot",
]
