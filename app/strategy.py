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


@dataclass(frozen=True)
class SessionEdge:
    sample_size: int
    win_rate: float
    ev: float


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
    30: {
        "WD-00": SessionEdge(11, 0.6364, 1.4545),
        "WD-05": SessionEdge(15, 0.6667, 2.0000),
        "WD-15": SessionEdge(36, 0.6389, 1.5000),
        "WE-21": SessionEdge(8, 0.7500, 3.5000),
        "WE-23": SessionEdge(8, 0.7500, 3.5000),
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
    if not klines:
        return _signal("WAIT", timeframe_minutes, "B", "暂无K线数据", 0.0, 0)

    if len(klines) < timeframe_minutes * 3:
        latest = klines[-1]
        return _signal("WAIT", timeframe_minutes, "B", "历史K线不足，等待累计样本", latest.close, latest.open_time)

    latest = klines[-1]
    recent_size = timeframe_minutes
    recent = list(klines[-recent_size:])
    history = list(klines[:-recent_size]) or list(klines[:-1])
    threshold_segment = _threshold_segment(latest.close_time)

    volume_ratio, volume_threshold, low_volume_threshold, volume_noise = _volume_context(
        history, recent, recent_size, threshold_segment
    )
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
    has_upper_rejection = candle_strength <= 0.35 and (latest.high - latest.close) > (latest.close - latest.low) * 1.2
    has_lower_reclaim = candle_strength >= 0.65 and (latest.close - latest.low) > (latest.high - latest.close) * 1.2
    trend_score = _trend_consistency(recent)
    base_threshold = _dynamic_trade_threshold(window_returns, volume_noise)

    raw_direction, score, reason = _score_setup(
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

    score = _clamp(score, -100.0, 100.0)
    session_edge = _session_edge(timeframe_minutes, threshold_segment, raw_direction)
    trend_broad_short = _is_trend_broad_short_reason(reason)
    threshold = _dynamic_direction_threshold(base_threshold, raw_direction)
    threshold = _session_adjusted_threshold(
        threshold,
        timeframe_minutes,
        session_edge,
        allow_unprofiled=trend_broad_short,
    )
    fear_greed_adjustment = _fear_greed_threshold_adjustment(raw_direction, fear_greed)
    regime_adjustment = _regime_threshold_adjustment(raw_direction, timeframe_minutes, regime, technical)
    threshold = round(_clamp(threshold + fear_greed_adjustment + regime_adjustment, 58.0, 95.0), 1)
    session_edge_min = _session_min_edge(
        timeframe_minutes,
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
        _risk_flags(regime, regime_adjustment, final_reason),
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
) -> tuple[str, float, str]:
    if volume_state == "LOW":
        if position == "LOW" and direction == "DOWN":
            return "WAIT", 0.0, "低位缩量下跌：买盘未明显承接，等待"
        return "WAIT", 0.0, f"{_cn_position(position)}缩量{_cn_direction(direction)}：量能不足，等待"

    volume_points = _volume_points(volume_ratio, volume_threshold)
    move_points = _move_points(price_change_pct, move_threshold_pct)
    trend_points = abs(trend_score) * 12.0

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
            return "WAIT", 0.0, f"高位放量下跌：SHORT确认不足（{note}），仅预警观察不开单"
        indicator_points = short_indicator_points(technical, mtf_10m_bias, mtf_30m_bias)
        close_points = (1.0 - close_strength) * 8.0
        score = -(30.0 + volume_points + move_points + max(-trend_score, 0.0) * 10.0 + close_points + indicator_points)
        return "SHORT", score, "高位放量下跌：MACD/RSI/BOLL确认卖压，动态评分偏空"

    if position == "HIGH" and volume_state == "HIGH" and (has_upper_rejection or direction == "FLAT"):
        return "WAIT", 0.0, "高位放量滞涨：回测胜率不足，仅预警观察不开单"

    if position == "LOW" and volume_state == "HIGH" and direction == "UP":
        return "WAIT", 0.0, "低位放量上涨：容易把低位反弹误判为确定性机会，回测未覆盖赔率，仅预警观察不开单"

    if position == "LOW" and volume_state == "HIGH" and direction == "DOWN" and has_lower_reclaim:
        return "WAIT", 0.0, "低位放量承接：三个月回测未覆盖赔率，仅预警观察不开单"

    if volume_state == "HIGH" and direction == "UP":
        return "WAIT", 0.0, "量增价升：回测未达到事件合约盈亏平衡，仅观察不开单"

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
            )
        if broad_rebound_risk:
            return (
                "SHORT",
                -score,
                "趋势候选顺势SHORT：BROAD_ONLY，双周期反弹中破位或BOLL未跌透，放弃急跌反抽LONG并顺势试空",
            )
        reason = "放量急跌反抽：回测显示急跌后后续窗口更偏反弹，动态评分偏多"
        if short_observe:
            reason += "；SHORT观察：恐慌下行中位急跌且RSI/BOLL未过冷，仅记录不阻断"
        return "LONG", score, reason

    if volume_state == "HIGH" and direction == "FLAT" and position != "LOW":
        return "WAIT", 0.0, "量增价平：胜率未覆盖事件合约赔率，仅预警观察不开单"

    if volume_state == "NORMAL" and direction == "UP":
        score = 20.0 + move_points * 0.8 + max(trend_score, 0.0) * 8.0
        return "LONG", score, "量平价升：趋势延续但量能未放大"

    if volume_state == "NORMAL" and direction == "DOWN":
        confirmed, note = confirm_short_setup(
            technical,
            mtf_10m_bias,
            has_lower_reclaim,
            indicator_profile,
            require_bollinger_room=False,
        )
        if not confirmed:
            return "WAIT", 0.0, f"量平价跌：SHORT确认不足（{note}），等待"
        indicator_points = short_indicator_points(technical, mtf_10m_bias, mtf_30m_bias)
        score = -(18.0 + move_points * 0.8 + max(-trend_score, 0.0) * 8.0 + indicator_points)
        return "SHORT", score, "量平价跌：MACD/RSI确认弱势延续，动态评分偏空"

    return "WAIT", 0.0, f"{_cn_position(position)}{_cn_volume(volume_state)}{_cn_direction(direction)}：信号不足"


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


def _volume_context(
    history: Sequence[Kline], recent: Sequence[Kline], window_size: int, threshold_segment: str
) -> tuple[float, float, float, float]:
    scoped_history = history[-DYNAMIC_PROFILE_LOOKBACK_MINUTES:]
    baseline_volumes = _rolling_volume_sums(scoped_history, window_size, threshold_segment)
    if len(baseline_volumes) < 20:
        baseline_volumes = _rolling_volume_sums(scoped_history, window_size, None)
    baseline_volumes = [item for item in baseline_volumes if item > 0]
    if not baseline_volumes:
        return 1.0, 1.5, 0.75, 0.0

    baseline = median(baseline_volumes)
    recent_volume = sum(item.volume for item in recent)
    volume_ratio = recent_volume / baseline if baseline > 0 else 1.0
    q75 = _percentile(baseline_volumes, 75)
    q25 = _percentile(baseline_volumes, 25)
    mad = median(abs(item - baseline) for item in baseline_volumes)
    noise = mad / baseline if baseline > 0 else 0.0
    high_threshold = _clamp(q75 / baseline + 0.2 + noise * 0.35, 1.25, 2.4) if baseline > 0 else 1.5
    low_threshold = _clamp(q25 / baseline - 0.1, 0.45, 0.8) if baseline > 0 else 0.75
    return volume_ratio, high_threshold, low_threshold, noise


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
    timeframe_minutes: int,
    regime: str,
    technical: TechnicalContext,
) -> float:
    adjustment = 0.0
    if regime == "FEAR_FALLING":
        if direction == "SHORT":
            adjustment += 4.0
        elif direction == "LONG" and timeframe_minutes == 30:
            adjustment += 3.0
    elif regime == "FEAR_RISING":
        if direction == "LONG" and technical.bollinger_position >= 0.88:
            adjustment += 3.0
    elif regime == "GREED_RISING" and direction == "LONG":
        adjustment += 2.0
    elif regime == "HIGH_VOL" and timeframe_minutes == 30:
        adjustment += 2.0
    return round(_clamp(adjustment, 0.0, 6.0), 1)


