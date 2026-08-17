from dataclasses import dataclass
from statistics import median
from typing import Sequence

from app.indicators import (
    SHORT_MAX_BOLLINGER_POSITION,
    SHORT_MAX_RSI,
    SHORT_MIN_BOLLINGER_POSITION,
    SHORT_MIN_RSI,
    IndicatorProfile,
    TechnicalContext,
    build_indicator_profile,
    build_technical_context,
    confirm_short_setup,
    short_indicator_points,
)
from app.math_tools import clamp as _clamp
from app.math_tools import percentile as _percentile
from app.models import FearGreedContext, Kline, Signal
from app.segments import threshold_segment as _threshold_segment
from app.timeframes import aggregate_klines, trend_bias


MIN_TRADE_EDGE = 10.0
MAX_TRADE_EDGE = 30.0
SEGMENT_MAX_TRADE_EDGE = {
    "WD-00": 16.0,
    "WD-08": 26.0,
    "WD-20": 14.0,
    "WD-22": 25.0,
    "WE-02": 24.0,
    "WE-03": 33.0,
    "WE-08": 25.0,
    "WE-17": 36.0,
}
SHORT_THRESHOLD_PREMIUM = 8.0
SHORT_EDGE_PREMIUM = 2.0
DYNAMIC_PROFILE_LOOKBACK_MINUTES = 30 * 24 * 60
LIVE_TRADE_TIMEFRAMES = (10,)
LONG_REBOUND_MIN_EDGE = 18.0
LONG_REBOUND_MAX_RSI = 45.0
LONG_REBOUND_MAX_BOLLINGER_POSITION = 0.35
LONG_REBOUND_MAX_VOLUME_RATIO = 4.0
LONG_REBOUND_MAX_MTF_10M_BIAS = 1.0
NORMAL_DOWN_SHORT_EXTENSION_MIN_EDGE = 8.0
EXTREME_DROP_RECLAIM_MIN_DROP_PCT = 0.012
EXTREME_DROP_RECLAIM_MIN_VOLUME_RATIO = 1.5
EXTREME_DROP_RECLAIM_MAX_RSI = 30.0
EXTREME_DROP_RECLAIM_MAX_BOLLINGER_POSITION = 0.1
FAILED_BREAKOUT_OBSERVATION_SEGMENTS = {
    "failed_high_120m_short_observe": set(),
    "failed_low_120m_long_observe": set(),
}


@dataclass(frozen=True)
class SessionEdge:
    sample_size: int
    win_rate: float
    ev: float


def _applied_score_components(
    branch: str,
    direction_multiplier: float = 0.0,
    *,
    base_points: float = 0.0,
    volume_points: float = 0.0,
    move_points: float = 0.0,
    trend_points: float = 0.0,
    close_points: float = 0.0,
    indicator_points: float = 0.0,
    diagnostic_trend_score: float | None = None,
    diagnostic_unweighted_volume_points: float | None = None,
    diagnostic_unweighted_move_points: float | None = None,
) -> dict[str, object]:
    reconstructed_raw_score = direction_multiplier * (
        base_points
        + volume_points
        + move_points
        + trend_points
        + close_points
        + indicator_points
    )
    components: dict[str, object] = {
        "branch": branch,
        "direction_multiplier": direction_multiplier,
        "base_points": base_points,
        "volume_points": volume_points,
        "move_points": move_points,
        "trend_points": trend_points,
        "close_points": close_points,
        "indicator_points": indicator_points,
        "reconstructed_raw_score": reconstructed_raw_score,
    }
    if diagnostic_trend_score is not None:
        components["diagnostic_trend_score"] = diagnostic_trend_score
    if diagnostic_unweighted_volume_points is not None:
        components["diagnostic_unweighted_volume_points"] = diagnostic_unweighted_volume_points
    if diagnostic_unweighted_move_points is not None:
        components["diagnostic_unweighted_move_points"] = diagnostic_unweighted_move_points
    return components


def _empty_decision_inputs(branch: str) -> dict[str, object]:
    return {
        "indicators": {},
        "volume_price": {},
        "thresholds": {},
        "score": {
            "raw_direction": "WAIT",
            "raw_score": 0.0,
            "signed_score": 0.0,
            "score_abs": 0.0,
            "edge": 0.0,
            "final_direction": "WAIT",
            "actionable": False,
            **_applied_score_components(branch),
        },
    }


NORMAL_DOWN_SHORT_EXTENSION_EDGE_BY_TIMEFRAME = {
    10: {
        "WD-02": SessionEdge(27, 0.7407, 3.3333),
        "WD-23": SessionEdge(23, 0.6522, 1.7391),
    },
}


SESSION_EDGE_BY_TIMEFRAME = {
    10: {
        "WD-00": SessionEdge(32, 0.6875, 2.3750),
        "WD-08": SessionEdge(23, 0.6087, 0.9565),
        "WD-12": SessionEdge(37, 0.6757, 2.1622),
        "WD-18": SessionEdge(26, 0.6538, 1.7692),
        "WD-20": SessionEdge(30, 0.6333, 1.4000),
        "WD-22": SessionEdge(30, 0.6333, 1.4000),
        "WE-02": SessionEdge(9, 0.7778, 4.0000),
        "WE-03": SessionEdge(8, 0.7500, 3.5000),
        "WE-08": SessionEdge(7, 0.5714, 0.2857),
        "WE-13": SessionEdge(6, 0.6667, 2.0000),
        "WE-17": SessionEdge(10, 0.7000, 2.6000),
    },
}


SHORT_SESSION_EDGE_BY_TIMEFRAME = {
    10: {
        "WD-13": SessionEdge(8, 0.7500, 3.5000),
        "WD-21": SessionEdge(8, 0.8750, 5.7500),
        "WD-22": SessionEdge(6, 0.8333, 5.0000),
    },
}


def choose_trade_signal(klines: Sequence[Kline], fear_greed: FearGreedContext | None = None) -> Signal:
    candidates = [
        analyze_volume_price(klines, timeframe_minutes=10, fear_greed=fear_greed),
    ]
    return choose_best_candidate(candidates)


def analyze_observation_signals(
    klines: Sequence[Kline],
    timeframe_minutes: int = 10,
    fear_greed: FearGreedContext | None = None,
) -> list[Signal]:
    """Generate research-only 10m candidates without changing live order decisions."""
    primary = analyze_volume_price(klines, timeframe_minutes=timeframe_minutes, fear_greed=fear_greed)
    if not klines or timeframe_minutes not in LIVE_TRADE_TIMEFRAMES:
        return []

    latest = klines[-1]
    recent = list(klines[-timeframe_minutes:])
    history = list(klines[:-timeframe_minutes]) or list(klines[:-1])
    candidates: list[Signal] = []

    failed_breakout = _failed_breakout_observation(primary, history, recent, latest)
    if failed_breakout:
        candidates.append(failed_breakout)

    return _dedupe_observation_signals(candidates)


def choose_best_candidate(candidates: Sequence[Signal]) -> Signal:
    live_candidates = [signal for signal in candidates if signal.timeframe_minutes in LIVE_TRADE_TIMEFRAMES]
    actionable = [signal for signal in live_candidates if signal.actionable]
    if actionable:
        return max(actionable, key=_candidate_rank)

    if live_candidates:
        return max(live_candidates, key=_candidate_rank)

    latest = candidates[0] if candidates else None
    return _signal(
        "WAIT",
        10,
        "B",
        "当前仅启用10分钟开单，忽略非10分钟候选",
        latest.price if latest else 0.0,
        latest.open_time if latest else 0,
    )


