# 10m Strategy Coarse Replay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a research-only replay harness that screens multiple 10-minute event-contract strategy families on cached BTCUSDT 1-minute data without changing live strategy code.

**Architecture:** Add one standalone research script under `scripts/` and one smoke/invariant test under `tests/`. The script owns data loading, feature computation, candidate generation, event-contract settlement, grouping, ranking, console summary, and JSON report output. Existing `app/` live modules remain read-only and unchanged.

**Tech Stack:** Python standard library, existing Binance Vision ZIP data in `data/`, existing unittest test runner.

---

## File Structure

- Create: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/scripts/research_10m_strategy_coarse_replay.py`
  - Responsibility: run the full two-year coarse replay and write JSON reports.
- Create: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/tests/test_10m_strategy_coarse_replay.py`
  - Responsibility: verify deterministic settlement, drawdown/streak stats, candidate generation on synthetic data, and that the script can run a small sample.
- Do not modify any file under `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/app/`.

## Task 1: Add research script skeleton and data loading

**Files:**
- Create: `scripts/research_10m_strategy_coarse_replay.py`
- Test: `tests/test_10m_strategy_coarse_replay.py`

- [ ] **Step 1: Write smoke tests for ZIP loading and synthetic kline settlement helpers**

Create `tests/test_10m_strategy_coarse_replay.py` with:

```python
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.research_10m_strategy_coarse_replay import (
    Candidate,
    Kline,
    event_contract_pnl,
    load_klines_from_zips,
    settle_candidate,
)


def kline(idx, close, open_price=None, high=None, low=None, volume=100):
    open_price = close if open_price is None else open_price
    high = max(open_price, close) if high is None else high
    low = min(open_price, close) if low is None else low
    return Kline(
        open_time=idx * 60_000,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        close_time=idx * 60_000 + 59_999,
    )


class TenMinuteStrategyCoarseReplayTest(unittest.TestCase):
    def test_load_klines_from_zips_deduplicates_and_sorts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first = Path(temp_dir) / "first.zip"
            second = Path(temp_dir) / "second.zip"
            with zipfile.ZipFile(first, "w") as archive:
                archive.writestr(
                    "first.csv",
                    "120000,100,101,99,100,1,179999\\n60000,100,101,99,99,1,119999\\n",
                )
            with zipfile.ZipFile(second, "w") as archive:
                archive.writestr(
                    "second.csv",
                    "120000,100,101,99,101,1,179999\\n180000,101,102,100,102,1,239999\\n",
                )

            klines = load_klines_from_zips([first, second])

        self.assertEqual([item.open_time for item in klines], [60_000, 120_000, 180_000])
        self.assertEqual(klines[1].close, 101.0)

    def test_event_contract_pnl_uses_10u_18u_event_payout(self):
        self.assertEqual(event_contract_pnl("LONG", 100, 101), ("WIN", 8.0))
        self.assertEqual(event_contract_pnl("LONG", 100, 100), ("LOSS", -10.0))
        self.assertEqual(event_contract_pnl("SHORT", 100, 99), ("WIN", 8.0))
        self.assertEqual(event_contract_pnl("SHORT", 100, 100), ("LOSS", -10.0))

    def test_settle_candidate_uses_10_minute_horizon(self):
        klines = [kline(i, 100 + i) for i in range(20)]
        candidate = Candidate(
            strategy="synthetic",
            family="unit",
            direction="LONG",
            entry_index=5,
            entry_time=klines[5].close_time,
            entry_price=klines[5].close,
            params={"window": 5},
        )

        order = settle_candidate(candidate, klines)

        self.assertEqual(order["result"], "WIN")
        self.assertEqual(order["exit_time"], klines[15].close_time)
        self.assertEqual(order["pnl"], 8.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail before script exists**

Run:

```bash
python3 -m unittest tests.test_10m_strategy_coarse_replay
```

Expected: import failure because `scripts.research_10m_strategy_coarse_replay` does not exist.

- [ ] **Step 3: Implement script skeleton, Kline/Candidate, loader, and settlement**

Create `scripts/research_10m_strategy_coarse_replay.py` with:

```python
import argparse
import csv
import json
import math
import statistics
import time
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT / "data"
DEFAULT_REPORT_DIR = ROOT / "reports"
SYMBOL = "BTCUSDT"
STAKE = 10.0
WIN_RETURN = 18.0
HORIZON_MINUTES = 10


