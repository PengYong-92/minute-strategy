from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from statistics import median
from typing import Sequence
from zoneinfo import ZoneInfo

from app.models import Kline


SHANGHAI = ZoneInfo("Asia/Shanghai")
MINUTE_MS = 60_000
DAY_MS = 86_400_000


@dataclass(frozen=True)
class MarketSequenceConfig:
    lookback_days: int = 7
    horizon_minutes: int = 10
    run_step_minutes: int = 2
    training_stride_minutes: int = 10
    entry_stride_minutes: int = 2
    key_mode: str = "move_run"
    min_samples: int = 20
    min_win_rate: float = 0.60
    min_ev: float = 0.0
    evaluation_hour: int = 7
    evaluation_minute: int = 50
    activation_hour: int = 8
    activation_minute: int = 0

    def normalized(self) -> "MarketSequenceConfig":
        key_mode = self.key_mode if self.key_mode in {
            "move_run",
            "move_run_volume",
            "move_run_volume_rsi",
        } else "move_run"
        return MarketSequenceConfig(
            lookback_days=max(1, int(self.lookback_days)),
            horizon_minutes=max(1, int(self.horizon_minutes)),
            run_step_minutes=max(1, int(self.run_step_minutes)),
            training_stride_minutes=max(1, int(self.training_stride_minutes)),
            entry_stride_minutes=max(1, int(self.entry_stride_minutes)),
            key_mode=key_mode,
            min_samples=max(1, int(self.min_samples)),
            min_win_rate=min(1.0, max(0.0, float(self.min_win_rate))),
            min_ev=float(self.min_ev),
            evaluation_hour=min(23, max(0, int(self.evaluation_hour))),
            evaluation_minute=min(59, max(0, int(self.evaluation_minute))),
            activation_hour=min(23, max(0, int(self.activation_hour))),
            activation_minute=min(59, max(0, int(self.activation_minute))),
        )


@dataclass(frozen=True)
class SequenceTrainingRow:
    entry_time: int
    settled_at: int
    state_key: str
    outcome: str


def run_bucket(length: int) -> str:
    if length <= 1:
        return "1"
    if length == 2:
        return "2"
    if length <= 4:
        return "3-4"
    return "5+"


def selection_window(current_time_ms: int, config: MarketSequenceConfig | None = None) -> dict:
    resolved = (config or MarketSequenceConfig()).normalized()
    current = datetime.fromtimestamp(current_time_ms / 1000, tz=SHANGHAI)
    evaluation_clock = time(resolved.evaluation_hour, resolved.evaluation_minute)
    evaluation_date = current.date()
    if current.timetz().replace(tzinfo=None) < evaluation_clock:
        evaluation_date -= timedelta(days=1)
    cutoff = datetime.combine(evaluation_date, evaluation_clock, tzinfo=SHANGHAI)
    effective_from = datetime.combine(
        evaluation_date,
        time(resolved.activation_hour, resolved.activation_minute),
        tzinfo=SHANGHAI,
    )
    return {
        "lookback_start": _timestamp_ms(cutoff - timedelta(days=resolved.lookback_days)),
        "lookback_end": _timestamp_ms(cutoff),
        "effective_from": _timestamp_ms(effective_from),
        "effective_until": _timestamp_ms(effective_from + timedelta(days=1)),
    }


def build_training_rows(
    klines: Sequence[Kline],
    *,
    lookback_start: int,
    lookback_end: int,
    config: MarketSequenceConfig | None = None,
) -> list[SequenceTrainingRow]:
    resolved = (config or MarketSequenceConfig()).normalized()
    ordered = sorted(klines, key=lambda item: item.close_time)
    close_index = {item.close_time: index for index, item in enumerate(ordered)}
    stride_ms = resolved.training_stride_minutes * MINUTE_MS
    horizon_ms = resolved.horizon_minutes * MINUTE_MS
    rows: list[SequenceTrainingRow] = []
    for index, item in enumerate(ordered):
        if item.close_time < lookback_start or (item.close_time + 1) % stride_ms != 0:
            continue
        settled_at = item.close_time + horizon_ms
        if settled_at >= lookback_end:
            continue
        exit_index = close_index.get(settled_at)
        if exit_index is None:
            continue
        features = _state_features(ordered, index, resolved)
        if features is None:
            continue
        outcome = _move(item.close, ordered[exit_index].close)
        if outcome == "FLAT":
            outcome = "DOWN"
        rows.append(
            SequenceTrainingRow(
                entry_time=item.close_time,
                settled_at=settled_at,
                state_key=_state_key(features, resolved.key_mode),
                outcome=outcome,
            )
        )
    return rows


