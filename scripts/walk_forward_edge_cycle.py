#!/usr/bin/env python3
import argparse
import bisect
import json
import math
import multiprocessing as mp
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import strategy
from app.backtest import BacktestConfig, load_klines_from_zips
from app.models import Kline, Signal
from app.rolling_edge import RollingEdgeConfig, rolling_edge_snapshot, should_degrade


EDGE_CANDIDATES = (12, 14, 16, 18, 20, 22, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 36, 38, 40)
DEFAULT_EDGE = 27.0
MS_PER_DAY = 86_400_000
TEN_MINUTES_MS = 10 * 60_000
BREAK_EVEN_WIN_RATE = 10.0 / 18.0
_WORKER_KLINES: Sequence[Kline] = ()
_WORKER_CLOSE_TIMES: Sequence[int] = ()


@dataclass(frozen=True)
class Candidate:
    entry_index: int
    exit_index: int
    entry_time: int
    expires_at: int
    direction: str
    segment: str
    setup: str
    reason: str
    score: float
    threshold: float
    edge: float
    stake_key: str


@dataclass(frozen=True)
class CycleConfig:
    lookback_days: int
    update_days: int
    max_step: float | None = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Walk-forward edge cycle evaluation on precomputed signals.")
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--report", default=str(ROOT / "reports" / "walk_forward_edge_cycle_20260521.json"))
    parser.add_argument("--start", default="2024-05-18")
    parser.add_argument("--end", default="2026-05-17")
    parser.add_argument("--eval-start", default="2025-05-18")
    parser.add_argument("--workers", default="auto")
    parser.add_argument("--cache", default=str(ROOT / "reports" / "walk_forward_candidates_20260521.json"))
    args = parser.parse_args(argv)

    started = time.perf_counter()
    data_dir = Path(args.data_dir)
    report_path = Path(args.report)
    start_ms = _parse_date(args.start)
    end_ms = _parse_date(args.end, end_of_day=True)
    eval_start_ms = _parse_date(args.eval_start)
    eval_end_ms = end_ms

    zip_paths = sorted(data_dir.glob("BTCUSDT-1m-*.zip"))
    klines = [item for item in load_klines_from_zips(zip_paths) if start_ms <= item.close_time <= end_ms]
    close_times = [item.close_time for item in klines]
    print(f"loaded klines={len(klines)} { _iso(klines[0].close_time) } -> { _iso(klines[-1].close_time) }")

    workers = _worker_count(args.workers)
    candidates = _load_or_precompute_candidates(Path(args.cache), klines, close_times, workers)
    print(f"candidate signals={len(candidates)}")

    base_config = BacktestConfig(enable_rolling_edge_guard=True, enable_stake_progression=True)
    baseline_eval = _simulate(
        klines,
        candidates,
        eval_start_ms,
        eval_end_ms,
        lambda _candidate, _time: DEFAULT_EDGE,
        base_config,
    )

    static_two_year_long_map = {
        "WD-00": 16.0,
        "WD-08": 26.0,
        "WD-20": 14.0,
        "WD-22": 25.0,
        "WE-02": 24.0,
        "WE-03": 33.0,
        "WE-08": 25.0,
        "WE-17": 36.0,
    }
    static_eval = _simulate(
        klines,
        candidates,
        eval_start_ms,
        eval_end_ms,
        lambda candidate, _time: static_two_year_long_map.get(candidate.segment, DEFAULT_EDGE)
        if candidate.direction == "LONG"
        else DEFAULT_EDGE,
        base_config,
    )

    cycle_configs = [
        CycleConfig(lookback_days=60, update_days=7),
        CycleConfig(lookback_days=60, update_days=14),
        CycleConfig(lookback_days=60, update_days=30),
        CycleConfig(lookback_days=60, update_days=60),
        CycleConfig(lookback_days=90, update_days=7),
        CycleConfig(lookback_days=90, update_days=14),
        CycleConfig(lookback_days=90, update_days=30),
        CycleConfig(lookback_days=90, update_days=60),
        CycleConfig(lookback_days=180, update_days=14),
        CycleConfig(lookback_days=180, update_days=30),
        CycleConfig(lookback_days=180, update_days=60),
        CycleConfig(lookback_days=180, update_days=90),
        CycleConfig(lookback_days=365, update_days=30),
        CycleConfig(lookback_days=365, update_days=60),
        CycleConfig(lookback_days=365, update_days=90),
        CycleConfig(lookback_days=180, update_days=30, max_step=4.0),
        CycleConfig(lookback_days=180, update_days=60, max_step=4.0),
        CycleConfig(lookback_days=180, update_days=30, max_step=2.0),
        CycleConfig(lookback_days=365, update_days=30, max_step=4.0),
    ]

    results = []
    for config in cycle_configs:
        print(f"evaluating lookback={config.lookback_days}d update={config.update_days}d")
        maps = _build_edge_schedule(klines, candidates, eval_start_ms, eval_end_ms, config, base_config)
        replay = _simulate(
            klines,
            candidates,
            eval_start_ms,
            eval_end_ms,
            _scheduled_edge_provider(maps),
            base_config,
        )
        changes = _schedule_change_stats(maps)
        results.append(
            {
                "lookback_days": config.lookback_days,
                "update_days": config.update_days,
                "max_step": config.max_step,
                "updates": len(maps),
                "summary": replay["summary"],
                "risk": replay["risk"],
                "by_direction": replay["by_direction"],
                "by_direction_segment": replay["by_direction_segment"],
                "edge_change_stats": changes,
                "score": _rank_score(replay["summary"], replay["risk"], changes),
                "schedule": maps,
            }
        )

    results.sort(key=lambda item: item["score"], reverse=True)
    report = {
        "coverage": {
            "data_start": _iso(start_ms),
            "data_end": _iso(end_ms),
            "eval_start": _iso(eval_start_ms),
            "eval_end": _iso(eval_end_ms),
            "klines": len(klines),
            "candidates": len(candidates),
            "method": (
                "walk-forward: each update trains edge caps from prior lookback only; "
                "validation replays the next period with rolling guard and three-order stake progression."
            ),
        },
        "baseline_edge27": _compact_replay(baseline_eval),
        "static_two_year_filtered_long_map": {
            "long_edge_map": static_two_year_long_map,
            "replay": _compact_replay(static_eval),
        },
        "ranked_cycle_results": results,
        "elapsed_seconds": round(time.perf_counter() - started, 4),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved {report_path}")
    print(json.dumps(_printable_summary(report), ensure_ascii=False, indent=2))
    return 0


def _load_or_precompute_candidates(
    cache_path: Path,
    klines: Sequence[Kline],
    close_times: Sequence[int],
    workers: int,
) -> list[Candidate]:
    if cache_path.exists():
        rows = json.loads(cache_path.read_text(encoding="utf-8"))
        print(f"loaded cached candidates={len(rows)} from {cache_path}")
        return [Candidate(**row) for row in rows]

    candidates = _precompute_candidates(klines, close_times, workers)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps([asdict(item) for item in candidates], ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"saved cached candidates={len(candidates)} to {cache_path}")
    return candidates


def _precompute_candidates(klines: Sequence[Kline], close_times: Sequence[int], workers: int) -> list[Candidate]:
    if workers <= 1:
        return _precompute_candidates_sequential(klines, close_times)

    global _WORKER_KLINES, _WORKER_CLOSE_TIMES
    _WORKER_KLINES = klines
    _WORKER_CLOSE_TIMES = close_times
    start_index = max(BacktestConfig().warmup_minutes, 1)
    chunk_size = 20_000
    chunks = [(start, min(start + chunk_size, len(klines))) for start in range(start_index, len(klines), chunk_size)]
    print(f"precompute workers={workers} chunks={len(chunks)} chunk_size={chunk_size}")
    started = time.perf_counter()
    candidates: list[Candidate] = []
    context = mp.get_context("fork")
    with context.Pool(processes=workers, initializer=_init_precompute_worker) as pool:
        for done, rows in enumerate(pool.imap_unordered(_precompute_chunk, chunks), start=1):
            candidates.extend(rows)
            if done == 1 or done % 5 == 0 or done == len(chunks):
                print(
                    f"precompute chunks={done}/{len(chunks)} candidates={len(candidates)} "
                    f"elapsed={time.perf_counter() - started:.1f}s"
                )
    candidates.sort(key=lambda item: item.entry_time)
    return candidates


def _precompute_candidates_sequential(klines: Sequence[Kline], close_times: Sequence[int]) -> list[Candidate]:
    original_max_edge = strategy.MAX_TRADE_EDGE
    original_segment_edges = dict(strategy.SEGMENT_MAX_TRADE_EDGE)
    strategy.MAX_TRADE_EDGE = 999.0
    strategy.SEGMENT_MAX_TRADE_EDGE.clear()
    candidates: list[Candidate] = []
    try:
        for index in range(max(BacktestConfig().warmup_minutes, 1), len(klines)):
            if index % 100_000 == 0:
                print(f"precompute index={index}/{len(klines)} candidates={len(candidates)}")
            history_start = max(0, index - BacktestConfig().strategy_history_limit)
            signal = strategy.choose_trade_signal(klines[history_start:index])
            if signal.direction not in {"LONG", "SHORT"}:
                continue
            current = klines[index - 1]
            expires_at = current.close_time + signal.timeframe_minutes * 60_000
            exit_index = bisect.bisect_left(close_times, expires_at)
            if exit_index >= len(klines):
                continue
            edge = round(abs(signal.score) - signal.threshold, 4)
            setup = signal.reason.split("：", 1)[0].split(";", 1)[0].strip() or "UNKNOWN"
            candidates.append(
                Candidate(
                    entry_index=index - 1,
                    exit_index=exit_index,
                    entry_time=current.close_time,
                    expires_at=expires_at,
                    direction=signal.direction,
                    segment=signal.threshold_segment,
                    setup=setup,
                    reason=signal.reason,
                    score=signal.score,
                    threshold=signal.threshold,
                    edge=edge,
                    stake_key=f"{signal.direction}|{signal.threshold_segment}",
                )
            )
    finally:
        strategy.MAX_TRADE_EDGE = original_max_edge
        strategy.SEGMENT_MAX_TRADE_EDGE.clear()
        strategy.SEGMENT_MAX_TRADE_EDGE.update(original_segment_edges)
    return candidates


def _init_precompute_worker() -> None:
    strategy.MAX_TRADE_EDGE = 999.0
    strategy.SEGMENT_MAX_TRADE_EDGE.clear()


def _precompute_chunk(bounds: tuple[int, int]) -> list[Candidate]:
    start, end = bounds
    return _precompute_candidate_range(_WORKER_KLINES, _WORKER_CLOSE_TIMES, start, end)


def _precompute_candidate_range(
    klines: Sequence[Kline],
    close_times: Sequence[int],
    start: int,
    end: int,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    config = BacktestConfig()
    for index in range(start, end):
        history_start = max(0, index - config.strategy_history_limit)
        signal = strategy.choose_trade_signal(klines[history_start:index])
        if signal.direction not in {"LONG", "SHORT"}:
            continue
        current = klines[index - 1]
        expires_at = current.close_time + signal.timeframe_minutes * 60_000
        exit_index = bisect.bisect_left(close_times, expires_at)
        if exit_index >= len(klines):
            continue
        edge = round(abs(signal.score) - signal.threshold, 4)
        setup = signal.reason.split("：", 1)[0].split(";", 1)[0].strip() or "UNKNOWN"
        candidates.append(
            Candidate(
                entry_index=index - 1,
                exit_index=exit_index,
                entry_time=current.close_time,
                expires_at=expires_at,
                direction=signal.direction,
                segment=signal.threshold_segment,
                setup=setup,
                reason=signal.reason,
                score=signal.score,
                threshold=signal.threshold,
                edge=edge,
                stake_key=f"{signal.direction}|{signal.threshold_segment}",
            )
        )
    return candidates


def _build_edge_schedule(
    klines: Sequence[Kline],
    candidates: Sequence[Candidate],
    eval_start_ms: int,
    eval_end_ms: int,
    cycle: CycleConfig,
    config: BacktestConfig,
) -> list[dict]:
    maps: list[dict] = []
    update_time = eval_start_ms
    previous_map: dict[str, float] = {}
    while update_time <= eval_end_ms:
        train_start = update_time - cycle.lookback_days * MS_PER_DAY
        train_end = update_time - 1
        edge_map = _train_edge_map(klines, candidates, train_start, train_end, config)
        if cycle.max_step is not None:
            edge_map = _limit_edge_step(edge_map, previous_map, cycle.max_step)
        maps.append(
            {
                "effective_at": update_time,
                "effective_at_iso": _iso(update_time),
                "train_start": train_start,
                "train_start_iso": _iso(train_start),
                "train_end": train_end,
                "train_end_iso": _iso(train_end),
                "edge_map": edge_map,
                "changed_keys": sorted(key for key in set(edge_map) | set(previous_map) if edge_map.get(key) != previous_map.get(key)),
            }
        )
        previous_map = edge_map
        update_time += cycle.update_days * MS_PER_DAY
    return maps


def _train_edge_map(
    klines: Sequence[Kline],
    candidates: Sequence[Candidate],
    train_start: int,
    train_end: int,
    config: BacktestConfig,
) -> dict[str, float]:
    train_candidates = [item for item in candidates if train_start <= item.entry_time <= train_end]
    keys = sorted({item.stake_key for item in train_candidates if item.direction == "LONG"})
    edge_map: dict[str, float] = {}
    baseline = _simulate(klines, train_candidates, train_start, train_end, lambda _candidate, _time: DEFAULT_EDGE, config)
    baseline_by_key = baseline["by_direction_segment"]

    for key in keys:
        baseline_key = baseline_by_key.get(key, _empty_summary())
        best_edge = DEFAULT_EDGE
        best_score = _train_key_score(baseline_key, baseline_key)
        for edge in EDGE_CANDIDATES:
            replay = _simulate(
                klines,
                train_candidates,
                train_start,
                train_end,
                lambda candidate, _time, target=key, cap=float(edge): cap if candidate.stake_key == target else DEFAULT_EDGE,
                config,
            )
            stats = replay["by_direction_segment"].get(key, _empty_summary())
            score = _train_key_score(stats, baseline_key)
            if score > best_score:
                best_score = score
                best_edge = float(edge)
        if best_edge != DEFAULT_EDGE:
            segment = key.split("|", 1)[1]
            edge_map[segment] = best_edge
    return edge_map


def _limit_edge_step(edge_map: dict[str, float], previous_map: dict[str, float], max_step: float) -> dict[str, float]:
    if not previous_map:
        return edge_map
    limited: dict[str, float] = {}
    for segment in set(edge_map) | set(previous_map):
        target = edge_map.get(segment, DEFAULT_EDGE)
        previous = previous_map.get(segment, DEFAULT_EDGE)
        delta = max(-max_step, min(max_step, target - previous))
        value = round(previous + delta, 4)
        if value != DEFAULT_EDGE:
            limited[segment] = value
    return limited


def _train_key_score(stats: dict, baseline: dict) -> float:
    orders = stats["total_orders"]
    if orders < 20:
        return -1_000_000.0 + orders
    if stats["win_rate"] < BREAK_EVEN_WIN_RATE or stats["balance"] <= 0:
        return -100_000.0 + stats["balance"]
    baseline_balance = baseline.get("balance", 0.0)
    improvement = stats["balance"] - baseline_balance
    drawdown_penalty = abs(stats.get("max_drawdown", 0.0)) * 0.25
    streak_penalty = stats.get("max_loss_streak", 0) * 8.0
    sample_bonus = min(orders, 120) * 0.25
    return improvement + stats["balance"] * 0.35 - drawdown_penalty - streak_penalty + sample_bonus


def _scheduled_edge_provider(schedule: Sequence[dict]) -> Callable[[Candidate, int], float]:
    effective_times = [item["effective_at"] for item in schedule]

    def provider(candidate: Candidate, current_time: int) -> float:
        if candidate.direction == "SHORT":
            return DEFAULT_EDGE
        index = bisect.bisect_right(effective_times, current_time) - 1
        if index < 0:
            return DEFAULT_EDGE
        return float(schedule[index]["edge_map"].get(candidate.segment, DEFAULT_EDGE))

    return provider


def _simulate(
    klines: Sequence[Kline],
    candidates: Sequence[Candidate],
    start_ms: int,
    end_ms: int,
    edge_provider: Callable[[Candidate, int], float],
    config: BacktestConfig,
) -> dict:
    orders: list[dict] = []
    open_orders: list[dict] = []
    last_order_time: int | None = None
    balance = 0.0
    next_stake = config.stake
    stake_progression_step = 1

    for candidate in candidates:
        if candidate.entry_time < start_ms:
            continue
        if candidate.entry_time > end_ms:
            break

        for order in list(open_orders):
            if order["expires_at"] > candidate.entry_time:
                continue
            _settle(order, klines[order["exit_index"]], config)
            balance = round(balance + order["pnl"], 4)
            next_stake, stake_progression_step = _next_stake_after_settlement(order, config)
            open_orders.remove(order)

        if candidate.expires_at > end_ms:
            continue
        if candidate.edge >= edge_provider(candidate, candidate.entry_time):
            continue
        if len(open_orders) >= config.max_open_orders:
            continue
        if last_order_time is not None and candidate.entry_time - last_order_time < config.min_order_gap_minutes * 60_000:
            continue
        if config.enable_rolling_edge_guard and _rolling_edge_degraded(orders, candidate, config):
            continue

        stake = round(next_stake, 4)
        win_return = _win_return_for_stake(stake, config)
        order = {
            "id": len(orders) + 1,
            "direction": candidate.direction,
            "timeframe_minutes": 10,
            "level": "A",
            "reason": candidate.reason,
            "score": candidate.score,
            "threshold": candidate.threshold,
            "threshold_segment": candidate.segment,
            "stake_key": candidate.stake_key,
            "stake": stake,
            "win_return": win_return,
            "stake_progression_step": stake_progression_step,
            "entry_price": klines[candidate.entry_index].close,
            "entry_time": candidate.entry_time,
            "expires_at": candidate.expires_at,
            "exit_index": candidate.exit_index,
            "exit_price": None,
            "exit_time": None,
            "result": None,
            "pnl": 0.0,
        }
        orders.append(order)
        open_orders.append(order)
        last_order_time = candidate.entry_time

    for order in list(open_orders):
        if order["expires_at"] <= end_ms:
            _settle(order, klines[order["exit_index"]], config)
            balance = round(balance + order["pnl"], 4)
            next_stake, stake_progression_step = _next_stake_after_settlement(order, config)
            open_orders.remove(order)

    settled = [order for order in orders if order["result"]]
    return {
        "summary": _summary(settled, balance),
        "risk": _risk(settled),
        "by_direction": _group(settled, "direction"),
        "by_direction_segment": _group(settled, "stake_key"),
        "orders": settled,
    }


def _settle(order: dict, exit_kline: Kline, config: BacktestConfig) -> None:
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
    if not config.enable_stake_progression or config.stake <= 0:
        return round(config.win_return, 4)
    return round(stake * (config.win_return / config.stake), 4)


def _next_stake_after_settlement(order: dict, config: BacktestConfig) -> tuple[float, int]:
    if not config.enable_stake_progression:
        return config.stake, 1
    if order.get("result") != "WIN":
        return config.stake, 1
    current_step = int(order.get("stake_progression_step", 1) or 1)
    if current_step >= max(1, config.stake_progression_max_orders):
        return config.stake, 1
    return float(order.get("win_return", config.win_return)), current_step + 1


def _rolling_edge_degraded(orders: Sequence[dict], candidate: Candidate, config: BacktestConfig) -> bool:
    edge_config = RollingEdgeConfig(
        lookback_days=config.rolling_edge_lookback_days,
        min_samples=config.rolling_edge_min_samples,
        min_win_rate=config.rolling_edge_min_win_rate,
        min_ev=config.rolling_edge_min_ev,
    )
    current_item = {
        "entry_time": candidate.entry_time,
        "timeframe_minutes": 10,
        "threshold_segment": candidate.segment,
        "reason": candidate.reason,
    }
    snapshot = rolling_edge_snapshot(orders, current_item, edge_config)
    return should_degrade(snapshot, edge_config)


def _summary(orders: Sequence[dict], balance: float) -> dict:
    wins = [order for order in orders if order["result"] == "WIN"]
    losses = [order for order in orders if order["result"] == "LOSS"]
    total_staked = round(sum(float(order.get("stake", 0.0)) for order in orders), 4)
    fixed_balance = len(wins) * 8.0 - len(losses) * 10.0
    return {
        "total_orders": len(orders),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(orders), 4) if orders else 0.0,
        "balance": round(balance, 4),
        "avg_pnl": round(balance / len(orders), 4) if orders else 0.0,
        "total_staked": total_staked,
        "roi": round(balance / total_staked, 4) if total_staked else 0.0,
        "fixed_balance": round(fixed_balance, 4),
        "fixed_roi": round(fixed_balance / (len(orders) * 10.0), 4) if orders else 0.0,
    }


def _risk(orders: Sequence[dict]) -> dict:
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    loss_streak = 0
    max_loss_streak = 0
    win_streak = 0
    max_win_streak = 0
    for order in orders:
        equity = round(equity + float(order["pnl"]), 4)
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


def _group(orders: Sequence[dict], key: str) -> dict:
    groups: dict[str, list[dict]] = {}
    for order in orders:
        groups.setdefault(str(order[key]), []).append(order)
    return {name: (_summary(group, sum(order["pnl"] for order in group)) | _risk(group)) for name, group in groups.items()}


def _empty_summary() -> dict:
    return {
        "total_orders": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "balance": 0.0,
        "avg_pnl": 0.0,
        "total_staked": 0.0,
        "roi": 0.0,
        "fixed_balance": 0.0,
        "fixed_roi": 0.0,
        "max_drawdown": 0.0,
        "max_loss_streak": 0,
        "max_win_streak": 0,
    }


def _schedule_change_stats(schedule: Sequence[dict]) -> dict:
    total_changed_keys = sum(len(item["changed_keys"]) for item in schedule[1:])
    total_non_default = sum(len(item["edge_map"]) for item in schedule)
    updates = max(len(schedule), 1)
    return {
        "total_changed_keys": total_changed_keys,
        "avg_changed_keys_per_update": round(total_changed_keys / max(len(schedule) - 1, 1), 4),
        "avg_non_default_segments": round(total_non_default / updates, 4),
    }


def _rank_score(summary: dict, risk: dict, changes: dict) -> float:
    return (
        summary["balance"]
        - abs(risk["max_drawdown"]) * 0.35
        - risk["max_loss_streak"] * 20.0
        - changes["avg_changed_keys_per_update"] * 12.0
    )


def _compact_replay(replay: dict) -> dict:
    return {
        "summary": replay["summary"],
        "risk": replay["risk"],
        "by_direction": replay["by_direction"],
        "by_direction_segment": replay["by_direction_segment"],
    }


def _printable_summary(report: dict) -> dict:
    return {
        "coverage": report["coverage"],
        "baseline_edge27": {
            "summary": report["baseline_edge27"]["summary"],
            "risk": report["baseline_edge27"]["risk"],
        },
        "static_two_year_filtered_long_map": {
            "summary": report["static_two_year_filtered_long_map"]["replay"]["summary"],
            "risk": report["static_two_year_filtered_long_map"]["replay"]["risk"],
        },
        "top_cycles": [
            {
                "lookback_days": item["lookback_days"],
                "update_days": item["update_days"],
                "max_step": item["max_step"],
                "updates": item["updates"],
                "summary": item["summary"],
                "risk": item["risk"],
                "edge_change_stats": item["edge_change_stats"],
                "score": round(item["score"], 4),
            }
            for item in report["ranked_cycle_results"][:8]
        ],
    }


def _parse_date(value: str, end_of_day: bool = False) -> int:
    dt = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    ms = int(dt.timestamp() * 1000)
    if end_of_day:
        ms += MS_PER_DAY - 1
    return ms


def _worker_count(value: str) -> int:
    if value == "auto":
        return max(1, (os.cpu_count() or 2) - 1)
    return max(1, int(value))


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