@dataclass(frozen=True)
class Kline:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int


@dataclass(frozen=True)
class Candidate:
    strategy: str
    family: str
    direction: str
    entry_index: int
    entry_time: int
    entry_price: float
    params: dict


def load_klines_from_zips(zip_paths: Sequence[str | Path]) -> list[Kline]:
    by_open_time: dict[int, Kline] = {}
    for zip_path in zip_paths:
        path = Path(zip_path)
        with zipfile.ZipFile(path) as archive:
            csv_names = [name for name in archive.namelist() if name.endswith(".csv")]
            if not csv_names:
                continue
            with archive.open(csv_names[0]) as handle:
                rows = csv.reader(line.decode("utf-8") for line in handle)
                for row in rows:
                    if len(row) < 7 or not row[0].isdigit():
                        continue
                    item = Kline(
                        open_time=_normalize_timestamp(int(row[0])),
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                        close_time=_normalize_timestamp(int(row[6])),
                    )
                    by_open_time[item.open_time] = item
    return [by_open_time[key] for key in sorted(by_open_time)]


def _normalize_timestamp(value: int) -> int:
    return value // 1000 if value >= 10_000_000_000_000 else value


def event_contract_pnl(direction: str, entry_price: float, exit_price: float) -> tuple[str, float]:
    if direction == "LONG":
        won = exit_price > entry_price
    elif direction == "SHORT":
        won = exit_price < entry_price
    else:
        raise ValueError(f"unsupported direction: {direction}")
    return ("WIN", WIN_RETURN - STAKE) if won else ("LOSS", -STAKE)


def settle_candidate(candidate: Candidate, klines: Sequence[Kline]) -> dict:
    exit_index = candidate.entry_index + HORIZON_MINUTES
    if exit_index >= len(klines):
        raise IndexError("candidate does not have enough future klines to settle")
    exit_kline = klines[exit_index]
    result, pnl = event_contract_pnl(candidate.direction, candidate.entry_price, exit_kline.close)
    return {
        "strategy": candidate.strategy,
        "family": candidate.family,
        "direction": candidate.direction,
        "entry_time": candidate.entry_time,
        "exit_time": exit_kline.close_time,
        "entry_price": candidate.entry_price,
        "exit_price": exit_kline.close,
        "result": result,
        "pnl": round(pnl, 4),
        "params": candidate.params,
    }
```

- [ ] **Step 4: Run tests and verify Task 1 passes**

Run:

```bash
python3 -m unittest tests.test_10m_strategy_coarse_replay
```

Expected: 3 tests pass.

## Task 2: Add feature computation and candidate generators

**Files:**
- Modify: `scripts/research_10m_strategy_coarse_replay.py`
- Test: `tests/test_10m_strategy_coarse_replay.py`

- [ ] **Step 1: Add tests for momentum, reversal, and failed-breakout candidates**

Append tests:

```python
    def test_generates_momentum_candidates_on_synthetic_up_move(self):
        from scripts.research_10m_strategy_coarse_replay import generate_candidates

        klines = [kline(i, 100 + i * 0.1, volume=100) for i in range(80)]
        candidates = generate_candidates(klines)
        names = {item.strategy for item in candidates}

        self.assertIn("momentum_3m_long_0bps", names)
        self.assertIn("momentum_5m_long_0bps", names)

    def test_generates_reversal_long_after_drop_and_reclaim(self):
        from scripts.research_10m_strategy_coarse_replay import generate_candidates

        klines = [kline(i, 100.0, volume=100) for i in range(60)]
        for idx in range(60, 70):
            klines.append(kline(idx, 100 - (idx - 59) * 0.2, volume=180))
        klines.append(kline(70, 98.2, open_price=98.0, high=98.3, low=97.5, volume=220))
        klines.extend(kline(i, 98.2, volume=100) for i in range(71, 90))

        candidates = generate_candidates(klines)

        self.assertTrue(any(item.family == "reversal" and item.direction == "LONG" for item in candidates))

    def test_generates_failed_breakout_short_after_high_reclaim_failure(self):
        from scripts.research_10m_strategy_coarse_replay import generate_candidates

        klines = [kline(i, 100.0 + (i % 5) * 0.01, high=100.2, low=99.8, volume=100) for i in range(80)]
        klines.append(kline(80, 100.05, open_price=100.4, high=100.8, low=100.0, volume=220))
        klines.extend(kline(i, 100.0, volume=100) for i in range(81, 100))

        candidates = generate_candidates(klines)

        self.assertTrue(any(item.family == "failed_breakout" and item.direction == "SHORT" for item in candidates))
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python3 -m unittest tests.test_10m_strategy_coarse_replay
```

Expected: failures because `generate_candidates` is not implemented.

- [ ] **Step 3: Implement indicators and candidate generators**

Add functions to the script:

```python
def generate_candidates(klines: Sequence[Kline]) -> list[Candidate]:
    candidates: list[Candidate] = []
    close_values = [item.close for item in klines]
    volumes = [item.volume for item in klines]
    rsi_values = rolling_rsi(close_values, 14)
    boll_positions = rolling_bollinger_position(close_values, 20)
    volume_means = rolling_mean(volumes, 60)
    for index in range(120, len(klines) - HORIZON_MINUTES):
        candidates.extend(momentum_candidates(klines, index, volume_means))
        candidates.extend(reversal_candidates(klines, index, rsi_values, boll_positions))
        candidates.extend(failed_breakout_candidates(klines, index, volume_means, boll_positions))
    return candidates


