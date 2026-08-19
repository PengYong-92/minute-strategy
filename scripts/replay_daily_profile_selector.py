#!/usr/bin/env python3
import argparse
import json
import math
import sqlite3
import sys
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Sequence
from contextlib import closing
from dataclasses import asdict, dataclass, fields
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.adaptive_profile_state import (
    ADAPTIVE_PROFILE_STATE_VERSION,
    evaluate_adaptive_profile_state,
    rebuild_adaptive_profile_states,
)
from app.daily_profile_selector import (
    SHANGHAI,
    DailyProfileSelectorConfig,
    build_daily_selection,
    profile_key,
    selection_window,
)
from app.models import ObservationSignal
from app.stake_progression import TWO_STAGE_VERSION, TwoStageStakeProgression
from app.storage import (
    _LINKED_CONTEXT_COLUMNS,
    _hydrate_decision_linked_payload,
)


ACCEPTANCE = {
    "total_win_rate_min": 0.60,
    "direction_win_rate_min": 0.5556,
    "total_order_retention_min": 0.80,
    "direction_order_retention_min": 0.70,
    "base_first_order_retention_min": 0.85,
    "ev_min": 0.0,
    "positive_oos_windows_min": 2,
}
GUARD_REJECTIONS = (
    "profile_not_selected",
    "adaptive_profile_paused",
    "adaptive_profile_second_blocked",
    "global_capacity",
    "direction_capacity_long",
    "direction_capacity_short",
    "cooldown",
    "three_loss_pause",
)
EQUALITY_FIELDS = (
    "order_id",
    "direction",
    "opened_at",
    "settled_at",
    "expires_at",
    "stake",
    "win_return",
    "progression_step",
    "progression_source_order_id",
    "progression_version",
    "progression_allowed",
)
OBSERVATION_LIFECYCLE_FIELDS = (
    "observation_key",
    "status",
    "result",
    "opened_at",
    "expires_at",
    "settled_at",
    "exit_price",
    "pnl",
)
REQUIRED_OBSERVATION_LIFECYCLE_FIELDS = frozenset(
    OBSERVATION_LIFECYCLE_FIELDS[:-2]
)


@dataclass(frozen=True)
class ReplayExecutionConfig:
    max_open_orders: int
    max_open_long_orders: int
    max_open_short_orders: int
    min_order_gap_ms: int
    stake: float
    win_return: float
    stake_progression_enabled: bool
    stake_progression_max_orders: int
    stake_progression_max_active: int
    stake_progression_second_stake: float
    stake_progression_base_only_segments: tuple[str, ...]

    def normalized(self) -> "ReplayExecutionConfig":
        integer_values = {
            "max_open_orders": self.max_open_orders,
            "max_open_long_orders": self.max_open_long_orders,
            "max_open_short_orders": self.max_open_short_orders,
            "stake_progression_max_orders": self.stake_progression_max_orders,
            "stake_progression_max_active": self.stake_progression_max_active,
        }
        normalized_integers: dict[str, int] = {}
        for name, value in integer_values.items():
            if isinstance(value, bool):
                raise ValueError(f"{name} must be a positive integer")
            normalized = int(value)
            if normalized <= 0 or normalized != value:
                raise ValueError(f"{name} must be a positive integer")
            normalized_integers[name] = normalized
        if normalized_integers["max_open_long_orders"] > normalized_integers["max_open_orders"]:
            raise ValueError("max_open_long_orders must not exceed max_open_orders")
        if normalized_integers["max_open_short_orders"] > normalized_integers["max_open_orders"]:
            raise ValueError("max_open_short_orders must not exceed max_open_orders")
        if normalized_integers["stake_progression_max_orders"] != 2:
            raise ValueError("stake_progression_max_orders must be 2 for TWO_STAGE_V1")
        if (
            normalized_integers["stake_progression_max_active"]
            > normalized_integers["max_open_orders"]
        ):
            raise ValueError("stake_progression_max_active must not exceed max_open_orders")

        if isinstance(self.min_order_gap_ms, bool):
            raise ValueError("min_order_gap_ms must be a non-negative integer")
        min_order_gap_ms = int(self.min_order_gap_ms)
        if min_order_gap_ms < 0 or min_order_gap_ms != self.min_order_gap_ms:
            raise ValueError("min_order_gap_ms must be a non-negative integer")
        if type(self.stake_progression_enabled) is not bool:
            raise ValueError("stake_progression_enabled must be explicitly true or false")

        amounts: dict[str, float] = {}
        for name, value in (
            ("stake", self.stake),
            ("win_return", self.win_return),
            ("stake_progression_second_stake", self.stake_progression_second_stake),
        ):
            if isinstance(value, bool):
                raise ValueError(f"{name} must be a positive finite amount")
            amount = float(value)
            if not math.isfinite(amount) or amount <= 0:
                raise ValueError(f"{name} must be a positive finite amount")
            amounts[name] = amount
        if amounts["win_return"] <= amounts["stake"]:
            raise ValueError("win_return must exceed stake")
        if float(self.stake_progression_second_stake) != float(self.win_return):
            raise ValueError("stake_progression_second_stake must equal win_return")

        if isinstance(self.stake_progression_base_only_segments, str):
            raise ValueError("stake_progression_base_only_segments must be a sequence")
        segments = tuple(
            sorted(
                {
                    str(item).strip().upper()
                    for item in self.stake_progression_base_only_segments
                    if str(item).strip()
                }
            )
        )
        if segments:
            raise ValueError("stake_progression_base_only_segments must be empty")
        return ReplayExecutionConfig(
            **normalized_integers,
            min_order_gap_ms=min_order_gap_ms,
            stake=amounts["stake"],
            win_return=amounts["win_return"],
            stake_progression_enabled=self.stake_progression_enabled,
            stake_progression_second_stake=amounts["stake_progression_second_stake"],
            stake_progression_base_only_segments=segments,
        )

    @property
    def enable_stake_progression(self) -> bool:
        return self.stake_progression_enabled

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self.normalized())
        payload["stake_progression_base_only_segments"] = list(
            payload["stake_progression_base_only_segments"]
        )
        payout_ratio = self.win_return / self.stake - 1.0
        payload["stake_progression_second_win_return"] = round(
            self.stake_progression_second_stake * (1.0 + payout_ratio),
            4,
        )
        payload["progression_version"] = TWO_STAGE_VERSION
        return payload


