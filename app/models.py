from copy import deepcopy
from dataclasses import asdict, dataclass, field, fields
import hashlib
import json
from typing import Optional


_DECISION_CONTEXT_CORE_COMPAT_FIELDS = {
    "decision_inputs",
    "decision_trace",
    "first_decisive_block",
    "quality_score_inputs",
}

_DECISION_CONTEXT_EXTENDED_COMPAT_FIELDS = {
    "quality_score",
    "quality_score_version",
    "quality_score_mode",
    "quality_score_context",
    "quality_score_components",
    "adaptive_profile_state",
    "entry_structure_shadow",
}

_DECISION_LINKED_LIFECYCLE_FIELDS = {
    "id",
    "observation_key",
    "status",
    "result",
    "exit_price",
    "settled_at",
    "pnl",
}


def canonical_identity_hash(identity: dict[str, object]) -> str:
    payload = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def decision_context_reference(
    *,
    decision_id: str,
    context_version: str,
    runtime_config_hash: str,
    strategy_build_id: str,
    candidate_origin: str,
    identity: dict[str, object],
) -> dict[str, str]:
    return {
        "decision_id": str(decision_id),
        "context_version": str(context_version),
        "runtime_config_hash": str(runtime_config_hash),
        "strategy_build_id": str(strategy_build_id),
        "candidate_origin": str(candidate_origin),
        "canonical_identity_hash": canonical_identity_hash(identity),
    }


def bind_canonical_quality_score_inputs(model):
    decision_inputs = getattr(model, "decision_inputs", None)
    if not isinstance(decision_inputs, dict) or "identity" not in decision_inputs:
        return model
    score = decision_inputs.get("score")
    if not isinstance(score, dict):
        return model
    canonical = score.get("quality_score_inputs")
    if not isinstance(canonical, dict):
        legacy = getattr(model, "quality_score_inputs", None)
        canonical = legacy if isinstance(legacy, dict) else {}
        score["quality_score_inputs"] = canonical
    object.__setattr__(model, "quality_score_inputs", canonical)
    return model


def decision_linked_storage_payload(
    model,
    *,
    retain_extended_views: bool = False,
) -> dict[str, object]:
    decision_inputs = getattr(model, "decision_inputs", None)
    identity = (
        decision_inputs.get("identity")
        if isinstance(decision_inputs, dict)
        else None
    )
    linked = bool(
        getattr(model, "decision_id", "")
        and isinstance(decision_inputs, dict)
        and isinstance(identity, dict)
    )
    omitted = set(_DECISION_CONTEXT_CORE_COMPAT_FIELDS)
    if not retain_extended_views:
        omitted.update(_DECISION_CONTEXT_EXTENDED_COMPAT_FIELDS)
    fully_canonical_model = bool(
        linked
        and not retain_extended_views
        and isinstance(decision_inputs.get("signal"), dict)
        and (hasattr(model, "id") or hasattr(model, "observation_key"))
    )
    if fully_canonical_model:
        omitted.update(
            item.name
            for item in fields(model)
            if item.name not in _DECISION_LINKED_LIFECYCLE_FIELDS
        )
    payload = {
        item.name: deepcopy(getattr(model, item.name))
        for item in fields(model)
        if not (linked and item.name in omitted)
    }
    if linked:
        payload["decision_context_ref"] = decision_context_reference(
            decision_id=model.decision_id,
            context_version=model.context_version,
            runtime_config_hash=model.runtime_config_hash,
            strategy_build_id=model.strategy_build_id,
            candidate_origin=model.candidate_origin,
            identity=identity,
        )
    return payload


