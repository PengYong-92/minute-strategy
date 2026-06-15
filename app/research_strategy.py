import argparse
import json
import math
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from app.backtest import load_klines_from_zips
from app.models import Kline


BREAK_EVEN_WIN_RATE = 10.0 / 18.0


@dataclass(frozen=True)
class CandidateSignal:
    index: int
    direction: str
    family: str
    score: float
    reason: str


@dataclass(frozen=True)
class BarFeatures:
    index: int
    close_time: int
    close: float
    utc_hour: int
    beijing_hour: int
    beijing_bucket: str
    is_weekend: bool
    ret_1: float
    ret_3: float
    ret_5: float
    ret_10: float
    ret_20: float
    ret_5_z: float
    range_30_pct: float
    vol_ratio_5: float
    rsi_14: float
    boll_pos_20: float
    ema20: float
    ema60: float
    trend_strength: float
    close_strength: float
    upper_rejection: bool
    lower_reclaim: bool
    break_up_20: bool
    break_down_20: bool
    compression_30: bool


@dataclass(frozen=True)
class ReversalParams:
    z_min: float
    rsi_extreme: float
    boll_extreme: float
    min_vol_ratio: float
    require_rejection: bool


@dataclass(frozen=True)
class TrendParams:
    min_vol_ratio: float
    max_abs_z: float
    boll_limit: float
    require_compression: bool


@dataclass(frozen=True)
class MagicianParams:
    min_vol_ratio: float = 1.0
    max_vol_ratio: float = 2.2
    max_abs_z: float = 1.8
    min_trend_strength: float = 0.03
    long_rsi_min: float = 45.0
    long_rsi_max: float = 78.0
    short_rsi_min: float = 22.0
    short_rsi_max: float = 55.0
    long_boll_max: float = 0.92
    short_boll_min: float = 0.08
    enable_pullback: bool = True


def event_contract_pnl(
    direction: str,
    entry_price: float,
    exit_price: float,
    stake: float = 10.0,
    win_profit: float = 8.0,
) -> tuple[str, float]:
    if direction == "LONG":
        won = exit_price > entry_price
    elif direction == "SHORT":
        won = exit_price < entry_price
    else:
        raise ValueError(f"unknown direction: {direction}")
    return ("WIN", round(win_profit, 4)) if won else ("LOSS", round(-stake, 4))


def apply_min_gap(
    signals: Sequence[CandidateSignal],
    klines: Sequence[Kline],
    gap_minutes: int = 10,
) -> list[CandidateSignal]:
    kept: list[CandidateSignal] = []
    last_entry_time: int | None = None
    gap_ms = gap_minutes * 60_000
    for signal in sorted(signals, key=lambda item: (item.index, -item.score)):
        entry_time = klines[signal.index].close_time
        if last_entry_time is not None and entry_time - last_entry_time < gap_ms:
            continue
        kept.append(signal)
        last_entry_time = entry_time
    return kept


def summarize_trades(trades: Sequence[dict]) -> dict:
    wins = [item for item in trades if item["result"] == "WIN"]
    losses = [item for item in trades if item["result"] == "LOSS"]
    balance = round(sum(float(item["pnl"]) for item in trades), 4)
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    loss_streak = 0
    max_loss_streak = 0
    win_streak = 0
    max_win_streak = 0
    for trade in trades:
        equity = round(equity + float(trade["pnl"]), 4)
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)
        if trade["result"] == "LOSS":
            loss_streak += 1
            win_streak = 0
        else:
            win_streak += 1
            loss_streak = 0
        max_loss_streak = max(max_loss_streak, loss_streak)
        max_win_streak = max(max_win_streak, win_streak)
    total_staked = round(len(trades) * 10.0, 4)
    return {
        "total_orders": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades), 4) if trades else 0.0,
        "balance": balance,
        "avg_pnl": round(balance / len(trades), 4) if trades else 0.0,
        "total_staked": total_staked,
        "roi": round(balance / total_staked, 4) if total_staked else 0.0,
        "break_even_win_rate": round(BREAK_EVEN_WIN_RATE, 4),
        "max_drawdown": round(max_drawdown, 4),
        "max_loss_streak": max_loss_streak,
        "max_win_streak": max_win_streak,
    }