def analyze_volume_price(
    klines: Sequence[Kline],
    timeframe_minutes: int,
    fear_greed: FearGreedContext | None = None,
) -> Signal:
    if timeframe_minutes not in LIVE_TRADE_TIMEFRAMES:
        raise ValueError("only 10-minute analysis is supported")
    if not klines:
        return _signal(
            "WAIT",
            timeframe_minutes,
            "B",
            "暂无K线数据",
            0.0,
            0,
            decision_inputs=_empty_decision_inputs("no_klines_wait"),
        )

    if len(klines) < timeframe_minutes * 3:
        latest = klines[-1]
        return _signal(
            "WAIT",
            timeframe_minutes,
            "B",
            "历史K线不足，等待累计样本",
            latest.close,
            latest.open_time,
            decision_inputs=_empty_decision_inputs("insufficient_history_wait"),
        )

    latest = klines[-1]
    recent_size = timeframe_minutes
    recent = list(klines[-recent_size:])
    history = list(klines[:-recent_size]) or list(klines[:-1])
    threshold_segment = _threshold_segment(latest.close_time)

    (
        volume_ratio,
        volume_threshold,
        low_volume_threshold,
        volume_noise,
        current_volume,
        volume_baseline,
    ) = _volume_context(history, recent, recent_size, threshold_segment)
    price_position = _price_position(history, latest.close)
    price_change_pct = _window_change(recent)
    window_returns = _window_returns(history[-DYNAMIC_PROFILE_LOOKBACK_MINUTES:], recent_size, threshold_segment)
    move_threshold_pct = _dynamic_move_threshold(window_returns, recent_size)
    direction = _price_direction(price_change_pct, move_threshold_pct)

    position = _position_bucket(price_position)
    volume_state = _volume_bucket(volume_ratio, volume_threshold, low_volume_threshold)
    close_strength = _range_close_strength(recent, latest.close)
    context_rows = list(history[-240:]) + recent
    mtf_10m_bias = trend_bias(aggregate_klines(context_rows, 10), lookback=3)
    mtf_30m_bias = trend_bias(aggregate_klines(context_rows, 30), lookback=3)
    technical = build_technical_context(context_rows)
    needs_short_profile = (
        (position == "HIGH" and volume_state == "HIGH" and direction == "DOWN")
        or (volume_state == "HIGH" and direction == "DOWN")
        or (volume_state == "NORMAL" and direction == "DOWN")
        or threshold_segment in SHORT_SESSION_EDGE_BY_TIMEFRAME.get(timeframe_minutes, {})
    )
    indicator_profile = (
        build_indicator_profile(
            history,
            timeframe_minutes,
            threshold_segment,
            DYNAMIC_PROFILE_LOOKBACK_MINUTES,
        )
        if needs_short_profile
        else IndicatorProfile(segment=threshold_segment)
    )
    regime = _regime_label(fear_greed, technical, mtf_30m_bias)
    candle_strength = _candle_close_strength(latest)
    candle_range = latest.high - latest.low
    upper_wick = latest.high - latest.close
    lower_wick = latest.close - latest.low
    upper_wick_ratio = upper_wick / candle_range if candle_range > 0.0 else 0.0
    lower_wick_ratio = lower_wick / candle_range if candle_range > 0.0 else 0.0
    has_upper_rejection = candle_strength <= 0.35 and upper_wick > lower_wick * 1.2
    has_lower_reclaim = candle_strength >= 0.65 and lower_wick > upper_wick * 1.2
    trend_score = _trend_consistency(recent)
    base_threshold = _dynamic_trade_threshold(window_returns, volume_noise)

    raw_direction, raw_score, reason, score_components = _score_setup(
        position=position,
        volume_state=volume_state,
        direction=direction,
        volume_ratio=volume_ratio,
        volume_threshold=volume_threshold,
        price_change_pct=price_change_pct,
        move_threshold_pct=move_threshold_pct,
        trend_score=trend_score,
        close_strength=close_strength,
        has_upper_rejection=has_upper_rejection,
        has_lower_reclaim=has_lower_reclaim,
        mtf_10m_bias=mtf_10m_bias,
        mtf_30m_bias=mtf_30m_bias,
        technical=technical,
        indicator_profile=indicator_profile,
        timeframe_minutes=timeframe_minutes,
        regime=regime,
        fear_greed_value=fear_greed.value if fear_greed else None,
    )

    score = _clamp(raw_score, -100.0, 100.0)
    session_edge = _session_edge(timeframe_minutes, threshold_segment, raw_direction)
    trend_broad_short = _is_trend_broad_short_reason(reason)
    direction_threshold = _dynamic_direction_threshold(base_threshold, raw_direction)
    session_adjusted_threshold = _session_adjusted_threshold(
        direction_threshold,
        session_edge,
        allow_unprofiled=trend_broad_short,
    )
    fear_greed_adjustment = _fear_greed_threshold_adjustment(raw_direction, fear_greed)
    regime_adjustment = _regime_threshold_adjustment(raw_direction, regime, technical)
    threshold = round(
        _clamp(
            session_adjusted_threshold + fear_greed_adjustment + regime_adjustment,
            58.0,
            95.0,
        ),
        1,
    )
    pre_override_threshold = threshold
    session_edge_min = _session_min_edge(
        session_edge,
        raw_direction,
        allow_unprofiled=trend_broad_short,
    )
    score_abs = abs(score)
    edge = score_abs - threshold
    session_allowed = session_edge is not None or trend_broad_short
    normal_down_short_override_reason = _normal_down_short_override_reason(
        raw_direction,
        reason,
        timeframe_minutes=timeframe_minutes,
        threshold_segment=threshold_segment,
        score_abs=score_abs,
        threshold=threshold,
    )
    if normal_down_short_override_reason:
        extension_edge = _normal_down_short_extension_edge(timeframe_minutes, threshold_segment)
        if extension_edge:
            session_edge = extension_edge
        threshold = round(max(0.0, score_abs - NORMAL_DOWN_SHORT_EXTENSION_MIN_EDGE), 1)
        session_edge_min = NORMAL_DOWN_SHORT_EXTENSION_MIN_EDGE
        edge = score_abs - threshold
        session_allowed = True
        reason = f"{reason}；{normal_down_short_override_reason}"
    normal_down_short_threshold_adjustment = threshold - pre_override_threshold
    max_trade_edge = max_trade_edge_for(timeframe_minutes, threshold_segment, raw_direction)
    long_rebound_guard_reason = _long_rebound_guard_reason(
        raw_direction,
        reason,
        edge,
        technical.rsi,
        technical.bollinger_position,
        volume_ratio,
        mtf_10m_bias,
    )
    strategy_family, strategy_tag, observe_direction, observe_only = _strategy_identity(
        raw_direction,
        reason,
        price_change_pct,
        volume_ratio,
        technical.rsi,
        technical.bollinger_position,
        has_lower_reclaim,
        has_upper_rejection,
    )
    actionable = (
        raw_direction in {"LONG", "SHORT"}
        and session_allowed
        and long_rebound_guard_reason is None
        and session_edge_min <= edge < max_trade_edge
    )
    level = _level(abs(score), threshold)
    final_direction = raw_direction if actionable else "WAIT"
    final_reason = reason
    if not actionable:
        if raw_direction in {"LONG", "SHORT"} and not session_allowed:
            final_reason = (
                f"{reason}；{timeframe_minutes}分钟 {threshold_segment} "
                "时段样本不足或EV未过线，该周期不开单"
            )
        elif edge < 0:
            final_reason = f"{reason}；分数 {score_abs:.1f} < 动态阈值 {threshold:.1f}，不开单"
        elif edge >= max_trade_edge:
            final_reason = f"{reason}；分数边际 {edge:.1f} >= {max_trade_edge:.0f}，极端过热不开单"
        elif long_rebound_guard_reason:
            final_reason = f"{reason}；{long_rebound_guard_reason}"
        elif edge < session_edge_min:
            final_reason = f"{reason}；分数边际 {edge:.1f} < 时段最小边际 {session_edge_min:.1f}，确认不足不开单"
        else:
            final_reason = f"{reason}；不满足开单条件"

    session_sample_size = session_edge.sample_size if session_edge else 0
    session_win_rate = session_edge.win_rate if session_edge else 0.0
    session_ev = session_edge.ev if session_edge else 0.0
    decision_inputs = {
        "indicators": {
            "macd_line": technical.macd_line,
            "macd_signal_line": technical.macd_signal_line,
            "macd_histogram": round(technical.macd_histogram, 6),
            "macd_histogram_delta": round(technical.macd_histogram_delta, 6),
            "atr": technical.atr,
            "macd_histogram_atr": technical.macd_histogram_atr,
            "macd_delta_atr": technical.macd_delta_atr,
            "rsi": round(technical.rsi, 2),
            "bollinger_position": round(technical.bollinger_position, 4),
            "bollinger_width": round(technical.bollinger_width, 4),
            "mtf_10m_bias": round(mtf_10m_bias, 4),
            "mtf_30m_bias": round(mtf_30m_bias, 4),
            "indicator_profile_segment": indicator_profile.segment,
            "indicator_profile_sample_size": indicator_profile.sample_size,
            "rsi_lower_threshold": indicator_profile.rsi_lower,
            "rsi_upper_threshold": indicator_profile.rsi_upper,
            "bollinger_lower_threshold": indicator_profile.bollinger_lower,
            "bollinger_upper_threshold": indicator_profile.bollinger_upper,
            "macd_histogram_threshold": indicator_profile.macd_histogram_threshold,
            "macd_delta_threshold": indicator_profile.macd_delta_threshold,
        },
        "volume_price": {
            "current_volume": current_volume,
            "volume_baseline": volume_baseline,
            "volume_ratio": volume_ratio,
            "high_volume_threshold": volume_threshold,
            "low_volume_threshold": low_volume_threshold,
            "volume_noise": volume_noise,
            "volume_state": volume_state,
            "price_change_pct": price_change_pct,
            "move_threshold_pct": move_threshold_pct,
            "price_direction": direction,
            "price_position": price_position,
            "position": position,
            "close_strength": close_strength,
            "candle_strength": candle_strength,
            "upper_wick_ratio": upper_wick_ratio,
            "lower_wick_ratio": lower_wick_ratio,
            "has_upper_rejection": has_upper_rejection,
            "has_lower_reclaim": has_lower_reclaim,
        },
        "thresholds": {
            "window_return_sample_size": len(window_returns),
            "volume_noise": volume_noise,
            "move_threshold_pct": move_threshold_pct,
            "base_threshold": base_threshold,
            "direction_threshold": direction_threshold,
            "session_adjusted_threshold": session_adjusted_threshold,
            "session_threshold_adjustment": session_adjusted_threshold - direction_threshold,
            "fear_greed_adjustment": fear_greed_adjustment,
            "regime_adjustment": regime_adjustment,
            "pre_override_threshold": pre_override_threshold,
            "normal_down_short_override_applied": normal_down_short_override_reason is not None,
            "normal_down_short_threshold_adjustment": normal_down_short_threshold_adjustment,
            "calculated_threshold": round(threshold, 1),
            "session_edge_min": session_edge_min,
            "max_trade_edge": max_trade_edge,
            "session_sample_size": session_sample_size,
            "session_win_rate": session_win_rate,
            "session_ev": session_ev,
            "fear_greed_value": fear_greed.value if fear_greed else None,
            "fear_greed_average_30d": fear_greed.average_30d if fear_greed else 0.0,
            "fear_greed_trend": fear_greed.trend if fear_greed else "",
            "regime": regime,
        },
        "score": {
            "raw_direction": raw_direction,
            "raw_score": raw_score,
            "signed_score": round(score, 1),
            "score_abs": score_abs,
            "edge": edge,
            "final_direction": final_direction,
            "actionable": actionable,
            **score_components,
        },
    }

    return _signal(
        final_direction,
        timeframe_minutes,
        level,
        final_reason,
        latest.close,
        latest.open_time,
        volume_ratio,
        price_position,
        price_change_pct,
        score,
        threshold,
        volume_threshold,
        move_threshold_pct,
        close_strength,
        recent_size,
        recent_size,
        threshold_segment,
        mtf_10m_bias,
        mtf_30m_bias,
        technical.macd_histogram,
        technical.macd_histogram_delta,
        technical.rsi,
        technical.bollinger_position,
        technical.bollinger_width,
        indicator_profile.segment,
        indicator_profile.sample_size,
        indicator_profile.rsi_lower,
        indicator_profile.rsi_upper,
        indicator_profile.bollinger_lower,
        indicator_profile.bollinger_upper,
        indicator_profile.macd_histogram_threshold,
        indicator_profile.macd_delta_threshold,
        fear_greed.value if fear_greed else None,
        fear_greed.classification if fear_greed else "",
        fear_greed.trend if fear_greed else "",
        fear_greed.average_30d if fear_greed else 0.0,
        fear_greed_adjustment,
        session_allowed,
        session_sample_size,
        session_win_rate,
        session_ev,
        session_edge_min,
        regime,
        _risk_flags(
            regime,
            regime_adjustment,
            final_reason,
            raw_direction=raw_direction,
            threshold_segment=threshold_segment,
            level=level,
            price_position=price_position,
            price_change_pct=price_change_pct,
            rsi=technical.rsi,
            mtf_10m_bias=mtf_10m_bias,
            mtf_30m_bias=mtf_30m_bias,
        ),
        strategy_family,
        strategy_tag,
        observe_direction,
        observe_only,
        _profile_key(strategy_family, observe_direction or final_direction or raw_direction, threshold_segment),
        decision_inputs=decision_inputs,
    )


