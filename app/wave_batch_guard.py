from dataclasses import dataclass
from typing import Sequence

from app.models import SimulatedOrder


MINUTE_MS = 60_000


@dataclass(frozen=True)
class WaveBatchGuardConfig:
    enabled: bool = True
    batch_size: int = 2
    failed_batches_for_cooldown: int = 2
    failed_batch_window_ms: int = 60 * MINUTE_MS
    cooldown_ms: int = 60 * MINUTE_MS

    def normalized(self) -> "WaveBatchGuardConfig":
        return WaveBatchGuardConfig(
            enabled=bool(self.enabled),
            batch_size=max(1, int(self.batch_size)),
            failed_batches_for_cooldown=max(2, int(self.failed_batches_for_cooldown)),
            failed_batch_window_ms=max(0, int(self.failed_batch_window_ms)),
            cooldown_ms=max(0, int(self.cooldown_ms)),
        )


@dataclass(frozen=True)
class WaveBatchGuardDecision:
    code: str
    mode: str
    blocked: bool
    allow_progression: bool
    current_batch_id: str
    batch_orders: int
    batch_wins: int
    batch_losses: int
    failed_batches: int
    pause_until: int
    reason: str


def evaluate_wave_batch_guard(
    orders: Sequence[SimulatedOrder],
    current_time: int,
    current_batch_id: str,
    config: WaveBatchGuardConfig | None = None,
) -> WaveBatchGuardDecision:
    normalized = (config or WaveBatchGuardConfig()).normalized()
    ordered = sorted(orders, key=lambda item: (item.opened_at, item.id))
    if not normalized.enabled:
        return _decision(
            "WAVE_BATCH_GUARD_DISABLED",
            "DISABLED",
            False,
            True,
            current_batch_id,
            reason="波段批次守卫已关闭",
        )

    failed = _failed_batches(ordered, normalized.batch_size)
    trigger_at = _latest_cooldown_trigger(failed, normalized)
    if trigger_at:
        recovery = _recovery_decision(
            ordered,
            int(current_time),
            trigger_at,
            current_batch_id,
            len(failed),
            normalized,
        )
        if recovery is not None:
            return recovery

    if not current_batch_id:
        return _decision(
            "WAVE_BATCH_UNAVAILABLE",
            "NORMAL",
            False,
            True,
            current_batch_id,
            failed_batches=len(failed),
            reason="当前信号没有波段批次，仅保留兼容放行",
        )

    current_orders = [
        item
        for item in ordered
        if item.wave_batch_id == current_batch_id
        and item.wave_guard_mode != "RECOVERY"
    ]
    settled = [item for item in current_orders if item.status == "SETTLED"]
    wins = sum(1 for item in settled if item.result == "WIN")
    losses = sum(1 for item in settled if item.result == "LOSS")
    common = {
        "current_batch_id": current_batch_id,
        "batch_orders": len(current_orders),
        "batch_wins": wins,
        "batch_losses": losses,
        "failed_batches": len(failed),
    }
    if losses:
        return _decision(
            "WAVE_BATCH_LOSS_LOCKED",
            "BATCH_LOCKED",
            True,
            False,
            reason=f"当前波段批次已出现 {losses} 笔亏损，等待新波段",
            **common,
        )
    if len(current_orders) >= normalized.batch_size:
        return _decision(
            "WAVE_BATCH_FULL",
            "BATCH_FULL",
            True,
            False,
            reason=f"当前波段批次已达到 {normalized.batch_size} 笔上限",
            **common,
        )
    return _decision(
        "WAVE_BATCH_NORMAL",
        "NORMAL",
        False,
        True,
        reason="当前波段批次允许开单",
        **common,
    )


