from dataclasses import replace

from app.models import Signal


QUALITY_SCORE_VERSION = "QS_V1_SHADOW"
QUALITY_SCORE_MODE = "SHADOW_ONLY"
QUALITY_SCORE_BASE = 50.0


_WAVE_STATE_POINTS = {
    "LONG_FIRST": {
        "UP_LEG": 6.0,
        "DOWN_LEG": 1.0,
        "RANGE_HIGH": 1.0,
        "RANGE_MID": 4.0,
        "RANGE_LOW": -4.0,
        "TURN_UP": -7.0,
        "TURN_DOWN": -6.0,
    },
    "LONG_SECOND": {
        "UP_LEG": 10.0,
        "DOWN_LEG": 1.0,
        "RANGE_HIGH": -4.0,
        "RANGE_MID": 0.0,
        "RANGE_LOW": -5.0,
        "TURN_UP": -12.0,
        "TURN_DOWN": -8.0,
    },
    "SHORT_FIRST": {
        "UP_LEG": 3.0,
        "DOWN_LEG": 2.0,
        "RANGE_HIGH": 1.0,
        "RANGE_MID": -5.0,
        "RANGE_LOW": 0.0,
        "TURN_UP": -5.0,
        "TURN_DOWN": -5.0,
    },
    "SHORT_SECOND": {
        "UP_LEG": 6.0,
        "DOWN_LEG": 1.0,
        "RANGE_HIGH": 2.0,
        "RANGE_MID": 4.0,
        "RANGE_LOW": 3.0,
        "TURN_UP": -4.0,
        "TURN_DOWN": -7.0,
    },
}


def attach_shadow_quality_score(signal: Signal, *, open_order_count: int) -> Signal:
    direction = (signal.observe_direction or signal.direction).upper()
    slot = "SECOND" if int(open_order_count) > 0 else "FIRST"
    context = f"{direction}_{slot}" if direction in {"LONG", "SHORT"} else f"NONE_{slot}"
    components = _score_components(signal, direction=direction, context=context)
    score = _clamp(QUALITY_SCORE_BASE + sum(components.values()), 0.0, 100.0)
    inputs = {
        "direction": direction,
        "slot": slot,
        "session_sample_size": signal.session_sample_size,
        "session_win_rate": signal.session_win_rate,
        "session_ev": signal.session_ev,
        "volume_ratio": signal.volume_ratio,
        "price_change_pct": signal.price_change_pct,
        "price_position": signal.price_position,
        "close_strength": signal.close_strength,
        "macd_histogram": signal.macd_histogram,
        "macd_histogram_delta": signal.macd_histogram_delta,
        "rsi": signal.rsi,
        "bollinger_position": signal.bollinger_position,
        "wave_state": signal.wave_state,
        "wave_efficiency": signal.wave_efficiency,
        "wave_direction_ratio": signal.wave_direction_ratio,
        "wave_atr_strength": signal.wave_atr_strength,
    }
    return replace(
        signal,
        order_slot=slot,
        quality_score=round(score, 2),
        quality_score_version=QUALITY_SCORE_VERSION,
        quality_score_mode=QUALITY_SCORE_MODE,
        quality_score_context=context,
        quality_score_components={key: round(value, 2) for key, value in components.items()},
        quality_score_inputs=inputs,
    )


def _score_components(signal: Signal, *, direction: str, context: str) -> dict[str, float]:
    if direction not in {"LONG", "SHORT"}:
        return {key: 0.0 for key in _COMPONENT_NAMES}

    direction_sign = 1.0 if direction == "LONG" else -1.0
    profile = 0.0
    if signal.session_sample_size >= 8:
        profile = _clamp(
            (signal.session_win_rate - 0.5556) * 40.0 + signal.session_ev * 0.4,
            -8.0,
            8.0,
        )

    macd = _signed_indicator_points(signal.macd_histogram, direction_sign, 2.5)
    macd += _signed_indicator_points(signal.macd_histogram_delta, direction_sign, 2.5)
    return {
        "profile": profile,
        "volume": _volume_points(signal.volume_ratio, context),
        "price_change": _clamp(direction_sign * signal.price_change_pct * 2_000.0, -5.0, 5.0),
        "price_position": _clamp(direction_sign * (0.5 - signal.price_position) * 12.0, -6.0, 6.0),
        "close_strength": _clamp(direction_sign * (signal.close_strength - 0.5) * 10.0, -5.0, 5.0),
        "macd": macd,
        "rsi": _clamp(direction_sign * (50.0 - signal.rsi) * 0.3, -6.0, 6.0),
        "bollinger": _clamp(direction_sign * (0.5 - signal.bollinger_position) * 10.0, -7.0, 7.0),
        "wave_state": _WAVE_STATE_POINTS.get(context, {}).get(signal.wave_state, 0.0),
        "wave_quality": _wave_quality_points(signal, second=context.endswith("_SECOND")),
    }


_COMPONENT_NAMES = (
    "profile",
    "volume",
    "price_change",
    "price_position",
    "close_strength",
    "macd",
    "rsi",
    "bollinger",
    "wave_state",
    "wave_quality",
)


def _volume_points(volume_ratio: float, context: str) -> float:
    ratio = float(volume_ratio)
    if ratio < 1.0:
        return -2.0
    if ratio < 1.5:
        return 2.0
    if ratio < 2.0:
        if context == "LONG_SECOND":
            return -7.0
        if context == "SHORT_SECOND":
            return 5.0
        return 1.0
    if ratio < 3.0:
        if context == "LONG_SECOND":
            return -3.0
        if context == "SHORT_SECOND":
            return 3.0
        return 2.0
    return 0.0


def _wave_quality_points(signal: Signal, *, second: bool) -> float:
    efficiency = _clamp((signal.wave_efficiency - 0.35) * 12.0, -4.0, 5.0)
    direction_ratio = _clamp((signal.wave_direction_ratio - 0.5) * 8.0, -3.0, 3.0)
    atr_strength = _clamp((signal.wave_atr_strength - 1.0) * 1.2, -2.0, 3.0)
    multiplier = 1.2 if second else 1.0
    return _clamp((efficiency + direction_ratio + atr_strength) * multiplier, -10.0, 10.0)


def _signed_indicator_points(value: float, direction_sign: float, weight: float) -> float:
    aligned = float(value) * direction_sign
    if aligned > 0.0:
        return weight
    if aligned < 0.0:
        return -weight
    return 0.0


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, float(value)))
