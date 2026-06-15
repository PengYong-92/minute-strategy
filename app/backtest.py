import csv
import json
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from app.models import Kline, Signal
from app.rolling_edge import RollingEdgeConfig, rolling_edge_snapshot, should_degrade
from app.strategy import choose_trade_signal


@dataclass(frozen=True)
class BacktestConfig:
    warmup_minutes: int = 360
    max_open_orders: int = 1
    min_order_gap_minutes: int = 10
    strategy_history_limit: int = 1440
    stake: float = 10.0
    win_return: float = 18.0
    enable_stake_progression: bool = False
    stake_progression_max_orders: int = 3
    enable_rolling_edge_guard: bool = False
    rolling_edge_lookback_days: int = 60
    rolling_edge_min_samples: int = 5
    rolling_edge_min_win_rate: float = 0.62
    rolling_edge_min_ev: float = 0.5
    short_observe_only: bool = True


def load_klines_from_zip(zip_path: str | Path) -> list[Kline]:
    path = Path(zip_path)
    with zipfile.ZipFile(path) as archive:
        csv_names = [name for name in archive.namelist() if name.endswith(".csv")]
        if not csv_names:
            raise ValueError(f"no csv file in {path}")
        with archive.open(csv_names[0]) as handle:
            text = (line.decode("utf-8") for line in handle)
            rows = csv.reader(text)
            return [_parse_csv_row(row) for row in rows if _is_data_row(row)]


def load_klines_from_zips(zip_paths: Sequence[str | Path]) -> list[Kline]:
    by_open_time: dict[int, Kline] = {}
    for zip_path in zip_paths:
        for kline in load_klines_from_zip(zip_path):
            by_open_time[kline.open_time] = kline
    return [by_open_time[key] for key in sorted(by_open_time)]


def run_backtest(
    klines: Sequence[Kline],
    config: BacktestConfig | None = None,
    signal_provider: Callable[[Sequence[Kline]], Signal] = choose_trade_signal,
) -> dict:
    config = config or BacktestConfig()
    started = time.perf_counter()
    orders: list[dict] = []
    open_orders: list[dict] = []
    rejected_signals: dict[str, int] = {}
    last_order_time: int | None = None
    balance = 0.0
    next_stake = config.stake
    stake_progression_step = 1

    for index in range(max(config.warmup_minutes, 1), len(klines)):
        current = klines[index - 1]

        for order in list(open_orders):
            if current.close_time < order["expires_at"]:
                continue
            exit_kline = _first_kline_at_or_after(klines, order["expires_at"], start=index - 1)
            if exit_kline is None:
                continue
            _settle_order(order, exit_kline, config)
            balance = round(balance + order["pnl"], 4)
            next_stake, stake_progression_step = _next_stake_after_settlement(order, config)
            open_orders.remove(order)

        history_start = max(0, index - config.strategy_history_limit)
        signal = signal_provider(klines[history_start:index])
        if not signal.actionable:
            _record_rejection(rejected_signals, signal)
            continue
        if len(open_orders) >= config.max_open_orders:
            continue
        if last_order_time is not None:
            if current.close_time - last_order_time < config.min_order_gap_minutes * 60_000:
                continue

        if config.short_observe_only and signal.direction == "SHORT":
            rejected_signals["short_observe_only"] = rejected_signals.get("short_observe_only", 0) + 1
            continue

        if config.enable_rolling_edge_guard and _rolling_edge_degraded(orders, signal, current, config):
            rejected_signals["rolling_edge_degraded"] = rejected_signals.get("rolling_edge_degraded", 0) + 1
            continue

        expires_at = current.close_time + signal.timeframe_minutes * 60_000
        if expires_at > klines[-1].close_time:
            continue

        stake = round(next_stake, 4)
        win_return = _win_return_for_stake(stake, config)
        order = {
            "id": len(orders) + 1,
            "direction": signal.direction,
            "timeframe_minutes": signal.timeframe_minutes,
            "level": signal.level,
            "reason": signal.reason,
            "score": signal.score,
            "threshold": signal.threshold,
            "threshold_segment": signal.threshold_segment,
            "session_allowed": signal.session_allowed,
            "session_sample_size": signal.session_sample_size,
            "session_win_rate": signal.session_win_rate,
            "session_ev": signal.session_ev,
            "session_edge_min": signal.session_edge_min,
            "regime": signal.regime,
            "stake": stake,
            "win_return": win_return,
            "stake_progression_step": stake_progression_step,
            "entry_price": current.close,
            "entry_time": current.close_time,
            "expires_at": expires_at,
            "exit_price": None,
            "exit_time": None,
            "result": None,
            "pnl": 0.0,
        }
        orders.append(order)
        open_orders.append(order)
        last_order_time = current.close_time

    for order in list(open_orders):
        exit_kline = _first_kline_at_or_after(klines, order["expires_at"], start=0)
        if exit_kline is None:
            continue
        _settle_order(order, exit_kline, config)
        balance = round(balance + order["pnl"], 4)
        next_stake, stake_progression_step = _next_stake_after_settlement(order, config)
        open_orders.remove(order)

    return {
        "stats": _stats(orders, balance),
        "risk": _risk_stats(orders),
        "by_timeframe": _group_stats(orders, "timeframe_minutes"),
        "by_direction": _group_stats(orders, "direction"),
        "by_session": _group_stats(orders, "threshold_segment"),
        "by_timeframe_session": _group_timeframe_session_stats(orders),
        "by_month": _group_month_stats(orders),
        "by_regime": _group_stats(orders, "regime"),
        "rejected_signals": rejected_signals,
        "elapsed_seconds": round(time.perf_counter() - started, 4),
        "orders": orders,
    }