def _failed_batches(
    orders: Sequence[SimulatedOrder],
    batch_size: int,
) -> list[tuple[str, int]]:
    grouped: dict[str, list[SimulatedOrder]] = {}
    for item in orders:
        if not item.wave_batch_id or item.wave_guard_mode == "RECOVERY":
            continue
        grouped.setdefault(item.wave_batch_id, []).append(item)

    failed = []
    for batch_id, batch_orders in grouped.items():
        settled = sorted(
            (item for item in batch_orders if item.status == "SETTLED"),
            key=lambda item: (item.settled_at or 0, item.id),
        )
        first_batch = settled[:batch_size]
        if len(first_batch) < batch_size:
            continue
        if all(item.result == "LOSS" for item in first_batch):
            failed.append((batch_id, max(int(item.settled_at or 0) for item in first_batch)))
    return sorted(failed, key=lambda item: (item[1], item[0]))


def _latest_cooldown_trigger(
    failed: Sequence[tuple[str, int]],
    config: WaveBatchGuardConfig,
) -> int:
    required = config.failed_batches_for_cooldown
    if len(failed) < required:
        return 0
    latest_trigger = 0
    for index in range(required - 1, len(failed)):
        window = failed[index - required + 1 : index + 1]
        if window[-1][1] - window[0][1] <= config.failed_batch_window_ms:
            latest_trigger = max(latest_trigger, window[-1][1])
    return latest_trigger


def _recovery_decision(
    orders: Sequence[SimulatedOrder],
    current_time: int,
    trigger_at: int,
    current_batch_id: str,
    failed_batches: int,
    config: WaveBatchGuardConfig,
) -> WaveBatchGuardDecision | None:
    initial_pause_until = trigger_at + config.cooldown_ms
    recovery_orders = [
        item
        for item in orders
        if item.wave_guard_mode == "RECOVERY" and item.opened_at >= initial_pause_until
    ]
    if not recovery_orders:
        if current_time < initial_pause_until:
            return _cooldown_decision(
                current_batch_id, failed_batches, initial_pause_until, "两个全亏波段批次触发冷却"
            )
        return _decision(
            "WAVE_RECOVERY_READY",
            "RECOVERY",
            False,
            False,
            current_batch_id,
            failed_batches=failed_batches,
            pause_until=initial_pause_until,
            reason="冷却结束，仅允许一笔固定基础金额恢复单",
        )

    latest = max(recovery_orders, key=lambda item: (item.opened_at, item.id))
    if latest.status == "OPEN":
        return _decision(
            "WAVE_RECOVERY_PENDING",
            "RECOVERY_PENDING",
            True,
            False,
            current_batch_id,
            failed_batches=failed_batches,
            pause_until=initial_pause_until,
            reason="恢复单尚未结算，暂停新单",
        )
    if latest.result == "WIN":
        return None

    pause_until = int(latest.settled_at or latest.expires_at) + config.cooldown_ms
    if current_time < pause_until:
        return _cooldown_decision(
            current_batch_id, failed_batches, pause_until, "恢复单亏损，重新进入冷却"
        )
    return _decision(
        "WAVE_RECOVERY_READY",
        "RECOVERY",
        False,
        False,
        current_batch_id,
        failed_batches=failed_batches,
        pause_until=pause_until,
        reason="恢复冷却结束，仅允许一笔固定基础金额恢复单",
    )


def _cooldown_decision(
    current_batch_id: str,
    failed_batches: int,
    pause_until: int,
    reason: str,
) -> WaveBatchGuardDecision:
    return _decision(
        "WAVE_GLOBAL_COOLDOWN",
        "COOLDOWN",
        True,
        False,
        current_batch_id,
        failed_batches=failed_batches,
        pause_until=pause_until,
        reason=reason,
    )


def _decision(
    code: str,
    mode: str,
    blocked: bool,
    allow_progression: bool,
    current_batch_id: str,
    *,
    batch_orders: int = 0,
    batch_wins: int = 0,
    batch_losses: int = 0,
    failed_batches: int = 0,
    pause_until: int = 0,
    reason: str = "",
) -> WaveBatchGuardDecision:
    return WaveBatchGuardDecision(
        code=code,
        mode=mode,
        blocked=blocked,
        allow_progression=allow_progression,
        current_batch_id=current_batch_id,
        batch_orders=batch_orders,
        batch_wins=batch_wins,
        batch_losses=batch_losses,
        failed_batches=failed_batches,
        pause_until=pause_until,
        reason=reason,
    )