@dataclass(frozen=True)
class Kline:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FearGreedContext:
    value: int
    classification: str
    average_30d: float = 0.0
    trend: str = "unknown"
    updated_at_ms: int = 0
    source: str = "feargreed"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Signal:
    direction: str
    timeframe_minutes: int
    level: str
    reason: str
    price: float
    open_time: int
    volume_ratio: float = 0.0
    price_position: float = 0.5
    price_change_pct: float = 0.0
    score: float = 0.0
    threshold: float = 0.0
    volume_threshold: float = 1.5
    move_threshold_pct: float = 0.0
    close_strength: float = 0.5
    analysis_window_minutes: int = 0
    threshold_window_minutes: int = 0
    threshold_segment: str = "GLOBAL"
    mtf_10m_bias: float = 0.0
    mtf_30m_bias: float = 0.0
    macd_histogram: float = 0.0
    macd_histogram_delta: float = 0.0
    rsi: float = 50.0
    bollinger_position: float = 0.5
    bollinger_width: float = 0.0
    indicator_profile_segment: str = "GLOBAL"
    indicator_profile_sample_size: int = 0
    rsi_lower_threshold: float = 35.0
    rsi_upper_threshold: float = 70.0
    bollinger_lower_threshold: float = 0.35
    bollinger_upper_threshold: float = 0.85
    macd_histogram_threshold: float = 0.0
    macd_delta_threshold: float = 0.0
    fear_greed_value: Optional[int] = None
    fear_greed_classification: str = ""
    fear_greed_trend: str = ""
    fear_greed_average_30d: float = 0.0
    fear_greed_adjustment: float = 0.0
    session_allowed: bool = False
    session_sample_size: int = 0
    session_win_rate: float = 0.0
    session_ev: float = 0.0
    session_edge_min: float = 0.0
    regime: str = "UNKNOWN"
    risk_flags: str = ""
    strategy_family: str = "unknown"
    strategy_tag: str = "unknown"
    observe_direction: str = ""
    observe_only: bool = False
    profile_key: str = ""
    daily_profile_selected: bool = False
    daily_profile_version: str = ""
    order_slot: str = field(default="", kw_only=True)
    order_slot_scope: str = field(default="", kw_only=True)
    wave_state: str = "UNKNOWN"
    wave_raw_state: str = "UNKNOWN"
    wave_window: int = 0
    wave_efficiency: float = 0.0
    wave_direction_ratio: float = 0.0
    wave_atr_strength: float = 0.0
    wave_confirmations: int = 0
    wave_confirmed_at: int = 0
    wave_batch_id: str = ""
    wave_guard_mode: str = "NORMAL"
    wave_guard_status: str = "UNKNOWN"
    wave_guard_reason: str = ""
    calculated_threshold: float = 0.0
    quality_score: float = 0.0
    quality_score_version: str = ""
    quality_score_mode: str = ""
    quality_score_context: str = ""
    quality_score_components: dict[str, float] = field(default_factory=dict)
    quality_score_inputs: dict[str, object] = field(default_factory=dict)
    direction_pulse_shadow: dict[str, object] = field(default_factory=dict)
    profile_health_status: str = ""
    profile_health_sample_size: int = 0
    profile_health_win_rate: float = 0.0
    profile_health_ev: float = 0.0
    profile_health_evaluated_at: int = 0
    profile_degradation_probe: bool = False
    profile_degradation_triggered_at: int = 0
    decision_id: str = ""
    context_version: str = ""
    runtime_config_hash: str = ""
    strategy_build_id: str = ""
    candidate_origin: str = ""
    decision_inputs: dict[str, object] = field(default_factory=dict)
    decision_trace: list[dict[str, object]] = field(default_factory=list)
    first_decisive_block: str = ""
    adaptive_profile_state: dict[str, object] = field(default_factory=dict)
    entry_structure_shadow: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        bind_canonical_quality_score_inputs(self)

    @property
    def actionable(self) -> bool:
        return self.direction in {"LONG", "SHORT"} and (
            self.daily_profile_selected or abs(self.score) >= self.threshold
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SimulatedOrder:
    id: int
    direction: str
    timeframe_minutes: int
    level: str
    reason: str
    entry_price: float
    opened_at: int
    expires_at: int
    threshold_segment: str = "GLOBAL"
    score: float = 0.0
    threshold: float = 0.0
    session_allowed: bool = False
    session_sample_size: int = 0
    session_win_rate: float = 0.0
    session_ev: float = 0.0
    session_edge_min: float = 0.0
    regime: str = "UNKNOWN"
    strategy_family: str = "unknown"
    strategy_tag: str = "unknown"
    profile_key: str = ""
    daily_profile_selected: bool = False
    daily_profile_version: str = ""
    order_slot: str = field(default="", kw_only=True)
    order_slot_scope: str = field(default="", kw_only=True)
    stake: float = 10.0
    win_return: float = 18.0
    stake_progression_step: int = 1
    status: str = "OPEN"
    result: Optional[str] = None
    exit_price: Optional[float] = None
    settled_at: Optional[int] = None
    pnl: float = 0.0
    stake_progression_source_order_id: Optional[int] = None
    stake_progression_version: str = ""
    wave_state: str = "UNKNOWN"
    wave_raw_state: str = "UNKNOWN"
    wave_window: int = 0
    wave_efficiency: float = 0.0
    wave_direction_ratio: float = 0.0
    wave_atr_strength: float = 0.0
    wave_confirmations: int = 0
    wave_confirmed_at: int = 0
    wave_batch_id: str = ""
    wave_guard_mode: str = "NORMAL"
    wave_guard_status: str = "UNKNOWN"
    wave_guard_reason: str = ""
    calculated_threshold: float = 0.0
    quality_score: float = 0.0
    quality_score_version: str = ""
    quality_score_mode: str = ""
    quality_score_context: str = ""
    quality_score_components: dict[str, float] = field(default_factory=dict)
    quality_score_inputs: dict[str, object] = field(default_factory=dict)
    direction_pulse_shadow: dict[str, object] = field(default_factory=dict)
    profile_health_status: str = ""
    profile_health_sample_size: int = 0
    profile_health_win_rate: float = 0.0
    profile_health_ev: float = 0.0
    profile_health_evaluated_at: int = 0
    profile_degradation_probe: bool = False
    profile_degradation_triggered_at: int = 0
    decision_id: str = ""
    context_version: str = ""
    runtime_config_hash: str = ""
    strategy_build_id: str = ""
    candidate_origin: str = ""
    decision_inputs: dict[str, object] = field(default_factory=dict)
    decision_trace: list[dict[str, object]] = field(default_factory=list)
    first_decisive_block: str = ""
    adaptive_profile_state: dict[str, object] = field(default_factory=dict)
    entry_structure_shadow: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        bind_canonical_quality_score_inputs(self)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ObservationSignal:
    observation_key: str
    strategy_family: str
    strategy_tag: str
    direction: str
    timeframe_minutes: int
    level: str
    reason: str
    entry_price: float
    opened_at: int
    expires_at: int
    threshold_segment: str = "GLOBAL"
    score: float = 0.0
    threshold: float = 0.0
    edge: float = 0.0
    regime: str = "UNKNOWN"
    source_decision: str = ""
    observe_only: bool = True
    status: str = "OPEN"
    result: Optional[str] = None
    exit_price: Optional[float] = None
    settled_at: Optional[int] = None
    pnl: float = 0.0
    wave_state: str = "UNKNOWN"
    wave_raw_state: str = "UNKNOWN"
    wave_window: int = 0
    wave_efficiency: float = 0.0
    wave_direction_ratio: float = 0.0
    wave_atr_strength: float = 0.0
    wave_confirmations: int = 0
    wave_confirmed_at: int = 0
    wave_batch_id: str = ""
    wave_guard_mode: str = "NORMAL"
    wave_guard_status: str = "UNKNOWN"
    wave_guard_reason: str = ""
    profile_key: str = ""
    daily_profile_version: str = ""
    order_slot: str = field(default="", kw_only=True)
    order_slot_scope: str = field(default="", kw_only=True)
    quality_score: float = 0.0
    quality_score_version: str = ""
    quality_score_mode: str = ""
    quality_score_context: str = ""
    quality_score_components: dict[str, float] = field(default_factory=dict)
    quality_score_inputs: dict[str, object] = field(default_factory=dict)
    direction_pulse_shadow: dict[str, object] = field(default_factory=dict)
    profile_health_status: str = ""
    profile_health_sample_size: int = 0
    profile_health_win_rate: float = 0.0
    profile_health_ev: float = 0.0
    profile_health_evaluated_at: int = 0
    decision_id: str = ""
    context_version: str = ""
    runtime_config_hash: str = ""
    strategy_build_id: str = ""
    candidate_origin: str = ""
    decision_inputs: dict[str, object] = field(default_factory=dict)
    decision_trace: list[dict[str, object]] = field(default_factory=list)
    first_decisive_block: str = ""
    adaptive_profile_state: dict[str, object] = field(default_factory=dict)
    entry_structure_shadow: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        bind_canonical_quality_score_inputs(self)

    def to_dict(self) -> dict:
        return asdict(self)