def momentum_candidates(klines: Sequence[Kline], index: int, volume_means: Sequence[float | None]) -> list[Candidate]:
    result: list[Candidate] = []
    thresholds = [0, 2, 5, 10, 20, 30, 50, 80, 100]
    for window in [3, 5]:
        if index - window < 0:
            continue
        start_price = klines[index - window + 1].open
        end_price = klines[index].close
        ret_bps = _return_bps(start_price, end_price)
        avg_volume = volume_means[index]
        volume_ratio = _safe_ratio(sum(item.volume for item in klines[index - window + 1 : index + 1]) / window, avg_volume)
        candle_ok_long = _window_rejection_ok(klines[index - window + 1 : index + 1], "LONG")
        candle_ok_short = _window_rejection_ok(klines[index - window + 1 : index + 1], "SHORT")
        for threshold in thresholds:
            if ret_bps >= threshold and candle_ok_long:
                result.append(_candidate("momentum", f"momentum_{window}m_long_{threshold}bps", "LONG", index, klines[index], {"window": window, "min_bps": threshold, "volume_ratio": round(volume_ratio, 4)}))
            if ret_bps <= -threshold and candle_ok_short:
                result.append(_candidate("momentum", f"momentum_{window}m_short_{threshold}bps", "SHORT", index, klines[index], {"window": window, "min_bps": threshold, "volume_ratio": round(volume_ratio, 4)}))
    return result


def reversal_candidates(klines: Sequence[Kline], index: int, rsi_values: Sequence[float | None], boll_positions: Sequence[float | None]) -> list[Candidate]:
    result: list[Candidate] = []
    move_thresholds = [10, 20, 30, 50, 80, 100]
    low_rsi_thresholds = [30, 35, 40, 45]
    high_rsi_thresholds = [55, 60, 65, 70]
    low_boll_thresholds = [0.1, 0.2, 0.3, 0.35]
    high_boll_thresholds = [0.65, 0.7, 0.8, 0.9]
    rsi = rsi_values[index]
    boll = boll_positions[index]
    if rsi is None or boll is None:
        return result
    latest = klines[index]
    has_lower_reclaim = _lower_reclaim(latest)
    has_upper_rejection = _upper_rejection(latest)
    for window in [3, 5, 10]:
        if index - window < 0:
            continue
        ret_bps = _return_bps(klines[index - window + 1].open, latest.close)
        for threshold in move_thresholds:
            if ret_bps <= -threshold and has_lower_reclaim:
                for rsi_limit in low_rsi_thresholds:
                    for boll_limit in low_boll_thresholds:
                        if rsi <= rsi_limit or boll <= boll_limit:
                            result.append(_candidate("reversal", f"drop_reclaim_{window}m_{threshold}bps_rsi{rsi_limit}_boll{boll_limit}", "LONG", index, latest, {"window": window, "min_drop_bps": threshold, "rsi_limit": rsi_limit, "boll_limit": boll_limit, "rsi": round(rsi, 4), "boll": round(boll, 4)}))
            if ret_bps >= threshold and has_upper_rejection:
                for rsi_limit in high_rsi_thresholds:
                    for boll_limit in high_boll_thresholds:
                        if rsi >= rsi_limit or boll >= boll_limit:
                            result.append(_candidate("reversal", f"rise_reject_{window}m_{threshold}bps_rsi{rsi_limit}_boll{boll_limit}", "SHORT", index, latest, {"window": window, "min_rise_bps": threshold, "rsi_limit": rsi_limit, "boll_limit": boll_limit, "rsi": round(rsi, 4), "boll": round(boll, 4)}))
    return result


