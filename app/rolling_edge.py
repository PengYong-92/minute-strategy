from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class RollingEdgeConfig:
    lookback_days: int = 90
    min_samples: int = 20
    min_win_rate: float = 0.5556
    min_ev: float = 0.5


@dataclass(frozen=True)
class RollingEdgeSnapshot:
    key: str
    sample_size: int
    wins: int
    losses: int
    win_rate: float
    pnl: float
    ev: float


def setup_key(item: Any) -> str:
    timeframe = _get(item, "timeframe_minutes", 0)
    segment = _get(item, "threshold_segment", "GLOBAL")
    reason = str(_get(item, "reason", "UNKNOWN"))
    setup = reason.split("：", 1)[0].split(";", 1)[0].strip() or "UNKNOWN"
    return f"{timeframe}|{segment}|{setup}"


def rolling_edge_snapshot(
    settled_orders: Sequence[Any],
    current_item: Any,
    config: RollingEdgeConfig | None = None,
) -> RollingEdgeSnapshot:
    config = config or RollingEdgeConfig()
    current_time = _entry_time(current_item)
    key = setup_key(current_item)
    start_time = current_time - config.lookback_days * 86_400_000
    rows = [
        order
        for order in settled_orders
        if setup_key(order) == key
        and _get(order, "result", None) in {"WIN", "LOSS"}
        and start_time <= _entry_time(order) < current_time
    ]
    wins = sum(1 for order in rows if _get(order, "result", None) == "WIN")
    sample_size = len(rows)
    pnl = round(sum(float(_get(order, "pnl", 0.0)) for order in rows), 4)
    return RollingEdgeSnapshot(
        key=key,
        sample_size=sample_size,
        wins=wins,
        losses=sample_size - wins,
        win_rate=round(wins / sample_size, 4) if sample_size else 0.0,
        pnl=pnl,
        ev=round(pnl / sample_size, 4) if sample_size else 0.0,
    )


def should_degrade(snapshot: RollingEdgeSnapshot, config: RollingEdgeConfig | None = None) -> bool:
    config = config or RollingEdgeConfig()
    if snapshot.sample_size < config.min_samples:
        return False
    return snapshot.win_rate < config.min_win_rate or snapshot.ev <= config.min_ev


def _get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _entry_time(item: Any) -> int:
    value = _get(item, "entry_time", None)
    if value is None:
        value = _get(item, "opened_at", None)
    if value is None:
        value = _get(item, "open_time", 0)
    return int(value or 0)
