from collections import deque
from dataclasses import dataclass
from math import sqrt
from typing import Sequence

from app.math_tools import clamp, percentile
from app.models import Kline
from app.segments import threshold_segment


SHORT_MIN_RSI = 35.0
SHORT_MAX_RSI = 70.0
SHORT_MIN_BOLLINGER_POSITION = 0.35
SHORT_MAX_BOLLINGER_POSITION = 0.85


@dataclass(frozen=True)
class TechnicalContext:
    macd_histogram: float = 0.0
    macd_histogram_delta: float = 0.0
    rsi: float = 50.0
    bollinger_position: float = 0.5
    bollinger_width: float = 0.0
    macd_line: float = 0.0
    macd_signal_line: float = 0.0
    atr: float = 0.0
    macd_histogram_atr: float = 0.0
    macd_delta_atr: float = 0.0


@dataclass(frozen=True)
class IndicatorProfile:
    segment: str = "GLOBAL"
    sample_size: int = 0
    rsi_lower: float = SHORT_MIN_RSI
    rsi_upper: float = SHORT_MAX_RSI
    bollinger_lower: float = SHORT_MIN_BOLLINGER_POSITION
    bollinger_upper: float = SHORT_MAX_BOLLINGER_POSITION
    macd_histogram_threshold: float = 0.0
    macd_delta_threshold: float = 0.0


def build_technical_context(klines: Sequence[Kline]) -> TechnicalContext:
    closes = [item.close for item in klines]
    if not closes:
        return TechnicalContext()

    macd_line, macd_signal_line, macd_histogram, macd_histogram_delta = _macd_context(closes)
    atr = _atr(klines, period=14)
    return TechnicalContext(
        macd_histogram=macd_histogram,
        macd_histogram_delta=macd_histogram_delta,
        rsi=_rsi(closes, period=14),
        bollinger_position=_bollinger_position(closes, period=20, std_dev=2.0),
        bollinger_width=_bollinger_width(closes, period=20, std_dev=2.0),
        macd_line=macd_line,
        macd_signal_line=macd_signal_line,
        atr=atr,
        macd_histogram_atr=macd_histogram / atr if atr > 0.0 else 0.0,
        macd_delta_atr=macd_histogram_delta / atr if atr > 0.0 else 0.0,
    )


def build_indicator_profile(
    history: Sequence[Kline],
    timeframe_minutes: int,
    segment: str,
    lookback_minutes: int,
) -> IndicatorProfile:
    scoped = list(history[-lookback_minutes:])
    if len(scoped) < 60:
        return IndicatorProfile(segment=segment)

    rsi_values, bollinger_values, macd_values, macd_delta_values = _technical_series(scoped)
    valid_indices = [index for index in range(len(scoped)) if rsi_values[index] > 0.0]
    session_indices = [
        index for index in valid_indices if threshold_segment(scoped[index].close_time) == segment
    ]
    if len(session_indices) >= max(12, timeframe_minutes):
        indices = session_indices
        profile_segment = segment
    else:
        indices = valid_indices
        profile_segment = "GLOBAL"

    if not indices:
        return IndicatorProfile(segment=profile_segment)

    rsi_sample = [rsi_values[index] for index in indices]
    bollinger_sample = [bollinger_values[index] for index in indices]
    macd_sample = [macd_values[index] for index in indices]
    macd_delta_sample = [macd_delta_values[index] for index in indices]

    return IndicatorProfile(
        segment=profile_segment,
        sample_size=len(indices),
        rsi_lower=round(clamp(percentile(rsi_sample, 20), 25.0, 45.0), 1),
        rsi_upper=round(clamp(percentile(rsi_sample, 85), 60.0, 82.0), 1),
        bollinger_lower=round(clamp(percentile(bollinger_sample, 25), 0.25, 0.50), 3),
        bollinger_upper=round(clamp(percentile(bollinger_sample, 90), 0.75, 0.95), 3),
        macd_histogram_threshold=round(min(0.0, percentile(macd_sample, 45)), 6),
        macd_delta_threshold=round(min(0.0, percentile(macd_delta_sample, 55)), 6),
    )


def confirm_short_setup(
    technical: TechnicalContext,
    mtf_10m_bias: float,
    has_lower_reclaim: bool,
    indicator_profile: IndicatorProfile,
    require_bollinger_room: bool = True,
) -> tuple[bool, str]:
    failed = []
    if technical.macd_histogram >= indicator_profile.macd_histogram_threshold:
        failed.append("MACD未转空")
    if technical.macd_histogram_delta >= indicator_profile.macd_delta_threshold:
        failed.append("MACD空头动能未增强")
    if not (indicator_profile.rsi_lower < technical.rsi < indicator_profile.rsi_upper):
        failed.append("RSI过冷或过热")
    if mtf_10m_bias >= 2.0:
        failed.append("10m聚合趋势仍过强")
    if has_lower_reclaim:
        failed.append("下影承接明显")
    if require_bollinger_room and not (
        indicator_profile.bollinger_lower <= technical.bollinger_position <= indicator_profile.bollinger_upper
    ):
        failed.append("BOLL位置追空风险高")
    if failed:
        return False, "、".join(failed)
    return True, "MACD空头、RSI未过冷、BOLL未贴下轨"


