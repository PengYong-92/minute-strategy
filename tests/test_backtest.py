import tempfile
import unittest
import zipfile
from pathlib import Path

from app.backtest import BacktestConfig, _risk_stats, load_klines_from_zip, load_klines_from_zips, main, run_backtest
from app.models import Kline, Signal
from app.stake_progression import TWO_STAGE_VERSION


def kline(idx, close):
    return Kline(
        open_time=idx * 60_000,
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
        volume=100,
        close_time=idx * 60_000 + 59_999,
    )


class BacktestTest(unittest.TestCase):
    def test_default_backtest_guard_config_uses_backtested_parameters(self):
        config = BacktestConfig()

        self.assertEqual(config.rolling_edge_lookback_days, 60)
        self.assertEqual(config.rolling_edge_min_samples, 5)
        self.assertEqual(config.rolling_edge_min_win_rate, 0.62)
        self.assertEqual(config.rolling_edge_min_ev, 0.5)
        self.assertEqual(config.stake_progression_max_orders, 2)
        self.assertEqual(config.stake_progression_max_active, 1)
        self.assertTrue(config.short_observe_only)

    def test_run_backtest_observes_short_without_opening_order_by_default(self):
        klines = [kline(i, 100 - i) for i in range(80)]

        def signal_provider(history):
            if len(history) == 40:
                return Signal(
                    direction="SHORT",
                    timeframe_minutes=10,
                    level="S",
                    reason="synthetic short",
                    price=history[-1].close,
                    open_time=history[-1].open_time,
                    score=-82,
                    threshold=70,
                    threshold_segment="WD-02",
                    session_allowed=True,
                    session_sample_size=20,
                    session_win_rate=0.7,
                    session_ev=2.0,
                )
            return Signal("WAIT", 10, "B", "wait", history[-1].close, history[-1].open_time)

        result = run_backtest(klines, BacktestConfig(warmup_minutes=40), signal_provider=signal_provider)

        self.assertEqual(result["stats"]["total_orders"], 0)
        self.assertEqual(result["rejected_signals"]["short_observe_only"], 1)
        self.assertEqual(result["by_direction"], {})

    def test_load_klines_from_zip_parses_binance_csv_rows(self):
        row = "1710000000000,100.0,101.0,99.0,100.5,12.3,1710000059999,0,0,0,0,0\n"
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = Path(temp_dir) / "BTCUSDT-1m-test.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("BTCUSDT-1m-test.csv", row)

            klines = load_klines_from_zip(zip_path)

        self.assertEqual(len(klines), 1)
        self.assertEqual(klines[0].open_time, 1710000000000)
        self.assertEqual(klines[0].close, 100.5)

    def test_load_klines_from_zip_normalizes_microsecond_timestamps(self):
        row = "1775001600000000,100.0,101.0,99.0,100.5,12.3,1775001659999999,0,0,0,0,0\n"
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = Path(temp_dir) / "BTCUSDT-1m-test.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("BTCUSDT-1m-test.csv", row)

            klines = load_klines_from_zip(zip_path)

        self.assertEqual(klines[0].open_time, 1775001600000)
        self.assertEqual(klines[0].close_time, 1775001659999)

    def test_run_backtest_settles_signal_at_matching_future_horizon(self):
        klines = [kline(i, 100 + i) for i in range(80)]

        def signal_provider(history):
            if len(history) == 40:
                return Signal(
                    direction="LONG",
                    timeframe_minutes=10,
                    level="A",
                    reason="synthetic",
                    price=history[-1].close,
                    open_time=history[-1].open_time,
                    score=80,
                    threshold=70,
                    threshold_segment="WD-12",
                    session_allowed=True,
                    session_sample_size=37,
                    session_win_rate=0.6757,
                    session_ev=2.1622,
                    session_edge_min=10.0,
                )
            return Signal("WAIT", 10, "B", "wait", history[-1].close, history[-1].open_time)

        result = run_backtest(klines, BacktestConfig(warmup_minutes=40), signal_provider=signal_provider)

        self.assertEqual(result["stats"]["total_orders"], 1)
        self.assertEqual(result["stats"]["wins"], 1)
        self.assertEqual(result["orders"][0]["entry_time"], klines[39].close_time)
        self.assertEqual(result["orders"][0]["exit_time"], klines[49].close_time)
        self.assertEqual(result["orders"][0]["pnl"], 8.0)
        self.assertEqual(result["orders"][0]["threshold_segment"], "WD-12")
        self.assertEqual(result["orders"][0]["session_win_rate"], 0.6757)
        self.assertIn("10|WD-12", result["by_timeframe_session"])
        self.assertEqual(result["by_timeframe_session"]["10|WD-12"]["wins"], 1)
        self.assertEqual(result["risk"]["max_drawdown"], 0.0)
        self.assertIn("1970-01", result["by_month"])

    def test_run_backtest_uses_strict_two_stage_progression(self):
        klines = [kline(i, 100 + i) for i in range(12)]
        signal_points = {2, 4, 6, 8}

        def signal_provider(history):
            if len(history) in signal_points:
                return Signal(
                    direction="LONG",
                    timeframe_minutes=1,
                    level="A",
                    reason="synthetic",
                    price=history[-1].close,
                    open_time=history[-1].open_time,
                    score=80,
                    threshold=70,
                    threshold_segment="WD-12",
                    session_allowed=True,
                )
            return Signal("WAIT", 1, "B", "wait", history[-1].close, history[-1].open_time)

        result = run_backtest(
            klines,
            BacktestConfig(
                warmup_minutes=2,
                min_order_gap_minutes=0,
                enable_stake_progression=True,
                stake_progression_max_orders=3,
            ),
            signal_provider=signal_provider,
        )

        self.assertEqual([order["stake"] for order in result["orders"]], [10.0, 18.0, 10.0, 18.0])
        self.assertEqual([order["win_return"] for order in result["orders"]], [18.0, 32.4, 18.0, 32.4])
        self.assertEqual([order["stake_progression_step"] for order in result["orders"]], [1, 2, 1, 2])
        self.assertEqual([order["pnl"] for order in result["orders"]], [8.0, 14.4, 8.0, 14.4])
        self.assertEqual(result["stats"]["balance"], 44.8)
        self.assertTrue(all(order["stake_progression_version"] == TWO_STAGE_VERSION for order in result["orders"]))

    def test_run_backtest_compounded_stake_resets_after_loss(self):
        klines = [
            kline(0, 100),
            kline(1, 101),
            kline(2, 102),
            kline(3, 103),
            kline(4, 104),
            kline(5, 105),
            kline(6, 104),
            kline(7, 103),
            kline(8, 104),
            kline(9, 105),
        ]
        signal_points = {2, 4, 6, 8}

        def signal_provider(history):
            if len(history) in signal_points:
                return Signal(
                    direction="LONG",
                    timeframe_minutes=1,
                    level="A",
                    reason="synthetic",
                    price=history[-1].close,
                    open_time=history[-1].open_time,
                    score=80,
                    threshold=70,
                    threshold_segment="WD-12",
                    session_allowed=True,
                )
            return Signal("WAIT", 1, "B", "wait", history[-1].close, history[-1].open_time)

        result = run_backtest(
            klines,
            BacktestConfig(warmup_minutes=2, min_order_gap_minutes=0, enable_stake_progression=True),
            signal_provider=signal_provider,
        )

        self.assertEqual([order["stake"] for order in result["orders"]], [10.0, 18.0, 10.0, 10.0])
        self.assertEqual([order["result"] for order in result["orders"]], ["WIN", "WIN", "LOSS", "WIN"])
        self.assertEqual([order["pnl"] for order in result["orders"]], [8.0, 14.4, -10.0, 8.0])
        self.assertEqual(result["stats"]["balance"], 20.4)

    def test_run_backtest_settles_all_due_orders_before_same_millisecond_entry(self):
        klines = [kline(i, 100 + i) for i in range(12)]
        signal_points = {2, 3, 5, 7}

        def signal_provider(history):
            if len(history) in signal_points:
                return Signal(
                    direction="LONG",
                    timeframe_minutes=3,
                    level="A",
                    reason="synthetic",
                    price=history[-1].close,
                    open_time=history[-1].open_time,
                    score=80,
                    threshold=70,
                    threshold_segment="WD-12",
                    session_allowed=True,
                )
            return Signal("WAIT", 3, "B", "wait", history[-1].close, history[-1].open_time)

        result = run_backtest(
            klines,
            BacktestConfig(
                warmup_minutes=2,
                max_open_orders=5,
                min_order_gap_minutes=0,
                enable_stake_progression=True,
                stake_progression_max_active=1,
            ),
            signal_provider=signal_provider,
        )

        orders = result["orders"]
        self.assertEqual([order["stake"] for order in orders], [10.0, 10.0, 18.0, 10.0])
        self.assertEqual([order["stake_progression_step"] for order in orders], [1, 1, 2, 1])
        self.assertEqual(orders[2]["entry_time"], orders[0]["exit_time"])
        self.assertEqual(orders[2]["stake_progression_source_order_id"], orders[0]["id"])
        self.assertTrue(all(order["stake_progression_version"] == TWO_STAGE_VERSION for order in orders))

    def test_run_backtest_keeps_fixed_stake_when_progression_is_disabled(self):
        klines = [kline(i, 100 + i) for i in range(10)]

        def signal_provider(history):
            if len(history) in {2, 4, 6}:
                return Signal(
                    direction="LONG",
                    timeframe_minutes=1,
                    level="A",
                    reason="synthetic",
                    price=history[-1].close,
                    open_time=history[-1].open_time,
                    score=80,
                    threshold=70,
                    session_allowed=True,
                )
            return Signal("WAIT", 1, "B", "wait", history[-1].close, history[-1].open_time)

        result = run_backtest(
            klines,
            BacktestConfig(
                warmup_minutes=2,
                min_order_gap_minutes=0,
                enable_stake_progression=False,
                stake_progression_max_orders=99,
            ),
            signal_provider=signal_provider,
        )

        self.assertEqual([order["stake"] for order in result["orders"]], [10.0, 10.0, 10.0])
        self.assertEqual([order["stake_progression_step"] for order in result["orders"]], [1, 1, 1])
        self.assertEqual([order["stake_progression_version"] for order in result["orders"]], ["", "", ""])

    def test_run_backtest_rejects_invalid_stake_terms_even_when_progression_is_disabled(self):
        klines = [kline(i, 100 + i) for i in range(4)]

        for config in (
            BacktestConfig(stake=-10.0),
            BacktestConfig(stake=float("inf")),
            BacktestConfig(stake=10.0, win_return=10.0),
            BacktestConfig(stake=10.0, win_return=float("nan")),
        ):
            with self.subTest(config=config), self.assertRaises(ValueError):
                run_backtest(klines, config)

    def test_custom_payout_reports_matching_break_even_rate(self):
        klines = [kline(i, 100 + i) for i in range(5)]

        def signal_provider(history):
            if len(history) == 2:
                return Signal(
                    direction="LONG", timeframe_minutes=1, level="A", reason="synthetic",
                    price=history[-1].close, open_time=history[-1].open_time,
                    score=80, threshold=70, session_allowed=True,
                )
            return Signal("WAIT", 1, "B", "wait", history[-1].close, history[-1].open_time)

        result = run_backtest(
            klines,
            BacktestConfig(warmup_minutes=2, stake=20.0, win_return=30.0),
            signal_provider=signal_provider,
        )

        self.assertEqual(result["stats"]["break_even_win_rate"], 0.6667)

    def test_risk_stats_use_settlement_order_for_overlapping_orders(self):
        orders = [
            {"id": 1, "result": "LOSS", "pnl": -10.0, "exit_time": 300},
            {"id": 2, "result": "LOSS", "pnl": -10.0, "exit_time": 100},
            {"id": 3, "result": "WIN", "pnl": 8.0, "exit_time": 200},
        ]

        risk = _risk_stats(orders)

        self.assertEqual(risk["max_drawdown"], -12.0)
        self.assertEqual(risk["max_loss_streak"], 1)

    def test_backtest_cli_rejects_extra_arguments(self):
        self.assertEqual(main(["input.zip", "report.json", "unexpected"]), 2)

    def test_stake_progression_does_not_change_rolling_edge_order_selection(self):
        klines = [kline(i, 100 + i) for i in range(10)]

        def signal_provider(history):
            if len(history) in {2, 4, 6}:
                return Signal(
                    direction="LONG",
                    timeframe_minutes=1,
                    level="A",
                    reason="same setup",
                    price=history[-1].close,
                    open_time=history[-1].open_time,
                    score=80,
                    threshold=70,
                    threshold_segment="WD-12",
                    session_allowed=True,
                )
            return Signal("WAIT", 1, "B", "wait", history[-1].close, history[-1].open_time)

        common = {
            "warmup_minutes": 2,
            "min_order_gap_minutes": 0,
            "enable_rolling_edge_guard": True,
            "rolling_edge_min_samples": 2,
            "rolling_edge_min_win_rate": 0.0,
            "rolling_edge_min_ev": 9.0,
        }
        fixed = run_backtest(
            klines,
            BacktestConfig(**common, enable_stake_progression=False),
            signal_provider=signal_provider,
        )
        progressed = run_backtest(
            klines,
            BacktestConfig(**common, enable_stake_progression=True),
            signal_provider=signal_provider,
        )

        fields = ("id", "direction", "entry_time", "expires_at", "result")
        self.assertEqual(
            [tuple(order[field] for field in fields) for order in progressed["orders"]],
            [tuple(order[field] for field in fields) for order in fixed["orders"]],
        )
        self.assertEqual(fixed["stats"]["total_orders"], 2)
        self.assertEqual(progressed["stats"]["total_orders"], 2)

    def test_load_klines_from_zips_deduplicates_and_sorts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first = Path(temp_dir) / "first.zip"
            second = Path(temp_dir) / "second.zip"
            with zipfile.ZipFile(first, "w") as archive:
                archive.writestr(
                    "first.csv",
                    "120000,100,101,99,100,1,179999\n60000,100,101,99,99,1,119999\n",
                )
            with zipfile.ZipFile(second, "w") as archive:
                archive.writestr(
                    "second.csv",
                    "120000,100,101,99,101,1,179999\n180000,101,102,100,102,1,239999\n",
                )

            klines = load_klines_from_zips([first, second])

        self.assertEqual([item.open_time for item in klines], [60_000, 120_000, 180_000])
        self.assertEqual(klines[1].close, 101.0)

    def test_rolling_edge_guard_blocks_degraded_setup(self):
        klines = [kline(i, 200 - i) for i in range(120)]
        signal_points = {20, 31, 42, 53}

        def signal_provider(history):
            if len(history) in signal_points:
                return Signal(
                    direction="LONG",
                    timeframe_minutes=10,
                    level="A",
                    reason="放量急跌反抽：synthetic",
                    price=history[-1].close,
                    open_time=history[-1].open_time,
                    score=86,
                    threshold=70,
                    threshold_segment="WD-12",
                    session_allowed=True,
                    session_sample_size=37,
                    session_win_rate=0.6757,
                    session_ev=2.1622,
                    session_edge_min=10.0,
                )
            return Signal("WAIT", 10, "B", "wait", history[-1].close, history[-1].open_time)

        result = run_backtest(
            klines,
            BacktestConfig(
                warmup_minutes=20,
                min_order_gap_minutes=0,
                enable_rolling_edge_guard=True,
                rolling_edge_lookback_days=1,
                rolling_edge_min_samples=3,
            ),
            signal_provider=signal_provider,
        )

        self.assertEqual(result["stats"]["total_orders"], 3)
        self.assertEqual(result["stats"]["losses"], 3)
        self.assertEqual(result["rejected_signals"]["rolling_edge_degraded"], 1)


if __name__ == "__main__":
    unittest.main()
