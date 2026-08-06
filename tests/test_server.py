import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from app.history import WarmupReport
from app.models import Kline, ObservationSignal, SimulatedOrder
from app.server import apply_warmup, make_handler, start_polling
from app.state import MonitorState
from app.storage import SQLiteMonitorStore


class OrdersApiTest(unittest.TestCase):
    def test_polling_discards_response_when_symbol_changes_during_request(self):
        updated = threading.Event()

        class NotifyingState(MonitorState):
            def update_from_klines(self, klines, **kwargs):
                try:
                    return super().update_from_klines(klines, **kwargs)
                finally:
                    updated.set()

        state = NotifyingState(symbol="BTCUSDT")

        class SwitchingClient:
            def get_klines(self, symbol, interval, limit):
                self.requested_symbol = symbol
                state.reset_symbol("ETHUSDT")
                return [Kline(0, 100.0, 101.0, 99.0, 100.5, 10.0, 59_999)]

        client = SwitchingClient()
        start_polling(state, client, poll_seconds=3_600, limit=100)

        self.assertTrue(updated.wait(timeout=2))
        self.assertEqual(client.requested_symbol, "BTCUSDT")
        self.assertEqual(state.snapshot()["symbol"], "ETHUSDT")
        self.assertEqual(state.snapshot()["kline_count"], 0)

    def test_warmup_discards_history_when_symbol_changes_during_download(self):
        state = MonitorState(symbol="BTCUSDT")
        report = WarmupReport(
            status="READY",
            symbol="BTCUSDT",
            interval="1m",
            data_dir="/tmp/data",
            loaded_klines=1,
        )

        def switching_warmup(_config):
            state.reset_symbol("ETHUSDT")
            return [Kline(0, 100.0, 101.0, 99.0, 100.5, 10.0, 59_999)], report

        with patch("app.server.warmup_history", side_effect=switching_warmup):
            apply_warmup(
                state,
                data_dir=Path("/tmp/data"),
                months=1,
                include_current_month_daily=False,
                timeout=1.0,
            )

        snapshot = state.snapshot()
        self.assertEqual(snapshot["symbol"], "ETHUSDT")
        self.assertEqual(snapshot["kline_count"], 0)
        self.assertIsNone(snapshot["warmup"])

    def test_state_and_observation_summary_expose_daily_profile_selection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.save_observation(
                ObservationSignal(
                    observation_key="daily-profile",
                    strategy_family="short_observe",
                    strategy_tag="generic_short_observe",
                    direction="SHORT",
                    timeframe_minutes=10,
                    level="B",
                    reason="observe short",
                    entry_price=100.0,
                    opened_at=1_000,
                    expires_at=601_000,
                    threshold_segment="WD-22",
                    status="SETTLED",
                    result="WIN",
                    settled_at=601_000,
                    pnl=8.0,
                ),
                "BTCUSDT",
            )
            state = MonitorState(symbol="BTCUSDT", storage=store, enable_daily_profile_selector=True)
            selection = {
                "version": "DPS-20260730-0800",
                "status": "READY",
                "evaluated_at": 900,
                "lookback_start": 0,
                "lookback_end": 1_000,
                "effective_from": 1_000,
                "effective_until": 86_401_000,
                "reason": "启用1个画像",
                "candidates": [],
                "selected_profiles": [
                    {
                        "key": "10|short_observe|generic_short_observe|SHORT|WD-22",
                        "direction": "SHORT",
                        "strategy_family": "short_observe",
                        "strategy_tag": "generic_short_observe",
                        "threshold_segment": "WD-22",
                        "sample_size": 31,
                        "win_rate": 0.68,
                        "ev": 2.24,
                    }
                ],
            }
            state.daily_profile_selection = selection
            state.active_daily_profile_selection = selection
            server = _serve(state)
            try:
                state_payload = _get_json(f"http://127.0.0.1:{server.server_port}/api/state")
                summary_payload = _get_json(f"http://127.0.0.1:{server.server_port}/api/observation-summary")
            finally:
                server.shutdown()
                server.server_close()

        self.assertTrue(state_payload["daily_profile_selection"]["enabled"])
        self.assertEqual(state_payload["daily_profile_selection"]["selected_count"], 1)
        self.assertEqual(state_payload["stake_progression"]["max_orders"], 2)
        self.assertEqual(state_payload["stake_progression"]["max_active"], 1)
        self.assertIn("next_stake", state_payload["stake_progression"])
        self.assertIn("wave_state", state_payload)
        self.assertIn("allowed_directions", state_payload["wave_state"])
        self.assertIn("wave_batch_guard", state_payload)
        self.assertIn("mode", state_payload["wave_batch_guard"])
        self.assertEqual(summary_payload["groups"][0]["selection_state"], "ACTIVE")
        self.assertEqual(summary_payload["groups"][0]["selection_reason"], "今日主程序已启用")

    def test_orders_api_pages_and_filters_persisted_orders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            for order in [
                SimulatedOrder(
                    id=1,
                    direction="LONG",
                    timeframe_minutes=10,
                    level="S",
                    reason="long win",
                    entry_price=100.0,
                    opened_at=1_000,
                    expires_at=601_000,
                    threshold_segment="WD-08",
                    status="SETTLED",
                    result="WIN",
                    exit_price=101.0,
                    settled_at=601_000,
                    pnl=8.0,
                ),
                SimulatedOrder(
                    id=2,
                    direction="SHORT",
                    timeframe_minutes=10,
                    level="S",
                    reason="short open",
                    entry_price=100.0,
                    opened_at=2_000,
                    expires_at=602_000,
                    threshold_segment="WD-23",
                    status="OPEN",
                    wave_guard_status="WAVE_BATCH_NORMAL",
                    wave_guard_reason="当前波段批次允许开单",
                ),
            ]:
                store.save_order(order, "BTCUSDT")
            state = MonitorState(symbol="BTCUSDT", storage=store)
            server = _serve(state)
            try:
                payload = _get_json(
                    f"http://127.0.0.1:{server.server_port}/api/orders"
                    "?page=1&page_size=10&direction=SHORT&level=S&segment=WD-23&result=OPEN"
                )
            finally:
                server.shutdown()
                server.server_close()

        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["page_size"], 10)
        self.assertEqual(payload["orders"][0]["id"], 2)
        self.assertEqual(payload["orders"][0]["direction"], "SHORT")
        self.assertEqual(payload["orders"][0]["wave_guard_status"], "WAVE_BATCH_NORMAL")
        self.assertEqual(payload["orders"][0]["wave_guard_reason"], "当前波段批次允许开单")

    def test_observations_api_pages_and_filters_persisted_signals(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.save_observation(
                ObservationSignal(
                    observation_key="1|10|SHORT|normal_down_short_extension_observe",
                    strategy_family="short_extension",
                    strategy_tag="normal_down_short_extension_observe",
                    direction="SHORT",
                    timeframe_minutes=10,
                    level="A",
                    reason="observe short",
                    entry_price=100.0,
                    opened_at=1_000,
                    expires_at=601_000,
                    threshold_segment="WD-23",
                    status="OPEN",
                ),
                "BTCUSDT",
            )
            state = MonitorState(symbol="BTCUSDT", storage=store)
            server = _serve(state)
            try:
                payload = _get_json(
                    f"http://127.0.0.1:{server.server_port}/api/observations"
                    "?page=1&page_size=10&direction=SHORT&family=short_extension"
                    "&tag=normal_down_short_extension_observe&segment=WD-23&result=OPEN"
                )
            finally:
                server.shutdown()
                server.server_close()

        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["page_size"], 10)
        self.assertEqual(payload["observations"][0]["strategy_family"], "short_extension")
        self.assertEqual(payload["observations"][0]["direction"], "SHORT")

    def test_observation_summary_api_reports_profile_groups(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            for idx in range(30):
                store.save_observation(
                    ObservationSignal(
                        observation_key=f"summary-{idx}",
                        strategy_family="short_extension",
                        strategy_tag="normal_down_short_extension_observe",
                        direction="SHORT",
                        timeframe_minutes=10,
                        level="A",
                        reason="observe short",
                        entry_price=100.0,
                        opened_at=idx * 1_000,
                        expires_at=idx * 1_000 + 600_000,
                        threshold_segment="WD-23",
                        status="SETTLED",
                        result="WIN" if idx < 20 else "LOSS",
                        pnl=8.0 if idx < 20 else -10.0,
                    ),
                    "BTCUSDT",
                )
            state = MonitorState(symbol="BTCUSDT", storage=store)
            server = _serve(state)
            try:
                payload = _get_json(f"http://127.0.0.1:{server.server_port}/api/observation-summary")
            finally:
                server.shutdown()
                server.server_close()

        self.assertEqual(payload["total"]["settled"], 30)
        self.assertEqual(payload["groups"][0]["strategy_family"], "short_extension")
        self.assertEqual(payload["groups"][0]["direction"], "SHORT")
        self.assertEqual(payload["groups"][0]["action"], "PROMOTE_WATCH")

    def test_order_profile_api_reports_weak_live_order_profiles(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            for order_id in (1, 2):
                order = SimulatedOrder(
                    id=order_id,
                    direction="LONG",
                    timeframe_minutes=10,
                    level="A",
                    reason="放量急跌反抽：synthetic",
                    entry_price=100.0,
                    opened_at=order_id * 1_000,
                    expires_at=order_id * 1_000 + 600_000,
                    threshold_segment="WD-18",
                    score=85.0,
                    threshold=70.0,
                    status="SETTLED",
                    result="LOSS",
                    exit_price=99.0,
                    settled_at=order_id * 1_000 + 600_000,
                    pnl=-10.0,
                )
                store.save_order_entry_snapshot(
                    order,
                    "BTCUSDT",
                    {
                        "signal": {
                            "level": "A",
                            "reason": "放量急跌反抽：synthetic",
                            "score": 85.0,
                            "threshold": 70.0,
                            "volume_ratio": 2.1,
                            "price_change_pct": -0.0015,
                            "price_position": 0.45,
                            "rsi": 46.0,
                            "bollinger_position": 0.2,
                            "mtf_10m_bias": 0.1,
                            "mtf_30m_bias": 0.2,
                            "regime": "FEAR_FALLING",
                        },
                        "fear_greed": {"value": 12, "trend": "falling"},
                    },
                )
                store.update_order_entry_snapshot_settlement(order, "BTCUSDT")
            state = MonitorState(symbol="BTCUSDT", storage=store)
            server = _serve(state)
            try:
                payload = _get_json(f"http://127.0.0.1:{server.server_port}/api/order-profile")
            finally:
                server.shutdown()
                server.server_close()

        self.assertEqual(payload["total"]["orders"], 2)
        self.assertEqual(payload["total"]["losses"], 2)
        self.assertIn("LEVEL_A_REBOUND", {item["key"] for item in payload["risk_hints"]})


def _serve(state):
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