def _risk_flags(regime: str, regime_adjustment: float, reason: str = "") -> str:
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
    return ",".join(flags)


def _session_adjusted_threshold(
    base_threshold: float,
    timeframe_minutes: int,
    edge: SessionEdge | None,
    allow_unprofiled: bool = False,
) -> float:
    adjustment = 2.0 if timeframe_minutes == 30 else 0.0
    if edge is None:
        if not allow_unprofiled:
            adjustment += 8.0
    elif edge.win_rate >= 0.68 and edge.ev >= 2.0:
        adjustment -= 3.0
    elif edge.win_rate < 0.60 or edge.ev < 1.0:
        adjustment += 3.0
    return round(_clamp(base_threshold + adjustment, 58.0, 88.0), 1)


def _session_min_edge(
    timeframe_minutes: int,
    edge: SessionEdge | None,
    direction: str = "LONG",
    allow_unprofiled: bool = False,
) -> float:
    minimum = MIN_TRADE_EDGE + (2.0 if timeframe_minutes == 30 else 0.0)
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
    timeframe_penalty = 1.5 if signal.timeframe_minutes == 30 else 0.0
    return edge + session_quality - timeframe_penalty


def _cn_position(position: str) -> str:
    return {"HIGH": "高位", "LOW": "低位", "MID": "中位"}[position]


def _cn_volume(volume_state: str) -> str:
    return {"HIGH": "放量", "LOW": "缩量", "NORMAL": "平量"}[volume_state]


def _cn_direction(direction: str) -> str:
    return {"UP": "上涨", "DOWN": "下跌", "FLAT": "横盘"}[direction]