ProductionReplayConfig = ReplayExecutionConfig


def _observation_lifecycle_select(columns: set[str]) -> tuple[str, ...]:
    return tuple(
        f"observation_signals.{field} as lifecycle_{field}"
        for field in OBSERVATION_LIFECYCLE_FIELDS
        if field in columns
    )


def load_replay_observations(
    db_path: str | Path,
    symbol: str,
) -> list[ObservationSignal]:
    path = Path(db_path).resolve()
    uri = f"{path.as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("pragma query_only = on")
        tables = {
            str(row["name"])
            for row in connection.execute(
                "select name from sqlite_master where type = 'table'"
            )
        }
        if "observation_signals" not in tables:
            raise ValueError("observation_signals table is missing")
        observation_columns = {
            str(row["name"])
            for row in connection.execute("pragma table_info(observation_signals)")
        }
        context_columns = (
            {
                str(row["name"])
                for row in connection.execute("pragma table_info(decision_contexts)")
            }
            if "decision_contexts" in tables
            else set()
        )
        linked_context_columns = {
            "decision_id",
            "context_version",
            "runtime_config_hash",
            "strategy_build_id",
            "symbol",
            "closed_kline_at_ms",
            "candidate_origin",
            "input_payload",
            "outcome_payload",
        }
        where = "where observation_signals.symbol = ?"
        if "status" in observation_columns:
            where += " and observation_signals.status = 'SETTLED'"
        lifecycle_select = _observation_lifecycle_select(observation_columns)
        if (
            "decision_id" in observation_columns
            and linked_context_columns <= context_columns
        ):
            select_columns = ",\n                       ".join(
                (
                    "observation_signals.payload",
                    *lifecycle_select,
                    _LINKED_CONTEXT_COLUMNS.strip(),
                )
            )
            rows = connection.execute(
                f"""
                select {select_columns}
                from observation_signals
                left join decision_contexts
                  on decision_contexts.symbol = observation_signals.symbol
                 and decision_contexts.decision_id = observation_signals.decision_id
                {where}
                order by observation_signals.settled_at,
                         observation_signals.opened_at,
                         observation_signals.observation_key
                """,
                (symbol.upper(),),
            ).fetchall()
            hydrate_lifecycle = True
        elif REQUIRED_OBSERVATION_LIFECYCLE_FIELDS <= observation_columns:
            select_columns = ",\n                       ".join(
                ("observation_signals.payload", *lifecycle_select)
            )
            rows = connection.execute(
                f"""
                select {select_columns}
                from observation_signals
                {where}
                order by observation_signals.settled_at,
                         observation_signals.opened_at,
                         observation_signals.observation_key
                """,
                (symbol.upper(),),
            ).fetchall()
            hydrate_lifecycle = True
        else:
            rows = connection.execute(
                f"select observation_signals.payload from observation_signals {where}",
                (symbol.upper(),),
            ).fetchall()
            hydrate_lifecycle = False

    accepted = {item.name for item in fields(ObservationSignal)}
    observations = []
    for row in rows:
        payload = json.loads(row["payload"])
        if hydrate_lifecycle:
            payload = _hydrate_decision_linked_payload(payload, row)
        observations.append(
            ObservationSignal(
                **{key: value for key, value in payload.items() if key in accepted}
            )
        )
    return sorted(observations, key=_settlement_event_key)


def replay_daily_profile_selection(
    observations: Sequence[ObservationSignal],
    config: DailyProfileSelectorConfig,
    *,
    execution: ReplayExecutionConfig | None = None,
    require_full_lookback: bool = True,
    max_open_orders: int | None = None,
    max_open_long_orders: int | None = None,
    max_open_short_orders: int | None = None,
    min_order_gap_ms: int | None = None,
    stake: float | None = None,
    win_return: float | None = None,
    stake_progression_enabled: bool | None = None,
    stake_progression_max_orders: int | None = None,
    stake_progression_max_active: int | None = None,
    stake_progression_second_stake: float | None = None,
    stake_progression_base_only_segments: Sequence[str] | None = None,
) -> dict[str, Any]:
    config = config.normalized()
    execution = _resolve_execution(
        execution,
        max_open_orders=max_open_orders,
        max_open_long_orders=max_open_long_orders,
        max_open_short_orders=max_open_short_orders,
        min_order_gap_ms=min_order_gap_ms,
        stake=stake,
        win_return=win_return,
        stake_progression_enabled=stake_progression_enabled,
        stake_progression_max_orders=stake_progression_max_orders,
        stake_progression_max_active=stake_progression_max_active,
        stake_progression_second_stake=stake_progression_second_stake,
        stake_progression_base_only_segments=stake_progression_base_only_segments,
    )
    settled = sorted(
        (
            item
            for item in observations
            if item.status == "SETTLED"
            and item.result in {"WIN", "LOSS"}
            and item.settled_at is not None
        ),
        key=_settlement_event_key,
    )
    snapshots = _build_schedule(
        settled,
        config,
        require_full_lookback=require_full_lookback,
    )
    event_rows = _build_adaptive_event_rows(settled)
    adaptive_timeline = _adaptive_timeline(event_rows)
    baseline_result = _execute_replay(
        settled,
        snapshots,
        execution,
        adaptive_timeline,
        apply_adaptive=False,
        include_structure_shadow=False,
    )
    structure_result = _execute_replay(
        settled,
        snapshots,
        execution,
        adaptive_timeline,
        apply_adaptive=False,
        include_structure_shadow=True,
    )
    candidate_result = _execute_replay(
        settled,
        snapshots,
        execution,
        adaptive_timeline,
        apply_adaptive=True,
        include_structure_shadow=False,
    )

    oos_start, oos_end = _oos_bounds(settled, snapshots)
    baseline = _execution_report(baseline_result, oos_start, oos_end)
    candidate = _execution_report(candidate_result, oos_start, oos_end)
    acceptance = evaluate_release_gates(baseline, candidate)
    equality = build_structure_shadow_equality_report(
        baseline_result["trade_rows"],
        structure_result["trade_rows"],
        baseline_result["webhook_count"],
        structure_result["webhook_count"],
    )
    ranking = rank_passing_configurations(
        [
            {
                "name": "adaptive_candidate",
                "passed": acceptance["passed"],
                "total": candidate["total"],
            }
        ]
    )
    compact_schedule = [_compact_snapshot(item) for item in snapshots]
    data = _data_summary(settled, snapshots, require_full_lookback)
    base_first_retention = round(
        _retention(
            candidate["base_first_orders"],
            baseline["base_first_orders"],
        ),
        6,
    )
    return {
        "config": _config_snapshot(config),
        "execution": execution.to_dict(),
        "data": data,
        "schedule": compact_schedule,
        "daily_snapshots": compact_schedule,
        "schedule_stats": _schedule_stats(compact_schedule),
        "events": event_rows,
        "baseline": baseline,
        "candidate": candidate,
        "total": candidate["total"],
        "by_direction": candidate["by_direction"],
        "base_first_retention": base_first_retention,
        "maximum_drawdown": candidate["total"]["max_drawdown"],
        "longest_loss_streak": candidate["total"]["max_loss_streak"],
        "daily_best": candidate["daily_best"],
        "daily_worst": candidate["daily_worst"],
        "guard_rejections": candidate["guard_rejections"],
        "oos_windows": candidate["oos_windows"],
        "acceptance": acceptance,
        "passing_configuration_ranking": ranking,
        "structure_shadow_equality": equality,
        "eligible_events": candidate_result["eligible_events"],
        "rejections": candidate["guard_rejections"],
        "trades": candidate["total"],
        "by_profile": _group_summaries(candidate["trade_rows"], "profile_key"),
        "by_day": candidate["daily"],
        "trade_rows": candidate["trade_rows"],
        "leakage_violations": _count_leakage_violations(settled, snapshots),
    }