def save_backtest_report(result: dict, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_csv_row(row: list[str]) -> Kline:
    return Kline(
        open_time=_normalize_timestamp(int(row[0])),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]),
        close_time=_normalize_timestamp(int(row[6])),
    )


def _is_data_row(row: list[str]) -> bool:
    return len(row) >= 7 and row[0].isdigit()


def _normalize_timestamp(value: int) -> int:
    if value >= 10_000_000_000_000:
        return value // 1000
    return value


def _first_kline_at_or_after(klines: Sequence[Kline], close_time: int, start: int) -> Kline | None:
    for item in klines[max(start, 0) :]:
        if item.close_time >= close_time:
            return item
    return None


def _settle_order(order: dict, exit_kline: Kline, config: BacktestConfig) -> None:
    if order["direction"] == "LONG":
        won = exit_kline.close > order["entry_price"]
    else:
        won = exit_kline.close < order["entry_price"]
    order["exit_price"] = exit_kline.close
    order["exit_time"] = exit_kline.close_time
    order["result"] = "WIN" if won else "LOSS"
    stake = float(order.get("stake", config.stake))
    win_return = float(order.get("win_return", config.win_return))
    order["pnl"] = round(win_return - stake, 4) if won else round(-stake, 4)


def _win_return_for_stake(stake: float, config: BacktestConfig) -> float:
    if not config.enable_stake_progression:
        return round(config.win_return, 4)
    if config.stake <= 0:
        return round(config.win_return, 4)
    return round(stake * (config.win_return / config.stake), 4)


def _next_stake_after_settlement(order: dict, config: BacktestConfig) -> tuple[float, int]:
    if not config.enable_stake_progression:
        return config.stake, 1
    max_orders = max(1, int(config.stake_progression_max_orders))
    if order.get("result") != "WIN":
        return config.stake, 1
    current_step = int(order.get("stake_progression_step", 1) or 1)
    if current_step >= max_orders:
        return config.stake, 1
    return float(order.get("win_return", config.win_return)), current_step + 1


