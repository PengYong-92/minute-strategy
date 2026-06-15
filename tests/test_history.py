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
