from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


MINUTE_MS = 60_000


@dataclass(frozen=True)
class ResultSequenceGuardConfig:
    enabled: bool = True
    loss_streak: int = 3
    cooldown_minutes: int = 20
    scope: str = "DIRECTION"

    def normalized(self) -> "ResultSequenceGuardConfig":
        return ResultSequenceGuardConfig(
            enabled=bool(self.enabled),
            loss_streak=max(1, int(self.loss_streak)),
            cooldown_minutes=max(1, int(self.cooldown_minutes)),
            scope="DIRECTION" if str(self.scope).upper() == "DIRECTION" else "GLOBAL",
        )


@dataclass(frozen=True)
class ResultSequenceGuardDecision:
    blocked: bool = False
    reason: str = ""
    scope: str = "GLOBAL"
    direction: str = ""
    consecutive_losses: int = 0
    last_settled_at: int = 0
    pause_until: int = 0


def evaluate_result_sequence_guard(
    orders: Sequence[Any],
    *,
    current_time: int,
    direction: str,
    config: ResultSequenceGuardConfig | None = None,
) -> ResultSequenceGuardDecision:
    resolved = (config or ResultSequenceGuardConfig()).normalized()
    target_direction = str(direction).upper()
    if not resolved.enabled:
        return ResultSequenceGuardDecision(scope=resolved.scope, direction=target_direction)

    settled = [
        item
        for item in orders
        if _get(item, "result", None) in {"WIN", "LOSS"}
        and 0 < int(_get(item, "settled_at", 0) or 0) <= int(current_time)
        and (
            resolved.scope == "GLOBAL"
            or str(_get(item, "direction", "")).upper() == target_direction
        )
    ]
    settled.sort(
        key=lambda item: (
            int(_get(item, "settled_at", 0) or 0),
            int(_get(item, "order_id", _get(item, "id", 0)) or 0),
        )
    )

    consecutive_losses = 0
    for item in reversed(settled):
        if _get(item, "result", None) != "LOSS":
            break
        consecutive_losses += 1

    last_settled_at = int(_get(settled[-1], "settled_at", 0) or 0) if settled else 0
    pause_until = last_settled_at + resolved.cooldown_minutes * MINUTE_MS if last_settled_at else 0
    blocked = consecutive_losses >= resolved.loss_streak and int(current_time) < pause_until
    if not blocked:
        return ResultSequenceGuardDecision(
            scope=resolved.scope,
            direction=target_direction,
            consecutive_losses=consecutive_losses,
            last_settled_at=last_settled_at,
            pause_until=pause_until,
        )

    scope_label = "全局" if resolved.scope == "GLOBAL" else target_direction
    return ResultSequenceGuardDecision(
        blocked=True,
        reason=(
            f"结算序列守卫：{scope_label}连续亏损 {consecutive_losses} 单，"
            f"冷却至 {pause_until}"
        ),
        scope=resolved.scope,
        direction=target_direction,
        consecutive_losses=consecutive_losses,
        last_settled_at=last_settled_at,
        pause_until=pause_until,
    )


def _get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)