def failed_breakout_candidates(klines: Sequence[Kline], index: int, volume_means: Sequence[float | None], boll_positions: Sequence[float | None]) -> list[Candidate]:
    result: list[Candidate] = []
    latest = klines[index]
    avg_volume = volume_means[index]
    if not avg_volume:
        return result
    volume_ratio = latest.volume / avg_volume
    boll = boll_positions[index]
    for lookback in [30, 60, 120]:
        if index - lookback < 0:
            continue
        previous = klines[index - lookback : index]
        prior_high = max(item.high for item in previous)
        prior_low = min(item.low for item in previous)
        for breakout_bps in [0, 5, 10, 20]:
            broke_high = latest.high >= prior_high * (1 + breakout_bps / 10_000)
            failed_high = latest.close <= prior_high
            broke_low = latest.low <= prior_low * (1 - breakout_bps / 10_000)
            failed_low = latest.close >= prior_low
            for min_volume_ratio in [1.0, 1.2, 1.5, 2.0]:
                if volume_ratio < min_volume_ratio:
                    continue
                if broke_high and failed_high and (boll is None or boll >= 0.65):
                    result.append(_candidate("failed_breakout", f"failed_high_{lookback}m_{breakout_bps}bps_vol{min_volume_ratio}", "SHORT", index, latest, {"lookback": lookback, "breakout_bps": breakout_bps, "min_volume_ratio": min_volume_ratio, "volume_ratio": round(volume_ratio, 4), "boll": None if boll is None else round(boll, 4)}))
                if broke_low and failed_low and (boll is None or boll <= 0.35):
                    result.append(_candidate("failed_breakout", f"failed_low_{lookback}m_{breakout_bps}bps_vol{min_volume_ratio}", "LONG", index, latest, {"lookback": lookback, "breakout_bps": breakout_bps, "min_volume_ratio": min_volume_ratio, "volume_ratio": round(volume_ratio, 4), "boll": None if boll is None else round(boll, 4)}))
    return result
```

Also add helper functions used above:

```python
def _candidate(family: str, strategy: str, direction: str, index: int, kline: Kline, params: dict) -> Candidate:
    return Candidate(strategy=strategy, family=family, direction=direction, entry_index=index, entry_time=kline.close_time, entry_price=kline.close, params=params)


def _return_bps(start: float, end: float) -> float:
    return ((end / start) - 1.0) * 10_000 if start else 0.0


def _safe_ratio(value: float, denominator: float | None) -> float:
    return value / denominator if denominator else 0.0