def max_trade_edge_for(timeframe_minutes: int, threshold_segment: str, direction: str = "LONG") -> float:
    if direction == "SHORT":
        return MAX_TRADE_EDGE
    return SEGMENT_MAX_TRADE_EDGE.get(threshold_segment, MAX_TRADE_EDGE)


def _score_setup(
    position: str,
    volume_state: str,
    direction: str,
    volume_ratio: float,
    volume_threshold: float,
    price_change_pct: float,
    move_threshold_pct: float,
    trend_score: float,
    close_strength: float,
    has_upper_rejection: bool,
    has_lower_reclaim: bool,
    mtf_10m_bias: float,
    mtf_30m_bias: float,
    technical: TechnicalContext,
    indicator_profile: IndicatorProfile,
    timeframe_minutes: int,
    regime: str,
    fear_greed_value: int | None,
) -> tuple[str, float, str, dict[str, object]]:
    if volume_state == "LOW":
        if position == "LOW" and direction == "DOWN":
            return (
                "WAIT",
                0.0,
                "低位缩量下跌：买盘未明显承接，等待",
                _applied_score_components("low_volume_low_position_down_wait"),
            )
        return (
            "WAIT",
            0.0,
            f"{_cn_position(position)}缩量{_cn_direction(direction)}：量能不足，等待",
            _applied_score_components("low_volume_wait"),
        )

    volume_points = _volume_points(volume_ratio, volume_threshold)
    move_points = _move_points(price_change_pct, move_threshold_pct)

    def components(
        branch: str,
        direction_multiplier: float = 0.0,
        *,
        base_points: float = 0.0,
        applied_volume_points: float = 0.0,
        applied_move_points: float = 0.0,
        applied_trend_points: float = 0.0,
        close_points: float = 0.0,
        indicator_points: float = 0.0,
    ) -> dict[str, object]:
        return _applied_score_components(
            branch,
            direction_multiplier,
            base_points=base_points,
            volume_points=applied_volume_points,
            move_points=applied_move_points,
            trend_points=applied_trend_points,
            close_points=close_points,
            indicator_points=indicator_points,
            diagnostic_trend_score=trend_score,
            diagnostic_unweighted_volume_points=volume_points,
            diagnostic_unweighted_move_points=move_points,
        )

    short_observe = _fear_falling_trend_short_confirmed(
        timeframe_minutes,
        regime,
        position,
        volume_state,
        direction,
        technical,
        has_lower_reclaim,
    )

    if position == "HIGH" and volume_state == "HIGH" and direction == "DOWN":
        confirmed, note = confirm_short_setup(technical, mtf_10m_bias, has_lower_reclaim, indicator_profile)
        if not confirmed:
            return (
                "WAIT",
                0.0,
                f"高位放量下跌：SHORT确认不足（{note}），仅预警观察不开单",
                components("high_position_high_volume_down_unconfirmed_wait"),
            )
        indicator_points = short_indicator_points(technical, mtf_10m_bias, mtf_30m_bias)
        close_points = (1.0 - close_strength) * 8.0
        score = -(30.0 + volume_points + move_points + max(-trend_score, 0.0) * 10.0 + close_points + indicator_points)
        return (
            "SHORT",
            score,
            "高位放量下跌：MACD/RSI/BOLL确认卖压，动态评分偏空",
            components(
                "high_position_high_volume_down_short",
                -1.0,
                base_points=30.0,
                applied_volume_points=volume_points,
                applied_move_points=move_points,
                applied_trend_points=max(-trend_score, 0.0) * 10.0,
                close_points=close_points,
                indicator_points=indicator_points,
            ),
        )

    if position == "HIGH" and volume_state == "HIGH" and (has_upper_rejection or direction == "FLAT"):
        return (
            "WAIT",
            0.0,
            "高位放量滞涨：回测胜率不足，仅预警观察不开单",
            components("high_position_high_volume_stall_wait"),
        )

    if position == "LOW" and volume_state == "HIGH" and direction == "UP":
        return (
            "WAIT",
            0.0,
            "低位放量上涨：容易把低位反弹误判为确定性机会，回测未覆盖赔率，仅预警观察不开单",
            components("low_position_high_volume_up_wait"),
        )

    if position == "LOW" and volume_state == "HIGH" and direction == "DOWN" and has_lower_reclaim:
        return (
            "WAIT",
            0.0,
            "低位放量承接：三个月回测未覆盖赔率，仅预警观察不开单",
            components("low_position_high_volume_down_reclaim_wait"),
        )

    if volume_state == "HIGH" and direction == "UP":
        return (
            "WAIT",
            0.0,
            "量增价升：回测未达到事件合约盈亏平衡，仅观察不开单",
            components("high_volume_up_wait"),
        )

    if volume_state == "HIGH" and direction == "DOWN":
        close_points = (1.0 - close_strength) * 6.0
        score = 34.0 + volume_points + move_points + max(-trend_score, 0.0) * 10.0 + close_points
        strict_rebound_risk = _trend_strict_rebound_risk(
            timeframe_minutes,
            position,
            volume_state,
            direction,
            technical,
            mtf_10m_bias,
            mtf_30m_bias,
            fear_greed_value,
            has_lower_reclaim,
        )
        broad_rebound_risk = _trend_broad_rebound_risk(
            timeframe_minutes,
            position,
            volume_state,
            direction,
            technical,
            mtf_10m_bias,
            mtf_30m_bias,
            fear_greed_value,
            has_lower_reclaim,
            strict_rebound_risk,
        )
        if strict_rebound_risk:
            return (
                "WAIT",
                0.0,
                "趋势过滤禁多：STRICT候选，RSI未跌透或恐慌低值下双周期反弹，禁止急跌反抽LONG，仅记录观察",
                components("high_volume_down_strict_wait"),
            )
        if broad_rebound_risk:
            return (
                "SHORT",
                -score,
                "趋势候选顺势SHORT：BROAD_ONLY，双周期反弹中破位或BOLL未跌透，放弃急跌反抽LONG并顺势试空",
                components(
                    "high_volume_down_broad_short",
                    -1.0,
                    base_points=34.0,
                    applied_volume_points=volume_points,
                    applied_move_points=move_points,
                    applied_trend_points=max(-trend_score, 0.0) * 10.0,
                    close_points=close_points,
                ),
            )
        reason = "放量急跌反抽：回测显示急跌后后续窗口更偏反弹，动态评分偏多"
        if short_observe:
            reason += "；SHORT观察：恐慌下行中位急跌且RSI/BOLL未过冷，仅记录不阻断"
        return (
            "LONG",
            score,
            reason,
            components(
                "high_volume_down_rebound_long",
                1.0,
                base_points=34.0,
                applied_volume_points=volume_points,
                applied_move_points=move_points,
                applied_trend_points=max(-trend_score, 0.0) * 10.0,
                close_points=close_points,
            ),
        )

    if volume_state == "HIGH" and direction == "FLAT" and position != "LOW":
        return (
            "WAIT",
            0.0,
            "量增价平：胜率未覆盖事件合约赔率，仅预警观察不开单",
            components("high_volume_flat_wait"),
        )

    if volume_state == "NORMAL" and direction == "UP":
        score = 20.0 + move_points * 0.8 + max(trend_score, 0.0) * 8.0
        return (
            "LONG",
            score,
            "量平价升：趋势延续但量能未放大",
            components(
                "normal_volume_up_long",
                1.0,
                base_points=20.0,
                applied_move_points=move_points * 0.8,
                applied_trend_points=max(trend_score, 0.0) * 8.0,
            ),
        )

    if volume_state == "NORMAL" and direction == "DOWN":
        confirmed, note = confirm_short_setup(
            technical,
            mtf_10m_bias,
            has_lower_reclaim,
            indicator_profile,
            require_bollinger_room=False,
        )
        if not confirmed:
            return (
                "WAIT",
                0.0,
                f"量平价跌：SHORT确认不足（{note}），等待",
                components("normal_volume_down_unconfirmed_wait"),
            )
        indicator_points = short_indicator_points(technical, mtf_10m_bias, mtf_30m_bias)
        score = -(18.0 + move_points * 0.8 + max(-trend_score, 0.0) * 8.0 + indicator_points)
        return (
            "SHORT",
            score,
            "量平价跌：MACD/RSI确认弱势延续，动态评分偏空",
            components(
                "normal_volume_down_short",
                -1.0,
                base_points=18.0,
                applied_move_points=move_points * 0.8,
                applied_trend_points=max(-trend_score, 0.0) * 8.0,
                indicator_points=indicator_points,
            ),
        )

    return (
        "WAIT",
        0.0,
        f"{_cn_position(position)}{_cn_volume(volume_state)}{_cn_direction(direction)}：信号不足",
        components("no_setup_wait"),
    )


