import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.daily_profile_selector import DailyProfileSelectorConfig
from app.models import FearGreedContext, Kline, ObservationSignal, Signal, SimulatedOrder
from app.result_sequence_guard import ResultSequenceGuardConfig
from app.rolling_edge import RollingEdgeConfig
from app.state import MonitorState
from app.stake_progression import TWO_STAGE_VERSION, StakeProgressionCredit
from app.storage import SQLiteMonitorStore
from app.wave_state import WaveSnapshot, advance_wave, analyze_wave
from app.wave_batch_guard import WaveBatchGuardConfig


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

    def save_signal(self, symbol, signal, decision, created_at_ms):
        self._wait_for_write_gate()
        self.signals.append((symbol, signal.to_dict(), decision, created_at_ms))

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


class MonitorStateTest(unittest.TestCase):
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

    def test_wave_global_cooldown_refreshes_and_cancels_credit_while_signal_waits(self):
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
        self.assertEqual(state.simulator.stake_progression.credits[-1].status, "CANCELLED")

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
            StakeProgressionCredit(source_order_id=1, created_at=600_000)
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
            StakeProgressionCredit(source_order_id=1, created_at=600_000)
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
        self.assertEqual(state.simulator.stake_progression.credits[0].status, "CANCELLED")

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
        self.assertEqual(
            state.snapshot()["order_policy"],
            {"max_open_orders": 2, "min_order_gap_ms": 2 * 60_000},
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

    def test_wd23_short_can_consume_second_stage_credit_despite_base_only_compat_config(self):
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
        self.assertEqual(opened.stake, 18.0)
        self.assertEqual(opened.stake_progression_step, 2)
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
            direction="SHORT", timeframe_minutes=10, level="A", reason="second",
            price=101.0, open_time=121_000, score=-80.0, threshold=70.0,
            threshold_segment="WD-23", session_allowed=True,
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
                direction="SHORT", timeframe_minutes=10, level="A", reason="second",
                price=101.0, open_time=121_000, score=-80.0, threshold=70.0,
                threshold_segment="WD-23", session_allowed=True,
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
        storage.fail_once("save_open_order_with_credit")
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
        self.assertEqual(state.simulator.orders, [])
        self.assertEqual(state.simulator.stats()["pending_credits"], 0)
        self.assertEqual(webhook.calls, [])
        self.assertEqual(storage.entry_snapshots, [])
        self.assertEqual(state.observations, [])
        self.assertIn("save_open_order_with_credit failed", state.last_error)

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
        storage.fail_once("save_open_order_with_credit")
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
            direction="SHORT", timeframe_minutes=10, level="A", reason="second",
            price=101.0, open_time=121_000, score=-80.0, threshold=70.0,
            threshold_segment="WD-23", session_allowed=True,
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
        original_save = storage.save_open_order_with_credit

        def blocking_save(order, symbol, credit):
            started.set()
            release.wait(timeout=5)
            original_save(order, symbol, credit)

        storage.save_open_order_with_credit = blocking_save
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


if __name__ == "__main__":
    unittest.main()