def short_indicator_points(technical: TechnicalContext, mtf_10m_bias: float, mtf_30m_bias: float) -> float:
    points = 0.0
    if technical.macd_histogram < 0:
        points += 7.0
    if technical.macd_histogram_delta < 0:
        points += 5.0
    if SHORT_MIN_RSI < technical.rsi < SHORT_MAX_RSI:
        points += 4.0
    if SHORT_MIN_BOLLINGER_POSITION <= technical.bollinger_position <= SHORT_MAX_BOLLINGER_POSITION:
        points += 4.0
    points += clamp(max(-mtf_10m_bias, 0.0) * 4.0, 0.0, 8.0)
    points += clamp(max(-mtf_30m_bias, 0.0) * 2.0, 0.0, 4.0)
    return points


def _technical_series(klines: Sequence[Kline]) -> tuple[list[float], list[float], list[float], list[float]]:
    closes = [item.close for item in klines]
    if not closes:
        return [], [], [], []

    _macd_line, _signal_line, macd_histogram, macd_delta = _macd_series(closes)

    return (
        _rsi_series(closes, 14),
        _bollinger_position_series(closes, 20, 2.0),
        macd_histogram,
        macd_delta,
    )


def _rsi_series(closes: Sequence[float], period: int) -> list[float]:
    values = [0.0] * len(closes)
    gains: deque[float] = deque()
    losses: deque[float] = deque()
    gain_sum = 0.0
    loss_sum = 0.0
    for index in range(1, len(closes)):
        change = closes[index] - closes[index - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        gains.append(gain)
        losses.append(loss)
        gain_sum += gain
        loss_sum += loss
        if len(gains) > period:
            gain_sum -= gains.popleft()
            loss_sum -= losses.popleft()
        if len(gains) == period:
            values[index] = 100.0 if loss_sum == 0 else 100.0 - 100.0 / (1.0 + (gain_sum / period) / (loss_sum / period))
    return values


def _bollinger_position_series(closes: Sequence[float], period: int, std_dev: float) -> list[float]:
    values = [0.5] * len(closes)
    window: deque[float] = deque()
    total = 0.0
    total_sq = 0.0
    for index, close in enumerate(closes):
        window.append(close)
        total += close
        total_sq += close * close
        if len(window) > period:
            old = window.popleft()
            total -= old
            total_sq -= old * old
        if len(window) == period:
            middle = total / period
            variance = max(0.0, total_sq / period - middle * middle)
            deviation = sqrt(variance)
            upper = middle + std_dev * deviation
            lower = middle - std_dev * deviation
            width = upper - lower
            values[index] = (close - lower) / width if width > 0 else 0.5
    return values


def _macd_context(closes: Sequence[float]) -> tuple[float, float, float, float]:
    if len(closes) < 35:
        return 0.0, 0.0, 0.0, 0.0
    macd_line, signal_line, histogram, delta = _macd_series(closes)
    return macd_line[-1], signal_line[-1], histogram[-1], delta[-1]


def _macd_series(
    closes: Sequence[float],
) -> tuple[list[float], list[float], list[float], list[float]]:
    ema_fast = _ema(closes, 12)
    ema_slow = _ema(closes, 26)
    macd_line = [fast - slow for fast, slow in zip(ema_fast, ema_slow)]
    signal_line = _ema(macd_line, 9)
    histogram = [macd - signal for macd, signal in zip(macd_line, signal_line)]
    delta = [0.0]
    for previous, current in zip(histogram, histogram[1:]):
        delta.append(current - previous)
    return macd_line, signal_line, histogram, delta


def _atr(klines: Sequence[Kline], period: int) -> float:
    if len(klines) < period:
        return 0.0
    true_ranges = []
    for index, item in enumerate(klines):
        if index == 0:
            true_ranges.append(item.high - item.low)
            continue
        previous_close = klines[index - 1].close
        true_ranges.append(
            max(
                item.high - item.low,
                abs(item.high - previous_close),
                abs(item.low - previous_close),
            )
        )
    return sum(true_ranges[-period:]) / period


def _ema(values: Sequence[float], period: int) -> list[float]:
    if not values:
        return []
    multiplier = 2.0 / (period + 1.0)
    current = values[0]
    result = []
    for value in values:
        current = value * multiplier + current * (1.0 - multiplier)
        result.append(current)
    return result


def _rsi(closes: Sequence[float], period: int) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains = 0.0
    losses = 0.0
    for previous, current in zip(closes[-period - 1 : -1], closes[-period:]):
        change = current - previous
        if change >= 0:
            gains += change
        else:
            losses -= change
    if losses == 0:
        return 100.0
    relative_strength = (gains / period) / (losses / period)
    return 100.0 - 100.0 / (1.0 + relative_strength)


def _bollinger_position(closes: Sequence[float], period: int, std_dev: float) -> float:
    _middle, upper, lower = _bollinger_bands(closes, period, std_dev)
    width = upper - lower
    if width <= 0:
        return 0.5
    return (closes[-1] - lower) / width


def _bollinger_width(closes: Sequence[float], period: int, std_dev: float) -> float:
    middle, upper, lower = _bollinger_bands(closes, period, std_dev)
    if middle == 0:
        return 0.0
    return (upper - lower) / middle


def _bollinger_bands(closes: Sequence[float], period: int, std_dev: float) -> tuple[float, float, float]:
    if len(closes) < period:
        latest = closes[-1] if closes else 0.0
        return latest, latest, latest
    values = closes[-period:]
    middle = sum(values) / period
    variance = sum((value - middle) ** 2 for value in values) / period
    deviation = sqrt(variance)
    upper = middle + std_dev * deviation
    lower = middle - std_dev * deviation
    return middle, upper, lower