def _fear_falling_trend_short_confirmed(
    timeframe_minutes: int,
    regime: str,
    position: str,
    volume_state: str,
    direction: str,
    technical: TechnicalContext,
    has_lower_reclaim: bool,
) -> bool:
    return (
        timeframe_minutes == 10
        and regime == "FEAR_FALLING"
        and position == "MID"
        and volume_state == "HIGH"
        and direction == "DOWN"
        and not has_lower_reclaim
        and technical.rsi >= 45.0
        and technical.bollinger_position >= 0.35
        and (technical.macd_histogram < 0.0 or technical.macd_histogram_delta < 0.0)
    )


def _trend_strict_rebound_risk(
    timeframe_minutes: int,
    position: str,
    volume_state: str,
    direction: str,
    technical: TechnicalContext,
    mtf_10m_bias: float,
    mtf_30m_bias: float,
    fear_greed_value: int | None,
    has_lower_reclaim: bool,
) -> bool:
    if not _is_mid_rebound_candidate(timeframe_minutes, position, volume_state, direction, has_lower_reclaim):
        return False
    if technical.rsi >= 45.0:
        return True
    return (
        mtf_10m_bias > 0.0
        and mtf_30m_bias > 0.0
        and fear_greed_value is not None
        and fear_greed_value > 0
        and fear_greed_value <= 25
    )