def build_snapshot_from_rows(
    rows: Sequence[SequenceTrainingRow],
    *,
    evaluated_at: int,
    effective_from: int,
    effective_until: int,
    config: MarketSequenceConfig | None = None,
) -> dict:
    resolved = (config or MarketSequenceConfig()).normalized()
    grouped: dict[str, list[SequenceTrainingRow]] = defaultdict(list)
    for row in rows:
        grouped[row.state_key].append(row)

    states = {}
    selected_states = {}
    for key, group in sorted(grouped.items()):
        up = sum(item.outcome == "UP" for item in group)
        down = sum(item.outcome == "DOWN" for item in group)
        sample_size = up + down
        wins = max(up, down)
        win_rate = wins / sample_size if sample_size else 0.0
        ev = round(18.0 * win_rate - 10.0, 4) if sample_size else 0.0
        direction = "LONG" if up > down else "SHORT" if down > up else "WAIT"
        if sample_size < resolved.min_samples:
            selection_state = "INSUFFICIENT_SAMPLES"
        elif direction == "WAIT":
            selection_state = "TIE"
        elif win_rate < resolved.min_win_rate:
            selection_state = "LOW_WIN_RATE"
        elif ev <= resolved.min_ev:
            selection_state = "LOW_EV"
        else:
            selection_state = "SELECTED"
        summary = {
            "state_key": key,
            "sample_size": sample_size,
            "up": up,
            "down": down,
            "direction": direction,
            "wins": wins,
            "losses": sample_size - wins,
            "win_rate": round(win_rate, 6),
            "wilson95_lower": round(_wilson_lower(wins, sample_size), 6),
            "ev": ev,
            "pnl": round(wins * 8.0 - (sample_size - wins) * 10.0, 4),
            "latest_settled_at": max((item.settled_at for item in group), default=0),
            "selection_state": selection_state,
        }
        states[key] = summary
        if selection_state == "SELECTED":
            selected_states[key] = summary

    effective_local = datetime.fromtimestamp(effective_from / 1000, tz=SHANGHAI)
    return {
        "version": f"MSS-{effective_local.strftime('%Y%m%d-%H%M')}",
        "status": "READY" if selected_states else "NO_EDGE",
        "evaluated_at": int(evaluated_at),
        "effective_from": int(effective_from),
        "effective_until": int(effective_until),
        "config": asdict(resolved),
        "training_samples": len(rows),
        "states": states,
        "selected_states": selected_states,
        "selected_count": len(selected_states),
        "reason": (
            f"启用 {len(selected_states)} 个10分钟序列状态"
            if selected_states
            else "最近窗口没有序列状态达到启用条件"
        ),
    }


def build_daily_snapshot(
    klines: Sequence[Kline],
    evaluated_at_ms: int,
    *,
    config: MarketSequenceConfig | None = None,
) -> dict:
    resolved = (config or MarketSequenceConfig()).normalized()
    window = selection_window(evaluated_at_ms, resolved)
    rows = build_training_rows(
        klines,
        lookback_start=window["lookback_start"],
        lookback_end=window["lookback_end"],
        config=resolved,
    )
    return {
        **build_snapshot_from_rows(
            rows,
            evaluated_at=evaluated_at_ms,
            effective_from=window["effective_from"],
            effective_until=window["effective_until"],
            config=resolved,
        ),
        "lookback_start": window["lookback_start"],
        "lookback_end": window["lookback_end"],
    }