def summarize_trades(trades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(trades, key=_trade_settlement_key)
    wins = sum(1 for item in ordered if item["result"] == "WIN")
    losses = len(ordered) - wins
    raw_pnl = math.fsum(float(item["pnl"]) for item in ordered)
    pnl = round(raw_pnl, 4)
    win_rate = wins / len(ordered) if ordered else 0.0
    low, high = _wilson_interval(wins, len(ordered))
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    max_loss_streak = 0
    loss_streak = 0
    for item in ordered:
        equity += float(item["pnl"])
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        if item["result"] == "LOSS":
            loss_streak += 1
            max_loss_streak = max(max_loss_streak, loss_streak)
        else:
            loss_streak = 0
    by_opened = sorted(
        ordered,
        key=lambda item: (
            int(item["opened_at"]),
            str(item.get("observation_key", "")),
        ),
    )
    return {
        "orders": len(ordered),
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 6),
        "win_rate_ci95": [round(low, 6), round(high, 6)],
        "pnl": pnl,
        "ev": round(raw_pnl / len(ordered), 4) if ordered else 0.0,
        "max_drawdown": round(max_drawdown, 4),
        "max_loss_streak": max_loss_streak,
        "first_trade": _iso(by_opened[0]["opened_at"]) if by_opened else None,
        "last_trade": _iso(by_opened[-1]["opened_at"]) if by_opened else None,
    }


def evaluate_release_gates(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    acceptance: dict[str, float | int] | None = None,
) -> dict[str, Any]:
    thresholds = dict(ACCEPTANCE if acceptance is None else acceptance)
    baseline_total = baseline["total"]
    candidate_total = candidate["total"]
    baseline_by_direction = baseline["by_direction"]
    candidate_by_direction = candidate["by_direction"]
    candidate_trades = candidate.get("trade_rows")

    gates: dict[str, dict[str, Any]] = {}

    def minimum(name: str, actual: float, threshold: float) -> None:
        gates[name] = {
            "actual": round(float(actual), 6),
            "minimum": float(threshold),
            "passed": float(actual) >= float(threshold),
        }

    def not_worse(name: str, actual: float, baseline_value: float) -> None:
        gates[name] = {
            "actual": round(float(actual), 6),
            "baseline": round(float(baseline_value), 6),
            "passed": float(actual) <= float(baseline_value),
        }

    def win_rate(summary: dict[str, Any]) -> float:
        orders = int(summary["orders"])
        return int(summary["wins"]) / orders if orders > 0 else 0.0

    def ev(summary: dict[str, Any], trades: Sequence[dict[str, Any]] | None) -> float:
        orders = int(summary["orders"])
        if orders <= 0:
            return 0.0
        pnl = (
            math.fsum(float(item["pnl"]) for item in trades)
            if trades is not None
            else float(summary["pnl"])
        )
        return pnl / orders

    direction_trades = {
        direction: (
            [item for item in candidate_trades if item["direction"] == direction]
            if candidate_trades is not None
            else None
        )
        for direction in ("LONG", "SHORT")
    }

    minimum("total_win_rate", win_rate(candidate_total), thresholds["total_win_rate_min"])
    for direction in ("LONG", "SHORT"):
        minimum(
            f"{direction.lower()}_win_rate",
            win_rate(candidate_by_direction[direction]),
            thresholds["direction_win_rate_min"],
        )
    minimum(
        "total_order_retention",
        _retention(candidate_total["orders"], baseline_total["orders"]),
        thresholds["total_order_retention_min"],
    )
    for direction in ("LONG", "SHORT"):
        minimum(
            f"{direction.lower()}_order_retention",
            _retention(
                candidate_by_direction[direction]["orders"],
                baseline_by_direction[direction]["orders"],
            ),
            thresholds["direction_order_retention_min"],
        )
    minimum(
        "base_first_order_retention",
        _retention(candidate["base_first_orders"], baseline["base_first_orders"]),
        thresholds["base_first_order_retention_min"],
    )
    minimum("total_ev", ev(candidate_total, candidate_trades), thresholds["ev_min"])
    minimum(
        "long_ev",
        ev(candidate_by_direction["LONG"], direction_trades["LONG"]),
        thresholds["ev_min"],
    )
    minimum(
        "short_ev",
        ev(candidate_by_direction["SHORT"], direction_trades["SHORT"]),
        thresholds["ev_min"],
    )
    positive_windows = 0
    for item in candidate["oos_windows"]:
        if candidate_trades is None:
            window_pnl = float(item["pnl"])
        else:
            lower = int(item["start_at"])
            upper = int(item["end_at"])
            window_pnl = math.fsum(
                float(trade["pnl"])
                for trade in candidate_trades
                if lower <= int(trade["opened_at"]) < upper
            )
        positive_windows += int(window_pnl > 0.0)
    minimum(
        "positive_oos_windows",
        positive_windows,
        thresholds["positive_oos_windows_min"],
    )
    not_worse(
        "maximum_drawdown_not_worse",
        candidate_total["max_drawdown"],
        baseline_total["max_drawdown"],
    )
    not_worse(
        "longest_loss_streak_not_worse",
        candidate_total["max_loss_streak"],
        baseline_total["max_loss_streak"],
    )
    return {
        "thresholds": thresholds,
        "gates": gates,
        "passed": all(item["passed"] for item in gates.values()),
    }


