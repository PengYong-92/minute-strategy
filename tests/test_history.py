import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

from app.history import WarmupConfig, warmup_history


def write_binance_zip(path: Path, timestamp_ms: int, close: float = 100.0) -> None:
    row = [
        str(timestamp_ms),
        str(close - 1.0),
        str(close + 1.0),
        str(close - 2.0),
        str(close),
        "10.0",
        str(timestamp_ms + 59_999),
    ]
    csv_name = path.with_suffix(".csv").name
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(csv_name, ",".join(row) + "\n")


class HistoryWarmupTest(unittest.TestCase):
    def test_warmup_falls_back_to_cached_daily_files_when_monthly_file_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_binance_zip(
                data_dir / "BTCUSDT-1m-2026-08-01.zip",
                1_754_006_400_000,
                close=101.0,
            )
            write_binance_zip(
                data_dir / "BTCUSDT-1m-2026-08-02.zip",
                1_754_092_800_000,
                close=102.0,
            )

            def downloader(url: str, target: Path, timeout: float) -> None:
                self.assertEqual(target.name, "BTCUSDT-1m-2026-09-01.zip")
                raise FileNotFoundError("current daily archive is not published yet")

            klines, report = warmup_history(
                WarmupConfig(
                    symbol="BTCUSDT",
                    data_dir=data_dir,
                    months=1,
                    include_current_month_daily=True,
                    today=date(2026, 9, 2),
                    downloader=downloader,
                )
            )

        self.assertEqual([item.close for item in klines], [101.0, 102.0])
        self.assertEqual(report.status, "READY")
        self.assertEqual(
            report.cached_files,
            ["BTCUSDT-1m-2026-08-01.zip", "BTCUSDT-1m-2026-08-02.zip"],
        )
        self.assertEqual(report.missing_files, ["BTCUSDT-1m-2026-09-01.zip"])

    def test_missing_current_month_daily_file_does_not_downgrade_cached_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_binance_zip(data_dir / "BTCUSDT-1m-2026-04.zip", 1_775_174_400_000)

            def downloader(url: str, target: Path, timeout: float) -> None:
                raise FileNotFoundError("current daily archive is not published yet")

            _, report = warmup_history(
                WarmupConfig(
                    symbol="BTCUSDT",
                    data_dir=data_dir,
                    months=1,
                    include_current_month_daily=True,
                    today=date(2026, 5, 3),
                    downloader=downloader,
                )
            )

        self.assertEqual(report.status, "READY")
        self.assertEqual(
            report.missing_files,
            ["BTCUSDT-1m-2026-05-01.zip", "BTCUSDT-1m-2026-05-02.zip"],
        )

    def test_warmup_downloads_missing_monthly_and_current_daily_files(self):
        calls = []

        def downloader(url: str, target: Path, timeout: float) -> None:
            calls.append((url, target.name, timeout))
            if "2026-04" in target.name:
                write_binance_zip(target, 1_775_174_400_000, close=101.0)
            elif "2026-05-01" in target.name:
                write_binance_zip(target, 1_777_766_400_000, close=102.0)
            elif "2026-05-02" in target.name:
                write_binance_zip(target, 1_777_852_800_000, close=103.0)
            else:
                raise AssertionError(f"unexpected target {target}")

        with tempfile.TemporaryDirectory() as temp_dir:
            klines, report = warmup_history(
                WarmupConfig(
                    symbol="BTCUSDT",
                    data_dir=Path(temp_dir),
                    months=1,
                    include_current_month_daily=True,
                    today=date(2026, 5, 3),
                    downloader=downloader,
                )
            )

        self.assertEqual([item.close for item in klines], [101.0, 102.0, 103.0])
        self.assertEqual(report.loaded_klines, 3)
        self.assertEqual(len(report.downloaded_files), 3)
        self.assertTrue(any("monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2026-04.zip" in call[0] for call in calls))
        self.assertTrue(any("daily/klines/BTCUSDT/1m/BTCUSDT-1m-2026-05-01.zip" in call[0] for call in calls))
        self.assertTrue(any("daily/klines/BTCUSDT/1m/BTCUSDT-1m-2026-05-02.zip" in call[0] for call in calls))

    def test_warmup_uses_cached_files_without_downloading_again(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_binance_zip(data_dir / "BTCUSDT-1m-2026-04.zip", 1_775_174_400_000, close=101.0)

            def downloader(url: str, target: Path, timeout: float) -> None:
                raise AssertionError("cached monthly data should not be downloaded")

            klines, report = warmup_history(
                WarmupConfig(
                    symbol="BTCUSDT",
                    data_dir=data_dir,
                    months=1,
                    include_current_month_daily=False,
                    today=date(2026, 5, 3),
                    downloader=downloader,
                )
            )

        self.assertEqual(len(klines), 1)
        self.assertEqual(report.loaded_klines, 1)
        self.assertEqual(report.downloaded_files, [])
        self.assertEqual(report.cached_files, ["BTCUSDT-1m-2026-04.zip"])


if __name__ == "__main__":
    unittest.main()