def rolling_mean(values: Sequence[float], window: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    total = 0.0
    for index, value in enumerate(values):
        total += value
        if index >= window:
            total -= values[index - window]
        if index >= window - 1:
            result[index] = total / window
    return result


def rolling_rsi(closes: Sequence[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(closes)
    gains: list[float] = []
    losses: list[float] = []
    for index in range(1, len(closes)):
        change = closes[index] - closes[index - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
        if len(gains) > period:
            gains.pop(0)
            losses.pop(0)
        if len(gains) == period:
            avg_gain = sum(gains) / period
            avg_loss = sum(losses) / period
            result[index] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return result


def rolling_bollinger_position(closes: Sequence[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(closes)
    for index in range(period - 1, len(closes)):
        window = closes[index - period + 1 : index + 1]
        middle = statistics.mean(window)
        deviation = statistics.pstdev(window)
        upper = middle + 2 * deviation
        lower = middle - 2 * deviation
        result[index] = 0.5 if upper == lower else (closes[index] - lower) / (upper - lower)
    return result


def _window_rejection_ok(window: Sequence[Kline], direction: str) -> bool:
    latest = window[-1]
    total_range = latest.high - latest.low
    if total_range <= 0:
        return True
    upper_shadow = latest.high - max(latest.open, latest.close)
    lower_shadow = min(latest.open, latest.close) - latest.low
    if direction == "LONG":
        return upper_shadow / total_range <= 0.6
    return lower_shadow / total_range <= 0.6


def _lower_reclaim(kline: Kline) -> bool:
    total_range = kline.high - kline.low
    if total_range <= 0:
        return False
    close_strength = (kline.close - kline.low) / total_range
    lower_shadow = min(kline.open, kline.close) - kline.low
    upper_shadow = kline.high - max(kline.open, kline.close)
    return close_strength >= 0.6 and lower_shadow >= upper_shadow


def _upper_rejection(kline: Kline) -> bool:
    total_range = kline.high - kline.low
    if total_range <= 0:
        return False
    close_strength = (kline.close - kline.low) / total_range
    upper_shadow = kline.high - max(kline.open, kline.close)
    lower_shadow = min(kline.open, kline.close) - kline.low
    return close_strength <= 0.4 and upper_shadow >= lower_shadow
```

- [ ] **Step 4: Run tests and verify candidate generators pass**

Run:

```bash
python3 -m unittest tests.test_10m_strategy_coarse_replay
```

Expected: all tests pass.

## Task 3: Add replay, stats, grouping, ranking, and report writer

**Files:**
- Modify: `scripts/research_10m_strategy_coarse_replay.py`
- Test: `tests/test_10m_strategy_coarse_replay.py`

- [ ] **Step 1: Add tests for stats and report shape**

Append tests:

```python
    def test_replay_applies_one_open_order_and_10_minute_gap(self):
        from scripts.research_10m_strategy_coarse_replay import replay_candidates

        klines = [kline(i, 100 + i) for i in range(40)]
        candidates = [
            Candidate("s1", "unit", "LONG", 5, klines[5].close_time, klines[5].close, {}),
            Candidate("s1", "unit", "LONG", 6, klines[6].close_time, klines[6].close, {}),
            Candidate("s1", "unit", "LONG", 16, klines[16].close_time, klines[16].close, {}),
        ]

        orders = replay_candidates(candidates, klines, enforce_cooldown=True)

        self.assertEqual(len(orders), 2)
        self.assertEqual([order["entry_time"] for order in orders], [klines[5].close_time, klines[16].close_time])

    def test_summarize_strategy_reports_risk_and_recent_windows(self):
        from scripts.research_10m_strategy_coarse_replay import summarize_strategy

        orders = [
            {"strategy": "s", "family": "unit", "direction": "LONG", "entry_time": 1714521600000, "result": "WIN", "pnl": 8.0, "params": {}},
            {"strategy": "s", "family": "unit", "direction": "LONG", "entry_time": 1714522200000, "result": "LOSS", "pnl": -10.0, "params": {}},
            {"strategy": "s", "family": "unit", "direction": "LONG", "entry_time": 1714522800000, "result": "LOSS", "pnl": -10.0, "params": {}},
        ]

        summary = summarize_strategy("s", orders)

        self.assertEqual(summary["orders"], 3)
        self.assertEqual(summary["wins"], 1)
        self.assertEqual(summary["max_loss_streak"], 2)
        self.assertLess(summary["max_drawdown"], 0)
        self.assertIn("by_month", summary)
        self.assertIn("recent_6m", summary)
        self.assertIn("recent_3m", summary)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python3 -m unittest tests.test_10m_strategy_coarse_replay
```

Expected: failures because replay/stats functions are missing.

- [ ] **Step 3: Implement replay and stats**

Add:

```python
def replay_candidates(candidates: Sequence[Candidate], klines: Sequence[Kline], enforce_cooldown: bool = True) -> list[dict]:
    ordered = sorted(candidates, key=lambda item: (item.entry_time, item.strategy))
    orders: list[dict] = []
    last_entry_time_by_strategy: dict[str, int] = {}
    for candidate in ordered:
        if candidate.entry_index + HORIZON_MINUTES >= len(klines):
            continue
        if enforce_cooldown:
            last_entry = last_entry_time_by_strategy.get(candidate.strategy)
            if last_entry is not None and candidate.entry_time - last_entry < HORIZON_MINUTES * 60_000:
                continue
        order = settle_candidate(candidate, klines)
        orders.append(order)
        last_entry_time_by_strategy[candidate.strategy] = candidate.entry_time
    return orders


def summarize_all(orders: Sequence[dict]) -> list[dict]:
    by_strategy: dict[str, list[dict]] = defaultdict(list)
    for order in orders:
        by_strategy[order["strategy"]].append(order)
    summaries = [summarize_strategy(strategy, strategy_orders) for strategy, strategy_orders in by_strategy.items()]
    return sorted(summaries, key=lambda item: (item["qualified"], item["pnl"], item["win_rate"], -abs(item["max_drawdown"])), reverse=True)


def summarize_strategy(strategy: str, orders: Sequence[dict]) -> dict:
    items = sorted(orders, key=lambda item: item["entry_time"])
    wins = [item for item in items if item["result"] == "WIN"]
    pnl = round(sum(item["pnl"] for item in items), 4)
    latest_time = max((item["entry_time"] for item in items), default=0)
    recent_6m = [item for item in items if latest_time and item["entry_time"] >= latest_time - 183 * 24 * 60 * 60 * 1000]
    recent_3m = [item for item in items if latest_time and item["entry_time"] >= latest_time - 92 * 24 * 60 * 60 * 1000]
    summary = {
        "strategy": strategy,
        "family": items[0]["family"] if items else "",
        "direction": _direction_label(items),
        "params": items[0]["params"] if items else {},
        "orders": len(items),
        "wins": len(wins),
        "losses": len(items) - len(wins),
        "win_rate": _ratio(len(wins), len(items)),
        "pnl": pnl,
        "avg_pnl": _ratio(pnl, len(items)),
        "total_staked": round(len(items) * STAKE, 4),
        "roi": _ratio(pnl, len(items) * STAKE),
        "max_drawdown": _max_drawdown(items),
        "max_loss_streak": _max_loss_streak(items),
        "recent_6m": _compact_stats(recent_6m),
        "recent_3m": _compact_stats(recent_3m),
        "by_month": _group_stats(items, lambda item: _dt(item["entry_time"]).strftime("%Y-%m")),
        "by_utc_hour": _group_stats(items, lambda item: f"{_dt(item['entry_time']).hour:02d}"),
        "by_weekpart": _group_stats(items, lambda item: "WE" if _dt(item["entry_time"]).weekday() >= 5 else "WD"),
        "by_direction": _group_stats(items, lambda item: item["direction"]),
    }
    summary["qualified"] = (
        summary["orders"] >= 300
        and summary["win_rate"] > STAKE / WIN_RETURN
        and summary["pnl"] > 0
        and summary["recent_6m"]["pnl"] >= 0
        and summary["recent_3m"]["win_rate"] >= STAKE / WIN_RETURN - 0.02
    )
    return summary
```

Add helpers:

```python
def _ratio(numerator: float, denominator: float) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _compact_stats(items: Sequence[dict]) -> dict:
    wins = sum(1 for item in items if item["result"] == "WIN")
    pnl = round(sum(item["pnl"] for item in items), 4)
    return {"orders": len(items), "wins": wins, "losses": len(items) - wins, "win_rate": _ratio(wins, len(items)), "pnl": pnl}


def _group_stats(items: Sequence[dict], key_func: Callable[[dict], str]) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        groups[key_func(item)].append(item)
    return {key: _compact_stats(value) for key, value in sorted(groups.items())}


def _max_drawdown(items: Sequence[dict]) -> float:
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for item in sorted(items, key=lambda row: row["entry_time"]):
        equity = round(equity + item["pnl"], 4)
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    return round(drawdown, 4)


def _max_loss_streak(items: Sequence[dict]) -> int:
    current = 0
    worst = 0
    for item in sorted(items, key=lambda row: row["entry_time"]):
        if item["result"] == "LOSS":
            current += 1
            worst = max(worst, current)
        else:
            current = 0
    return worst


def _direction_label(items: Sequence[dict]) -> str:
    directions = sorted({item["direction"] for item in items})
    return directions[0] if len(directions) == 1 else "BOTH"
```

- [ ] **Step 4: Add report writer and CLI**

Add:

```python
def run_replay(data_dir: Path = DEFAULT_DATA_DIR, report_dir: Path = DEFAULT_REPORT_DIR, limit_files: int | None = None) -> dict:
    zip_paths = sorted(data_dir.glob(f"{SYMBOL}-1m-*.zip"))
    if limit_files:
        zip_paths = zip_paths[-limit_files:]
    started = time.perf_counter()
    klines = load_klines_from_zips(zip_paths)
    candidates = generate_candidates(klines)
    orders = replay_candidates(candidates, klines, enforce_cooldown=True)
    summaries = summarize_all(orders)
    qualified = [item for item in summaries if item["qualified"]]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbol": SYMBOL,
        "data_files": len(zip_paths),
        "from": _dt(klines[0].open_time).isoformat() if klines else "",
        "to": _dt(klines[-1].open_time).isoformat() if klines else "",
        "klines": len(klines),
        "candidate_signals": len(candidates),
        "orders": len(orders),
        "strategy_count": len(summaries),
        "qualified_count": len(qualified),
        "top": summaries[:50],
        "qualified": qualified,
        "all": summaries,
        "elapsed_seconds": round(time.perf_counter() - started, 4),
    }
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"10m_strategy_coarse_replay_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def print_summary(report: dict) -> None:
    print("=== 10分钟策略粗筛回放 ===")
    print(f"数据: {report['from']} -> {report['to']} files={report['data_files']} klines={report['klines']}")
    print(f"候选信号: {report['candidate_signals']} 订单: {report['orders']} 策略数: {report['strategy_count']} 达标: {report['qualified_count']}")
    print(f"报告: {report['report_path']}")
    print("\\nTop 20:")
    for item in report["top"][:20]:
        print(
            f"{item['strategy']} family={item['family']} dir={item['direction']} "
            f"orders={item['orders']} win={item['win_rate']:.2%} pnl={item['pnl']:.2f} "
            f"mdd={item['max_drawdown']:.2f} l_streak={item['max_loss_streak']} "
            f"6m={item['recent_6m']['pnl']:.2f} 3m_win={item['recent_3m']['win_rate']:.2%} "
            f"qualified={item['qualified']}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Research-only coarse replay for BTCUSDT 10m event-contract strategies.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--limit-files", type=int, default=None)
    args = parser.parse_args(argv)
    report = run_replay(data_dir=args.data_dir, report_dir=args.report_dir, limit_files=args.limit_files)
    print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run tests and a small smoke replay**

Run:

```bash
python3 -m unittest tests.test_10m_strategy_coarse_replay
python3 scripts/research_10m_strategy_coarse_replay.py --limit-files 1
```

Expected:
- tests pass.
- script prints summary and writes a JSON report.

## Task 4: Run full replay and produce findings

**Files:**
- No code modifications expected after Task 3 unless tests reveal a defect in the research script.

- [ ] **Step 1: Run full replay**

Run:

```bash
python3 scripts/research_10m_strategy_coarse_replay.py
```

Expected:
- command completes.
- JSON report path printed.
- Top 20 strategy table printed.

- [ ] **Step 2: Inspect report programmatically**

Run:

```bash
python3 - <<'PY'
import json
from pathlib import Path
latest = sorted(Path("reports").glob("10m_strategy_coarse_replay_*.json"))[-1]
report = json.loads(latest.read_text(encoding="utf-8"))
print("report", latest)
print("qualified", report["qualified_count"])
for item in report["top"][:10]:
    print(item["strategy"], item["family"], item["direction"], item["orders"], item["win_rate"], item["pnl"], item["max_drawdown"], item["recent_6m"], item["recent_3m"])
PY
```

Expected:
- prints report path.
- prints qualified count and top 10.

- [ ] **Step 3: Verify business code was not modified**

Run:

```bash
git diff --name-only
git status --short
```

Expected:
- only `scripts/research_10m_strategy_coarse_replay.py`, `tests/test_10m_strategy_coarse_replay.py`, and generated ignored report files are changed/untracked.
- no `app/` files are listed.

- [ ] **Step 4: Summarize findings in Chinese**

Provide:

- data range and number of 1m klines.
- total strategy variants screened.
- number of qualified variants.
- best strategy per family.
- whether LONG has viable candidate.
- whether SHORT has viable candidate.
- which high-PnL strategies fail recent-window checks.
- recommended next step: roll-forward validation, observation-only integration, or reject.

## Plan Self-Review

- Spec coverage: data loading, settlement, candidate families, grouping, ranking, reports, and no-business-code constraint are all mapped to tasks.
- Placeholder scan: no TBD/TODO/fill-in steps remain.
- Type consistency: `Kline`, `Candidate`, `generate_candidates`, `replay_candidates`, `summarize_strategy`, and `run_replay` are defined before use.
- Scope: one research script plus one test file; live strategy code remains untouched.