def _stats(orders: Sequence[dict], balance: float) -> dict:
    settled = [order for order in orders if order["result"]]
    wins = [order for order in settled if order["result"] == "WIN"]
    losses = [order for order in settled if order["result"] == "LOSS"]
    total_staked = round(sum(float(order.get("stake", 0.0)) for order in settled), 4)
    return {
        "total_orders": len(settled),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(settled), 4) if settled else 0.0,
        "balance": round(balance, 4),
        "avg_pnl": round(balance / len(settled), 4) if settled else 0.0,
        "total_staked": total_staked,
        "roi": round(balance / total_staked, 4) if total_staked else 0.0,
        "break_even_win_rate": 0.5556,
    }


def _risk_stats(orders: Sequence[dict]) -> dict:
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    loss_streak = 0
    max_loss_streak = 0
    win_streak = 0
    max_win_streak = 0
    for order in orders:
        if not order["result"]:
            continue
        equity = round(equity + order["pnl"], 4)
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)
        if order["result"] == "LOSS":
            loss_streak += 1
            win_streak = 0
        else:
            win_streak += 1
            loss_streak = 0
        max_loss_streak = max(max_loss_streak, loss_streak)
        max_win_streak = max(max_win_streak, win_streak)
    return {
        "max_drawdown": round(max_drawdown, 4),
        "max_loss_streak": max_loss_streak,
        "max_win_streak": max_win_streak,
    }


def _group_stats(orders: Sequence[dict], key: str) -> dict:
    groups: dict[str, list[dict]] = {}
    for order in orders:
        if not order["result"]:
            continue
        groups.setdefault(str(order[key]), []).append(order)
    return {name: _stats(group, sum(order["pnl"] for order in group)) for name, group in groups.items()}


def _group_month_stats(orders: Sequence[dict]) -> dict:
    groups: dict[str, list[dict]] = {}
    for order in orders:
        if not order["result"]:
            continue
        name = datetime.fromtimestamp(order["entry_time"] / 1000, timezone.utc).strftime("%Y-%m")
        groups.setdefault(name, []).append(order)
    return {name: _stats(group, sum(order["pnl"] for order in group)) for name, group in groups.items()}


def _group_timeframe_session_stats(orders: Sequence[dict]) -> dict:
    groups: dict[str, list[dict]] = {}
    for order in orders:
        if not order["result"]:
            continue
        name = f"{order['timeframe_minutes']}|{order.get('threshold_segment', 'GLOBAL')}"
        groups.setdefault(name, []).append(order)
    return {name: _stats(group, sum(order["pnl"] for order in group)) for name, group in groups.items()}


def _record_rejection(rejected_signals: dict[str, int], signal: Signal) -> None:
    if abs(signal.score) >= signal.threshold and not signal.session_allowed:
        key = "session_blocked"
    elif abs(signal.score) < signal.threshold:
        key = "below_threshold"
    elif "极端过热" in signal.reason or "过热" in signal.reason:
        key = "overheated"
    elif "确认不足" in signal.reason:
        key = "confirmation_failed"
    else:
        key = "other"
    rejected_signals[key] = rejected_signals.get(key, 0) + 1


def _rolling_edge_degraded(
    orders: Sequence[dict],
    signal: Signal,
    current: Kline,
    config: BacktestConfig,
) -> bool:
    edge_config = RollingEdgeConfig(
        lookback_days=config.rolling_edge_lookback_days,
        min_samples=config.rolling_edge_min_samples,
        min_win_rate=config.rolling_edge_min_win_rate,
        min_ev=config.rolling_edge_min_ev,
    )
    current_item = {
        "entry_time": current.close_time,
        "timeframe_minutes": signal.timeframe_minutes,
        "threshold_segment": signal.threshold_segment,
        "reason": signal.reason,
    }
    snapshot = rolling_edge_snapshot(orders, current_item, edge_config)
    return should_degrade(snapshot, edge_config)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python3 -m app.backtest PATH_TO_BINANCE_ZIP [REPORT_JSON]", file=sys.stderr)
        return 2
    klines = load_klines_from_zip(argv[0])
    result = run_backtest(klines)
    if len(argv) >= 2:
        save_backtest_report(result, argv[1])
    print(json.dumps({k: result[k] for k in ("stats", "by_timeframe", "by_direction")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
