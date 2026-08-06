from dataclasses import dataclass
from typing import Sequence

from app.models import Kline


@dataclass(frozen=True)
class WaveSnapshot:
    state: str
    raw_state: str
    window: int
    efficiency: float
    direction_ratio: float
    atr_strength: float
    range_position: float
    confirmations: int
    confirmed_at: int
    allowed_directions: tuple[str, ...]


def analyze_wave(
    klines: Sequence[Kline],
    previous: WaveSnapshot | None = None,
    *,
    window: int = 8,
    atr_window: int = 14,
    min_efficiency: float = 0.35,
    min_direction_ratio: float = 0.60,
    min_atr_strength: float = 0.50,
) -> WaveSnapshot:
    if window < 2 or atr_window < 1:
        raise ValueError("wave and ATR windows must be positive")

    ordered = sorted(klines, key=lambda item: item.close_time)
    if len(ordered) < max(window, atr_window + 1):
        return _unknown_snapshot(window)

    wave_bars = ordered[-window:]
    closes = [item.close for item in wave_bars]
    deltas = [current - prior for prior, current in zip(closes, closes[1:])]
    net_change = closes[-1] - closes[0]
    path_length = sum(abs(value) for value in deltas)
    efficiency = abs(net_change) / path_length if path_length > 0 else 0.0

    direction = 1 if net_change > 0 else -1 if net_change < 0 else 0
    matching_moves = sum(
        1
        for value in deltas
        if (direction > 0 and value > 0) or (direction < 0 and value < 0)
    )
    direction_ratio = matching_moves / len(deltas) if direction else 0.0

    true_ranges = []
    atr_start = len(ordered) - atr_window
    for index in range(atr_start, len(ordered)):
        item = ordered[index]
        previous_close = ordered[index - 1].close
        true_ranges.append(
            max(
                item.high - item.low,
                abs(item.high - previous_close),
                abs(item.low - previous_close),
            )
        )
    atr = sum(true_ranges) / len(true_ranges) if true_ranges else 0.0
    atr_strength = abs(net_change) / atr if atr > 0 else 0.0

    range_low = min(item.low for item in wave_bars)
    range_high = max(item.high for item in wave_bars)
    range_span = range_high - range_low
    range_position = (closes[-1] - range_low) / range_span if range_span > 0 else 0.5
    range_position = min(1.0, max(0.0, range_position))

    trend_qualified = (
        direction != 0
        and efficiency >= min_efficiency
        and direction_ratio >= min_direction_ratio
        and atr_strength >= min_atr_strength
    )
    if trend_qualified:
        raw_state = "UP_LEG" if direction > 0 else "DOWN_LEG"
    elif range_position >= 0.70:
        raw_state = "RANGE_HIGH"
    elif range_position <= 0.30:
        raw_state = "RANGE_LOW"
    else:
        raw_state = "RANGE_MID"

    current_time = wave_bars[-1].close_time
    if raw_state not in {"UP_LEG", "DOWN_LEG"}:
        confirmed_at = (
            previous.confirmed_at
            if previous is not None and previous.state == raw_state
            else current_time
        )
        return WaveSnapshot(
            state=raw_state,
            raw_state=raw_state,
            window=window,
            efficiency=efficiency,
            direction_ratio=direction_ratio,
            atr_strength=atr_strength,
            range_position=range_position,
            confirmations=0,
            confirmed_at=confirmed_at,
            allowed_directions=_allowed_directions(raw_state),
        )

    confirmations = 1
    if previous is not None and previous.raw_state == raw_state:
        confirmations = previous.confirmations
        if current_time > previous.confirmed_at:
            confirmations += 1
    confirmations = min(2, max(1, confirmations))
    if confirmations < 2:
        state = "TURN_UP" if raw_state == "UP_LEG" else "TURN_DOWN"
        allowed_directions: tuple[str, ...] = ()
        confirmed_at = current_time
    else:
        state = raw_state
        allowed_directions = _allowed_directions(state)
        confirmed_at = (
            previous.confirmed_at
            if previous is not None and previous.state == state
            else current_time
        )

    return WaveSnapshot(
        state=state,
        raw_state=raw_state,
        window=window,
        efficiency=efficiency,
        direction_ratio=direction_ratio,
        atr_strength=atr_strength,
        range_position=range_position,
        confirmations=confirmations,
        confirmed_at=confirmed_at,
        allowed_directions=allowed_directions,
    )


def _allowed_directions(state: str) -> tuple[str, ...]:
    if state in {"UP_LEG", "RANGE_LOW"}:
        return ("LONG",)
    if state in {"DOWN_LEG", "RANGE_HIGH"}:
        return ("SHORT",)
    return ()


def _unknown_snapshot(window: int) -> WaveSnapshot:
    return WaveSnapshot(
        state="UNKNOWN",
        raw_state="UNKNOWN",
        window=window,
        efficiency=0.0,
        direction_ratio=0.0,
        atr_strength=0.0,
        range_position=0.5,
        confirmations=0,
        confirmed_at=0,
        allowed_directions=(),
    )
