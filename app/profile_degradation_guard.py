from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


PROFILE_DEGRADATION_LOSS_STREAK = 3
MINUTE_MS = 60_000


@dataclass(frozen=True)
class ProfileDegradationGuardConfig:
    cooldown_minutes: int = 60

    def normalized(self) -> "ProfileDegradationGuardConfig":
        return ProfileDegradationGuardConfig(
            cooldown_minutes=max(0, int(self.cooldown_minutes))
        )


@dataclass(frozen=True)
class ProfileDegradationGuardDecision:
    status: str = "NORMAL"
    blocked: bool = False
    allow_progression: bool = True
    profile_key: str = ""
    daily_profile_version: str = ""
    consecutive_losses: int = 0
    last_loss_settled_at: int = 0
    pause_until: int = 0
    probe_order_id: int = 0
    triggered_at: int = 0
    reason: str = ""


@dataclass(frozen=True)
class _SettledOrder:
    id: int
    result: str
    settled_at: int


@dataclass(frozen=True)
class _OpenProbe:
    id: int
    opened_at: int
    triggered_at: int


def evaluate_profile_degradation_guard(
    orders: Sequence[Any],
    *,
    current_time: int,
    profile_key: str,
    daily_profile_version: str,
    config: ProfileDegradationGuardConfig | None = None,
) -> ProfileDegradationGuardDecision:
    resolved = (config or ProfileDegradationGuardConfig()).normalized()
    target_profile = str(profile_key or "")
    target_version = str(daily_profile_version or "")
    scope = {
        "profile_key": target_profile,
        "daily_profile_version": target_version,
    }

    if resolved.cooldown_minutes == 0:
        return ProfileDegradationGuardDecision(
            status="DISABLED",
            reason="Profile degradation guard is disabled",
            **scope,
        )
    if not target_profile or not target_version:
        return ProfileDegradationGuardDecision(
            status="NOT_APPLICABLE",
            reason="A complete profile key and version are required",
            **scope,
        )

    now = int(current_time)
    settled = _parse_settled_orders(
        orders,
        current_time=now,
        profile_key=target_profile,
        daily_profile_version=target_version,
    )

    consecutive_losses = 0
    for item in reversed(settled):
        if item.result != "LOSS":
            break
        consecutive_losses += 1

    last_loss_settled_at = settled[-1].settled_at if consecutive_losses else 0
    streak = {
        "consecutive_losses": consecutive_losses,
        "last_loss_settled_at": last_loss_settled_at,
    }
    if consecutive_losses < PROFILE_DEGRADATION_LOSS_STREAK:
        return ProfileDegradationGuardDecision(
            reason="Profile loss streak is below the trigger",
            **scope,
            **streak,
        )

    triggered_at = last_loss_settled_at
    pause_until = triggered_at + resolved.cooldown_minutes * MINUTE_MS
    open_probes = _parse_open_probes(
        orders,
        triggered_at=triggered_at,
        profile_key=target_profile,
        daily_profile_version=target_version,
    )
    if open_probes:
        probe = min(open_probes, key=lambda item: (item.opened_at, item.id))
        return ProfileDegradationGuardDecision(
            status="RECOVERY_PENDING",
            blocked=True,
            allow_progression=False,
            pause_until=pause_until,
            probe_order_id=probe.id,
            triggered_at=triggered_at,
            reason="The recovery probe is still open",
            **scope,
            **streak,
        )

    if now < pause_until:
        return ProfileDegradationGuardDecision(
            status="COOLDOWN",
            blocked=True,
            allow_progression=False,
            pause_until=pause_until,
            triggered_at=triggered_at,
            reason="Three trailing profile losses triggered cooldown",
            **scope,
            **streak,
        )

    return ProfileDegradationGuardDecision(
        status="RECOVERY_READY",
        allow_progression=False,
        pause_until=pause_until,
        triggered_at=triggered_at,
        reason="Cooldown completed; one base-stake probe is allowed",
        **scope,
        **streak,
    )


def _get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _parse_settled_orders(
    orders: Sequence[Any],
    *,
    current_time: int,
    profile_key: str,
    daily_profile_version: str,
) -> list[_SettledOrder]:
    parsed = []
    for item in orders:
        result = _get(item, "result", None)
        if (
            _get(item, "status", None) != "SETTLED"
            or result not in {"WIN", "LOSS"}
            or _get(item, "profile_key", "") != profile_key
            or _get(item, "daily_profile_version", "")
            != daily_profile_version
        ):
            continue
        order_id = _parse_int(_get(item, "id", None))
        settled_at = _parse_int(_get(item, "settled_at", None))
        if order_id is None or settled_at is None or settled_at > current_time:
            continue
        parsed.append(
            _SettledOrder(
                id=order_id,
                result=result,
                settled_at=settled_at,
            )
        )
    return sorted(parsed, key=lambda item: (item.settled_at, item.id))


def _parse_open_probes(
    orders: Sequence[Any],
    *,
    triggered_at: int,
    profile_key: str,
    daily_profile_version: str,
) -> list[_OpenProbe]:
    parsed = []
    for item in orders:
        if (
            _get(item, "status", None) != "OPEN"
            or _get(item, "profile_degradation_probe", False) is not True
            or _get(item, "profile_key", "") != profile_key
            or _get(item, "daily_profile_version", "")
            != daily_profile_version
        ):
            continue
        order_id = _parse_int(_get(item, "id", None))
        opened_at = _parse_int(_get(item, "opened_at", None))
        probe_triggered_at = _parse_int(
            _get(item, "profile_degradation_triggered_at", None)
        )
        if (
            order_id is None
            or opened_at is None
            or probe_triggered_at is None
            or probe_triggered_at != triggered_at
        ):
            continue
        parsed.append(
            _OpenProbe(
                id=order_id,
                opened_at=opened_at,
                triggered_at=probe_triggered_at,
            )
        )
    return parsed


def _parse_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None