def _trend_broad_rebound_risk(
    timeframe_minutes: int,
    position: str,
    volume_state: str,
    direction: str,
    technical: TechnicalContext,
    mtf_10m_bias: float,
    mtf_30m_bias: float,
    fear_greed_value: int | None,
    has_lower_reclaim: bool,
    strict_rebound_risk: bool | None = None,
) -> bool:
    if not _is_mid_rebound_candidate(timeframe_minutes, position, volume_state, direction, has_lower_reclaim):
        return False
    strict = (
        _trend_strict_rebound_risk(
            timeframe_minutes,
            position,
            volume_state,
            direction,
            technical,
            mtf_10m_bias,
            mtf_30m_bias,
            fear_greed_value,
            has_lower_reclaim,
        )
        if strict_rebound_risk is None
        else strict_rebound_risk
    )
    if strict:
        return True
    return (mtf_10m_bias > 0.0 and mtf_30m_bias > 0.0) or technical.bollinger_position >= 0.35


def _is_mid_rebound_candidate(
    timeframe_minutes: int,
    position: str,
    volume_state: str,
    direction: str,
    has_lower_reclaim: bool,
) -> bool:
    return (
        timeframe_minutes == 10
        and position == "MID"
        and volume_state == "HIGH"
        and direction == "DOWN"
        and not has_lower_reclaim
    )


def _is_trend_broad_short_reason(reason: str) -> bool:
    return "趋势候选顺势SHORT" in reason and "BROAD_ONLY" in reason


def _normal_down_short_extension_edge(timeframe_minutes: int, threshold_segment: str) -> SessionEdge | None:
    return NORMAL_DOWN_SHORT_EXTENSION_EDGE_BY_TIMEFRAME.get(timeframe_minutes, {}).get(threshold_segment)


def _normal_down_short_override_reason(
    raw_direction: str,
    reason: str,
    *,
    timeframe_minutes: int,
    threshold_segment: str,
    score_abs: float,
    threshold: float,
) -> str | None:
    if raw_direction != "SHORT":
        return None
    if timeframe_minutes != 10:
        return None
    if _normal_down_short_extension_edge(timeframe_minutes, threshold_segment) is None:
        return None
    if "量平价跌" not in reason or "动态评分偏空" not in reason:
        return None

    raw_edge = score_abs - threshold
    return (
        "量平价跌SHORT扩展：WD-02/WD-23审计回放占优，"
        f"原始边际 {raw_edge:.1f}，重估边际为 {NORMAL_DOWN_SHORT_EXTENSION_MIN_EDGE:.1f}，顺势试空"
    )


def _long_rebound_guard_reason(
    raw_direction: str,
    reason: str,
    edge: float,
    rsi: float,
    bollinger_position: float,
    volume_ratio: float,
    mtf_10m_bias: float,
) -> str | None:
    if raw_direction != "LONG" or "放量急跌反抽" not in reason:
        return None

    failures = []
    if rsi >= LONG_REBOUND_MAX_RSI:
        failures.append(f"RSI {rsi:.1f} >= {LONG_REBOUND_MAX_RSI:.1f}")
    if bollinger_position >= LONG_REBOUND_MAX_BOLLINGER_POSITION:
        failures.append(
            f"BOLL位置 {bollinger_position:.3f} >= {LONG_REBOUND_MAX_BOLLINGER_POSITION:.3f}"
        )
    if edge < LONG_REBOUND_MIN_EDGE:
        failures.append(f"边际 {edge:.1f} < {LONG_REBOUND_MIN_EDGE:.1f}")
    if volume_ratio >= LONG_REBOUND_MAX_VOLUME_RATIO:
        failures.append(f"量比 {volume_ratio:.3f} >= {LONG_REBOUND_MAX_VOLUME_RATIO:.3f}")
    if mtf_10m_bias >= LONG_REBOUND_MAX_MTF_10M_BIAS:
        failures.append(f"10m偏向 {mtf_10m_bias:.3f} >= {LONG_REBOUND_MAX_MTF_10M_BIAS:.3f}")

    if not failures:
        return None
    return "急跌反抽过滤：" + "，".join(failures) + "，不开单"


def _drop_reclaim_mirror_short_observation(signal: Signal) -> Signal | None:
    if signal.timeframe_minutes != 10:
        return None
    if "放量急跌反抽" not in signal.reason:
        return None
    if signal.price_position < 0.35:
        return None
    if signal.close_strength >= 0.35:
        return None
    if signal.rsi < 38.0 and signal.bollinger_position < 0.10:
        return None

    edge = max(0.0, abs(signal.score) - signal.threshold)
    score = round(-(58.0 + min(edge, 24.0) + max(signal.rsi - 38.0, 0.0) * 0.35), 4)
    threshold = 58.0
    return _signal(
        "WAIT",
        10,
        _level(abs(score), threshold),
        (
            "急跌反抽镜像SHORT观察：中位急跌收弱且未充分过冷，"
            "用于验证亏损LONG形态是否可转为空单观察"
        ),
        signal.price,
        signal.open_time,
        signal.volume_ratio,
        signal.price_position,
        signal.price_change_pct,
        score,
        threshold,
        signal.volume_threshold,
        signal.move_threshold_pct,
        signal.close_strength,
        signal.analysis_window_minutes,
        signal.threshold_window_minutes,
        signal.threshold_segment,
        signal.mtf_10m_bias,
        signal.mtf_30m_bias,
        signal.macd_histogram,
        signal.macd_histogram_delta,
        signal.rsi,
        signal.bollinger_position,
        signal.bollinger_width,
        signal.indicator_profile_segment,
        signal.indicator_profile_sample_size,
        signal.rsi_lower_threshold,
        signal.rsi_upper_threshold,
        signal.bollinger_lower_threshold,
        signal.bollinger_upper_threshold,
        signal.macd_histogram_threshold,
        signal.macd_delta_threshold,
        signal.fear_greed_value,
        signal.fear_greed_classification,
        signal.fear_greed_trend,
        signal.fear_greed_average_30d,
        signal.fear_greed_adjustment,
        False,
        0,
        0.0,
        0.0,
        0.0,
        signal.regime,
        f"{signal.risk_flags},DROP_RECLAIM_MIRROR_SHORT_OBSERVE".strip(","),
        "short_observe",
        "drop_reclaim_mirror_short_observe",
        "SHORT",
        True,
        _profile_key("short_observe", "SHORT", signal.threshold_segment),
    )


