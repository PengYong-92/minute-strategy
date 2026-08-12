import argparse
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app.server as server_module
from app.history import WarmupReport
from app.models import Kline, ObservationSignal, Signal, SimulatedOrder
from app.server import apply_warmup, make_handler, start_polling
from app.state import MonitorState
from app.storage import SQLiteMonitorStore


class OrdersApiTest(unittest.TestCase):
    def test_main_injects_profile_degradation_cooldown_config(self):
        cases = (
            ({}, [], 60, 60),
            ({"PROFILE_DEGRADATION_COOLDOWN_MINUTES": "75"}, [], 75, 75),
            (
                {"PROFILE_DEGRADATION_COOLDOWN_MINUTES": "75"},
                ["--profile-degradation-cooldown-minutes", "30"],
                30,
                30,
            ),
            (
                {"PROFILE_DEGRADATION_COOLDOWN_MINUTES": "75"},
                ["--profile-degradation-cooldown-minutes", "-5"],
                -5,
                0,
            ),
        )

        for environment, profile_args, raw_expected, normalized_expected in cases:
            with self.subTest(environment=environment, profile_args=profile_args):
                fake_server = SimpleNamespace(
                    serve_forever=lambda: None,
                    server_close=lambda: None,
                )
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch.object(
                        sys,
                        "argv",
                        [
                            "app.server",
                            "--no-warmup",
                            "--no-persistence",
                            "--no-webhook",
                            *profile_args,
                        ],
                    ),
                    patch(
                        "app.server.MonitorState",
                        return_value=SimpleNamespace(symbol="BTCUSDT"),
                    ) as monitor_state,
                    patch("app.server.start_polling"),
                    patch("app.server.ThreadingHTTPServer", return_value=fake_server),
                ):
                    server_module.main()

                config = monitor_state.call_args.kwargs[
                    "profile_degradation_guard_config"
                ]
                self.assertIsInstance(config.cooldown_minutes, int)
                self.assertEqual(config.cooldown_minutes, raw_expected)
                self.assertEqual(
                    config.normalized().cooldown_minutes,
                    normalized_expected,
                )

    def test_main_reports_invalid_profile_degradation_env_through_argparse(self):
        for value in ("bad", ""):
            with self.subTest(value=value):
                stderr = StringIO()
                with (
                    patch.dict(
                        os.environ,
                        {"PROFILE_DEGRADATION_COOLDOWN_MINUTES": value},
                        clear=True,
                    ),
                    patch.object(
                        sys,
                        "argv",
                        [
                            "app.server",
                            "--no-warmup",
                            "--no-persistence",
                            "--no-webhook",
                        ],
                    ),
                    patch("app.server.MonitorState") as monitor_state,
                    patch("app.server.start_polling") as start_polling_mock,
                    patch("app.server.ThreadingHTTPServer") as server_mock,
                    redirect_stderr(stderr),
                ):
                    caught = None
                    try:
                        server_module.main()
                    except BaseException as exc:  # argparse exits through SystemExit.
                        caught = exc

                self.assertIsInstance(caught, SystemExit)
                self.assertEqual(caught.code, 2)
                self.assertIn(
                    "argument --profile-degradation-cooldown-minutes: invalid int value",
                    stderr.getvalue(),
                )
                self.assertNotIn("Traceback", stderr.getvalue())
                monitor_state.assert_not_called()
                start_polling_mock.assert_not_called()
                server_mock.assert_not_called()

    def test_help_works_with_invalid_profile_degradation_env(self):
        for value in ("bad", ""):
            with self.subTest(value=value):
                stdout = StringIO()
                stderr = StringIO()
                with (
                    patch.dict(
                        os.environ,
                        {"PROFILE_DEGRADATION_COOLDOWN_MINUTES": value},
                        clear=True,
                    ),
                    patch.object(sys, "argv", ["app.server", "--help"]),
                    patch("app.server.MonitorState") as monitor_state,
                    patch("app.server.start_polling") as start_polling_mock,
                    patch("app.server.ThreadingHTTPServer") as server_mock,
                    redirect_stdout(stdout),
                    redirect_stderr(stderr),
                ):
                    caught = None
                    try:
                        server_module.main()
                    except BaseException as exc:  # argparse exits through SystemExit.
                        caught = exc

                self.assertIsInstance(caught, SystemExit)
                self.assertEqual(caught.code, 0)
                self.assertIn(
                    "--profile-degradation-cooldown-minutes",
                    stdout.getvalue(),
                )
                self.assertIn("完整画像连续亏损3单后的冷却分钟数", stdout.getvalue())
                self.assertEqual(stderr.getvalue(), "")
                monitor_state.assert_not_called()
                start_polling_mock.assert_not_called()
                server_mock.assert_not_called()

    def test_trade_score_threshold_accepts_auto_and_range(self):
        self.assertIsNone(server_module._trade_score_threshold("auto"))
        self.assertIsNone(server_module._trade_score_threshold(""))
        self.assertEqual(server_module._trade_score_threshold("0"), 0.0)
        self.assertEqual(server_module._trade_score_threshold("35.5"), 35.5)
        self.assertEqual(server_module._trade_score_threshold("95"), 95.0)
        with self.assertRaises(argparse.ArgumentTypeError):
            server_module._trade_score_threshold("-1")
        with self.assertRaises(argparse.ArgumentTypeError):
            server_module._trade_score_threshold("96")
        with self.assertRaises(argparse.ArgumentTypeError):
            server_module._trade_score_threshold("invalid")

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
        self.assertEqual(
            set(state_payload["stats"]["today"]),
            {"date", "pnl", "settled_orders", "wins", "losses", "win_rate"},
        )
        self.assertEqual(
            state_payload["stats"]["profile_period"],
            {
                "active": True,
                "version": "DPS-20260730-0800",
                "effective_from": 1_000,
                "effective_until": 86_401_000,
                "pnl": 0.0,
                "settled_orders": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "by_direction_slot": [],
            },
        )
        self.assertIn("wave_state", state_payload)
        self.assertIn("allowed_directions", state_payload["wave_state"])
        self.assertIn("wave_batch_guard", state_payload)
        self.assertIn("mode", state_payload["wave_batch_guard"])
        self.assertIn("profile_degradation_guard", state_payload)
        self.assertTrue(
            {
                "enabled",
                "status",
                "cooldown_minutes",
                "profile_key",
                "pause_until",
                "probe_order_id",
            }.issubset(state_payload["profile_degradation_guard"])
        )
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

    def test_signal_audit_summary_api_reports_guard_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            state = MonitorState(symbol="BTCUSDT", storage=store)
            audited = state._attach_quality_score(
                Signal(
                    direction="LONG",
                    timeframe_minutes=10,
                    level="A",
                    reason="audit",
                    price=100.0,
                    open_time=1_000,
                    profile_key="10|drop_reclaim|long_observe|LONG|WD-08",
                    daily_profile_version="DPS-1",
                )
            )
            store.save_signal(
                "BTCUSDT",
                audited,
                "RESULT_SEQUENCE_GUARD_BLOCKED",
                1_000,
                audit_context={
                    "result_sequence_guard": {"status": "COOLDOWN"},
                    "profile_degradation_guard": {"status": "NORMAL"},
                },
            )
            server = _serve(state)
            try:
                payload = _get_json(
                    f"http://127.0.0.1:{server.server_port}/api/signal-audit-summary"
                )
            finally:
                server.shutdown()
                server.server_close()

        self.assertEqual(payload["sample_count"], 1)
        self.assertEqual(payload["by_decision"][0]["key"], "RESULT_SEQUENCE_GUARD_BLOCKED")
        self.assertEqual(payload["by_result_sequence_status"][0]["key"], "COOLDOWN")


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