def rank_passing_configurations(
    configurations: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    passing = [
        dict(item)
        for item in configurations
        if bool(item.get("passed", item.get("acceptance", {}).get("passed", False)))
    ]
    return sorted(
        passing,
        key=lambda item: (
            -float(item["total"]["win_rate"]),
            -int(item["total"]["orders"]),
            float(item["total"]["max_drawdown"]),
            str(item.get("name", "")),
        ),
    )


def build_structure_shadow_equality_report(
    baseline_rows: Sequence[dict[str, Any]],
    structure_shadow_rows: Sequence[dict[str, Any]],
    baseline_webhook_count: int,
    structure_shadow_webhook_count: int,
) -> dict[str, Any]:
    differences: list[dict[str, Any]] = []
    row_count = max(len(baseline_rows), len(structure_shadow_rows))
    for index in range(row_count):
        baseline = baseline_rows[index] if index < len(baseline_rows) else None
        shadow = structure_shadow_rows[index] if index < len(structure_shadow_rows) else None
        fields = [
            field
            for field in EQUALITY_FIELDS
            if (baseline or {}).get(field) != (shadow or {}).get(field)
        ]
        if baseline is None or shadow is None:
            fields = ["order_presence", *fields]
        if fields:
            differences.append(
                {
                    "index": index,
                    "fields": fields,
                    "baseline": {
                        field: (baseline or {}).get(field) for field in EQUALITY_FIELDS
                    }
                    if baseline is not None
                    else None,
                    "structure_shadow": {
                        field: (shadow or {}).get(field) for field in EQUALITY_FIELDS
                    }
                    if shadow is not None
                    else None,
                }
            )
    if int(baseline_webhook_count) != int(structure_shadow_webhook_count):
        differences.append(
            {
                "index": None,
                "fields": ["webhook_count"],
                "baseline": int(baseline_webhook_count),
                "structure_shadow": int(structure_shadow_webhook_count),
            }
        )
    return {
        "equal": not differences,
        "independent_execution_count": 2,
        "compared_fields": [*EQUALITY_FIELDS, "webhook_count"],
        "baseline_order_count": len(baseline_rows),
        "structure_shadow_order_count": len(structure_shadow_rows),
        "baseline_webhook_count": int(baseline_webhook_count),
        "structure_shadow_webhook_count": int(structure_shadow_webhook_count),
        "differences": differences,
    }


def _resolve_execution(
    execution: ReplayExecutionConfig | None,
    **explicit: Any,
) -> ReplayExecutionConfig:
    supplied = {name: value for name, value in explicit.items() if value is not None}
    if execution is not None:
        if supplied:
            raise ValueError("execution config and explicit production values are mutually exclusive")
        if not isinstance(execution, ReplayExecutionConfig):
            raise TypeError("execution must be a ReplayExecutionConfig")
        return execution.normalized()
    missing = [name for name, value in explicit.items() if value is None]
    if missing:
        raise ValueError(
            "explicit production execution settings are required: " + ", ".join(missing)
        )
    return ReplayExecutionConfig(
        max_open_orders=explicit["max_open_orders"],
        max_open_long_orders=explicit["max_open_long_orders"],
        max_open_short_orders=explicit["max_open_short_orders"],
        min_order_gap_ms=explicit["min_order_gap_ms"],
        stake=explicit["stake"],
        win_return=explicit["win_return"],
        stake_progression_enabled=explicit["stake_progression_enabled"],
        stake_progression_max_orders=explicit["stake_progression_max_orders"],
        stake_progression_max_active=explicit["stake_progression_max_active"],
        stake_progression_second_stake=explicit["stake_progression_second_stake"],
        stake_progression_base_only_segments=tuple(
            explicit["stake_progression_base_only_segments"]
        ),
    ).normalized()


def _build_schedule(
    observations: Sequence[ObservationSignal],
    config: DailyProfileSelectorConfig,
    *,
    require_full_lookback: bool,
) -> list[dict[str, Any]]:
    if not observations:
        return []
    config = config.normalized()
    first_opened_at = min(item.opened_at for item in observations)
    last_opened_at = max(item.opened_at for item in observations)
    current_date = datetime.fromtimestamp(first_opened_at / 1000, tz=SHANGHAI).date()
    end_date = datetime.fromtimestamp(last_opened_at / 1000, tz=SHANGHAI).date()
    previous = None
    snapshots = []
    while current_date <= end_date:
        evaluated_at = int(
            datetime.combine(
                current_date,
                time(config.evaluation_hour, config.evaluation_minute),
                tzinfo=SHANGHAI,
            ).timestamp()
            * 1000
        )
        stable_window = selection_window(
            evaluated_at,
            lookback_days=config.effective_stable_lookback_days,
            evaluation_hour=config.evaluation_hour,
            evaluation_minute=config.evaluation_minute,
            activation_hour=config.activation_hour,
            activation_minute=config.activation_minute,
        )
        full_lookback = first_opened_at <= stable_window["lookback_start"]
        if (
            (full_lookback or not require_full_lookback)
            and stable_window["effective_from"] <= last_opened_at
        ):
            known = [
                item
                for item in observations
                if item.settled_at is not None and item.settled_at < evaluated_at
            ]
            snapshot = build_daily_selection(
                known,
                evaluated_at,
                config=config,
                previous_snapshot=previous,
            )
            snapshot["sample_keys"] = [
                item.observation_key
                for item in sorted(known, key=_settlement_event_key)
                if stable_window["lookback_start"]
                <= item.opened_at
                < stable_window["lookback_end"]
            ]
            snapshots.append(snapshot)
            previous = snapshot
        current_date += timedelta(days=1)
    return snapshots


def _build_adaptive_event_rows(
    observations: Sequence[ObservationSignal],
) -> list[dict[str, Any]]:
    history_by_profile: dict[str, list[ObservationSignal]] = defaultdict(list)
    states: dict[str, dict[str, Any]] = {}
    rows = []
    for item in observations:
        key = _observation_profile_key(item)
        before = states.get(key) or _adaptive_state((), key, int(item.settled_at))
        after = before
        if _is_adaptive_profile_key(key):
            evaluated_at = int(item.settled_at) + 1
            cutoff = evaluated_at - 15 * 86_400_000
            history_by_profile[key] = [
                event
                for event in (*history_by_profile[key], item)
                if event.settled_at is not None
                and cutoff <= event.settled_at < evaluated_at
            ]
            rebuilt = rebuild_adaptive_profile_states(
                history_by_profile[key],
                evaluated_at,
            )
            after = rebuilt.get(key) or _adaptive_state((), key, evaluated_at)
            states[key] = after
        rows.append(
            {
                "observation_key": item.observation_key,
                "profile_key": key,
                "direction": item.direction,
                "opened_at": item.opened_at,
                "settled_at": item.settled_at,
                "result": item.result,
                "adaptive_state_before": before["status"],
                "adaptive_state_after": after["status"],
                "adaptive_version": str(after.get("version", "")),
                "n12_before": before["n12"],
                "n12_after": after["n12"],
                "n20_before": before["n20"],
                "n20_after": after["n20"],
                "adaptive_evaluated_at": int(after.get("evaluated_at", 0)),
            }
        )
    return rows


def _adaptive_timeline(
    event_rows: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    by_profile: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    settled_times = []
    for row in event_rows:
        settled_at = int(row["settled_at"])
        settled_times.append(settled_at)
        by_profile[row["profile_key"]].append(
            (
                settled_at,
                {
                    "version": row["adaptive_version"],
                    "status": row["adaptive_state_after"],
                    "profile_key": row["profile_key"],
                    "evaluated_at": row["adaptive_evaluated_at"],
                    "n12": row["n12_after"],
                    "n20": row["n20_after"],
                },
            )
        )
    return {"by_profile": dict(by_profile), "settled_times": settled_times}


def _execute_replay(
    observations: Sequence[ObservationSignal],
    snapshots: Sequence[dict[str, Any]],
    execution: ReplayExecutionConfig,
    adaptive_timeline: dict[str, Any],
    *,
    apply_adaptive: bool,
    include_structure_shadow: bool,
) -> dict[str, Any]:
    trades: list[dict[str, Any]] = []
    open_orders: list[dict[str, Any]] = []
    rejections = {name: 0 for name in GUARD_REJECTIONS}
    eligible_events = 0
    webhook_count = 0
    last_order_opened_at: dict[str, int | None] = {"LONG": None, "SHORT": None}
    progression = TwoStageStakeProgression(
        enabled=execution.stake_progression_enabled,
        base_stake=execution.stake,
        base_win_return=execution.win_return,
        max_active=execution.stake_progression_max_active,
        max_open_orders=execution.max_open_orders,
        activated_at=0,
        second_stake=execution.stake_progression_second_stake,
    )

    for snapshot in snapshots:
        grouped: dict[int, list[ObservationSignal]] = defaultdict(list)
        for item in observations:
            if snapshot["effective_from"] <= item.opened_at < snapshot["effective_until"]:
                grouped[item.opened_at].append(item)
        selected_profiles = snapshot.get("selected_profiles") or []
        selected_by_key = {item["key"]: item for item in selected_profiles}

        for opened_at in sorted(grouped):
            rows = grouped[opened_at]
            eligible_events += 1
            _settle_due_orders(open_orders, opened_at, progression)
            by_key: dict[str, ObservationSignal] = {}
            for item in sorted(rows, key=lambda row: row.observation_key):
                by_key.setdefault(_observation_profile_key(item), item)
            chosen = next(
                (by_key[item["key"]] for item in selected_profiles if item["key"] in by_key),
                None,
            )
            if chosen is None:
                rejections["profile_not_selected"] += 1
                continue

            direction = str(chosen.direction).upper()
            profile = selected_by_key[_observation_profile_key(chosen)]
            adaptive, evaluated_through = _adaptive_state_before(
                adaptive_timeline,
                profile["key"],
                opened_at,
            )
            direction_open_count = sum(
                1 for order in open_orders if order["direction"] == direction
            )
            if apply_adaptive and adaptive["status"] == "PAUSED":
                rejections["adaptive_profile_paused"] += 1
                continue
            if (
                apply_adaptive
                and adaptive["status"] == "WATCH"
                and direction_open_count >= 1
            ):
                rejections["adaptive_profile_second_blocked"] += 1
                continue
            if len(open_orders) >= execution.max_open_orders:
                rejections["global_capacity"] += 1
                continue
            direction_limit = (
                execution.max_open_long_orders
                if direction == "LONG"
                else execution.max_open_short_orders
            )
            last_opened = last_order_opened_at[direction]
            if (
                last_opened is not None
                and opened_at - last_opened < execution.min_order_gap_ms
            ):
                rejections["cooldown"] += 1
                continue
            if direction_open_count >= direction_limit:
                rejections[f"direction_capacity_{direction.lower()}"] += 1
                continue
            if _has_three_segment_losses(trades, chosen, opened_at):
                rejections["three_loss_pause"] += 1
                continue

            order_id = len(trades) + 1
            allow_progression = not (
                apply_adaptive and adaptive["status"] == "WATCH"
            )
            if allow_progression:
                terms, _credit = progression.assign(
                    order_id,
                    opened_at,
                    direction=direction,
                )
                order_stake = terms.stake
                order_win_return = terms.win_return
                progression_step = terms.step
                progression_source_order_id = terms.source_order_id
            else:
                order_stake = execution.stake
                order_win_return = execution.win_return
                progression_step = 1
                progression_source_order_id = None
            progression_version = (
                TWO_STAGE_VERSION if execution.stake_progression_enabled else ""
            )
            pnl = (
                round(order_win_return - order_stake, 4)
                if chosen.result == "WIN"
                else round(-order_stake, 4)
            )
            trade = {
                "order_id": order_id,
                "observation_key": chosen.observation_key,
                "opened_at": chosen.opened_at,
                "settled_at": chosen.settled_at,
                "expires_at": chosen.expires_at,
                "direction": direction,
                "threshold_segment": chosen.threshold_segment,
                "strategy_family": chosen.strategy_family,
                "strategy_tag": chosen.strategy_tag,
                "profile_key": profile["key"],
                "training_samples": profile["sample_size"],
                "training_win_rate": profile["win_rate"],
                "training_ev": profile["ev"],
                "adaptive_state_before": adaptive["status"],
                "adaptive_n12_before": adaptive["n12"],
                "adaptive_n20_before": adaptive["n20"],
                "adaptive_evaluated_through": evaluated_through,
                "order_slot": "FIRST" if direction_open_count == 0 else "SECOND",
                "order_slot_scope": "DIRECTION_V2",
                "result": chosen.result,
                "stake": float(order_stake),
                "win_return": float(order_win_return),
                "pnl": pnl,
                "progression_step": progression_step,
                "progression_source_order_id": progression_source_order_id,
                "progression_version": progression_version,
                "progression_allowed": allow_progression,
            }
            if include_structure_shadow:
                trade["entry_structure_shadow"] = dict(
                    chosen.entry_structure_shadow
                    if isinstance(chosen.entry_structure_shadow, dict)
                    else {}
                )
            trades.append(trade)
            open_orders.append(trade)
            last_order_opened_at[direction] = opened_at
            webhook_count += 1

    _settle_due_orders(open_orders, 2**63 - 1, progression)
    return {
        "trade_rows": trades,
        "guard_rejections": rejections,
        "eligible_events": eligible_events,
        "webhook_count": webhook_count,
    }


def _settle_due_orders(
    open_orders: list[dict[str, Any]],
    current_time: int,
    progression: TwoStageStakeProgression,
) -> None:
    due = sorted(
        (
            item
            for item in open_orders
            if item["settled_at"] is not None and item["settled_at"] <= current_time
        ),
        key=_trade_settlement_key,
    )
    for order in due:
        progression.settle(
            order["order_id"],
            order["opened_at"],
            order["progression_step"],
            order["result"],
            order["settled_at"],
            allow_credit=order["progression_allowed"],
            direction=order["direction"],
        )
        open_orders.remove(order)


def _adaptive_state_before(
    timeline: dict[str, Any],
    profile_key_value: str,
    opened_at: int,
) -> tuple[dict[str, Any], int]:
    entries = timeline["by_profile"].get(profile_key_value, [])
    position = bisect_right([item[0] for item in entries], opened_at)
    state = (
        entries[position - 1][1]
        if position
        else _adaptive_state((), profile_key_value, opened_at + 1)
    )
    settled_times = timeline["settled_times"]
    global_position = bisect_right(settled_times, opened_at)
    evaluated_through = settled_times[global_position - 1] if global_position else 0
    return state, evaluated_through


def _adaptive_state(
    observations: Sequence[ObservationSignal],
    key: str,
    evaluated_at: int,
) -> dict[str, Any]:
    if not _is_adaptive_profile_key(key):
        return {
            "version": ADAPTIVE_PROFILE_STATE_VERSION,
            "status": "WARMUP",
            "profile_key": key,
            "evaluated_at": evaluated_at,
            "n12": _empty_sample_summary(),
            "n20": _empty_sample_summary(),
        }
    return evaluate_adaptive_profile_state(observations, key, max(0, int(evaluated_at)))


def _execution_report(
    execution_result: dict[str, Any],
    oos_start: int,
    oos_end: int,
) -> dict[str, Any]:
    trades = execution_result["trade_rows"]
    directions = {
        direction: summarize_trades(
            [item for item in trades if item["direction"] == direction]
        )
        for direction in ("LONG", "SHORT")
    }
    daily = _group_summaries(trades, "day")
    daily_best = max(daily, key=lambda item: (item["pnl"], item["day"])) if daily else None
    daily_worst = min(daily, key=lambda item: (item["pnl"], item["day"])) if daily else None
    return {
        "total": summarize_trades(trades),
        "by_direction": directions,
        "base_first_orders": sum(
            1
            for item in trades
            if item["progression_step"] == 1 and item["order_slot"] == "FIRST"
        ),
        "daily": daily,
        "daily_best": daily_best,
        "daily_worst": daily_worst,
        "guard_rejections": dict(execution_result["guard_rejections"]),
        "oos_windows": _oos_windows(trades, oos_start, oos_end),
        "webhook_count": execution_result["webhook_count"],
        "eligible_events": execution_result["eligible_events"],
        "trade_rows": list(trades),
    }


def _oos_windows(
    trades: Sequence[dict[str, Any]],
    start_at: int,
    end_at: int,
) -> list[dict[str, Any]]:
    start_at = int(start_at)
    end_at = max(start_at + 3, int(end_at))
    span = end_at - start_at
    boundaries = [start_at + (span * index) // 3 for index in range(4)]
    boundaries[-1] = end_at
    windows = []
    for index in range(3):
        lower = boundaries[index]
        upper = boundaries[index + 1]
        summary = summarize_trades(
            [item for item in trades if lower <= item["opened_at"] < upper]
        )
        windows.append(
            {
                "window": index + 1,
                "start_at": lower,
                "end_at": upper,
                "start_local": _iso(lower),
                "end_local": _iso(upper),
                **summary,
            }
        )
    return windows


def _oos_bounds(
    observations: Sequence[ObservationSignal],
    snapshots: Sequence[dict[str, Any]],
) -> tuple[int, int]:
    if snapshots:
        start_at = snapshots[0]["effective_from"]
        observed_end = max((item.opened_at for item in observations), default=start_at) + 1
        return start_at, max(start_at + 3, min(snapshots[-1]["effective_until"], observed_end))
    if observations:
        start_at = min(item.opened_at for item in observations)
        return start_at, max(start_at + 3, max(item.opened_at for item in observations) + 1)
    return 0, 3


def _compact_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": snapshot["version"],
        "evaluated_at": snapshot["evaluated_at"],
        "evaluated_at_local": _iso(snapshot["evaluated_at"]),
        "lookback_start": snapshot["lookback_start"],
        "lookback_end": snapshot["lookback_end"],
        "effective_from": snapshot["effective_from"],
        "effective_until": snapshot["effective_until"],
        "effective_from_local": _iso(snapshot["effective_from"]),
        "sample_keys": list(snapshot.get("sample_keys") or []),
        "selected_count": snapshot["selected_count"],
        "selected_profiles": [
            {
                key: item[key]
                for key in (
                    "key",
                    "direction",
                    "threshold_segment",
                    "sample_size",
                    "wins",
                    "losses",
                    "win_rate",
                    "pnl",
                    "ev",
                    "selection_state",
                )
            }
            for item in snapshot.get("selected_profiles") or []
        ],
    }


def _schedule_stats(schedule: Sequence[dict[str, Any]]) -> dict[str, Any]:
    counts = [item["selected_count"] for item in schedule]
    activated = 0
    removed = 0
    previous: set[str] = set()
    for item in schedule:
        current = {profile["key"] for profile in item["selected_profiles"]}
        activated += len(current - previous)
        removed += len(previous - current)
        previous = current
    return {
        "evaluations": len(schedule),
        "average_selected": round(sum(counts) / len(counts), 4) if counts else 0.0,
        "min_selected": min(counts) if counts else 0,
        "max_selected": max(counts) if counts else 0,
        "activations": activated,
        "removals": removed,
    }


def _group_summaries(
    trades: Sequence[dict[str, Any]],
    field: str,
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in trades:
        key = (
            datetime.fromtimestamp(item["settled_at"] / 1000, tz=SHANGHAI).strftime(
                "%Y-%m-%d"
            )
            if field == "day"
            else str(item[field])
        )
        groups[key].append(item)
    rows = [{field: key, **summarize_trades(values)} for key, values in groups.items()]
    return sorted(rows, key=lambda item: item[field])


def _has_three_segment_losses(
    trades: Sequence[dict[str, Any]],
    candidate: ObservationSignal,
    opened_at: int,
) -> bool:
    day = datetime.fromtimestamp(opened_at / 1000, tz=SHANGHAI).date()
    matching = [
        item
        for item in trades
        if item["settled_at"] is not None
        and item["settled_at"] <= opened_at
        and datetime.fromtimestamp(item["settled_at"] / 1000, tz=SHANGHAI).date() == day
        and item["threshold_segment"] == candidate.threshold_segment
    ]
    consecutive = 0
    for item in sorted(matching, key=_trade_settlement_key, reverse=True):
        if item["result"] != "LOSS":
            break
        consecutive += 1
    return consecutive >= 3


def _count_leakage_violations(
    observations: Sequence[ObservationSignal],
    snapshots: Sequence[dict[str, Any]],
) -> int:
    violations = 0
    for snapshot in snapshots:
        if any(
            item.settled_at is None or item.settled_at >= snapshot["lookback_end"]
            for item in observations
            if item.observation_key in set(snapshot.get("sample_keys") or [])
        ):
            violations += 1
    return violations


def _data_summary(
    observations: Sequence[ObservationSignal],
    snapshots: Sequence[dict[str, Any]],
    require_full_lookback: bool,
) -> dict[str, Any]:
    if not observations:
        return {
            "settled_observations": 0,
            "require_full_lookback": require_full_lookback,
            "out_of_sample_from": None,
            "out_of_sample_until": None,
        }
    oos_start, oos_end = _oos_bounds(observations, snapshots)
    return {
        "settled_observations": len(observations),
        "first_observation": _iso(min(item.opened_at for item in observations)),
        "last_observation": _iso(max(item.opened_at for item in observations)),
        "require_full_lookback": require_full_lookback,
        "out_of_sample_from": _iso(oos_start) if snapshots else None,
        "out_of_sample_until": _iso(oos_end) if snapshots else None,
    }


def _observation_profile_key(item: ObservationSignal) -> str:
    return profile_key(
        item.timeframe_minutes,
        item.strategy_family,
        item.strategy_tag,
        item.direction,
        item.threshold_segment,
    )


def _is_adaptive_profile_key(value: str) -> bool:
    parts = value.split("|")
    if len(parts) != 5 or parts[0] != "10" or parts[3] not in {"LONG", "SHORT"}:
        return False
    segment = parts[4]
    if len(segment) != 5 or segment[:3] not in {"WD-", "WE-"}:
        return False
    try:
        return 0 <= int(segment[3:]) <= 23
    except ValueError:
        return False


def _empty_sample_summary() -> dict[str, Any]:
    return {
        "sample_size": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "pnl": 0.0,
        "ev": 0.0,
    }


def _settlement_event_key(item: ObservationSignal) -> tuple[int, int, str]:
    return int(item.settled_at or 0), int(item.opened_at), str(item.observation_key)


def _trade_settlement_key(item: dict[str, Any]) -> tuple[int, int, str, int]:
    return (
        int(item.get("settled_at") or 0),
        int(item.get("opened_at") or 0),
        str(item.get("observation_key") or ""),
        int(item.get("order_id") or 0),
    )


def _retention(candidate_count: int, baseline_count: int) -> float:
    if int(baseline_count) <= 0:
        return 1.0 if int(candidate_count) <= 0 else 1.0
    return int(candidate_count) / int(baseline_count)


def _wilson_interval(
    wins: int,
    count: int,
    z: float = 1.959963984540054,
) -> tuple[float, float]:
    if count <= 0:
        return 0.0, 0.0
    ratio = wins / count
    denominator = 1.0 + z * z / count
    center = (ratio + z * z / (2 * count)) / denominator
    margin = (
        z
        * math.sqrt(ratio * (1 - ratio) / count + z * z / (4 * count * count))
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def _iso(timestamp_ms: int | None) -> str | None:
    if timestamp_ms is None:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=SHANGHAI).isoformat(
        timespec="seconds"
    )


def _config_snapshot(config: DailyProfileSelectorConfig) -> dict[str, Any]:
    normalized = config.normalized()
    return {
        **normalized.__dict__,
        "effective_stable_lookback_days": normalized.effective_stable_lookback_days,
        "stable_lookback_source": normalized.stable_lookback_source,
        "effective_joint_failures_to_exit": normalized.joint_failures_to_exit,
    }


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip().upper() for item in str(value).split(",") if item.strip())


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="严格因果回放每日与自适应观察画像")
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--lookback-days", type=int, default=7, help="快速画像回看天数，默认: 7")
    parser.add_argument(
        "--stable-lookback-days",
        type=int,
        help="稳定画像回看天数；未指定时取 14 与快速窗口天数的较大值",
    )
    parser.add_argument("--min-samples", type=int, default=20)
    parser.add_argument("--min-win-rate", type=float, default=0.60)
    parser.add_argument("--min-ev", type=float, default=0.0)
    parser.add_argument(
        "--degraded-runs-to-exit",
        type=int,
        default=2,
        help="兼容连续退化退出次数，默认: 2",
    )
    parser.add_argument(
        "--joint-failures-to-exit",
        type=int,
        help="双窗口同时失败退出次数；未指定时沿用兼容连续退化次数",
    )
    parser.add_argument("--max-open-orders", type=int, required=True, help="生产全局并发上限")
    parser.add_argument(
        "--max-open-long-orders",
        type=int,
        required=True,
        help="生产 LONG 方向并发上限",
    )
    parser.add_argument(
        "--max-open-short-orders",
        type=int,
        required=True,
        help="生产 SHORT 方向并发上限",
    )
    parser.add_argument(
        "--min-order-gap-minutes",
        type=float,
        required=True,
        help="生产同方向冷却/最小开单间隔（分钟）",
    )
    parser.add_argument("--stake", type=float, required=True, help="生产基础 stake")
    parser.add_argument("--win-return", type=float, required=True, help="生产基础赢单返还")
    progression = parser.add_mutually_exclusive_group(required=True)
    progression.add_argument(
        "--stake-progression",
        dest="stake_progression_enabled",
        action="store_true",
        help="显式启用生产两阶段金额叠加",
    )
    progression.add_argument(
        "--no-stake-progression",
        dest="stake_progression_enabled",
        action="store_false",
        help="显式关闭生产两阶段金额叠加",
    )
    parser.add_argument(
        "--stake-progression-max-orders",
        type=int,
        required=True,
        help="生产金额叠加级数；TWO_STAGE_V1 必须为 2",
    )
    parser.add_argument(
        "--stake-progression-max-active",
        type=int,
        required=True,
        help="生产并行第二级订单上限",
    )
    parser.add_argument(
        "--stake-progression-second-stake",
        type=float,
        required=True,
        help="生产第二级 stake；必须与 --win-return 相等",
    )
    parser.add_argument(
        "--stake-progression-base-only-segments",
        required=True,
        help="当前生产兼容参数不生效；回放只接受显式空字符串",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-partial-lookback", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    config = DailyProfileSelectorConfig(
        lookback_days=args.lookback_days,
        stable_lookback_days=args.stable_lookback_days,
        min_samples=args.min_samples,
        min_win_rate=args.min_win_rate,
        min_ev=args.min_ev,
        exit_win_rate=args.min_win_rate,
        exit_ev=args.min_ev,
        degraded_runs_to_exit=args.degraded_runs_to_exit,
        joint_failures_to_exit=args.joint_failures_to_exit,
        max_active_profiles=0,
    ).normalized()
    try:
        execution = ReplayExecutionConfig(
            max_open_orders=args.max_open_orders,
            max_open_long_orders=args.max_open_long_orders,
            max_open_short_orders=args.max_open_short_orders,
            min_order_gap_ms=round(args.min_order_gap_minutes * 60_000),
            stake=args.stake,
            win_return=args.win_return,
            stake_progression_enabled=args.stake_progression_enabled,
            stake_progression_max_orders=args.stake_progression_max_orders,
            stake_progression_max_active=args.stake_progression_max_active,
            stake_progression_second_stake=args.stake_progression_second_stake,
            stake_progression_base_only_segments=_split_csv(
                args.stake_progression_base_only_segments
            ),
        ).normalized()
    except (TypeError, ValueError) as error:
        parser.error(str(error))
    result = replay_daily_profile_selection(
        load_replay_observations(args.db_path, args.symbol),
        config,
        execution=execution,
        require_full_lookback=not args.allow_partial_lookback,
    )
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
