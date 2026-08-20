from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Sequence


PROFILE_ADMISSION_VERSION = "PROFILE_ADMISSION_V1"
_ADAPTIVE_STATES = {"WARMUP", "ACTIVE", "WATCH", "PAUSED"}
_ORDER_SLOTS = {"FIRST", "SECOND"}
_DIRECTIONS = {"LONG", "SHORT"}
_RESIDENT_QUALIFICATIONS = {"QUALIFIED", "QUALIFICATION_WATCH"}


def _normalized_values(values: Sequence[str], *, field_name: str) -> tuple[str, ...]:
    normalized = tuple(sorted({str(value).strip().upper() for value in values if str(value).strip()}))
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


@dataclass(frozen=True)
class ProfileAdmissionPolicy:
    version: str = PROFILE_ADMISSION_VERSION
    resident_allowed_states: tuple[str, ...] = ("ACTIVE", "WATCH")
    resident_n12_max_wins: int = 12
    resident_daily_win_rate_floor: float | None = None
    fast_enabled: bool = False
    fast_directions: tuple[str, ...] = ("SHORT",)
    fast_allowed_states: tuple[str, ...] = ("ACTIVE",)
    fast_n12_min_wins: int = 7
    fast_n12_max_wins: int = 8
    fast_n20_ev_min: float = 0.0
    fast_allow_second_order: bool = False
    fast_allow_progression: bool = False
    watch_allow_first_order: bool = True
    watch_allow_second_order: bool = False
    watch_allow_progression: bool = False

    def __post_init__(self) -> None:
        if self.version != PROFILE_ADMISSION_VERSION:
            raise ValueError(f"unsupported profile admission version: {self.version}")
        object.__setattr__(
            self,
            "resident_allowed_states",
            _normalized_values(
                self.resident_allowed_states,
                field_name="resident_allowed_states",
            ),
        )
        object.__setattr__(
            self,
            "fast_directions",
            _normalized_values(self.fast_directions, field_name="fast_directions"),
        )
        object.__setattr__(
            self,
            "fast_allowed_states",
            _normalized_values(
                self.fast_allowed_states,
                field_name="fast_allowed_states",
            ),
        )
        unknown_resident = set(self.resident_allowed_states) - _ADAPTIVE_STATES
        unknown_fast = set(self.fast_allowed_states) - _ADAPTIVE_STATES
        if unknown_resident:
            raise ValueError(f"unknown resident states: {sorted(unknown_resident)}")
        if unknown_fast:
            raise ValueError(f"unknown fast states: {sorted(unknown_fast)}")
        if set(self.fast_directions) - {"SHORT"}:
            raise ValueError("fast_directions only supports SHORT")
        if set(self.fast_allowed_states) - {"ACTIVE"}:
            raise ValueError("fast_allowed_states only supports ACTIVE")
        for name in (
            "fast_enabled",
            "fast_allow_second_order",
            "fast_allow_progression",
            "watch_allow_first_order",
            "watch_allow_second_order",
            "watch_allow_progression",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        if self.fast_allow_second_order or self.fast_allow_progression:
            raise ValueError("FAST must not allow second orders or progression")
        if self.watch_allow_second_order or self.watch_allow_progression:
            raise ValueError("WATCH must not allow second orders or progression")
        if type(self.resident_n12_max_wins) is not int or not 0 <= self.resident_n12_max_wins <= 12:
            raise ValueError("resident_n12_max_wins must be between 0 and 12")
        if (
            type(self.fast_n12_min_wins) is not int
            or type(self.fast_n12_max_wins) is not int
            or not 0 <= self.fast_n12_min_wins <= self.fast_n12_max_wins <= 12
        ):
            raise ValueError("fast_n12 bounds must satisfy 0 <= min <= max <= 12")
        if not math.isfinite(float(self.fast_n20_ev_min)):
            raise ValueError("fast_n20_ev_min must be finite")
        if self.resident_daily_win_rate_floor is not None:
            floor = float(self.resident_daily_win_rate_floor)
            if not math.isfinite(floor) or not 0.0 <= floor <= 1.0:
                raise ValueError("resident_daily_win_rate_floor must be between 0 and 1")
            object.__setattr__(
                self,
                "resident_daily_win_rate_floor",
                0.0 if floor == 0.0 else floor,
            )
        fast_n20_ev_min = float(self.fast_n20_ev_min)
        object.__setattr__(
            self,
            "fast_n20_ev_min",
            0.0 if fast_n20_ev_min == 0.0 else fast_n20_ev_min,
        )

    def to_dict(self) -> dict:
        payload = asdict(self)
        for name in (
            "resident_allowed_states",
            "fast_directions",
            "fast_allowed_states",
        ):
            payload[name] = list(payload[name])
        return payload

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def policy_hash(self) -> str:
        return hashlib.sha256(self.to_json().encode("ascii")).hexdigest()

    @property
    def complexity(self) -> int:
        return int(self.fast_enabled) + int(self.resident_daily_win_rate_floor is not None)


@dataclass(frozen=True)
class ProfileAdmissionContext:
    profile_key: str
    direction: str
    order_slot: str
    daily_selected: bool
    qualification_state: str
    daily_rank: int | None
    daily_win_rate: float
    adaptive_state: str
    adaptive_transition: str
    adaptive_evaluated_at: int
    n12_sample_size: int
    n12_wins: int
    n20_sample_size: int
    n20_ev: float
    candidate_origin: str
    candidate_ordinal: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "direction", str(self.direction).upper())
        object.__setattr__(self, "order_slot", str(self.order_slot).upper())
        object.__setattr__(self, "adaptive_state", str(self.adaptive_state).upper())
        object.__setattr__(
            self,
            "qualification_state",
            str(self.qualification_state).upper(),
        )
        object.__setattr__(
            self,
            "candidate_origin",
            str(self.candidate_origin).upper(),
        )
        if not self.profile_key:
            raise ValueError("profile_key must not be empty")
        if self.order_slot not in _ORDER_SLOTS:
            raise ValueError(f"unknown order_slot: {self.order_slot}")
        if self.direction not in _DIRECTIONS:
            raise ValueError(f"unknown direction: {self.direction}")
        if self.adaptive_state not in _ADAPTIVE_STATES:
            raise ValueError(f"unknown adaptive_state: {self.adaptive_state}")
        if type(self.daily_selected) is not bool:
            raise ValueError("daily_selected must be a boolean")
        if self.daily_rank is not None and (type(self.daily_rank) is not int or self.daily_rank <= 0):
            raise ValueError("daily_rank must be a positive integer")
        daily_win_rate = float(self.daily_win_rate)
        if not math.isfinite(daily_win_rate) or not 0.0 <= daily_win_rate <= 1.0:
            raise ValueError("daily_win_rate must be between 0 and 1")
        object.__setattr__(
            self,
            "daily_win_rate",
            0.0 if daily_win_rate == 0.0 else daily_win_rate,
        )
        for name in ("n12_sample_size", "n12_wins", "n20_sample_size"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.n12_sample_size > 12 or self.n12_wins > self.n12_sample_size:
            raise ValueError("n12_wins must fit n12_sample_size <= 12")
        if self.n20_sample_size > 20:
            raise ValueError("n20_sample_size must not exceed 20")
        n20_ev = float(self.n20_ev)
        if not math.isfinite(n20_ev):
            raise ValueError("n20_ev must be finite")
        object.__setattr__(self, "n20_ev", 0.0 if n20_ev == 0.0 else n20_ev)
        if type(self.adaptive_evaluated_at) is not int or self.adaptive_evaluated_at < 0:
            raise ValueError("adaptive_evaluated_at must be a non-negative integer")
        if type(self.candidate_ordinal) is not int or self.candidate_ordinal < 0:
            raise ValueError("candidate_ordinal must be a non-negative integer")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ProfileAdmissionDecision:
    allowed: bool
    channel: str
    code: str
    allow_second_order: bool
    allow_progression: bool
    policy_version: str
    policy_hash: str
    rank_key: tuple

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["rank_key"] = list(self.rank_key)
        return payload


@dataclass(frozen=True)
class RankedAdmission:
    context: ProfileAdmissionContext
    decision: ProfileAdmissionDecision


def baseline_policy() -> ProfileAdmissionPolicy:
    return ProfileAdmissionPolicy(
        resident_allowed_states=("ACTIVE", "WATCH", "WARMUP"),
        resident_n12_max_wins=12,
        fast_enabled=False,
    )


def candidate_policy() -> ProfileAdmissionPolicy:
    return ProfileAdmissionPolicy(
        resident_allowed_states=("ACTIVE", "WATCH"),
        resident_n12_max_wins=8,
        resident_daily_win_rate_floor=None,
        fast_enabled=True,
        fast_directions=("SHORT",),
        fast_allowed_states=("ACTIVE",),
        fast_n12_min_wins=7,
        fast_n12_max_wins=8,
        fast_n20_ev_min=0.0,
    )


def policy_grid() -> tuple[ProfileAdmissionPolicy, ...]:
    return tuple(
        ProfileAdmissionPolicy(
            resident_allowed_states=("ACTIVE", "WATCH"),
            resident_n12_max_wins=resident_max,
            resident_daily_win_rate_floor=daily_floor,
            fast_enabled=True,
            fast_directions=("SHORT",),
            fast_allowed_states=("ACTIVE",),
            fast_n12_min_wins=7,
            fast_n12_max_wins=fast_max,
            fast_n20_ev_min=0.0,
        )
        for resident_max in (7, 8, 9, 12)
        for daily_floor in (None, 0.55, 0.60, 0.65)
        for fast_max in (7, 8)
    )


def _rank_key(context: ProfileAdmissionContext, channel: str) -> tuple:
    return (
        0 if channel == "RESIDENT" else 1,
        context.daily_rank if context.daily_rank is not None else 1_000_000,
        context.profile_key,
        context.candidate_origin,
        context.candidate_ordinal,
    )


def _decision(
    context: ProfileAdmissionContext,
    policy: ProfileAdmissionPolicy,
    *,
    allowed: bool,
    channel: str,
    code: str,
    allow_second_order: bool = False,
    allow_progression: bool = False,
) -> ProfileAdmissionDecision:
    return ProfileAdmissionDecision(
        allowed=allowed,
        channel=channel,
        code=code,
        allow_second_order=allow_second_order,
        allow_progression=allow_progression,
        policy_version=policy.version,
        policy_hash=policy.policy_hash,
        rank_key=_rank_key(context, channel if allowed else "NONE"),
    )


def evaluate_profile_admission(
    context: ProfileAdmissionContext,
    policy: ProfileAdmissionPolicy,
) -> ProfileAdmissionDecision:
    state = context.adaptive_state
    if state == "PAUSED":
        return _decision(
            context,
            policy,
            allowed=False,
            channel="NONE",
            code="ADAPTIVE_PAUSED",
        )

    if context.daily_selected:
        if context.qualification_state not in _RESIDENT_QUALIFICATIONS:
            return _decision(
                context,
                policy,
                allowed=False,
                channel="NONE",
                code="RESIDENT_QUALIFICATION_BLOCKED",
            )
        if state not in policy.resident_allowed_states:
            return _decision(
                context,
                policy,
                allowed=False,
                channel="NONE",
                code="RESIDENT_STATE_BLOCKED",
            )
        if context.n12_wins > policy.resident_n12_max_wins:
            return _decision(
                context,
                policy,
                allowed=False,
                channel="NONE",
                code="RESIDENT_N12_OVERHEATED",
            )
        if (
            policy.resident_daily_win_rate_floor is not None
            and context.daily_win_rate < policy.resident_daily_win_rate_floor
        ):
            return _decision(
                context,
                policy,
                allowed=False,
                channel="NONE",
                code="RESIDENT_DAILY_WIN_RATE_BLOCKED",
            )
        if state == "WATCH":
            if context.order_slot == "FIRST" and not policy.watch_allow_first_order:
                return _decision(
                    context,
                    policy,
                    allowed=False,
                    channel="NONE",
                    code="WATCH_FIRST_ORDER_BLOCKED",
                )
            if context.order_slot == "SECOND" and not policy.watch_allow_second_order:
                return _decision(
                    context,
                    policy,
                    allowed=False,
                    channel="NONE",
                    code="WATCH_SECOND_ORDER_BLOCKED",
                )
            return _decision(
                context,
                policy,
                allowed=True,
                channel="RESIDENT",
                code="RESIDENT_WATCH_ADMITTED",
                allow_second_order=False,
                allow_progression=False,
            )
        return _decision(
            context,
            policy,
            allowed=True,
            channel="RESIDENT",
            code="RESIDENT_ADMITTED",
            allow_second_order=True,
            allow_progression=True,
        )

    if not policy.fast_enabled:
        return _decision(
            context,
            policy,
            allowed=False,
            channel="NONE",
            code="DAILY_PROFILE_NOT_SELECTED",
        )
    if context.direction != "SHORT" or context.direction not in policy.fast_directions:
        return _decision(
            context,
            policy,
            allowed=False,
            channel="NONE",
            code="FAST_DIRECTION_BLOCKED",
        )
    if state in {"WARMUP", "WATCH"} or state not in policy.fast_allowed_states:
        return _decision(
            context,
            policy,
            allowed=False,
            channel="NONE",
            code="FAST_STATE_BLOCKED",
        )
    if not policy.fast_n12_min_wins <= context.n12_wins <= policy.fast_n12_max_wins:
        return _decision(
            context,
            policy,
            allowed=False,
            channel="NONE",
            code="FAST_N12_BLOCKED",
        )
    if context.n20_ev < policy.fast_n20_ev_min:
        return _decision(
            context,
            policy,
            allowed=False,
            channel="NONE",
            code="FAST_N20_EV_BLOCKED",
        )
    if context.order_slot == "SECOND":
        return _decision(
            context,
            policy,
            allowed=False,
            channel="NONE",
            code="FAST_SECOND_ORDER_BLOCKED",
        )
    return _decision(
        context,
        policy,
        allowed=True,
        channel="FAST",
        code="FAST_ADMITTED",
        allow_second_order=False,
        allow_progression=False,
    )


def rank_admitted_candidates(
    contexts: Sequence[ProfileAdmissionContext],
    policy: ProfileAdmissionPolicy,
) -> tuple[RankedAdmission, ...]:
    admitted = [
        RankedAdmission(context=context, decision=decision)
        for context in contexts
        if (decision := evaluate_profile_admission(context, policy)).allowed
    ]
    admitted.sort(key=lambda item: item.decision.rank_key)
    return tuple(admitted)


def select_admitted_candidate(
    contexts: Sequence[ProfileAdmissionContext],
    policy: ProfileAdmissionPolicy,
) -> RankedAdmission | None:
    ranked = rank_admitted_candidates(contexts, policy)
    return ranked[0] if ranked else None