def _failed_breakout_observation(
    primary: Signal,
    history: Sequence[Kline],
    recent: Sequence[Kline],
    latest: Kline,
) -> Signal | None:
    if primary.timeframe_minutes != 10 or len(history) < 120 or not recent:
        return None

    lookback = history[-120:]
    prior_high = max(item.high for item in lookback)
    prior_low = min(item.low for item in lookback)
    candle_strength = _candle_close_strength(latest)
    recent_high = max(item.high for item in recent)
    recent_low = min(item.low for item in recent)
    score_abs = 0.0
    direction = ""
    reason = ""
    family = ""
    tag = ""

    if (
        recent_high > prior_high
        and latest.close <= prior_high
        and candle_strength <= 0.45
        and primary.bollinger_position >= 0.50
        and primary.macd_histogram < 0.0
    ):
        score_abs = 58.0 + min((recent_high / prior_high - 1.0) * 10_000, 28.0) if prior_high else 58.0
        direction = "SHORT"
        reason = "冲高失败SHORT观察：10分钟窗口突破120分钟高点后收回，BOLL>=0.5且MACD柱<0"
        family = "failed_breakout"
        tag = "failed_high_120m_short_observe"
    elif (
        recent_low < prior_low
        and latest.close >= prior_low
        and candle_strength >= 0.55
        and primary.bollinger_position <= 0.35
        and primary.close_strength <= 0.35
    ):
        score_abs = 58.0 + min((1.0 - recent_low / prior_low) * 10_000, 28.0) if prior_low else 58.0
        direction = "LONG"
        reason = "破低收回LONG观察：10分钟窗口跌破120分钟低点后收回，BOLL<=0.35且10m收盘偏弱"
        family = "failed_breakout"
        tag = "failed_low_120m_long_observe"
    else:
        return None

    if primary.threshold_segment not in FAILED_BREAKOUT_OBSERVATION_SEGMENTS.get(tag, set()):
        return None

    score = -round(score_abs, 4) if direction == "SHORT" else round(score_abs, 4)
    threshold = 58.0
    return _signal(
        "WAIT",
        10,
        _level(abs(score), threshold),
        reason,
        primary.price,
        primary.open_time,
        primary.volume_ratio,
        primary.price_position,
        primary.price_change_pct,
        score,
        threshold,
        primary.volume_threshold,
        primary.move_threshold_pct,
        primary.close_strength,
        primary.analysis_window_minutes,
        primary.threshold_window_minutes,
        primary.threshold_segment,
        primary.mtf_10m_bias,
        primary.mtf_30m_bias,
        primary.macd_histogram,
        primary.macd_histogram_delta,
        primary.rsi,
        primary.bollinger_position,
        primary.bollinger_width,
        primary.indicator_profile_segment,
        primary.indicator_profile_sample_size,
        primary.rsi_lower_threshold,
        primary.rsi_upper_threshold,
        primary.bollinger_lower_threshold,
        primary.bollinger_upper_threshold,
        primary.macd_histogram_threshold,
        primary.macd_delta_threshold,
        primary.fear_greed_value,
        primary.fear_greed_classification,
        primary.fear_greed_trend,
        primary.fear_greed_average_30d,
        primary.fear_greed_adjustment,
        False,
        0,
        0.0,
        0.0,
        0.0,
        primary.regime,
        f"{primary.risk_flags},FAILED_BREAKOUT_OBSERVE".strip(","),
        family,
        tag,
        direction,
        True,
        _profile_key(family, direction, primary.threshold_segment),
    )


def _dedupe_observation_signals(signals: Sequence[Signal]) -> list[Signal]:
    by_tag: dict[str, Signal] = {}
    for signal in signals:
        if signal.observe_direction not in {"LONG", "SHORT"}:
            continue
        by_tag.setdefault(signal.strategy_tag, signal)
    return list(by_tag.values())


def _volume_context(
    history: Sequence[Kline], recent: Sequence[Kline], window_size: int, threshold_segment: str
) -> tuple[float, float, float, float, float, float]:
    scoped_history = history[-DYNAMIC_PROFILE_LOOKBACK_MINUTES:]
    baseline_volumes = _rolling_volume_sums(scoped_history, window_size, threshold_segment)
    if len(baseline_volumes) < 20:
        baseline_volumes = _rolling_volume_sums(scoped_history, window_size, None)
    baseline_volumes = [item for item in baseline_volumes if item > 0]
    recent_volume = sum(item.volume for item in recent)
    if not baseline_volumes:
        return 1.0, 1.5, 0.75, 0.0, recent_volume, 0.0

    baseline = median(baseline_volumes)
    volume_ratio = recent_volume / baseline if baseline > 0 else 1.0
    q75 = _percentile(baseline_volumes, 75)
    q25 = _percentile(baseline_volumes, 25)
    mad = median(abs(item - baseline) for item in baseline_volumes)
    noise = mad / baseline if baseline > 0 else 0.0
    high_threshold = _clamp(q75 / baseline + 0.2 + noise * 0.35, 1.25, 2.4) if baseline > 0 else 1.5
    low_threshold = _clamp(q25 / baseline - 0.1, 0.45, 0.8) if baseline > 0 else 0.75
    return volume_ratio, high_threshold, low_threshold, noise, recent_volume, baseline


def _window_returns(klines: Sequence[Kline], window_size: int, threshold_segment: str | None = None) -> list[float]:
    returns: list[float] = []
    if window_size <= 0 or len(klines) < window_size:
        return returns
    for start in range(0, len(klines) - window_size + 1):
        window = klines[start : start + window_size]
        if threshold_segment is not None and _threshold_segment(window[-1].close_time) != threshold_segment:
            continue
        window_open = window[0].open
        if window_open > 0:
            returns.append((window[-1].close - window_open) / window_open)
    if threshold_segment is not None and len(returns) < 20:
        return _window_returns(klines, window_size, None)
    return returns


def _dynamic_move_threshold(returns: Sequence[float], window_size: int) -> float:
    if not returns:
        return 0.0015 * (window_size / 10.0) ** 0.5
    typical = median(abs(item) for item in returns)
    floor = 0.0015 * (window_size / 10.0) ** 0.5
    return max(floor, typical * 1.6)


def _dynamic_trade_threshold(returns: Sequence[float], volume_noise: float) -> float:
    typical = median(abs(item) for item in returns) if returns else 0.001
    volatility_penalty = _clamp(typical * 2500.0, 0.0, 18.0)
    volume_penalty = _clamp(volume_noise * 14.0, 0.0, 8.0)
    return round(64.0 + volatility_penalty + volume_penalty, 1)


def _dynamic_direction_threshold(base_threshold: float, direction: str) -> float:
    if direction == "SHORT":
        return round(_clamp(base_threshold + SHORT_THRESHOLD_PREMIUM, 0.0, 95.0), 1)
    return base_threshold


def _fear_greed_threshold_adjustment(direction: str, context: FearGreedContext | None) -> float:
    if context is None or context.value <= 0:
        return 0.0

    adjustment = 0.0
    if direction == "SHORT":
        if context.value <= 25:
            adjustment += 6.0
        elif context.value <= 45:
            adjustment += 3.0
        if context.average_30d and context.value <= context.average_30d - 10:
            adjustment += 1.0
        if context.trend.lower() == "falling":
            adjustment += 1.0
    elif direction == "LONG":
        if context.value >= 75:
            adjustment += 6.0
        elif context.value >= 60:
            adjustment += 3.0
        if context.average_30d and context.value >= context.average_30d + 10:
            adjustment += 1.0
        if context.trend.lower() == "rising":
            adjustment += 1.0
    return round(_clamp(adjustment, 0.0, 9.0), 1)


def _regime_label(
    context: FearGreedContext | None,
    technical: TechnicalContext,
    mtf_30m_bias: float,
) -> str:
    if context is not None and context.value > 0:
        trend = context.trend.lower()
        if context.value <= 45:
            if trend == "rising":
                return "FEAR_RISING"
            if trend == "falling":
                return "FEAR_FALLING"
            return "FEAR_FLAT"
        if context.value >= 60:
            if trend == "rising":
                return "GREED_RISING"
            if trend == "falling":
                return "GREED_FALLING"
            return "GREED_FLAT"
    if technical.bollinger_width >= 0.02:
        return "HIGH_VOL"
    if technical.bollinger_width <= 0.002 and abs(mtf_30m_bias) < 1.0:
        return "LOW_VOL_RANGE"
    return "NEUTRAL"


def _regime_threshold_adjustment(
    direction: str,
    regime: str,
    technical: TechnicalContext,
) -> float:
    adjustment = 0.0
    if regime == "FEAR_FALLING":
        if direction == "SHORT":
            adjustment += 4.0
    elif regime == "FEAR_RISING":
        if direction == "LONG" and technical.bollinger_position >= 0.88:
            adjustment += 3.0
    elif regime == "GREED_RISING" and direction == "LONG":
        adjustment += 2.0
    return round(_clamp(adjustment, 0.0, 6.0), 1)


