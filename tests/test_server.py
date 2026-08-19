import argparse
import json
import os
import sys
import tempfile
import threading
import time
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
from app.server import apply_warmup, make_handler
from app.state import MonitorState
from app.storage import SQLiteMonitorStore


class OrdersApiTest(unittest.TestCase):
    def test_snapshot_capacity_sampling_does_not_hold_realtime_state_lock(self):
        state = MonitorState(symbol="BTCUSDT")
        capacity_started = threading.Event()
        release_capacity = threading.Event()
        mutation_done = threading.Event()
        result = {}
        errors = []

        def blocking_capacity(_sampled_at_ms):
            capacity_started.set()
            release_capacity.wait(timeout=2)
            return {"status": "IN_MEMORY"}

        def take_snapshot():
            try:
                result["snapshot"] = state.snapshot()
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        def update_realtime_state():
            try:
                state.reset_symbol("ETHUSDT")
                state.update_realtime_price(123.0, 2_000, 2_000)
                mutation_done.set()
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        with patch.object(state, "_sample_storage_capacity", side_effect=blocking_capacity):
            snapshot_thread = threading.Thread(target=take_snapshot)
            snapshot_thread.start()
            self.assertTrue(capacity_started.wait(timeout=1))
            mutation_thread = threading.Thread(target=update_realtime_state)
            mutation_thread.start()
            try:
                self.assertTrue(
                    mutation_done.wait(timeout=0.2),
                    "容量采样阻塞时不应占用实时状态锁",
                )
            finally:
                release_capacity.set()
                snapshot_thread.join(timeout=2)
                mutation_thread.join(timeout=2)

        self.assertEqual(errors, [])
        self.assertEqual(result["snapshot"]["symbol"], "ETHUSDT")
        self.assertEqual(state.price_snapshot()["latest_price"], 123.0)

    def test_main_injects_versioned_strategy_build_id_from_cli_or_environment(self):
        cases = (
            ({}, [], server_module.DEFAULT_STRATEGY_BUILD_ID),
            ({"STRATEGY_BUILD_ID": "commit-ae7b484"}, [], "commit-ae7b484"),
            (
                {"STRATEGY_BUILD_ID": "environment-build"},
                ["--strategy-build-id", "tag-v2.1.0"],
                "tag-v2.1.0",
            ),
        )
        for environment, build_args, expected in cases:
            with self.subTest(environment=environment, build_args=build_args):
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
                            *build_args,
                        ],
                    ),
                    patch(
                        "app.server.MonitorState",
                        return_value=SimpleNamespace(symbol="BTCUSDT"),
                    ) as monitor_state,
                    patch(
                        "app.server.start_market_data",
                        return_value=SimpleNamespace(stop=lambda: None),
                    ),
                    patch("app.server.ThreadingHTTPServer", return_value=fake_server),
                ):
                    server_module.main()

                self.assertEqual(
                    monitor_state.call_args.kwargs["strategy_build_id"],
                    expected,
                )

        self.assertRegex(
            server_module.DEFAULT_STRATEGY_BUILD_ID,
            r"^minute-strategy-src-[0-9a-f]{16}$",
        )

    def test_strategy_source_build_id_is_stable_and_changes_with_source(self):
        self.assertTrue(hasattr(server_module, "strategy_source_build_id"))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "strategy.py"
            second = root / "order_policy.py"
            first.write_text("RULE = 1\n", encoding="utf-8")
            second.write_text("GATE = 2\n", encoding="utf-8")

            initial = server_module.strategy_source_build_id((first, second))
            repeated = server_module.strategy_source_build_id((second, first))
            first.write_text("RULE = 3\n", encoding="utf-8")
            changed = server_module.strategy_source_build_id((first, second))

            self.assertEqual(initial, repeated)
            self.assertNotEqual(initial, changed)

    def test_help_documents_strategy_build_id_in_chinese(self):
        stdout = StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(sys, "argv", ["app.server", "--help"]),
            redirect_stdout(stdout),
            self.assertRaises(SystemExit) as caught,
        ):
            server_module.main()

        self.assertEqual(caught.exception.code, 0)
        self.assertIn("--strategy-build-id", stdout.getvalue())
        self.assertIn("策略构建标识", stdout.getvalue())

    def test_server_help_has_no_english_argparse_labels(self):
        stdout = StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(sys, "argv", ["app.server", "--help"]),
            redirect_stdout(stdout),
            self.assertRaises(SystemExit) as caught,
        ):
            server_module.main()

        help_text = stdout.getvalue()
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("用法:", help_text)
        self.assertIn("参数:", help_text)
        for english_label in (
            "usage",
            "options",
            "show this help message",
            "missing value",
        ):
            self.assertNotIn(english_label, help_text.lower())

    def test_daily_profile_startup_defaults_and_explicit_precedence(self):
        cases = (
            ({}, [], None, 14, "default", 2, "default"),
            (
                {"DAILY_PROFILE_LOOKBACK_DAYS": "15", "DAILY_PROFILE_DEGRADED_RUNS": "5"},
                [],
                None,
                15,
                "lookback_days",
                5,
                "degraded_runs_to_exit",
            ),
            (
                {
                    "DAILY_PROFILE_DEGRADED_RUNS": "5",
                    "DAILY_PROFILE_JOINT_FAILURES_TO_EXIT": "4",
                    "DAILY_PROFILE_STABLE_LOOKBACK_DAYS": "21",
                },
                [
                    "--daily-profile-joint-failures-to-exit", "3",
                    "--daily-profile-stable-lookback-days", "18",
                ],
                18,
                18,
                "stable_lookback_days",
                3,
                "joint_failures_to_exit",
            ),
        )
        for environment, cli_args, raw_stable, effective_stable, stable_source, failures, failure_source in cases:
            with self.subTest(environment=environment, cli_args=cli_args):
                fake_server = SimpleNamespace(serve_forever=lambda: None, server_close=lambda: None)
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch.object(
                        sys,
                        "argv",
                        ["app.server", "--no-warmup", "--no-persistence", "--no-webhook", *cli_args],
                    ),
                    patch("app.server.MonitorState", return_value=SimpleNamespace(symbol="BTCUSDT")) as monitor_state,
                    patch("app.server.start_market_data", return_value=SimpleNamespace(stop=lambda: None)),
                    patch("app.server.ThreadingHTTPServer", return_value=fake_server),
                ):
                    server_module.main()

                raw = monitor_state.call_args.kwargs["daily_profile_selector_config"]
                normalized = raw.normalized()
                self.assertEqual(raw.stable_lookback_days, raw_stable)
                self.assertEqual(normalized.effective_stable_lookback_days, effective_stable)
                self.assertEqual(normalized.stable_lookback_source, stable_source)
                self.assertEqual(normalized.joint_failures_to_exit, failures)
                self.assertEqual(normalized.joint_failures_source, failure_source)

    def test_help_documents_daily_profile_compatibility_options_in_chinese(self):
        stdout = StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(sys, "argv", ["app.server", "--help"]),
            redirect_stdout(stdout),
            self.assertRaises(SystemExit) as caught,
        ):
            server_module.main()

        help_text = stdout.getvalue()
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("--daily-profile-stable-lookback-days", help_text)
        self.assertIn("--daily-profile-joint-failures-to-exit", help_text)
        self.assertIn("未指定时取 14 与快速窗口天数的较大值", help_text)
        self.assertIn("未指定时沿用连续退化次数", help_text)

    def test_main_injects_time_period_guard_config(self):
        cases = (
            ({}, [], False),
            ({"TIME_PERIOD_GUARD": "1"}, [], True),
            ({"TIME_PERIOD_GUARD": "0"}, [], False),
            ({}, ["--no-time-period-guard"], False),
        )

        for environment, guard_args, expected in cases:
            with self.subTest(environment=environment, guard_args=guard_args):
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
                            *guard_args,
                        ],
                    ),
                    patch(
                        "app.server.MonitorState",
                        return_value=SimpleNamespace(symbol="BTCUSDT"),
                    ) as monitor_state,
                    patch("app.server.start_market_data", return_value=SimpleNamespace(stop=lambda: None)),
                    patch("app.server.ThreadingHTTPServer", return_value=fake_server),
                ):
                    server_module.main()

                config = monitor_state.call_args.kwargs["time_period_guard_config"]
                self.assertEqual(config.enabled, expected)

    def test_main_injects_profile_health_guard_config(self):
        cases = (
            ({}, [], True),
            ({"PROFILE_HEALTH_GUARD": "0"}, [], False),
            ({}, ["--no-profile-health-guard"], False),
        )

        for environment, guard_args, expected in cases:
            with self.subTest(environment=environment, guard_args=guard_args):
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
                            *guard_args,
                        ],
                    ),
                    patch(
                        "app.server.MonitorState",
                        return_value=SimpleNamespace(symbol="BTCUSDT"),
                    ) as monitor_state,
                    patch("app.server.start_market_data", return_value=SimpleNamespace(stop=lambda: None)),
                    patch("app.server.ThreadingHTTPServer", return_value=fake_server),
                ):
                    server_module.main()

                config = monitor_state.call_args.kwargs["profile_health_guard_config"]
                self.assertEqual(config.enabled, expected)

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
                    patch("app.server.start_market_data", return_value=SimpleNamespace(stop=lambda: None)),
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
                    patch("app.server.start_market_data") as start_market_data_mock,
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
                start_market_data_mock.assert_not_called()
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
                    patch("app.server.start_market_data") as start_market_data_mock,
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
                start_market_data_mock.assert_not_called()
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

    def test_price_api_returns_only_lightweight_realtime_fields(self):
        payload = {
            "symbol": "BTCUSDT",
            "latest_price": 101.25,
            "event_time_ms": 100_000,
            "received_at_ms": 100_010,
            "stale": False,
            "stream_status": "CONNECTED",
        }
        state = SimpleNamespace(price_snapshot=lambda: payload)
        handler = make_handler(state)
        instance = object.__new__(handler)
        instance.path = "/api/price"
        captured = []
        instance._send_json = captured.append

        instance.do_GET()

        self.assertEqual(captured, [payload])
        self.assertNotIn("orders", captured[0])
        self.assertNotIn("stats", captured[0])

    def test_concurrent_symbol_switches_serialize_warmup_and_keep_responses_isolated(self):
        class SwitchingState:
            def __init__(self):
                self.symbol = "BTCUSDT"

            def reset_symbol(self, symbol):
                self.symbol = symbol

        class MarketData:
            def pause_updates(self):
                return None

            def request_symbol_refresh(self):
                return None

        active_warmups = 0
        max_active_warmups = 0
        active_lock = threading.Lock()

        def warmup_loader(_state):
            nonlocal active_warmups, max_active_warmups
            with active_lock:
                active_warmups += 1
                max_active_warmups = max(max_active_warmups, active_warmups)
            time.sleep(0.03)
            with active_lock:
                active_warmups -= 1

        state = SwitchingState()
        handler = make_handler(
            state,
            warmup_loader=warmup_loader,
            market_data=MarketData(),
        )
        responses = {}

        def switch(symbol):
            instance = object.__new__(handler)
            instance.path = f"/api/config?symbol={symbol}"
            instance._send_json = lambda payload: responses.__setitem__(symbol, payload)
            instance.do_GET()

        threads = [
            threading.Thread(target=switch, args=(symbol,))
            for symbol in ("ETHUSDT", "BTCUSDT")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=1)

        self.assertEqual(max_active_warmups, 1)
        self.assertEqual(responses["ETHUSDT"], {"symbol": "ETHUSDT"})
        self.assertEqual(responses["BTCUSDT"], {"symbol": "BTCUSDT"})

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
                "by_direction_slot_scope": [],
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

    def test_adaptive_structure_and_capacity_api_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            state = MonitorState(
                symbol="BTCUSDT",
                storage=store,
                enable_daily_profile_selector=True,
            )
            profile_key = "10|short_observe|generic_short_observe|SHORT|WD-22"
            candidate = {
                "key": profile_key,
                "direction": "SHORT",
                "strategy_family": "short_observe",
                "strategy_tag": "generic_short_observe",
                "threshold_segment": "WD-22",
                "qualification_state": "QUALIFIED",
                "fast_7d": {"sample_size": 24, "win_rate": 0.625, "ev": 0.8},
                "stable_14d": {"sample_size": 40, "win_rate": 0.6, "ev": 0.7},
            }
            selection = {
                "version": "DPS-20260819-0800",
                "status": "READY",
                "evaluated_at": 1_000,
                "effective_from": 2_000,
                "effective_until": 86_402_000,
                "fast_7d": {"lookback_days": 7, "lookback_start": 10, "lookback_end": 20},
                "stable_14d": {"lookback_days": 14, "lookback_start": 1, "lookback_end": 20},
                "candidates": [candidate],
                "selected_profiles": [candidate],
            }
            state.daily_profile_selection = selection
            state.active_daily_profile_selection = selection
            state.adaptive_profile_states = {
                profile_key: {
                    "profile_key": profile_key,
                    "status": "ACTIVE",
                    "reason": "N12胜7且N20 EV非负",
                    "evaluated_at": 3_000,
                    "n12": {"sample_size": 12, "wins": 7, "win_rate": 7 / 12, "ev": 0.8},
                    "n20": {"sample_size": 20, "wins": 12, "win_rate": 0.6, "ev": 0.7},
                }
            }
            state.selected_signal = Signal(
                direction="SHORT",
                timeframe_minutes=10,
                level="A",
                reason="diagnostic",
                price=100.0,
                open_time=3_000,
                entry_structure_shadow={
                    "entry_structure_version": "ENTRY_STRUCTURE_SHADOW_V1",
                    "entry_structure_mode": "SHADOW_ONLY",
                    "entry_structure_evaluated_at": 3_000,
                    "entry_structure_state": "RESISTANCE_REJECTION",
                    "entry_structure_bias": "CONFIRMED",
                    "entry_structure_reason_code": "STRUCTURE_CONFIRMED",
                    "candidate_origin": "NATIVE_ACTIONABLE",
                    "active_level_source": "SWING",
                },
            )
            server = _serve(state)
            try:
                payload = _get_json(
                    f"http://127.0.0.1:{server.server_port}/api/state"
                )
            finally:
                server.shutdown()
                server.server_close()

        selection_payload = payload["daily_profile_selection"]
        self.assertEqual(selection_payload["fast_7d"]["lookback_days"], 7)
        self.assertEqual(selection_payload["stable_14d"]["lookback_days"], 14)
        self.assertEqual(selection_payload["candidates"][0]["utc_segment_label"], "22:00-22:59 UTC")
        self.assertEqual(
            selection_payload["candidates"][0]["shanghai_segment_label"],
            "次日06:00-06:59 Asia/Shanghai",
        )
        self.assertEqual(selection_payload["evaluation_time_label"], "07:50 Asia/Shanghai")
        self.assertEqual(selection_payload["activation_time_label"], "08:00 Asia/Shanghai")
        immediate = selection_payload["immediate_state"]["profiles"][0]
        self.assertEqual(immediate["status"], "ACTIVE")
        self.assertEqual(immediate["n12"]["sample_size"], 12)
        self.assertEqual(immediate["n20"]["sample_size"], 20)
        self.assertIn(
            payload["storage_capacity"]["status"],
            {"NORMAL", "WARNING", "COMPACT_ONLY", "HARD_LIMIT"},
        )
        self.assertEqual(
            payload["entry_structure_shadow"]["entry_structure_bias"],
            "CONFIRMED",
        )

    def test_observation_windows_filters_and_legacy_normalization(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.save_observation(
                ObservationSignal(
                    observation_key="legacy",
                    strategy_family="short_observe",
                    strategy_tag="legacy_short_observe",
                    direction="SHORT",
                    timeframe_minutes=10,
                    level="B",
                    reason="legacy",
                    entry_price=100.0,
                    opened_at=1_000,
                    expires_at=601_000,
                    threshold_segment="WD-22",
                ),
                "BTCUSDT",
            )
            store.save_observation(
                ObservationSignal(
                    observation_key="adaptive",
                    strategy_family="short_observe",
                    strategy_tag="adaptive_short_observe",
                    direction="SHORT",
                    timeframe_minutes=10,
                    level="A",
                    reason="adaptive",
                    entry_price=100.0,
                    opened_at=2_000,
                    expires_at=602_000,
                    threshold_segment="WD-23",
                    context_version="DECISION_CONTEXT_V2",
                    candidate_origin="PROFILE_PROMOTED_WAIT",
                    adaptive_profile_state={
                        "qualification_state": "QUALIFIED",
                        "status": "ACTIVE",
                    },
                    entry_structure_shadow={
                        "entry_structure_state": "RESISTANCE_REJECTION",
                        "entry_structure_bias": "CONFLICT",
                        "active_level_source": "SWING",
                    },
                ),
                "BTCUSDT",
            )
            state = MonitorState(symbol="BTCUSDT", storage=store)
            server = _serve(state)
            try:
                summaries = {
                    window: _get_json(
                        f"http://127.0.0.1:{server.server_port}/api/observation-summary"
                        f"?window={window}"
                    )
                    for window in ("7d", "14d", "30d", "all")
                }
                default_summary = _get_json(
                    f"http://127.0.0.1:{server.server_port}/api/observation-summary"
                )
                filtered = _get_json(
                    f"http://127.0.0.1:{server.server_port}/api/observations"
                    "?candidate_origin=PROFILE_PROMOTED_WAIT"
                    "&qualification_state=QUALIFIED&adaptive_state=ACTIVE"
                    "&entry_structure_state=RESISTANCE_REJECTION"
                    "&entry_structure_bias=CONFLICT&active_level_source=SWING"
                )
                unfiltered = _get_json(
                    f"http://127.0.0.1:{server.server_port}/api/observations?page_size=10"
                )
            finally:
                server.shutdown()
                server.server_close()

        self.assertEqual([summaries[item]["window"] for item in summaries], ["7d", "14d", "30d", "all"])
        self.assertEqual(default_summary["window"], "14d")
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["filters"]["candidate_origin"], "PROFILE_PROMOTED_WAIT")
        self.assertIn("PROFILE_PROMOTED_WAIT", filtered["filter_options"]["candidate_origin"])
        current = filtered["observations"][0]
        self.assertEqual(current["candidate_origin"], "PROFILE_PROMOTED_WAIT")
        self.assertEqual(current["qualification_state"], "QUALIFIED")
        self.assertEqual(current["adaptive_state"], "ACTIVE")
        self.assertEqual(current["entry_structure_state"], "RESISTANCE_REJECTION")
        self.assertEqual(current["entry_structure_bias"], "CONFLICT")
        self.assertEqual(current["active_level_source"], "SWING")
        legacy = next(item for item in unfiltered["observations"] if item["observation_key"] == "legacy")
        self.assertEqual(legacy["context_version"], "LEGACY")
        self.assertEqual(legacy["candidate_origin"], "UNKNOWN")
        self.assertEqual(legacy["qualification_state"], "UNKNOWN")
        self.assertEqual(legacy["adaptive_state"], "UNKNOWN")
        self.assertEqual(legacy["entry_structure_state"], "UNKNOWN")
        self.assertEqual(legacy["entry_structure_bias"], "UNKNOWN")
        self.assertEqual(legacy["active_level_source"], "UNKNOWN")

    def test_in_memory_observation_diagnostic_filter_options_include_unknown(self):
        state = MonitorState(symbol="BTCUSDT")
        state.observations = [
            ObservationSignal(
                observation_key="legacy-memory",
                strategy_family="short_observe",
                strategy_tag="legacy",
                direction="SHORT",
                timeframe_minutes=10,
                level="B",
                reason="legacy",
                entry_price=100.0,
                opened_at=1_000,
                expires_at=601_000,
            ),
            ObservationSignal(
                observation_key="current-memory",
                strategy_family="short_observe",
                strategy_tag="current",
                direction="SHORT",
                timeframe_minutes=10,
                level="A",
                reason="current",
                entry_price=100.0,
                opened_at=2_000,
                expires_at=602_000,
                candidate_origin="NATIVE_ACTIONABLE",
                adaptive_profile_state={
                    "qualification_state": "QUALIFIED",
                    "status": "ACTIVE",
                },
                entry_structure_shadow={
                    "entry_structure_state": "BREAKOUT_CONFIRMED",
                    "entry_structure_bias": "CONFIRMED",
                    "active_level_source": "SWING",
                },
            ),
        ]

        payload = state.page_observations(
            candidate_origin="UNKNOWN",
            qualification_state="UNKNOWN",
            adaptive_state="UNKNOWN",
            entry_structure_state="UNKNOWN",
            entry_structure_bias="UNKNOWN",
            active_level_source="UNKNOWN",
        )

        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["observations"][0]["observation_key"], "legacy-memory")
        expected = {
            "candidate_origin": {"NATIVE_ACTIONABLE", "UNKNOWN"},
            "qualification_state": {"QUALIFIED", "UNKNOWN"},
            "adaptive_state": {"ACTIVE", "UNKNOWN"},
            "entry_structure_state": {"BREAKOUT_CONFIRMED", "UNKNOWN"},
            "entry_structure_bias": {"CONFIRMED", "UNKNOWN"},
            "active_level_source": {"SWING", "UNKNOWN"},
        }
        for name, values in expected.items():
            self.assertEqual(set(payload["filter_options"][name]), values)
            self.assertEqual(payload["filters"][name], "UNKNOWN")

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
            store.wait_for_profile_summary_rebuilds(timeout=10)
            server = _serve(state)
            try:
                payload = _get_json(f"http://127.0.0.1:{server.server_port}/api/order-profile")
            finally:
                server.shutdown()
                server.server_close()
                state.close()

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