def compute_features(klines: Sequence[Kline]) -> list[BarFeatures]:
    closes = [item.close for item in klines]
    volumes = [item.volume for item in klines]
    ema20_values = _ema_series(closes, 20)
    ema60_values = _ema_series(closes, 60)
    rsi_values = _rsi_series(closes, 14)
    boll_values = _bollinger_position_series(closes, 20, 2.0)
    ret5_values = [_return_pct(closes, index, 5) for index in range(len(closes))]
    ret5_z_values = _rolling_zscore(ret5_values, 240)
    range30_values = _range_pct_series(klines, 30)
    range30_mean = _rolling_mean(range30_values, 240)
    vol_ratio_values = _volume_ratio_series(volumes, recent=5, baseline=240)

    features: list[BarFeatures] = []
    for index, kline in enumerate(klines):
        close_strength = _close_strength(kline)
        dt_utc = datetime.fromtimestamp(kline.close_time / 1000, timezone.utc)
        bjt_hour = (dt_utc.hour + 8) % 24
        bjt_day_shift = 1 if dt_utc.hour >= 16 else 0
        bjt_weekday = (dt_utc.weekday() + bjt_day_shift) % 7
        bucket_minute = (dt_utc.minute // 10) * 10
        features.append(
            BarFeatures(
                index=index,
                close_time=kline.close_time,
                close=kline.close,
                utc_hour=dt_utc.hour,
                beijing_hour=bjt_hour,
                beijing_bucket=f"BJT-{bjt_hour:02d}:{bucket_minute:02d}",
                is_weekend=bjt_weekday >= 5,
                ret_1=_return_pct(closes, index, 1),
                ret_3=_return_pct(closes, index, 3),
                ret_5=ret5_values[index],
                ret_10=_return_pct(closes, index, 10),
                ret_20=_return_pct(closes, index, 20),
                ret_5_z=ret5_z_values[index],
                range_30_pct=range30_values[index],
                vol_ratio_5=vol_ratio_values[index],
                rsi_14=rsi_values[index],
                boll_pos_20=boll_values[index],
                ema20=ema20_values[index],
                ema60=ema60_values[index],
                trend_strength=((ema20_values[index] - ema60_values[index]) / kline.close * 100.0)
                if kline.close
                else 0.0,
                close_strength=close_strength,
                upper_rejection=close_strength <= 0.35
                and (kline.high - kline.close) > (kline.close - kline.low) * 1.2,
                lower_reclaim=close_strength >= 0.65
                and (kline.close - kline.low) > (kline.high - kline.close) * 1.2,
                break_up_20=_break_up(klines, index, 20),
                break_down_20=_break_down(klines, index, 20),
                compression_30=range30_mean[index] > 0 and range30_values[index] < range30_mean[index] * 0.70,
            )
        )
    return features


def reversal_signals(features: Sequence[BarFeatures], params: ReversalParams) -> list[CandidateSignal]:
    signals: list[CandidateSignal] = []
    lower_rsi = 100.0 - params.rsi_extreme
    lower_boll = 1.0 - params.boll_extreme
    for item in features:
        if item.index < 260 or item.vol_ratio_5 < params.min_vol_ratio:
            continue
        short_confirmed = item.upper_rejection or item.ret_1 <= 0.0
        long_confirmed = item.lower_reclaim or item.ret_1 >= 0.0
        if (
            item.ret_5_z >= params.z_min
            and item.rsi_14 >= params.rsi_extreme
            and item.boll_pos_20 >= params.boll_extreme
            and (not params.require_rejection or short_confirmed)
        ):
            signals.append(
                CandidateSignal(
                    index=item.index,
                    direction="SHORT",
                    family="reversal",
                    score=round(abs(item.ret_5_z) + item.vol_ratio_5 + max(item.boll_pos_20 - 0.5, 0.0) * 2.0, 4),
                    reason=(
                        f"extreme_up_reversal z={item.ret_5_z:.2f} rsi={item.rsi_14:.1f} "
                        f"boll={item.boll_pos_20:.2f} vol={item.vol_ratio_5:.2f}"
                    ),
                )
            )
        elif (
            item.ret_5_z <= -params.z_min
            and item.rsi_14 <= lower_rsi
            and item.boll_pos_20 <= lower_boll
            and (not params.require_rejection or long_confirmed)
        ):
            signals.append(
                CandidateSignal(
                    index=item.index,
                    direction="LONG",
                    family="reversal",
                    score=round(abs(item.ret_5_z) + item.vol_ratio_5 + max(0.5 - item.boll_pos_20, 0.0) * 2.0, 4),
                    reason=(
                        f"extreme_down_reversal z={item.ret_5_z:.2f} rsi={item.rsi_14:.1f} "
                        f"boll={item.boll_pos_20:.2f} vol={item.vol_ratio_5:.2f}"
                    ),
                )
            )
    return signals


def trend_signals(features: Sequence[BarFeatures], params: TrendParams) -> list[CandidateSignal]:
    signals: list[CandidateSignal] = []
    lower_boll_limit = 1.0 - params.boll_limit
    for item in features:
        if item.index < 260 or item.vol_ratio_5 < params.min_vol_ratio:
            continue
        if params.require_compression and not item.compression_30:
            continue
        if abs(item.ret_5_z) > params.max_abs_z:
            continue
        if (
            item.break_up_20
            and item.ema20 > item.ema60
            and item.close > item.ema20
            and item.boll_pos_20 <= params.boll_limit
            and item.ret_1 > 0.0
        ):
            signals.append(
                CandidateSignal(
                    index=item.index,
                    direction="LONG",
                    family="trend",
                    score=round(4.0 + item.vol_ratio_5 + max(item.trend_strength, 0.0) * 10.0, 4),
                    reason=(
                        f"breakout_long vol={item.vol_ratio_5:.2f} z={item.ret_5_z:.2f} "
                        f"boll={item.boll_pos_20:.2f}"
                    ),
                )
            )
        elif (
            item.break_down_20
            and item.ema20 < item.ema60
            and item.close < item.ema20
            and item.boll_pos_20 >= lower_boll_limit
            and item.ret_1 < 0.0
        ):
            signals.append(
                CandidateSignal(
                    index=item.index,
                    direction="SHORT",
                    family="trend",
                    score=round(4.0 + item.vol_ratio_5 + max(-item.trend_strength, 0.0) * 10.0, 4),
                    reason=(
                        f"breakout_short vol={item.vol_ratio_5:.2f} z={item.ret_5_z:.2f} "
                        f"boll={item.boll_pos_20:.2f}"
                    ),
                )
            )
    return signals


def magician_signals(features: Sequence[BarFeatures], params: MagicianParams) -> list[CandidateSignal]:
    signals: list[CandidateSignal] = []
    for item in features:
        if item.index < 260:
            continue
        if item.vol_ratio_5 < params.min_vol_ratio or item.vol_ratio_5 > params.max_vol_ratio:
            continue
        if abs(item.ret_5_z) > params.max_abs_z:
            continue

        if _magician_vcp_long(item, params):
            signals.append(
                CandidateSignal(
                    index=item.index,
                    direction="LONG",
                    family="magician_vcp",
                    score=_magician_score(item, "LONG", base=6.0),
                    reason=(
                        f"vcp_breakout_long trend={item.trend_strength:.3f} "
                        f"vol={item.vol_ratio_5:.2f} z={item.ret_5_z:.2f} "
                        f"rsi={item.rsi_14:.1f} boll={item.boll_pos_20:.2f}"
                    ),
                )
            )
            continue

        if _magician_vcp_short(item, params):
            signals.append(
                CandidateSignal(
                    index=item.index,
                    direction="SHORT",
                    family="magician_vcp",
                    score=_magician_score(item, "SHORT", base=6.0),
                    reason=(
                        f"vcp_breakout_short trend={item.trend_strength:.3f} "
                        f"vol={item.vol_ratio_5:.2f} z={item.ret_5_z:.2f} "
                        f"rsi={item.rsi_14:.1f} boll={item.boll_pos_20:.2f}"
                    ),
                )
            )
            continue

        if not params.enable_pullback:
            continue

        if _magician_pullback_long(item, params):
            signals.append(
                CandidateSignal(
                    index=item.index,
                    direction="LONG",
                    family="magician_pullback",
                    score=_magician_score(item, "LONG", base=5.0),
                    reason=(
                        f"pullback_restart_long trend={item.trend_strength:.3f} "
                        f"vol={item.vol_ratio_5:.2f} z={item.ret_5_z:.2f} "
                        f"rsi={item.rsi_14:.1f} boll={item.boll_pos_20:.2f}"
                    ),
                )
            )
        elif _magician_pullback_short(item, params):
            signals.append(
                CandidateSignal(
                    index=item.index,
                    direction="SHORT",
                    family="magician_pullback",
                    score=_magician_score(item, "SHORT", base=5.0),
                    reason=(
                        f"pullback_restart_short trend={item.trend_strength:.3f} "
                        f"vol={item.vol_ratio_5:.2f} z={item.ret_5_z:.2f} "
                        f"rsi={item.rsi_14:.1f} boll={item.boll_pos_20:.2f}"
                    ),
                )
            )
    return signals


def backtest_signals(
    klines: Sequence[Kline],
    features: Sequence[BarFeatures],
    signals: Sequence[CandidateSignal],
    horizon_minutes: int = 10,
    min_gap_minutes: int = 10,
) -> list[dict]:
    resolved = _resolve_same_bar(signals)
    filtered = apply_min_gap(resolved, klines, min_gap_minutes)
    trades: list[dict] = []
    for signal in filtered:
        exit_index = signal.index + horizon_minutes
        if exit_index >= len(klines):
            continue
        entry = klines[signal.index]
        exit_bar = klines[exit_index]
        result, pnl = event_contract_pnl(signal.direction, entry.close, exit_bar.close)
        feature = features[signal.index]
        trades.append(
            {
                "index": signal.index,
                "direction": signal.direction,
                "family": signal.family,
                "entry_time": entry.close_time,
                "entry_time_utc": _format_ms(entry.close_time),
                "entry_price": entry.close,
                "exit_time": exit_bar.close_time,
                "exit_price": exit_bar.close,
                "result": result,
                "pnl": pnl,
                "score": signal.score,
                "reason": signal.reason,
                "beijing_bucket": feature.beijing_bucket,
                "beijing_hour": feature.beijing_hour,
                "is_weekend": feature.is_weekend,
                "ret_5_z": round(feature.ret_5_z, 4),
                "rsi_14": round(feature.rsi_14, 4),
                "boll_pos_20": round(feature.boll_pos_20, 4),
                "vol_ratio_5": round(feature.vol_ratio_5, 4),
            }
        )
    return trades


def filter_signals(
    signals: Sequence[CandidateSignal],
    features: Sequence[BarFeatures],
    start_ms: int | None = None,
    end_ms: int | None = None,
    allowed_family_buckets: set[str] | None = None,
) -> list[CandidateSignal]:
    result: list[CandidateSignal] = []
    for signal in signals:
        feature = features[signal.index]
        if start_ms is not None and feature.close_time < start_ms:
            continue
        if end_ms is not None and feature.close_time >= end_ms:
            continue
        if allowed_family_buckets is not None:
            key = f"{signal.family}|{feature.beijing_bucket}"
            if key not in allowed_family_buckets:
                continue
        result.append(signal)
    return result


def learn_allowed_family_buckets(
    trades: Sequence[dict],
    min_samples: int = 20,
    min_win_rate: float = 0.58,
    min_avg_pnl: float = 0.25,
) -> set[str]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for trade in trades:
        groups[f"{trade['family']}|{trade['beijing_bucket']}"].append(trade)
    allowed = set()
    for key, items in groups.items():
        stats = summarize_trades(items)
        if (
            stats["total_orders"] >= min_samples
            and stats["win_rate"] >= min_win_rate
            and stats["avg_pnl"] >= min_avg_pnl
        ):
            allowed.add(key)
    return allowed


def rolling_observation_guard_trades(
    klines: Sequence[Kline],
    features: Sequence[BarFeatures],
    signals: Sequence[CandidateSignal],
    start_ms: int,
    horizon_minutes: int = 10,
    min_gap_minutes: int = 10,
    min_samples: int = 20,
    lookback_days: int = 60,
    min_win_rate: float = BREAK_EVEN_WIN_RATE,
    min_avg_pnl: float = 0.0,
    key_mode: str = "family_hour",
) -> list[dict]:
    """Trade only when prior observed signals in the same bucket have edge.

    Every candidate signal is observed and added to its rolling history after
    the current decision, whether or not it was traded. This avoids lookahead
    while still allowing a live monitor to learn from skipped signals.
    """
    history: dict[str, deque[tuple[int, str, float]]] = defaultdict(deque)
    trades: list[dict] = []
    last_trade_time: int | None = None
    lookback_ms = lookback_days * 86_400_000
    min_gap_ms = min_gap_minutes * 60_000

    for signal in _resolve_same_bar(signals):
        exit_index = signal.index + horizon_minutes
        if exit_index >= len(klines):
            continue

        feature = features[signal.index]
        entry = klines[signal.index]
        exit_bar = klines[exit_index]
        result, pnl = event_contract_pnl(signal.direction, entry.close, exit_bar.close)
        key = _rolling_guard_key(signal, feature, key_mode)
        bucket_history = history[key]
        while bucket_history and bucket_history[0][0] < feature.close_time - lookback_ms:
            bucket_history.popleft()

        allowed = _history_has_edge(bucket_history, min_samples, min_win_rate, min_avg_pnl)
        if (
            feature.close_time >= start_ms
            and allowed
            and (last_trade_time is None or feature.close_time - last_trade_time >= min_gap_ms)
        ):
            trades.append(
                {
                    "index": signal.index,
                    "direction": signal.direction,
                    "family": signal.family,
                    "entry_time": entry.close_time,
                    "entry_time_utc": _format_ms(entry.close_time),
                    "entry_price": entry.close,
                    "exit_time": exit_bar.close_time,
                    "exit_price": exit_bar.close,
                    "result": result,
                    "pnl": pnl,
                    "score": signal.score,
                    "reason": signal.reason,
                    "beijing_bucket": feature.beijing_bucket,
                    "beijing_hour": feature.beijing_hour,
                    "rolling_key": key,
                    "rolling_sample_size": len(bucket_history),
                    "rolling_win_rate": _history_win_rate(bucket_history),
                    "rolling_avg_pnl": _history_avg_pnl(bucket_history),
                }
            )
            last_trade_time = feature.close_time

        bucket_history.append((feature.close_time, result, pnl))

    return trades


def grouped_stats(trades: Sequence[dict], key: str, min_samples: int = 1) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for trade in trades:
        groups[str(trade[key])].append(trade)
    rows = {}
    for name, items in sorted(groups.items()):
        if len(items) >= min_samples:
            rows[name] = summarize_trades(items)
    return rows


def optimize_reversal(
    klines: Sequence[Kline],
    features: Sequence[BarFeatures],
    train_start: int,
    train_end: int,
) -> list[dict]:
    results: list[dict] = []
    for z_min in (1.4, 1.7, 2.0, 2.3, 2.6):
        for rsi_extreme in (65.0, 70.0, 75.0):
            for boll_extreme in (0.85, 0.95, 1.05):
                for min_vol_ratio in (0.8, 1.0, 1.2):
                    for require_rejection in (False, True):
                        params = ReversalParams(z_min, rsi_extreme, boll_extreme, min_vol_ratio, require_rejection)
                        signals = reversal_signals(features, params)
                        train_signals = filter_signals(signals, features, train_start, train_end)
                        trades = backtest_signals(klines, features, train_signals)
                        stats = summarize_trades(trades)
                        if stats["total_orders"] < 30:
                            continue
                        results.append(
                            {
                                "family": "reversal",
                                "params": asdict(params),
                                "stats": stats,
                                "objective": _objective(stats),
                            }
                        )
    return sorted(results, key=lambda item: item["objective"], reverse=True)


def optimize_trend(
    klines: Sequence[Kline],
    features: Sequence[BarFeatures],
    train_start: int,
    train_end: int,
) -> list[dict]:
    results: list[dict] = []
    for min_vol_ratio in (0.8, 1.0, 1.2, 1.5):
        for max_abs_z in (1.0, 1.4, 1.8, 2.2):
            for boll_limit in (0.80, 0.90, 1.00):
                for require_compression in (False, True):
                    params = TrendParams(min_vol_ratio, max_abs_z, boll_limit, require_compression)
                    signals = trend_signals(features, params)
                    train_signals = filter_signals(signals, features, train_start, train_end)
                    trades = backtest_signals(klines, features, train_signals)
                    stats = summarize_trades(trades)
                    if stats["total_orders"] < 30:
                        continue
                    results.append(
                        {
                            "family": "trend",
                            "params": asdict(params),
                            "stats": stats,
                            "objective": _objective(stats),
                        }
                    )
    return sorted(results, key=lambda item: item["objective"], reverse=True)


def optimize_magician(
    klines: Sequence[Kline],
    features: Sequence[BarFeatures],
    train_start: int,
    train_end: int,
) -> list[dict]:
    results: list[dict] = []
    for min_vol_ratio in (0.8, 1.0, 1.2, 1.5):
        for max_vol_ratio in (1.8, 2.2, 3.0):
            if max_vol_ratio <= min_vol_ratio:
                continue
            for max_abs_z in (0.9, 1.2, 1.5, 1.8, 2.2):
                for min_trend_strength in (0.02, 0.04, 0.06, 0.10):
                    for enable_pullback in (False, True):
                        params = MagicianParams(
                            min_vol_ratio=min_vol_ratio,
                            max_vol_ratio=max_vol_ratio,
                            max_abs_z=max_abs_z,
                            min_trend_strength=min_trend_strength,
                            enable_pullback=enable_pullback,
                        )
                        signals = magician_signals(features, params)
                        train_signals = filter_signals(signals, features, train_start, train_end)
                        trades = backtest_signals(klines, features, train_signals)
                        stats = summarize_trades(trades)
                        if stats["total_orders"] < 30:
                            continue
                        results.append(
                            {
                                "family": "magician",
                                "params": asdict(params),
                                "stats": stats,
                                "objective": _objective(stats),
                            }
                        )
    return sorted(results, key=lambda item: item["objective"], reverse=True)


def run_research(data_dir: Path, split_ms: int) -> dict:
    zip_paths = sorted(data_dir.glob("BTCUSDT-1m-*.zip"))
    klines = load_klines_from_zips(zip_paths)
    features = compute_features(klines)
    start_ms = klines[0].close_time
    end_ms = klines[-1].close_time

    reversal_rank = optimize_reversal(klines, features, start_ms, split_ms)
    trend_rank = optimize_trend(klines, features, start_ms, split_ms)
    magician_rank = optimize_magician(klines, features, start_ms, split_ms)
    if not reversal_rank:
        raise RuntimeError("no reversal parameter set met minimum sample requirement")
    if not trend_rank:
        raise RuntimeError("no trend parameter set met minimum sample requirement")
    if not magician_rank:
        raise RuntimeError("no magician parameter set met minimum sample requirement")

    best_reversal_params = ReversalParams(**reversal_rank[0]["params"])
    best_trend_params = TrendParams(**trend_rank[0]["params"])
    best_magician_params = MagicianParams(**magician_rank[0]["params"])
    reversal_all = reversal_signals(features, best_reversal_params)
    trend_all = trend_signals(features, best_trend_params)
    magician_all = magician_signals(features, best_magician_params)
    combined_all = _resolve_same_bar(list(reversal_all) + list(trend_all))

    train_reversal = _evaluate_named("reversal_train", klines, features, reversal_all, start_ms, split_ms)
    test_reversal = _evaluate_named("reversal_test", klines, features, reversal_all, split_ms, None)
    train_trend = _evaluate_named("trend_train", klines, features, trend_all, start_ms, split_ms)
    test_trend = _evaluate_named("trend_test", klines, features, trend_all, split_ms, None)
    train_magician = _evaluate_named("magician_train", klines, features, magician_all, start_ms, split_ms)
    test_magician = _evaluate_named("magician_test", klines, features, magician_all, split_ms, None)
    train_combined = _evaluate_named("combined_train", klines, features, combined_all, start_ms, split_ms)
    test_combined = _evaluate_named("combined_test", klines, features, combined_all, split_ms, None)

    allowed_buckets = learn_allowed_family_buckets(train_combined["trades"])
    train_combined_session = _evaluate_named(
        "combined_session_train",
        klines,
        features,
        combined_all,
        start_ms,
        split_ms,
        allowed_buckets,
    )
    test_combined_session = _evaluate_named(
        "combined_session_test",
        klines,
        features,
        combined_all,
        split_ms,
        None,
        allowed_buckets,
    )
    rolling_guard_test_trades = rolling_observation_guard_trades(
        klines,
        features,
        combined_all,
        start_ms=split_ms,
        min_samples=20,
        lookback_days=60,
        min_win_rate=BREAK_EVEN_WIN_RATE,
        min_avg_pnl=0.0,
        key_mode="family_hour",
    )

    report = {
        "dataset": {
            "data_dir": str(data_dir),
            "zip_files": len(zip_paths),
            "klines": len(klines),
            "start_utc": _format_ms(start_ms),
            "end_utc": _format_ms(end_ms),
            "split_utc": _format_ms(split_ms),
            "payout": "risk 10U to win 8U",
            "break_even_win_rate": round(BREAK_EVEN_WIN_RATE, 4),
        },
        "best_reversal_grid_train_top5": reversal_rank[:5],
        "best_trend_grid_train_top5": trend_rank[:5],
        "best_magician_grid_train_top5": magician_rank[:5],
        "selected_params": {
            "reversal": asdict(best_reversal_params),
            "trend": asdict(best_trend_params),
            "magician": asdict(best_magician_params),
            "session_filter": {
                "min_samples": 20,
                "min_win_rate": 0.58,
                "min_avg_pnl": 0.25,
                "allowed_family_buckets": sorted(allowed_buckets),
            },
            "rolling_observation_guard": {
                "key_mode": "family_hour",
                "lookback_days": 60,
                "min_samples": 20,
                "min_win_rate": round(BREAK_EVEN_WIN_RATE, 4),
                "min_avg_pnl": 0.0,
            },
        },
        "evaluations": {
            "reversal_train": _without_trades(train_reversal),
            "reversal_test": _without_trades(test_reversal),
            "trend_train": _without_trades(train_trend),
            "trend_test": _without_trades(test_trend),
            "magician_train": _without_trades(train_magician),
            "magician_test": _without_trades(test_magician),
            "combined_train": _without_trades(train_combined),
            "combined_test": _without_trades(test_combined),
            "combined_session_train": _without_trades(train_combined_session),
            "combined_session_test": _without_trades(test_combined_session),
            "combined_rolling_guard_test": {
                "name": "combined_rolling_guard_test",
                "stats": summarize_trades(rolling_guard_test_trades),
                "by_direction": grouped_stats(rolling_guard_test_trades, "direction"),
                "by_family": grouped_stats(rolling_guard_test_trades, "family"),
                "by_bucket_top": _top_grouped(grouped_stats(rolling_guard_test_trades, "beijing_bucket")),
            },
        },
        "time_of_day": {
            "volatility_by_beijing_hour": volatility_by_beijing_hour(klines),
            "combined_session_train_by_family": grouped_stats(train_combined_session["trades"], "family"),
            "combined_session_test_by_family": grouped_stats(test_combined_session["trades"], "family"),
            "combined_session_test_by_bucket": _top_grouped(grouped_stats(test_combined_session["trades"], "beijing_bucket")),
        },
    }
    report["markdown"] = build_markdown_summary(report)
    return report


def volatility_by_beijing_hour(klines: Sequence[Kline], horizon_minutes: int = 10) -> list[dict]:
    groups: dict[int, list[float]] = defaultdict(list)
    for index in range(0, len(klines) - horizon_minutes, horizon_minutes):
        current = klines[index]
        future = klines[index + horizon_minutes]
        if current.close <= 0:
            continue
        dt_utc = datetime.fromtimestamp(current.close_time / 1000, timezone.utc)
        bjt_hour = (dt_utc.hour + 8) % 24
        groups[bjt_hour].append(abs(future.close / current.close - 1.0) * 100.0)
    rows = []
    for hour, values in groups.items():
        rows.append(
            {
                "beijing_hour": hour,
                "samples": len(values),
                "avg_abs_10m_pct": round(sum(values) / len(values), 4) if values else 0.0,
                "p90_abs_10m_pct": round(_percentile(values, 90), 4) if values else 0.0,
            }
        )
    return sorted(rows, key=lambda item: item["avg_abs_10m_pct"], reverse=True)


def build_markdown_summary(report: dict) -> str:
    lines = [
        "# BTC 10分钟事件合约策略验证",
        "",
        "## 数据与赔率",
        "",
        f"- 数据目录：`{report['dataset']['data_dir']}`",
        f"- 1m ZIP 文件数：{report['dataset']['zip_files']}",
        f"- K线数量：{report['dataset']['klines']}",
        f"- 覆盖：{report['dataset']['start_utc']} 到 {report['dataset']['end_utc']} UTC",
        f"- 样本外切分：{report['dataset']['split_utc']} UTC",
        f"- 赔率：10U 赢 8U，盈亏平衡胜率 {report['dataset']['break_even_win_rate']:.2%}",
        "",
        "## 样本外结果",
        "",
        "| 策略 | 单数 | 胜率 | 盈亏(U) | 平均PnL | 最大回撤 | 最长连亏 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    evals = report["evaluations"]
    for key, label in (
        ("reversal_test", "反转"),
        ("trend_test", "趋势追击"),
        ("magician_test", "股票魔法师VCP/回踩"),
        ("combined_test", "反转+趋势"),
        ("combined_session_test", "反转+趋势+时间过滤"),
        ("combined_rolling_guard_test", "反转+趋势+滚动观察过滤"),
    ):
        stats = evals[key]["stats"]
        lines.append(
            f"| {label} | {stats['total_orders']} | {stats['win_rate']:.2%} | "
            f"{stats['balance']:.1f} | {stats['avg_pnl']:.2f} | "
            f"{stats['max_drawdown']:.1f} | {stats['max_loss_streak']} |"
        )

    lines.extend(
        [
            "",
            "## 选中参数",
            "",
            f"- 反转：`{json.dumps(report['selected_params']['reversal'], ensure_ascii=False)}`",
            f"- 趋势：`{json.dumps(report['selected_params']['trend'], ensure_ascii=False)}`",
            f"- 股票魔法师VCP/回踩：`{json.dumps(report['selected_params']['magician'], ensure_ascii=False)}`",
            f"- 时间过滤允许桶数：{len(report['selected_params']['session_filter']['allowed_family_buckets'])}",
            "",
            "## 北京时间波动最高小时",
            "",
            "| 排名 | BJT小时 | 样本 | 平均10m绝对波动 | P90 10m绝对波动 |",
            "|---:|---:|---:|---:|---:|",
        ]
    )
    for rank, row in enumerate(report["time_of_day"]["volatility_by_beijing_hour"][:8], start=1):
        lines.append(
            f"| {rank} | {row['beijing_hour']:02d}:00 | {row['samples']} | "
            f"{row['avg_abs_10m_pct']:.4f}% | {row['p90_abs_10m_pct']:.4f}% |"
        )
    lines.extend(
        [
            "",
            "## 结论",
            "",
            "- 只要样本外胜率低于 55.56%，即使看起来方向判断接近随机，也不适合实盘下注。",
            "- 时间过滤后的样本外结果比不过滤结果更接近实盘，因为它剔除了训练期没有正期望的固定时段。",
            "- 若某策略样本外单数太少，只能作为观察信号，不能作为主要下单来源。",
        ]
    )
    return "\n".join(lines) + "\n"


def save_report(report: dict, output_json: Path, output_md: Path | None = None) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    markdown = report.pop("markdown")
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if output_md:
        output_md.parent.mkdir(parents=True, exist_ok=True)
        output_md.write_text(markdown, encoding="utf-8")
    report["markdown"] = markdown


def _evaluate_named(
    name: str,
    klines: Sequence[Kline],
    features: Sequence[BarFeatures],
    signals: Sequence[CandidateSignal],
    start_ms: int | None,
    end_ms: int | None,
    allowed_family_buckets: set[str] | None = None,
) -> dict:
    scoped = filter_signals(signals, features, start_ms, end_ms, allowed_family_buckets)
    trades = backtest_signals(klines, features, scoped)
    return {
        "name": name,
        "stats": summarize_trades(trades),
        "by_direction": grouped_stats(trades, "direction"),
        "by_family": grouped_stats(trades, "family"),
        "by_bucket_top": _top_grouped(grouped_stats(trades, "beijing_bucket")),
        "trades": trades,
    }


def _without_trades(evaluation: dict) -> dict:
    return {key: value for key, value in evaluation.items() if key != "trades"}


def _resolve_same_bar(signals: Sequence[CandidateSignal]) -> list[CandidateSignal]:
    by_index: dict[int, CandidateSignal] = {}
    for signal in signals:
        current = by_index.get(signal.index)
        if current is None or signal.score > current.score:
            by_index[signal.index] = signal
    return [by_index[index] for index in sorted(by_index)]


def _objective(stats: dict) -> float:
    if stats["total_orders"] == 0:
        return -1_000_000.0
    edge = stats["win_rate"] - BREAK_EVEN_WIN_RATE
    sample_bonus = math.log(stats["total_orders"] + 1.0)
    drawdown_penalty = abs(stats["max_drawdown"]) * 0.02
    streak_penalty = stats["max_loss_streak"] * 2.0
    return stats["balance"] + edge * 250.0 + sample_bonus * 5.0 - drawdown_penalty - streak_penalty


def _magician_vcp_long(item: BarFeatures, params: MagicianParams) -> bool:
    return (
        item.compression_30
        and item.break_up_20
        and item.ema20 > item.ema60
        and item.close > item.ema20
        and item.trend_strength >= params.min_trend_strength
        and item.ret_1 > 0.0
        and params.long_rsi_min <= item.rsi_14 <= params.long_rsi_max
        and 0.45 <= item.boll_pos_20 <= params.long_boll_max
    )


def _magician_vcp_short(item: BarFeatures, params: MagicianParams) -> bool:
    return (
        item.compression_30
        and item.break_down_20
        and item.ema20 < item.ema60
        and item.close < item.ema20
        and item.trend_strength <= -params.min_trend_strength
        and item.ret_1 < 0.0
        and params.short_rsi_min <= item.rsi_14 <= params.short_rsi_max
        and params.short_boll_min <= item.boll_pos_20 <= 0.55
    )


def _magician_pullback_long(item: BarFeatures, params: MagicianParams) -> bool:
    return (
        item.ema20 > item.ema60
        and item.close > item.ema20
        and item.trend_strength >= params.min_trend_strength
        and item.ret_3 > 0.0
        and item.ret_10 >= 0.0
        and params.long_rsi_min <= item.rsi_14 <= params.long_rsi_max
        and 0.35 <= item.boll_pos_20 <= params.long_boll_max
    )


def _magician_pullback_short(item: BarFeatures, params: MagicianParams) -> bool:
    return (
        item.ema20 < item.ema60
        and item.close < item.ema20
        and item.trend_strength <= -params.min_trend_strength
        and item.ret_3 < 0.0
        and item.ret_10 <= 0.0
        and params.short_rsi_min <= item.rsi_14 <= params.short_rsi_max
        and params.short_boll_min <= item.boll_pos_20 <= 0.65
    )


def _magician_score(item: BarFeatures, direction: str, base: float) -> float:
    trend_points = abs(item.trend_strength) * 12.0
    volume_points = min(max(item.vol_ratio_5 - 1.0, 0.0), 1.5)
    z_penalty = max(abs(item.ret_5_z) - 1.0, 0.0)
    boll_center = 0.70 if direction == "LONG" else 0.30
    pivot_points = max(0.0, 1.0 - abs(item.boll_pos_20 - boll_center))
    return round(base + trend_points + volume_points + pivot_points - z_penalty, 4)


def _rolling_guard_key(signal: CandidateSignal, feature: BarFeatures, key_mode: str) -> str:
    if key_mode == "family":
        return signal.family
    if key_mode == "family_bucket":
        return f"{signal.family}|{feature.beijing_bucket}"
    if key_mode == "family_direction_hour":
        return f"{signal.family}|{signal.direction}|{feature.beijing_hour:02d}"
    if key_mode == "family_hour":
        return f"{signal.family}|{feature.beijing_hour:02d}"
    raise ValueError(f"unknown rolling guard key mode: {key_mode}")


def _history_has_edge(
    history: Sequence[tuple[int, str, float]],
    min_samples: int,
    min_win_rate: float,
    min_avg_pnl: float,
) -> bool:
    if len(history) < min_samples:
        return False
    return _history_win_rate(history) >= min_win_rate and _history_avg_pnl(history) >= min_avg_pnl


def _history_win_rate(history: Sequence[tuple[int, str, float]]) -> float:
    if not history:
        return 0.0
    return sum(1 for _time, result, _pnl in history if result == "WIN") / len(history)


def _history_avg_pnl(history: Sequence[tuple[int, str, float]]) -> float:
    if not history:
        return 0.0
    return sum(pnl for _time, _result, pnl in history) / len(history)


def _top_grouped(groups: dict, limit: int = 12) -> list[dict]:
    rows = []
    for name, stats in groups.items():
        item = {"name": name}
        item.update(stats)
        rows.append(item)
    return sorted(rows, key=lambda item: item["balance"], reverse=True)[:limit]


def _return_pct(closes: Sequence[float], index: int, window: int) -> float:
    if index < window or closes[index - window] == 0:
        return 0.0
    return (closes[index] / closes[index - window] - 1.0) * 100.0


def _ema_series(values: Sequence[float], period: int) -> list[float]:
    if not values:
        return []
    multiplier = 2.0 / (period + 1.0)
    current = values[0]
    result = []
    for value in values:
        current = value * multiplier + current * (1.0 - multiplier)
        result.append(current)
    return result


def _rsi_series(closes: Sequence[float], period: int) -> list[float]:
    result = [50.0] * len(closes)
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
            result[index] = 100.0 if loss_sum == 0 else 100.0 - 100.0 / (1.0 + gain_sum / loss_sum)
    return result


def _bollinger_position_series(closes: Sequence[float], period: int, std_dev: float) -> list[float]:
    result = [0.5] * len(closes)
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
            mean = total / period
            variance = max(0.0, total_sq / period - mean * mean)
            deviation = math.sqrt(variance)
            lower = mean - std_dev * deviation
            upper = mean + std_dev * deviation
            width = upper - lower
            result[index] = (close - lower) / width if width > 0 else 0.5
    return result


def _rolling_zscore(values: Sequence[float], window_size: int) -> list[float]:
    result = [0.0] * len(values)
    window: deque[float] = deque()
    total = 0.0
    total_sq = 0.0
    for index, value in enumerate(values):
        if len(window) >= max(30, window_size // 4):
            mean = total / len(window)
            variance = max(0.0, total_sq / len(window) - mean * mean)
            std = math.sqrt(variance)
            result[index] = (value - mean) / std if std > 1e-12 else 0.0
        window.append(value)
        total += value
        total_sq += value * value
        if len(window) > window_size:
            old = window.popleft()
            total -= old
            total_sq -= old * old
    return result


def _range_pct_series(klines: Sequence[Kline], window_size: int) -> list[float]:
    result = [0.0] * len(klines)
    for index, item in enumerate(klines):
        if index < window_size:
            continue
        scoped = klines[index - window_size + 1 : index + 1]
        high = max(kline.high for kline in scoped)
        low = min(kline.low for kline in scoped)
        result[index] = (high - low) / item.close * 100.0 if item.close else 0.0
    return result


def _rolling_mean(values: Sequence[float], window_size: int) -> list[float]:
    result = [0.0] * len(values)
    window: deque[float] = deque()
    total = 0.0
    for index, value in enumerate(values):
        if window:
            result[index] = total / len(window)
        window.append(value)
        total += value
        if len(window) > window_size:
            total -= window.popleft()
    return result


def _volume_ratio_series(volumes: Sequence[float], recent: int, baseline: int) -> list[float]:
    result = [1.0] * len(volumes)
    prefix = [0.0]
    for value in volumes:
        prefix.append(prefix[-1] + value)
    for index in range(len(volumes)):
        if index < recent:
            continue
        recent_sum = prefix[index + 1] - prefix[index + 1 - recent]
        base_start = max(0, index + 1 - recent - baseline)
        base_end = index + 1 - recent
        base_count = base_end - base_start
        if base_count <= 0:
            continue
        base_avg = (prefix[base_end] - prefix[base_start]) / base_count
        result[index] = recent_sum / (base_avg * recent) if base_avg > 0 else 1.0
    return result


def _break_up(klines: Sequence[Kline], index: int, lookback: int) -> bool:
    if index < lookback:
        return False
    previous_high = max(item.high for item in klines[index - lookback : index])
    return klines[index].close > previous_high


def _break_down(klines: Sequence[Kline], index: int, lookback: int) -> bool:
    if index < lookback:
        return False
    previous_low = min(item.low for item in klines[index - lookback : index])
    return klines[index].close < previous_low


def _close_strength(kline: Kline) -> float:
    spread = kline.high - kline.low
    if spread <= 0:
        return 0.5
    return (kline.close - kline.low) / spread


def _percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100.0
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[int(rank)]
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _format_ms(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_utc_ms(value: str) -> int:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--split", default="2026-02-01T00:00:00Z")
    parser.add_argument("--output-json", default="reports/btcusdt_event_strategy_research_20260518.json")
    parser.add_argument("--output-md", default="reports/btcusdt_event_strategy_research_20260518.md")
    args = parser.parse_args(argv)

    report = run_research(Path(args.data_dir), _parse_utc_ms(args.split))
    save_report(report, Path(args.output_json), Path(args.output_md))
    print(report["markdown"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