def _risk_flags(
    regime: str,
    regime_adjustment: float,
    reason: str = "",
    *,
    raw_direction: str = "",
    threshold_segment: str = "",
    level: str = "",
    price_position: float = 0.5,
    price_change_pct: float = 0.0,
    rsi: float = 50.0,
    mtf_10m_bias: float = 0.0,
    mtf_30m_bias: float = 0.0,
) -> str:
    flags = [regime]
    if regime_adjustment > 0:
        flags.append(f"regime_threshold+{regime_adjustment:.1f}")
    if "SHORT观察" in reason:
        flags.append("SHORT_OBSERVE")
    if "趋势过滤禁多" in reason and "STRICT" in reason:
        flags.append("TREND_STRICT_WAIT")
    if _is_trend_broad_short_reason(reason):
        flags.append("TREND_BROAD_SHORT")
    if "急跌反抽过滤" in reason:
        flags.append("REBOUND_LONG_GUARD")
    if "量平价跌SHORT扩展" in reason:
        flags.append("NORMAL_DOWN_SHORT_EXTENSION")
    if "极端急跌反抽" in reason:
        flags.append("EXTREME_DROP_RECLAIM")
    flags.extend(
        _sample_hint_flags(
            raw_direction=raw_direction,
            reason=reason,
            threshold_segment=threshold_segment,
            level=level,
            price_position=price_position,
            price_change_pct=price_change_pct,
            rsi=rsi,
            mtf_10m_bias=mtf_10m_bias,
            mtf_30m_bias=mtf_30m_bias,
        )
    )
    return ",".join(flags)


def _sample_hint_flags(
    *,
    raw_direction: str,
    reason: str,
    threshold_segment: str,
    level: str,
    price_position: float,
    price_change_pct: float,
    rsi: float,
    mtf_10m_bias: float,
    mtf_30m_bias: float,
) -> list[str]:
    if raw_direction != "LONG" or "放量急跌反抽" not in reason:
        return []

    flags = []
    if level == "A":
        flags.append("SAMPLE_WEAK_LEVEL_A_REBOUND")
    if threshold_segment in {"WD-00", "WD-18", "WD-22"}:
        flags.append(f"SAMPLE_WEAK_SEGMENT_{threshold_segment}")
    if 0.35 <= price_position < 0.65:
        flags.append("SAMPLE_WEAK_MID_POSITION_REBOUND")
    if -0.002 <= price_change_pct < -0.001:
        flags.append("SAMPLE_WEAK_SHALLOW_DROP_REBOUND")
    if rsi >= 45.0:
        flags.append("SAMPLE_WEAK_HIGH_RSI_REBOUND")
    if mtf_10m_bias >= 0.0 and mtf_30m_bias >= 0.0:
        flags.append("SAMPLE_WEAK_DUAL_UP_BIAS_REBOUND")
    return flags


def _strategy_identity(
    raw_direction: str,
    reason: str,
    price_change_pct: float,
    volume_ratio: float,
    rsi: float,
    bollinger_position: float,
    has_lower_reclaim: bool,
    has_upper_rejection: bool,
) -> tuple[str, str, str, bool]:
    if "放量急跌反抽" in reason:
        if _is_extreme_drop_reclaim(price_change_pct, volume_ratio, rsi, bollinger_position, has_lower_reclaim):
            return "drop_reclaim", "drop_reclaim_extreme_10m_120bps_v1.5_rsi30_boll0.1", "LONG", False
        return "reversal", "drop_reclaim_live_guarded", "LONG", False
    if "量平价跌SHORT扩展" in reason:
        return "short_extension", "normal_down_short_extension_observe", "SHORT", True
    if "趋势候选顺势SHORT" in reason:
        return "short_observe", "broad_rebound_short_observe", "SHORT", True
    if "高位放量下跌" in reason:
        return "short_observe", "high_volume_drop_short_observe", "SHORT", True
    if "高位放量滞涨" in reason:
        return "rise_reject", "high_stall_observe", "SHORT", True
    if "低位放量承接" in reason:
        return "failed_low", "low_volume_reclaim_observe", "LONG", True
    if "低位放量上涨" in reason:
        return "low_rise_observe", "low_volume_rise_observe", "LONG", True
    if "量增价升" in reason:
        return "momentum_observe", "high_volume_rise_observe", "LONG", True
    if "量平价跌" in reason:
        return "short_observe", "normal_down_short_observe", "SHORT", True
    if raw_direction == "SHORT" or has_upper_rejection:
        return "short_observe", "generic_short_observe", "SHORT", True
    if raw_direction == "LONG" or has_lower_reclaim:
        return "long_observe", "generic_long_observe", "LONG", raw_direction != "LONG"
    return "unknown", "unknown", raw_direction if raw_direction in {"LONG", "SHORT"} else "", False


def _is_extreme_drop_reclaim(
    price_change_pct: float,
    volume_ratio: float,
    rsi: float,
    bollinger_position: float,
    has_lower_reclaim: bool,
) -> bool:
    return (
        price_change_pct <= -EXTREME_DROP_RECLAIM_MIN_DROP_PCT
        and volume_ratio >= EXTREME_DROP_RECLAIM_MIN_VOLUME_RATIO
        and has_lower_reclaim
        and (rsi <= EXTREME_DROP_RECLAIM_MAX_RSI or bollinger_position <= EXTREME_DROP_RECLAIM_MAX_BOLLINGER_POSITION)
    )


def _profile_key(strategy_family: str, direction: str, threshold_segment: str) -> str:
    clean_direction = direction if direction in {"LONG", "SHORT"} else "WAIT"
    return f"{strategy_family}|{clean_direction}|{threshold_segment}"


def _session_adjusted_threshold(
    base_threshold: float,
    edge: SessionEdge | None,
    allow_unprofiled: bool = False,
) -> float:
    adjustment = 0.0
    if edge is None:
        if not allow_unprofiled:
            adjustment += 8.0
    elif edge.win_rate >= 0.68 and edge.ev >= 2.0:
        adjustment -= 3.0
    elif edge.win_rate < 0.60 or edge.ev < 1.0:
        adjustment += 3.0
    return round(_clamp(base_threshold + adjustment, 58.0, 88.0), 1)


def _session_min_edge(
    edge: SessionEdge | None,
    direction: str = "LONG",
    allow_unprofiled: bool = False,
) -> float:
    minimum = MIN_TRADE_EDGE
    if direction == "SHORT":
        minimum += SHORT_EDGE_PREMIUM
    if edge is None:
        return minimum if allow_unprofiled else minimum + 8.0
    if edge.win_rate >= 0.68 and edge.ev >= 2.0:
        minimum -= 2.0
    elif edge.win_rate < 0.60 or edge.ev < 1.0:
        minimum += 4.0
    return round(_clamp(minimum, 8.0, 18.0), 1)


def _rolling_volume_sums(
    klines: Sequence[Kline], window_size: int, threshold_segment: str | None = None
) -> list[float]:
    if window_size <= 0 or len(klines) < window_size:
        return []
    values = []
    rolling_sum = sum(item.volume for item in klines[:window_size])
    for start in range(0, len(klines) - window_size + 1):
        end = start + window_size - 1
        if threshold_segment is not None and _threshold_segment(klines[end].close_time) != threshold_segment:
            if start + window_size < len(klines):
                rolling_sum += klines[start + window_size].volume - klines[start].volume
            continue
        values.append(rolling_sum)
        if start + window_size < len(klines):
            rolling_sum += klines[start + window_size].volume - klines[start].volume
    return values


def _window_change(recent: Sequence[Kline]) -> float:
    window_open = recent[0].open
    return (recent[-1].close - window_open) / window_open if window_open else 0.0


def _price_position(history: Sequence[Kline], latest_close: float) -> float:
    scoped_history = history[-1440:] if len(history) > 1440 else history
    prior_high = max(item.high for item in scoped_history)
    prior_low = min(item.low for item in scoped_history)
    prior_range = prior_high - prior_low
    if prior_range <= 0:
        return 0.5
    return _clamp((latest_close - prior_low) / prior_range, 0.0, 1.0)


