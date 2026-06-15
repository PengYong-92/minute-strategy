import tempfile
import unittest
import zipfile
from pathlib import Path

from app.backtest import BacktestConfig, load_klines_from_zip, load_klines_from_zips, run_backtest
from app.models import Kline, Signal


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

        self.assertEqual(config.rolling_edge_lookback_days, 90)
        self.assertEqual(config.rolling_edge_min_samples, 20)
        self.assertEqual(config.rolling_edge_min_win_rate, 0.5556)
        self.assertEqual(config.rolling_edge_min_ev, 0.5)

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

    def test_run_backtest_compounds_stake_after_wins_up_to_three_orders(self):
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

        self.assertEqual([order["stake"] for order in result["orders"]], [10.0, 18.0, 32.4, 10.0])
        self.assertEqual([order["win_return"] for order in result["orders"]], [18.0, 32.4, 58.32, 18.0])
        self.assertEqual([order["stake_progression_step"] for order in result["orders"]], [1, 2, 3, 1])
        self.assertEqual([order["pnl"] for order in result["orders"]], [8.0, 14.4, 25.92, 8.0])
        self.assertEqual(result["stats"]["balance"], 56.32)

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

        self.assertEqual([order["stake"] for order in result["orders"]], [10.0, 18.0, 32.4, 10.0])
        self.assertEqual([order["result"] for order in result["orders"]], ["WIN", "WIN", "LOSS", "WIN"])
        self.assertEqual([order["pnl"] for order in result["orders"]], [8.0, 14.4, -32.4, 8.0])
        self.assertEqual(result["stats"]["balance"], -2.0)

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
