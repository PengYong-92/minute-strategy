"""Shadow evaluation for range-state entry policy."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass

from app.models import Signal


RANGE_POLICY_VERSION = "RANGE_POLICY_V1"
_VALID_MODES = {"SHADOW_ONLY", "LIVE"}
_VALID_LEVEL_KINDS = {"SUPPORT", "RESISTANCE", ""}
_DEFAULT_HIGH_STATES = ("RESISTANCE_REJECTED", "NO_NEARBY_LEVEL")


@dataclass(frozen=True)
class RangePolicyConfig:
    mode: str = "SHADOW_ONLY"
    version: str = RANGE_POLICY_VERSION
    mid_short_required_level_kind: str = "SUPPORT"
    high_allowed_structure_states: tuple[str, ...] = _DEFAULT_HIGH_STATES

    def __post_init__(self) -> None:
        mode = str(self.mode).upper()
        version = str(self.version).strip()
        level_kind = str(self.mid_short_required_level_kind).upper()
        states = tuple(str(item).upper() for item in self.high_allowed_structure_states)
        if mode not in _VALID_MODES:
            raise ValueError(f"unsupported range policy mode: {mode}")
        if not version:
            raise ValueError("range policy version must not be empty")
        if level_kind not in _VALID_LEVEL_KINDS:
            raise ValueError(f"unsupported mid short level kind: {level_kind}")
        if not states or any(not item for item in states):
            raise ValueError("high allowed structure states must not be empty")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "mid_short_required_level_kind", level_kind)
        object.__setattr__(self, "high_allowed_structure_states", states)

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["high_allowed_structure_states"] = list(
            self.high_allowed_structure_states
        )
        return payload


def _structure_value(structure: Mapping[str, object], *keys: str) -> str:
    for key in keys:
        value = structure.get(key)
        if value is not None and str(value).strip():
            return str(value).strip().upper()
    return ""


def evaluate_range_policy(
    signal: Signal,
    *,
    config: RangePolicyConfig | None = None,
) -> dict[str, object]:
    """Evaluate range policy without changing the supplied signal."""

    policy = config or RangePolicyConfig()
    direction = str(signal.direction or "").upper()
    wave_state = str(signal.wave_state or "").upper()
    structure = signal.entry_structure_shadow
    structure_map = structure if isinstance(structure, Mapping) else {}
    structure_state = _structure_value(
        structure_map,
        "entry_structure_state",
        "state",
    ) or "UNKNOWN"
    active_level_kind = _structure_value(structure_map, "active_level_kind")
    base = {
        "version": policy.version,
        "mode": policy.mode,
        "wave_state": wave_state,
        "direction": direction,
        "structure_state": structure_state,
        "active_level_kind": active_level_kind,
        "would_block": False,
        "allowed": True,
        "action": "UNCHANGED",
        "reason_code": "RANGE_POLICY_NOT_APPLICABLE",
        "reason": "非震荡区间状态，不适用范围策略",
        "config": policy.to_dict(),
    }

    if wave_state not in {"RANGE_MID", "RANGE_HIGH"} or direction not in {
        "LONG",
        "SHORT",
    }:
        return base

    should_allow = True
    reason_code = "RANGE_POLICY_ALLOWED"
    reason = "范围状态允许当前方向"
    if wave_state == "RANGE_MID":
        should_allow = direction == "LONG" or (
            direction == "SHORT"
            and active_level_kind == policy.mid_short_required_level_kind
        )
        if not should_allow:
            reason_code = "RANGE_MID_SHORT_NO_SUPPORT"
            reason = "RANGE_MID 下 SHORT 仅允许在 SUPPORT 结构开单"
    elif structure_state not in policy.high_allowed_structure_states:
        should_allow = False
        reason_code = "RANGE_HIGH_STRUCTURE_RISK"
        reason = "RANGE_HIGH 仅允许阻力拒绝或无邻近价位结构"

    base.update(
        {
            "would_block": not should_allow,
            "allowed": should_allow or policy.mode == "SHADOW_ONLY",
            "action": (
                "ALLOW"
                if should_allow
                else ("BLOCK" if policy.mode == "LIVE" else "RESTRICT")
            ),
            "reason_code": reason_code,
            "reason": reason,
        }
    )
    return base