def _price_direction(price_change_pct: float, move_threshold_pct: float) -> str:
    if price_change_pct >= move_threshold_pct:
        return "UP"
    if price_change_pct <= -move_threshold_pct:
        return "DOWN"
    return "FLAT"


def _position_bucket(price_position: float) -> str:
    if price_position >= 0.8:
        return "HIGH"
    if price_position <= 0.2:
        return "LOW"
    return "MID"


def _volume_bucket(volume_ratio: float, high_threshold: float, low_threshold: float) -> str:
    if volume_ratio >= high_threshold:
        return "HIGH"
    if volume_ratio <= low_threshold:
        return "LOW"
    return "NORMAL"


def _range_close_strength(recent: Sequence[Kline], close: float) -> float:
    recent_high = max(item.high for item in recent)
    recent_low = min(item.low for item in recent)
    recent_range = recent_high - recent_low
    if recent_range <= 0:
        return 0.5
    return _clamp((close - recent_low) / recent_range, 0.0, 1.0)


def _candle_close_strength(kline: Kline) -> float:
    candle_range = kline.high - kline.low
    if candle_range <= 0:
        return 0.5
    return _clamp((kline.close - kline.low) / candle_range, 0.0, 1.0)


def _trend_consistency(recent: Sequence[Kline]) -> float:
    ups = 0
    downs = 0
    for previous, current in zip(recent, recent[1:]):
        if current.close > previous.close:
            ups += 1
        elif current.close < previous.close:
            downs += 1
    total = ups + downs
    if total == 0:
        return 0.0
    return (ups - downs) / total


def _volume_points(volume_ratio: float, volume_threshold: float) -> float:
    if volume_threshold <= 0:
        return 0.0
    return _clamp(24.0 + (volume_ratio / volume_threshold - 1.0) * 34.0, 0.0, 42.0)


def _move_points(price_change_pct: float, move_threshold_pct: float) -> float:
    if move_threshold_pct <= 0:
        return 0.0
    return _clamp(abs(price_change_pct) / move_threshold_pct * 12.0, 0.0, 30.0)


def _level(score_abs: float, threshold: float) -> str:
    if score_abs >= threshold + 18.0:
        return "S"
    if score_abs >= threshold:
        return "A"
    return "B"


def _signal(
    direction: str,
    timeframe_minutes: int,
    level: str,
    reason: str,
    price: float,
    open_time: int,
    volume_ratio: float = 0.0,
    price_position: float = 0.5,
    price_change_pct: float = 0.0,
    score: float = 0.0,
    threshold: float = 0.0,
    volume_threshold: float = 1.5,
    move_threshold_pct: float = 0.0,
    close_strength: float = 0.5,
    analysis_window_minutes: int = 0,
    threshold_window_minutes: int = 0,
    threshold_segment: str = "GLOBAL",
    mtf_10m_bias: float = 0.0,
    mtf_30m_bias: float = 0.0,
    macd_histogram: float = 0.0,
    macd_histogram_delta: float = 0.0,
    rsi: float = 50.0,
    bollinger_position: float = 0.5,
    bollinger_width: float = 0.0,
    indicator_profile_segment: str = "GLOBAL",
    indicator_profile_sample_size: int = 0,
    rsi_lower_threshold: float = SHORT_MIN_RSI,
    rsi_upper_threshold: float = SHORT_MAX_RSI,
    bollinger_lower_threshold: float = SHORT_MIN_BOLLINGER_POSITION,
    bollinger_upper_threshold: float = SHORT_MAX_BOLLINGER_POSITION,
    macd_histogram_threshold: float = 0.0,
    macd_delta_threshold: float = 0.0,
    fear_greed_value: int | None = None,
    fear_greed_classification: str = "",
    fear_greed_trend: str = "",
    fear_greed_average_30d: float = 0.0,
    fear_greed_adjustment: float = 0.0,
    session_allowed: bool = False,
    session_sample_size: int = 0,
    session_win_rate: float = 0.0,
    session_ev: float = 0.0,
    session_edge_min: float = 0.0,
    regime: str = "UNKNOWN",
    risk_flags: str = "",
    strategy_family: str = "unknown",
    strategy_tag: str = "unknown",
    observe_direction: str = "",
    observe_only: bool = False,
    profile_key: str = "",
    decision_inputs: dict[str, object] | None = None,
) -> Signal:
    return Signal(
        direction=direction,
        timeframe_minutes=timeframe_minutes,
        level=level,
        reason=reason,
        price=price,
        open_time=open_time,
        volume_ratio=round(volume_ratio, 3),
        price_position=round(price_position, 3),
        price_change_pct=round(price_change_pct, 5),
        score=round(score, 1),
        threshold=round(threshold, 1),
        calculated_threshold=round(threshold, 1),
        volume_threshold=round(volume_threshold, 3),
        move_threshold_pct=round(move_threshold_pct, 5),
        close_strength=round(close_strength, 3),
        analysis_window_minutes=analysis_window_minutes,
        threshold_window_minutes=threshold_window_minutes,
        threshold_segment=threshold_segment,
        mtf_10m_bias=round(mtf_10m_bias, 4),
        mtf_30m_bias=round(mtf_30m_bias, 4),
        macd_histogram=round(macd_histogram, 6),
        macd_histogram_delta=round(macd_histogram_delta, 6),
        rsi=round(rsi, 2),
        bollinger_position=round(bollinger_position, 4),
        bollinger_width=round(bollinger_width, 4),
        indicator_profile_segment=indicator_profile_segment,
        indicator_profile_sample_size=indicator_profile_sample_size,
        rsi_lower_threshold=round(rsi_lower_threshold, 1),
        rsi_upper_threshold=round(rsi_upper_threshold, 1),
        bollinger_lower_threshold=round(bollinger_lower_threshold, 3),
        bollinger_upper_threshold=round(bollinger_upper_threshold, 3),
        macd_histogram_threshold=round(macd_histogram_threshold, 6),
        macd_delta_threshold=round(macd_delta_threshold, 6),
        fear_greed_value=fear_greed_value,
        fear_greed_classification=fear_greed_classification,
        fear_greed_trend=fear_greed_trend,
        fear_greed_average_30d=round(fear_greed_average_30d, 2),
        fear_greed_adjustment=round(fear_greed_adjustment, 1),
        session_allowed=session_allowed,
        session_sample_size=session_sample_size,
        session_win_rate=round(session_win_rate, 4),
        session_ev=round(session_ev, 4),
        session_edge_min=round(session_edge_min, 1),
        regime=regime,
        risk_flags=risk_flags,
        strategy_family=strategy_family,
        strategy_tag=strategy_tag,
        observe_direction=observe_direction,
        observe_only=observe_only,
        profile_key=profile_key,
        decision_inputs=decision_inputs if decision_inputs is not None else {},
    )


def _session_edge(timeframe_minutes: int, threshold_segment: str, direction: str = "LONG") -> SessionEdge | None:
    if direction == "SHORT":
        return SHORT_SESSION_EDGE_BY_TIMEFRAME.get(timeframe_minutes, {}).get(threshold_segment)
    if direction == "LONG":
        return SESSION_EDGE_BY_TIMEFRAME.get(timeframe_minutes, {}).get(threshold_segment)
    return None


def _candidate_rank(signal: Signal) -> float:
    edge = abs(signal.score) - signal.threshold
    session_quality = signal.session_ev * 3.0 + signal.session_win_rate * 10.0
    return edge + session_quality


def _cn_position(position: str) -> str:
    return {"HIGH": "高位", "LOW": "低位", "MID": "中位"}[position]


def _cn_volume(volume_state: str) -> str:
    return {"HIGH": "放量", "LOW": "缩量", "NORMAL": "平量"}[volume_state]


def _cn_direction(direction: str) -> str:
    return {"UP": "上涨", "DOWN": "下跌", "FLAT": "横盘"}[direction]
