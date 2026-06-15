import tempfile
import unittest
from pathlib import Path

from app.models import FearGreedContext, Kline, Signal, SimulatedOrder
from app.rolling_edge import RollingEdgeConfig
from app.state import MonitorState


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
        self.entry_snapshots = []
        self.settlements = []

    def load_orders(self, symbol):
        return []

    def save_order(self, order, symbol):
        self.orders.append((symbol, order.to_dict()))

    def save_signal(self, symbol, signal, decision, created_at_ms):
        self.signals.append((symbol, signal.to_dict(), decision, created_at_ms))

    def save_order_entry_snapshot(self, order, symbol, entry_snapshot):
        self.entry_snapshots.append((symbol, order.to_dict(), entry_snapshot))

    def update_order_entry_snapshot_settlement(self, order, symbol):
        self.settlements.append((symbol, order.to_dict()))


class MonitorStateTest(unittest.TestCase):
    def test_state_restores_persisted_orders_from_sqlite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            klines = actionable_rebound_klines()
            state = MonitorState(symbol="BTCUSDT", storage_path=db_path)
            state.update_from_klines(klines)
            state.wait_for_storage_writes()

            restored = MonitorState(symbol="BTCUSDT", storage_path=db_path)
            snapshot = restored.snapshot()

        self.assertEqual(snapshot["stats"]["total_orders"], 1)
        self.assertEqual(snapshot["orders"][0]["status"], "OPEN")

    def test_risk_pause_after_three_segment_losses(self):
        state = MonitorState(symbol="BTCUSDT")
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

        decision = state._maybe_open_order(signal, kline(70, 100.0, 100))

        self.assertEqual(decision, "RISK_PAUSED")
        self.assertIn("连续亏损", state.snapshot()["risk_pause"])

    def test_daily_drawdown_does_not_pause_when_segment_is_not_losing(self):
        state = MonitorState(symbol="BTCUSDT")
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
        state = MonitorState(symbol="BTCUSDT")

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
        state = MonitorState(symbol="BTCUSDT", webhook=webhook)

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
        state = MonitorState(symbol="BTCUSDT", webhook=webhook)
        signal = Signal(
            direction="SHORT",
            timeframe_minutes=10,
            level="A",
            reason="量平价跌：MACD/RSI确认弱势延续",
            price=100.0,
            open_time=4_200_000,
            score=-90.0,
            threshold=70.0,
            threshold_segment="WD-02",
            session_allowed=True,
        )

        decision = state._maybe_open_order(signal, kline(70, 100.0, 100))
        snapshot = state.snapshot()

        self.assertEqual(decision, "SHORT_OBSERVE_ONLY")
        self.assertEqual(snapshot["stats"]["total_orders"], 0)
        self.assertEqual(webhook.calls, [])
        self.assertIn("SHORT观察模式", snapshot["risk_pause"])

    def test_state_uses_configured_stake_terms_for_orders_and_webhook(self):
        klines = actionable_rebound_klines()
        webhook = RecordingWebhook()
        state = MonitorState(symbol="BTCUSDT", webhook=webhook, stake=20.0, win_return=36.0)

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
        self.assertFalse(state.snapshot()["stats"]["stake_progression_enabled"])
        self.assertEqual(state.snapshot()["stats"]["stake_progression_max_orders"], 5)

    def test_state_does_not_reopen_while_order_is_open(self):
        klines = actionable_rebound_klines()
        state = MonitorState(symbol="BTCUSDT")

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

    def test_state_records_order_entry_snapshot_and_settlement_asynchronously(self):
        klines = actionable_rebound_klines()
        storage = RecordingStorage()
        state = MonitorState(symbol="BTCUSDT", storage=storage)

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
        self.assertEqual(entry_snapshot["rolling_edge"]["status"], "NORMAL")
        self.assertEqual(entry_snapshot["latest_kline"]["close"], order_payload["entry_price"])
        self.assertEqual(entry_snapshot["stake_config"]["stake"], 10.0)
        self.assertEqual(len(storage.settlements), 1)
        self.assertEqual(storage.settlements[0][1]["status"], "SETTLED")


if __name__ == "__main__":
    unittest.main()