def decide_current_state(
    klines: Sequence[Kline],
    selected_states: dict[str, dict],
    *,
    current_time: int,
    config: MarketSequenceConfig | None = None,
) -> dict:
    resolved = (config or MarketSequenceConfig()).normalized()
    if ((current_time + 1) // MINUTE_MS) % resolved.entry_stride_minutes != 0:
        return _wait_decision("ENTRY_NOT_ALIGNED")
    ordered = sorted((item for item in klines if item.close_time <= current_time), key=lambda item: item.close_time)
    if not ordered or ordered[-1].close_time != current_time:
        return _wait_decision("LATEST_KLINE_MISSING")
    features = _state_features(ordered, len(ordered) - 1, resolved)
    if features is None:
        return _wait_decision("INSUFFICIENT_KLINES")
    key = _state_key(features, resolved.key_mode)
    selected = selected_states.get(key)
    if selected is None:
        return {**_wait_decision("STATE_NOT_SELECTED"), "state_key": key, "features": features}
    return {
        "direction": str(selected["direction"]),
        "selected": True,
        "reason": "STATE_SELECTED",
        "state_key": key,
        "features": features,
        "sample_size": int(selected.get("sample_size", 0)),
        "win_rate": float(selected.get("win_rate", 0.0)),
        "wilson95_lower": float(selected.get("wilson95_lower", 0.0)),
        "ev": float(selected.get("ev", 0.0)),
    }


def _state_features(
    ordered: Sequence[Kline],
    index: int,
    config: MarketSequenceConfig,
) -> dict | None:
    horizon = config.horizon_minutes
    if index < max(horizon, 14):
        return None
    direction = _move(ordered[index - horizon].close, ordered[index].close)
    if direction == "FLAT":
        direction = "DOWN"
    run_length = 0
    cursor = index
    while cursor >= horizon:
        item_direction = _move(ordered[cursor - horizon].close, ordered[cursor].close)
        if item_direction == "FLAT":
            item_direction = "DOWN"
        if item_direction != direction:
            break
        run_length += 1
        cursor -= config.run_step_minutes

    recent_volume = sum(item.volume for item in ordered[max(0, index - horizon + 1) : index + 1])
    prior_volumes = []
    cursor = index - horizon
    for _ in range(24):
        start = cursor - horizon + 1
        if start < 0:
            break
        prior_volumes.append(sum(item.volume for item in ordered[start : cursor + 1]))
        cursor -= horizon
    baseline_volume = median(prior_volumes) if prior_volumes else recent_volume
    volume_ratio = recent_volume / baseline_volume if baseline_volume > 0 else 1.0
    rsi = _rsi(ordered, index)
    return {
        "move": direction,
        "run_length": run_length,
        "run_bucket": run_bucket(run_length),
        "volume_ratio": round(volume_ratio, 6),
        "volume_bucket": "LOW" if volume_ratio < 0.8 else "HIGH" if volume_ratio > 1.3 else "NORMAL",
        "rsi": round(rsi, 4),
        "rsi_bucket": "LOW" if rsi < 35.0 else "HIGH" if rsi > 70.0 else "NORMAL",
    }


def _state_key(features: dict, key_mode: str) -> str:
    parts = [features["move"], features["run_bucket"]]
    if key_mode in {"move_run_volume", "move_run_volume_rsi"}:
        parts.append(features["volume_bucket"])
    if key_mode == "move_run_volume_rsi":
        parts.append(features["rsi_bucket"])
    return "|".join(parts)


def _rsi(ordered: Sequence[Kline], index: int, period: int = 14) -> float:
    changes = [
        ordered[position].close - ordered[position - 1].close
        for position in range(index - period + 1, index + 1)
    ]
    gains = sum(max(change, 0.0) for change in changes) / period
    losses = sum(max(-change, 0.0) for change in changes) / period
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    relative_strength = gains / losses
    return 100.0 - (100.0 / (1.0 + relative_strength))


def _move(entry: float, exit_price: float) -> str:
    if exit_price > entry:
        return "UP"
    if exit_price < entry:
        return "DOWN"
    return "FLAT"


def _wilson_lower(wins: int, total: int, z: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    probability = wins / total
    denominator = 1.0 + z * z / total
    centre = probability + z * z / (2.0 * total)
    margin = z * math.sqrt(
        probability * (1.0 - probability) / total + z * z / (4.0 * total * total)
    )
    return (centre - margin) / denominator


def _wait_decision(reason: str) -> dict:
    return {
        "direction": "WAIT",
        "selected": False,
        "reason": reason,
        "state_key": "",
        "features": {},
        "sample_size": 0,
        "win_rate": 0.0,
        "wilson95_lower": 0.0,
        "ev": 0.0,
    }


def _timestamp_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)
