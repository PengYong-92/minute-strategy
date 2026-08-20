import json
import tempfile
import threading
import time
import unittest
import sqlite3
from copy import deepcopy
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import storage as storage_module
from app.adaptive_profile_state import ADAPTIVE_PROFILE_STATE_VERSION
from app.daily_profile_selector import (
    DailyProfileSelectorConfig,
    build_daily_selection,
    profile_key as daily_profile_key,
)
from app.decision_context import DecisionContext
from app.entry_structure_shadow import EntryStructureGate, StructureConfig
from app.models import (
    FearGreedContext,
    Kline,
    ObservationSignal,
    Signal,
    SimulatedOrder,
    decision_linked_storage_payload,
)
from app.order_policy import OrderGate
from app.order_profile import sample_from_entry_snapshot, summarize_order_samples_with_guard
from app.profile_degradation_guard import MINUTE_MS, ProfileDegradationGuardConfig
from app.profile_health_guard import ProfileHealthGuardConfig, ProfileHealthGuardDecision
from app.profile_admission import candidate_policy
from app.result_sequence_guard import ResultSequenceGuardConfig
from app.rolling_edge import RollingEdgeConfig
from app.state import MonitorState
from app.stake_progression import TWO_STAGE_VERSION, StakeProgressionCredit
from app.storage import SQLiteMonitorStore
from app.storage_capacity import MAX_DATABASE_BYTES
from app.strategy import analyze_volume_price
from app.time_period_guard import TimePeriodGuardConfig
from app.wave_state import WaveSnapshot, advance_wave, analyze_wave
from app.wave_batch_guard import WaveBatchGuardConfig
from app.webhook import WebhookSignalProxy


def kline(idx, close, volume, open_price=None, high=None, low=None):
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


def shanghai_timestamp(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)


def actionable_rebound_klines():
    klines = [
        kline(i, 100 + (0.2 if i % 2 else -0.2), 100 + (30 if i % 3 == 0 else 0))
        for i in range(360, 480)
    ]
    for offset in range(10):
        idx = 480 + offset
        open_price = 100.0 - offset * 0.2
        close = open_price - 0.15
        klines.append(kline(idx, close, 160, open_price=open_price, high=open_price + 0.05, low=close - 0.1))
    return klines


def actionable_short_klines():
    klines = []
    for offset in range(260):
        idx = 960 + offset
        close = 105.0 + offset * 0.015
        low = 100.0 if offset == 0 else close - 0.2
        klines.append(
            kline(
                idx,
                close,
                100,
                open_price=close - 0.01,
                high=close + 0.2,
                low=low,
            )
        )
    for offset in range(40):
        idx = 1220 + offset
        close = (
            109.0 + (offset % 3) * 0.02
            if offset < 34
            else 108.8 + (offset - 34) * 0.3
        )
        klines.append(
            kline(
                idx,
                close,
                100,
                open_price=close - 0.02,
                high=112.2,
                low=close - 0.2,
            )
        )
    start = klines[-1].open_time // 60_000 + 1
    price = 111.2
    for offset, step in enumerate((-1, -1, 1, -1, -1, 1, -1, -1, -1, -1)):
        idx = start + offset
        open_price = price
        price += step * 0.15
        close = price
        klines.append(
            kline(
                idx,
                close,
                170,
                open_price=open_price,
                high=max(open_price, close) + 0.04,
                low=min(open_price, close) - 0.08,
            )
        )
    return klines


def settled_observation(
    idx,
    result,
    opened_at,
    *,
    family="drop_reclaim",
    tag="drop_reclaim_observe",
    direction="LONG",
    segment="WD-07",
):
    return ObservationSignal(
        observation_key=f"history-{idx}",
        strategy_family=family,
        strategy_tag=tag,
        direction=direction,
        timeframe_minutes=10,
        level="A",
        reason="历史观察样本",
        entry_price=100.0,
        opened_at=opened_at,
        expires_at=opened_at + 600_000,
        threshold_segment=segment,
        score=90.0 if direction == "LONG" else -90.0,
        threshold=70.0,
        edge=20.0,
        source_decision="SESSION_BLOCKED",
        status="SETTLED",
        result=result,
        exit_price=101.0 if result == "WIN" else 99.0,
        settled_at=opened_at + 600_000,
        pnl=8.0 if result == "WIN" else -10.0,
    )


PROFILE_KEY = "10|drop_reclaim|live_profile|LONG|WD-08"
PROFILE_VERSION = "DPS-20260810-0800"


@contextmanager
def managed_sqlite_states():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "monitor.sqlite3"
        store = SQLiteMonitorStore(db_path)
        states = []
        try:
            yield db_path, store, states
        finally:
            first_error = None
            for state in states:
                try:
                    state.close()
                except Exception as error:  # noqa: BLE001 - 仍需释放其余测试资源。
                    if first_error is None:
                        first_error = error
            try:
                store.close()
            except Exception as error:  # noqa: BLE001 - 保留首个关闭错误。
                if first_error is None:
                    first_error = error
            if first_error is not None:
                raise first_error


def atomic_sqlite_bundle_counts(db_path: Path) -> dict[str, int]:
    with closing(sqlite3.connect(db_path)) as connection:
        counts = {
            table: connection.execute(f"select count(*) from {table}").fetchone()[0]
            for table in (
                "runtime_config_snapshots",
                "decision_contexts",
                "orders",
                "stake_progression_credits",
                "order_entry_snapshots",
                "signal_audit",
                "observation_signals",
            )
        }
        counts["audit_occurrences"] = connection.execute(
            "select coalesce(sum(occurrences), 0) from signal_audit"
        ).fetchone()[0]
        return counts


def selected_profile_signal(
    current_time: int,
    *,
    profile_key: str = PROFILE_KEY,
    daily_profile_version: str = PROFILE_VERSION,
    reason: str = "selected live profile",
    wave_batch_id: str = "",
) -> Signal:
    return Signal(
        direction="LONG",
        timeframe_minutes=10,
        level="A",
        reason=reason,
        price=100.0,
        open_time=current_time,
        score=90.0,
        threshold=70.0,
        threshold_segment="WD-08",
        session_allowed=True,
        observe_direction="LONG",
        strategy_family="drop_reclaim",
        strategy_tag="live_profile",
        profile_key=profile_key,
        daily_profile_selected=True,
        daily_profile_version=daily_profile_version,
        wave_batch_id=wave_batch_id,
    )


def adaptive_profile_snapshot(
    status: str,
    evaluated_at: int,
    *,
    profile_key: str = PROFILE_KEY,
) -> dict:
    sample_size = 0 if status == "WARMUP" else 12
    wins = 0 if status == "WARMUP" else (7 if status == "ACTIVE" else 6)
    if status == "PAUSED":
        sample_size = 20
        wins = 5
    n12_size = min(sample_size, 12)
    return {
        "version": ADAPTIVE_PROFILE_STATE_VERSION,
        "status": status,
        "reason": f"fixture {status}",
        "evaluated_at": evaluated_at,
        "profile_key": profile_key,
        "n12": {
            "sample_size": n12_size,
            "wins": min(wins, n12_size),
            "losses": max(0, n12_size - wins),
            "win_rate": 0.0 if not n12_size else min(wins, n12_size) / n12_size,
            "pnl": 0.0,
            "ev": 0.0,
        },
        "n20": {
            "sample_size": sample_size,
            "wins": wins,
            "losses": max(0, sample_size - wins),
            "win_rate": 0.0 if not sample_size else wins / sample_size,
            "pnl": -10.0 if status == "PAUSED" else 0.0,
            "ev": -0.5 if status == "PAUSED" else 0.0,
        },
        "previous": None,
        "transition": f"NONE->{status}",
        "previous_ignored_reason": "",
    }


def adaptive_admission_state(
    status: str,
    current_time: int,
    *,
    storage=None,
    profile_admission_policy=None,
) -> MonitorState:
    state = MonitorState(
        symbol="BTCUSDT",
        storage=storage,
        max_open_orders=3,
        max_open_long_orders=2,
        max_open_short_orders=2,
        min_order_gap_ms=0,
        enable_daily_profile_selector=True,
        enable_rolling_edge_guard=False,
        result_sequence_guard_config=ResultSequenceGuardConfig(enabled=False),
        profile_degradation_guard_config=ProfileDegradationGuardConfig(
            cooldown_minutes=0
        ),
        profile_health_guard_config=ProfileHealthGuardConfig(enabled=False),
        wave_batch_guard_config=WaveBatchGuardConfig(enabled=False),
        profile_admission_policy=profile_admission_policy,
        now_ms=lambda: current_time,
    )
    qualification = {
        "key": PROFILE_KEY,
        "direction": "LONG",
        "threshold_segment": "WD-08",
        "qualification_state": "QUALIFIED",
        "joint_failure_runs": 0,
        "fast_7d": {"sample_size": 24, "win_rate": 0.625, "pnl": 20.0, "ev": 0.8333},
        "stable_14d": {"sample_size": 40, "win_rate": 0.6, "pnl": 32.0, "ev": 0.8},
    }
    state.active_daily_profile_selection = {
        "version": PROFILE_VERSION,
        "status": "READY",
        "evaluated_at": current_time - 10 * MINUTE_MS,
        "effective_from": current_time - 86_400_000,
        "effective_until": current_time + 86_400_000,
        "selected_profiles": [qualification],
        "candidates": [qualification],
    }
    state.adaptive_profile_states = {
        PROFILE_KEY: adaptive_profile_snapshot(status, current_time - 1)
    }
    return state


def latest_kline(current_time: int, close: float = 100.0) -> Kline:
    return Kline(
        open_time=current_time - MINUTE_MS,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1.0,
        close_time=current_time,
    )


def settle_profile_losses(
    state: "MonitorState",
    *,
    profile_key: str = PROFILE_KEY,
    daily_profile_version: str = PROFILE_VERSION,
    wave_batch_ids: tuple[str, ...] = ("", "", ""),
) -> None:
    for index, (opened_minute, wave_batch_id) in enumerate(
        zip((0, 11, 22), wave_batch_ids),
        start=1,
    ):
        opened_at = opened_minute * MINUTE_MS
        state.simulator.open_order(
            selected_profile_signal(
                opened_at,
                profile_key=profile_key,
                daily_profile_version=daily_profile_version,
                reason=f"profile loss {index}",
                wave_batch_id=wave_batch_id,
            ),
            entry_price=100.0,
            opened_at=opened_at,
        )
        state.simulator.settle_expired_orders(
            opened_at + 10 * MINUTE_MS,
            99.0,
        )


def profile_guard_state(*, max_open_orders: int = 2, **kwargs) -> "MonitorState":
    return MonitorState(
        symbol="BTCUSDT",
        max_open_orders=max_open_orders,
        min_order_gap_ms=0,
        enable_rolling_edge_guard=False,
        result_sequence_guard_config=ResultSequenceGuardConfig(enabled=False),
        **kwargs,
    )


def profile_health_observations(
    evaluation_at: int,
    results: str,
    *,
    direction: str = "LONG",
    segment: str = "WD-08",
) -> list[ObservationSignal]:
    start = evaluation_at - len(results) * 11 * MINUTE_MS
    return [
        settled_observation(
            index,
            "WIN" if result == "W" else "LOSS",
            start + index * 11 * MINUTE_MS,
            family="drop_reclaim",
            tag="live_profile",
            direction=direction,
            segment=segment,
        )
        for index, result in enumerate(results)
    ]


def activate_profile_health_selection(
    state: "MonitorState",
    current_time: int,
    *,
    direction: str = "LONG",
    segment: str = "WD-08",
) -> str:
    key = f"10|drop_reclaim|live_profile|{direction}|{segment}"
    state.active_daily_profile_selection = {
        "version": PROFILE_VERSION,
        "status": "READY",
        "effective_from": current_time - 86_400_000,
        "effective_until": current_time + 86_400_000,
        "selected_profiles": [
            {
                "key": key,
                "direction": direction,
                "threshold_segment": segment,
            }
        ],
    }
    return key


class StaticFearGreedProvider:
    def __init__(self, context):
        self.context = context
        self.calls = 0

    def get_context(self):
        self.calls += 1
        return self.context


class RecordingWebhook:
    def __init__(self):
        self.calls = []
        self.last_error = None

    def send_signal(self, symbol, signal, message=None, amount=None):
        self.calls.append(
            (symbol, signal.direction, signal.timeframe_minutes, signal.reason if message is None else message, amount)
        )

    def status(self):
        return {"enabled": True, "last_error": self.last_error}


class RecordingStorage:
    def __init__(self):
        self.orders = []
        self.signals = []
        self.observations = []
        self.entry_snapshots = []
        self.settlements = []
        self.atomic_calls = []
        self.credit_saves = []
        self.progression_prepares = []
        self.persisted_orders = {}
        self.persisted_credits = {}
        self.progression_runtime = {}
        self.fail_once_methods = set()
        self.write_gate = None
        self.order_profile = None
        self.daily_profile_selections = []
        self.wave_runtime = {}
        self.decision_bundles = []
        self.runtime_configs = {}
        self.decision_contexts = {}

    def load_orders(self, symbol):
        return [replace(order) for order in self.persisted_orders.get(symbol.upper(), {}).values()]

    def load_observations(self, symbol):
        return []

    def load_wave_runtime(self, symbol):
        runtime = self.wave_runtime.get(symbol.upper())
        if runtime is None:
            return None
        return {
            "evaluated_at": runtime["evaluated_at"],
            "snapshot": runtime["snapshot"],
        }

    def save_wave_runtime(self, symbol, snapshot, evaluated_at):
        self._maybe_fail("save_wave_runtime")
        self.wave_runtime[symbol.upper()] = {
            "evaluated_at": evaluated_at,
            "snapshot": snapshot,
        }

    def save_order(self, order, symbol):
        self._wait_for_write_gate()
        self.orders.append((symbol, order.to_dict()))
        self._persist_order(order, symbol)

    def prepare_stake_progression(self, symbol, version, enabled, activated_at):
        symbol = symbol.upper()
        self.progression_prepares.append((symbol, version, enabled, activated_at))
        runtime = self.progression_runtime.get(symbol)
        should_cancel = False
        if runtime is None:
            actual_activation = activated_at
            should_cancel = not enabled
        else:
            version_changed = runtime[0] != version
            reenabled = not runtime[2] and enabled
            disabling = runtime[2] and not enabled
            should_cancel = version_changed or reenabled or disabling
            actual_activation = activated_at if version_changed or reenabled else runtime[1]
        if should_cancel:
            for key, credit in list(self.persisted_credits.items()):
                if key[0] == symbol and credit.status == "PENDING":
                    self.persisted_credits[key] = replace(credit, status="CANCELLED")
        self.progression_runtime[symbol] = (version, actual_activation, enabled)
        return actual_activation

    def load_stake_progression_credits(self, symbol, version=TWO_STAGE_VERSION):
        credits = [
            replace(credit)
            for (item_symbol, item_version, _source_id), credit in self.persisted_credits.items()
            if item_symbol == symbol.upper() and item_version == version
        ]
        return sorted(credits, key=lambda item: (item.created_at, item.credit_id))

    def save_stake_progression_credit(self, symbol, credit):
        self._maybe_fail("save_stake_progression_credit")
        credit_snapshot = replace(credit)
        self.credit_saves.append((symbol, credit_snapshot.to_dict()))
        self._persist_credit(credit_snapshot, symbol)

    def cancel_stake_progression_credits(self, symbol, credits):
        self._maybe_fail("cancel_stake_progression_credits")
        snapshots = [replace(credit) for credit in credits]
        self.atomic_calls.append(
            ("cancel", symbol, [credit.to_dict() for credit in snapshots])
        )
        for credit in snapshots:
            self.credit_saves.append((symbol, credit.to_dict()))
            self._persist_credit(credit, symbol)

    def save_settled_order_with_credit(self, order, symbol, credit):
        self._maybe_fail("save_settled_order_with_credit")
        order_snapshot = replace(order)
        credit_snapshot = replace(credit) if credit is not None else None
        self.atomic_calls.append(
            (
                "settled",
                symbol,
                order_snapshot.to_dict(),
                credit_snapshot.to_dict() if credit_snapshot is not None else None,
            )
        )
        self._persist_order(order_snapshot, symbol)
        if credit_snapshot is not None:
            self._persist_credit(credit_snapshot, symbol)
        self.settlements.append((symbol, order_snapshot.to_dict()))

    def save_open_order_with_credit(self, order, symbol, credit):
        self._maybe_fail("save_open_order_with_credit")
        order_snapshot = replace(order)
        credit_snapshot = replace(credit) if credit is not None else None
        self.atomic_calls.append(
            (
                "open",
                symbol,
                order_snapshot.to_dict(),
                credit_snapshot.to_dict() if credit_snapshot is not None else None,
            )
        )
        self._persist_order(order_snapshot, symbol)
        if credit_snapshot is not None:
            self._persist_credit(credit_snapshot, symbol)

    def save_open_order_decision(
        self,
        *,
        config,
        context,
        order,
        credit,
        entry_snapshot,
        audit,
        observation=None,
    ):
        self._maybe_fail("save_open_order_decision")
        order_snapshot = replace(order)
        credit_snapshot = replace(credit) if credit is not None else None
        observation_snapshot = replace(observation) if observation is not None else None
        self.runtime_configs[config.hash] = config
        self.decision_contexts[(context.symbol, context.decision_id)] = context
        self.decision_bundles.append(
            (
                "open",
                context,
                audit,
                order_snapshot,
                observation_snapshot,
                entry_snapshot,
            )
        )
        self.atomic_calls.append(
            (
                "open",
                context.symbol,
                order_snapshot.to_dict(),
                credit_snapshot.to_dict() if credit_snapshot is not None else None,
            )
        )
        self._persist_order(order_snapshot, context.symbol)
        if credit_snapshot is not None:
            self._persist_credit(credit_snapshot, context.symbol)
        self.entry_snapshots.append(
            (context.symbol, order_snapshot.to_dict(), entry_snapshot)
        )
        if observation_snapshot is not None:
            self.observations.append(
                (context.symbol, observation_snapshot.to_dict())
            )
        self.signals.append(
            (
                context.symbol,
                audit.signal.to_dict(),
                audit.decision,
                audit.created_at_ms,
                audit.audit_context,
                True,
                True,
                audit.event_kind,
            )
        )

    def save_decision_bundle(self, *, config, context, audit, observation=None):
        self._maybe_fail("save_decision_bundle")
        observation_snapshot = replace(observation) if observation is not None else None
        self.runtime_configs[config.hash] = config
        self.decision_contexts[(context.symbol, context.decision_id)] = context
        self.decision_bundles.append(
            ("decision", context, audit, None, observation_snapshot, None)
        )
        if observation_snapshot is not None:
            self.observations.append(
                (context.symbol, observation_snapshot.to_dict())
            )
        self.signals.append(
            (
                context.symbol,
                audit.signal.to_dict(),
                audit.decision,
                audit.created_at_ms,
                audit.audit_context,
                bool(audit.signal.actionable),
                True,
                audit.event_kind,
            )
        )

    def _persist_order(self, order, symbol):
        self.persisted_orders.setdefault(symbol.upper(), {})[order.id] = replace(order)

    def _persist_credit(self, credit, symbol):
        key = (symbol.upper(), credit.version, credit.source_order_id)
        self.persisted_credits[key] = replace(credit)

    def _wait_for_write_gate(self):
        if self.write_gate is not None:
            self.write_gate.wait(timeout=5)

    def fail_once(self, method_name):
        self.fail_once_methods.add(method_name)

    def _maybe_fail(self, method_name):
        if method_name in self.fail_once_methods:
            self.fail_once_methods.remove(method_name)
            raise OSError(f"{method_name} failed")

    def save_signal(
        self,
        symbol,
        signal,
        decision,
        created_at_ms,
        audit_context=None,
        *,
        has_formal_candidate=False,
        force_independent=False,
        event_kind=None,
    ):
        self._wait_for_write_gate()
        self.signals.append(
            (
                symbol,
                signal.to_dict(),
                decision,
                created_at_ms,
                audit_context,
                has_formal_candidate,
                force_independent,
                event_kind,
            )
        )

    def save_observation(self, observation, symbol):
        self._wait_for_write_gate()
        self.observations.append((symbol, observation.to_dict()))

    def save_order_entry_snapshot(self, order, symbol, entry_snapshot):
        self._wait_for_write_gate()
        self.entry_snapshots.append((symbol, order.to_dict(), entry_snapshot))

    def update_order_entry_snapshot_settlement(self, order, symbol):
        self._wait_for_write_gate()
        self.settlements.append((symbol, order.to_dict()))

    def page_observations(self, symbol, **kwargs):
        return {"observations": [item for _symbol, item in self.observations], "total": len(self.observations)}

    def order_profile_summary(self, symbol, **kwargs):
        return self.order_profile

    def save_daily_profile_selection(self, symbol, snapshot):
        self.daily_profile_selections.append((symbol, snapshot))

    def load_latest_daily_profile_selection(self, symbol):
        matching = [snapshot for item_symbol, snapshot in self.daily_profile_selections if item_symbol == symbol]
        return matching[-1] if matching else None

    def load_daily_profile_selection_as_of(
        self,
        symbol,
        evaluation_key,
        evaluated_at_ms=None,
    ):
        matching = [
            snapshot
            for item_symbol, snapshot in self.daily_profile_selections
            if item_symbol == symbol
            and int(
                snapshot.get(
                    "evaluation_key",
                    snapshot.get("lookback_end", snapshot.get("evaluated_at", -1)),
                )
            )
            <= evaluation_key
            and (
                evaluated_at_ms is None
                or int(snapshot.get("evaluated_at", -1)) <= evaluated_at_ms
            )
        ]
        return max(
            matching,
            key=lambda snapshot: (
                int(
                    snapshot.get(
                        "evaluation_key",
                        snapshot.get("lookback_end", snapshot.get("evaluated_at", -1)),
                    )
                ),
                int(snapshot.get("evaluated_at", -1)),
            ),
            default=None,
        )

    def load_daily_profile_selection(self, symbol, effective_at_ms):
        matching = [
            snapshot
            for item_symbol, snapshot in self.daily_profile_selections
            if item_symbol == symbol
            and snapshot["effective_from"] <= effective_at_ms < snapshot["effective_until"]
        ]
        return matching[-1] if matching else None


class FailingDailySelectionStorage(RecordingStorage):
    def save_daily_profile_selection(self, symbol, snapshot):
        raise OSError("database unavailable")


class LegacyFailingDailySelectionStorage(FailingDailySelectionStorage):
    def __getattribute__(self, name):
        if name == "load_daily_profile_selection_as_of":
            raise AttributeError(name)
        return super().__getattribute__(name)

    def load_daily_profile_selection(self, symbol, effective_at_ms):
        matching = [
            snapshot
            for item_symbol, snapshot in self.daily_profile_selections
            if item_symbol == symbol
        ]
        return matching[-1] if matching else None


class MonitorStateTest(unittest.TestCase):
    def test_decision_trace_names_first_decisive_branch_without_changing_decision(self):
        now = 119_999

        def run_case(label):
            storage = RecordingStorage()
            state = MonitorState(
                symbol="BTCUSDT",
                storage=storage,
                min_order_gap_ms=0,
                enable_rolling_edge_guard=False,
                enable_observation_profile_promotion=False,
                result_sequence_guard_config=ResultSequenceGuardConfig(enabled=False),
            )
            self.addCleanup(state.close)
            candidate = selected_profile_signal(now)
            case_now = now
            kwargs = {}
            context = None
            if label == "below threshold":
                candidate = replace(
                    candidate,
                    direction="WAIT",
                    observe_direction="LONG",
                    daily_profile_selected=False,
                    score=69.0,
                    session_allowed=True,
                )
            elif label == "session blocked":
                candidate = replace(
                    candidate,
                    direction="WAIT",
                    observe_direction="LONG",
                    daily_profile_selected=False,
                    session_allowed=False,
                )
            elif label == "daily profile":
                candidate = replace(candidate, daily_profile_selected=False)
                kwargs["daily_profile_required"] = True
            elif label == "wave direction":
                candidate = replace(
                    candidate,
                    direction="WAIT",
                    observe_direction="LONG",
                    wave_guard_mode="DIRECTION_BLOCKED",
                    wave_state="DOWN_LEG",
                )
            elif label == "profile summary":
                state.enable_profile_guard = True
                context = patch.object(
                    state,
                    "_profile_guard_shadow",
                    return_value={
                        "status": "WOULD_BLOCK",
                        "hit_keys": ["segment"],
                        "min_history": 15,
                        "min_group_size": 2,
                        "cache_status": "FRESH",
                        "source_revision": 4,
                        "current_revision": 4,
                        "stale": False,
                    },
                )
            elif label == "profile health":
                context = patch.object(
                    state,
                    "_refresh_profile_health_guard",
                    return_value=ProfileHealthGuardDecision(
                        enabled=True,
                        status="DEGRADED",
                        direction="LONG",
                        evaluated_at=now,
                        next_evaluation_at=now + 1,
                        lookback_start=0,
                        lookback_end=now,
                        blocked=True,
                        reason="degraded profile health",
                    ),
                )
            elif label == "capacity":
                state.order_policy = replace(state.order_policy, max_open_orders=1)
                state.simulator.open_order(
                    replace(candidate, direction="SHORT", observe_direction="SHORT"),
                    entry_price=100.0,
                    opened_at=0,
                )
            elif label == "cooldown":
                state.order_policy = replace(state.order_policy, min_order_gap_ms=120_000)
                state._last_order_opened_at = {
                    "LONG": now - 60_000,
                    "SHORT": None,
                }
            elif label == "short observe only":
                candidate = replace(
                    candidate,
                    direction="SHORT",
                    observe_direction="SHORT",
                    score=-90.0,
                    threshold_segment="WD-12",
                    profile_key="",
                    daily_profile_selected=False,
                    daily_profile_version="",
                )
            elif label == "wave batch":
                state.wave_batch_guard_config = WaveBatchGuardConfig()
                candidate = replace(candidate, wave_batch_id="wave-a")
                state.simulator.orders.append(
                    SimulatedOrder(
                        id=1,
                        direction="LONG",
                        timeframe_minutes=10,
                        level="A",
                        reason="wave loss",
                        entry_price=100.0,
                        opened_at=0,
                        expires_at=60_000,
                        status="SETTLED",
                        result="LOSS",
                        settled_at=60_000,
                        pnl=-10.0,
                        wave_batch_id="wave-a",
                    )
                )
            elif label == "profile degradation":
                case_now = 33 * MINUTE_MS
                candidate = selected_profile_signal(case_now)
                settle_profile_losses(state)
            elif label == "result sequence":
                case_now = 120_000
                state.result_sequence_guard_config = ResultSequenceGuardConfig(
                    enabled=True,
                    loss_streak=1,
                    cooldown_minutes=20,
                )
                state.simulator.orders.append(
                    SimulatedOrder(
                        id=1,
                        direction="LONG",
                        timeframe_minutes=1,
                        level="A",
                        reason="sequence loss",
                        entry_price=100.0,
                        opened_at=0,
                        expires_at=60_000,
                        status="SETTLED",
                        result="LOSS",
                        settled_at=60_000,
                        pnl=-10.0,
                    )
                )
                candidate = selected_profile_signal(case_now)
            elif label == "rolling edge":
                state.enable_rolling_edge_guard = True
                context = patch.object(
                    state,
                    "_rolling_edge_status",
                    return_value={
                        "status": "DEGRADED",
                        "key": "10|WD-08|selected live profile",
                        "sample_size": 5,
                        "wins": 2,
                        "losses": 3,
                        "win_rate": 0.4,
                        "pnl": -14.0,
                        "ev": -2.8,
                    },
                )
            elif label == "time period":
                case_now = shanghai_timestamp("2026-08-18T12:30:00")
                state.time_period_guard_config = TimePeriodGuardConfig(enabled=True)
                candidate = selected_profile_signal(case_now)
            elif label == "profile health second order":
                state.order_policy = replace(
                    state.order_policy,
                    max_open_long_orders=2,
                )
                state.simulator.open_order(
                    candidate,
                    entry_price=100.0,
                    opened_at=0,
                )
                context = patch.object(
                    state,
                    "_refresh_profile_health_guard",
                    return_value=ProfileHealthGuardDecision(
                        enabled=True,
                        status="WATCH",
                        direction="LONG",
                        evaluated_at=now,
                        next_evaluation_at=now + 1,
                        lookback_start=0,
                        lookback_end=now,
                        allow_second_order=False,
                        allow_progression=False,
                        reason="watch permits first order only",
                    ),
                )

            if context is None:
                decision = state._maybe_open_order(
                    candidate,
                    latest_kline(case_now),
                    **kwargs,
                )
            else:
                with context:
                    decision = state._maybe_open_order(
                        candidate,
                        latest_kline(case_now),
                        **kwargs,
                    )
            return decision, state.selected_signal

        cases = (
            ("below threshold", "BELOW_THRESHOLD", "SCORE", "BELOW_THRESHOLD"),
            ("session blocked", "SESSION_BLOCKED", "SESSION", "SESSION_BLOCKED"),
            (
                "daily profile",
                "DAILY_PROFILE_NOT_SELECTED",
                "DAILY_PROFILE",
                "PROFILE_NOT_SELECTED",
            ),
            (
                "wave direction",
                "WAVE_DIRECTION_BLOCKED",
                "WAVE_GUARD",
                "WAVE_BLOCKED",
            ),
            (
                "profile summary",
                "PROFILE_GUARD_BLOCKED",
                "PROFILE_HEALTH",
                "PROFILE_GUARD_BLOCKED",
            ),
            (
                "profile health",
                "PROFILE_HEALTH_BLOCKED",
                "PROFILE_HEALTH_SHORT_WINDOW",
                "PROFILE_HEALTH_BLOCKED",
            ),
            ("capacity", "HOLD_OPEN_ORDER", "CAPACITY", "MAX_OPEN_ORDERS"),
            ("cooldown", "COOLDOWN", "COOLDOWN", "DIRECTION_COOLDOWN"),
            (
                "short observe only",
                "SHORT_OBSERVE_ONLY",
                "SHORT_MODE",
                "SHORT_OBSERVE_ONLY",
            ),
            (
                "wave batch",
                "WAVE_BATCH_LOSS_LOCKED",
                "WAVE_BATCH",
                "WAVE_BATCH_LOSS_LOCKED",
            ),
            (
                "profile degradation",
                "PROFILE_DEGRADATION_BLOCKED",
                "PROFILE_DEGRADATION",
                "PROFILE_DEGRADATION_BLOCKED",
            ),
            (
                "result sequence",
                "RESULT_SEQUENCE_GUARD_BLOCKED",
                "RESULT_SEQUENCE",
                "RESULT_SEQUENCE_GUARD_BLOCKED",
            ),
            (
                "rolling edge",
                "ROLLING_EDGE_BLOCKED",
                "ROLLING_EDGE",
                "ROLLING_EDGE_BLOCKED",
            ),
            (
                "time period",
                "TIME_PERIOD_SHADOW_ONLY",
                "TIME_PERIOD",
                "TIME_PERIOD_SHADOW_ONLY",
            ),
            (
                "profile health second order",
                "PROFILE_HEALTH_SECOND_ORDER_BLOCKED",
                "PROFILE_HEALTH_SECOND_ORDER",
                "PROFILE_HEALTH_SECOND_ORDER_BLOCKED",
            ),
        )

        for label, expected_decision, expected_stage, expected_reason in cases:
            with self.subTest(label=label):
                decision, selected = run_case(label)

                self.assertEqual(decision, expected_decision)
                self.assertEqual(selected.first_decisive_block, expected_stage)
                self.assertEqual(selected.decision_trace[-1]["result"], "BLOCK")
                self.assertEqual(selected.decision_trace[-1]["reason_code"], expected_reason)
                self.assertTrue(
                    all(
                        record["result"] == "PASS"
                        for record in selected.decision_trace[:-1]
                    )
                )

    def test_open_decision_freezes_complete_canonical_input_and_ordered_pass_trace(self):
        storage = RecordingStorage()
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            min_order_gap_ms=0,
            enable_rolling_edge_guard=False,
            enable_observation_profile_promotion=False,
            result_sequence_guard_config=ResultSequenceGuardConfig(enabled=False),
        )
        self.addCleanup(state.close)
        state.klines = [kline(index, 100.0 + index / 100.0, 100 + index) for index in range(20)]
        latest = state.klines[-1]
        now = latest.close_time
        state.fear_greed = FearGreedContext(
            value=23,
            classification="Fear",
            average_30d=31.5,
            trend="rising",
            updated_at_ms=now - 1_000,
        )
        signal = replace(
            selected_profile_signal(now),
            decision_inputs={
                "score": {
                    "raw_direction": "LONG",
                    "raw_score": 90.0,
                    "signed_score": 90.0,
                    "edge": 20.0,
                    "volume_points": 8.0,
                },
                "thresholds": {
                    "base_threshold": 68.0,
                    "calculated_threshold": 70.0,
                    "fear_greed_adjustment": 2.0,
                },
                "volume_price": {"current_volume": 119.0, "volume_baseline": 100.0},
                "indicators": {"macd_line": 1.0, "macd_signal_line": 0.8, "atr": 2.0},
            },
            entry_structure_shadow={
                "version": "ENTRY_STRUCTURE_SHADOW_V1",
                "status": "READY",
                "audit_only": True,
                "state": "BREAKOUT",
            },
        )

        decision = state._maybe_open_order(signal, latest)

        self.assertEqual(decision, "OPENED")
        selected = state.selected_signal
        canonical = selected.decision_inputs
        self.assertEqual(
            set(canonical),
            {
                "identity",
                "market",
                "score",
                "volume_price",
                "indicators",
                "context",
                "admission",
                "entry_structure",
                "signal",
                "audit_snapshot",
            },
        )
        self.assertEqual(canonical["identity"]["symbol"], "BTCUSDT")
        self.assertEqual(canonical["identity"]["decision_id"], selected.decision_id)
        self.assertEqual(canonical["market"]["closed_kline"]["close_time"], now)
        self.assertEqual(len(canonical["market"]["analysis_10m_window"]), 10)
        self.assertEqual(canonical["score"]["raw_score"], 90.0)
        self.assertEqual(canonical["volume_price"]["current_volume"], 119.0)
        self.assertEqual(canonical["indicators"]["macd_line"], 1.0)
        self.assertEqual(canonical["context"]["fear_greed"]["value"], 23)
        self.assertIn("daily_7d_14d", canonical["context"])
        self.assertIn("n12_n20", canonical["context"])
        self.assertIn("profile_summary_cache", canonical["context"])
        self.assertIn("storage_capacity", canonical["admission"])
        self.assertEqual(
            canonical["entry_structure"]["version"],
            "ENTRY_STRUCTURE_SHADOW_V1",
        )
        self.assertTrue(canonical["entry_structure"]["audit_only"])
        self.assertIs(
            selected.quality_score_inputs,
            selected.decision_inputs["score"]["quality_score_inputs"],
        )
        self.assertEqual(selected.first_decisive_block, "")
        self.assertTrue(
            all(record["result"] == "PASS" for record in selected.decision_trace)
        )
        self.assertEqual(selected.decision_trace[-1]["stage"], "ADMISSION")
        self.assertEqual(
            selected.decision_trace[-1]["decisive_values"]["selected_order_terms"]["stake"],
            10.0,
        )
        order = state.simulator.orders[-1]
        self.assertEqual(order.decision_id, selected.decision_id)
        self.assertEqual(order.decision_inputs, canonical)
        self.assertEqual(order.decision_trace, selected.decision_trace)

    def test_real_long_and_short_strategy_inputs_round_trip_into_canonical_groups(self):
        fixtures = (
            ("LONG", actionable_rebound_klines(), None),
            (
                "SHORT",
                actionable_short_klines(),
                FearGreedContext(
                    value=28,
                    classification="Fear",
                    average_30d=21.0,
                    trend="rising",
                ),
            ),
        )
        for expected_direction, klines, fear_greed in fixtures:
            with self.subTest(direction=expected_direction):
                strategy_signal = analyze_volume_price(
                    klines,
                    timeframe_minutes=10,
                    fear_greed=fear_greed,
                )
                self.assertEqual(strategy_signal.direction, expected_direction)
                expected = strategy_signal.decision_inputs
                state = MonitorState(
                    symbol="BTCUSDT",
                    min_order_gap_ms=0,
                    enable_rolling_edge_guard=False,
                    enable_observation_profile_promotion=False,
                )
                self.addCleanup(state.close)
                state.klines = list(klines)
                state.fear_greed = fear_greed
                candidate = replace(
                    strategy_signal,
                    daily_profile_selected=True,
                    daily_profile_version="REAL-INPUT-V1",
                )

                decision = state._maybe_open_order(candidate, klines[-1])

                self.assertEqual(decision, "OPENED")
                canonical = state.selected_signal.decision_inputs
                for key, value in expected["thresholds"].items():
                    self.assertEqual(canonical["score"][key], value, key)
                score_control = {
                    "raw_direction",
                    "raw_score",
                    "signed_score",
                    "score_abs",
                    "edge",
                    "final_direction",
                    "actionable",
                }
                for key, value in expected["score"].items():
                    target = canonical["score"] if key in score_control else canonical["score"]["components"]
                    self.assertEqual(target[key], value, key)
                for key, value in expected["volume_price"].items():
                    self.assertEqual(canonical["volume_price"][key], value, key)
                indicator_aliases = {"atr": "atr14"}
                for key, value in expected["indicators"].items():
                    canonical_key = indicator_aliases.get(key, key)
                    target = (
                        canonical["context"]
                        if canonical_key in {"mtf_10m_bias", "mtf_30m_bias"}
                        else canonical["indicators"]
                    )
                    self.assertEqual(target[canonical_key], value, key)

    def test_entry_structure_v1_is_complete_and_conflicting_shadow_never_changes_gate(self):
        latest = latest_kline(119_999)
        base = selected_profile_signal(119_999, reason="entry structure control")
        conflict = replace(
            base,
            entry_structure_shadow={
                "entry_structure_evaluated_at": latest.close_time,
                "entry_structure_state": "BREAKDOWN",
                "entry_structure_bias": "SHORT",
                "entry_structure_reason_code": "TASK12_BREAKDOWN_CONFIRMED",
                "candidate_origin": "NATIVE_ACTIONABLE",
                "candidate_direction": "LONG",
                "breakout_direction": "SHORT",
                "retest_status": "FAILED",
            },
        )
        control_state = MonitorState(symbol="BTCUSDT", min_order_gap_ms=0)
        conflict_state = MonitorState(symbol="BTCUSDT", min_order_gap_ms=0)
        self.addCleanup(conflict_state.close)
        self.addCleanup(control_state.close)

        control_decision = control_state._maybe_open_order(base, latest)
        conflict_decision = conflict_state._maybe_open_order(conflict, latest)

        self.assertEqual((control_decision, conflict_decision), ("OPENED", "OPENED"))
        self.assertEqual(
            control_state.simulator.orders[0].stake,
            conflict_state.simulator.orders[0].stake,
        )
        structure = conflict_state.selected_signal.decision_inputs["entry_structure"]
        required = {
            "entry_structure_version",
            "entry_structure_mode",
            "entry_structure_evaluated_at",
            "entry_structure_state",
            "entry_structure_bias",
            "entry_structure_reason_code",
            "evaluated_at",
            "state",
            "bias",
            "reason_code",
            "candidate_origin",
            "active_level_source",
            "active_level_lower",
            "active_level_upper",
            "active_level_touch_count",
            "active_level_confirmed_at",
            "nearest_support_lower",
            "nearest_support_upper",
            "nearest_resistance_lower",
            "nearest_resistance_upper",
            "support_distance_price",
            "support_distance_bps",
            "support_distance_atr",
            "resistance_distance_price",
            "resistance_distance_bps",
            "resistance_distance_atr",
            "breakout_direction",
            "breakout_closed_bars",
            "breakout_buffer_atr",
            "retest_status",
            "round_level_price",
            "round_level_step",
            "audit_only",
        }
        self.assertTrue(required.issubset(structure))
        self.assertEqual(
            structure["entry_structure_evaluated_at"],
            structure["evaluated_at"],
        )
        self.assertEqual(structure["entry_structure_state"], structure["state"])
        self.assertEqual(structure["entry_structure_bias"], structure["bias"])
        self.assertEqual(
            structure["entry_structure_reason_code"],
            structure["reason_code"],
        )
        default_structure = control_state.selected_signal.decision_inputs["entry_structure"]
        for canonical, alias in (
            ("entry_structure_evaluated_at", "evaluated_at"),
            ("entry_structure_state", "state"),
            ("entry_structure_bias", "bias"),
            ("entry_structure_reason_code", "reason_code"),
        ):
            self.assertIn(canonical, default_structure)
            self.assertEqual(default_structure[canonical], default_structure[alias])
        self.assertEqual(structure["state"], "BREAKDOWN")
        self.assertEqual(structure["bias"], "SHORT")
        self.assertTrue(structure["audit_only"])

    def test_admission_observation_allowed_matches_final_outcome_and_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            states = []
            try:
                scenarios = (
                    ("BTCUSDT", True, False, "OPENED", 1),
                    ("ETHUSDT", False, False, "OPENED", 0),
                    (
                        "SOLUSDT",
                        True,
                        True,
                        "DAILY_PROFILE_NOT_SELECTED",
                        1,
                    ),
                )
                for index, (
                    symbol,
                    should_observe,
                    block_profile,
                    expected_decision,
                    expected_rows,
                ) in enumerate(scenarios):
                    current_time = 119_999 + index * 60_000
                    state = MonitorState(
                        symbol=symbol,
                        storage=store,
                        min_order_gap_ms=0,
                    )
                    states.append(state)
                    candidate = selected_profile_signal(
                        current_time,
                        profile_key=f"profile-{symbol}",
                        reason=f"observation semantics {symbol}",
                    )
                    if not should_observe:
                        candidate = replace(candidate, observe_direction="")
                    if block_profile:
                        candidate = replace(candidate, daily_profile_selected=False)

                    decision = state._maybe_open_order(
                        candidate,
                        latest_kline(current_time),
                        daily_profile_required=block_profile,
                    )
                    context = store.load_decision_context(
                        symbol,
                        state.selected_signal.decision_id,
                    )
                    with closing(sqlite3.connect(store.path)) as connection:
                        row_count = connection.execute(
                            "select count(*) from observation_signals where symbol = ?",
                            (symbol,),
                        ).fetchone()[0]

                    self.assertEqual(decision, expected_decision)
                    self.assertEqual(row_count, expected_rows)
                    self.assertEqual(
                        state.selected_signal.decision_inputs["admission"][
                            "observation_allowed"
                        ],
                        bool(expected_rows),
                    )
                    self.assertEqual(context["observation_allowed"], bool(expected_rows))
                    self.assertEqual(
                        context["inputs"]["admission"]["observation_allowed"],
                        context["observation_allowed"],
                    )

                overlap_time = 299_999
                overlap_candidate = replace(
                    selected_profile_signal(
                        overlap_time,
                        profile_key="profile-SOLUSDT",
                        reason="blocked overlapping observation",
                    ),
                    daily_profile_selected=False,
                )
                overlap_state = states[2]
                overlap_decision = overlap_state._maybe_open_order(
                    overlap_candidate,
                    latest_kline(overlap_time),
                    daily_profile_required=True,
                )
                overlap_context = store.load_decision_context(
                    "SOLUSDT",
                    overlap_state.selected_signal.decision_id,
                )
                with closing(sqlite3.connect(store.path)) as connection:
                    overlap_rows = connection.execute(
                        "select count(*) from observation_signals "
                        "where symbol = 'SOLUSDT' and decision_id = ?",
                        (overlap_context["decision_id"],),
                    ).fetchone()[0]
                self.assertEqual(
                    overlap_decision,
                    "DAILY_PROFILE_NOT_SELECTED",
                )
                self.assertEqual(overlap_rows, 0)
                self.assertFalse(overlap_context["observation_allowed"])
                self.assertFalse(
                    overlap_context["inputs"]["admission"]["observation_allowed"]
                )

                failed = MonitorState(
                    symbol="ADAUSDT",
                    storage=store,
                    min_order_gap_ms=0,
                )
                states.append(failed)
                with patch.object(
                    store,
                    "save_open_order_decision",
                    side_effect=RuntimeError("injected persistence failure"),
                ):
                    decision = failed._maybe_open_order(
                        selected_profile_signal(
                            359_999,
                            profile_key="profile-ADAUSDT",
                        ),
                        latest_kline(359_999),
                    )
                with closing(sqlite3.connect(store.path)) as connection:
                    observation_rows = connection.execute(
                        "select count(*) from observation_signals where symbol = 'ADAUSDT'"
                    ).fetchone()[0]
                    context_rows = connection.execute(
                        "select count(*) from decision_contexts where symbol = 'ADAUSDT'"
                    ).fetchone()[0]
                self.assertEqual(decision, "STORAGE_ERROR")
                self.assertEqual((observation_rows, context_rows), (0, 0))
                self.assertFalse(
                    failed.selected_signal.decision_inputs["admission"][
                        "observation_allowed"
                    ]
                )
                self.assertEqual(failed.observations, [])
            finally:
                for state in states:
                    state.close()
                store.close()

    def test_selected_order_terms_persist_replay_and_reject_frozen_conflict(self):
        with managed_sqlite_states() as (_db_path, store, states):
            state = MonitorState(
                symbol="BTCUSDT",
                storage=store,
                min_order_gap_ms=0,
            )
            states.append(state)
            signal = selected_profile_signal(119_999, reason="terms outcome")
            latest = latest_kline(119_999)

            first = state._maybe_open_order(signal, latest)
            decision_id = state.selected_signal.decision_id
            persisted = store.load_decision_context("BTCUSDT", decision_id)
            replay = state._maybe_open_order(replace(signal, reason="recomputed"), latest)

            self.assertEqual((first, replay), ("OPENED", "OPENED"))
            expected_terms = {
                "stake": 10.0,
                "win_return": 18.0,
                "progression_step": 1,
                "progression_source_order_id": None,
                "progression_version": TWO_STAGE_VERSION,
                "progression_limits": {"max_orders": 2, "max_active": 1},
                "allow_progression": True,
                "expires_at": 719_999,
                "timeframe_minutes": 10,
                "order_slot": "FIRST",
                "order_slot_scope": "DIRECTION_V2",
                "direction": "LONG",
                "entry_price": 100.0,
            }
            self.assertEqual(persisted["selected_order_terms"], expected_terms)
            self.assertEqual(
                state.selected_signal.decision_inputs["admission"]["stake"]["selected_order_terms"],
                expected_terms,
            )
            conflicting = DecisionContext(
                **{
                    **persisted,
                    "selected_order_terms": {**expected_terms, "stake": 99.0},
                }
            )
            with self.assertRaisesRegex(ValueError, "conflicts with frozen"):
                store.save_decision_context(conflicting)

            blocked_state = MonitorState(
                symbol="ETHUSDT",
                storage=store,
                min_order_gap_ms=0,
            )
            states.append(blocked_state)
            blocked_signal = replace(
                selected_profile_signal(179_999),
                daily_profile_selected=False,
            )
            blocked = blocked_state._maybe_open_order(
                blocked_signal,
                latest_kline(179_999),
                daily_profile_required=True,
            )
            blocked_context = store.load_decision_context(
                "ETHUSDT",
                blocked_state.selected_signal.decision_id,
            )
            self.assertEqual(blocked, "DAILY_PROFILE_NOT_SELECTED")
            self.assertEqual(blocked_context["selected_order_terms"], {})

    def test_quality_inputs_are_one_canonical_persisted_source_across_models_and_restart(self):
        marker = "TASK8-QUALITY-INPUT-UNIQUE-MARKER"
        with managed_sqlite_states() as (db_path, store, states):
            state = MonitorState(
                symbol="BTCUSDT",
                storage=store,
                min_order_gap_ms=0,
            )
            states.append(state)
            signal = selected_profile_signal(119_999, reason="canonical quality")
            latest = latest_kline(119_999)
            original_attach = state._attach_quality_score

            def attach_with_marker(candidate, *, current_time=None):
                scored = original_attach(candidate, current_time=current_time)
                return replace(
                    scored,
                    quality_score_inputs={"unique_marker": marker, "slot": scored.order_slot},
                )

            with patch.object(state, "_attach_quality_score", side_effect=attach_with_marker):
                decision = state._maybe_open_order(signal, latest)

            self.assertEqual(decision, "OPENED")
            selected = state.selected_signal
            order = state.simulator.orders[0]
            observation = state.observations[0]
            for model in (selected, order, observation):
                self.assertIs(
                    model.quality_score_inputs,
                    model.decision_inputs["score"]["quality_score_inputs"],
                )
            selected.quality_score_inputs["alias_probe"] = True
            self.assertTrue(
                selected.decision_inputs["score"]["quality_score_inputs"]["alias_probe"]
            )

            with closing(sqlite3.connect(db_path)) as connection:
                connection.row_factory = sqlite3.Row
                persisted_payloads = []
                for table, columns in (
                    ("decision_contexts", ("input_payload", "outcome_payload")),
                    ("orders", ("payload",)),
                    ("observation_signals", ("payload",)),
                    ("order_entry_snapshots", ("entry_payload",)),
                    ("signal_audit", ("payload",)),
                ):
                    rows = connection.execute(
                        f"select {', '.join(columns)} from {table}"
                    ).fetchall()
                    persisted_payloads.extend(
                        str(row[column])
                        for row in rows
                        for column in columns
                        if row[column] is not None
                    )
                raw_order = json.loads(
                    connection.execute("select payload from orders").fetchone()[0]
                )
                raw_observation = json.loads(
                    connection.execute("select payload from observation_signals").fetchone()[0]
                )
            self.assertEqual("".join(persisted_payloads).count(marker), 1)
            for payload in (raw_order, raw_observation):
                self.assertIn("decision_context_ref", payload)
                self.assertNotIn("decision_inputs", payload)
                self.assertNotIn("decision_trace", payload)
                self.assertNotIn("quality_score_inputs", payload)

            restored_order = store.load_orders("BTCUSDT")[0]
            restored_observation = store.load_observations("BTCUSDT")[0]
            for model in (restored_order, restored_observation):
                self.assertIs(
                    model.quality_score_inputs,
                    model.decision_inputs["score"]["quality_score_inputs"],
                )
                self.assertEqual(model.quality_score_inputs["unique_marker"], marker)

            restored_order.status = "SETTLED"
            restored_order.result = "WIN"
            restored_order.settled_at = restored_order.expires_at
            restored_order.exit_price = restored_order.entry_price + 1.0
            restored_order.pnl = restored_order.win_return - restored_order.stake
            store.save_settled_order_with_credit(restored_order, "BTCUSDT", None)
            settled = store.load_orders("BTCUSDT")[0]
            self.assertIs(
                settled.quality_score_inputs,
                settled.decision_inputs["score"]["quality_score_inputs"],
            )
            self.assertEqual(settled.quality_score_inputs["unique_marker"], marker)
            restored_observation.status = "SETTLED"
            restored_observation.result = "WIN"
            restored_observation.settled_at = restored_observation.expires_at
            restored_observation.exit_price = restored_observation.entry_price + 1.0
            restored_observation.pnl = 8.0
            store.save_observation(restored_observation, "BTCUSDT")
            settled_observation = store.load_observations("BTCUSDT")[0]
            self.assertIs(
                settled_observation.quality_score_inputs,
                settled_observation.decision_inputs["score"]["quality_score_inputs"],
            )
            page_item = store.page_observations("BTCUSDT")["observations"][0]
            self.assertEqual(
                page_item["quality_score_inputs"],
                page_item["decision_inputs"]["score"]["quality_score_inputs"],
            )

    def test_context_is_authoritative_and_rejects_tampered_model_provenance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            state = MonitorState(
                symbol="BTCUSDT",
                storage=store,
                min_order_gap_ms=0,
            )
            restart_store = None
            state_closed = False
            try:
                decision = state._maybe_open_order(
                    selected_profile_signal(119_999, reason="provenance control"),
                    latest_kline(119_999),
                )
                self.assertEqual(decision, "OPENED")
                order = state.simulator.orders[0]
                observed = state.observations[0]
                context = store.load_decision_context(
                    "BTCUSDT",
                    state.selected_signal.decision_id,
                )
                with closing(sqlite3.connect(db_path)) as connection:
                    compact_order = json.loads(
                        connection.execute("select payload from orders").fetchone()[0]
                    )
                    compact_observation = json.loads(
                        connection.execute(
                            "select payload from observation_signals"
                        ).fetchone()[0]
                    )
                required_marker = {
                    "decision_id",
                    "context_version",
                    "runtime_config_hash",
                    "strategy_build_id",
                    "candidate_origin",
                    "canonical_identity_hash",
                }
                self.assertEqual(
                    set(compact_order["decision_context_ref"]),
                    required_marker,
                )
                self.assertEqual(
                    compact_order["decision_context_ref"],
                    compact_observation["decision_context_ref"],
                )

                legacy_order = order.to_dict()
                legacy_observation = observed.to_dict()

                def write_payload(table, payload):
                    with closing(sqlite3.connect(db_path)) as connection:
                        connection.execute(
                            f"update {table} set payload = ?",
                            (json.dumps(payload, ensure_ascii=False),),
                        )
                        connection.commit()

                state.close()
                state_closed = True
                store.close()
                restart_store = SQLiteMonitorStore(db_path)

                write_payload("orders", legacy_order)
                write_payload("observation_signals", legacy_observation)
                restored_order = restart_store.load_orders("BTCUSDT")[0]
                restored_observation = restart_store.load_observations("BTCUSDT")[0]
                for model in (restored_order, restored_observation):
                    self.assertEqual(model.decision_inputs, context["inputs"])
                    self.assertIs(
                        model.quality_score_inputs,
                        model.decision_inputs["score"]["quality_score_inputs"],
                    )
                    self.assertEqual(
                        model.adaptive_profile_state,
                        context["inputs"]["signal"]["adaptive_profile_state"],
                    )
                    self.assertEqual(
                        model.entry_structure_shadow,
                        context["inputs"]["signal"]["entry_structure_shadow"],
                    )

                tampered_order = deepcopy(legacy_order)
                tampered_order["quality_score_inputs"] = {"tampered": True}
                write_payload("orders", tampered_order)
                with self.assertRaisesRegex(ValueError, "quality_score_inputs"):
                    restart_store.load_orders("BTCUSDT")

                tampered_order = deepcopy(legacy_order)
                tampered_order["strategy_tag"] = "tampered-strategy"
                write_payload("orders", tampered_order)
                with self.assertRaisesRegex(ValueError, "strategy_tag"):
                    restart_store.load_orders("BTCUSDT")

                for field, value in (
                    ("runtime_config_hash", "tampered-config"),
                    ("strategy_build_id", "tampered-build"),
                    ("candidate_origin", "TAMPERED_ORIGIN"),
                ):
                    tampered_ref = deepcopy(compact_order)
                    tampered_ref[field] = value
                    write_payload("orders", tampered_ref)
                    with self.assertRaisesRegex(ValueError, field):
                        restart_store.load_orders("BTCUSDT")

                incomplete_ref = deepcopy(compact_order)
                incomplete_ref["decision_context_ref"].pop("strategy_build_id")
                write_payload("orders", incomplete_ref)
                with self.assertRaisesRegex(ValueError, "reference"):
                    restart_store.load_orders("BTCUSDT")

                tampered_observation = deepcopy(legacy_observation)
                tampered_observation["direction"] = "SHORT"
                write_payload("observation_signals", tampered_observation)
                with self.assertRaisesRegex(ValueError, "direction"):
                    restart_store.load_observations("BTCUSDT")

                tampered_observation = deepcopy(legacy_observation)
                tampered_observation["decision_inputs"]["score"][
                    "quality_score_inputs"
                ] = {"tampered": "canonical-copy"}
                write_payload("observation_signals", tampered_observation)
                with self.assertRaisesRegex(ValueError, "decision_inputs"):
                    restart_store.load_observations("BTCUSDT")

                write_payload("orders", tampered_order)
                settled_order = replace(
                    order,
                    status="SETTLED",
                    result="WIN",
                    settled_at=order.expires_at,
                    exit_price=order.entry_price + 1.0,
                    pnl=order.win_return - order.stake,
                )
                with self.assertRaisesRegex(ValueError, "strategy_tag"):
                    restart_store.save_settled_order_with_credit(
                        settled_order,
                        "BTCUSDT",
                        None,
                    )

                write_payload("observation_signals", tampered_observation)
                settled_observation = replace(
                    observed,
                    status="SETTLED",
                    result="WIN",
                    settled_at=observed.expires_at,
                    exit_price=observed.entry_price + 1.0,
                    pnl=8.0,
                )
                with self.assertRaisesRegex(ValueError, "decision_inputs"):
                    restart_store.save_observation(
                        settled_observation,
                        "BTCUSDT",
                    )
            finally:
                if not state_closed:
                    state.close()
                store.close()
                if restart_store is not None:
                    restart_store.close()

    def test_real_task8_bundle_replays_full_e0_ref_and_compact_payloads(self):
        compatibility_fields = {
            "decision_inputs",
            "decision_trace",
            "first_decisive_block",
            "quality_score_inputs",
            "quality_score",
            "quality_score_version",
            "quality_score_mode",
            "quality_score_context",
            "quality_score_components",
            "adaptive_profile_state",
            "entry_structure_shadow",
        }

        class CapturingStore(SQLiteMonitorStore):
            def __init__(self, path):
                super().__init__(path)
                self.last_open_bundle = None

            def save_open_order_decision(self, **arguments):
                created = super().save_open_order_decision(**arguments)
                self.last_open_bundle = arguments
                return created

        def stored_payload(model, style):
            if style == "full":
                return model.to_dict()
            compact = decision_linked_storage_payload(model)
            if style == "compact":
                return compact
            payload = model.to_dict()
            for field in compatibility_fields:
                payload.pop(field, None)
            payload["decision_context_ref"] = deepcopy(
                compact["decision_context_ref"]
            )
            return payload

        for style in ("full", "e0_ref", "compact"):
            with self.subTest(style=style), tempfile.TemporaryDirectory() as temp_dir:
                db_path = Path(temp_dir) / "monitor.sqlite3"
                store = CapturingStore(db_path)
                webhook = RecordingWebhook()
                state = MonitorState(
                    symbol="BTCUSDT",
                    storage=store,
                    webhook=webhook,
                    min_order_gap_ms=0,
                )
                try:
                    signal = selected_profile_signal(
                        119_999,
                        reason=f"legacy replay {style}",
                    )
                    latest = latest_kline(119_999)
                    self.assertEqual(state._maybe_open_order(signal, latest), "OPENED")
                    arguments = store.last_open_bundle
                    self.assertIsNotNone(arguments)
                    self.assertIn("signal", arguments["context"].inputs)
                    order = arguments["order"]
                    observed = arguments["observation"]
                    self.assertIsNotNone(observed)

                    def rewrite(order_model, observation_model):
                        with closing(sqlite3.connect(db_path)) as connection:
                            connection.execute(
                                "update orders set payload = ? where symbol = ? and order_id = ?",
                                (
                                    json.dumps(stored_payload(order_model, style), ensure_ascii=False),
                                    "BTCUSDT",
                                    order_model.id,
                                ),
                            )
                            self.assertEqual(
                                connection.execute("select changes()").fetchone()[0],
                                1,
                            )
                            connection.execute(
                                "update observation_signals set payload = ? "
                                "where symbol = ? and observation_key = ?",
                                (
                                    json.dumps(
                                        stored_payload(observation_model, style),
                                        ensure_ascii=False,
                                    ),
                                    "BTCUSDT",
                                    observation_model.observation_key,
                                ),
                            )
                            self.assertEqual(
                                connection.execute("select changes()").fetchone()[0],
                                1,
                            )
                            connection.commit()

                    rewrite(order, observed)
                    if style == "e0_ref":
                        tampered = stored_payload(order, style)
                        tampered["score"] = float(tampered["score"]) + 1.0
                        with closing(sqlite3.connect(db_path)) as connection:
                            connection.execute(
                                "update orders set payload = ? where order_id = ?",
                                (json.dumps(tampered, ensure_ascii=False), order.id),
                            )
                            connection.commit()
                        with self.assertRaisesRegex(ValueError, "score|frozen"):
                            store.save_open_order_decision(**arguments)
                        rewrite(order, observed)

                    before = atomic_sqlite_bundle_counts(db_path)
                    self.assertFalse(store.save_open_order_decision(**arguments))
                    self.assertFalse(store.save_open_order_decision(**arguments))
                    self.assertEqual(atomic_sqlite_bundle_counts(db_path), before)
                    self.assertEqual(state._maybe_open_order(signal, latest), "OPENED")
                    self.assertEqual(len(webhook.calls), 1)

                    settled_order = replace(
                        store.load_orders("BTCUSDT")[0],
                        status="SETTLED",
                        result="WIN",
                        settled_at=order.expires_at,
                        exit_price=order.entry_price + 1.0,
                        pnl=order.win_return - order.stake,
                    )
                    settled_observation = replace(
                        store.load_observations("BTCUSDT")[0],
                        status="SETTLED",
                        result="WIN",
                        settled_at=observed.expires_at,
                        exit_price=observed.entry_price + 1.0,
                        pnl=8.0,
                    )
                    credit = StakeProgressionCredit(
                        source_order_id=settled_order.id,
                        created_at=settled_order.settled_at,
                        version=settled_order.stake_progression_version,
                        direction=settled_order.direction,
                    )
                    store.save_settled_order_with_credit(
                        settled_order,
                        "BTCUSDT",
                        credit,
                    )
                    store.save_observations((settled_observation,), "BTCUSDT")
                    rewrite(settled_order, settled_observation)
                    settled_counts = atomic_sqlite_bundle_counts(db_path)
                    with closing(sqlite3.connect(db_path)) as connection:
                        settled_revision = connection.execute(
                            "select revision from profile_summary_revisions "
                            "where symbol = 'BTCUSDT'"
                        ).fetchone()[0]
                        settled_credit = connection.execute(
                            "select version, credit_id, source_order_id, status, "
                            "created_at, consumed_order_id, consumed_at, direction "
                            "from stake_progression_credits where symbol = 'BTCUSDT'"
                        ).fetchall()

                    store.save_settled_order_with_credit(
                        settled_order,
                        "BTCUSDT",
                        credit,
                    )
                    store.save_observations((settled_observation,), "BTCUSDT")
                    self.assertEqual(atomic_sqlite_bundle_counts(db_path), settled_counts)
                    with closing(sqlite3.connect(db_path)) as connection:
                        self.assertEqual(
                            connection.execute(
                                "select revision from profile_summary_revisions "
                                "where symbol = 'BTCUSDT'"
                            ).fetchone()[0],
                            settled_revision,
                        )
                        self.assertEqual(
                            connection.execute(
                                "select version, credit_id, source_order_id, status, "
                                "created_at, consumed_order_id, consumed_at, direction "
                                "from stake_progression_credits where symbol = 'BTCUSDT'"
                            ).fetchall(),
                            settled_credit,
                        )

                    order_conflicts = (
                        replace(settled_order, result="LOSS"),
                        replace(settled_order, exit_price=settled_order.exit_price + 1.0),
                        replace(settled_order, settled_at=settled_order.settled_at + 1),
                        replace(settled_order, pnl=settled_order.pnl + 1.0),
                    )
                    for conflict in order_conflicts:
                        with self.assertRaisesRegex(ValueError, "terminal order"):
                            store.save_settled_order_with_credit(
                                conflict,
                                "BTCUSDT",
                                credit,
                            )
                    observation_conflicts = (
                        replace(settled_observation, result="LOSS"),
                        replace(
                            settled_observation,
                            exit_price=settled_observation.exit_price + 1.0,
                        ),
                        replace(
                            settled_observation,
                            settled_at=settled_observation.settled_at + 1,
                        ),
                        replace(settled_observation, pnl=settled_observation.pnl + 1.0),
                    )
                    for conflict in observation_conflicts:
                        with self.assertRaisesRegex(ValueError, "terminal observation"):
                            store.save_observations((conflict,), "BTCUSDT")
                    with self.assertRaisesRegex(ValueError, "frozen order"):
                        store.save_settled_order_with_credit(
                            replace(settled_order, direction="SHORT"),
                            "BTCUSDT",
                            None,
                        )
                    with self.assertRaisesRegex(ValueError, "frozen observation"):
                        store.save_observations(
                            (
                                replace(
                                    settled_observation,
                                    strategy_family="tampered-family",
                                ),
                            ),
                            "BTCUSDT",
                        )

                    self.assertFalse(store.save_open_order_decision(**arguments))
                    self.assertFalse(store.save_open_order_decision(**arguments))
                    self.assertEqual(atomic_sqlite_bundle_counts(db_path), settled_counts)
                    self.assertEqual(store.load_orders("BTCUSDT")[0].status, "SETTLED")
                    self.assertEqual(
                        store.load_observations("BTCUSDT")[0].status,
                        "SETTLED",
                    )
                    self.assertEqual(len(webhook.calls), 1)
                finally:
                    state.close()
                    store.close()

    def test_context_frozen_fields_and_sql_lifecycle_columns_are_authoritative(self):
        with managed_sqlite_states() as (db_path, store, states):
            state = MonitorState(
                symbol="BTCUSDT",
                storage=store,
                min_order_gap_ms=0,
            )
            states.append(state)
            self.assertEqual(
                state._maybe_open_order(
                    selected_profile_signal(119_999, reason="canonical lifecycle"),
                    latest_kline(119_999),
                ),
                "OPENED",
            )

            with closing(sqlite3.connect(db_path)) as connection:
                original_order_payload = json.loads(
                    connection.execute("select payload from orders").fetchone()[0]
                )
                original_observation_payload = json.loads(
                    connection.execute(
                        "select payload from observation_signals"
                    ).fetchone()[0]
                )

            def write_payload(table, payload):
                with closing(sqlite3.connect(db_path)) as connection:
                    connection.execute(
                        f"update {table} set payload = ?",
                        (json.dumps(payload, ensure_ascii=False),),
                    )
                    connection.commit()

            for field, value in (
                ("score", 999.0),
                ("threshold", 1.0),
                ("calculated_threshold", 2.0),
                ("reason", "tampered reason"),
                ("direction", "SHORT"),
                ("stake", 777.0),
                ("opened_at", 42),
            ):
                tampered = deepcopy(original_order_payload)
                tampered[field] = value
                write_payload("orders", tampered)
                with self.assertRaisesRegex(ValueError, field):
                    store.load_orders("BTCUSDT")

            for field, value in (
                ("score", -999.0),
                ("threshold", 1.0),
                ("reason", "tampered observation"),
                ("direction", "SHORT"),
                ("opened_at", 42),
            ):
                tampered = deepcopy(original_observation_payload)
                tampered[field] = value
                write_payload("observation_signals", tampered)
                with self.assertRaisesRegex(ValueError, field):
                    store.load_observations("BTCUSDT")

            payload_settled_order = deepcopy(original_order_payload)
            payload_settled_order.update(
                {
                    "status": "SETTLED",
                    "result": "WIN",
                    "exit_price": 999.0,
                    "settled_at": 719_999,
                    "pnl": 989.0,
                }
            )
            write_payload("orders", payload_settled_order)
            payload_settled_observation = deepcopy(original_observation_payload)
            payload_settled_observation.update(
                {
                    "status": "SETTLED",
                    "result": "WIN",
                    "exit_price": 999.0,
                    "settled_at": 719_999,
                    "pnl": 899.0,
                }
            )
            write_payload("observation_signals", payload_settled_observation)

            state.close()
            store.close()
            restart_store = SQLiteMonitorStore(db_path)
            restarted = MonitorState(
                symbol="BTCUSDT",
                storage=restart_store,
                max_open_orders=1,
                min_order_gap_ms=0,
            )
            states.append(restarted)
            restored_order = restarted.simulator.orders[0]
            restored_observation = restart_store.load_observations("BTCUSDT")[0]
            self.assertEqual(
                (
                    restored_order.status,
                    restored_order.result,
                    restored_order.exit_price,
                    restored_order.settled_at,
                    restored_order.pnl,
                ),
                ("OPEN", None, None, None, 0.0),
            )
            self.assertEqual(
                (
                    restored_observation.status,
                    restored_observation.result,
                    restored_observation.exit_price,
                    restored_observation.settled_at,
                    restored_observation.pnl,
                ),
                ("OPEN", None, None, None, 0.0),
            )

            blocked = restarted._maybe_open_order(
                replace(
                    selected_profile_signal(179_999, reason="must remain blocked"),
                    profile_key="second-profile",
                ),
                latest_kline(179_999),
            )
            self.assertEqual(blocked, "HOLD_OPEN_ORDER")
            with closing(sqlite3.connect(db_path)) as connection:
                self.assertEqual(
                    connection.execute(
                        "select count(*) from orders where status = 'OPEN'"
                    ).fetchone()[0],
                    1,
                )

            restored_order.status = "SETTLED"
            restored_order.result = "WIN"
            restored_order.exit_price = 101.0
            restored_order.settled_at = restored_order.expires_at
            restored_order.pnl = restored_order.win_return - restored_order.stake
            restart_store.save_settled_order_with_credit(
                restored_order,
                "BTCUSDT",
                None,
            )
            restored_observation.status = "SETTLED"
            restored_observation.result = "WIN"
            restored_observation.exit_price = 101.0
            restored_observation.settled_at = restored_observation.expires_at
            restored_observation.pnl = 8.0
            restart_store.save_observation(restored_observation, "BTCUSDT")
            settled_order = restart_store.load_orders("BTCUSDT")[0]
            settled_observation = restart_store.load_observations("BTCUSDT")[0]
            self.assertEqual(
                (settled_order.status, settled_order.result, settled_order.exit_price),
                ("SETTLED", "WIN", 101.0),
            )
            self.assertEqual(
                (
                    settled_observation.status,
                    settled_observation.result,
                    settled_observation.exit_price,
                ),
                ("SETTLED", "WIN", 101.0),
            )

    def test_observation_pages_filter_by_canonical_context_not_redundant_columns(self):
        with managed_sqlite_states() as (db_path, store, states):
            state = MonitorState(
                symbol="BTCUSDT",
                storage=store,
                min_order_gap_ms=0,
            )
            states.append(state)
            long_profile_key = (
                "10|canonical_long_family|canonical_long_tag|LONG|WD-08"
            )
            long_signal = replace(
                selected_profile_signal(
                    119_999,
                    profile_key=long_profile_key,
                    reason="canonical long observation",
                ),
                strategy_family="canonical_long_family",
                strategy_tag="canonical_long_tag",
                adaptive_profile_state={
                    "qualification_state": "QUALIFIED",
                    "status": "RESIDENT",
                },
                entry_structure_shadow={
                    "entry_structure_evaluated_at": 119_999,
                    "entry_structure_state": "SUPPORT_RECLAIM",
                    "entry_structure_bias": "LONG",
                    "active_level_source": "RECENT_SWING",
                    "candidate_origin": "NATIVE_ACTIONABLE",
                    "candidate_direction": "LONG",
                },
            )
            self.assertEqual(
                state._maybe_open_order(long_signal, latest_kline(119_999)),
                "OPENED",
            )
            short_signal = replace(
                selected_profile_signal(
                    179_999,
                    profile_key="canonical-short-profile",
                    reason="canonical short observation",
                ),
                direction="WAIT",
                observe_direction="SHORT",
                observe_only=True,
                strategy_family="canonical_short_family",
                strategy_tag="canonical_short_tag",
                threshold_segment="WD-23",
                score=-88.0,
                adaptive_profile_state={
                    "qualification_state": "WATCH",
                    "status": "CANDIDATE",
                },
                entry_structure_shadow={
                    "entry_structure_evaluated_at": 179_999,
                    "entry_structure_state": "RESISTANCE_REJECT",
                    "entry_structure_bias": "SHORT",
                    "active_level_source": "ROUND_LEVEL",
                    "candidate_origin": "RESEARCH_OBSERVATION",
                    "candidate_direction": "SHORT",
                },
            )
            self.assertTrue(
                state._record_observation(
                    short_signal,
                    latest_kline(179_999),
                    "RESEARCH_OBSERVE",
                )
            )
            for index in range(9):
                store.save_observation(
                    settled_observation(
                        10_000 + index,
                        "WIN",
                        240_000 + index * 60_000,
                        family="pagination_control",
                        tag="pagination_control",
                        direction="WAIT",
                        segment="PAGINATION",
                    ),
                    "BTCUSDT",
                )

            with closing(sqlite3.connect(db_path)) as connection:
                long_key = next(
                    item.observation_key
                    for item in state.observations
                    if item.direction == "LONG"
                )
                connection.execute(
                    """
                    update observation_signals
                    set direction = 'SHORT', strategy_family = 'canonical_short_family',
                        strategy_tag = 'canonical_short_tag',
                        threshold_segment = 'WD-23',
                        candidate_origin = 'RESEARCH_OBSERVATION',
                        qualification_state = 'WATCH', adaptive_state = 'CANDIDATE',
                        entry_structure_state = 'RESISTANCE_REJECT',
                        entry_structure_bias = 'SHORT',
                        active_level_source = 'ROUND_LEVEL'
                    where symbol = ? and observation_key = ?
                    """,
                    ("BTCUSDT", long_key),
                )
                self.assertEqual(connection.execute("select changes()").fetchone()[0], 1)
                connection.commit()

            long_page = store.page_observations("BTCUSDT", direction="LONG")
            short_page = store.page_observations("BTCUSDT", direction="SHORT")
            self.assertEqual(long_page["total"], 1)
            self.assertEqual(short_page["total"], 1)
            self.assertEqual(long_page["observations"][0]["direction"], "LONG")
            self.assertEqual(short_page["observations"][0]["direction"], "SHORT")
            self.assertEqual(
                store.page_observations(
                    "BTCUSDT", family="canonical_long_family"
                )["total"],
                1,
            )
            self.assertEqual(
                store.page_observations("BTCUSDT", tag="canonical_long_tag")["total"],
                1,
            )
            self.assertEqual(
                store.page_observations("BTCUSDT", segment="WD-08")["total"],
                1,
            )
            self.assertEqual(
                store.page_observations(
                    "BTCUSDT", profile=long_profile_key
                )["total"],
                1,
            )
            self.assertEqual(
                store.page_observations(
                    "BTCUSDT", origin="NATIVE_ACTIONABLE"
                )["total"],
                1,
            )
            self.assertEqual(
                store.page_observations(
                    "BTCUSDT", entry_structure_state="SUPPORT_RECLAIM"
                )["total"],
                1,
            )
            self.assertEqual(
                store.page_observations(
                    "BTCUSDT", entry_structure_bias="LONG"
                )["total"],
                1,
            )
            self.assertEqual(
                store.page_observations(
                    "BTCUSDT", active_level_source="RECENT_SWING"
                )["total"],
                1,
            )
            self.assertTrue(
                {"LONG", "SHORT"}.issubset(
                    long_page["filter_options"]["direction"]
                )
            )
            self.assertIn(
                "CANONICAL_LONG_FAMILY",
                long_page["filter_options"]["family"],
            )
            paged = store.page_observations("BTCUSDT", page=2, page_size=10)
            self.assertEqual((paged["total"], paged["page"], paged["total_pages"]), (11, 2, 2))
            self.assertEqual(len(paged["observations"]), 1)

    def test_open_bundle_payload_size_and_5000_projection_fit_capacity_contract(self):
        with managed_sqlite_states() as (db_path, store, states):
            state = MonitorState(
                symbol="BTCUSDT",
                storage=store,
                min_order_gap_ms=0,
            )
            states.append(state)

            decision = state._maybe_open_order(
                selected_profile_signal(119_999, reason="payload sizing"),
                latest_kline(119_999),
            )

            self.assertEqual(decision, "OPENED")
            with closing(sqlite3.connect(db_path)) as connection:
                component_bytes = {
                    "runtime": connection.execute(
                        "select coalesce(sum(length(canonical_payload)), 0) "
                        "from runtime_config_snapshots"
                    ).fetchone()[0],
                    "context": connection.execute(
                        "select coalesce(sum(length(input_payload) + length(outcome_payload)), 0) "
                        "from decision_contexts"
                    ).fetchone()[0],
                    "order": connection.execute(
                        "select coalesce(sum(length(payload)), 0) from orders"
                    ).fetchone()[0],
                    "observation": connection.execute(
                        "select coalesce(sum(length(payload)), 0) from observation_signals"
                    ).fetchone()[0],
                    "entry_snapshot": connection.execute(
                        "select coalesce(sum(length(entry_payload)), 0) "
                        "from order_entry_snapshots"
                    ).fetchone()[0],
                    "audit": connection.execute(
                        "select coalesce(sum(length(payload)), 0) from signal_audit"
                    ).fetchone()[0],
                }
            bundle_bytes = sum(component_bytes.values())
            projected_5000_bytes = bundle_bytes * 5_000

            self.assertLess(bundle_bytes, 64 * 1024, component_bytes)
            self.assertLess(projected_5000_bytes, MAX_DATABASE_BYTES)
            self.assertLess(
                component_bytes["order"] + component_bytes["observation"],
                component_bytes["context"],
            )

    def test_early_wave_block_samples_complete_admission_and_sqlite_capacity_once(self):
        with managed_sqlite_states() as (_db_path, store, states):
            state = MonitorState(
                symbol="BTCUSDT",
                storage=store,
                max_open_orders=5,
                max_open_long_orders=3,
                min_order_gap_ms=1_000,
            )
            states.append(state)
            state.simulator.orders = [
                SimulatedOrder(
                    id=1,
                    direction="LONG",
                    timeframe_minutes=10,
                    level="A",
                    reason="existing long",
                    entry_price=100.0,
                    opened_at=500,
                    expires_at=600_500,
                ),
                SimulatedOrder(
                    id=2,
                    direction="SHORT",
                    timeframe_minutes=10,
                    level="A",
                    reason="existing short",
                    entry_price=100.0,
                    opened_at=600,
                    expires_at=600_600,
                ),
            ]
            state._last_order_opened_at = {"LONG": 1_000, "SHORT": 900}
            latest = Kline(0, 100.0, 101.0, 99.0, 100.0, 10.0, 1_500)
            signal = replace(
                selected_profile_signal(1_500),
                wave_guard_mode="DIRECTION_BLOCKED",
                wave_guard_status="DIRECTION_BLOCKED",
                wave_state="DOWN_LEG",
            )

            with patch.object(
                store,
                "storage_capacity",
                wraps=store.storage_capacity,
            ) as capacity:
                decision = state._maybe_open_order(signal, latest)

            self.assertEqual(decision, "WAVE_DIRECTION_BLOCKED")
            self.assertEqual(capacity.call_count, 1)
            admission = state.selected_signal.decision_inputs["admission"]
            self.assertEqual(admission["global_open_count"], 2)
            self.assertEqual(admission["global_open_limit"], 5)
            self.assertEqual(admission["direction_open_count"], 1)
            self.assertEqual(admission["direction_open_limit"], 3)
            self.assertEqual(
                admission["cooldown"],
                {
                    "last_opened_at": 1_000,
                    "candidate_time": 1_500,
                    "minimum_gap_ms": 1_000,
                    "earliest_open_at": 2_000,
                    "elapsed_ms": 500,
                    "remaining_ms": 500,
                    "would_pass": False,
                },
            )
            storage_capacity = admission["storage_capacity"]
            self.assertEqual(storage_capacity["status"], "NORMAL")
            self.assertGreater(storage_capacity["database_bytes"], 0)
            self.assertGreater(storage_capacity["max_database_bytes"], 0)
            self.assertGreater(storage_capacity["core_reserve_bytes"], 0)
            self.assertTrue(storage_capacity["core_write_allowed"])
            self.assertTrue(storage_capacity["observation_write_allowed"])
            self.assertTrue(storage_capacity["compact_audit_allowed"])
            self.assertEqual(storage_capacity["sampled_at_ms"], 1_500)
            self.assertEqual(
                admission["guard_results"]["CAPACITY"]["result"],
                "NOT_EVALUATED",
            )

    def test_capacity_snapshot_error_is_audit_only_and_keeps_business_decision(self):
        storage = RecordingStorage()
        storage.storage_capacity = lambda: (_ for _ in ()).throw(OSError("capacity unavailable"))
        state = MonitorState(symbol="BTCUSDT", storage=storage)
        self.addCleanup(state.close)
        latest = latest_kline(119_999)
        signal = replace(
            selected_profile_signal(119_999),
            wave_guard_mode="DIRECTION_BLOCKED",
            wave_guard_status="DIRECTION_BLOCKED",
        )

        decision = state._maybe_open_order(signal, latest)

        self.assertEqual(decision, "WAVE_DIRECTION_BLOCKED")
        capacity = state.selected_signal.decision_inputs["admission"]["storage_capacity"]
        self.assertEqual(capacity["status"], "ERROR")
        self.assertEqual(capacity["error_type"], "OSError")
        self.assertIn("capacity unavailable", capacity["error"])
        self.assertNotIn(None, capacity.values())

    def test_opened_main_and_actual_observations_write_distinct_signal_audits(self):
        storage = RecordingStorage()
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            enable_rolling_edge_guard=False,
            enable_observation_profile_promotion=False,
        )
        selected = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="main long",
            price=100.0,
            open_time=60_000,
            score=90.0,
            threshold=70.0,
            threshold_segment="WD-01",
            strategy_family="main_family",
            strategy_tag="main_tag",
            profile_key="main-profile",
        )
        first_observation = replace(
            selected,
            direction="WAIT",
            observe_direction="SHORT",
            observe_only=True,
            score=-65.0,
            strategy_family="observe_short",
            strategy_tag="observe_short_tag",
            profile_key="short-profile",
        )
        second_observation = replace(
            selected,
            direction="WAIT",
            observe_direction="LONG",
            observe_only=True,
            score=66.0,
            threshold_segment="WD-02",
            strategy_family="observe_long",
            strategy_tag="observe_long_tag",
            profile_key="long-profile",
        )
        overlapping_attempt = replace(
            first_observation,
            strategy_tag="overlap_same_profile",
            profile_key="short-overlap",
        )

        with patch("app.state.analyze_volume_price", return_value=selected), patch(
            "app.state.analyze_observation_signals",
            return_value=[first_observation, second_observation, overlapping_attempt],
        ), patch("app.state.choose_trade_signal", return_value=selected):
            self.assertTrue(state.update_from_klines([kline(1, 100.0, 100.0)]))
        state.wait_for_storage_writes()

        self.assertEqual(len(storage.signals), 3)
        main = storage.signals[0]
        observations = storage.signals[1:]
        self.assertEqual(main[2], "OPENED")
        self.assertEqual(main[7], "ORDER_OPENED")
        self.assertTrue(main[5])
        self.assertTrue(main[6])
        self.assertIn("time_period_guard", main[4])
        self.assertEqual(
            main[4]["profile_guard"],
            {
                "status": "NOT_EVALUATED",
                "code": "PROFILE_GUARD_NOT_EVALUATED",
                "enabled": False,
                "observe_only": True,
                "blocked": False,
                "hit_keys": [],
                "cache_status": "UNKNOWN",
                "source_revision": None,
                "current_revision": None,
                "stale": False,
            },
        )
        self.assertNotIn("min_history", main[4]["profile_guard"])
        self.assertEqual([item[2] for item in observations], ["RESEARCH_OBSERVE", "RESEARCH_OBSERVE"])
        self.assertEqual([item[7] for item in observations], ["OBSERVATION_CANDIDATE"] * 2)
        self.assertEqual([item[1]["direction"] for item in observations], ["SHORT", "LONG"])
        self.assertEqual(
            [item[1]["profile_key"] for item in observations],
            ["short-profile", "long-profile"],
        )
        self.assertEqual(main[4]["result_sequence_guard"]["direction"], "LONG")
        self.assertNotEqual(main[4]["result_sequence_guard"]["status"], "NOT_EVALUATED")
        self.assertEqual(main[4]["profile_health_guard"]["direction"], "LONG")
        short_audit = observations[0][4]
        for guard_name in (
            "rolling_edge",
            "result_sequence_guard",
            "wave_batch_guard",
            "profile_degradation_guard",
            "profile_health_guard",
            "profile_guard",
        ):
            self.assertEqual(short_audit[guard_name]["status"], "NOT_EVALUATED")
            self.assertEqual(short_audit[guard_name]["code"], "NOT_EVALUATED")
        self.assertEqual(short_audit["result_sequence_guard"]["direction"], "SHORT")
        self.assertEqual(short_audit["profile_health_guard"]["direction"], "SHORT")
        self.assertNotIn("consecutive_losses", short_audit["result_sequence_guard"])
        self.assertNotIn("sample_size", short_audit["profile_health_guard"])
        self.assertNotIn("batch_orders", short_audit["wave_batch_guard"])
        self.assertEqual(
            short_audit["profile_degradation_guard"]["profile_key"],
            "short-profile",
        )
        self.assertEqual(
            short_audit["wave_batch_guard"]["current_batch_id"],
            observations[0][1]["wave_batch_id"],
        )
        self.assertEqual(short_audit["time_period_guard"]["status"], "NOT_EVALUATED")
        self.assertEqual(short_audit["time_period_guard"]["code"], "NOT_EVALUATED")
        self.assertIn("local_hour", short_audit["time_period_guard"])
        self.assertIn("window", short_audit["time_period_guard"])
        self.assertEqual(len(storage.observations), 2)

    def test_signal_audit_context_override_is_snapshotted_before_async_write(self):
        storage = RecordingStorage()
        storage.write_gate = threading.Event()
        state = MonitorState(symbol="BTCUSDT", storage=storage)
        audit_context = {
            "result_sequence_guard": {
                "status": "NOT_EVALUATED",
                "code": "NOT_EVALUATED",
                "direction": "SHORT",
                "hit_keys": ["original"],
            }
        }

        state._save_signal(
            Signal(
                direction="SHORT",
                timeframe_minutes=10,
                level="B",
                reason="observation",
                price=100.0,
                open_time=60_000,
            ),
            "RESEARCH_OBSERVE",
            60_000,
            force_independent=True,
            event_kind="OBSERVATION_CANDIDATE",
            audit_context_override=audit_context,
        )
        audit_context["result_sequence_guard"]["status"] = "MUTATED"
        audit_context["result_sequence_guard"]["hit_keys"].append("mutated")
        state.result_sequence_guard["status"] = "PAUSED"
        storage.write_gate.set()
        state.wait_for_storage_writes()

        stored = storage.signals[0][4]["result_sequence_guard"]
        self.assertEqual(stored["status"], "NOT_EVALUATED")
        self.assertEqual(stored["hit_keys"], ["original"])

    def test_observation_audit_collector_is_cleared_after_update_exception(self):
        state = MonitorState(symbol="BTCUSDT")
        selected = Signal(
            direction="WAIT",
            timeframe_minutes=10,
            level="B",
            reason="wait",
            price=100.0,
            open_time=60_000,
        )
        with patch("app.state.analyze_volume_price", return_value=selected), patch(
            "app.state.analyze_observation_signals", return_value=[]
        ), patch("app.state.choose_trade_signal", return_value=selected), patch.object(
            state, "_maybe_open_order", side_effect=RuntimeError("decision failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "decision failed"):
                state.update_from_klines([kline(1, 100.0, 100.0)])

        self.assertIsNone(state._observation_audit_collector)

    def test_realtime_price_update_does_not_enter_strategy_state(self):
        state = MonitorState(symbol="BTCUSDT", now_ms=lambda: 100_020)
        context = state.capture_symbol_context()
        before = state.snapshot()

        accepted = state.update_realtime_price(
            101.25,
            event_time_ms=100_000,
            received_at_ms=100_010,
            expected_context=context,
        )

        self.assertTrue(accepted)
        self.assertEqual(
            state.price_snapshot(),
            {
                "symbol": "BTCUSDT",
                "latest_price": 101.25,
                "event_time_ms": 100_000,
                "received_at_ms": 100_010,
                "stale": False,
                "stream_status": "CONNECTED",
            },
        )
        after = state.snapshot()
        for key in (
            "latest_kline",
            "signals",
            "selected_signal",
            "order_decision",
            "orders",
            "kline_count",
            "updated_at_ms",
        ):
            self.assertEqual(after[key], before[key], key)

    def test_realtime_price_rejects_stale_or_old_symbol_update(self):
        state = MonitorState(symbol="BTCUSDT")
        old_context = state.capture_symbol_context()
        self.assertTrue(
            state.update_realtime_price(
                101.0,
                event_time_ms=200,
                received_at_ms=210,
                expected_context=old_context,
            )
        )
        self.assertFalse(
            state.update_realtime_price(
                99.0,
                event_time_ms=199,
                received_at_ms=220,
                expected_context=old_context,
            )
        )
        state.reset_symbol("ETHUSDT")
        self.assertFalse(
            state.update_realtime_price(
                1.0,
                event_time_ms=300,
                received_at_ms=301,
                expected_context=old_context,
            )
        )
        self.assertEqual(state.price_snapshot()["symbol"], "ETHUSDT")
        self.assertIsNone(state.price_snapshot()["latest_price"])

    def test_old_symbol_async_error_does_not_overwrite_current_state(self):
        state = MonitorState(symbol="BTCUSDT")
        old_context = state.capture_symbol_context()
        state.reset_symbol("ETHUSDT")
        before = state.snapshot()

        accepted = state.record_error(
            "old BTC stream failed",
            expected_context=old_context,
        )

        self.assertFalse(accepted)
        self.assertEqual(state.snapshot()["last_error"], before["last_error"])
        self.assertEqual(state.snapshot()["updated_at_ms"], before["updated_at_ms"])

    def test_profile_degradation_blocks_selected_profile_after_three_losses(self):
        state = MonitorState(
            symbol="BTCUSDT",
            min_order_gap_ms=0,
            enable_rolling_edge_guard=False,
            result_sequence_guard_config=ResultSequenceGuardConfig(enabled=False),
        )
        settle_profile_losses(state)
        current_time = 33 * MINUTE_MS
        signal = selected_profile_signal(current_time)

        decision = state._maybe_open_order(
            signal,
            latest_kline(current_time),
        )

        snapshot = state.snapshot()
        self.assertEqual(decision, "PROFILE_DEGRADATION_BLOCKED")
        self.assertEqual(snapshot["profile_degradation_guard"]["status"], "COOLDOWN")
        self.assertEqual(
            snapshot["profile_degradation_guard"]["consecutive_losses"],
            3,
        )
        self.assertEqual(
            snapshot["observations"][0]["source_decision"],
            "PROFILE_DEGRADATION_BLOCKED",
        )
        self.assertEqual(snapshot["risk_pause"], "画像连续三笔亏损，进入冷却")

    def test_profile_recovery_probe_uses_base_stake_without_consuming_pending_credit(self):
        state = profile_guard_state(max_open_orders=1)
        settle_profile_losses(state)
        pending = StakeProgressionCredit(
            source_order_id=99,
            created_at=33 * MINUTE_MS,
        )
        state.simulator.stake_progression.credits.append(pending)
        current_time = 92 * MINUTE_MS

        decision = state._maybe_open_order(
            selected_profile_signal(current_time),
            latest_kline(current_time),
        )

        probe = state.simulator.orders[-1]
        guard = state.snapshot()["profile_degradation_guard"]
        self.assertEqual(decision, "OPENED")
        self.assertEqual(guard["status"], "RECOVERY_PENDING")
        self.assertTrue(guard["blocked"])
        self.assertFalse(guard["allow_progression"])
        self.assertEqual(guard["pause_until"], current_time)
        self.assertEqual(guard["probe_order_id"], probe.id)
        self.assertTrue(probe.profile_degradation_probe)
        self.assertEqual(probe.profile_degradation_triggered_at, 32 * MINUTE_MS)
        self.assertEqual(probe.reason, "selected live profile；画像退化试探单")
        self.assertEqual(probe.stake, 10.0)
        self.assertEqual(probe.stake_progression_step, 1)
        self.assertEqual(pending.status, "PENDING")
        self.assertTrue(state.selected_signal.profile_degradation_probe)

        blocked = state._maybe_open_order(
            selected_profile_signal(93 * MINUTE_MS, reason="second profile signal"),
            latest_kline(93 * MINUTE_MS),
        )

        pending_guard = state.snapshot()["profile_degradation_guard"]
        self.assertEqual(blocked, "HOLD_OPEN_ORDER")
        self.assertEqual(pending_guard["status"], "RECOVERY_PENDING")
        self.assertEqual(pending_guard["probe_order_id"], probe.id)
        self.assertEqual(len(state.simulator.orders), 4)

    def test_profile_degradation_is_scoped_to_exact_profile(self):
        state = profile_guard_state()
        settle_profile_losses(state)
        state._maybe_open_order(
            selected_profile_signal(33 * MINUTE_MS),
            latest_kline(33 * MINUTE_MS),
        )
        self.assertEqual(
            state.snapshot()["profile_degradation_guard"]["status"],
            "COOLDOWN",
        )
        current_time = 34 * MINUTE_MS
        other_profile = "10|drop_reclaim|other_profile|LONG|WD-08"

        decision = state._maybe_open_order(
            selected_profile_signal(current_time, profile_key=other_profile),
            latest_kline(current_time),
        )

        guard = state.snapshot()["profile_degradation_guard"]
        self.assertEqual(decision, "OPENED")
        self.assertEqual(guard["status"], "NORMAL")
        self.assertEqual(guard["profile_key"], other_profile)
        self.assertEqual(guard["consecutive_losses"], 0)

    def test_non_applicable_candidates_clear_previous_profile_state(self):
        candidates = (
            (
                "unselected",
                replace(
                    selected_profile_signal(34 * MINUTE_MS),
                    daily_profile_selected=False,
                ),
                True,
                "DAILY_PROFILE_NOT_SELECTED",
            ),
            (
                "missing profile key",
                replace(
                    selected_profile_signal(34 * MINUTE_MS),
                    profile_key="",
                ),
                False,
                "OPENED",
            ),
        )
        for label, candidate, daily_profile_required, expected_decision in candidates:
            with self.subTest(label=label):
                state = profile_guard_state()
                settle_profile_losses(state)
                state._maybe_open_order(
                    selected_profile_signal(33 * MINUTE_MS),
                    latest_kline(33 * MINUTE_MS),
                )
                self.assertEqual(
                    state.snapshot()["profile_degradation_guard"]["status"],
                    "COOLDOWN",
                )

                decision = state._maybe_open_order(
                    candidate,
                    latest_kline(34 * MINUTE_MS),
                    daily_profile_required=daily_profile_required,
                )

                guard = state.snapshot()["profile_degradation_guard"]
                self.assertEqual(decision, expected_decision)
                self.assertEqual(guard["status"], "NORMAL")
                self.assertEqual(guard["profile_key"], "")

    def test_profile_probe_win_restores_normal_state(self):
        state = profile_guard_state()
        settle_profile_losses(state)
        boundary = 92 * MINUTE_MS
        state._maybe_open_order(
            selected_profile_signal(boundary),
            latest_kline(boundary),
        )
        probe = state.simulator.orders[-1]
        state.simulator.settle_expired_orders(102 * MINUTE_MS, 101.0)

        decision = state._maybe_open_order(
            selected_profile_signal(103 * MINUTE_MS, reason="after probe win"),
            latest_kline(103 * MINUTE_MS),
        )

        guard = state.snapshot()["profile_degradation_guard"]
        self.assertEqual(probe.result, "WIN")
        self.assertEqual(decision, "OPENED")
        self.assertEqual(guard["status"], "NORMAL")
        self.assertEqual(guard["consecutive_losses"], 0)
        self.assertFalse(state.simulator.orders[-1].profile_degradation_probe)

    def test_profile_probe_loss_restarts_cooldown_from_settlement(self):
        state = profile_guard_state()
        settle_profile_losses(state)
        boundary = 92 * MINUTE_MS
        state._maybe_open_order(
            selected_profile_signal(boundary),
            latest_kline(boundary),
        )
        probe = state.simulator.orders[-1]
        state.simulator.settle_expired_orders(102 * MINUTE_MS, 99.0)

        decision = state._maybe_open_order(
            selected_profile_signal(103 * MINUTE_MS, reason="after probe loss"),
            latest_kline(103 * MINUTE_MS),
        )

        guard = state.snapshot()["profile_degradation_guard"]
        self.assertEqual(probe.result, "LOSS")
        self.assertEqual(decision, "PROFILE_DEGRADATION_BLOCKED")
        self.assertEqual(guard["status"], "COOLDOWN")
        self.assertEqual(guard["consecutive_losses"], 4)
        self.assertEqual(guard["last_loss_settled_at"], 102 * MINUTE_MS)
        self.assertEqual(guard["pause_until"], 162 * MINUTE_MS)

    def test_profile_degradation_rebuilds_identically_after_sqlite_restart(self):
        with managed_sqlite_states() as (_db_path, store, states):
            running = profile_guard_state(storage=store, now_ms=lambda: 0)
            states.append(running)
            settle_profile_losses(running)
            for order in running.simulator.orders:
                store.save_order(order, "BTCUSDT")
            current_time = 33 * MINUTE_MS
            signal = selected_profile_signal(current_time)
            latest = latest_kline(current_time)

            before_decision = running._maybe_open_order(signal, latest)
            before_guard = dict(running.snapshot()["profile_degradation_guard"])
            running.wait_for_storage_writes()

            restarted = profile_guard_state(storage=store, now_ms=lambda: 0)
            states.append(restarted)
            restart_empty_guard = dict(
                restarted.snapshot()["profile_degradation_guard"]
            )
            after_decision = restarted._maybe_open_order(signal, latest)
            after_guard = dict(restarted.snapshot()["profile_degradation_guard"])
            restarted.wait_for_storage_writes()

        self.assertEqual(before_decision, "PROFILE_DEGRADATION_BLOCKED")
        self.assertEqual(restart_empty_guard["status"], "NORMAL")
        self.assertEqual(restart_empty_guard["profile_key"], "")
        self.assertEqual(restart_empty_guard["consecutive_losses"], 0)
        self.assertEqual(after_decision, before_decision)
        self.assertEqual(after_guard, before_guard)

    def test_profile_probe_storage_failure_rolls_back_pending_state(self):
        storage = RecordingStorage()
        state = profile_guard_state(storage=storage, now_ms=lambda: 0)
        settle_profile_losses(state)
        storage.fail_once("save_open_order_decision")
        boundary = 92 * MINUTE_MS

        decision = state._maybe_open_order(
            selected_profile_signal(boundary),
            latest_kline(boundary),
        )

        guard = state.snapshot()["profile_degradation_guard"]
        self.assertEqual(decision, "STORAGE_ERROR")
        self.assertEqual(guard["status"], "RECOVERY_READY")
        self.assertEqual(guard["probe_order_id"], 0)
        self.assertEqual(len(state.simulator.orders), 3)

    def test_wave_recovery_and_profile_probe_both_disable_progression(self):
        state = profile_guard_state(
            wave_batch_guard_config=WaveBatchGuardConfig(),
        )
        for index, (opened_minute, batch_id) in enumerate(
            ((0, "wave-a"), (11, "wave-a"), (30, "wave-b"), (41, "wave-b")),
            start=1,
        ):
            opened_at = opened_minute * MINUTE_MS
            state.simulator.open_order(
                selected_profile_signal(
                    opened_at,
                    reason=f"failed wave order {index}",
                    wave_batch_id=batch_id,
                ),
                entry_price=100.0,
                opened_at=opened_at,
            )
            state.simulator.settle_expired_orders(opened_at + 10 * MINUTE_MS, 99.0)
        pending = StakeProgressionCredit(
            source_order_id=99,
            created_at=52 * MINUTE_MS,
        )
        state.simulator.stake_progression.credits.append(pending)
        current_time = 111 * MINUTE_MS

        decision = state._maybe_open_order(
            selected_profile_signal(current_time, wave_batch_id="wave-c"),
            latest_kline(current_time),
        )

        order = state.simulator.orders[-1]
        snapshot = state.snapshot()
        self.assertEqual(decision, "OPENED")
        self.assertEqual(snapshot["wave_batch_guard"]["mode"], "RECOVERY")
        self.assertEqual(snapshot["profile_degradation_guard"]["status"], "RECOVERY_PENDING")
        self.assertEqual(snapshot["profile_degradation_guard"]["probe_order_id"], order.id)
        self.assertFalse(snapshot["profile_degradation_guard"]["allow_progression"])
        self.assertEqual(order.wave_guard_mode, "RECOVERY")
        self.assertTrue(order.profile_degradation_probe)
        self.assertEqual(order.stake, 10.0)
        self.assertEqual(order.stake_progression_step, 1)
        self.assertEqual(pending.status, "PENDING")
        self.assertIsNone(pending.consumed_at)
        self.assertIsNone(pending.consumed_order_id)

    def test_zero_profile_cooldown_disables_guard_without_blocking(self):
        state = profile_guard_state(
            profile_degradation_guard_config=ProfileDegradationGuardConfig(
                cooldown_minutes=0
            ),
        )
        settle_profile_losses(state)
        current_time = 33 * MINUTE_MS

        decision = state._maybe_open_order(
            selected_profile_signal(current_time),
            latest_kline(current_time),
        )

        order = state.simulator.orders[-1]
        guard = state.snapshot()["profile_degradation_guard"]
        self.assertEqual(decision, "OPENED")
        self.assertFalse(guard["enabled"])
        self.assertEqual(guard["status"], "DISABLED")
        self.assertEqual(guard["cooldown_minutes"], 0)
        self.assertFalse(order.profile_degradation_probe)

    def test_reset_symbol_resets_profile_degradation_state(self):
        state = profile_guard_state()
        settle_profile_losses(state)
        current_time = 33 * MINUTE_MS
        state._maybe_open_order(
            selected_profile_signal(current_time),
            latest_kline(current_time),
        )

        state.reset_symbol("ETHUSDT")

        guard = state.snapshot()["profile_degradation_guard"]
        self.assertTrue(guard["enabled"])
        self.assertEqual(guard["status"], "NORMAL")
        self.assertEqual(guard["profile_key"], "")
        self.assertEqual(guard["consecutive_losses"], 0)

    def test_daily_profile_rejection_precedes_mechanical_admission(self):
        state = MonitorState(symbol="BTCUSDT", max_open_orders=1)
        state.simulator.open_order(
            Signal("LONG", 10, "A", "existing", 100.0, 0),
            entry_price=100.0,
            opened_at=0,
        )
        signal = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="unselected daily profile",
            price=101.0,
            open_time=120_000,
            score=90.0,
            threshold=70.0,
            threshold_segment="WD-08",
            session_allowed=True,
            observe_direction="LONG",
        )

        decision = state._maybe_open_order(
            signal,
            Kline(60_000, 101.0, 101.0, 101.0, 101.0, 1.0, 120_000),
            daily_profile_required=True,
        )

        snapshot = state.snapshot()
        self.assertEqual(decision, "DAILY_PROFILE_NOT_SELECTED")
        self.assertEqual(snapshot["stats"]["total_orders"], 1)
        self.assertEqual(
            snapshot["observations"][0]["source_decision"],
            "DAILY_PROFILE_NOT_SELECTED",
        )

    def test_hold_and_cooldown_precede_risk_guards_and_clear_stale_pause(self):
        latest = Kline(2_940_001, 100.0, 100.0, 100.0, 100.0, 1.0, 3_000_000)
        signal = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="guarded setup",
            price=100.0,
            open_time=latest.close_time,
            score=90.0,
            threshold=70.0,
            threshold_segment="WD-08",
            session_allowed=True,
            observe_direction="LONG",
        )

        for expected in ("HOLD_OPEN_ORDER", "COOLDOWN"):
            with self.subTest(expected=expected):
                state = MonitorState(
                    symbol="BTCUSDT",
                    max_open_orders=1 if expected == "HOLD_OPEN_ORDER" else 2,
                    min_order_gap_ms=120_000,
                    rolling_edge_config=RollingEdgeConfig(min_samples=3),
                )
                for idx in range(3):
                    state.simulator.orders.append(
                        SimulatedOrder(
                            id=idx + 1,
                            direction="LONG",
                            timeframe_minutes=10,
                            level="A",
                            reason="guarded setup",
                            entry_price=100.0,
                            opened_at=600_000 + idx * 600_000,
                            expires_at=1_200_000 + idx * 600_000,
                            threshold_segment="WD-08",
                            status="SETTLED",
                            result="LOSS",
                            exit_price=99.0,
                            settled_at=1_200_000 + idx * 600_000,
                            pnl=-10.0,
                        )
                    )
                if expected == "HOLD_OPEN_ORDER":
                    state.simulator.orders.append(
                        SimulatedOrder(
                            id=4,
                            direction="SHORT",
                            timeframe_minutes=10,
                            level="A",
                            reason="open",
                            entry_price=100.0,
                            opened_at=2_000_000,
                            expires_at=3_600_000,
                            status="OPEN",
                        )
                    )
                else:
                    state._last_order_opened_at = latest.close_time - 60_000
                state.risk_pause = "stale risk pause"

                decision = state._maybe_open_order(signal, latest)

                self.assertEqual(decision, expected)
                self.assertEqual(state.risk_pause, "")
                self.assertEqual(
                    state.snapshot()["result_sequence_guard"]["consecutive_losses"],
                    0,
                )

    def test_result_sequence_guard_precedes_rolling_edge_guard(self):
        state = MonitorState(
            symbol="BTCUSDT",
            rolling_edge_config=RollingEdgeConfig(min_samples=3),
        )
        for idx in range(3):
            state.simulator.orders.append(
                SimulatedOrder(
                    id=idx + 1,
                    direction="LONG",
                    timeframe_minutes=10,
                    level="A",
                    reason="guarded setup",
                    entry_price=100.0,
                    opened_at=600_000 + idx * 600_000,
                    expires_at=1_200_000 + idx * 600_000,
                    threshold_segment="WD-08",
                    status="SETTLED",
                    result="LOSS",
                    exit_price=99.0,
                    settled_at=1_200_000 + idx * 600_000,
                    pnl=-10.0,
                )
            )
        latest = Kline(2_940_001, 100.0, 100.0, 100.0, 100.0, 1.0, 3_000_000)
        signal = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="guarded setup",
            price=100.0,
            open_time=latest.close_time,
            score=90.0,
            threshold=70.0,
            threshold_segment="WD-08",
            session_allowed=True,
            observe_direction="LONG",
        )

        decision = state._maybe_open_order(signal, latest)
        snapshot = state.snapshot()

        self.assertEqual(decision, "RESULT_SEQUENCE_GUARD_BLOCKED")
        self.assertEqual(snapshot["rolling_edge"]["status"], "DEGRADED")
        self.assertIn("结算序列守卫", snapshot["risk_pause"])
        self.assertNotIn("滚动优势衰退", snapshot["risk_pause"])
        self.assertEqual(
            snapshot["observations"][0]["source_decision"],
            "RESULT_SEQUENCE_GUARD_BLOCKED",
        )

    def test_successful_open_persists_atomically_before_follow_up_side_effects(self):
        webhook = RecordingWebhook()

        class BoundaryRecordingStorage(RecordingStorage):
            def __init__(self):
                super().__init__()
                self.state = None
                self.atomic_boundary = None

            def save_open_order_decision(self, **kwargs):
                self.atomic_boundary = {
                    "orders": len(self.state.simulator.orders),
                    "observations": len(self.state.observations),
                    "entry_snapshots": len(self.entry_snapshots),
                    "webhooks": len(webhook.calls),
                }
                super().save_open_order_decision(**kwargs)

        storage = BoundaryRecordingStorage()
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            webhook=webhook,
            now_ms=lambda: 1_000,
        )
        self.addCleanup(state.close)
        storage.state = state
        signal = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="observable open",
            price=100.0,
            open_time=1_000,
            score=80.0,
            threshold=70.0,
            threshold_segment="WD-08",
            session_allowed=True,
            observe_direction="LONG",
        )

        decision = state._maybe_open_order(
            signal,
            Kline(0, 100.0, 100.0, 100.0, 100.0, 1.0, 1_000),
        )
        state.wait_for_storage_writes()
        snapshot = state.snapshot()

        self.assertEqual(decision, "OPENED")
        self.assertEqual([call[0] for call in storage.atomic_calls], ["open"])
        self.assertEqual(
            storage.atomic_boundary,
            {"orders": 1, "observations": 1, "entry_snapshots": 0, "webhooks": 0},
        )
        self.assertEqual(len(snapshot["orders"]), 1)
        self.assertEqual(len(snapshot["observations"]), 1)
        self.assertEqual(snapshot["observations"][0]["source_decision"], "OPENED")
        self.assertEqual(len(storage.entry_snapshots), 1)
        self.assertEqual(len(webhook.calls), 1)

    def test_open_bundle_failure_rolls_back_order_observation_keys_and_webhook(self):
        storage = RecordingStorage()
        storage.fail_once("save_open_order_decision")
        webhook = RecordingWebhook()
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            webhook=webhook,
            now_ms=lambda: 1_000,
        )
        self.addCleanup(state.close)
        signal = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="atomic failure",
            price=100.0,
            open_time=1_000,
            score=80.0,
            threshold=70.0,
            threshold_segment="WD-08",
            session_allowed=True,
            observe_direction="LONG",
        )
        latest = Kline(0, 100.0, 100.0, 100.0, 100.0, 1.0, 1_000)

        decision = state._maybe_open_order(signal, latest)

        self.assertEqual(decision, "STORAGE_ERROR")
        self.assertEqual(state.simulator.orders, [])
        self.assertEqual(state.simulator.stake_progression.pending_credits(), [])
        self.assertEqual(state.observations, [])
        self.assertEqual(state._opened_signal_keys, set())
        self.assertEqual(
            state._last_order_opened_at,
            {"LONG": None, "SHORT": None},
        )
        self.assertEqual(storage.decision_bundles, [])
        self.assertEqual(storage.entry_snapshots, [])
        self.assertEqual(webhook.calls, [])

    def test_real_sqlite_open_bundle_failure_rebuilds_persistence_block_outcome(self):
        with managed_sqlite_states() as (db_path, store, states):
            webhook = RecordingWebhook()
            state = MonitorState(
                symbol="BTCUSDT",
                storage=store,
                webhook=webhook,
                min_order_gap_ms=0,
                enable_rolling_edge_guard=False,
                enable_observation_profile_promotion=False,
            )
            states.append(state)
            signal = selected_profile_signal(119_999, reason="sqlite rollback")
            latest = latest_kline(119_999)

            def fail_after_order(step):
                if step == "order":
                    raise RuntimeError("injected bundle failure")

            with patch.object(
                store,
                "_after_bundle_step",
                side_effect=fail_after_order,
            ):
                decision = state._maybe_open_order(signal, latest)

            self.assertEqual(decision, "STORAGE_ERROR")
            self.assertEqual(state.simulator.orders, [])
            self.assertEqual(state.observations, [])
            self.assertEqual(webhook.calls, [])
            self.assertEqual(state.selected_signal.decision_trace[-1]["stage"], "PERSISTENCE")
            self.assertEqual(state.selected_signal.decision_trace[-1]["result"], "BLOCK")
            self.assertEqual(
                state.selected_signal.decision_trace[-1]["reason_code"],
                "STORAGE_ERROR",
            )
            self.assertEqual(state.selected_signal.first_decisive_block, "PERSISTENCE")
            self.assertFalse(
                state.selected_signal.decision_inputs["admission"]["open_allowed"]
            )
            self.assertEqual(
                state.selected_signal.decision_inputs["admission"]["stake"]["selected_order_terms"],
                {},
            )
            with closing(sqlite3.connect(db_path)) as connection:
                self.assertEqual(
                    connection.execute("select count(*) from orders").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute("select count(*) from decision_contexts").fetchone()[0],
                    0,
                )
            retried = state._maybe_open_order(signal, latest)
            persisted = store.load_decision_context(
                "BTCUSDT",
                state.selected_signal.decision_id,
            )
            self.assertEqual(retried, "OPENED")
            self.assertEqual(len(state.simulator.orders), 1)
            self.assertEqual(len(webhook.calls), 1)
            self.assertEqual(persisted["final_decision"], "OPENED")
            self.assertTrue(persisted["open_allowed"])

    def test_candidate_replay_read_failure_records_decision_replay_block(self):
        storage = RecordingStorage()
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            min_order_gap_ms=0,
        )
        self.addCleanup(state.close)
        signal = selected_profile_signal(119_999, reason="replay failure")
        latest = latest_kline(119_999)
        storage.load_decision_context_for_candidate = lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("replay unavailable")
        )

        decision = state._maybe_open_order(signal, latest)

        self.assertEqual(decision, "STORAGE_ERROR")
        self.assertEqual(state.simulator.orders, [])
        self.assertEqual(state.selected_signal.decision_trace[-1]["stage"], "DECISION_REPLAY")
        self.assertEqual(state.selected_signal.decision_trace[-1]["result"], "BLOCK")
        self.assertEqual(state.selected_signal.first_decisive_block, "DECISION_REPLAY")
        self.assertFalse(state.selected_signal.decision_inputs["admission"]["open_allowed"])

    def test_blocked_bundle_failure_keeps_business_block_first_and_appends_persistence(self):
        storage = RecordingStorage()
        storage.fail_once("save_decision_bundle")
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            min_order_gap_ms=0,
        )
        self.addCleanup(state.close)
        signal = replace(
            selected_profile_signal(119_999, reason="profile rejected"),
            daily_profile_selected=False,
        )

        decision = state._maybe_open_order(
            signal,
            latest_kline(119_999),
            daily_profile_required=True,
        )

        self.assertEqual(decision, "STORAGE_ERROR")
        self.assertEqual(state.simulator.orders, [])
        self.assertEqual(state.selected_signal.first_decisive_block, "DAILY_PROFILE")
        self.assertEqual(state.selected_signal.decision_trace[-1]["stage"], "PERSISTENCE")
        self.assertEqual(state.selected_signal.decision_trace[-1]["result"], "BLOCK")
        self.assertEqual(
            state.selected_signal.decision_trace[-1]["reason_code"],
            "STORAGE_ERROR",
        )

    def test_decision_freeze_failure_rolls_back_provisional_order(self):
        webhook = RecordingWebhook()
        state = MonitorState(
            symbol="BTCUSDT",
            webhook=webhook,
            now_ms=lambda: 1_000,
        )
        self.addCleanup(state.close)
        signal = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="freeze failure",
            price=100.0,
            open_time=1_000,
            score=80.0,
            threshold=70.0,
            threshold_segment="WD-08",
            session_allowed=True,
            observe_direction="LONG",
        )
        latest = Kline(0, 100.0, 100.0, 100.0, 100.0, 1.0, 1_000)

        original = state._decision_artifacts
        attempts = 0

        def fail_first_freeze(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ValueError("freeze failed")
            return original(*args, **kwargs)

        with patch.object(state, "_decision_artifacts", side_effect=fail_first_freeze):
            decision = state._maybe_open_order(signal, latest)

        self.assertEqual(decision, "STORAGE_ERROR")
        self.assertEqual(state.simulator.orders, [])
        self.assertEqual(state.simulator.stake_progression.pending_credits(), [])
        self.assertEqual(state.observations, [])
        self.assertEqual(state._opened_signal_keys, set())
        self.assertEqual(webhook.calls, [])
        self.assertEqual(state.selected_signal.decision_trace[-1]["stage"], "PERSISTENCE")
        self.assertEqual(state.selected_signal.decision_trace[-1]["result"], "BLOCK")
        self.assertEqual(state.selected_signal.first_decisive_block, "PERSISTENCE")

    def test_canonical_input_freeze_failure_still_builds_standard_error_outcome(self):
        webhook = RecordingWebhook()
        state = MonitorState(
            symbol="BTCUSDT",
            webhook=webhook,
            now_ms=lambda: 1_000,
        )
        self.addCleanup(state.close)
        signal = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="canonical freeze failure",
            price=100.0,
            open_time=1_000,
            score=80.0,
            threshold=70.0,
            threshold_segment="WD-08",
            session_allowed=True,
            observe_direction="LONG",
        )
        latest = Kline(0, 100.0, 100.0, 100.0, 100.0, 1.0, 1_000)

        with patch.object(
            state,
            "_canonical_decision_inputs",
            side_effect=ValueError("canonical inputs unavailable"),
        ):
            decision = state._maybe_open_order(signal, latest)

        self.assertEqual(decision, "STORAGE_ERROR")
        self.assertEqual(state.simulator.orders, [])
        self.assertEqual(state.observations, [])
        self.assertEqual(webhook.calls, [])
        self.assertEqual(state.selected_signal.first_decisive_block, "PERSISTENCE")
        self.assertEqual(state.selected_signal.decision_trace[-1]["stage"], "PERSISTENCE")
        self.assertEqual(state.selected_signal.decision_trace[-1]["result"], "BLOCK")
        self.assertEqual(
            state.selected_signal.decision_trace[-1]["reason_code"],
            "STORAGE_ERROR",
        )
        self.assertEqual(
            set(state.selected_signal.decision_inputs),
            {
                "identity",
                "market",
                "score",
                "volume_price",
                "indicators",
                "context",
                "admission",
                "entry_structure",
                "signal",
                "audit_snapshot",
            },
        )
        admission = state.selected_signal.decision_inputs["admission"]
        self.assertFalse(admission["open_allowed"])
        self.assertFalse(admission["observation_allowed"])
        self.assertEqual(admission["stake"]["selected_order_terms"], {})
        self.assertEqual(admission["stake"]["commit_status"], "NOT_COMMITTED")

    def test_post_commit_profile_maintenance_failure_keeps_open_and_dispatches_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            webhook = RecordingWebhook()
            state = MonitorState(
                symbol="BTCUSDT",
                storage=store,
                webhook=webhook,
                min_order_gap_ms=0,
            )
            store.wait_for_profile_summary_rebuilds(timeout=10)
            signal = selected_profile_signal(119_999, reason="committed before cache")
            latest = latest_kline(119_999)

            with patch.object(
                store,
                "_refresh_profile_summary_cache",
                side_effect=RuntimeError("cache maintenance failed"),
            ):
                first = state._maybe_open_order(signal, latest)
            self.assertIn("BTCUSDT", store._profile_summary_dirty)
            replay = state._maybe_open_order(signal, latest)
            store.wait_for_profile_summary_rebuilds(timeout=10)

            self.assertEqual((first, replay), ("OPENED", "OPENED"))
            self.assertEqual(len(store.load_orders("BTCUSDT")), 1)
            self.assertEqual(len(state.simulator.orders), 1)
            self.assertEqual(len(webhook.calls), 1)

    def test_successful_open_bundle_shares_one_decision_identity_before_webhook(self):
        events = []

        class OrderingStorage(RecordingStorage):
            def save_open_order_decision(self, **kwargs):
                events.append("bundle")
                super().save_open_order_decision(**kwargs)

        class OrderingWebhook(RecordingWebhook):
            def send_signal(self, symbol, signal, message=None, amount=None):
                events.append("webhook")
                super().send_signal(symbol, signal, message=message, amount=amount)

        storage = OrderingStorage()
        webhook = OrderingWebhook()
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            webhook=webhook,
            now_ms=lambda: 1_000,
        )
        signal = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="atomic success",
            price=100.0,
            open_time=1_000,
            score=80.0,
            threshold=70.0,
            threshold_segment="WD-08",
            session_allowed=True,
            observe_direction="LONG",
        )

        decision = state._maybe_open_order(
            signal,
            Kline(0, 100.0, 100.0, 100.0, 100.0, 1.0, 1_000),
        )

        self.assertEqual(decision, "OPENED")
        self.assertEqual(events, ["bundle", "webhook"])
        self.assertEqual(len(storage.decision_bundles), 1)
        _kind, context, audit, order, observed, entry_snapshot = (
            storage.decision_bundles[0]
        )
        identities = {
            context.decision_id,
            audit.signal.decision_id,
            order.decision_id,
            observed.decision_id,
            entry_snapshot["signal"]["decision_id"],
        }
        self.assertEqual(len(identities), 1)
        self.assertEqual(context.final_decision, "OPENED")
        self.assertTrue(context.open_allowed)
        self.assertEqual(audit.event_kind, "ORDER_OPENED")

    def test_formal_candidate_and_two_observations_persist_three_unique_contexts(self):
        storage = RecordingStorage()
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            enable_rolling_edge_guard=False,
            enable_observation_profile_promotion=False,
            min_order_gap_ms=0,
        )
        now = 119_999
        selected = selected_profile_signal(now, reason="formal")
        first_observation = replace(
            selected,
            direction="WAIT",
            observe_direction="SHORT",
            observe_only=True,
            strategy_family="observe_short",
            strategy_tag="observe_short_tag",
            profile_key="short-profile",
            threshold_segment="WD-02",
            score=-66.0,
        )
        second_observation = replace(
            selected,
            direction="WAIT",
            observe_direction="LONG",
            observe_only=True,
            strategy_family="observe_long",
            strategy_tag="observe_long_tag",
            profile_key="long-profile",
            threshold_segment="WD-15",
            score=66.0,
        )

        with patch("app.state.analyze_volume_price", return_value=selected), patch(
            "app.state.analyze_observation_signals",
            return_value=[first_observation, second_observation],
        ), patch("app.state.choose_trade_signal", return_value=selected):
            self.assertTrue(state.update_from_klines([kline(1, 100.0, 100.0)]))

        contexts = [item[1] for item in storage.decision_bundles]
        audits = [item[2] for item in storage.decision_bundles]
        self.assertEqual(len(contexts), 3)
        self.assertEqual(len({item.decision_id for item in contexts}), 3)
        self.assertEqual(
            {item.decision_id for item in contexts},
            {item.signal.decision_id for item in audits},
        )
        self.assertEqual(
            sorted(item.candidate_origin for item in contexts),
            ["NATIVE_ACTIONABLE", "RESEARCH_OBSERVATION", "RESEARCH_OBSERVATION"],
        )

    def test_primary_observation_uses_decisive_block_audit_for_hold_decision(self):
        storage = RecordingStorage()
        state = MonitorState(symbol="BTCUSDT", storage=storage)
        signal = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="capacity occupied",
            price=100.0,
            open_time=1_000,
            score=80.0,
            threshold=70.0,
            threshold_segment="WD-08",
            session_allowed=True,
            observe_direction="LONG",
        )

        recorded = state._record_observation(
            signal,
            Kline(0, 100.0, 100.0, 100.0, 100.0, 1.0, 1_000),
            "HOLD_OPEN_ORDER",
            candidate_origin="NATIVE_ACTIONABLE",
            candidate_ordinal=0,
            primary_decision=True,
        )

        self.assertTrue(recorded)
        self.assertEqual(storage.decision_bundles[0][2].event_kind, "DECISIVE_BLOCK")

    def test_same_state_replay_reuses_opened_decision_without_second_webhook(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            webhook = RecordingWebhook()
            state = MonitorState(
                symbol="BTCUSDT",
                storage=store,
                webhook=webhook,
                min_order_gap_ms=0,
            )
            signal = replace(
                selected_profile_signal(119_999),
                reason="frozen first reason",
                score=90.0,
            )
            latest = latest_kline(119_999)

            first = state._maybe_open_order(signal, latest)
            first_page = state.snapshot()
            second = state._maybe_open_order(
                replace(signal, reason="recomputed reason", score=95.0),
                latest,
            )
            replay_page = state.snapshot()

            self.assertEqual((first, second), ("OPENED", "OPENED"))
            self.assertEqual(state.selected_signal.reason, "frozen first reason")
            self.assertEqual(state.selected_signal.score, 90.0)
            self.assertEqual(len(state.simulator.orders), 1)
            self.assertEqual(len(store.load_orders("BTCUSDT")), 1)
            self.assertEqual(len(store.load_recent_signals("BTCUSDT")), 1)
            self.assertEqual(len(webhook.calls), 1)
            for key in (
                "selected_signal",
                "rolling_edge",
                "result_sequence_guard",
                "wave_batch_guard",
                "profile_degradation_guard",
                "profile_health_guard",
                "time_period_guard",
            ):
                self.assertEqual(first_page[key], replay_page[key], key)

    def test_two_state_instances_reuse_same_committed_open_decision(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            webhook = RecordingWebhook()
            first_state = MonitorState(
                symbol="BTCUSDT",
                storage=SQLiteMonitorStore(db_path),
                webhook=webhook,
                min_order_gap_ms=0,
            )
            second_state = MonitorState(
                symbol="BTCUSDT",
                storage=SQLiteMonitorStore(db_path),
                webhook=webhook,
                min_order_gap_ms=0,
            )
            signal = replace(
                selected_profile_signal(119_999),
                reason="frozen first reason",
                score=90.0,
            )
            latest = latest_kline(119_999)

            first = first_state._maybe_open_order(signal, latest)
            second = second_state._maybe_open_order(
                replace(signal, reason="second state recomputed", score=94.0),
                latest,
            )

            self.assertEqual((first, second), ("OPENED", "OPENED"))
            self.assertEqual(second_state.selected_signal.reason, "frozen first reason")
            self.assertEqual(second_state.selected_signal.score, 90.0)
            self.assertEqual(len(SQLiteMonitorStore(db_path).load_orders("BTCUSDT")), 1)
            self.assertEqual(len(second_state.simulator.orders), 1)
            self.assertEqual(
                second_state.simulator.orders[0].decision_id,
                first_state.simulator.orders[0].decision_id,
            )
            self.assertEqual(len(webhook.calls), 1)

    def test_replay_identity_never_mixes_frozen_long_with_recomputed_short(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            webhook = RecordingWebhook()
            state = MonitorState(
                symbol="BTCUSDT",
                storage=store,
                webhook=webhook,
                max_open_orders=1,
                min_order_gap_ms=0,
            )
            latest = latest_kline(119_999)
            long_signal = replace(
                selected_profile_signal(119_999),
                direction="LONG",
                observe_direction="LONG",
                reason="frozen long",
                profile_key="",
                daily_profile_selected=False,
                daily_profile_version="",
                threshold_segment="WD-02",
            )
            short_signal = replace(
                long_signal,
                direction="SHORT",
                observe_direction="SHORT",
                reason="new short",
                score=-90.0,
            )

            opened = state._maybe_open_order(long_signal, latest)
            replayed = state._maybe_open_order(
                replace(long_signal, reason="recomputed long", score=95.0),
                latest,
            )
            blocked_short = state._maybe_open_order(short_signal, latest)

            self.assertEqual(opened, "OPENED")
            self.assertEqual(replayed, "OPENED")
            self.assertEqual(blocked_short, "HOLD_OPEN_ORDER")
            self.assertEqual(len(webhook.calls), 1)
            self.assertEqual(state.selected_signal.direction, "SHORT")
            self.assertEqual(state.selected_signal.reason, "new short")
            orders = store.load_orders("BTCUSDT")
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0].direction, "LONG")
            with closing(sqlite3.connect(db_path)) as connection:
                contexts = connection.execute(
                    "select direction, profile_key, input_payload from decision_contexts "
                    "order by created_at_ms, decision_id"
                ).fetchall()
                audits = connection.execute(
                    "select direction, decision_id from signal_audit order by id"
                ).fetchall()
            self.assertEqual({row[0] for row in contexts}, {"LONG", "SHORT"})
            self.assertTrue(all(row[1] == "" for row in contexts))
            self.assertEqual({row[0] for row in audits}, {"LONG", "SHORT"})
            for direction, _profile_key, payload in contexts:
                self.assertEqual(json.loads(payload)["identity"]["direction"], direction)

    def test_same_blocked_candidate_replay_reuses_stable_decision(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            state = MonitorState(symbol="BTCUSDT", storage=store)
            signal = replace(
                selected_profile_signal(119_999),
                direction="WAIT",
                session_allowed=False,
                daily_profile_selected=False,
            )
            latest = latest_kline(119_999)

            first = state._maybe_open_order(signal, latest)
            second = state._maybe_open_order(signal, latest)

            self.assertEqual((first, second), ("SESSION_BLOCKED", "SESSION_BLOCKED"))
            recent = store.load_recent_signals("BTCUSDT")
            self.assertEqual(len(recent), 1)
            self.assertEqual(recent[0]["created_at_ms"], latest.close_time)

    def test_daily_profile_block_replay_restores_all_visible_decision_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            state = MonitorState(symbol="BTCUSDT", storage=store)
            signal = replace(
                selected_profile_signal(119_999, reason="not selected today"),
                daily_profile_selected=False,
            )
            latest = latest_kline(119_999)

            first = state._maybe_open_order(
                signal,
                latest,
                daily_profile_required=True,
            )
            first_page = state.snapshot()
            replay = state._maybe_open_order(
                replace(signal, reason="recomputed blocked reason"),
                latest,
                daily_profile_required=True,
            )
            replay_page = state.snapshot()

            self.assertEqual((first, replay), (
                "DAILY_PROFILE_NOT_SELECTED",
                "DAILY_PROFILE_NOT_SELECTED",
            ))
            visible_keys = (
                "risk_pause",
                "rolling_edge",
                "result_sequence_guard",
                "wave_batch_guard",
                "profile_degradation_guard",
                "profile_health_guard",
                "time_period_guard",
                "selected_signal",
            )
            self.assertEqual(
                {key: first_page[key] for key in visible_keys},
                {key: replay_page[key] for key in visible_keys},
            )

    def test_same_kline_opposite_directions_create_independent_decisions(self):
        with managed_sqlite_states() as (_db_path, store, states):
            webhook = RecordingWebhook()
            state = MonitorState(
                symbol="BTCUSDT",
                storage=store,
                webhook=webhook,
                max_open_orders=2,
                max_open_long_orders=1,
                max_open_short_orders=1,
                min_order_gap_ms=0,
            )
            states.append(state)
            latest = latest_kline(119_999)
            long_signal = replace(
                selected_profile_signal(119_999),
                daily_profile_selected=False,
                daily_profile_version="",
                profile_key="",
                threshold_segment="WD-02",
            )
            short_signal = replace(
                long_signal,
                direction="SHORT",
                observe_direction="SHORT",
                score=-90.0,
                reason="same kline short",
            )

            decisions = (
                state._maybe_open_order(long_signal, latest),
                state._maybe_open_order(short_signal, latest),
            )

            self.assertEqual(decisions, ("OPENED", "OPENED"))
            orders = store.load_orders("BTCUSDT")
            self.assertEqual({order.direction for order in orders}, {"LONG", "SHORT"})
            self.assertEqual(len({order.decision_id for order in orders}), 2)
            self.assertEqual(len(webhook.calls), 2)

    def test_failed_real_bundle_does_not_fall_back_to_orphan_signal_audit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            state = MonitorState(
                symbol="BTCUSDT",
                storage=store,
                enable_rolling_edge_guard=False,
                enable_observation_profile_promotion=False,
                min_order_gap_ms=0,
            )
            selected = selected_profile_signal(119_999)

            def fail_after_order(step):
                if step == "order":
                    raise RuntimeError("injected bundle failure")

            with patch.object(
                store,
                "_after_bundle_step",
                side_effect=fail_after_order,
            ), patch("app.state.analyze_volume_price", return_value=selected), patch(
                "app.state.analyze_observation_signals",
                return_value=[
                    replace(
                        selected,
                        direction="WAIT",
                        observe_direction="SHORT",
                        observe_only=True,
                        strategy_family="failed_round_observation",
                        strategy_tag="failed_round_observation",
                        profile_key="failed-round-observation",
                    )
                ],
            ), patch("app.state.choose_trade_signal", return_value=selected):
                updated = state.update_from_klines([kline(1, 100.0, 100.0)])
            state.wait_for_storage_writes()

            self.assertFalse(updated)
            with closing(sqlite3.connect(db_path)) as connection:
                counts = {
                    table: connection.execute(
                        f"select count(*) from {table}"
                    ).fetchone()[0]
                    for table in (
                        "runtime_config_snapshots",
                        "decision_contexts",
                        "orders",
                        "order_entry_snapshots",
                        "signal_audit",
                        "observation_signals",
                    )
                }
            self.assertEqual(counts, {table: 0 for table in counts})

    def test_collector_duplicate_observation_preserves_first_source_in_memory_and_db(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            state = MonitorState(symbol="BTCUSDT", storage=store)
            signal = selected_profile_signal(119_999)
            latest = latest_kline(119_999)
            state._observation_audit_collector = []
            try:
                first = state._record_observation(
                    signal,
                    latest,
                    "SESSION_BLOCKED",
                )
                second = state._record_observation(
                    signal,
                    latest,
                    "PROFILE_HEALTH_BLOCKED",
                )
            finally:
                state._observation_audit_collector = None

            self.assertTrue(first)
            self.assertFalse(second)
            self.assertEqual(state.observations[0].source_decision, "SESSION_BLOCKED")
            self.assertEqual(
                store.load_observations("BTCUSDT")[0].source_decision,
                "SESSION_BLOCKED",
            )

    def test_build_id_changes_config_and_decision_identity_but_same_build_is_stable(self):
        latest = latest_kline(119_999)
        signal = selected_profile_signal(119_999)
        first = MonitorState(symbol="BTCUSDT", strategy_build_id="build-a")
        same = MonitorState(symbol="BTCUSDT", strategy_build_id="build-a")
        changed = MonitorState(symbol="BTCUSDT", strategy_build_id="build-b")

        first_artifacts = first._decision_artifacts(
            signal,
            latest,
            "OPENED",
            final_reason=signal.reason,
            candidate_origin="NATIVE_ACTIONABLE",
            candidate_ordinal=0,
            observation_allowed=False,
            audit_context={},
            event_kind="ORDER_OPENED",
        )
        same_artifacts = same._decision_artifacts(
            signal,
            latest,
            "OPENED",
            final_reason=signal.reason,
            candidate_origin="NATIVE_ACTIONABLE",
            candidate_ordinal=0,
            observation_allowed=False,
            audit_context={},
            event_kind="ORDER_OPENED",
        )
        changed_artifacts = changed._decision_artifacts(
            signal,
            latest,
            "OPENED",
            final_reason=signal.reason,
            candidate_origin="NATIVE_ACTIONABLE",
            candidate_ordinal=0,
            observation_allowed=False,
            audit_context={},
            event_kind="ORDER_OPENED",
        )

        self.assertEqual(first_artifacts[0].hash, same_artifacts[0].hash)
        self.assertEqual(first_artifacts[1].decision_id, same_artifacts[1].decision_id)
        self.assertNotEqual(first_artifacts[0].hash, changed_artifacts[0].hash)
        self.assertNotEqual(
            first_artifacts[1].decision_id,
            changed_artifacts[1].decision_id,
        )
        self.assertEqual(first_artifacts[3].created_at_ms, latest.close_time)
        self.assertEqual(first_artifacts[0].strategy_build_id, "build-a")

    def test_real_sqlite_candidate_lookup_distinguishes_empty_profile_and_ordinal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            state = MonitorState(symbol="BTCUSDT", storage=store)
            latest = latest_kline(119_999)
            signal = replace(
                selected_profile_signal(119_999),
                profile_key="",
                daily_profile_selected=False,
                daily_profile_version="",
            )
            contexts = []
            for ordinal in (0, 1):
                config, context, _enriched, audit = state._decision_artifacts(
                    signal,
                    latest,
                    "RESEARCH_OBSERVE",
                    final_reason=signal.reason,
                    candidate_origin="RESEARCH_OBSERVATION",
                    candidate_ordinal=ordinal,
                    observation_allowed=False,
                    audit_context={},
                    event_kind="OBSERVATION_CANDIDATE",
                )
                store.save_decision_bundle(
                    config=config,
                    context=context,
                    audit=audit,
                )
                contexts.append(context)

            first = store.load_decision_context_for_candidate(
                "BTCUSDT",
                closed_kline_at_ms=latest.close_time,
                candidate_origin="RESEARCH_OBSERVATION",
                profile_key="",
                runtime_config_hash=contexts[0].runtime_config_hash,
                strategy_build_id=contexts[0].strategy_build_id,
                candidate_identity=state._candidate_identity(
                    signal,
                    candidate_ordinal=0,
                    candidate_origin="RESEARCH_OBSERVATION",
                    closed_kline_at_ms=latest.close_time,
                ),
            )
            second = store.load_decision_context_for_candidate(
                "BTCUSDT",
                closed_kline_at_ms=latest.close_time,
                candidate_origin="RESEARCH_OBSERVATION",
                profile_key="",
                runtime_config_hash=contexts[1].runtime_config_hash,
                strategy_build_id=contexts[1].strategy_build_id,
                candidate_identity=state._candidate_identity(
                    signal,
                    candidate_ordinal=1,
                    candidate_origin="RESEARCH_OBSERVATION",
                    closed_kline_at_ms=latest.close_time,
                ),
            )

            self.assertNotEqual(contexts[0].decision_id, contexts[1].decision_id)
            self.assertEqual(first["decision_id"], contexts[0].decision_id)
            self.assertEqual(second["decision_id"], contexts[1].decision_id)
            self.assertEqual(first["inputs"]["identity"]["candidate_ordinal"], 0)
            self.assertEqual(second["inputs"]["identity"]["candidate_ordinal"], 1)

    def test_candidate_slot_identity_uses_order_lifecycle_at_closed_kline(self):
        state = MonitorState(symbol="BTCUSDT")
        state.simulator.orders.append(
            SimulatedOrder(
                id=1,
                direction="LONG",
                timeframe_minutes=10,
                level="A",
                reason="historical open order",
                entry_price=100.0,
                opened_at=1_000,
                expires_at=200_000,
                status="SETTLED",
                result="WIN",
                settled_at=200_000,
                exit_price=101.0,
                pnl=8.0,
            )
        )
        signal = replace(selected_profile_signal(119_999), direction="LONG")

        while_open = state._candidate_identity(
            signal,
            candidate_ordinal=0,
            closed_kline_at_ms=119_999,
        )
        after_settlement = state._candidate_identity(
            signal,
            candidate_ordinal=0,
            closed_kline_at_ms=300_000,
        )

        self.assertEqual(while_open["order_slot"], "SECOND")
        self.assertEqual(after_settlement["order_slot"], "FIRST")

    def test_profile_promoted_wait_origin_reaches_open_and_blocked_bundles(self):
        for blocked in (False, True):
            with self.subTest(blocked=blocked), managed_sqlite_states() as (
                _db_path,
                store,
                states,
            ):
                state = MonitorState(
                    symbol="BTCUSDT",
                    storage=store,
                    enable_daily_profile_selector=True,
                    max_open_orders=1,
                    min_order_gap_ms=0,
                )
                states.append(state)
                state.active_daily_profile_selection = {
                    "version": PROFILE_VERSION,
                    "status": "READY",
                    "selected_profiles": [
                        {
                            "key": PROFILE_KEY,
                            "sample_size": 20,
                            "win_rate": 0.7,
                            "ev": 2.6,
                        }
                    ],
                }
                wait_candidate = replace(
                    selected_profile_signal(119_999),
                    direction="WAIT",
                    score=60.0,
                    session_allowed=False,
                    daily_profile_selected=False,
                    daily_profile_version="",
                    profile_key="",
                    observe_only=True,
                )
                primary = replace(wait_candidate, strategy_tag="not-selected")
                promoted, required = state._select_daily_profile_signal(
                    primary,
                    [wait_candidate],
                    119_999,
                )
                if blocked:
                    state.simulator.orders.append(
                        SimulatedOrder(
                            id=99,
                            direction="SHORT",
                            timeframe_minutes=10,
                            level="A",
                            reason="existing",
                            entry_price=100.0,
                            opened_at=1,
                            expires_at=999_999,
                        )
                    )

                decision = state._maybe_open_order(
                    promoted,
                    latest_kline(119_999),
                    daily_profile_required=required,
                )

                expected = "HOLD_OPEN_ORDER" if blocked else "OPENED"
                self.assertEqual(decision, expected)
                context = store.load_decision_context(
                    "BTCUSDT",
                    state.selected_signal.decision_id,
                )
                self.assertEqual(context["candidate_origin"], "PROFILE_PROMOTED_WAIT")
                self.assertEqual(
                    state.selected_signal.candidate_origin,
                    "PROFILE_PROMOTED_WAIT",
                )
                if not blocked:
                    order = store.load_orders("BTCUSDT")[0]
                    self.assertEqual(order.candidate_origin, "PROFILE_PROMOTED_WAIT")
                observation = store.load_observations("BTCUSDT")[0]
                self.assertEqual(
                    observation.candidate_origin,
                    "PROFILE_PROMOTED_WAIT",
                )

    def test_open_path_loads_profile_summary_only_once_per_decision(self):
        class CountingStore(SQLiteMonitorStore):
            def __init__(self, path):
                self.profile_computes = 0
                super().__init__(path)

            def _compute_profile_summary(self, key, samples):
                self.profile_computes += 1
                return super()._compute_profile_summary(key, samples)

        with tempfile.TemporaryDirectory() as temp_dir:
            store = CountingStore(Path(temp_dir) / "monitor.sqlite3")
            state = MonitorState(
                symbol="BTCUSDT",
                storage=store,
                enable_profile_guard=True,
                min_order_gap_ms=0,
            )
            store.wait_for_profile_summary_rebuilds(timeout=10)
            self.assertEqual(store.profile_computes, 1)
            store.profile_computes = 0

            decision = state._maybe_open_order(
                selected_profile_signal(119_999),
                latest_kline(119_999),
            )

            self.assertEqual(decision, "OPENED")
            self.assertLessEqual(store.profile_computes, 1)

    def test_5000_snapshot_profile_summary_is_precomputed_off_open_hot_path(self):
        events = []

        class CountingStore(SQLiteMonitorStore):
            def __init__(self, path):
                self.snapshot_loads = 0
                super().__init__(path)

            def _profile_summary_rebuild_input(self, key):
                self.snapshot_loads += 1
                return super()._profile_summary_rebuild_input(key)

            def save_open_order_decision(self, **kwargs):
                result = super().save_open_order_decision(**kwargs)
                events.append("commit")
                return result

        class OrderedWebhook(RecordingWebhook):
            def send_signal(self, symbol, signal, message=None, amount=None):
                events.append("webhook")
                return super().send_signal(
                    symbol,
                    signal,
                    message=message,
                    amount=amount,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = CountingStore(db_path)
            signal_payload = selected_profile_signal(1_000).to_dict()
            entry_payload = json.dumps(
                {
                    "signal": signal_payload,
                    "fear_greed": {"value": 50, "trend": "flat"},
                    "profile_guard_shadow": {"status": "PASS"},
                },
                ensure_ascii=False,
            )
            with closing(sqlite3.connect(db_path)) as connection:
                connection.executemany(
                    """
                    insert into order_entry_snapshots(
                        symbol, order_id, direction, timeframe_minutes,
                        opened_at, expires_at, entry_price, stake, win_return,
                        stake_progression_step, threshold_segment, regime,
                        score, threshold, edge, result, settled_at, exit_price,
                        pnl, entry_payload
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            "BTCUSDT",
                            10_000 + index,
                            "LONG",
                            10,
                            index * 600_000,
                            (index + 1) * 600_000,
                            100.0,
                            10.0,
                            18.0,
                            1,
                            "WD-08",
                            "FEAR_FLAT",
                            90.0,
                            70.0,
                            20.0,
                            (
                                None
                                if index == 4_999
                                else "WIN" if index % 2 == 0 else "LOSS"
                            ),
                            None if index == 4_999 else (index + 1) * 600_000,
                            (
                                None
                                if index == 4_999
                                else 101.0 if index % 2 == 0 else 99.0
                            ),
                            (
                                0.0
                                if index == 4_999
                                else 8.0 if index % 2 == 0 else -10.0
                            ),
                            entry_payload,
                        )
                        for index in range(5_000)
                    ),
                )
                connection.commit()

            state = MonitorState(
                symbol="BTCUSDT",
                storage=store,
                webhook=OrderedWebhook(),
                enable_profile_guard=False,
                min_order_gap_ms=0,
            )
            enabled_state = MonitorState(
                symbol="BTCUSDT",
                storage=store,
                enable_profile_guard=True,
            )
            store.wait_for_profile_summary_rebuilds(timeout=20)
            self.assertEqual(store.snapshot_loads, 1)
            store.snapshot_loads = 0

            disabled_source = state._profile_guard_shadow_source()
            enabled_source = enabled_state._profile_guard_shadow_source()
            settling = SimulatedOrder(
                id=14_999,
                direction="LONG",
                timeframe_minutes=10,
                level="A",
                reason="settled before immediate open",
                entry_price=100.0,
                opened_at=4_999 * 600_000,
                expires_at=5_000 * 600_000,
                threshold_segment="WD-08",
                score=90.0,
                threshold=70.0,
                status="SETTLED",
                result="LOSS",
                settled_at=5_000 * 600_000,
                exit_price=99.0,
                pnl=-10.0,
            )
            started = time.perf_counter()
            store.update_order_entry_snapshot_settlement(settling, "BTCUSDT")
            decision = state._maybe_open_order(
                selected_profile_signal(3_100_000_000),
                latest_kline(3_100_000_000),
            )
            elapsed = time.perf_counter() - started

            self.assertEqual(disabled_source["snapshot_count"], 5_000)
            self.assertEqual(enabled_source["snapshot_count"], 5_000)
            self.assertEqual(disabled_source["total"]["orders"], 4_999)
            self.assertEqual(disabled_source["total"]["wins"], 2_500)
            self.assertEqual(decision, "OPENED")
            self.assertLess(elapsed, 2.0)
            self.assertEqual(events, ["commit", "webhook"])
            store.wait_for_profile_summary_rebuilds(timeout=20)
            materialized = store.profile_summary_snapshot("BTCUSDT")
            snapshots = store.load_order_entry_snapshots("BTCUSDT", limit=5_000)
            expected = summarize_order_samples_with_guard(
                [sample_from_entry_snapshot(item) for item in reversed(snapshots)],
                profile_guard_min_history=15,
                profile_guard_min_group_size=2,
            )
            ignored = {
                "generated_at",
                "elapsed_seconds",
                "cache_status",
                "source_revision",
                "current_revision",
                "stale",
            }
            self.assertEqual(
                {key: value for key, value in materialized.items() if key not in ignored},
                {key: value for key, value in expected.items() if key not in ignored},
            )

    def test_formal_guard_5000_settlement_to_committed_open_stays_off_full_scan(self):
        events = []

        class TrackingStore(SQLiteMonitorStore):
            def __init__(self, path):
                self.load_threads = []
                self.summarize_threads = []
                self.exact_results = []
                super().__init__(path)

            def _profile_summary_rebuild_input(self, key):
                self.load_threads.append(threading.get_ident())
                return super()._profile_summary_rebuild_input(key)

            def _compute_profile_summary(self, key, samples):
                self.summarize_threads.append(threading.get_ident())
                return super()._compute_profile_summary(key, samples)

            def exact_order_profile_summary(self, symbol, **kwargs):
                result = super().exact_order_profile_summary(symbol, **kwargs)
                self.exact_results.append(json.loads(json.dumps(result)))
                return result

            def save_open_order_decision(self, **kwargs):
                result = super().save_open_order_decision(**kwargs)
                events.append("commit")
                return result

        class OrderedWebhook(RecordingWebhook):
            def send_signal(self, symbol, signal, message=None, amount=None):
                events.append("webhook")
                return super().send_signal(
                    symbol,
                    signal,
                    message=message,
                    amount=amount,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = TrackingStore(db_path)
            state = None
            webhook = OrderedWebhook()
            signal_payload = selected_profile_signal(1_000).to_dict()
            entry_payload = json.dumps(
                {
                    "signal": signal_payload,
                    "fear_greed": {"value": 50, "trend": "flat"},
                    "profile_guard_shadow": {"status": "PASS"},
                },
                ensure_ascii=False,
            )
            try:
                with closing(sqlite3.connect(db_path)) as connection:
                    connection.executemany(
                        """
                        insert into order_entry_snapshots(
                            symbol, order_id, direction, timeframe_minutes,
                            opened_at, expires_at, entry_price, stake, win_return,
                            stake_progression_step, threshold_segment, regime,
                            score, threshold, edge, result, settled_at, exit_price,
                            pnl, entry_payload
                        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            (
                                "BTCUSDT",
                                10_000 + index,
                                "LONG",
                                10,
                                index * 600_000,
                                (index + 1) * 600_000,
                                100.0,
                                10.0,
                                18.0,
                                1,
                                "WD-08",
                                "FEAR_FLAT",
                                90.0,
                                70.0,
                                20.0,
                                None if index == 4_999 else "WIN",
                                None if index == 4_999 else (index + 1) * 600_000,
                                None if index == 4_999 else 101.0,
                                0.0 if index == 4_999 else 8.0,
                                entry_payload,
                            )
                            for index in range(5_000)
                        ),
                    )
                    connection.commit()

                open_order = SimulatedOrder(
                    id=14_999,
                    direction="LONG",
                    timeframe_minutes=10,
                    level="A",
                    reason="formal guard 5000 settlement",
                    entry_price=100.0,
                    opened_at=4_999 * 600_000,
                    expires_at=5_000 * 600_000,
                    threshold_segment="WD-08",
                    score=90.0,
                    threshold=70.0,
                    regime="FEAR_FLAT",
                    strategy_family="drop_reclaim",
                    strategy_tag="live_profile",
                    profile_key=PROFILE_KEY,
                    daily_profile_selected=True,
                    daily_profile_version=PROFILE_VERSION,
                    order_slot="FIRST",
                    order_slot_scope="DIRECTION_V2",
                )
                store.save_order(open_order, "BTCUSDT")
                state = MonitorState(
                    symbol="BTCUSDT",
                    storage=store,
                    webhook=webhook,
                    enable_profile_guard=True,
                    min_order_gap_ms=0,
                )
                store.wait_for_profile_summary_rebuilds(timeout=30)

                key = store._profile_summary_key("BTCUSDT", 5_000, 15, 2)
                _revision, samples = store._profile_summary_rebuild_input(key)
                expected_samples = [dict(sample) for sample in samples]
                expected_samples[-1].update(
                    {
                        "result": "WIN",
                        "pnl": 8.0,
                        "settled_at": open_order.expires_at,
                        "exit_price": 101.0,
                    }
                )
                expected_guard = summarize_order_samples_with_guard(
                    expected_samples,
                    profile_guard_min_history=15,
                    profile_guard_min_group_size=2,
                )["profile_guard"]
                store.load_threads.clear()
                store.summarize_threads.clear()
                trading_thread = threading.get_ident()
                original_full_summary = (
                    storage_module.summarize_order_samples_with_guard
                )
                original_guard_summary = (
                    storage_module.summarize_profile_guard_materialization
                )

                def tracked_full_summary(*args, **kwargs):
                    store.summarize_threads.append(threading.get_ident())
                    return original_full_summary(*args, **kwargs)

                def tracked_guard_summary(*args, **kwargs):
                    store.summarize_threads.append(threading.get_ident())
                    return original_guard_summary(*args, **kwargs)

                started = time.perf_counter()
                with (
                    patch.object(
                        storage_module,
                        "summarize_order_samples_with_guard",
                        side_effect=tracked_full_summary,
                    ),
                    patch.object(
                        storage_module,
                        "summarize_profile_guard_materialization",
                        side_effect=tracked_guard_summary,
                    ),
                ):
                    settlement_events = (
                        state.simulator.settle_expired_order_events(
                            open_order.expires_at,
                            101.0,
                        )
                    )
                    self.assertEqual(len(settlement_events), 1)
                    state._pending_settlement_events.extend(
                        ("BTCUSDT", event) for event in settlement_events
                    )
                    self.assertTrue(
                        state._flush_pending_settlement_events(),
                        state.last_error,
                    )
                    decision = state._maybe_open_order(
                        selected_profile_signal(3_100_000_000),
                        latest_kline(3_100_000_000),
                    )
                elapsed = time.perf_counter() - started

                self.assertEqual(decision, "OPENED")
                self.assertNotIn(trading_thread, store.load_threads)
                self.assertNotIn(trading_thread, store.summarize_threads)
                self.assertTrue(store.exact_results)
                exact = store.exact_results[-1]
                self.assertEqual(exact["profile_guard"], expected_guard)
                self.assertEqual(exact["source_revision"], 1)
                self.assertEqual(exact["current_revision"], 1)
                self.assertFalse(exact["stale"])
                audited = state.selected_signal.decision_inputs[
                    "audit_snapshot"
                ]["profile_guard"]
                self.assertEqual(audited["source_revision"], 1)
                self.assertEqual(audited["current_revision"], 1)
                self.assertFalse(audited["stale"])
                self.assertEqual(store.profile_summary_revision("BTCUSDT"), 2)
                self.assertEqual(events, ["commit", "webhook"])
                self.assertEqual(len(webhook.calls), 1)
                orders = store.load_orders("BTCUSDT")
                self.assertEqual(len(orders), 2)
                self.assertEqual(sum(order.status == "OPEN" for order in orders), 1)
                self.assertLess(elapsed, 2.0)
            finally:
                store.wait_for_profile_summary_rebuilds(timeout=60)
                if state is not None:
                    state.close()
                else:
                    store.close()

    def test_profile_summary_cache_refreshes_on_snapshot_changes_and_is_thread_safe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            signal = selected_profile_signal(1_000)
            first = SimulatedOrder(
                id=1,
                direction="LONG",
                timeframe_minutes=10,
                level="A",
                reason="first",
                entry_price=100.0,
                opened_at=1_000,
                expires_at=601_000,
                threshold_segment="WD-08",
                score=90.0,
                threshold=70.0,
            )
            store.save_order_entry_snapshot(
                first,
                "BTCUSDT",
                {"signal": signal.to_dict()},
            )
            store.prepare_order_profile_summary(
                "BTCUSDT",
                profile_guard_min_history=15,
                profile_guard_min_group_size=2,
            )
            store.wait_for_profile_summary_rebuilds(timeout=10)
            initial = store.profile_summary_snapshot(
                "BTCUSDT",
                profile_guard_min_history=15,
                profile_guard_min_group_size=2,
            )
            self.assertEqual(initial["snapshot_count"], 1)
            self.assertEqual(initial["open_orders"], 1)

            second = replace(first, id=2, opened_at=2_000, expires_at=602_000)
            store.save_order_entry_snapshot(
                second,
                "BTCUSDT",
                {"signal": replace(signal, open_time=2_000).to_dict()},
            )
            store.wait_for_profile_summary_rebuilds(timeout=10)
            after_insert = store.profile_summary_snapshot(
                "BTCUSDT",
                profile_guard_min_history=15,
                profile_guard_min_group_size=2,
            )
            self.assertEqual(after_insert["snapshot_count"], 2)
            self.assertEqual(after_insert["open_orders"], 2)

            errors = []
            snapshots = []
            barrier = threading.Barrier(6)

            def read_cache():
                try:
                    barrier.wait(timeout=5)
                    for _ in range(50):
                        snapshots.append(
                            store.profile_summary_snapshot(
                                "BTCUSDT",
                                profile_guard_min_history=15,
                                profile_guard_min_group_size=2,
                            )
                        )
                except Exception as exc:  # noqa: BLE001 - captures concurrent failure.
                    errors.append(exc)

            threads = [threading.Thread(target=read_cache) for _ in range(5)]
            for thread in threads:
                thread.start()
            barrier.wait(timeout=5)
            settled_second = replace(
                second,
                status="SETTLED",
                result="WIN",
                settled_at=second.expires_at,
                exit_price=101.0,
                pnl=8.0,
            )
            store.update_order_entry_snapshot_settlement(
                settled_second,
                "BTCUSDT",
            )
            for thread in threads:
                thread.join(timeout=5)
            store.wait_for_profile_summary_rebuilds(timeout=10)

            final = store.profile_summary_snapshot(
                "BTCUSDT",
                profile_guard_min_history=15,
                profile_guard_min_group_size=2,
            )
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertTrue(snapshots)
            self.assertTrue(all(item["snapshot_count"] == 2 for item in snapshots))
            self.assertEqual(final["open_orders"], 1)
            self.assertEqual(final["total"]["orders"], 1)
            self.assertEqual(final["total"]["wins"], 1)

    def test_reset_symbol_requests_profile_summary_for_new_symbol(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            eth_order = SimulatedOrder(
                id=1,
                direction="LONG",
                timeframe_minutes=10,
                level="A",
                reason="eth profile",
                entry_price=100.0,
                opened_at=1_000,
                expires_at=601_000,
            )
            store.save_order_entry_snapshot(
                eth_order,
                "ETHUSDT",
                {"signal": selected_profile_signal(1_000).to_dict()},
            )
            state = MonitorState(symbol="BTCUSDT", storage=store)

            state.reset_symbol("ETHUSDT")
            store.wait_for_profile_summary_rebuilds(timeout=10)
            summary = store.profile_summary_snapshot("ETHUSDT")

            self.assertEqual(summary["cache_status"], "READY")
            self.assertEqual(summary["snapshot_count"], 1)

    def test_failed_initial_profile_prewarm_retries_on_next_read(self):
        class FailOnceStore(SQLiteMonitorStore):
            def __init__(self, path):
                self.compute_attempts = 0
                super().__init__(path)

            def _compute_profile_summary(self, key, samples):
                self.compute_attempts += 1
                if self.compute_attempts == 1:
                    raise RuntimeError("first prewarm failed")
                return super()._compute_profile_summary(key, samples)

        with tempfile.TemporaryDirectory() as temp_dir:
            store = FailOnceStore(Path(temp_dir) / "monitor.sqlite3")
            state = MonitorState(symbol="BTCUSDT", storage=store)
            store.wait_for_profile_summary_rebuilds(timeout=10)

            first_page = state.order_profile_summary()
            store.wait_for_profile_summary_rebuilds(timeout=10)
            recovered_page = state.order_profile_summary()

            self.assertIn(first_page["cache_status"], {"PREPARING", "STALE"})
            self.assertEqual(recovered_page["cache_status"], "READY")
            self.assertGreaterEqual(store.compute_attempts, 2)

    def test_stale_profile_summary_uses_exact_fallback_when_guard_enabled(self):
        class StaleProfileStorage(RecordingStorage):
            def __init__(self):
                super().__init__()
                self.exact_calls = 0

            def profile_summary_snapshot(self, symbol, **kwargs):
                return {
                    "cache_status": "STALE",
                    "source_revision": 4,
                    "current_revision": 5,
                    "stale": True,
                    "profile_guard": {},
                }

            def exact_order_profile_summary(self, symbol, **kwargs):
                self.exact_calls += 1
                return {
                    "cache_status": "READY",
                    "source_revision": 5,
                    "current_revision": 5,
                    "stale": False,
                    "profile_guard": {
                        "walk_forward_combined": {
                            "risk_keys": [
                                "HIGH_RSI_REBOUND",
                                "WEAK_SEGMENT_WD00_WD18_WD22",
                            ],
                            "min_history": 15,
                            "min_group_size": 2,
                            "traded": {"orders": 28},
                            "blocked": {"orders": 21},
                        }
                    },
                }

        storage = StaleProfileStorage()
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            enable_profile_guard=True,
        )
        signal = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="放量急跌反抽：stale exact fallback",
            price=100.0,
            open_time=4_200_000,
            score=90.0,
            threshold=70.0,
            threshold_segment="WD-18",
            session_allowed=True,
            price_change_pct=-0.0015,
            price_position=0.45,
            rsi=46.0,
            mtf_10m_bias=0.1,
            mtf_30m_bias=0.2,
            observe_direction="LONG",
        )

        decision = state._maybe_open_order(signal, kline(70, 100.0, 100.0))

        self.assertEqual(decision, "PROFILE_GUARD_BLOCKED")
        self.assertEqual(storage.exact_calls, 1)
        self.assertFalse(state.profile_guard_audit["stale"])
        self.assertEqual(state.profile_guard_audit["source_revision"], 5)

    def test_shadow_mode_audits_stale_profile_revision_without_blocking(self):
        class StaleProfileStorage(RecordingStorage):
            def profile_summary_snapshot(self, symbol, **kwargs):
                return {
                    "cache_status": "STALE",
                    "source_revision": 7,
                    "current_revision": 8,
                    "stale": True,
                    "profile_guard": {},
                }

        state = MonitorState(
            symbol="BTCUSDT",
            storage=StaleProfileStorage(),
            enable_profile_guard=False,
            min_order_gap_ms=0,
        )

        decision = state._maybe_open_order(
            selected_profile_signal(119_999),
            latest_kline(119_999),
        )
        audited = state.selected_signal.decision_inputs["audit_snapshot"]["profile_guard"]

        self.assertEqual(decision, "OPENED")
        self.assertEqual(audited["source_revision"], 7)
        self.assertEqual(audited["current_revision"], 8)
        self.assertTrue(audited["stale"])

    def test_observation_settlement_failure_restores_open_state_for_retry(self):
        class RetryStorage(RecordingStorage):
            def __init__(self):
                super().__init__()
                self.fail_settlement = True

            def save_observations(self, observations, symbol):
                if self.fail_settlement:
                    self.fail_settlement = False
                    raise OSError("settlement unavailable")
                for item in observations:
                    self.save_observation(item, symbol)

        storage = RetryStorage()
        state = MonitorState(symbol="BTCUSDT", storage=storage)
        pending = ObservationSignal(
            observation_key="retry-observation",
            strategy_family="observe",
            strategy_tag="retry",
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="retry settlement",
            entry_price=100.0,
            opened_at=1_000,
            expires_at=601_000,
        )
        state.observations = [pending]

        first = state._settle_observations(601_000, 101.0)
        second = state._settle_observations(601_000, 101.0)

        self.assertEqual(first, [])
        self.assertEqual(len(second), 1)
        self.assertEqual(pending.status, "SETTLED")
        self.assertEqual(pending.result, "WIN")
        self.assertEqual(len(storage.observations), 1)

    def test_real_sqlite_observation_settlement_failure_restores_for_retry(self):
        class FailOnceSettlementStore(SQLiteMonitorStore):
            def __init__(self, path):
                super().__init__(path)
                self.fail_settlement_once = True

            def _update_observation_settlement(self, connection, observation, symbol):
                if self.fail_settlement_once:
                    self.fail_settlement_once = False
                    raise sqlite3.OperationalError("injected observation settlement failure")
                return super()._update_observation_settlement(
                    connection,
                    observation,
                    symbol,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            store = FailOnceSettlementStore(Path(temp_dir) / "monitor.sqlite3")
            pending = ObservationSignal(
                observation_key="real-sqlite-retry",
                strategy_family="observe",
                strategy_tag="retry",
                direction="LONG",
                timeframe_minutes=10,
                level="A",
                reason="retry settlement",
                entry_price=100.0,
                opened_at=1_000,
                expires_at=601_000,
                profile_key="profile-retry",
                candidate_origin="RESEARCH_OBSERVATION",
            )
            store.save_observation(pending, "BTCUSDT")
            state = MonitorState(symbol="BTCUSDT", storage=store)

            first = state._settle_observations(601_000, 101.0)
            after_failure = store.load_observations("BTCUSDT")[0]
            memory_after_failure = replace(state.observations[0])
            second = state._settle_observations(601_000, 101.0)

            self.assertEqual(first, [])
            self.assertEqual(after_failure.status, "OPEN")
            self.assertEqual(memory_after_failure.status, "OPEN")
            self.assertEqual(len(second), 1)
            self.assertEqual(store.load_observations("BTCUSDT")[0].status, "SETTLED")

    def test_entry_snapshot_profile_context_is_captured_before_webhook(self):
        events = []

        class OrderingStorage(RecordingStorage):
            def order_profile_summary(self, symbol, **kwargs):
                events.append("profile_summary")
                return None

        class OrderingWebhook(RecordingWebhook):
            def send_signal(self, symbol, signal, message=None, amount=None):
                events.append("webhook")
                super().send_signal(symbol, signal, message=message, amount=amount)

        state = MonitorState(
            symbol="BTCUSDT",
            storage=OrderingStorage(),
            webhook=OrderingWebhook(),
            now_ms=lambda: 1_000,
        )
        signal = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="fast webhook",
            price=100.0,
            open_time=1_000,
            score=80.0,
            threshold=70.0,
            threshold_segment="WD-08",
            session_allowed=True,
            observe_direction="LONG",
        )

        decision = state._maybe_open_order(
            signal,
            Kline(0, 100.0, 100.0, 100.0, 100.0, 1.0, 1_000),
        )

        self.assertEqual(decision, "OPENED")
        self.assertEqual(events, ["profile_summary", "webhook"])

    def test_webhook_dispatch_failure_does_not_interrupt_committed_order(self):
        class FailingWebhook(RecordingWebhook):
            def send_signal(self, symbol, signal, message=None, amount=None):
                raise TypeError("payload failed")

        state = MonitorState(
            symbol="BTCUSDT",
            webhook=FailingWebhook(),
            min_order_gap_ms=120_000,
            now_ms=lambda: 1_000,
        )
        signal = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="committed order",
            price=100.0,
            open_time=1_000,
            score=80.0,
            threshold=70.0,
            threshold_segment="WD-08",
            session_allowed=True,
        )
        latest = Kline(0, 100.0, 100.0, 100.0, 100.0, 1.0, 1_000)

        decision = state._maybe_open_order(signal, latest)
        repeated = state._maybe_open_order(signal, latest)

        self.assertEqual(decision, "OPENED")
        self.assertEqual(repeated, "COOLDOWN")
        self.assertEqual(state.snapshot()["stats"]["total_orders"], 1)

    def test_mechanical_rejections_clear_stale_risk_pause(self):
        latest = Kline(60_001, 100.0, 100.0, 100.0, 100.0, 1.0, 120_000)
        cases = (
            (
                "BELOW_THRESHOLD",
                Signal(
                    direction="WAIT",
                    timeframe_minutes=10,
                    level="B",
                    reason="below threshold",
                    price=100.0,
                    open_time=latest.close_time,
                    score=0.0,
                    threshold=70.0,
                ),
                None,
            ),
            (
                "COOLDOWN",
                Signal(
                    direction="LONG",
                    timeframe_minutes=10,
                    level="A",
                    reason="cooldown",
                    price=100.0,
                    open_time=latest.close_time,
                    score=90.0,
                    threshold=70.0,
                    threshold_segment="WD-08",
                    session_allowed=True,
                ),
                latest.close_time - 60_000,
            ),
        )

        for expected, signal, last_opened_at in cases:
            with self.subTest(expected=expected):
                state = MonitorState(symbol="BTCUSDT", min_order_gap_ms=120_000)
                state._last_order_opened_at = last_opened_at
                state.risk_pause = "stale risk pause"

                decision = state._maybe_open_order(signal, latest)

                self.assertEqual(decision, expected)
                self.assertEqual(state.snapshot()["risk_pause"], "")

    def test_restart_restores_minimum_order_gap_from_latest_order(self):
        storage = RecordingStorage()
        storage._persist_order(
            SimulatedOrder(
                id=1,
                direction="LONG",
                timeframe_minutes=10,
                level="A",
                reason="restored",
                entry_price=100.0,
                opened_at=600_000,
                expires_at=1_200_000,
                threshold_segment="WD-08",
                wave_batch_id="old-wave",
            ),
            "BTCUSDT",
        )
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            max_open_orders=2,
            min_order_gap_ms=120_000,
            enable_wave_guard=False,
        )
        signal = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="new",
            price=101.0,
            open_time=660_000,
            score=85.0,
            threshold=70.0,
            threshold_segment="WD-08",
            session_allowed=True,
            wave_batch_id="new-wave",
        )

        decision = state._maybe_open_order(
            signal,
            Kline(600_001, 101.0, 101.0, 101.0, 101.0, 1.0, 660_000),
        )

        self.assertEqual(decision, "COOLDOWN")
        self.assertEqual(len(state.simulator.orders), 1)

    def test_main_order_path_applies_capacity_and_cooldown_per_direction(self):
        state = MonitorState(
            symbol="BTCUSDT",
            max_open_orders=2,
            max_open_long_orders=1,
            max_open_short_orders=2,
            min_order_gap_ms=120_000,
        )

        def candidate(direction: str, opened_at: int) -> Signal:
            return Signal(
                direction=direction,
                timeframe_minutes=10,
                level="A",
                reason="direction capacity",
                price=100.0,
                open_time=opened_at,
                score=90.0 if direction == "LONG" else -90.0,
                threshold=70.0,
                threshold_segment="WD-08" if direction == "LONG" else "WD-23",
                session_allowed=True,
            )

        first_long = state._maybe_open_order(
            candidate("LONG", 1_000),
            Kline(0, 100.0, 100.0, 100.0, 100.0, 1.0, 1_000),
        )
        blocked_long = state._maybe_open_order(
            candidate("LONG", 121_000),
            Kline(60_000, 100.0, 100.0, 100.0, 100.0, 1.0, 121_000),
        )
        opposite_short = state._maybe_open_order(
            candidate("SHORT", 122_000),
            Kline(120_000, 100.0, 100.0, 100.0, 100.0, 1.0, 122_000),
        )

        self.assertEqual(first_long, "OPENED")
        self.assertEqual(blocked_long, "HOLD_LONG_OPEN_ORDER")
        self.assertEqual(opposite_short, "OPENED")
        self.assertEqual(
            [(order.direction, order.order_slot) for order in state.simulator.orders],
            [("LONG", "FIRST"), ("SHORT", "FIRST")],
        )

    def test_reset_symbol_restores_minimum_order_gap_from_latest_order(self):
        storage = RecordingStorage()
        storage._persist_order(
            SimulatedOrder(
                id=1,
                direction="SHORT",
                timeframe_minutes=10,
                level="A",
                reason="restored",
                entry_price=100.0,
                opened_at=600_000,
                expires_at=1_200_000,
                threshold_segment="WD-23",
                wave_batch_id="old-wave",
            ),
            "ETHUSDT",
        )
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            max_open_orders=2,
            min_order_gap_ms=120_000,
            enable_wave_guard=False,
        )

        state.reset_symbol("ETHUSDT")
        signal = Signal(
            direction="SHORT",
            timeframe_minutes=10,
            level="A",
            reason="new",
            price=99.0,
            open_time=660_000,
            score=-85.0,
            threshold=70.0,
            threshold_segment="WD-23",
            session_allowed=True,
            wave_batch_id="new-wave",
        )
        decision = state._maybe_open_order(
            signal,
            Kline(600_001, 99.0, 99.0, 99.0, 99.0, 1.0, 660_000),
        )

        self.assertEqual(decision, "COOLDOWN")
        self.assertEqual(len(state.simulator.orders), 1)

    def test_symbol_change_during_analysis_discards_computed_state(self):
        state = MonitorState(symbol="BTCUSDT")
        original_advance_wave = advance_wave

        def switch_symbol(*args, **kwargs):
            state.reset_symbol("ETHUSDT")
            return original_advance_wave(*args, **kwargs)

        with patch("app.state.advance_wave", side_effect=switch_symbol):
            state.update_from_klines([kline(1, 100.0, 10.0)])

        snapshot = state.snapshot()
        self.assertEqual(snapshot["symbol"], "ETHUSDT")
        self.assertEqual(snapshot["kline_count"], 0)

    def test_completed_async_storage_writes_are_released_without_manual_wait(self):
        state = MonitorState(symbol="BTCUSDT", storage=RecordingStorage())
        for _ in range(100):
            state._submit_storage_write(lambda: None)

        deadline = time.monotonic() + 2
        while state._storage_futures and time.monotonic() < deadline:
            time.sleep(0.001)

        self.assertEqual(len(state._storage_futures), 0)

    def test_async_storage_failure_is_exposed_in_state(self):
        state = MonitorState(symbol="BTCUSDT", storage=RecordingStorage())

        def fail_write():
            raise OSError("audit write failed")

        state._submit_storage_write(fail_write)
        deadline = time.monotonic() + 2
        while state.snapshot()["last_error"] is None and time.monotonic() < deadline:
            time.sleep(0.001)

        error = state.snapshot()["last_error"]
        self.assertIsNotNone(error)
        self.assertIn("异步存储写入失败", error)
        self.assertIn("audit write failed", error)

    def test_wave_batch_guard_stops_refill_after_first_loss(self):
        state = MonitorState(
            symbol="BTCUSDT",
            wave_batch_guard_config=WaveBatchGuardConfig(),
        )
        state.simulator.orders.append(
            SimulatedOrder(
                id=1,
                direction="LONG",
                timeframe_minutes=10,
                level="A",
                reason="first loss",
                entry_price=100.0,
                opened_at=0,
                expires_at=600_000,
                status="SETTLED",
                result="LOSS",
                settled_at=600_000,
                pnl=-10.0,
                wave_batch_id="wave-a",
            )
        )
        signal = Signal(
            "LONG",
            10,
            "A",
            "same wave",
            100.0,
            700_000,
            score=84.0,
            threshold=79.0,
            session_allowed=True,
            wave_batch_id="wave-a",
        )

        decision = state._maybe_open_order(
            signal,
            Kline(640_000, 100.0, 100.0, 100.0, 100.0, 1.0, 700_000),
        )

        self.assertEqual(decision, "WAVE_BATCH_LOSS_LOCKED")
        self.assertEqual(state.snapshot()["wave_batch_guard"]["mode"], "BATCH_LOCKED")
        self.assertEqual(len(state.simulator.orders), 1)

    def test_wave_batch_lock_cancels_and_persists_pending_progression_credit(self):
        storage = RecordingStorage()
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            now_ms=lambda: 0,
            wave_batch_guard_config=WaveBatchGuardConfig(),
        )
        first = state.simulator.open_order(
            Signal("LONG", 1, "A", "first", 100.0, 0, wave_batch_id="source-wave"),
            100.0,
            0,
        )
        state.simulator.settle_expired_orders(60_000, 101.0)
        self.assertEqual(state.simulator.stake_progression.credits[0].status, "PENDING")
        state.simulator.orders.append(
            SimulatedOrder(
                id=2,
                direction="LONG",
                timeframe_minutes=10,
                level="A",
                reason="loss",
                entry_price=100.0,
                opened_at=100_000,
                expires_at=700_000,
                status="SETTLED",
                result="LOSS",
                settled_at=700_000,
                pnl=-10.0,
                wave_batch_id="lock-wave",
            )
        )
        signal = Signal(
            "LONG",
            10,
            "A",
            "same wave",
            100.0,
            800_000,
            score=84.0,
            threshold=79.0,
            session_allowed=True,
            wave_batch_id="lock-wave",
        )

        decision = state._maybe_open_order(
            signal,
            Kline(740_000, 100.0, 100.0, 100.0, 100.0, 1.0, 800_000),
        )
        state.wait_for_storage_writes()

        self.assertEqual(first.result, "WIN")
        self.assertEqual(decision, "WAVE_BATCH_LOSS_LOCKED")
        self.assertEqual(state.simulator.stake_progression.credits[0].status, "CANCELLED")
        self.assertEqual(storage.credit_saves[-1][1]["status"], "CANCELLED")

    def test_wave_batch_guard_marks_first_post_cooldown_order_as_recovery(self):
        state = MonitorState(
            symbol="BTCUSDT",
            result_sequence_guard_config=ResultSequenceGuardConfig(enabled=False),
            wave_batch_guard_config=WaveBatchGuardConfig(),
        )
        for order_id, batch_id, opened_minute, segment in (
            (1, "wave-a", 0, "WD-01"),
            (2, "wave-a", 2, "WD-02"),
            (3, "wave-b", 30, "WD-03"),
            (4, "wave-b", 32, "WD-04"),
        ):
            state.simulator.orders.append(
                SimulatedOrder(
                    id=order_id,
                    direction="LONG",
                    timeframe_minutes=10,
                    level="A",
                    reason="failed batch",
                    entry_price=100.0,
                    opened_at=opened_minute * 60_000,
                    expires_at=(opened_minute + 10) * 60_000,
                    threshold_segment=segment,
                    status="SETTLED",
                    result="LOSS",
                    settled_at=(opened_minute + 10) * 60_000,
                    pnl=-10.0,
                    wave_batch_id=batch_id,
                )
            )
        current_time = 103 * 60_000
        signal = Signal(
            "LONG",
            10,
            "A",
            "recovery candidate",
            100.0,
            current_time,
            score=84.0,
            threshold=79.0,
            threshold_segment="WD-05",
            session_allowed=True,
            wave_batch_id="wave-c",
        )

        decision = state._maybe_open_order(
            signal,
            Kline(current_time - 60_000, 100.0, 100.0, 100.0, 100.0, 1.0, current_time),
        )

        self.assertEqual(decision, "OPENED")
        self.assertEqual(state.simulator.orders[-1].wave_guard_mode, "RECOVERY")
        self.assertEqual(state.snapshot()["wave_batch_guard"]["mode"], "RECOVERY")

    def test_wave_global_cooldown_refreshes_without_cancelling_directionless_credit(self):
        state = MonitorState(
            symbol="BTCUSDT",
            result_sequence_guard_config=ResultSequenceGuardConfig(enabled=False),
            wave_batch_guard_config=WaveBatchGuardConfig(),
        )
        for order_id, batch_id, opened_minute in (
            (1, "wave-a", 0),
            (2, "wave-a", 2),
            (3, "wave-b", 30),
            (4, "wave-b", 32),
        ):
            state.simulator.orders.append(
                SimulatedOrder(
                    id=order_id,
                    direction="LONG",
                    timeframe_minutes=10,
                    level="A",
                    reason="失败波段",
                    entry_price=100.0,
                    opened_at=opened_minute * 60_000,
                    expires_at=(opened_minute + 10) * 60_000,
                    status="SETTLED",
                    result="LOSS",
                    settled_at=(opened_minute + 10) * 60_000,
                    pnl=-10.0,
                    wave_batch_id=batch_id,
                )
            )
        state.simulator.stake_progression.credits.append(
            StakeProgressionCredit(source_order_id=99, created_at=0)
        )
        current_time = 50 * 60_000
        signal = Signal(
            direction="WAIT",
            timeframe_minutes=10,
            level="B",
            reason="实时信号不足",
            price=100.0,
            open_time=current_time - 60_000,
            score=0.0,
            threshold=80.0,
        )

        decision = state._maybe_open_order(
            signal,
            Kline(current_time - 60_000, 100.0, 100.0, 100.0, 100.0, 1.0, current_time),
        )

        self.assertEqual(decision, "BELOW_THRESHOLD")
        self.assertEqual(state.snapshot()["wave_batch_guard"]["mode"], "COOLDOWN")
        self.assertEqual(state.simulator.stake_progression.credits[-1].status, "PENDING")

    def test_new_wave_cancels_old_credit_before_opening_base_order(self):
        state = MonitorState(
            symbol="BTCUSDT",
            enable_wave_guard=True,
            wave_batch_guard_config=WaveBatchGuardConfig(),
        )
        state.simulator.orders.append(
            SimulatedOrder(
                id=1,
                direction="LONG",
                timeframe_minutes=10,
                level="A",
                reason="旧波段盈利",
                entry_price=100.0,
                opened_at=0,
                expires_at=600_000,
                status="SETTLED",
                result="WIN",
                settled_at=600_000,
                pnl=8.0,
                wave_state="RANGE_LOW",
                wave_confirmed_at=60_000,
                wave_batch_id="60000|RANGE_LOW|LONG|WD-00|STATIC",
            )
        )
        state.simulator.stake_progression.credits.append(
            StakeProgressionCredit(
                source_order_id=1,
                created_at=600_000,
                direction="LONG",
            )
        )
        signal = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="新波段首单",
            price=101.0,
            open_time=900_000,
            score=84.0,
            threshold=79.0,
            session_allowed=True,
            wave_state="UP_LEG",
            wave_raw_state="UP_LEG",
            wave_window=8,
            wave_confirmations=2,
            wave_confirmed_at=900_000,
            wave_batch_id="900000|UP_LEG|LONG|WD-00|STATIC",
        )

        decision = state._maybe_open_order(
            signal,
            Kline(840_000, 101.0, 101.0, 101.0, 101.0, 1.0, 900_000),
        )

        self.assertEqual(decision, "OPENED")
        self.assertEqual(state.simulator.stake_progression.credits[0].status, "CANCELLED")
        self.assertEqual(state.simulator.orders[-1].stake, 10.0)
        self.assertEqual(state.simulator.orders[-1].stake_progression_step, 1)

    def test_turn_state_cancels_credit_before_any_new_signal(self):
        state = MonitorState(
            symbol="BTCUSDT",
            enable_wave_guard=True,
            wave_batch_guard_config=WaveBatchGuardConfig(),
        )
        state.simulator.orders.append(
            SimulatedOrder(
                id=1,
                direction="LONG",
                timeframe_minutes=10,
                level="A",
                reason="旧上涨波段盈利",
                entry_price=100.0,
                opened_at=0,
                expires_at=600_000,
                status="SETTLED",
                result="WIN",
                settled_at=600_000,
                pnl=8.0,
                wave_state="UP_LEG",
                wave_confirmed_at=60_000,
                wave_batch_id="60000|UP_LEG|LONG|WD-00|STATIC",
            )
        )
        state.simulator.stake_progression.credits.append(
            StakeProgressionCredit(
                source_order_id=1,
                created_at=600_000,
                direction="LONG",
            )
        )
        signal = Signal(
            direction="WAIT",
            timeframe_minutes=10,
            level="B",
            reason="转折确认中",
            price=99.0,
            open_time=900_000,
            score=0.0,
            threshold=79.0,
            wave_state="TURN_DOWN",
            wave_raw_state="DOWN_LEG",
            wave_window=8,
            wave_confirmations=1,
            wave_confirmed_at=900_000,
        )

        decision = state._maybe_open_order(
            signal,
            Kline(840_000, 99.0, 99.0, 99.0, 99.0, 1.0, 900_000),
        )

        self.assertEqual(decision, "BELOW_THRESHOLD")
        self.assertEqual(state.simulator.stake_progression.credits[0].status, "PENDING")

    def test_directionless_wait_does_not_cancel_either_direction_credit(self):
        state = MonitorState(symbol="BTCUSDT", enable_wave_guard=True)
        state.simulator.stake_progression.credits.extend(
            [
                StakeProgressionCredit(1, 100, direction="LONG"),
                StakeProgressionCredit(2, 100, direction="SHORT"),
            ]
        )
        signal = Signal(
            direction="WAIT",
            timeframe_minutes=10,
            level="B",
            reason="无明确方向",
            price=100.0,
            open_time=1_000,
            wave_guard_mode="DIRECTION_BLOCKED",
        )

        state._maybe_open_order(
            signal,
            Kline(0, 100.0, 100.0, 100.0, 100.0, 1.0, 1_000),
        )

        self.assertEqual(
            [credit.status for credit in state.simulator.stake_progression.credits],
            ["PENDING", "PENDING"],
        )

    def test_credit_cancellation_failure_keeps_memory_pending_and_blocks_order(self):
        storage = RecordingStorage()
        source = SimulatedOrder(
            id=1,
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="旧波段盈利",
            entry_price=100.0,
            opened_at=0,
            expires_at=600_000,
            status="SETTLED",
            result="WIN",
            settled_at=600_000,
            pnl=8.0,
            wave_state="RANGE_LOW",
            wave_confirmed_at=60_000,
            wave_batch_id="60000|RANGE_LOW|LONG|WD-00|STATIC",
        )
        storage._persist_order(source, "BTCUSDT")
        storage._persist_credit(
            StakeProgressionCredit(source_order_id=1, created_at=600_000),
            "BTCUSDT",
        )
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            now_ms=lambda: 0,
            enable_wave_guard=True,
            wave_batch_guard_config=WaveBatchGuardConfig(),
        )
        storage.fail_once("cancel_stake_progression_credits")
        signal = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="新波段首单",
            price=101.0,
            open_time=900_000,
            score=84.0,
            threshold=79.0,
            session_allowed=True,
            wave_state="UP_LEG",
            wave_raw_state="UP_LEG",
            wave_window=8,
            wave_confirmations=2,
            wave_confirmed_at=900_000,
            wave_batch_id="900000|UP_LEG|LONG|WD-00|STATIC",
        )

        decision = state._maybe_open_order(
            signal,
            Kline(840_000, 101.0, 101.0, 101.0, 101.0, 1.0, 900_000),
        )

        self.assertEqual(decision, "STORAGE_ERROR")
        self.assertEqual(state.simulator.stake_progression.credits[0].status, "PENDING")
        self.assertEqual(len(state.simulator.orders), 1)
        self.assertIn("资格取消持久化失败", state.last_error)
        self.assertEqual(state.selected_signal.decision_trace[-1]["stage"], "PERSISTENCE")
        self.assertEqual(state.selected_signal.decision_trace[-1]["result"], "BLOCK")
        self.assertEqual(state.selected_signal.first_decisive_block, "PERSISTENCE")

    def test_wave_guard_blocks_short_in_up_leg_and_keeps_long(self):
        state = MonitorState(symbol="BTCUSDT", enable_wave_guard=True)
        wave = WaveSnapshot(
            state="UP_LEG",
            raw_state="UP_LEG",
            window=8,
            efficiency=0.8,
            direction_ratio=0.86,
            atr_strength=2.1,
            range_position=0.9,
            confirmations=2,
            confirmed_at=4_000_000,
            allowed_directions=("LONG",),
        )
        short_signal = Signal(
            "SHORT",
            10,
            "A",
            "实时SHORT",
            100.0,
            4_100_000,
            score=-84.0,
            threshold=79.0,
            threshold_segment="WD-22",
        )

        blocked = state._apply_wave_guard(short_signal, wave)
        allowed = state._apply_wave_guard(replace(short_signal, direction="LONG", score=84.0), wave)

        self.assertEqual(blocked.direction, "WAIT")
        self.assertEqual(blocked.observe_direction, "SHORT")
        self.assertEqual(blocked.wave_guard_mode, "DIRECTION_BLOCKED")
        self.assertEqual(blocked.wave_guard_status, "DIRECTION_BLOCKED")
        self.assertIn("不允许 SHORT", blocked.wave_guard_reason)
        self.assertIn("波段方向冲突", blocked.reason)
        self.assertEqual(allowed.direction, "LONG")
        self.assertEqual(allowed.wave_guard_mode, "NORMAL")
        self.assertTrue(allowed.wave_batch_id)

    def test_wave_guard_records_state_without_blocking_by_default(self):
        state = MonitorState(symbol="BTCUSDT")
        wave = WaveSnapshot(
            state="UP_LEG",
            raw_state="UP_LEG",
            window=8,
            efficiency=0.8,
            direction_ratio=0.86,
            atr_strength=2.1,
            range_position=0.9,
            confirmations=2,
            confirmed_at=4_000_000,
            allowed_directions=("LONG",),
        )
        signal = Signal(
            "SHORT",
            10,
            "A",
            "实时SHORT",
            100.0,
            4_100_000,
            score=-84.0,
            threshold=79.0,
            threshold_segment="WD-22",
        )

        observed = state._apply_wave_guard(signal, wave)

        self.assertEqual(observed.direction, "SHORT")
        self.assertEqual(observed.wave_guard_mode, "DISABLED")
        self.assertEqual(observed.wave_guard_status, "DISABLED")
        self.assertTrue(observed.wave_batch_id)

    def test_seed_rebuilds_wave_anchor_and_preserves_loss_lock_after_restart(self):
        closes = [100.0] * 12 + [100.2, 100.5, 100.9, 101.3, 101.8, 102.3, 102.9, 103.5, 104.1]
        history = []
        for index, close in enumerate(closes):
            previous_close = closes[index - 1] if index else close
            history.append(
                Kline(
                    open_time=index * 60_000,
                    open=previous_close,
                    high=max(previous_close, close) + 0.2,
                    low=min(previous_close, close) - 0.2,
                    close=close,
                    volume=100.0,
                    close_time=(index + 1) * 60_000,
                )
            )
        uninterrupted = analyze_wave(())
        for end in range(15, len(history) + 1):
            uninterrupted = analyze_wave(history[:end], previous=uninterrupted)
        signal = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="同一上涨波段",
            price=closes[-1],
            open_time=history[-1].open_time,
            score=84.0,
            threshold=79.0,
            session_allowed=True,
            threshold_segment="WD-00",
        )
        before_restart = MonitorState(symbol="BTCUSDT")
        before_restart.wave_state = uninterrupted
        original = before_restart._attach_wave_metadata(signal, uninterrupted)

        restarted = MonitorState(
            symbol="BTCUSDT",
            wave_batch_guard_config=WaveBatchGuardConfig(),
        )
        restarted.seed_klines(history)
        restored = restarted._attach_wave_metadata(signal, restarted.wave_state)
        restarted.simulator.orders.append(
            SimulatedOrder(
                id=1,
                direction="LONG",
                timeframe_minutes=10,
                level="A",
                reason="重启前首亏",
                entry_price=103.5,
                opened_at=history[-2].close_time,
                expires_at=history[-2].close_time + 600_000,
                status="SETTLED",
                result="LOSS",
                settled_at=history[-1].close_time,
                pnl=-10.0,
                wave_batch_id=original.wave_batch_id,
            )
        )

        decision = restarted._maybe_open_order(restored, history[-1])

        self.assertEqual(restarted.wave_state.confirmed_at, uninterrupted.confirmed_at)
        self.assertEqual(restored.wave_batch_id, original.wave_batch_id)
        self.assertEqual(decision, "WAVE_BATCH_LOSS_LOCKED")

    def test_restart_preserves_wave_anchor_when_warmup_omits_long_wave_start(self):
        history = []
        for index in range(400):
            open_price = 100.0 + index * 0.2
            close_price = open_price + 0.2
            history.append(
                Kline(
                    open_time=index * 60_000,
                    open=open_price,
                    high=close_price + 0.1,
                    low=open_price - 0.1,
                    close=close_price,
                    volume=100.0,
                    close_time=(index + 1) * 60_000,
                )
            )
        storage = RecordingStorage()
        running = MonitorState(symbol="BTCUSDT", storage=storage)
        running.seed_klines(history)
        signal = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="超长上涨波段",
            price=history[-1].close,
            open_time=history[-1].open_time,
            score=84.0,
            threshold=79.0,
            session_allowed=True,
            threshold_segment="WD-00",
        )
        original = running._attach_wave_metadata(signal, running.wave_state)
        loss = SimulatedOrder(
            id=1,
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="重启前首亏",
            entry_price=history[-2].close,
            opened_at=history[-2].close_time,
            expires_at=history[-2].close_time + 600_000,
            status="SETTLED",
            result="LOSS",
            settled_at=history[-1].close_time,
            pnl=-10.0,
            wave_batch_id=original.wave_batch_id,
        )
        storage.save_order(loss, "BTCUSDT")

        restarted = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            min_order_gap_ms=0,
            wave_batch_guard_config=WaveBatchGuardConfig(),
        )
        restarted.seed_klines(history[-300:])
        restored = restarted._attach_wave_metadata(signal, restarted.wave_state)

        decision = restarted._maybe_open_order(restored, history[-1])

        self.assertEqual(restarted.wave_state.confirmed_at, running.wave_state.confirmed_at)
        self.assertEqual(restored.wave_batch_id, original.wave_batch_id)
        self.assertEqual(decision, "WAVE_BATCH_LOSS_LOCKED")

    def test_first_upgrade_inherits_persisted_order_anchor_without_wave_runtime(self):
        history = []
        for index in range(400):
            open_price = 100.0 + index * 0.2
            close_price = open_price + 0.2
            history.append(
                Kline(
                    open_time=index * 60_000,
                    open=open_price,
                    high=close_price + 0.1,
                    low=open_price - 0.1,
                    close=close_price,
                    volume=100.0,
                    close_time=(index + 1) * 60_000,
                )
            )
        uninterrupted = analyze_wave(())
        for end in range(15, len(history) + 1):
            uninterrupted = analyze_wave(history[:end], previous=uninterrupted)
        original_batch_id = (
            f"{uninterrupted.confirmed_at}|UP_LEG|LONG|WD-00|STATIC"
        )
        storage = RecordingStorage()
        storage.save_order(
            SimulatedOrder(
                id=1,
                direction="LONG",
                timeframe_minutes=10,
                level="A",
                reason="升级前首亏",
                entry_price=history[-2].close,
                opened_at=history[-2].close_time,
                expires_at=history[-2].close_time + 600_000,
                status="SETTLED",
                result="LOSS",
                settled_at=history[-1].close_time,
                pnl=-10.0,
                threshold_segment="WD-00",
                wave_state="UP_LEG",
                wave_raw_state="UP_LEG",
                wave_confirmed_at=uninterrupted.confirmed_at,
                wave_batch_id=original_batch_id,
            ),
            "BTCUSDT",
        )
        restarted = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            min_order_gap_ms=0,
            wave_batch_guard_config=WaveBatchGuardConfig(),
        )
        restarted.seed_klines([])
        restarted.seed_klines(history[-14:])
        restarted.seed_klines(history[-15:])
        restarted.seed_klines(history[-300:])
        restored = restarted._attach_wave_metadata(
            Signal(
                direction="LONG",
                timeframe_minutes=10,
                level="A",
                reason="升级后同一上涨波段",
                price=history[-1].close,
                open_time=history[-1].open_time,
                score=84.0,
                threshold=79.0,
                session_allowed=True,
                threshold_segment="WD-00",
            ),
            restarted.wave_state,
        )

        decision = restarted._maybe_open_order(restored, history[-1])

        self.assertEqual(restarted.wave_state.confirmed_at, uninterrupted.confirmed_at)
        self.assertEqual(restored.wave_batch_id, original_batch_id)
        self.assertEqual(decision, "WAVE_BATCH_LOSS_LOCKED")

    def test_kline_gap_starts_new_wave_and_cancels_old_progression_credit(self):
        old_wave = WaveSnapshot(
            state="UP_LEG",
            raw_state="UP_LEG",
            window=8,
            efficiency=0.9,
            direction_ratio=0.85,
            atr_strength=1.7,
            range_position=0.92,
            confirmations=2,
            confirmed_at=960_000,
            allowed_directions=("LONG",),
        )
        old_batch_id = "960000|UP_LEG|LONG|WD-00|STATIC"
        source = SimulatedOrder(
            id=1,
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="缺口前赢单",
            entry_price=100.0,
            opened_at=23_400_000,
            expires_at=24_000_000,
            status="SETTLED",
            result="WIN",
            settled_at=24_000_000,
            pnl=8.0,
            threshold_segment="WD-00",
            wave_state="UP_LEG",
            wave_raw_state="UP_LEG",
            wave_confirmed_at=old_wave.confirmed_at,
            wave_batch_id=old_batch_id,
        )
        storage = RecordingStorage()
        storage.save_order(source, "BTCUSDT")
        storage.save_wave_runtime("BTCUSDT", old_wave, evaluated_at=24_000_000)
        storage.progression_runtime["BTCUSDT"] = (TWO_STAGE_VERSION, 0, True)
        storage.save_stake_progression_credit(
            "BTCUSDT",
            StakeProgressionCredit(source_order_id=1, created_at=24_000_000),
        )
        history = []
        for index in range(300):
            open_time = 48_000_000 + index * 60_000
            open_price = 200.0 + index * 0.2
            close_price = open_price + 0.2
            history.append(
                Kline(
                    open_time=open_time,
                    open=open_price,
                    high=close_price + 0.1,
                    low=open_price - 0.1,
                    close=close_price,
                    volume=100.0,
                    close_time=open_time + 60_000,
                )
            )
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            now_ms=lambda: history[-1].close_time,
            enable_wave_guard=True,
            wave_batch_guard_config=WaveBatchGuardConfig(),
        )
        state.seed_klines(history)
        signal = state._attach_wave_metadata(
            Signal(
                direction="LONG",
                timeframe_minutes=10,
                level="A",
                reason="缺口后上涨波段",
                price=history[-1].close,
                open_time=history[-1].open_time,
                score=84.0,
                threshold=79.0,
                session_allowed=True,
                threshold_segment="WD-00",
            ),
            state.wave_state,
        )

        decision = state._maybe_open_order(signal, history[-1])

        self.assertEqual(decision, "OPENED")
        self.assertNotEqual(signal.wave_confirmed_at, old_wave.confirmed_at)
        self.assertNotEqual(signal.wave_batch_id, old_batch_id)
        self.assertEqual(state.simulator.orders[-1].stake, 10.0)
        self.assertEqual(state.simulator.orders[-1].stake_progression_step, 1)
        self.assertEqual(
            storage.persisted_credits[("BTCUSDT", TWO_STAGE_VERSION, 1)].status,
            "CANCELLED",
        )

    def test_first_upgrade_cancels_pending_credit_before_runtime_snapshot(self):
        history = []
        for index in range(400):
            open_price = 100.0 + index * 0.2
            close_price = open_price + 0.2
            history.append(
                Kline(
                    open_time=index * 60_000,
                    open=open_price,
                    high=close_price + 0.1,
                    low=open_price - 0.1,
                    close=close_price,
                    volume=100.0,
                    close_time=(index + 1) * 60_000,
                )
            )
        old_wave = analyze_wave(())
        for end in range(15, len(history) + 1):
            old_wave = analyze_wave(history[:end], previous=old_wave)
        old_batch_id = f"{old_wave.confirmed_at}|UP_LEG|LONG|WD-00|STATIC"
        source = SimulatedOrder(
            id=1,
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="升级前赢单",
            entry_price=history[-2].close,
            opened_at=history[-2].open_time,
            expires_at=history[-1].close_time,
            status="SETTLED",
            result="WIN",
            settled_at=history[-1].close_time,
            pnl=8.0,
            threshold_segment="WD-00",
            wave_state="UP_LEG",
            wave_raw_state="UP_LEG",
            wave_confirmed_at=old_wave.confirmed_at,
            wave_batch_id=old_batch_id,
        )
        storage = RecordingStorage()
        storage.save_order(source, "BTCUSDT")
        storage.progression_runtime["BTCUSDT"] = (TWO_STAGE_VERSION, 0, True)
        storage.save_stake_progression_credit(
            "BTCUSDT",
            StakeProgressionCredit(
                source_order_id=source.id,
                created_at=source.settled_at,
            ),
        )

        first_boot = MonitorState(symbol="BTCUSDT", storage=storage)
        first_boot.seed_klines(history[-300:])
        second_boot = MonitorState(symbol="BTCUSDT", storage=storage)
        second_boot.seed_klines(history[-300:])
        signal = second_boot._attach_wave_metadata(
            Signal(
                direction="LONG",
                timeframe_minutes=10,
                level="A",
                reason="二次重启后同一波段",
                price=history[-1].close,
                open_time=history[-1].open_time,
                score=84.0,
                threshold=79.0,
                session_allowed=True,
                threshold_segment="WD-00",
            ),
            second_boot.wave_state,
        )

        decision = second_boot._maybe_open_order(signal, history[-1])

        self.assertEqual(
            storage.persisted_credits[("BTCUSDT", TWO_STAGE_VERSION, source.id)].status,
            "CANCELLED",
        )
        self.assertEqual(decision, "OPENED")
        self.assertEqual(len(second_boot.simulator.orders), 2)
        self.assertEqual(second_boot.simulator.orders[-1].stake, 10.0)
        self.assertEqual(second_boot.simulator.orders[-1].stake_progression_step, 1)

    def test_newer_wave_snapshot_is_not_overwritten_by_older_warmup(self):
        history = []
        for index in range(400):
            open_price = 100.0 + index * 0.2
            close_price = open_price + 0.2
            history.append(
                Kline(
                    open_time=index * 60_000,
                    open=open_price,
                    high=close_price + 0.1,
                    low=open_price - 0.1,
                    close=close_price,
                    volume=100.0,
                    close_time=(index + 1) * 60_000,
                )
            )
        saved_wave = analyze_wave(())
        for end in range(15, len(history) + 1):
            saved_wave = analyze_wave(history[:end], previous=saved_wave)
        batch_id = f"{saved_wave.confirmed_at}|UP_LEG|LONG|WD-00|STATIC"
        storage = RecordingStorage()
        storage.save_wave_runtime(
            "BTCUSDT",
            saved_wave,
            evaluated_at=history[-1].close_time,
        )
        storage.save_order(
            SimulatedOrder(
                id=1,
                direction="LONG",
                timeframe_minutes=10,
                level="A",
                reason="快照时刻首亏",
                entry_price=history[-1].close,
                opened_at=history[-1].open_time,
                expires_at=history[-1].close_time + 600_000,
                status="SETTLED",
                result="LOSS",
                settled_at=history[-1].close_time,
                pnl=-10.0,
                threshold_segment="WD-00",
                wave_state="UP_LEG",
                wave_raw_state="UP_LEG",
                wave_confirmed_at=saved_wave.confirmed_at,
                wave_batch_id=batch_id,
            ),
            "BTCUSDT",
        )
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            min_order_gap_ms=0,
            wave_batch_guard_config=WaveBatchGuardConfig(),
        )

        state.seed_klines(history[-301:-1])
        state.seed_klines([history[-1]])
        restored = state._attach_wave_metadata(
            Signal(
                direction="LONG",
                timeframe_minutes=10,
                level="A",
                reason="实时数据追平快照",
                price=history[-1].close,
                open_time=history[-1].open_time,
                score=84.0,
                threshold=79.0,
                session_allowed=True,
                threshold_segment="WD-00",
            ),
            state.wave_state,
        )

        decision = state._maybe_open_order(restored, history[-1])

        self.assertEqual(state.wave_state.confirmed_at, saved_wave.confirmed_at)
        self.assertEqual(restored.wave_batch_id, batch_id)
        self.assertEqual(decision, "WAVE_BATCH_LOSS_LOCKED")

    def test_wave_runtime_persistence_failure_pauses_order_opening(self):
        storage = RecordingStorage()
        storage.fail_once("save_wave_runtime")
        state = MonitorState(symbol="BTCUSDT", storage=storage)
        history = [
            Kline(
                open_time=index * 60_000,
                open=100.0 + index,
                high=101.2 + index,
                low=99.8 + index,
                close=101.0 + index,
                volume=100.0,
                close_time=(index + 1) * 60_000,
            )
            for index in range(16)
        ]

        state.seed_klines(history)

        self.assertEqual(state.order_decision, "STORAGE_ERROR")
        self.assertEqual(state.risk_pause, "存储写入失败，暂停开单")
        self.assertIn("波段运行态持久化失败", state.last_error)

    def test_wave_guard_blocks_turn_and_range_middle(self):
        state = MonitorState(symbol="BTCUSDT", enable_wave_guard=True)
        signal = Signal(
            "LONG",
            10,
            "A",
            "实时LONG",
            100.0,
            4_100_000,
            score=84.0,
            threshold=79.0,
        )
        for wave_state in ("TURN_UP", "TURN_DOWN", "RANGE_MID"):
            wave = WaveSnapshot(
                state=wave_state,
                raw_state="UP_LEG" if wave_state == "TURN_UP" else wave_state,
                window=8,
                efficiency=0.5,
                direction_ratio=0.7,
                atr_strength=1.0,
                range_position=0.5,
                confirmations=1,
                confirmed_at=4_000_000,
                allowed_directions=(),
            )

            guarded = state._apply_wave_guard(signal, wave)

            self.assertEqual(guarded.direction, "WAIT")
            self.assertEqual(guarded.wave_guard_mode, "DIRECTION_BLOCKED")

    def test_daily_profile_cannot_restore_wave_blocked_direction(self):
        now = shanghai_timestamp("2026-07-30T08:00:00")
        state = MonitorState(
            symbol="BTCUSDT",
            enable_daily_profile_selector=True,
            enable_wave_guard=True,
        )
        state.active_daily_profile_selection = {
            "version": "DPS-20260730-0800",
            "status": "READY",
            "selected_profiles": [
                {"key": "10|short_observe|generic_short_observe|SHORT|WD-22"}
            ],
        }
        signal = Signal(
            "SHORT",
            10,
            "A",
            "实时SHORT",
            100.0,
            now,
            score=-84.0,
            threshold=79.0,
            threshold_segment="WD-22",
            strategy_family="short_observe",
            strategy_tag="generic_short_observe",
        )
        up_wave = WaveSnapshot(
            "UP_LEG", "UP_LEG", 8, 0.8, 0.8, 2.0, 0.9, 2, now, ("LONG",)
        )

        guarded = state._apply_wave_guard(signal, up_wave)
        selected, required = state._select_daily_profile_signal(guarded, [], now)
        decision = state._maybe_open_order(
            selected,
            kline(1, 100.0, 100),
            daily_profile_required=required,
        )

        self.assertEqual(selected.direction, "SHORT")
        self.assertTrue(selected.daily_profile_selected)
        self.assertEqual(decision, "WAVE_DIRECTION_BLOCKED")
        self.assertEqual(state.simulator.orders, [])
        self.assertEqual(state.observations[-1].wave_guard_status, "DIRECTION_BLOCKED")
        self.assertIn("不允许 SHORT", state.observations[-1].wave_guard_reason)

    def test_default_order_policy_supports_two_open_orders_two_minutes_apart(self):
        state = MonitorState(symbol="BTCUSDT")

        self.assertEqual(state.order_policy.max_open_orders, 2)
        self.assertEqual(state.order_policy.min_order_gap_ms, 2 * 60_000)
        order_policy = state.snapshot()["order_policy"]
        self.assertEqual(order_policy["max_open_orders"], 2)
        self.assertEqual(order_policy["max_open_long_orders"], 1)
        self.assertEqual(order_policy["max_open_short_orders"], 2)
        self.assertEqual(order_policy["min_order_gap_ms"], 2 * 60_000)
        self.assertEqual(
            order_policy["by_direction"],
            {
                "LONG": {"last_opened_at": None, "next_allowed_at": None},
                "SHORT": {"last_opened_at": None, "next_allowed_at": None},
            },
        )
        self.assertTrue(state.snapshot()["result_sequence_guard"]["enabled"])
        self.assertFalse(state.snapshot()["wave_state"]["enabled"])
        self.assertFalse(state.snapshot()["wave_batch_guard"]["enabled"])

    def test_daily_profile_status_does_not_repeat_reloaded_active_snapshot_as_pending(self):
        now = shanghai_timestamp("2026-07-30T08:00:00")
        state = MonitorState(symbol="BTCUSDT", enable_daily_profile_selector=True)
        snapshot = {
            "version": "DPS-20260730-0800",
            "status": "READY",
            "effective_from": now,
            "effective_until": now + 86_400_000,
            "selected_profiles": [{"key": "10|long_observe|generic_long_observe|LONG|WD-06"}],
        }
        state.daily_profile_selection = dict(snapshot)
        state.active_daily_profile_selection = dict(snapshot)

        status = state._daily_profile_selector_status()

        self.assertEqual(status["selected_count"], 1)
        self.assertEqual(status["pending_profiles"], [])

    def test_daily_profile_selection_evaluates_once_at_0750_and_activates_at_0800(self):
        cutoff = shanghai_timestamp("2026-07-30T07:50:00")
        storage = RecordingStorage()
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            enable_daily_profile_selector=True,
            daily_profile_selector_config=DailyProfileSelectorConfig(
                min_samples=3,
                min_win_rate=0.6,
                min_ev=0.8,
            ),
        )
        state.observations.extend(
            settled_observation(
                idx,
                result,
                cutoff - (40 - idx * 10) * 60_000,
                family="short_observe",
                tag="generic_short_observe",
                direction="SHORT",
                segment="WD-22",
            )
            for idx, result in enumerate(["WIN", "WIN", "LOSS"])
        )

        state._refresh_daily_profile_selection(cutoff)
        state._refresh_daily_profile_selection(cutoff + 5 * 60_000)

        self.assertEqual(len(storage.daily_profile_selections), 1)
        self.assertEqual(state.daily_profile_selection["version"], "DPS-20260730-0800")
        self.assertEqual(state.daily_profile_selection["selected_count"], 1)
        self.assertIsNone(state.active_daily_profile_selection)

        state._refresh_daily_profile_selection(shanghai_timestamp("2026-07-30T08:00:00"))

        self.assertEqual(len(storage.daily_profile_selections), 1)
        self.assertEqual(state.active_daily_profile_selection["version"], "DPS-20260730-0800")

    def test_profile_restore_uses_stable_window_for_startup_and_symbol_reset(self):
        class ProfileRestoreStorage(RecordingStorage):
            def __init__(self):
                super().__init__()
                self.profile_loads = []

            def load_observations_for_profile(self, symbol, *, lookback_days=7):
                self.profile_loads.append((symbol, lookback_days))
                return []

        storage = ProfileRestoreStorage()
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            observation_profile_lookback_days=10,
            daily_profile_selector_config=DailyProfileSelectorConfig(
                lookback_days=7,
                stable_lookback_days=18,
            ),
        )

        state.reset_symbol("ETHUSDT")

        self.assertEqual(storage.profile_loads, [("BTCUSDT", 18), ("ETHUSDT", 18)])
        state.close()

    def test_profile_restore_uses_effective_stable_window_when_unspecified(self):
        class ProfileRestoreStorage(RecordingStorage):
            def __init__(self):
                super().__init__()
                self.profile_loads = []

            def load_observations_for_profile(self, symbol, *, lookback_days=7):
                self.profile_loads.append((symbol, lookback_days))
                return []

        storage = ProfileRestoreStorage()
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            observation_profile_lookback_days=10,
            daily_profile_selector_config=DailyProfileSelectorConfig(lookback_days=30),
        )
        self.assertEqual(storage.profile_loads, [("BTCUSDT", 30)])
        runtime = json.loads(state._decision_runtime_config().canonical_payload)["profiles"][
            "daily_selector"
        ]
        self.assertIsNone(runtime["stable_lookback_days"])
        self.assertEqual(runtime["effective_stable_lookback_days"], 30)
        self.assertEqual(runtime["stable_lookback_source"], "lookback_days")
        state.close()

    def test_restart_loaded_history_matches_full_history_dual_window_selection(self):
        cutoff = shanghai_timestamp("2026-07-30T07:50:00")
        config = DailyProfileSelectorConfig()
        results = [
            (["WIN"] * 11 + ["LOSS"] * 9, cutoff - 2 * 86_400_000),
            (["WIN"] * 13 + ["LOSS"] * 7, cutoff - 10 * 86_400_000),
        ]
        rows = []
        row_index = 0
        for outcomes, end in results:
            start = end - len(outcomes) * 20 * 60_000
            for offset, result in enumerate(outcomes):
                rows.append(
                    settled_observation(
                        row_index,
                        result,
                        start + offset * 20 * 60_000,
                        family="short_observe",
                        tag="generic_short_observe",
                        direction="SHORT",
                        segment="WD-02",
                    )
                )
                row_index += 1
        key = daily_profile_key(
            10,
            "short_observe",
            "generic_short_observe",
            "SHORT",
            "WD-02",
        )
        selected = {
            "key": key,
            "qualification_state": "QUALIFIED",
            "selection_state": "RETAINED",
            "joint_failure_runs": 0,
            "fast_7d": {},
            "stable_14d": {},
        }
        previous = {
            "lookback_end": cutoff - 86_400_000,
            "candidates": [selected],
            "selected_profiles": [selected],
        }
        full = build_daily_selection(
            rows,
            cutoff,
            config=config,
            previous_snapshot=previous,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            for row in rows:
                store.save_observation(row, "BTCUSDT")
            state = MonitorState(
                symbol="BTCUSDT",
                storage=store,
                daily_profile_selector_config=config,
            )
            restarted = build_daily_selection(
                state.observations,
                cutoff,
                config=config,
                previous_snapshot=previous,
            )
            state.close()

        full_item = next(item for item in full["candidates"] if item["key"] == key)
        restarted_item = next(item for item in restarted["candidates"] if item["key"] == key)
        self.assertEqual(restarted_item, full_item)
        self.assertEqual(restarted_item["selection_state"], "RETAINED")
        self.assertEqual(restarted_item["stable_14d"]["sample_size"], 40)

    def test_daily_profile_selection_re_evaluates_same_day_when_config_changes(self):
        current = shanghai_timestamp("2026-07-30T08:00:00")
        cutoff = shanghai_timestamp("2026-07-30T07:50:00")
        storage = RecordingStorage()
        config = DailyProfileSelectorConfig(
            min_samples=1,
            min_win_rate=0.60,
            exit_win_rate=0.60,
        )
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            enable_daily_profile_selector=True,
            daily_profile_selector_config=config,
        )
        previous = {
            "version": "DPS-20260730-0800",
            "status": "READY",
            "evaluated_at": cutoff,
            "lookback_start": cutoff - 7 * 86_400_000,
            "lookback_end": cutoff,
            "effective_from": current,
            "effective_until": current + 86_400_000,
            "config": {
                **config.normalized().__dict__,
                "min_win_rate": 0.65,
                "exit_win_rate": 0.65,
            },
            "selected_profiles": [],
            "selected_count": 0,
        }
        state.daily_profile_selection = dict(previous)
        state.active_daily_profile_selection = dict(previous)
        state.observations.append(
            settled_observation(1, "WIN", cutoff - 20 * 60_000)
        )

        state._refresh_daily_profile_selection(current)

        self.assertEqual(len(storage.daily_profile_selections), 1)
        self.assertEqual(state.daily_profile_selection["config"]["min_win_rate"], 0.60)
        self.assertEqual(state.daily_profile_selection["selected_count"], 1)
        self.assertEqual(state.active_daily_profile_selection, state.daily_profile_selection)

    def test_daily_selected_profile_promotes_wait_signal_with_observe_direction(self):
        now = shanghai_timestamp("2026-07-30T08:00:00")
        state = MonitorState(
            symbol="BTCUSDT",
            enable_daily_profile_selector=True,
            daily_profile_selector_config=DailyProfileSelectorConfig(min_samples=1),
            live_short_segments=("WD-02",),
        )
        state.active_daily_profile_selection = {
            "version": "DPS-20260730-0800",
            "status": "READY",
            "effective_from": now,
            "effective_until": now + 86_400_000,
            "selected_profiles": [
                {
                    "key": "10|short_observe|generic_short_observe|SHORT|WD-22",
                    "direction": "SHORT",
                    "sample_size": 32,
                    "win_rate": 0.65625,
                    "ev": 1.8125,
                }
            ],
        }
        primary = Signal(
            direction="WAIT",
            timeframe_minutes=10,
            level="B",
            reason="中位平量横盘：信号不足",
            price=100.0,
            open_time=now - 60_000,
            score=0.0,
            threshold=79.0,
            threshold_segment="WD-22",
            strategy_family="short_observe",
            strategy_tag="generic_short_observe",
            observe_direction="SHORT",
            observe_only=True,
        )
        latest = Kline(now - 60_000, 100.0, 100.0, 100.0, 100.0, 1.0, now)

        selected, required = state._select_daily_profile_signal(primary, [], now)
        decision = state._maybe_open_order(selected, latest, daily_profile_required=required)

        self.assertTrue(required)
        self.assertTrue(selected.daily_profile_selected)
        self.assertEqual(selected.direction, "SHORT")
        self.assertTrue(selected.actionable)
        self.assertFalse(selected.observe_only)
        self.assertEqual(selected.score, 0.0)
        self.assertEqual(selected.threshold, 79.0)
        self.assertEqual(decision, "OPENED")
        self.assertEqual(len(state.simulator.orders), 1)

    def test_numeric_trade_score_threshold_does_not_override_daily_profile_threshold(self):
        now = shanghai_timestamp("2026-07-30T08:00:00")
        state = MonitorState(
            symbol="BTCUSDT",
            enable_daily_profile_selector=True,
            trade_score_threshold=0.0,
        )
        state.active_daily_profile_selection = {
            "version": "DPS-20260730-0800",
            "status": "READY",
            "effective_from": now,
            "effective_until": now + 86_400_000,
            "selected_profiles": [
                {
                    "key": "10|short_observe|generic_short_observe|SHORT|WD-22",
                    "direction": "SHORT",
                    "sample_size": 32,
                    "win_rate": 0.65625,
                    "ev": 1.8125,
                }
            ],
        }
        primary = Signal(
            direction="WAIT",
            timeframe_minutes=10,
            level="B",
            reason="中位平量横盘：信号不足；分数 0.0 < 动态阈值 79.0，不开单",
            price=100.0,
            open_time=now - 60_000,
            score=0.0,
            threshold=79.0,
            threshold_segment="WD-22",
            strategy_family="short_observe",
            strategy_tag="generic_short_observe",
            observe_direction="SHORT",
            observe_only=True,
        )

        selected, required = state._select_daily_profile_signal(primary, [], now)

        self.assertTrue(required)
        self.assertEqual(selected.direction, "SHORT")
        self.assertTrue(selected.actionable)
        self.assertTrue(selected.daily_profile_selected)
        self.assertFalse(selected.observe_only)
        self.assertEqual(selected.threshold, 79.0)
        self.assertEqual(selected.calculated_threshold, 79.0)
        self.assertNotIn("动态阈值 79.0，不开单", selected.reason)
        self.assertEqual(
            state.snapshot()["trade_score_threshold"],
            {"mode": "AUDIT_ONLY", "value": 0.0},
        )

    def test_auto_trade_score_threshold_is_exposed_in_snapshot(self):
        state = MonitorState(symbol="BTCUSDT")

        self.assertEqual(
            state.snapshot()["trade_score_threshold"],
            {"mode": "AUTO", "value": None},
        )

    def test_numeric_trade_score_threshold_is_audit_only_for_below_threshold_primary(self):
        now = shanghai_timestamp("2026-07-30T08:00:00")
        state = MonitorState(
            symbol="BTCUSDT",
            enable_daily_profile_selector=True,
            trade_score_threshold=35.0,
        )
        state.active_daily_profile_selection = {
            "version": "DPS-20260730-0800",
            "status": "READY",
            "effective_from": now,
            "effective_until": now + 86_400_000,
            "selected_profiles": [
                {
                    "key": "10|short_observe|generic_short_observe|SHORT|WD-22",
                    "direction": "SHORT",
                    "sample_size": 32,
                    "win_rate": 0.65625,
                    "ev": 1.8125,
                }
            ],
        }
        primary = Signal(
            direction="WAIT",
            timeframe_minutes=10,
            level="B",
            reason="中位平量横盘：信号不足",
            price=100.0,
            open_time=now - 60_000,
            score=-34.9,
            threshold=79.0,
            threshold_segment="WD-22",
            strategy_family="short_observe",
            strategy_tag="generic_short_observe",
            observe_direction="SHORT",
            observe_only=True,
        )

        selected, required = state._select_daily_profile_signal(primary, [], now)

        self.assertTrue(required)
        self.assertEqual(selected.direction, "SHORT")
        self.assertTrue(selected.actionable)
        self.assertTrue(selected.daily_profile_selected)
        self.assertEqual(selected.threshold, 79.0)

    def test_numeric_trade_score_threshold_does_not_promote_unselected_profile(self):
        now = shanghai_timestamp("2026-07-30T08:00:00")
        state = MonitorState(
            symbol="BTCUSDT",
            enable_daily_profile_selector=True,
            trade_score_threshold=0.0,
        )
        state.active_daily_profile_selection = {
            "version": "DPS-20260730-0800",
            "status": "READY",
            "effective_from": now,
            "effective_until": now + 86_400_000,
            "selected_profiles": [
                {"key": "10|short_observe|generic_short_observe|SHORT|WD-23"}
            ],
        }
        primary = Signal(
            direction="WAIT",
            timeframe_minutes=10,
            level="B",
            reason="中位平量横盘：信号不足；分数 0.0 < 动态阈值 79.0，不开单",
            price=100.0,
            open_time=now - 60_000,
            score=0.0,
            threshold=79.0,
            threshold_segment="WD-22",
            strategy_family="short_observe",
            strategy_tag="generic_short_observe",
            observe_direction="SHORT",
            observe_only=True,
        )

        selected, required = state._select_daily_profile_signal(primary, [], now)

        self.assertTrue(required)
        self.assertEqual(selected.direction, "WAIT")
        self.assertFalse(selected.daily_profile_selected)
        self.assertEqual(selected.threshold, 79.0)
        self.assertIn("动态阈值 79.0，不开单", selected.reason)

    def test_daily_profile_uses_existing_direction_when_observe_direction_is_empty(self):
        now = shanghai_timestamp("2026-07-30T08:00:00")
        state = MonitorState(
            symbol="BTCUSDT",
            enable_daily_profile_selector=True,
            trade_score_threshold=0.0,
        )
        state.active_daily_profile_selection = {
            "version": "DPS-20260730-0800",
            "status": "READY",
            "effective_from": now,
            "effective_until": now + 86_400_000,
            "selected_profiles": [
                {"key": "10|rebound|drop_reclaim|LONG|WD-22"}
            ],
        }
        primary = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="B",
            reason="原方向未过动态阈值",
            price=100.0,
            open_time=now - 60_000,
            score=0.0,
            threshold=79.0,
            threshold_segment="WD-22",
            strategy_family="rebound",
            strategy_tag="drop_reclaim",
            observe_direction="",
        )

        selected, required = state._select_daily_profile_signal(primary, [], now)

        self.assertTrue(required)
        self.assertTrue(selected.actionable)
        self.assertTrue(selected.daily_profile_selected)
        self.assertEqual(selected.direction, "LONG")
        self.assertEqual(selected.threshold, 79.0)

    def test_promoted_daily_profile_is_still_blocked_by_wave_direction(self):
        now = shanghai_timestamp("2026-07-30T08:00:00")
        state = MonitorState(
            symbol="BTCUSDT",
            enable_daily_profile_selector=True,
            trade_score_threshold=0.0,
            enable_wave_guard=True,
        )
        state.active_daily_profile_selection = {
            "version": "DPS-20260730-0800",
            "status": "READY",
            "effective_from": now,
            "effective_until": now + 86_400_000,
            "selected_profiles": [
                {"key": "10|short_observe|generic_short_observe|SHORT|WD-22"}
            ],
        }
        primary = Signal(
            direction="WAIT",
            timeframe_minutes=10,
            level="B",
            reason="中位平量横盘：信号不足",
            price=100.0,
            open_time=now - 60_000,
            threshold=79.0,
            threshold_segment="WD-22",
            strategy_family="short_observe",
            strategy_tag="generic_short_observe",
            observe_direction="SHORT",
            observe_only=True,
        )
        up_wave = WaveSnapshot(
            state="UP_LEG",
            raw_state="UP_LEG",
            window=8,
            efficiency=0.8,
            direction_ratio=0.86,
            atr_strength=2.1,
            range_position=0.9,
            confirmations=2,
            allowed_directions=("LONG",),
            confirmed_at=now - 120_000,
        )

        baseline = Signal("WAIT", 10, "B", "无主信号", 100.0, now)
        selected, required = state._select_daily_profile_signal(baseline, [primary], now)
        guarded = state._apply_wave_guard(selected, up_wave)

        self.assertTrue(required)
        self.assertEqual(selected.direction, "SHORT")
        self.assertEqual(guarded.direction, "WAIT")
        self.assertEqual(guarded.wave_guard_status, "DIRECTION_BLOCKED")
        self.assertTrue(guarded.daily_profile_selected)

    def test_state_update_applies_daily_profile_before_wave_guard(self):
        state = MonitorState(symbol="BTCUSDT")
        primary = Signal(
            direction="WAIT",
            timeframe_minutes=10,
            level="B",
            reason="等待画像",
            price=100.0,
            open_time=0,
        )
        promoted = replace(
            primary,
            direction="SHORT",
            level="A",
            daily_profile_selected=True,
        )
        calls = []

        def select_profile(signal, observation_candidates, current_time):
            calls.append(("profile", signal.direction))
            return promoted, True

        def apply_wave(signal, wave):
            calls.append(("wave", signal.direction))
            return signal

        with (
            patch("app.state.choose_trade_signal", return_value=primary),
            patch.object(state, "_select_daily_profile_signal", side_effect=select_profile),
            patch.object(state, "_apply_wave_guard", side_effect=apply_wave),
            patch.object(state, "_maybe_open_order", return_value="OPENED"),
        ):
            state.update_from_klines(
                [Kline(0, 100.0, 100.0, 100.0, 100.0, 1.0, 59_999)]
            )

        self.assertEqual(calls, [("profile", "WAIT"), ("wave", "SHORT")])
        self.assertEqual(state.selected_signal.direction, "SHORT")

    def test_daily_selected_profile_verifies_actionable_same_direction_signal(self):
        now = shanghai_timestamp("2026-07-30T08:00:00")
        state = MonitorState(symbol="BTCUSDT", enable_daily_profile_selector=True)
        state.active_daily_profile_selection = {
            "version": "DPS-20260730-0800",
            "status": "READY",
            "effective_from": now,
            "effective_until": now + 86_400_000,
            "selected_profiles": [
                {
                    "key": "10|short_observe|generic_short_observe|SHORT|WD-22",
                    "direction": "SHORT",
                    "sample_size": 32,
                    "win_rate": 0.65625,
                    "ev": 1.8125,
                }
            ],
        }
        primary = Signal(
            direction="SHORT",
            timeframe_minutes=10,
            level="A",
            reason="实时SHORT已过线",
            price=100.0,
            open_time=now - 60_000,
            score=-84.0,
            threshold=79.0,
            threshold_segment="WD-22",
            strategy_family="short_observe",
            strategy_tag="generic_short_observe",
            observe_direction="SHORT",
            observe_only=False,
        )

        selected, required = state._select_daily_profile_signal(primary, [], now)

        self.assertTrue(required)
        self.assertEqual(selected.direction, "SHORT")
        self.assertTrue(selected.daily_profile_selected)
        self.assertEqual(selected.score, -84.0)

    def test_daily_selector_blocks_unselected_profile_but_keeps_observation(self):
        now = shanghai_timestamp("2026-07-30T08:00:00")
        state = MonitorState(symbol="BTCUSDT", enable_daily_profile_selector=True)
        state.active_daily_profile_selection = {
            "version": "DPS-20260730-0800",
            "status": "READY",
            "effective_from": now,
            "effective_until": now + 86_400_000,
            "selected_profiles": [{"key": "10|short_observe|other|SHORT|WD-02"}],
        }
        primary = Signal(
            direction="SHORT",
            timeframe_minutes=10,
            level="A",
            reason="实时SHORT已过线",
            price=100.0,
            open_time=now - 60_000,
            score=-84.0,
            threshold=79.0,
            threshold_segment="WD-01",
            strategy_family="short_observe",
            strategy_tag="generic_short_observe",
            observe_direction="SHORT",
            observe_only=False,
        )
        latest = Kline(now - 60_000, 100.0, 100.0, 100.0, 100.0, 1.0, now)

        selected, required = state._select_daily_profile_signal(primary, [], now)
        decision = state._maybe_open_order(selected, latest, daily_profile_required=required)

        self.assertTrue(required)
        self.assertFalse(selected.daily_profile_selected)
        self.assertEqual(decision, "DAILY_PROFILE_NOT_SELECTED")
        self.assertEqual(len(state.observations), 1)

    def test_daily_selector_executes_matching_research_observation_candidate(self):
        now = shanghai_timestamp("2026-07-30T08:00:00")
        state = MonitorState(symbol="BTCUSDT", enable_daily_profile_selector=True)
        state.active_daily_profile_selection = {
            "version": "DPS-20260730-0800",
            "status": "READY",
            "effective_from": now,
            "effective_until": now + 86_400_000,
            "selected_profiles": [
                {
                    "key": "10|failed_low|low_volume_reclaim_observe|LONG|WD-08",
                    "direction": "LONG",
                    "sample_size": 30,
                    "win_rate": 0.7,
                    "ev": 2.6,
                }
            ],
        }
        primary = Signal("WAIT", 10, "B", "无主信号", 100.0, now, threshold_segment="WD-08")
        candidate = Signal(
            "WAIT",
            10,
            "B",
            "低位放量承接观察",
            100.0,
            now,
            threshold_segment="WD-08",
            strategy_family="failed_low",
            strategy_tag="low_volume_reclaim_observe",
            observe_direction="LONG",
            observe_only=True,
        )

        selected, required = state._select_daily_profile_signal(primary, [candidate], now)
        latest = Kline(now - 60_000, 100.0, 100.0, 100.0, 100.0, 1.0, now)
        decision = state._maybe_open_order(
            selected,
            latest,
            daily_profile_required=required,
        )

        self.assertTrue(required)
        self.assertEqual(selected.direction, "LONG")
        self.assertEqual(selected.strategy_tag, "low_volume_reclaim_observe")
        self.assertTrue(selected.daily_profile_selected)
        self.assertTrue(selected.actionable)
        self.assertFalse(selected.observe_only)
        self.assertEqual(decision, "OPENED")
        self.assertEqual(state.simulator.orders[-1].strategy_tag, "low_volume_reclaim_observe")

    def test_profile_admission_fast_selects_existing_short_observation_and_recomputes_audit(self):
        current_time = 1_800_000_000_000
        policy = candidate_policy()
        state = adaptive_admission_state(
            "ACTIVE",
            current_time,
            profile_admission_policy=policy,
        )
        self.addCleanup(state.close)
        fast_key = "10|short_observe|fast_short|SHORT|WD-08"
        state.active_daily_profile_selection["selected_profiles"] = []
        state.active_daily_profile_selection["candidates"] = [
            {"key": fast_key, "win_rate": 0.59}
        ]
        state.adaptive_profile_states = {
            fast_key: adaptive_profile_snapshot(
                "ACTIVE",
                current_time - 1,
                profile_key=fast_key,
            )
        }
        primary = Signal("WAIT", 10, "B", "无主方向", 100.0, current_time)
        observation = Signal(
            "WAIT",
            10,
            "A",
            "已有SHORT观察候选",
            100.0,
            current_time,
            score=-84.0,
            threshold=79.0,
            threshold_segment="WD-08",
            strategy_family="short_observe",
            strategy_tag="fast_short",
            observe_direction="SHORT",
            observe_only=True,
        )

        selected, required = state._select_daily_profile_signal(
            primary,
            [observation],
            current_time,
        )

        self.assertTrue(required)
        self.assertEqual(selected.direction, "SHORT")
        self.assertFalse(selected.daily_profile_selected)
        self.assertFalse(selected.observe_only)
        admission = selected.adaptive_profile_state["admission"]
        self.assertEqual(admission["context"]["profile_key"], fast_key)
        self.assertEqual(admission["context"]["n12_wins"], 7)
        self.assertEqual(admission["decision"]["channel"], "FAST")
        self.assertEqual(admission["decision"]["code"], "FAST_ADMITTED")
        self.assertEqual(admission["decision"]["policy_version"], policy.version)
        self.assertEqual(admission["decision"]["policy_hash"], policy.policy_hash)
        self.assertFalse(admission["release_allowed"])

        admission["decision"]["policy_hash"] = "tampered"
        decision = state._maybe_open_order(
            selected,
            latest_kline(current_time),
            daily_profile_required=required,
        )

        self.assertEqual(decision, "OPENED")
        order = state.simulator.orders[-1]
        frozen = order.adaptive_profile_state["admission"]
        self.assertEqual(frozen["decision"]["policy_hash"], policy.policy_hash)
        self.assertEqual(frozen["decision"]["channel"], "FAST")
        self.assertEqual(frozen["context"]["candidate_ordinal"], 1)
        self.assertFalse(order.daily_profile_selected)
        runtime = json.loads(state._decision_runtime_config().canonical_payload)
        self.assertEqual(runtime["profiles"]["admission"]["policy_hash"], policy.policy_hash)
        self.assertFalse(runtime["profiles"]["admission"]["release_allowed"])
        self.assertFalse(state.snapshot()["profile_admission"]["release_allowed"])

    def test_profile_admission_does_not_create_fast_direction_from_primary_or_long_observation(self):
        current_time = 1_800_000_000_000
        state = adaptive_admission_state(
            "ACTIVE",
            current_time,
            profile_admission_policy=candidate_policy(),
        )
        self.addCleanup(state.close)
        state.active_daily_profile_selection["selected_profiles"] = []
        short_key = "10|short_observe|primary_short|SHORT|WD-08"
        long_key = "10|drop_reclaim|long_observe|LONG|WD-08"
        state.adaptive_profile_states = {
            short_key: adaptive_profile_snapshot(
                "ACTIVE", current_time - 1, profile_key=short_key
            ),
            long_key: adaptive_profile_snapshot(
                "ACTIVE", current_time - 1, profile_key=long_key
            ),
        }
        primary = Signal(
            "SHORT", 10, "A", "未入选主候选", 100.0, current_time,
            score=-84.0, threshold=79.0, threshold_segment="WD-08",
            strategy_family="short_observe", strategy_tag="primary_short",
            observe_direction="SHORT",
        )
        long_observation = Signal(
            "WAIT", 10, "B", "未入选LONG观察", 100.0, current_time,
            score=84.0, threshold=79.0, threshold_segment="WD-08",
            strategy_family="drop_reclaim", strategy_tag="long_observe",
            observe_direction="LONG", observe_only=True,
        )

        selected, required = state._select_daily_profile_signal(
            primary,
            [long_observation],
            current_time,
        )

        self.assertTrue(required)
        self.assertFalse(selected.daily_profile_selected)
        self.assertNotIn("admission", selected.adaptive_profile_state)
        candidate_audit = selected.adaptive_profile_state["admission_candidates"]
        self.assertEqual(len(candidate_audit), 1)
        self.assertEqual(
            candidate_audit[0]["decision"]["code"],
            "FAST_DIRECTION_BLOCKED",
        )

        decision = state._maybe_open_order(
            selected,
            latest_kline(current_time),
            daily_profile_required=required,
        )

        self.assertEqual(decision, "FAST_PRIMARY_BLOCKED")
        self.assertEqual(state.simulator.orders, [])
        frozen_candidates = state.selected_signal.decision_inputs["admission"][
            "profile_admission_candidates"
        ]
        self.assertEqual(
            frozen_candidates[0]["decision"]["code"],
            "FAST_DIRECTION_BLOCKED",
        )

    def test_profile_admission_fast_keeps_the_original_score_threshold(self):
        current_time = 1_800_000_000_000
        state = adaptive_admission_state(
            "ACTIVE",
            current_time,
            profile_admission_policy=candidate_policy(),
        )
        self.addCleanup(state.close)
        fast_key = "10|short_observe|fast_short|SHORT|WD-08"
        state.active_daily_profile_selection["selected_profiles"] = []
        state.adaptive_profile_states = {
            fast_key: adaptive_profile_snapshot(
                "ACTIVE", current_time - 1, profile_key=fast_key
            )
        }
        primary = Signal("WAIT", 10, "B", "无主方向", 100.0, current_time)
        observation = Signal(
            "WAIT", 10, "A", "评分不足的SHORT快速候选", 100.0, current_time,
            score=-70.0, threshold=79.0, threshold_segment="WD-08",
            strategy_family="short_observe", strategy_tag="fast_short",
            observe_direction="SHORT", observe_only=True,
        )

        selected, required = state._select_daily_profile_signal(
            primary, [observation], current_time
        )
        decision = state._maybe_open_order(
            selected,
            latest_kline(current_time),
            daily_profile_required=required,
        )

        self.assertEqual(selected.threshold, 79.0)
        self.assertEqual(decision, "BELOW_THRESHOLD")
        self.assertEqual(state.simulator.orders, [])

    def test_profile_admission_all_rejected_wait_persists_candidate_audit(self):
        current_time = 1_800_000_000_000
        state = adaptive_admission_state(
            "ACTIVE",
            current_time,
            profile_admission_policy=candidate_policy(),
        )
        self.addCleanup(state.close)
        state.active_daily_profile_selection["selected_profiles"] = []
        long_key = "10|drop_reclaim|blocked_long|LONG|WD-08"
        state.adaptive_profile_states = {
            long_key: adaptive_profile_snapshot(
                "ACTIVE", current_time - 1, profile_key=long_key
            )
        }
        primary = Signal("WAIT", 10, "B", "无主方向", 100.0, current_time)
        observation = Signal(
            "WAIT", 10, "B", "未入选LONG观察", 100.0, current_time,
            score=84.0, threshold=79.0, threshold_segment="WD-08",
            strategy_family="drop_reclaim", strategy_tag="blocked_long",
            observe_direction="LONG", observe_only=True,
        )

        selected, required = state._select_daily_profile_signal(
            primary, [observation], current_time
        )
        decision = state._maybe_open_order(
            selected,
            latest_kline(current_time),
            daily_profile_required=required,
        )

        self.assertEqual(decision, "SESSION_BLOCKED")
        self.assertEqual(state.simulator.orders, [])
        frozen_candidates = state.selected_signal.decision_inputs["admission"][
            "profile_admission_candidates"
        ]
        self.assertEqual(
            frozen_candidates[0]["decision"]["code"],
            "FAST_DIRECTION_BLOCKED",
        )

    def test_profile_admission_overheated_resident_falls_through_to_next_resident(self):
        current_time = 1_800_000_000_000
        state = adaptive_admission_state(
            "ACTIVE",
            current_time,
            profile_admission_policy=candidate_policy(),
        )
        self.addCleanup(state.close)
        next_key = "10|drop_reclaim|next_profile|LONG|WD-08"
        first_qualification = state.active_daily_profile_selection["selected_profiles"][0]
        next_qualification = {
            **first_qualification,
            "key": next_key,
            "win_rate": 0.61,
        }
        state.active_daily_profile_selection["selected_profiles"] = [
            first_qualification,
            next_qualification,
        ]
        overheated = adaptive_profile_snapshot("ACTIVE", current_time - 1)
        overheated["n12"]["wins"] = 9
        overheated["n12"]["losses"] = 3
        admitted = adaptive_profile_snapshot(
            "ACTIVE", current_time - 1, profile_key=next_key
        )
        state.adaptive_profile_states = {PROFILE_KEY: overheated, next_key: admitted}
        primary = selected_profile_signal(current_time)
        observation = replace(
            primary,
            direction="WAIT",
            observe_direction="LONG",
            observe_only=True,
            profile_key="",
            daily_profile_selected=False,
            strategy_tag="next_profile",
        )

        selected, required = state._select_daily_profile_signal(
            primary,
            [observation],
            current_time,
        )

        self.assertTrue(required)
        self.assertEqual(selected.profile_key, next_key)
        self.assertTrue(selected.daily_profile_selected)
        self.assertEqual(
            selected.adaptive_profile_state["admission"]["decision"]["channel"],
            "RESIDENT",
        )
        candidate_audit = selected.adaptive_profile_state["admission_candidates"]
        self.assertEqual(
            [item["decision"]["code"] for item in candidate_audit],
            ["RESIDENT_N12_OVERHEATED", "RESIDENT_ADMITTED"],
        )
        self.assertEqual(candidate_audit[0]["context"]["n12_wins"], 9)
        self.assertEqual(candidate_audit[1]["context"]["profile_key"], next_key)

    def test_profile_admission_fast_never_consumes_progression_and_rechecks_current_state(self):
        current_time = 1_800_000_000_000
        policy = candidate_policy()
        state = adaptive_admission_state(
            "ACTIVE",
            current_time,
            profile_admission_policy=policy,
        )
        self.addCleanup(state.close)
        fast_key = "10|short_observe|fast_short|SHORT|WD-08"
        state.active_daily_profile_selection["selected_profiles"] = []
        state.adaptive_profile_states = {
            fast_key: adaptive_profile_snapshot(
                "ACTIVE", current_time - 1, profile_key=fast_key
            )
        }
        primary = Signal("WAIT", 10, "B", "无主方向", 100.0, current_time)
        observation = Signal(
            "WAIT", 10, "A", "已有SHORT观察候选", 100.0, current_time,
            score=-84.0, threshold=79.0, threshold_segment="WD-08",
            strategy_family="short_observe", strategy_tag="fast_short",
            observe_direction="SHORT", observe_only=True,
        )
        selected, required = state._select_daily_profile_signal(
            primary, [observation], current_time
        )
        pending = StakeProgressionCredit(
            source_order_id=77,
            created_at=current_time - 1,
            direction="SHORT",
        )
        state.simulator.stake_progression.credits.append(pending)

        decision = state._maybe_open_order(
            selected,
            latest_kline(current_time),
            daily_profile_required=required,
        )

        self.assertEqual(decision, "OPENED")
        self.assertEqual(state.simulator.orders[-1].stake, 10.0)
        self.assertFalse(
            state.simulator.orders[-1].decision_inputs["admission"]["stake"]
            ["selected_order_terms"]["allow_progression"]
        )
        self.assertEqual(pending.status, "PENDING")

        next_time = current_time + MINUTE_MS
        second_state = adaptive_admission_state(
            "ACTIVE",
            next_time,
            profile_admission_policy=policy,
        )
        self.addCleanup(second_state.close)
        second_state.active_daily_profile_selection["selected_profiles"] = []
        second_state.adaptive_profile_states = {
            fast_key: adaptive_profile_snapshot(
                "ACTIVE", next_time - 1, profile_key=fast_key
            )
        }
        stale, required = second_state._select_daily_profile_signal(
            primary, [replace(observation, open_time=next_time)], next_time
        )
        second_state.adaptive_profile_states[fast_key] = adaptive_profile_snapshot(
            "WARMUP", next_time, profile_key=fast_key
        )

        blocked = second_state._maybe_open_order(
            stale,
            latest_kline(next_time),
            daily_profile_required=required,
        )

        self.assertEqual(blocked, "FAST_STATE_BLOCKED")
        self.assertEqual(second_state.simulator.orders, [])

    def test_profile_admission_rechecks_fast_second_slot_before_order_terms(self):
        current_time = 1_800_000_000_000
        state = adaptive_admission_state(
            "ACTIVE",
            current_time,
            profile_admission_policy=candidate_policy(),
        )
        self.addCleanup(state.close)
        fast_key = "10|short_observe|fast_short|SHORT|WD-08"
        state.active_daily_profile_selection["selected_profiles"] = []
        state.adaptive_profile_states = {
            fast_key: adaptive_profile_snapshot(
                "ACTIVE", current_time - 1, profile_key=fast_key
            )
        }
        primary = Signal("WAIT", 10, "B", "无主方向", 100.0, current_time)
        observation = Signal(
            "WAIT", 10, "A", "已有SHORT观察候选", 100.0, current_time,
            score=-84.0, threshold=79.0, threshold_segment="WD-08",
            strategy_family="short_observe", strategy_tag="fast_short",
            observe_direction="SHORT", observe_only=True,
        )
        selected, required = state._select_daily_profile_signal(
            primary, [observation], current_time
        )
        state.simulator.orders.append(
            SimulatedOrder(
                id=91,
                direction="SHORT",
                timeframe_minutes=10,
                level="A",
                reason="选择后出现的同方向订单",
                entry_price=100.0,
                opened_at=current_time - MINUTE_MS,
                expires_at=current_time + 9 * MINUTE_MS,
            )
        )

        blocked = state._maybe_open_order(
            selected,
            latest_kline(current_time),
            daily_profile_required=required,
        )

        self.assertEqual(blocked, "FAST_SECOND_ORDER_BLOCKED")
        self.assertIn("禁止同方向第二席位", state.risk_pause)
        self.assertEqual(len(state.simulator.orders), 1)
        self.assertEqual(state.selected_signal.first_decisive_block, "ADAPTIVE_PROFILE")

    def test_daily_selector_uses_previous_profiles_when_evaluation_save_fails(self):
        current = shanghai_timestamp("2026-07-30T08:00:00")
        storage = FailingDailySelectionStorage()
        storage.daily_profile_selections.append(
            (
                "BTCUSDT",
                {
                    "version": "DPS-20260729-0800",
                    "status": "READY",
                    "effective_from": shanghai_timestamp("2026-07-29T08:00:00"),
                    "effective_until": current,
                    "selected_profiles": [{"key": "10|family|tag|LONG|WD-01"}],
                },
            )
        )
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            enable_daily_profile_selector=True,
        )

        state._refresh_daily_profile_selection(current)

        self.assertEqual(state.daily_profile_selection["status"], "FALLBACK")
        self.assertEqual(state.active_daily_profile_selection["status"], "FALLBACK")
        self.assertEqual(
            state.active_daily_profile_selection["selected_profiles"][0]["key"],
            "10|family|tag|LONG|WD-01",
        )
        self.assertIn("database unavailable", state.daily_profile_selection["reason"])

    def test_daily_selector_fallback_uses_current_as_of_snapshot_not_future_latest(self):
        current = shanghai_timestamp("2026-07-30T08:00:00")
        cutoff = shanghai_timestamp("2026-07-30T07:50:00")
        storage = FailingDailySelectionStorage()

        def snapshot(version, evaluation_key, selected_key):
            return {
                "version": version,
                "status": "READY",
                "evaluated_at": evaluation_key,
                "evaluation_key": evaluation_key,
                "lookback_end": evaluation_key,
                "effective_from": evaluation_key + 10 * 60_000,
                "effective_until": evaluation_key + 86_400_000 + 10 * 60_000,
                "selected_profiles": [{"key": selected_key}],
                "selected_count": 1,
            }

        storage.daily_profile_selections.extend(
            [
                ("BTCUSDT", snapshot("PAST", cutoff - 86_400_000, "past")),
                ("BTCUSDT", snapshot("CURRENT", cutoff, "current")),
                ("BTCUSDT", snapshot("FUTURE", cutoff + 86_400_000, "future")),
            ]
        )
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            enable_daily_profile_selector=True,
        )
        self.addCleanup(state.close)
        state._refresh_daily_profile_selection(current)

        self.assertEqual(state.daily_profile_selection["status"], "FALLBACK")
        self.assertEqual(state.daily_profile_selection["evaluation_key"], cutoff)
        self.assertEqual(
            state.daily_profile_selection["selected_profiles"],
            [{"key": "current"}],
        )
        self.assertNotIn("future", json.dumps(state.daily_profile_selection))

    def test_daily_selector_save_failure_with_only_future_snapshot_is_empty_error(self):
        current = shanghai_timestamp("2026-07-30T08:00:00")
        cutoff = shanghai_timestamp("2026-07-30T07:50:00")
        future_key = cutoff + 86_400_000
        storage = FailingDailySelectionStorage()
        storage.daily_profile_selections.append(
            (
                "BTCUSDT",
                {
                    "version": "FUTURE",
                    "status": "READY",
                    "evaluated_at": future_key,
                    "evaluation_key": future_key,
                    "lookback_end": future_key,
                    "effective_from": current + 86_400_000,
                    "effective_until": current + 2 * 86_400_000,
                    "selected_profiles": [{"key": "future"}],
                    "selected_count": 1,
                },
            )
        )
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            enable_daily_profile_selector=True,
        )
        self.addCleanup(state.close)
        state.active_daily_profile_selection = {
            **storage.daily_profile_selections[-1][1],
            "effective_from": current,
            "effective_until": current + 86_400_000,
        }

        state._refresh_daily_profile_selection(current)

        self.assertEqual(state.daily_profile_selection["status"], "ERROR")
        self.assertEqual(state.daily_profile_selection["evaluation_key"], cutoff)
        self.assertEqual(state.daily_profile_selection["selected_profiles"], [])
        self.assertIsNone(state.active_daily_profile_selection)

    def test_daily_selector_clock_rollback_uses_nearest_snapshot_before_replayed_day(self):
        current = shanghai_timestamp("2026-07-30T08:00:00")
        cutoff = shanghai_timestamp("2026-07-30T07:50:00")
        storage = FailingDailySelectionStorage()
        for day_offset, version in ((-2, "OLDER"), (-1, "NEAREST")):
            evaluation_key = cutoff + day_offset * 86_400_000
            storage.daily_profile_selections.append(
                (
                    "BTCUSDT",
                    {
                        "version": version,
                        "status": "READY",
                        "evaluated_at": evaluation_key,
                        "evaluation_key": evaluation_key,
                        "lookback_end": evaluation_key,
                        "effective_from": evaluation_key + 10 * 60_000,
                        "effective_until": evaluation_key + 86_400_000 + 10 * 60_000,
                        "selected_profiles": [{"key": version.lower()}],
                        "selected_count": 1,
                    },
                )
            )
        storage.daily_profile_selections.append(
            (
                "BTCUSDT",
                {
                    "version": "FUTURE-SAME-EVALUATION",
                    "status": "READY",
                    "evaluated_at": current + 60 * 60_000,
                    "evaluation_key": cutoff,
                    "lookback_end": cutoff,
                    "effective_from": current,
                    "effective_until": current + 86_400_000,
                    "selected_profiles": [{"key": "future"}],
                    "selected_count": 1,
                },
            )
        )
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            enable_daily_profile_selector=True,
        )
        self.addCleanup(state.close)

        state._refresh_daily_profile_selection(current)

        self.assertTrue(state.daily_profile_selection["version"].startswith("NEAREST"))
        self.assertEqual(
            state.daily_profile_selection["selected_profiles"],
            [{"key": "nearest"}],
        )
        self.assertLessEqual(state.daily_profile_selection["evaluation_key"], cutoff)

    def test_legacy_daily_selector_rejects_same_key_future_evaluation_before_save_fallback(self):
        current = shanghai_timestamp("2026-07-30T08:00:00")
        cutoff = shanghai_timestamp("2026-07-30T07:50:00")
        storage = LegacyFailingDailySelectionStorage()
        future = {
            "version": "SAME-KEY-FUTURE",
            "status": "READY",
            "evaluation_key": cutoff,
            "lookback_end": cutoff,
            "evaluated_at": current + 1,
            "effective_from": current,
            "effective_until": current + 86_400_000,
            "selected_profiles": [{"key": "future-without-row-time"}],
            "selected_count": 1,
        }
        storage.daily_profile_selections.append(("BTCUSDT", future))
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            enable_daily_profile_selector=True,
        )
        self.addCleanup(state.close)

        with patch("app.state.build_daily_selection", wraps=build_daily_selection) as builder:
            state._refresh_daily_profile_selection(current)

        self.assertIsNone(builder.call_args.kwargs["previous_snapshot"])
        self.assertEqual(state.daily_profile_selection["status"], "ERROR")
        self.assertEqual(state.daily_profile_selection["selected_profiles"], [])
        self.assertIsNone(state.active_daily_profile_selection)

    def test_daily_profile_as_of_rejects_present_invalid_time_fields_but_allows_missing_fields(self):
        current = shanghai_timestamp("2026-07-30T08:00:00")
        cutoff = shanghai_timestamp("2026-07-30T07:50:00")
        fields = (
            "evaluation_key",
            "lookback_start",
            "lookback_end",
            "evaluated_at",
            "evaluation_time",
            "effective_from",
            "effective_until",
        )
        valid = {
            "evaluation_key": cutoff,
            "lookback_start": cutoff - 7 * 86_400_000,
            "lookback_end": cutoff,
            "evaluated_at": current,
            "evaluation_time": current,
            "effective_from": current,
            "effective_until": current + 86_400_000,
        }
        limits = {
            "evaluation_key": cutoff,
            "evaluated_at": current,
            "effective_from": current,
            "effective_until": current + 86_400_000,
        }
        invalid_values = (
            None,
            "",
            "not-a-timestamp",
            float("nan"),
            float("inf"),
            float("-inf"),
            True,
            False,
        )

        for field in fields:
            for invalid in invalid_values:
                with self.subTest(field=field, invalid=repr(invalid)):
                    self.assertFalse(
                        MonitorState._is_snapshot_as_of(
                            {**valid, field: invalid},
                            **limits,
                        )
                    )
            with self.subTest(field=field, value="missing"):
                missing = dict(valid)
                missing.pop(field)
                self.assertTrue(MonitorState._is_snapshot_as_of(missing, **limits))
            with self.subTest(field=field, value="numeric string"):
                self.assertTrue(
                    MonitorState._is_snapshot_as_of(
                        {**valid, field: str(valid[field])},
                        **limits,
                    )
                )

    def test_legacy_daily_selector_invalid_effective_times_refresh_to_inactive_error(self):
        current = shanghai_timestamp("2026-07-30T08:00:00")
        cutoff = shanghai_timestamp("2026-07-30T07:50:00")
        config = DailyProfileSelectorConfig().normalized()
        invalid_values = (
            None,
            "",
            "not-a-timestamp",
            float("nan"),
            float("inf"),
            True,
        )
        for field in ("effective_from", "effective_until"):
            for invalid in invalid_values:
                with self.subTest(field=field, invalid=repr(invalid)):
                    storage = LegacyFailingDailySelectionStorage()
                    snapshot = {
                        "version": "INVALID-EFFECTIVE-TIME",
                        "status": "READY",
                        "evaluation_key": cutoff,
                        "lookback_end": cutoff,
                        "evaluated_at": current,
                        "effective_from": current,
                        "effective_until": current + 86_400_000,
                        "config": dict(config.__dict__),
                        "selected_profiles": [
                            {"key": "invalid-effective-without-row-time"}
                        ],
                        "selected_count": 1,
                        field: invalid,
                    }
                    storage.daily_profile_selections.append(("BTCUSDT", snapshot))
                    state = MonitorState(
                        symbol="BTCUSDT",
                        storage=storage,
                        enable_daily_profile_selector=True,
                        daily_profile_selector_config=config,
                    )
                    self.addCleanup(state.close)

                    state._refresh_daily_profile_selection(current)

                    self.assertEqual(state.daily_profile_selection["status"], "ERROR")
                    self.assertEqual(state.daily_profile_selection["selected_profiles"], [])
                    self.assertIsNone(state.active_daily_profile_selection)

    def test_legacy_daily_selector_rejects_any_future_or_unparseable_top_level_time(self):
        current = shanghai_timestamp("2026-07-30T08:00:00")
        cutoff = shanghai_timestamp("2026-07-30T07:50:00")
        cases = {
            "future lookback start": {"lookback_start": cutoff + 1},
            "future lookback": {"lookback_end": cutoff + 1},
            "future effective": {"effective_from": current + 1},
            "future effective until": {"effective_until": current + 86_400_001},
            "future evaluation time": {"evaluation_time": current + 1},
            "fractional future evaluation": {"evaluated_at": current + 0.5},
            "boolean evaluation": {"evaluated_at": True},
            "unparseable evaluated at": {"evaluated_at": "not-a-timestamp"},
            "unparseable effective until": {"effective_until": "not-a-timestamp"},
        }
        for label, override in cases.items():
            with self.subTest(label=label):
                storage = LegacyFailingDailySelectionStorage()
                snapshot = {
                    "version": "UNSAFE",
                    "status": "READY",
                    "evaluation_key": cutoff,
                    "lookback_end": cutoff,
                    "evaluated_at": cutoff,
                    "effective_from": current,
                    "effective_until": current + 86_400_000,
                    "selected_profiles": [{"key": "unsafe-without-row-time"}],
                    "selected_count": 1,
                    **override,
                }
                storage.daily_profile_selections.append(("BTCUSDT", snapshot))
                state = MonitorState(
                    symbol="BTCUSDT",
                    storage=storage,
                    enable_daily_profile_selector=True,
                )
                self.addCleanup(state.close)

                with patch(
                    "app.state.build_daily_selection",
                    wraps=build_daily_selection,
                ) as builder:
                    state._refresh_daily_profile_selection(current)

                self.assertIsNone(builder.call_args.kwargs["previous_snapshot"])
                self.assertEqual(state.daily_profile_selection["status"], "ERROR")
                self.assertEqual(state.daily_profile_selection["selected_profiles"], [])
                self.assertIsNone(state.active_daily_profile_selection)

    def test_legacy_daily_selector_accepts_provably_past_latest_snapshot(self):
        current = shanghai_timestamp("2026-07-30T08:00:00")
        cutoff = shanghai_timestamp("2026-07-30T07:50:00")
        previous_key = cutoff - 86_400_000
        storage = LegacyFailingDailySelectionStorage()
        previous = {
            "version": "PAST",
            "status": "READY",
            "evaluation_key": previous_key,
            "lookback_end": previous_key,
            "evaluated_at": previous_key,
            "evaluation_time": previous_key,
            "effective_from": current - 86_400_000,
            "effective_until": current,
            "selected_profiles": [{"key": "past-without-row-time"}],
            "selected_count": 1,
        }
        storage.daily_profile_selections.append(("BTCUSDT", previous))
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            enable_daily_profile_selector=True,
        )
        self.addCleanup(state.close)

        with patch("app.state.build_daily_selection", wraps=build_daily_selection) as builder:
            state._refresh_daily_profile_selection(current)

        self.assertIs(builder.call_args.kwargs["previous_snapshot"], previous)
        self.assertEqual(state.daily_profile_selection["status"], "FALLBACK")
        self.assertEqual(
            state.daily_profile_selection["selected_profiles"],
            [{"key": "past-without-row-time"}],
        )

    def test_legacy_daily_selector_with_only_future_latest_snapshot_fails_empty(self):
        current = shanghai_timestamp("2026-07-30T08:00:00")
        cutoff = shanghai_timestamp("2026-07-30T07:50:00")
        future_key = cutoff + 86_400_000
        storage = LegacyFailingDailySelectionStorage()
        storage.daily_profile_selections.append(
            (
                "BTCUSDT",
                {
                    "version": "ONLY-FUTURE",
                    "status": "READY",
                    "evaluation_key": future_key,
                    "lookback_end": future_key,
                    "evaluated_at": future_key,
                    "effective_from": current + 86_400_000,
                    "effective_until": current + 2 * 86_400_000,
                    "selected_profiles": [{"key": "future-without-row-time"}],
                    "selected_count": 1,
                },
            )
        )
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            enable_daily_profile_selector=True,
        )
        self.addCleanup(state.close)

        with patch("app.state.build_daily_selection", wraps=build_daily_selection) as builder:
            state._refresh_daily_profile_selection(current)

        self.assertIsNone(builder.call_args.kwargs["previous_snapshot"])
        self.assertEqual(state.daily_profile_selection["status"], "ERROR")
        self.assertEqual(state.daily_profile_selection["selected_profiles"], [])
        self.assertIsNone(state.active_daily_profile_selection)

    def test_state_restores_persisted_orders_from_sqlite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            klines = actionable_rebound_klines()
            state = MonitorState(
                symbol="BTCUSDT", storage_path=db_path, enable_wave_guard=False
            )
            state.update_from_klines(klines)
            state.wait_for_storage_writes()

            restored = MonitorState(symbol="BTCUSDT", storage_path=db_path)
            snapshot = restored.snapshot()

        self.assertEqual(snapshot["stats"]["total_orders"], 1)
        self.assertEqual(snapshot["orders"][0]["status"], "OPEN")

    def test_segment_losses_are_left_to_explicit_risk_guards(self):
        state = MonitorState(
            symbol="BTCUSDT",
            result_sequence_guard_config=ResultSequenceGuardConfig(enabled=False),
        )
        for idx in range(3):
            state.simulator.orders.append(
                SimulatedOrder(
                    id=idx + 1,
                    direction="LONG",
                    timeframe_minutes=10,
                    level="A",
                    reason="loss",
                    entry_price=100.0,
                    opened_at=1_800_000 + idx * 600_000,
                    expires_at=2_400_000 + idx * 600_000,
                    threshold_segment="WD-00",
                    status="SETTLED",
                    result="LOSS",
                    exit_price=99.0,
                    settled_at=2_400_000 + idx * 600_000,
                    pnl=-10.0,
                )
            )
        signal = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="new",
            price=100.0,
            open_time=4_200_000,
            score=90.0,
            threshold=70.0,
            threshold_segment="WD-00",
            session_allowed=True,
        )
        state.risk_pause = "stale risk pause"

        decision = state._maybe_open_order(signal, kline(70, 100.0, 100))

        self.assertEqual(decision, "OPENED")
        self.assertEqual(state.snapshot()["risk_pause"], "")

    def test_result_sequence_guard_pauses_only_the_losing_direction(self):
        state = MonitorState(
            symbol="BTCUSDT",
            result_sequence_guard_config=ResultSequenceGuardConfig(
                loss_streak=3,
                cooldown_minutes=20,
                scope="DIRECTION",
            ),
        )
        for idx, segment in enumerate(("WD-00", "WD-01", "WD-03")):
            state.simulator.orders.append(
                SimulatedOrder(
                    id=idx + 1,
                    direction="SHORT",
                    timeframe_minutes=10,
                    level="B",
                    reason="历史亏损",
                    entry_price=100.0,
                    opened_at=300_000 + idx * 120_000,
                    expires_at=900_000 + idx * 120_000,
                    threshold_segment=segment,
                    status="SETTLED",
                    result="LOSS",
                    exit_price=101.0,
                    settled_at=900_000 + idx * 120_000,
                    pnl=-10.0,
                )
            )
        latest = kline(20, 100.0, 100)
        short_signal = Signal(
            direction="SHORT",
            timeframe_minutes=10,
            level="B",
            reason="新 SHORT",
            price=100.0,
            open_time=latest.close_time,
            score=-90.0,
            threshold=70.0,
            threshold_segment="WD-02",
            session_allowed=True,
        )
        long_signal = replace(
            short_signal,
            direction="LONG",
            reason="新 LONG",
            score=90.0,
        )

        short_decision = state._maybe_open_order(short_signal, latest)
        short_status = state.snapshot()["result_sequence_guard"]
        short_pause = state.risk_pause
        long_decision = state._maybe_open_order(long_signal, latest)

        self.assertEqual(short_decision, "RESULT_SEQUENCE_GUARD_BLOCKED")
        self.assertIn("连续亏损 3 单", short_pause)
        self.assertEqual(short_status["status"], "PAUSED")
        self.assertEqual(short_status["paused_directions"], ["SHORT"])
        self.assertEqual(long_decision, "OPENED")

    def test_daily_drawdown_does_not_pause_when_segment_is_not_losing(self):
        state = MonitorState(
            symbol="BTCUSDT",
            result_sequence_guard_config=ResultSequenceGuardConfig(enabled=False),
        )
        for idx in range(4):
            state.simulator.orders.append(
                SimulatedOrder(
                    id=idx + 1,
                    direction="LONG",
                    timeframe_minutes=10,
                    level="A",
                    reason="loss",
                    entry_price=100.0,
                    opened_at=1_800_000 + idx * 600_000,
                    expires_at=2_400_000 + idx * 600_000,
                    threshold_segment=f"WD-{idx + 1:02d}",
                    status="SETTLED",
                    result="LOSS",
                    exit_price=99.0,
                    settled_at=2_400_000 + idx * 600_000,
                    pnl=-10.0,
                )
            )
        signal = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="new",
            price=100.0,
            open_time=4_200_000,
            score=90.0,
            threshold=70.0,
            threshold_segment="WD-00",
            session_allowed=True,
        )

        decision = state._maybe_open_order(signal, kline(70, 100.0, 100))

        self.assertEqual(decision, "OPENED")
        self.assertEqual(state.snapshot()["risk_pause"], "")

    def test_state_preserves_warmup_history_when_live_poll_updates_arrive(self):
        state = MonitorState(symbol="BTCUSDT", max_klines=200)
        warmup = [kline(i, 100.0 + i * 0.01, 100) for i in range(100)]

        state.seed_klines(
            warmup,
            {
                "status": "READY",
                "loaded_klines": len(warmup),
                "cached_files": ["BTCUSDT-1m-2026-04.zip"],
                "downloaded_files": [],
                "errors": [],
            },
        )
        state.update_from_klines([kline(i, 101.0 + i * 0.01, 120) for i in range(95, 105)])

        snapshot = state.snapshot()
        self.assertEqual(snapshot["kline_count"], 105)
        self.assertEqual(snapshot["warmup"]["loaded_klines"], 100)
        self.assertEqual(snapshot["latest_kline"]["open_time"], kline(104, 0, 0).open_time)

    def test_update_opens_only_one_selected_duration(self):
        klines = actionable_rebound_klines()
        state = MonitorState(symbol="BTCUSDT", enable_wave_guard=False)

        state.update_from_klines(klines)
        state.update_from_klines(klines)

        snapshot = state.snapshot()
        self.assertEqual(snapshot["symbol"], "BTCUSDT")
        self.assertEqual(snapshot["stats"]["total_orders"], 1)
        self.assertEqual(snapshot["orders"][0]["timeframe_minutes"], 10)
        self.assertEqual([signal["timeframe_minutes"] for signal in snapshot["signals"]], [10])
        self.assertGreaterEqual(abs(snapshot["selected_signal"]["score"]), snapshot["selected_signal"]["threshold"])

    def test_state_sends_webhook_only_when_order_opens(self):
        klines = actionable_rebound_klines()
        webhook = RecordingWebhook()
        state = MonitorState(symbol="BTCUSDT", webhook=webhook, enable_wave_guard=False)

        state.update_from_klines(klines)
        state.update_from_klines(klines)

        self.assertEqual(len(webhook.calls), 1)
        self.assertEqual(webhook.calls[0][0], "BTCUSDT")
        self.assertIn(webhook.calls[0][1], {"LONG", "SHORT"})
        self.assertEqual(webhook.calls[0][2], 10)
        self.assertEqual(webhook.calls[0][3], state.snapshot()["orders"][0]["reason"])
        self.assertEqual(webhook.calls[0][4], state.snapshot()["orders"][0]["stake"])
        self.assertTrue(state.snapshot()["webhook"]["enabled"])

    def test_time_period_guard_records_shadow_without_real_order_or_webhook(self):
        webhook = RecordingWebhook()
        storage = RecordingStorage()
        state = profile_guard_state(
            webhook=webhook,
            storage=storage,
            time_period_guard_config=TimePeriodGuardConfig(enabled=True),
        )
        opened_at = shanghai_timestamp("2026-08-14 12:00:00")

        decision = state._maybe_open_order(
            selected_profile_signal(opened_at),
            latest_kline(opened_at),
        )
        state.wait_for_storage_writes()
        snapshot = state.snapshot()

        self.assertEqual(decision, "TIME_PERIOD_SHADOW_ONLY")
        self.assertEqual(snapshot["stats"]["total_orders"], 0)
        self.assertEqual(webhook.calls, [])
        self.assertTrue(snapshot["time_period_guard"]["blocked"])
        self.assertIn("12:00-18:00", snapshot["risk_pause"])
        self.assertEqual(len(snapshot["observations"]), 1)
        self.assertEqual(
            snapshot["observations"][0]["source_decision"],
            "TIME_PERIOD_SHADOW_ONLY",
        )
        self.assertEqual(len(storage.observations), 1)

        settled = state._settle_observations(opened_at + 10 * MINUTE_MS, 101.0)

        self.assertEqual(len(settled), 1)
        self.assertEqual(settled[0].result, "WIN")

    def test_time_period_guard_allows_order_at_six_pm_boundary(self):
        state = profile_guard_state(
            time_period_guard_config=TimePeriodGuardConfig(enabled=True),
        )
        opened_at = shanghai_timestamp("2026-08-14 18:00:00")

        decision = state._maybe_open_order(
            selected_profile_signal(opened_at),
            latest_kline(opened_at),
        )

        self.assertEqual(decision, "OPENED")
        self.assertEqual(state.snapshot()["stats"]["total_orders"], 1)

    def test_disabled_time_period_guard_restores_noon_order_path(self):
        state = profile_guard_state(
            time_period_guard_config=TimePeriodGuardConfig(enabled=False),
        )
        opened_at = shanghai_timestamp("2026-08-14 12:00:00")

        decision = state._maybe_open_order(
            selected_profile_signal(opened_at),
            latest_kline(opened_at),
        )

        self.assertEqual(decision, "OPENED")
        self.assertFalse(state.snapshot()["time_period_guard"]["enabled"])
        self.assertEqual(state.snapshot()["stats"]["total_orders"], 1)

    def test_profile_health_degraded_blocks_selected_direction_and_records_audit(self):
        current_time = shanghai_timestamp("2026-08-15 10:30:00")
        evaluation_at = shanghai_timestamp("2026-08-15 08:00:00")
        state = profile_guard_state()
        activate_profile_health_selection(state, current_time)
        state.observations = profile_health_observations(
            evaluation_at,
            "WWWWWLLLLLLL",
        )

        decision = state._maybe_open_order(
            selected_profile_signal(current_time),
            latest_kline(current_time),
        )
        snapshot = state.snapshot()

        self.assertEqual(decision, "PROFILE_HEALTH_BLOCKED")
        self.assertEqual(snapshot["stats"]["total_orders"], 0)
        self.assertEqual(snapshot["profile_health_guard"]["status"], "DEGRADED")
        self.assertEqual(snapshot["profile_health_guard"]["sample_size"], 12)
        self.assertTrue(snapshot["profile_health_guard"]["blocked"])
        self.assertEqual(snapshot["selected_signal"]["profile_health_status"], "DEGRADED")
        self.assertEqual(snapshot["observations"][0]["profile_health_status"], "DEGRADED")

    def test_profile_health_watch_opens_base_first_order_without_consuming_credit(self):
        current_time = shanghai_timestamp("2026-08-15 10:30:00")
        evaluation_at = shanghai_timestamp("2026-08-15 08:00:00")
        state = profile_guard_state()
        activate_profile_health_selection(state, current_time)
        state.observations = profile_health_observations(
            evaluation_at,
            "WWWWWWLLLLLL",
        )
        source_opened_at = current_time - 20 * MINUTE_MS
        source = state.simulator.open_order(
            selected_profile_signal(source_opened_at),
            entry_price=100.0,
            opened_at=source_opened_at,
        )
        state.simulator.settle_expired_orders(source.expires_at, 101.0)
        self.assertEqual(state.simulator.stake_progression.credits[0].status, "PENDING")

        decision = state._maybe_open_order(
            selected_profile_signal(current_time),
            latest_kline(current_time),
        )
        opened = state.simulator.orders[-1]

        self.assertEqual(decision, "OPENED")
        self.assertEqual(opened.stake, 10.0)
        self.assertEqual(opened.stake_progression_step, 1)
        self.assertEqual(opened.profile_health_status, "WATCH")
        self.assertEqual(state.simulator.stake_progression.credits[0].status, "PENDING")

    def test_profile_health_watch_blocks_same_direction_second_order(self):
        current_time = shanghai_timestamp("2026-08-15 10:30:00")
        evaluation_at = shanghai_timestamp("2026-08-15 08:00:00")
        state = profile_guard_state(max_open_long_orders=2)
        activate_profile_health_selection(state, current_time)
        state.observations = profile_health_observations(
            evaluation_at,
            "WWWWWWLLLLLL",
        )
        state.simulator.open_order(
            selected_profile_signal(current_time - 2 * MINUTE_MS),
            entry_price=100.0,
            opened_at=current_time - 2 * MINUTE_MS,
        )

        decision = state._maybe_open_order(
            selected_profile_signal(current_time),
            latest_kline(current_time),
        )

        self.assertEqual(decision, "PROFILE_HEALTH_SECOND_ORDER_BLOCKED")
        self.assertEqual(len(state.simulator.orders), 1)
        self.assertEqual(state.snapshot()["selected_signal"]["order_slot"], "SECOND")

    def test_profile_health_healthy_preserves_progression(self):
        current_time = shanghai_timestamp("2026-08-15 10:30:00")
        evaluation_at = shanghai_timestamp("2026-08-15 08:00:00")
        state = profile_guard_state()
        activate_profile_health_selection(state, current_time)
        state.observations = profile_health_observations(
            evaluation_at,
            "WWWWWWWLLLLL",
        )
        source_opened_at = current_time - 20 * MINUTE_MS
        source = state.simulator.open_order(
            selected_profile_signal(source_opened_at),
            entry_price=100.0,
            opened_at=source_opened_at,
        )
        state.simulator.settle_expired_orders(source.expires_at, 101.0)

        decision = state._maybe_open_order(
            selected_profile_signal(current_time),
            latest_kline(current_time),
        )
        opened = state.simulator.orders[-1]

        self.assertEqual(decision, "OPENED")
        self.assertEqual(opened.stake, 18.0)
        self.assertEqual(opened.stake_progression_step, 2)
        self.assertEqual(opened.profile_health_status, "HEALTHY")

    def test_disabled_profile_health_guard_preserves_selected_profile_behavior(self):
        current_time = shanghai_timestamp("2026-08-15 10:30:00")
        evaluation_at = shanghai_timestamp("2026-08-15 08:00:00")
        state = profile_guard_state(
            profile_health_guard_config=ProfileHealthGuardConfig(enabled=False),
        )
        activate_profile_health_selection(state, current_time)
        state.observations = profile_health_observations(
            evaluation_at,
            "LLLLLLLLLLLL",
        )

        decision = state._maybe_open_order(
            selected_profile_signal(current_time),
            latest_kline(current_time),
        )

        self.assertEqual(decision, "OPENED")
        self.assertEqual(state.snapshot()["profile_health_guard"]["status"], "DISABLED")

    def test_direction_pulse_shadow_refreshes_on_each_observation_settlement(self):
        state = profile_guard_state(
            profile_health_guard_config=ProfileHealthGuardConfig(enabled=False),
        )
        settled = [
            settled_observation(
                index,
                "WIN",
                index * 11 * MINUTE_MS,
                direction="SHORT",
            )
            for index in range(11)
        ]
        opened_at = 11 * 11 * MINUTE_MS
        pending = ObservationSignal(
            observation_key="direction-pulse-pending",
            strategy_family="short_observe",
            strategy_tag="generic_short_observe",
            direction="SHORT",
            timeframe_minutes=10,
            level="B",
            reason="pending pulse sample",
            entry_price=100.0,
            opened_at=opened_at,
            expires_at=opened_at + 10 * MINUTE_MS,
        )
        state.observations = [*settled, pending]
        state._refresh_direction_pulse_shadow(opened_at)
        self.assertEqual(
            state.snapshot()["direction_pulse_shadow"]["directions"]["SHORT"]["12"]["status"],
            "WARMUP",
        )

        result = state._settle_observations(pending.expires_at, 99.0)
        pulse = state.snapshot()["direction_pulse_shadow"]

        self.assertEqual(result, [pending])
        self.assertEqual(pulse["evaluated_at"], pending.expires_at)
        self.assertEqual(pulse["directions"]["SHORT"]["12"]["sample_size"], 12)
        self.assertEqual(pulse["directions"]["SHORT"]["12"]["status"], "NORMAL")

    def test_direction_pulse_shadow_never_blocks_live_order_path(self):
        current_time = 20 * 11 * MINUTE_MS
        state = profile_guard_state(
            max_open_short_orders=2,
            profile_health_guard_config=ProfileHealthGuardConfig(enabled=False),
        )
        state.observations = [
            settled_observation(
                index,
                "WIN" if index < 4 else "LOSS",
                index * 11 * MINUTE_MS,
                family="short_observe",
                tag="generic_short_observe",
                direction="SHORT",
                segment="WD-08",
            )
            for index in range(12)
        ]
        state._refresh_direction_pulse_shadow(current_time)
        signal = replace(
            selected_profile_signal(current_time),
            direction="SHORT",
            observe_direction="SHORT",
            score=-90.0,
            strategy_family="short_observe",
            strategy_tag="generic_short_observe",
            profile_key="10|short_observe|generic_short_observe|SHORT|WD-08",
        )

        decision = state._maybe_open_order(signal, latest_kline(current_time))
        opened = state.simulator.orders[-1]

        self.assertEqual(decision, "OPENED")
        self.assertTrue(opened.direction_pulse_shadow["windows"]["12"]["would_block"])
        self.assertEqual(opened.direction_pulse_shadow["mode"], "SHADOW_ONLY")
        self.assertEqual(state.snapshot()["stats"]["total_orders"], 1)

    def test_direction_pulse_shadow_failure_never_blocks_live_order_path(self):
        current_time = 20 * 11 * MINUTE_MS
        state = profile_guard_state(
            max_open_short_orders=2,
            profile_health_guard_config=ProfileHealthGuardConfig(enabled=False),
        )
        signal = replace(
            selected_profile_signal(current_time),
            direction="SHORT",
            observe_direction="SHORT",
            score=-90.0,
            strategy_family="short_observe",
            strategy_tag="generic_short_observe",
            profile_key="10|short_observe|generic_short_observe|SHORT|WD-08",
        )

        with patch("app.state.attach_candidate_shadow", side_effect=ValueError("bad shadow")):
            decision = state._maybe_open_order(signal, latest_kline(current_time))

        self.assertEqual(decision, "OPENED")
        self.assertEqual(state.snapshot()["stats"]["total_orders"], 1)

    def test_direction_pulse_candidate_excludes_future_settlements(self):
        current_time = 20 * 11 * MINUTE_MS
        state = profile_guard_state(
            profile_health_guard_config=ProfileHealthGuardConfig(enabled=False),
        )
        samples = [
            settled_observation(
                index,
                "LOSS",
                index * 11 * MINUTE_MS,
                family="short_observe",
                tag="generic_short_observe",
                direction="SHORT",
                segment="WD-08",
            )
            for index in range(12)
        ]
        state.observations = samples
        state._refresh_direction_pulse_shadow(current_time)
        candidate_time = samples[-1].settled_at - 1

        audited = state._attach_direction_pulse_shadow(
            replace(
                selected_profile_signal(candidate_time),
                direction="SHORT",
                observe_direction="SHORT",
            ),
            current_time=candidate_time,
        )

        self.assertEqual(audited.direction_pulse_shadow["evaluated_at"], candidate_time)
        self.assertEqual(audited.direction_pulse_shadow["windows"]["12"]["sample_size"], 11)
        self.assertEqual(audited.direction_pulse_shadow["windows"]["12"]["status"], "WARMUP")

    def test_direction_pulse_restart_restores_n16_beyond_profile_lookback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            for index in range(16):
                store.save_observation(
                    settled_observation(
                        index,
                        "WIN",
                        index * 24 * 60 * MINUTE_MS,
                        family="short_observe",
                        tag="generic_short_observe",
                        direction="SHORT",
                        segment="WD-08",
                    ),
                    "BTCUSDT",
                )

            state = MonitorState(symbol="BTCUSDT", storage=store)
            pulse = state.snapshot()["direction_pulse_shadow"]["directions"]["SHORT"]["16"]

        self.assertEqual(pulse["sample_size"], 16)
        self.assertEqual(pulse["status"], "NORMAL")

    def test_direction_pulse_settlement_is_persisted_before_snapshot_refresh(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            state = MonitorState(symbol="BTCUSDT", storage=store)
            opened_at = 20 * 11 * MINUTE_MS
            pending = ObservationSignal(
                observation_key="persist-before-pulse",
                strategy_family="short_observe",
                strategy_tag="generic_short_observe",
                direction="SHORT",
                timeframe_minutes=10,
                level="B",
                reason="pending pulse sample",
                entry_price=100.0,
                opened_at=opened_at,
                expires_at=opened_at + 10 * MINUTE_MS,
            )
            state.observations = [pending]
            store.save_observation(pending, "BTCUSDT")

            state._settle_observations(pending.expires_at, 99.0)
            restored = store.load_observations("BTCUSDT")[0]

        self.assertEqual(restored.status, "SETTLED")
        self.assertEqual(restored.result, "WIN")
        self.assertEqual(
            state.snapshot()["direction_pulse_shadow"]["evaluated_at"],
            pending.expires_at,
        )

    def test_slow_webhook_transport_does_not_block_market_update(self):
        transport_started = threading.Event()
        release_transport = threading.Event()
        update_finished = threading.Event()

        def blocking_transport(url, body, timeout):
            transport_started.set()
            release_transport.wait(timeout=2)

        state = MonitorState(
            symbol="BTCUSDT",
            webhook=WebhookSignalProxy(transport=blocking_transport),
            enable_wave_guard=False,
        )

        updater = threading.Thread(
            target=lambda: (
                state.update_from_klines(actionable_rebound_klines()),
                update_finished.set(),
            )
        )
        updater.start()

        self.assertTrue(transport_started.wait(timeout=2))
        self.assertTrue(update_finished.wait(timeout=0.5))
        self.assertEqual(state.snapshot()["stats"]["total_orders"], 1)
        release_transport.set()
        updater.join(timeout=2)

    def test_short_signal_is_observed_without_opening_order_or_webhook(self):
        webhook = RecordingWebhook()
        storage = RecordingStorage()
        state = MonitorState(symbol="BTCUSDT", webhook=webhook, storage=storage)
        signal = Signal(
            direction="SHORT",
            timeframe_minutes=10,
            level="A",
            reason="量平价跌：MACD/RSI确认弱势延续",
            price=100.0,
            open_time=4_200_000,
            score=-90.0,
            threshold=70.0,
            threshold_segment="WD-21",
            session_allowed=True,
            strategy_family="short_extension",
            strategy_tag="normal_down_short_extension_observe",
            observe_direction="SHORT",
            observe_only=True,
        )

        decision = state._maybe_open_order(signal, kline(70, 100.0, 100))
        state.wait_for_storage_writes()
        snapshot = state.snapshot()

        self.assertEqual(decision, "SHORT_OBSERVE_ONLY")
        self.assertEqual(snapshot["stats"]["total_orders"], 0)
        self.assertEqual(webhook.calls, [])
        self.assertIn("SHORT观察模式", snapshot["risk_pause"])
        self.assertEqual(len(snapshot["observations"]), 1)
        self.assertEqual(snapshot["observations"][0]["strategy_tag"], "normal_down_short_extension_observe")
        self.assertEqual(len(storage.observations), 1)

    def test_wd23_short_cannot_consume_long_progression_credit(self):
        state = MonitorState(
            symbol="BTCUSDT",
            stake_progression_base_only_segments=["WD-23"],
        )
        first = state.simulator.open_order(
            Signal(
                direction="LONG",
                timeframe_minutes=1,
                level="A",
                reason="建立滚单状态",
                price=100.0,
                open_time=0,
                threshold_segment="WE-17",
            ),
            entry_price=100.0,
            opened_at=0,
        )
        state.simulator.settle_expired_orders(current_time=60_000, current_price=101.0)
        signal = Signal(
            direction="SHORT",
            timeframe_minutes=10,
            level="A",
            reason="量平价跌SHORT扩展",
            price=100.0,
            open_time=4_200_000,
            score=-90.0,
            threshold=70.0,
            threshold_segment="WD-23",
            session_allowed=True,
            strategy_family="short_extension",
            strategy_tag="normal_down_short_extension_observe",
            observe_direction="SHORT",
            observe_only=True,
        )

        decision = state._maybe_open_order(signal, kline(70, 100.0, 100))
        opened = state.simulator.orders[-1]

        self.assertEqual(first.result, "WIN")
        self.assertEqual(decision, "OPENED")
        self.assertEqual(opened.direction, "SHORT")
        self.assertEqual(opened.stake, 10.0)
        self.assertEqual(opened.stake_progression_step, 1)
        self.assertNotIn("固定基础金额", opened.reason)

    def test_wd02_short_is_live_enabled_by_default(self):
        state = MonitorState(symbol="BTCUSDT")
        signal = Signal(
            direction="SHORT",
            timeframe_minutes=10,
            level="A",
            reason="量平价跌SHORT扩展",
            price=100.0,
            open_time=4_200_000,
            score=-90.0,
            threshold=70.0,
            threshold_segment="WD-02",
            session_allowed=True,
            strategy_family="short_extension",
            strategy_tag="normal_down_short_extension_observe",
            observe_direction="SHORT",
            observe_only=True,
        )

        decision = state._maybe_open_order(signal, kline(70, 100.0, 100))

        self.assertEqual(decision, "OPENED")
        self.assertEqual(state.simulator.orders[-1].direction, "SHORT")

    def test_legacy_observation_profile_cannot_promote_wait_signal(self):
        state = MonitorState(
            symbol="BTCUSDT",
            observation_profile_min_samples=8,
            observation_profile_min_win_rate=0.68,
            observation_profile_min_ev=3.0,
            observation_profile_min_edge=8.0,
        )
        latest = kline(20_000, 100.0, 100)
        profile_start = latest.close_time - 6 * 86_400_000
        state.observations.extend(
            settled_observation(
                idx,
                "WIN" if idx < 7 else "LOSS",
                profile_start + idx * 600_000,
            )
            for idx in range(8)
        )
        signal = Signal(
            direction="WAIT",
            observe_direction="LONG",
            observe_only=True,
            timeframe_minutes=10,
            level="A",
            reason="放量急跌反抽：时段画像待恢复",
            price=100.0,
            open_time=latest.open_time,
            score=90.0,
            threshold=70.0,
            threshold_segment="WD-07",
            session_allowed=False,
            strategy_family="drop_reclaim",
            strategy_tag="drop_reclaim_observe",
        )

        decision = state._maybe_open_order(signal, latest)
        self.assertEqual(decision, "SESSION_BLOCKED")
        self.assertEqual(state.simulator.orders, [])

    def test_default_legacy_observation_profile_cannot_promote_wait_signal(self):
        state = MonitorState(symbol="BTCUSDT")
        latest = kline(20_000, 100.0, 100)
        profile_start = latest.close_time - 6 * 86_400_000
        state.observations.extend(
            settled_observation(
                idx,
                "WIN" if idx < 10 else "LOSS",
                profile_start + idx * 600_000,
            )
            for idx in range(12)
        )
        signal = Signal(
            direction="WAIT",
            observe_direction="LONG",
            observe_only=True,
            timeframe_minutes=10,
            level="A",
            reason="放量急跌反抽：默认画像放行",
            price=100.0,
            open_time=latest.open_time,
            score=90.0,
            threshold=70.0,
            threshold_segment="WD-07",
            session_allowed=False,
            strategy_family="drop_reclaim",
            strategy_tag="drop_reclaim_observe",
        )

        decision = state._maybe_open_order(signal, latest)

        self.assertEqual(decision, "SESSION_BLOCKED")
        self.assertEqual(state.simulator.orders, [])

    def test_legacy_observation_profile_does_not_bypass_wait_with_open_order(self):
        state = MonitorState(symbol="BTCUSDT", max_open_orders=1)
        latest = kline(20_000, 100.0, 100)
        profile_start = latest.close_time - 6 * 86_400_000
        state.observations.extend(
            settled_observation(idx, "WIN", profile_start + idx * 600_000)
            for idx in range(12)
        )
        state.simulator.orders.append(
            SimulatedOrder(
                id=1,
                direction="LONG",
                timeframe_minutes=10,
                level="A",
                reason="已有订单",
                entry_price=100.0,
                opened_at=latest.close_time - 60_000,
                expires_at=latest.close_time + 540_000,
                threshold_segment="WE-17",
            )
        )
        signal = Signal(
            direction="WAIT",
            observe_direction="LONG",
            observe_only=True,
            timeframe_minutes=10,
            level="A",
            reason="放量急跌反抽：已有订单时不放行",
            price=100.0,
            open_time=latest.open_time,
            score=90.0,
            threshold=70.0,
            threshold_segment="WD-07",
            session_allowed=False,
            strategy_family="drop_reclaim",
            strategy_tag="drop_reclaim_observe",
        )

        decision = state._maybe_open_order(signal, latest)

        self.assertEqual(decision, "SESSION_BLOCKED")
        self.assertEqual(len(state.simulator.orders), 1)

    def test_session_blocked_signal_ignores_observations_older_than_lookback(self):
        state = MonitorState(symbol="BTCUSDT", observation_profile_lookback_days=7)
        latest = kline(20_000, 100.0, 100)
        old_start = latest.close_time - 8 * 86_400_000
        state.observations.extend(
            settled_observation(idx, "WIN", old_start + idx * 60_000)
            for idx in range(8)
        )
        signal = Signal(
            direction="WAIT",
            observe_direction="LONG",
            observe_only=True,
            timeframe_minutes=10,
            level="A",
            reason="放量急跌反抽：过期画像不应放行",
            price=100.0,
            open_time=latest.open_time,
            score=90.0,
            threshold=70.0,
            threshold_segment="WD-07",
            session_allowed=False,
            strategy_family="drop_reclaim",
            strategy_tag="drop_reclaim_observe",
        )

        decision = state._maybe_open_order(signal, latest)

        self.assertEqual(decision, "SESSION_BLOCKED")
        self.assertEqual(state.snapshot()["stats"]["total_orders"], 0)

    def test_wait_signal_is_rejected_before_rolling_edge_guard(self):
        state = MonitorState(
            symbol="BTCUSDT",
            rolling_edge_config=RollingEdgeConfig(min_samples=3),
            observation_profile_min_samples=8,
            observation_profile_min_win_rate=0.68,
            observation_profile_min_ev=3.0,
            observation_profile_min_edge=8.0,
        )
        latest = kline(20_000, 100.0, 100)
        profile_start = latest.close_time - 6 * 86_400_000
        state.observations.extend(
            settled_observation(idx, "WIN", profile_start + idx * 600_000)
            for idx in range(8)
        )
        for idx in range(3):
            prior_day_settlement = (latest.close_time // 86_400_000) * 86_400_000 - 1 - idx * 600_000
            state.simulator.orders.append(
                SimulatedOrder(
                    id=idx + 1,
                    direction="LONG",
                    timeframe_minutes=10,
                    level="A",
                    reason="放量急跌反抽：历史亏损",
                    entry_price=100.0,
                    opened_at=prior_day_settlement - 600_000,
                    expires_at=prior_day_settlement,
                    threshold_segment="WD-07",
                    status="SETTLED",
                    result="LOSS",
                    exit_price=99.0,
                    settled_at=prior_day_settlement,
                    pnl=-10.0,
                )
            )
        signal = Signal(
            direction="WAIT",
            observe_direction="LONG",
            observe_only=True,
            timeframe_minutes=10,
            level="A",
            reason="放量急跌反抽：时段画像待恢复",
            price=100.0,
            open_time=latest.open_time,
            score=90.0,
            threshold=70.0,
            threshold_segment="WD-07",
            session_allowed=False,
            strategy_family="drop_reclaim",
            strategy_tag="drop_reclaim_observe",
        )

        decision = state._maybe_open_order(signal, latest)

        self.assertEqual(decision, "SESSION_BLOCKED")
        self.assertEqual(len(state.simulator.orders), 3)
        self.assertEqual(state.snapshot()["rolling_edge"]["status"], "DEGRADED")

    def test_observation_signals_settle_without_real_order(self):
        state = MonitorState(symbol="BTCUSDT")
        signal = Signal(
            direction="SHORT",
            timeframe_minutes=10,
            level="A",
            reason="量平价跌SHORT扩展",
            price=100.0,
            open_time=4_200_000,
            score=-90.0,
            threshold=70.0,
            threshold_segment="WD-21",
            session_allowed=True,
            strategy_family="short_extension",
            strategy_tag="normal_down_short_extension_observe",
            observe_direction="SHORT",
            observe_only=True,
        )

        state._maybe_open_order(signal, kline(70, 100.0, 100))
        settled = state._settle_observations(70 * 60_000 + 59_999 + 10 * 60_000, 99.0)
        snapshot = state.snapshot()

        self.assertEqual(len(settled), 1)
        self.assertEqual(snapshot["observations"][0]["status"], "SETTLED")
        self.assertEqual(snapshot["observations"][0]["result"], "WIN")
        self.assertEqual(snapshot["stats"]["total_orders"], 0)

    def test_observation_settlement_uses_expiry_kline_instead_of_latest_price(self):
        state = MonitorState(symbol="BTCUSDT")
        observation = ObservationSignal(
            observation_key="expiry-price",
            strategy_family="drop_reclaim",
            strategy_tag="drop_reclaim_observe",
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="到期价格测试",
            entry_price=100.0,
            opened_at=59_999,
            expires_at=659_999,
            threshold_segment="WD-07",
        )
        state.observations.append(observation)
        expiry_kline = Kline(600_000, 100.0, 100.0, 90.0, 90.0, 1.0, 659_999)
        later_kline = Kline(1_200_000, 100.0, 110.0, 100.0, 110.0, 1.0, 1_259_999)

        settled = state._settle_observations(
            later_kline.close_time,
            later_kline.close,
            [expiry_kline, later_kline],
        )

        self.assertEqual(settled, [observation])
        self.assertEqual(observation.result, "LOSS")
        self.assertEqual(observation.exit_price, 90.0)
        self.assertEqual(observation.settled_at, 659_999)

    def test_observation_settlement_waits_when_exact_expiry_kline_is_missing(self):
        state = MonitorState(symbol="BTCUSDT")
        observation = ObservationSignal(
            observation_key="missing-expiry-price",
            strategy_family="drop_reclaim",
            strategy_tag="drop_reclaim_observe",
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="缺失到期价格测试",
            entry_price=100.0,
            opened_at=59_999,
            expires_at=659_999,
            threshold_segment="WD-07",
        )
        state.observations.append(observation)
        next_minute = Kline(660_000, 100.0, 110.0, 100.0, 110.0, 1.0, 719_999)

        settled = state._settle_observations(
            next_minute.close_time,
            next_minute.close,
            [next_minute],
        )

        self.assertEqual(settled, [])
        self.assertEqual(observation.status, "OPEN")

    def test_restart_restores_complete_latest_profile_window_instead_of_last_500_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            for idx in range(650):
                store.save_observation(
                    settled_observation(
                        idx,
                        "LOSS" if idx < 150 else "WIN",
                        idx * 600_000,
                    ),
                    "BTCUSDT",
                )
            state = MonitorState(symbol="BTCUSDT", storage_path=db_path)
            latest = kline(7_000, 100.0, 100)
            signal = Signal(
                direction="WAIT",
                observe_direction="LONG",
                observe_only=True,
                timeframe_minutes=10,
                level="A",
                reason="放量急跌反抽：完整恢复测试",
                price=100.0,
                open_time=latest.open_time,
                score=90.0,
                threshold=70.0,
                threshold_segment="WD-07",
                session_allowed=False,
                strategy_family="drop_reclaim",
                strategy_tag="drop_reclaim_observe",
            )

            profile = state._observation_profile(signal, "LONG", latest.close_time)
            decision = state._maybe_open_order(signal, latest)

        self.assertEqual(profile["sample_size"], 650)
        self.assertAlmostEqual(profile["win_rate"], 500 / 650)
        self.assertLess(profile["ev"], 4.0)
        self.assertEqual(decision, "SESSION_BLOCKED")

    def test_observation_profile_records_only_one_open_sample_per_profile(self):
        state = MonitorState(symbol="BTCUSDT")
        first = Signal(
            direction="WAIT",
            observe_direction="LONG",
            observe_only=True,
            timeframe_minutes=10,
            level="A",
            reason="放量急跌反抽：连续候选",
            price=100.0,
            open_time=4_200_000,
            score=90.0,
            threshold=70.0,
            threshold_segment="WD-07",
            strategy_family="drop_reclaim",
            strategy_tag="drop_reclaim_observe",
        )
        second = replace(first, open_time=4_260_000)

        state._record_observation(first, kline(70, 100.0, 100), "SESSION_BLOCKED")
        state._record_observation(second, kline(71, 99.9, 100), "SESSION_BLOCKED")

        self.assertEqual(len(state.observations), 1)

    def test_observation_profile_does_not_count_overlapping_settled_rows_as_independent_samples(self):
        state = MonitorState(symbol="BTCUSDT")
        latest = kline(20_000, 100.0, 100)
        start = latest.close_time - 86_400_000
        state.observations.extend(
            settled_observation(idx, "WIN", start + idx * 60_000)
            for idx in range(8)
        )
        signal = Signal(
            direction="WAIT",
            observe_direction="LONG",
            observe_only=True,
            timeframe_minutes=10,
            level="A",
            reason="放量急跌反抽：重叠画像不放行",
            price=100.0,
            open_time=latest.open_time,
            score=90.0,
            threshold=70.0,
            threshold_segment="WD-07",
            session_allowed=False,
            strategy_family="drop_reclaim",
            strategy_tag="drop_reclaim_observe",
        )

        decision = state._maybe_open_order(signal, latest)

        self.assertEqual(decision, "SESSION_BLOCKED")
        self.assertEqual(state._observation_profile(signal, "LONG", latest.close_time)["sample_size"], 1)

    def test_state_does_not_record_unproven_research_observation_candidates(self):
        from tests.test_strategy import fear_falling_mid_drop_klines

        state = MonitorState(
            symbol="BTCUSDT",
            fear_greed_provider=StaticFearGreedProvider(
                FearGreedContext(value=28, classification="Fear", average_30d=37.0, trend="falling")
            ),
        )

        state.update_from_klines(fear_falling_mid_drop_klines(drop_total=1.0))
        snapshot = state.snapshot()

        self.assertEqual(snapshot["stats"]["total_orders"], 0)
        self.assertFalse(
            any(item["strategy_tag"] == "drop_reclaim_mirror_short_observe" for item in snapshot["observations"])
        )

    def test_research_observation_candidates_do_not_overlap_same_tag_before_expiry(self):
        state = MonitorState(symbol="BTCUSDT")
        signal = Signal(
            direction="WAIT",
            observe_direction="SHORT",
            observe_only=True,
            timeframe_minutes=10,
            level="A",
            reason="冲高失败SHORT观察",
            price=100.0,
            open_time=4_200_000,
            score=-66.0,
            threshold=58.0,
            threshold_segment="WD-12",
            strategy_family="failed_breakout",
            strategy_tag="failed_high_120m_short_observe",
        )

        state._record_observation_candidates([signal], kline(70, 100.0, 100))
        state._record_observation_candidates([signal], kline(71, 100.1, 100))
        snapshot = state.snapshot()

        self.assertEqual(len(snapshot["observations"]), 1)
        self.assertEqual(snapshot["observations"][0]["source_decision"], "RESEARCH_OBSERVE")

    def test_opened_long_records_strategy_fields_without_changing_order_gate(self):
        state = MonitorState(symbol="BTCUSDT")
        signal = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="放量急跌反抽：synthetic",
            price=100.0,
            open_time=4_200_000,
            score=90.0,
            threshold=70.0,
            threshold_segment="WD-12",
            session_allowed=True,
            strategy_family="drop_reclaim",
            strategy_tag="drop_reclaim_extreme_10m_120bps_v1.5_rsi30_boll0.1",
            observe_direction="LONG",
            profile_key="drop_reclaim|LONG|WD-12",
        )

        decision = state._maybe_open_order(signal, kline(70, 100.0, 100))
        snapshot = state.snapshot()

        self.assertEqual(decision, "OPENED")
        self.assertEqual(snapshot["stats"]["total_orders"], 1)
        self.assertEqual(snapshot["orders"][0]["strategy_tag"], "drop_reclaim_extreme_10m_120bps_v1.5_rsi30_boll0.1")
        self.assertEqual(snapshot["orders"][0]["profile_key"], "drop_reclaim|LONG|WD-12")
        self.assertEqual(snapshot["observations"][0]["source_decision"], "OPENED")

    def test_opened_order_and_observation_keep_same_pre_open_quality_context(self):
        state = MonitorState(symbol="BTCUSDT")
        signal = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="B",
            reason="影子评分上下文",
            price=100.0,
            open_time=4_200_000,
            score=90.0,
            threshold=70.0,
            threshold_segment="WD-12",
            session_allowed=True,
            strategy_family="drop_reclaim",
            strategy_tag="quality_context_test",
            observe_direction="LONG",
        )
        scored = state._attach_quality_score(signal)

        decision = state._maybe_open_order(scored, kline(70, 100.0, 100))
        snapshot = state.snapshot()

        self.assertEqual(decision, "OPENED")
        self.assertEqual(snapshot["orders"][0]["quality_score_context"], "LONG_FIRST")
        self.assertEqual(snapshot["observations"][0]["quality_score_context"], "LONG_FIRST")
        self.assertEqual(
            snapshot["orders"][0]["quality_score"],
            snapshot["observations"][0]["quality_score"],
        )

    def test_wait_observation_quality_slot_uses_observe_direction(self):
        state = MonitorState(symbol="BTCUSDT")
        state.simulator.open_order(
            Signal(
                direction="SHORT",
                timeframe_minutes=10,
                level="A",
                reason="open short",
                price=100.0,
                open_time=0,
            ),
            100.0,
            0,
        )
        wait_short = Signal(
            direction="WAIT",
            observe_direction="SHORT",
            timeframe_minutes=10,
            level="B",
            reason="observe short",
            price=100.0,
            open_time=60_000,
        )

        scored = state._attach_quality_score(wait_short)

        self.assertEqual(scored.order_slot, "SECOND")
        self.assertEqual(scored.order_slot_scope, "DIRECTION_V2")
        self.assertEqual(scored.quality_score_context, "SHORT_SECOND")

    def test_shadow_quality_score_failure_does_not_block_existing_order_path(self):
        state = MonitorState(symbol="BTCUSDT")
        signal = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="B",
            reason="影子评分故障隔离",
            price=100.0,
            open_time=4_200_000,
            score=90.0,
            threshold=70.0,
            threshold_segment="WD-12",
            session_allowed=True,
        )

        with patch(
            "app.state.attach_shadow_quality_score",
            side_effect=RuntimeError("synthetic quality score failure"),
        ):
            decision = state._maybe_open_order(signal, kline(70, 100.0, 100))

        self.assertEqual(decision, "OPENED")
        self.assertEqual(state.snapshot()["stats"]["total_orders"], 1)
        self.assertEqual(state.snapshot()["orders"][0]["order_slot"], "FIRST")
        self.assertIn("影子质量评分失败", state.last_error)

    def test_shadow_quality_score_does_not_change_end_to_end_order_identity(self):
        klines = actionable_rebound_klines()
        baseline = MonitorState(symbol="BTCUSDT", enable_wave_guard=False)
        scored = MonitorState(symbol="BTCUSDT", enable_wave_guard=False)

        with patch(
            "app.state.attach_shadow_quality_score",
            side_effect=lambda signal, **_kwargs: signal,
        ):
            baseline.update_from_klines(klines)
        scored.update_from_klines(klines)

        baseline_orders = [
            (order.id, order.direction, order.opened_at, order.expires_at)
            for order in baseline.simulator.orders
        ]
        scored_orders = [
            (order.id, order.direction, order.opened_at, order.expires_at)
            for order in scored.simulator.orders
        ]
        self.assertEqual(scored.order_decision, baseline.order_decision)
        self.assertEqual(scored_orders, baseline_orders)

    def test_profile_guard_defaults_to_observe_only(self):
        state = MonitorState(symbol="BTCUSDT")
        snapshot = state.snapshot()

        self.assertFalse(snapshot["profile_guard"]["enabled"])
        self.assertTrue(snapshot["profile_guard"]["observe_only"])
        self.assertEqual(snapshot["profile_guard"]["min_history"], 15)
        self.assertEqual(snapshot["profile_guard"]["min_group_size"], 2)
        promotion = snapshot["observation_profile_promotion"]
        self.assertEqual(promotion["lookback_days"], 7)
        self.assertEqual(promotion["min_samples"], 12)
        self.assertEqual(promotion["min_win_rate"], 0.72)
        self.assertEqual(promotion["min_ev"], 4.0)
        self.assertEqual(promotion["min_edge"], 10.0)
        self.assertEqual(promotion["live_short_segments"], ["WD-02", "WD-23"])

    def test_disabled_profile_guard_skips_shadow_on_formal_decision_path(self):
        state = profile_guard_state(enable_profile_guard=False)
        current_time = 1 * MINUTE_MS

        with patch.object(
            state,
            "_profile_guard_shadow",
            return_value={"status": "NORMAL"},
        ) as shadow:
            decision = state._maybe_open_order(
                selected_profile_signal(current_time),
                latest_kline(current_time),
            )

        self.assertEqual(decision, "OPENED")
        shadow.assert_not_called()

    def test_profile_guard_can_block_when_explicitly_enabled(self):
        storage = RecordingStorage()
        storage.order_profile = {
            "profile_guard": {
                "walk_forward_combined": {
                    "risk_keys": ["HIGH_RSI_REBOUND", "WEAK_SEGMENT_WD00_WD18_WD22"],
                    "min_history": 15,
                    "min_group_size": 2,
                    "traded": {"orders": 28, "win_rate": 0.7143, "ev": 5.35, "pnl": 149.76},
                    "blocked": {"orders": 21},
                    "delta_pnl": 190.32,
                }
            }
        }
        state = MonitorState(symbol="BTCUSDT", storage=storage, enable_profile_guard=True)
        signal = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="放量急跌反抽：synthetic",
            price=100.0,
            open_time=4_200_000,
            score=90.0,
            threshold=70.0,
            threshold_segment="WD-18",
            session_allowed=True,
            price_change_pct=-0.0015,
            price_position=0.45,
            rsi=46.0,
            mtf_10m_bias=0.1,
            mtf_30m_bias=0.2,
            observe_direction="LONG",
        )

        decision = state._maybe_open_order(signal, kline(70, 100.0, 100))
        snapshot = state.snapshot()

        self.assertEqual(decision, "PROFILE_GUARD_BLOCKED")
        self.assertEqual(snapshot["stats"]["total_orders"], 0)
        self.assertIn("画像守卫命中", snapshot["risk_pause"])
        self.assertEqual(snapshot["observations"][0]["source_decision"], "PROFILE_GUARD_BLOCKED")

    def test_state_uses_configured_stake_terms_for_orders_and_webhook(self):
        klines = actionable_rebound_klines()
        webhook = RecordingWebhook()
        state = MonitorState(
            symbol="BTCUSDT",
            webhook=webhook,
            stake=20.0,
            win_return=36.0,
            enable_wave_guard=False,
        )

        state.update_from_klines(klines)

        snapshot = state.snapshot()
        self.assertEqual(snapshot["orders"][0]["stake"], 20.0)
        self.assertEqual(snapshot["orders"][0]["win_return"], 36.0)
        self.assertEqual(snapshot["stats"]["stake"], 20.0)
        self.assertEqual(snapshot["stats"]["win_return"], 36.0)
        self.assertEqual(webhook.calls[0][4], 20.0)

    def test_state_can_disable_stake_progression_from_startup_config(self):
        state = MonitorState(
            symbol="BTCUSDT",
            stake=20.0,
            win_return=36.0,
            enable_stake_progression=False,
            stake_progression_max_orders=5,
        )
        first = state.simulator.open_order(Signal(direction="LONG", timeframe_minutes=1, level="A", reason="first", price=100.0, open_time=0), entry_price=100.0, opened_at=0)
        state.simulator.settle_expired_orders(current_time=60_000, current_price=101.0)
        second = state.simulator.open_order(Signal(direction="LONG", timeframe_minutes=1, level="A", reason="second", price=101.0, open_time=60_000), entry_price=101.0, opened_at=60_000)

        self.assertEqual([first.stake, second.stake], [20.0, 20.0])
        self.assertEqual([first.win_return, second.win_return], [36.0, 36.0])
        snapshot = state.snapshot()
        self.assertFalse(snapshot["stats"]["stake_progression_enabled"])
        self.assertEqual(snapshot["stats"]["stake_progression_max_orders"], 2)
        self.assertFalse(snapshot["stake_progression"]["enabled"])
        self.assertEqual(snapshot["stake_progression"]["max_orders"], 2)
        self.assertEqual(snapshot["stake_progression"]["next_stake"], 20.0)

    def test_state_does_not_reopen_while_order_is_open(self):
        klines = actionable_rebound_klines()
        state = MonitorState(symbol="BTCUSDT", enable_wave_guard=False)

        state.update_from_klines(klines)
        state.update_from_klines(klines + [kline(490, 95.2, 265, open_price=95.5, high=95.6, low=95.1)])

        snapshot = state.snapshot()
        self.assertEqual(snapshot["stats"]["total_orders"], 1)
        self.assertEqual(snapshot["stats"]["open_orders"], 1)

    def test_state_marks_session_blocked_when_score_passes_threshold_but_time_segment_is_blocked(self):
        klines = [kline(i, 100 + (0.2 if i % 2 else -0.2), 100 + (30 if i % 3 == 0 else 0)) for i in range(830)]
        for offset in range(10):
            idx = 830 + offset
            open_price = 100.0 - offset * 0.2
            close = open_price - 0.15
            klines.append(kline(idx, close, 220, open_price=open_price, high=open_price + 0.05, low=close - 0.1))
        state = MonitorState(symbol="BTCUSDT")

        state.update_from_klines(klines)

        snapshot = state.snapshot()
        self.assertEqual(snapshot["order_decision"], "SESSION_BLOCKED")
        self.assertEqual(snapshot["stats"]["total_orders"], 0)

    def test_state_passes_fear_greed_context_into_snapshot_and_signals(self):
        klines = actionable_rebound_klines()
        context = FearGreedContext(
            value=84,
            classification="Extreme Greed",
            average_30d=62.0,
            trend="rising",
            updated_at_ms=1778889600000,
        )
        provider = StaticFearGreedProvider(context)
        state = MonitorState(symbol="BTCUSDT", fear_greed_provider=provider)

        state.update_from_klines(klines)

        snapshot = state.snapshot()
        self.assertEqual(provider.calls, 1)
        self.assertEqual(snapshot["fear_greed"]["value"], 84)
        self.assertEqual(snapshot["selected_signal"]["fear_greed_value"], 84)
        self.assertGreater(snapshot["selected_signal"]["fear_greed_adjustment"], 0.0)

    def test_state_blocks_order_when_rolling_edge_is_degraded(self):
        state = MonitorState(symbol="BTCUSDT", rolling_edge_config=RollingEdgeConfig(min_samples=3))
        for idx in range(3):
            state.simulator.orders.append(
                SimulatedOrder(
                    id=idx + 1,
                    direction="LONG",
                    timeframe_minutes=10,
                    level="A",
                    reason="放量急跌反抽：synthetic",
                    entry_price=100.0,
                    opened_at=1_000_000 + idx * 600_000,
                    expires_at=1_600_000 + idx * 600_000,
                    threshold_segment="WD-12",
                    status="SETTLED",
                    result="LOSS",
                    exit_price=99.0,
                    settled_at=1_600_000 + idx * 600_000,
                    pnl=-10.0,
                )
            )
        signal = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="放量急跌反抽：synthetic",
            price=100.0,
            open_time=4_200_000,
            score=90.0,
            threshold=70.0,
            threshold_segment="WD-12",
            session_allowed=True,
        )

        decision = state._maybe_open_order(signal, kline(3000, 100.0, 100))
        snapshot = state.snapshot()

        self.assertEqual(decision, "ROLLING_EDGE_BLOCKED")
        self.assertEqual(snapshot["rolling_edge"]["status"], "DEGRADED")
        self.assertFalse(snapshot["rolling_edge"]["observe_only"])
        self.assertEqual(snapshot["rolling_edge"]["sample_size"], 3)
        self.assertEqual(snapshot["rolling_edge"]["key"], "10|WD-12|放量急跌反抽")
        self.assertEqual(snapshot["stats"]["total_orders"], 3)

    def test_rolling_edge_uses_base_stake_pnl_when_progression_is_enabled(self):
        state = MonitorState(
            symbol="BTCUSDT",
            enable_stake_progression=True,
            rolling_edge_config=RollingEdgeConfig(
                min_samples=2,
                min_win_rate=0.0,
                min_ev=0.0,
            ),
        )
        state.simulator.orders.extend(
            [
                SimulatedOrder(
                    id=1,
                    direction="LONG",
                    timeframe_minutes=10,
                    level="A",
                    reason="放量急跌反抽：synthetic",
                    entry_price=100.0,
                    opened_at=1_000_000,
                    expires_at=1_600_000,
                    threshold_segment="WD-12",
                    stake=18.0,
                    win_return=32.4,
                    stake_progression_step=2,
                    stake_progression_version=TWO_STAGE_VERSION,
                    status="SETTLED",
                    result="WIN",
                    exit_price=101.0,
                    settled_at=1_600_000,
                    pnl=14.4,
                ),
                SimulatedOrder(
                    id=2,
                    direction="LONG",
                    timeframe_minutes=10,
                    level="A",
                    reason="放量急跌反抽：synthetic",
                    entry_price=100.0,
                    opened_at=2_000_000,
                    expires_at=2_600_000,
                    threshold_segment="WD-12",
                    status="SETTLED",
                    result="LOSS",
                    exit_price=99.0,
                    settled_at=2_600_000,
                    pnl=-10.0,
                ),
            ]
        )
        signal = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="放量急跌反抽：synthetic",
            price=100.0,
            open_time=4_200_000,
            score=90.0,
            threshold=70.0,
            threshold_segment="WD-12",
            session_allowed=True,
        )

        snapshot = state._rolling_edge_status(signal, kline(70, 100.0, 100))

        self.assertEqual(snapshot["sample_size"], 2)
        self.assertEqual(snapshot["pnl"], -2.0)
        self.assertEqual(snapshot["ev"], -1.0)
        self.assertEqual(snapshot["status"], "DEGRADED")

    def test_state_records_order_entry_snapshot_and_settlement_asynchronously(self):
        klines = actionable_rebound_klines()
        storage = RecordingStorage()
        storage.order_profile = {
            "profile_guard": {
                "recommended_key_subset": {
                    "selection_policy": {
                        "name": "STABILITY_BAND",
                        "reason": "最高稳定分组合已满足稳定带",
                        "selected_keys": ["HIGH_RSI_REBOUND"],
                        "score_best_keys": ["HIGH_RSI_REBOUND"],
                    },
                    "final_active_keys": ["HIGH_RSI_REBOUND"],
                    "risk_keys": ["HIGH_RSI_REBOUND"],
                    "min_history": 15,
                    "min_group_size": 2,
                    "traded": {"orders": 29, "win_rate": 0.7241, "ev": 6.06, "pnl": 175.68},
                    "blocked": {"orders": 20},
                    "delta_pnl": 216.24,
                },
                "walk_forward_combined": {
                    "risk_keys": ["HIGH_RSI_REBOUND", "WEAK_SEGMENT_WD00_WD18_WD22"],
                    "min_history": 15,
                    "min_group_size": 2,
                    "traded": {"orders": 28, "win_rate": 0.7143, "ev": 5.35, "pnl": 149.76},
                    "blocked": {"orders": 21},
                    "delta_pnl": 190.32,
                },
            }
        }
        state = MonitorState(symbol="BTCUSDT", storage=storage, enable_wave_guard=False)

        state.update_from_klines(klines)
        opened_at = state.snapshot()["orders"][0]["opened_at"]
        state.simulator.orders[0].expires_at = opened_at
        state.update_from_klines([kline(opened_at // 60_000, 96.0, 160)])
        state.wait_for_storage_writes()

        self.assertEqual(len(storage.entry_snapshots), 1)
        symbol, order_payload, entry_snapshot = storage.entry_snapshots[0]
        self.assertEqual(symbol, "BTCUSDT")
        self.assertEqual(order_payload["status"], "OPEN")
        self.assertEqual(entry_snapshot["signal"]["direction"], order_payload["direction"])
        self.assertIn("strategy_tag", entry_snapshot["signal"])
        self.assertEqual(entry_snapshot["signal"]["quality_score_mode"], "SHADOW_ONLY")
        self.assertEqual(entry_snapshot["signal"]["quality_score_version"], "QS_V1_SHADOW")
        self.assertEqual(entry_snapshot["signal"]["quality_score"], order_payload["quality_score"])
        self.assertTrue(entry_snapshot["signal"]["quality_score_components"])
        self.assertEqual(entry_snapshot["rolling_edge"]["status"], "NORMAL")
        self.assertIn("result_sequence_guard", entry_snapshot)
        self.assertEqual(entry_snapshot["profile_guard_shadow"]["variant"], "recommended_key_subset")
        self.assertEqual(entry_snapshot["profile_guard_shadow"]["selection_policy"]["name"], "STABILITY_BAND")
        self.assertEqual(entry_snapshot["profile_guard_default_shadow"]["variant"], "walk_forward_combined")
        self.assertEqual(entry_snapshot["profile_guard_selection_policy"]["name"], "STABILITY_BAND")
        self.assertTrue(entry_snapshot["profile_guard_shadow"]["observe_only"])
        self.assertEqual(entry_snapshot["latest_kline"]["close"], order_payload["entry_price"])
        self.assertEqual(entry_snapshot["stake_config"]["stake"], 10.0)
        self.assertEqual(len(storage.settlements), 1)
        self.assertEqual(storage.settlements[0][1]["status"], "SETTLED")

    def test_progression_atomic_event_order_and_recording_restart_restore_active(self):
        storage = RecordingStorage()
        state = MonitorState(
            symbol="BTCUSDT", storage=storage, enable_wave_guard=False, now_ms=lambda: 1_000
        )
        first_signal = Signal(
            direction="LONG", timeframe_minutes=2, level="A", reason="first",
            price=100.0, open_time=1_000, score=80.0, threshold=70.0,
            threshold_segment="WD-08", session_allowed=True,
        )
        first_kline = Kline(0, 100.0, 100.0, 100.0, 100.0, 1.0, 1_000)
        state._maybe_open_order(first_signal, first_kline)
        state.wait_for_storage_writes()
        storage.atomic_calls.clear()
        storage.entry_snapshots.clear()

        second_signal = Signal(
            direction="LONG", timeframe_minutes=10, level="A", reason="second",
            price=101.0, open_time=121_000, score=80.0, threshold=70.0,
            threshold_segment="WD-08", session_allowed=True,
        )
        expiry_kline = Kline(120_000, 100.0, 101.0, 100.0, 101.0, 1.0, 121_000)
        with patch("app.state.choose_trade_signal", return_value=second_signal):
            state.update_from_klines([expiry_kline])
        state.wait_for_storage_writes()

        self.assertEqual([call[0] for call in storage.atomic_calls], ["settled", "open"])
        self.assertEqual(storage.atomic_calls[0][3]["status"], "PENDING")
        self.assertEqual(storage.atomic_calls[1][3]["status"], "CONSUMED")
        self.assertEqual(storage.atomic_calls[1][2]["stake"], 18.0)
        entry_snapshot = storage.entry_snapshots[-1][2]
        self.assertEqual(entry_snapshot["stake_progression"]["active_second_orders"], 1)
        self.assertEqual(entry_snapshot["stake_progression_source_order_id"], 1)
        self.assertEqual(entry_snapshot["stake_progression_version"], TWO_STAGE_VERSION)
        self.assertEqual(entry_snapshot["stake_config"]["stake_progression_max_orders"], 2)
        self.assertEqual(entry_snapshot["stake_config"]["stake_progression_max_active"], 1)

        restarted = MonitorState(symbol="BTCUSDT", storage=storage, now_ms=lambda: 9_000)
        progression = restarted.snapshot()["stake_progression"]
        self.assertEqual(progression["active_second_orders"], 1)
        self.assertEqual(progression["pending_credits"], 0)
        self.assertEqual(restarted.simulator.stake_progression.activated_at, 1_000)

    def test_sqlite_progression_restart_keeps_activation_and_active_second_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            state = MonitorState(
                symbol="BTCUSDT",
                storage_path=db_path,
                enable_wave_guard=False,
                now_ms=lambda: 1_000,
            )
            first_signal = Signal(
                direction="LONG", timeframe_minutes=2, level="A", reason="first",
                price=100.0, open_time=1_000, score=80.0, threshold=70.0,
                threshold_segment="WD-08", session_allowed=True,
            )
            state._maybe_open_order(
                first_signal,
                Kline(0, 100.0, 100.0, 100.0, 100.0, 1.0, 1_000),
            )
            state.wait_for_storage_writes()
            second_signal = Signal(
                direction="LONG", timeframe_minutes=10, level="A", reason="second",
                price=101.0, open_time=121_000, score=80.0, threshold=70.0,
                threshold_segment="WD-08", session_allowed=True,
            )
            with patch("app.state.choose_trade_signal", return_value=second_signal):
                state.update_from_klines(
                    [Kline(120_000, 100.0, 101.0, 100.0, 101.0, 1.0, 121_000)]
                )
            state.wait_for_storage_writes()

            restarted = MonitorState(symbol="BTCUSDT", storage_path=db_path, now_ms=lambda: 99_000)
            snapshot = restarted.snapshot()

        self.assertEqual([order["stake"] for order in reversed(snapshot["orders"])], [10.0, 18.0])
        self.assertEqual(snapshot["stake_progression"]["active_second_orders"], 1)
        self.assertEqual(snapshot["stake_progression"]["pending_credits"], 0)
        self.assertEqual(restarted.simulator.stake_progression.activated_at, 1_000)

    def test_order_opened_before_persisted_activation_does_not_create_credit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.save_order(
                SimulatedOrder(
                    id=1, direction="LONG", timeframe_minutes=1, level="A", reason="pre-release",
                    entry_price=100.0, opened_at=0, expires_at=60_000,
                    threshold_segment="WD-08", stake_progression_step=1,
                    stake_progression_version=TWO_STAGE_VERSION,
                ),
                "BTCUSDT",
            )
            state = MonitorState(symbol="BTCUSDT", storage=store, now_ms=lambda: 1_000)
            state.update_from_klines(
                [Kline(1, 100.0, 101.0, 100.0, 101.0, 1.0, 60_000)]
            )
            state.wait_for_storage_writes()
            credits = store.load_stake_progression_credits("BTCUSDT", TWO_STAGE_VERSION)

        self.assertEqual(credits, [])
        self.assertEqual(state.simulator.orders[0].result, "WIN")

    def test_smaller_max_active_keeps_existing_active_orders_and_cancels_pending(self):
        storage = RecordingStorage()
        for order_id, source_id in ((10, 1), (11, 2)):
            storage._persist_order(
                SimulatedOrder(
                    id=order_id, direction="LONG", timeframe_minutes=10, level="A", reason="active",
                    entry_price=100.0, opened_at=200, expires_at=600_200,
                    stake=18.0, win_return=32.4, stake_progression_step=2,
                    stake_progression_source_order_id=source_id,
                    stake_progression_version=TWO_STAGE_VERSION,
                ),
                "BTCUSDT",
            )
            storage._persist_credit(
                StakeProgressionCredit(
                    source_order_id=source_id, created_at=100,
                    consumed_order_id=order_id, consumed_at=200, status="CONSUMED",
                ),
                "BTCUSDT",
            )
        storage._persist_credit(
            StakeProgressionCredit(source_order_id=3, created_at=300),
            "BTCUSDT",
        )

        state = MonitorState(
            symbol="BTCUSDT", storage=storage, stake_progression_max_active=1,
            now_ms=lambda: 1_000,
        )
        progression = state.snapshot()["stake_progression"]

        self.assertEqual(progression["max_active"], 1)
        self.assertEqual(progression["active_second_orders"], 2)
        self.assertEqual(progression["pending_credits"], 0)
        self.assertIn("取消 1 个待用资格", progression["recovery_warning"])
        self.assertEqual([order.status for order in state.simulator.orders], ["OPEN", "OPEN"])
        self.assertEqual(storage.credit_saves[-1][1]["status"], "CANCELLED")

    def test_recovery_cancel_credit_save_failure_aborts_construction(self):
        storage = RecordingStorage()
        storage._persist_order(
            SimulatedOrder(
                id=10, direction="LONG", timeframe_minutes=10, level="A", reason="active",
                entry_price=100.0, opened_at=200, expires_at=600_200,
                stake=18.0, win_return=32.4, stake_progression_step=2,
                stake_progression_source_order_id=1,
                stake_progression_version=TWO_STAGE_VERSION,
            ),
            "BTCUSDT",
        )
        storage._persist_credit(
            StakeProgressionCredit(
                source_order_id=1, created_at=100,
                consumed_order_id=10, consumed_at=200, status="CONSUMED",
            ),
            "BTCUSDT",
        )
        storage._persist_credit(
            StakeProgressionCredit(source_order_id=2, created_at=300),
            "BTCUSDT",
        )
        storage.fail_once("cancel_stake_progression_credits")

        with self.assertRaisesRegex(OSError, "cancel_stake_progression_credits failed"):
            MonitorState(
                symbol="BTCUSDT", storage=storage, stake_progression_max_active=1,
                now_ms=lambda: 1_000,
            )

    def test_open_storage_failure_rolls_back_base_order_without_side_effects(self):
        storage = RecordingStorage()
        webhook = RecordingWebhook()
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            webhook=webhook,
            enable_wave_guard=False,
            now_ms=lambda: 1_000,
        )
        storage.fail_once("save_open_order_decision")
        signal = Signal(
            direction="LONG", timeframe_minutes=10, level="A", reason="base",
            price=100.0, open_time=1_000, score=80.0, threshold=70.0,
            threshold_segment="WD-08", session_allowed=True,
        )

        decision = state._maybe_open_order(
            signal,
            Kline(0, 100.0, 100.0, 100.0, 100.0, 1.0, 1_000),
        )

        self.assertEqual(decision, "STORAGE_ERROR")
        self.assertEqual(storage.atomic_calls, [])
        self.assertEqual(state.simulator.orders, [])
        self.assertEqual(state.snapshot()["stats"]["total_orders"], 0)
        self.assertEqual(state.simulator.stats()["pending_credits"], 0)
        self.assertEqual(webhook.calls, [])
        self.assertEqual(storage.entry_snapshots, [])
        self.assertEqual(state.observations, [])
        self.assertIn("save_open_order_decision failed", state.last_error)

    def test_open_storage_failure_rolls_back_18u_order_and_restores_credit(self):
        storage = RecordingStorage()
        webhook = RecordingWebhook()
        state = MonitorState(
            symbol="BTCUSDT", storage=storage, webhook=webhook, now_ms=lambda: 0,
        )
        source = state.simulator.open_order(
            Signal(
                direction="LONG", timeframe_minutes=1, level="A", reason="source",
                price=100.0, open_time=0, threshold_segment="WD-08",
            ),
            100.0,
            0,
        )
        state.simulator.settle_expired_order_events(60_000, 101.0)
        storage.fail_once("save_open_order_decision")
        signal = Signal(
            direction="LONG", timeframe_minutes=10, level="A", reason="second",
            price=101.0, open_time=120_000, score=80.0, threshold=70.0,
            threshold_segment="WD-12", session_allowed=True,
        )

        decision = state._maybe_open_order(
            signal,
            Kline(120_000, 101.0, 101.0, 101.0, 101.0, 1.0, 120_000),
        )

        self.assertEqual(decision, "STORAGE_ERROR")
        self.assertEqual(state.simulator.orders, [source])
        self.assertEqual(state.simulator.stake_progression.credits[0].status, "PENDING")
        self.assertEqual(state.simulator.stats()["active_second_orders"], 0)
        self.assertEqual(webhook.calls, [])
        self.assertEqual(storage.entry_snapshots, [])

    def test_settlement_storage_failure_retries_before_opening_next_order(self):
        storage = RecordingStorage()
        webhook = RecordingWebhook()
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            webhook=webhook,
            enable_wave_guard=False,
            now_ms=lambda: 1_000,
        )
        first_signal = Signal(
            direction="LONG", timeframe_minutes=2, level="A", reason="first",
            price=100.0, open_time=1_000, score=80.0, threshold=70.0,
            threshold_segment="WD-08", session_allowed=True,
        )
        state._maybe_open_order(
            first_signal,
            Kline(0, 100.0, 100.0, 100.0, 100.0, 1.0, 1_000),
        )
        state.wait_for_storage_writes()
        storage.atomic_calls.clear()
        storage.entry_snapshots.clear()
        webhook.calls.clear()
        storage.fail_once("save_settled_order_with_credit")
        second_signal = Signal(
            direction="LONG", timeframe_minutes=10, level="A", reason="second",
            price=101.0, open_time=121_000, score=80.0, threshold=70.0,
            threshold_segment="WD-08", session_allowed=True,
        )
        expiry_kline = Kline(120_000, 100.0, 101.0, 100.0, 101.0, 1.0, 121_000)

        with patch("app.state.choose_trade_signal", return_value=second_signal):
            state.update_from_klines([expiry_kline])

        self.assertEqual(state.order_decision, "STORAGE_ERROR")
        self.assertEqual(len(state._pending_settlement_events), 1)
        self.assertEqual(len(state.simulator.orders), 1)
        self.assertEqual(webhook.calls, [])
        self.assertEqual(storage.entry_snapshots, [])

        with patch("app.state.choose_trade_signal", return_value=second_signal):
            state.update_from_klines([expiry_kline])
        state.wait_for_storage_writes()

        self.assertEqual([call[0] for call in storage.atomic_calls], ["settled", "open"])
        self.assertEqual(storage.atomic_calls[0][3]["status"], "PENDING")
        self.assertEqual(storage.atomic_calls[1][3]["status"], "CONSUMED")
        self.assertEqual(state._pending_settlement_events, [])
        self.assertEqual(state.simulator.orders[-1].stake, 18.0)
        self.assertEqual(len(webhook.calls), 1)

    def test_snapshot_waits_for_synchronous_open_storage_commit(self):
        storage = RecordingStorage()
        state = MonitorState(symbol="BTCUSDT", storage=storage, now_ms=lambda: 1_000)
        started = threading.Event()
        release = threading.Event()
        open_done = threading.Event()
        snapshot_done = threading.Event()
        original_save = storage.save_open_order_decision

        def blocking_save(**kwargs):
            started.set()
            release.wait(timeout=5)
            original_save(**kwargs)

        storage.save_open_order_decision = blocking_save
        signal = Signal(
            direction="LONG", timeframe_minutes=10, level="A", reason="locked",
            price=100.0, open_time=1_000, score=80.0, threshold=70.0,
            threshold_segment="WD-08", session_allowed=True,
        )

        opener = threading.Thread(
            target=lambda: (
                state._maybe_open_order(
                    signal,
                    Kline(0, 100.0, 100.0, 100.0, 100.0, 1.0, 1_000),
                ),
                open_done.set(),
            )
        )
        opener.start()
        self.assertTrue(started.wait(timeout=2))
        reader = threading.Thread(target=lambda: (state.snapshot(), snapshot_done.set()))
        reader.start()

        self.assertFalse(snapshot_done.wait(timeout=0.05))
        release.set()
        opener.join(timeout=2)
        reader.join(timeout=2)

        self.assertTrue(open_done.is_set())
        self.assertTrue(snapshot_done.is_set())

    def test_reset_symbol_keeps_queued_storage_writes_on_original_symbol(self):
        storage = RecordingStorage()
        state = MonitorState(symbol="BTCUSDT", storage=storage, now_ms=lambda: 1_000)
        storage.write_gate = threading.Event()
        signal = Signal(
            direction="LONG", timeframe_minutes=10, level="A", reason="queued",
            price=100.0, open_time=1_000, score=80.0, threshold=70.0,
            threshold_segment="WD-08", session_allowed=True,
        )
        state._maybe_open_order(
            signal,
            Kline(0, 100.0, 100.0, 100.0, 100.0, 1.0, 1_000),
        )
        state.reset_symbol("ETHUSDT")
        storage.write_gate.set()
        state.wait_for_storage_writes()

        self.assertEqual(storage.atomic_calls[0][0], "open")
        self.assertEqual(storage.atomic_calls[0][1], "BTCUSDT")
        self.assertEqual(storage.entry_snapshots[0][0], "BTCUSDT")

    def test_adaptive_admission_matrix_and_frozen_audit_snapshot(self):
        current_time = 1_800_000_000_000
        cases = (
            ("WARMUP", "FIRST", True, 10.0, True, "OPENED"),
            ("ACTIVE", "SECOND", True, 18.0, True, "OPENED"),
            (
                "WATCH",
                "FIRST",
                True,
                10.0,
                False,
                "OPENED",
            ),
            (
                "WATCH",
                "SECOND",
                False,
                0.0,
                False,
                "ADAPTIVE_PROFILE_SECOND_BLOCKED",
            ),
            (
                "PAUSED",
                "FIRST",
                False,
                0.0,
                False,
                "ADAPTIVE_PROFILE_PAUSED",
            ),
        )
        for status, slot, opens, stake, progression, expected in cases:
            with self.subTest(status=status, slot=slot):
                state = adaptive_admission_state(status, current_time)
                pending = StakeProgressionCredit(
                    source_order_id=77,
                    created_at=current_time - 1,
                    direction="LONG",
                )
                if status in {"ACTIVE", "WATCH"}:
                    state.simulator.stake_progression.credits.append(pending)
                if slot == "SECOND":
                    state.simulator.orders.append(
                        SimulatedOrder(
                            id=88,
                            direction="LONG",
                            timeframe_minutes=10,
                            level="A",
                            reason="existing same-direction order",
                            entry_price=100.0,
                            opened_at=current_time - 60_000,
                            expires_at=current_time + 540_000,
                        )
                    )
                    state.simulator._next_id = 89

                candidate = selected_profile_signal(current_time)
                if not opens:
                    candidate = replace(
                        candidate,
                        observe_direction="",
                        observe_only=False,
                    )
                decision = state._maybe_open_order(
                    candidate,
                    latest_kline(current_time),
                    daily_profile_required=True,
                )

                self.assertEqual(decision, expected)
                opened = [item for item in state.simulator.orders if item.id != 88]
                self.assertEqual(bool(opened), opens)
                if opens:
                    order = opened[-1]
                    self.assertEqual(order.stake, stake)
                    self.assertEqual(order.adaptive_profile_state["status"], status)
                    self.assertEqual(
                        order.adaptive_profile_state["qualification_state"],
                        "QUALIFIED",
                    )
                    self.assertEqual(
                        order.decision_inputs["context"]["n12_n20"]["profile_key"],
                        PROFILE_KEY,
                    )
                    self.assertEqual(
                        order.decision_inputs["signal"]["adaptive_profile_state"][
                            "status"
                        ],
                        status,
                    )
                    self.assertEqual(
                        order.decision_inputs["admission"]["stake"][
                            "selected_order_terms"
                        ]["allow_progression"],
                        progression,
                    )
                else:
                    self.assertEqual(len(state.observations), 1)
                    observed = state.observations[0]
                    self.assertEqual(observed.adaptive_profile_state["status"], status)
                    self.assertEqual(observed.first_decisive_block, "ADAPTIVE_PROFILE")
                    self.assertEqual(observed.decision_id, state.selected_signal.decision_id)
                    self.assertEqual(
                        observed.decision_inputs["signal"]["adaptive_profile_state"][
                            "status"
                        ],
                        status,
                    )
                state.close()

    def test_watch_first_preserves_pending_credit_and_win_does_not_create_credit(self):
        current_time = 1_800_000_000_000
        state = adaptive_admission_state("WATCH", current_time)
        pending = StakeProgressionCredit(
            source_order_id=77,
            created_at=current_time - 1,
            direction="LONG",
        )
        state.simulator.stake_progression.credits.append(pending)

        decision = state._maybe_open_order(
            selected_profile_signal(current_time),
            latest_kline(current_time),
            daily_profile_required=True,
        )
        event = state.simulator.settle_expired_order_events(
            current_time + 10 * MINUTE_MS,
            101.0,
        )[0]

        self.assertEqual(decision, "OPENED")
        self.assertEqual(event.order.stake, 10.0)
        self.assertEqual(event.order.adaptive_profile_state["status"], "WATCH")
        self.assertIsNone(event.progression_credit)
        self.assertEqual(state.simulator.stake_progression.credits, [pending])
        self.assertEqual(pending.status, "PENDING")
        state.close()

    def test_adaptive_committed_decision_reuses_same_closed_kline(self):
        current_time = 1_800_000_000_000
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            state = adaptive_admission_state(
                "ACTIVE",
                current_time,
                storage=store,
            )
            signal = selected_profile_signal(current_time)
            latest = latest_kline(current_time)

            first = state._maybe_open_order(
                signal,
                latest,
                daily_profile_required=True,
            )
            first_order = state.simulator.orders[0]
            first_decision_id = first_order.decision_id
            identity = first_order.decision_inputs["identity"]["candidate_identity"]
            self.assertEqual(
                set(identity),
                {
                    "candidate_origin",
                    "candidate_ordinal",
                    "direction",
                    "profile_key",
                    "strategy_family",
                    "strategy_tag",
                    "order_slot",
                    "order_slot_scope",
                    "timeframe_minutes",
                    "threshold_segment",
                },
            )
            self.assertEqual(
                first_order.decision_inputs["signal"]["adaptive_profile_state"][
                    "status"
                ],
                "ACTIVE",
            )
            state.adaptive_profile_states[PROFILE_KEY] = adaptive_profile_snapshot(
                "WATCH",
                current_time + 1,
            )
            replay = state._maybe_open_order(
                replace(signal, reason="recomputed duplicate"),
                latest,
                daily_profile_required=True,
            )

            self.assertEqual((first, replay), ("OPENED", "OPENED"))
            self.assertEqual(len(state.simulator.orders), 1)
            self.assertEqual(len(store.load_orders("BTCUSDT")), 1)
            self.assertEqual(state.simulator.orders[0].decision_id, first_decision_id)
            self.assertEqual(
                state.selected_signal.adaptive_profile_state["status"],
                "ACTIVE",
            )
            self.assertEqual(
                state.simulator.orders[0].order_slot,
                "FIRST",
            )
            state.close()
            store.close()

    def test_stale_selected_boolean_without_exact_membership_is_not_qualified(self):
        current_time = 1_800_000_000_000
        state = adaptive_admission_state("ACTIVE", current_time)
        state.active_daily_profile_selection["selected_profiles"] = []
        stale = selected_profile_signal(current_time)

        decision = state._maybe_open_order(
            stale,
            latest_kline(current_time),
            daily_profile_required=True,
        )

        self.assertEqual(decision, "DAILY_PROFILE_NOT_SELECTED")
        self.assertEqual(state.simulator.orders, [])
        self.assertEqual(
            state.selected_signal.adaptive_profile_state["qualification_state"],
            "NOT_QUALIFIED",
        )
        self.assertEqual(state.selected_signal.first_decisive_block, "DAILY_PROFILE")
        state.close()

    def test_adaptive_slot_is_same_direction_only_before_context_freeze(self):
        current_time = 1_800_000_000_000
        state = adaptive_admission_state("WATCH", current_time)
        state.simulator.orders.append(
            SimulatedOrder(
                id=91,
                direction="SHORT",
                timeframe_minutes=10,
                level="A",
                reason="opposite direction",
                entry_price=100.0,
                opened_at=current_time - MINUTE_MS,
                expires_at=current_time + 9 * MINUTE_MS,
            )
        )
        state.simulator._next_id = 92

        decision = state._maybe_open_order(
            selected_profile_signal(current_time),
            latest_kline(current_time),
            daily_profile_required=True,
        )

        self.assertEqual(decision, "OPENED")
        opened = state.simulator.orders[-1]
        self.assertEqual(opened.order_slot, "FIRST")
        self.assertEqual(opened.decision_inputs["identity"]["order_slot"], "FIRST")
        self.assertEqual(opened.adaptive_profile_state["status"], "WATCH")
        state.close()

    def test_adaptive_state_never_promotes_unqualified_or_wait_candidate(self):
        current_time = 1_800_000_000_000
        state = adaptive_admission_state("ACTIVE", current_time)
        unqualified = replace(
            selected_profile_signal(current_time),
            daily_profile_selected=False,
        )

        blocked = state._maybe_open_order(
            unqualified,
            latest_kline(current_time),
            daily_profile_required=True,
        )
        waiting = state._maybe_open_order(
            Signal(
                "WAIT",
                10,
                "B",
                "no candidate",
                100.0,
                current_time,
                threshold_segment="WD-08",
                strategy_family="drop_reclaim",
                strategy_tag="live_profile",
                profile_key=PROFILE_KEY,
                threshold=1.0,
            ),
            latest_kline(current_time + MINUTE_MS),
        )

        self.assertEqual(blocked, "DAILY_PROFILE_NOT_SELECTED")
        self.assertEqual(waiting, "BELOW_THRESHOLD")
        self.assertEqual(state.simulator.orders, [])
        state.close()

    def test_adaptive_blocked_observations_use_exact_five_part_identity(self):
        current_time = 1_800_000_000_000
        state = adaptive_admission_state("PAUSED", current_time)
        first = selected_profile_signal(current_time)
        second_key = "10|drop_reclaim|other_profile|LONG|WD-08"
        second = replace(
            first,
            strategy_tag="other_profile",
            profile_key=second_key,
            open_time=current_time + 1,
        )
        state.adaptive_profile_states[second_key] = adaptive_profile_snapshot(
            "PAUSED",
            current_time - 1,
            profile_key=second_key,
        )
        state.active_daily_profile_selection["selected_profiles"].append(
            {
                "key": second_key,
                "qualification_state": "QUALIFIED",
                "fast_7d": {},
                "stable_14d": {},
            }
        )

        first_decision = state._maybe_open_order(
            first,
            latest_kline(current_time),
            daily_profile_required=True,
        )
        second_decision = state._maybe_open_order(
            second,
            latest_kline(current_time),
            daily_profile_required=True,
        )

        self.assertEqual(first_decision, "ADAPTIVE_PROFILE_PAUSED")
        self.assertEqual(second_decision, "ADAPTIVE_PROFILE_PAUSED")
        self.assertEqual(len(state.observations), 2)
        self.assertEqual(
            {item.profile_key for item in state.observations},
            {PROFILE_KEY, second_key},
        )
        self.assertEqual(len({item.observation_key for item in state.observations}), 2)
        state.close()

    def test_settlement_commit_precedes_exact_key_refresh_with_strict_cutoff(self):
        class OrderedStorage(RecordingStorage):
            def __init__(self):
                super().__init__()
                self.events = []

            def save_observations(self, observations, symbol):
                self.events.append("settlement_committed")
                self.observations.extend(
                    (symbol, replace(item).to_dict()) for item in observations
                )

        current_time = 1_800_000_000_000
        storage = OrderedStorage()
        state = adaptive_admission_state("WARMUP", current_time, storage=storage)
        opened_at = current_time - 10 * MINUTE_MS
        pending = replace(
            settled_observation(
                501,
                "WIN",
                opened_at,
                family="drop_reclaim",
                tag="live_profile",
                segment="WD-08",
            ),
            observation_key="adaptive-pending",
            status="OPEN",
            result=None,
            exit_price=None,
            settled_at=None,
            pnl=0.0,
            profile_key=PROFILE_KEY,
        )
        state.observations = [pending]
        cutoffs = []
        original_refresh = state._refresh_adaptive_profile_keys

        def recording_refresh(keys, evaluated_at):
            storage.events.append(f"refresh:{next(iter(keys))}")
            cutoffs.append(evaluated_at)
            return original_refresh(keys, evaluated_at)

        state._refresh_adaptive_profile_keys = recording_refresh

        settled = state._settle_observations(current_time, 101.0)

        self.assertEqual(settled, [pending])
        self.assertEqual(
            storage.events[:2],
            ["settlement_committed", f"refresh:{PROFILE_KEY}"],
        )
        self.assertEqual(cutoffs, [current_time + 1])
        state.close()

    def test_refresh_failure_preserves_committed_settlement_and_prior_state(self):
        class OrderedStorage(RecordingStorage):
            def save_observations(self, observations, symbol):
                self.observations.extend(
                    (symbol, replace(item).to_dict()) for item in observations
                )

        current_time = 1_800_000_000_000
        storage = OrderedStorage()
        state = adaptive_admission_state("WARMUP", current_time, storage=storage)
        prior = deepcopy(state.adaptive_profile_states[PROFILE_KEY])
        pending = replace(
            settled_observation(
                502,
                "WIN",
                current_time - 10 * MINUTE_MS,
                family="drop_reclaim",
                tag="live_profile",
                segment="WD-08",
            ),
            observation_key="adaptive-refresh-failure",
            status="OPEN",
            result=None,
            exit_price=None,
            settled_at=None,
            pnl=0.0,
            profile_key=PROFILE_KEY,
        )
        state.observations = [pending]

        with patch(
            "app.state.rebuild_adaptive_profile_states",
            side_effect=RuntimeError("adaptive rebuild failed"),
        ):
            settled = state._settle_observations(current_time, 101.0)

        self.assertEqual(settled, [pending])
        self.assertEqual(pending.status, "SETTLED")
        self.assertEqual(storage.observations[-1][1]["status"], "SETTLED")
        self.assertEqual(state.adaptive_profile_states[PROFILE_KEY], prior)
        self.assertIn("adaptive rebuild failed", state.last_error)

        state._refresh_adaptive_profile_keys({PROFILE_KEY}, current_time + 2)
        self.assertIsNone(state.last_error)
        self.assertEqual(
            state.adaptive_profile_states[PROFILE_KEY]["n12"]["sample_size"],
            1,
        )
        state.close()

    def test_adaptive_restart_rebuilds_at_least_fifteen_days_and_reset_isolated(self):
        current_time = 20 * 86_400_000
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            start = current_time - 14 * 86_400_000
            rows = [
                settled_observation(
                    index,
                    "WIN" if index < 5 else "LOSS",
                    start + index * 11 * MINUTE_MS,
                    family="drop_reclaim",
                    tag="live_profile",
                    segment="WD-08",
                )
                for index in range(20)
            ]
            for row in rows:
                store.save_observation(row, "BTCUSDT")
            eth_row = settled_observation(
                900,
                "WIN",
                current_time - 11 * MINUTE_MS,
                family="other",
                tag="eth_profile",
                segment="WD-09",
            )
            store.save_observation(eth_row, "ETHUSDT")

            first = MonitorState(
                symbol="BTCUSDT",
                storage=store,
                now_ms=lambda: current_time,
            )
            before = deepcopy(first.adaptive_profile_states)
            first.close()
            restarted = MonitorState(
                symbol="BTCUSDT",
                storage=store,
                now_ms=lambda: current_time,
            )

            self.assertEqual(restarted.adaptive_profile_states, before)
            self.assertEqual(restarted.adaptive_profile_states[PROFILE_KEY]["status"], "PAUSED")
            restarted.reset_symbol("ETHUSDT")
            self.assertNotIn(PROFILE_KEY, restarted.adaptive_profile_states)
            self.assertEqual(
                set(restarted.adaptive_profile_states),
                {"10|other|eth_profile|LONG|WD-09"},
            )
            restarted.close()
            store.close()

    def test_constructor_keeps_adaptive_rebuild_error_visible(self):
        with patch(
            "app.state.rebuild_adaptive_profile_states",
            side_effect=RuntimeError("startup adaptive rebuild failed"),
        ):
            state = MonitorState(
                symbol="BTCUSDT",
                now_ms=lambda: 1_800_000_000_000,
            )

        self.assertIn("startup adaptive rebuild failed", state.last_error)
        state.close()

    def test_legacy_profile_incremental_refresh_equals_restart(self):
        current_time = 1_800_000_000_000
        legacy = replace(
            settled_observation(
                901,
                "WIN",
                current_time - 11 * MINUTE_MS,
                family="drop_reclaim",
                tag="live_profile",
                segment="WD-08",
            ),
            profile_key="drop_reclaim|LONG|WD-08",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            state = MonitorState(
                symbol="BTCUSDT",
                storage=store,
                now_ms=lambda: current_time,
            )
            store.save_observation(legacy, "BTCUSDT")

            state._refresh_adaptive_profile_keys({PROFILE_KEY}, current_time)
            incremental = deepcopy(state.adaptive_profile_states[PROFILE_KEY])
            state.close()
            restarted = MonitorState(
                symbol="BTCUSDT",
                storage=store,
                now_ms=lambda: current_time,
            )

            self.assertEqual(incremental, restarted.adaptive_profile_states[PROFILE_KEY])
            self.assertEqual(incremental["n12"]["sample_size"], 1)
            restarted.close()
            store.close()

    def test_constructor_adaptive_load_failure_uses_fallback_and_recovers(self):
        current_time = 1_800_000_000_000
        row = settled_observation(
            902,
            "WIN",
            current_time - 11 * MINUTE_MS,
            family="drop_reclaim",
            tag="live_profile",
            segment="WD-08",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.save_observation(row, "BTCUSDT")
            original_loader = store.load_adaptive_profile_observations
            calls = 0

            def fail_once(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError("adaptive loader unavailable")
                return original_loader(*args, **kwargs)

            with patch.object(
                store,
                "load_adaptive_profile_observations",
                side_effect=fail_once,
            ):
                state = MonitorState(
                    symbol="BTCUSDT",
                    storage=store,
                    now_ms=lambda: current_time,
                )
                self.assertEqual(
                    state.adaptive_profile_states[PROFILE_KEY]["n12"]["sample_size"],
                    1,
                )
                self.assertIn("adaptive loader unavailable", state.last_error)

                state._refresh_adaptive_profile_keys({PROFILE_KEY}, current_time + 1)

            self.assertIsNone(state.last_error)
            state.close()
            store.close()

    def test_constructor_adaptive_load_and_fallback_failure_uses_empty_state(self):
        current_time = 1_800_000_000_000

        class BrokenAdaptiveStorage(RecordingStorage):
            def load_observations(self, symbol):
                return [
                    replace(
                        settled_observation(
                            905,
                            "WIN",
                            current_time - 11 * MINUTE_MS,
                        ),
                        settled_at="invalid-settled-at",
                    )
                ]

            def load_adaptive_profile_observations(self, *args, **kwargs):
                raise OSError("dedicated adaptive load failed")

        state = MonitorState(
            symbol="BTCUSDT",
            storage=BrokenAdaptiveStorage(),
            now_ms=lambda: current_time,
        )

        self.assertEqual(state.adaptive_profile_states, {})
        self.assertIn("dedicated adaptive load failed", state.last_error)
        self.assertIn("fallback", state.last_error)
        state.close()

    def test_reset_symbol_adaptive_load_failure_keeps_target_state_coherent(self):
        current_time = 1_800_000_000_000
        btc_order = SimulatedOrder(
            id=41,
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="btc old symbol",
            entry_price=100.0,
            opened_at=current_time - MINUTE_MS,
            expires_at=current_time + 9 * MINUTE_MS,
        )
        eth_order = replace(btc_order, id=42, reason="eth target symbol")
        eth_row = settled_observation(
            903,
            "WIN",
            current_time - 11 * MINUTE_MS,
            family="other",
            tag="eth_profile",
            segment="WD-09",
        )
        eth_key = "10|other|eth_profile|LONG|WD-09"
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.save_order(btc_order, "BTCUSDT")
            store.save_order(eth_order, "ETHUSDT")
            store.save_observation(eth_row, "ETHUSDT")
            state = MonitorState(
                symbol="BTCUSDT",
                storage=store,
                now_ms=lambda: current_time,
            )
            original_loader = store.load_adaptive_profile_observations
            calls = 0

            def fail_once(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError("reset adaptive loader unavailable")
                return original_loader(*args, **kwargs)

            with patch.object(
                store,
                "load_adaptive_profile_observations",
                side_effect=fail_once,
            ):
                state.reset_symbol("ETHUSDT")
                self.assertEqual(state.symbol, "ETHUSDT")
                self.assertEqual([item.id for item in state.simulator.orders], [42])
                self.assertEqual(
                    {item.strategy_tag for item in state.observations},
                    {"eth_profile"},
                )
                self.assertEqual(
                    state.adaptive_profile_states[eth_key]["n12"]["sample_size"],
                    1,
                )
                self.assertIn("reset adaptive loader unavailable", state.last_error)

                state._refresh_adaptive_profile_keys({eth_key}, current_time + 1)

            self.assertIsNone(state.last_error)
            self.assertNotIn(PROFILE_KEY, state.adaptive_profile_states)
            state.close()
            store.close()

    def test_reset_symbol_adaptive_rebuild_failure_keeps_target_state_coherent(self):
        current_time = 1_800_000_000_000
        target_order = SimulatedOrder(
            id=52,
            direction="SHORT",
            timeframe_minutes=10,
            level="A",
            reason="eth rebuild target",
            entry_price=100.0,
            opened_at=current_time - MINUTE_MS,
            expires_at=current_time + 9 * MINUTE_MS,
        )
        target_row = settled_observation(
            904,
            "WIN",
            current_time - 11 * MINUTE_MS,
            family="other",
            tag="eth_rebuild_profile",
            segment="WD-10",
        )
        target_key = "10|other|eth_rebuild_profile|LONG|WD-10"
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.save_order(target_order, "ETHUSDT")
            store.save_observation(target_row, "ETHUSDT")
            state = MonitorState(
                symbol="BTCUSDT",
                storage=store,
                now_ms=lambda: current_time,
            )

            with patch(
                "app.state.rebuild_adaptive_profile_states",
                side_effect=RuntimeError("reset adaptive rebuild failed"),
            ):
                state.reset_symbol("ETHUSDT")

            self.assertEqual(state.symbol, "ETHUSDT")
            self.assertEqual([item.id for item in state.simulator.orders], [52])
            self.assertEqual(
                {item.strategy_tag for item in state.observations},
                {"eth_rebuild_profile"},
            )
            self.assertEqual(state.adaptive_profile_states, {})
            self.assertIn("reset adaptive rebuild failed", state.last_error)

            state._refresh_adaptive_profile_keys({target_key}, current_time + 1)

            self.assertEqual(
                state.adaptive_profile_states[target_key]["n12"]["sample_size"],
                1,
            )
            self.assertIsNone(state.last_error)
            state.close()
            store.close()


class EntryStructureDecisionIntegrationTest(unittest.TestCase):
    class CountingDetector:
        def __init__(self, *, error: Exception | None = None):
            self.config = StructureConfig()
            self.error = error
            self.calls = []

        def detect(self, symbol, closed_klines):
            self.calls.append((symbol, closed_klines[-1].close_time))
            if self.error is not None:
                raise self.error
            return {
                "version": "ENTRY_STRUCTURE_SHADOW_V1",
                "mode": "SHADOW_ONLY",
                "status": "INSUFFICIENT_DATA",
                "symbol": symbol,
                "evaluated_at": closed_klines[-1].close_time,
                "atr": None,
                "levels": [],
                "nearest_support": None,
                "nearest_resistance": None,
            }

    class CountingStateMachine:
        def __init__(self, config, *, error: Exception | None = None):
            self.config = config
            self.error = error
            self.calls = []

        def evaluate(self, detected, closed_klines):
            self.calls.append(closed_klines[-1].close_time)
            if self.error is not None:
                raise self.error
            return []

    @staticmethod
    def _install_structure_pipeline(state, *, detector_error=None, state_error=None):
        detector = EntryStructureDecisionIntegrationTest.CountingDetector(
            error=detector_error
        )
        machine = EntryStructureDecisionIntegrationTest.CountingStateMachine(
            detector.config,
            error=state_error,
        )
        state._entry_structure_detector = detector
        state._entry_structure_state_machine = machine
        state._entry_structure_gate = EntryStructureGate(detector, machine)
        state._entry_structure_market_cache_key = None
        state._entry_structure_market_cache = None
        return detector, machine

    @staticmethod
    def _run_update(state, bars):
        def formal(klines, fear_greed=None):
            return replace(
                selected_profile_signal(klines[-1].close_time),
                open_time=klines[-1].open_time,
                daily_profile_selected=False,
                daily_profile_version="",
            )

        def analyzed(klines, timeframe_minutes, fear_greed=None):
            return formal(klines)

        def research(klines, timeframe_minutes, fear_greed=None):
            return [
                replace(
                    formal(klines),
                    direction="WAIT",
                    observe_direction="SHORT",
                    observe_only=True,
                    strategy_family="short_extension",
                    strategy_tag="task14_research",
                    profile_key="",
                    score=-60.0,
                    threshold=70.0,
                    session_allowed=False,
                )
            ]

        with (
            patch("app.state.analyze_volume_price", side_effect=analyzed),
            patch("app.state.analyze_observation_signals", side_effect=research),
            patch("app.state.choose_trade_signal", side_effect=formal),
        ):
            return state.update_from_klines(bars)

    def test_one_raw_snapshot_per_closed_kline_is_shared_cached_and_reset(self):
        state = MonitorState(
            symbol="BTCUSDT",
            min_order_gap_ms=0,
            enable_rolling_edge_guard=False,
            enable_observation_profile_promotion=False,
            result_sequence_guard_config=ResultSequenceGuardConfig(enabled=False),
        )
        self.addCleanup(state.close)
        detector, machine = self._install_structure_pipeline(state)
        bars = [kline(index, 100.0 + index / 100.0, 100.0) for index in range(30)]

        self.assertTrue(self._run_update(state, bars))
        self.assertEqual(len(detector.calls), 1)
        self.assertEqual(len(machine.calls), 1)
        self.assertEqual(
            state.selected_signal.entry_structure_shadow["candidate_origin"],
            "NATIVE_ACTIONABLE",
        )
        research = next(
            item
            for item in state.observations
            if item.candidate_origin == "RESEARCH_OBSERVATION"
        )
        self.assertEqual(research.direction, "SHORT")
        self.assertEqual(
            research.entry_structure_shadow["candidate_direction"], "SHORT"
        )

        self.assertTrue(self._run_update(state, bars))
        self.assertEqual(len(detector.calls), 1)
        next_bars = [*bars, kline(30, 100.3, 100.0)]
        self.assertTrue(self._run_update(state, next_bars))
        self.assertEqual(len(detector.calls), 2)

        state.reset_symbol("BTCUSDT")
        self.assertTrue(self._run_update(state, bars))
        self.assertEqual(len(detector.calls), 3)
        self.assertEqual(len(machine.calls), 3)

    def test_open_bundle_copies_one_structure_value_without_shared_references(self):
        storage = RecordingStorage()
        state = MonitorState(
            symbol="BTCUSDT",
            storage=storage,
            min_order_gap_ms=0,
            enable_rolling_edge_guard=False,
            enable_observation_profile_promotion=False,
            result_sequence_guard_config=ResultSequenceGuardConfig(enabled=False),
        )
        self.addCleanup(state.close)
        self._install_structure_pipeline(state)
        bars = [kline(index, 100.0 + index / 100.0, 100.0) for index in range(30)]

        self.assertTrue(self._run_update(state, bars))

        selected = state.selected_signal
        order = next(item for item in state.simulator.orders if item.decision_id == selected.decision_id)
        observation = next(
            item for item in state.observations if item.decision_id == selected.decision_id
        )
        context_structure = selected.decision_inputs["entry_structure"]
        signal_structure = selected.decision_inputs["signal"]["entry_structure_shadow"]
        structures = (
            selected.entry_structure_shadow,
            context_structure,
            signal_structure,
            observation.entry_structure_shadow,
            order.entry_structure_shadow,
        )
        for structure in structures[1:]:
            self.assertEqual(structure, structures[0])
            self.assertIsNot(structure, structures[0])
        self.assertIsNot(context_structure, signal_structure)

    def test_detector_and_state_machine_failures_are_shadow_only(self):
        outcomes = []
        for failure in (None, "detector", "state"):
            webhook = RecordingWebhook()
            state = MonitorState(
                symbol="BTCUSDT",
                webhook=webhook,
                min_order_gap_ms=0,
                enable_rolling_edge_guard=False,
                enable_observation_profile_promotion=False,
                result_sequence_guard_config=ResultSequenceGuardConfig(enabled=False),
            )
            self.addCleanup(state.close)
            self._install_structure_pipeline(
                state,
                detector_error=(RuntimeError("detector failed") if failure == "detector" else None),
                state_error=(RuntimeError("state failed") if failure == "state" else None),
            )
            bars = [kline(index, 100.0 + index / 100.0, 100.0) for index in range(30)]

            self.assertTrue(self._run_update(state, bars))
            order = state.simulator.orders[0]
            outcomes.append(
                (
                    order.id,
                    order.direction,
                    order.opened_at,
                    order.expires_at,
                    order.stake,
                    order.stake_progression_step,
                    list(webhook.calls),
                )
            )
            self.assertEqual(order.entry_structure_shadow["entry_structure_mode"], "SHADOW_ONLY")
            if failure is not None:
                self.assertEqual(order.entry_structure_shadow["entry_structure_state"], "ERROR")

        self.assertEqual(outcomes[1:], [outcomes[0], outcomes[0]])


if __name__ == "__main__":
    unittest.main()
