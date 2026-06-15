import unittest

from app.models import Kline
from app.timeframes import aggregate_klines


def kline(idx, open_price, close, volume=1.0):
    return Kline(
        open_time=idx * 60_000,
        open=open_price,
        high=max(open_price, close) + 0.5,
        low=min(open_price, close) - 0.5,
        close=close,
        volume=volume,
        close_time=idx * 60_000 + 59_999,
    )


class TimeframesTest(unittest.TestCase):
    def test_aggregate_10m_kline_from_1m_rows(self):
        klines = [kline(i, 100 + i, 100.5 + i, volume=2) for i in range(10)]

        result = aggregate_klines(klines, timeframe_minutes=10)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].open_time, klines[0].open_time)
        self.assertEqual(result[0].close_time, klines[-1].close_time)
        self.assertEqual(result[0].open, klines[0].open)
        self.assertEqual(result[0].close, klines[-1].close)
        self.assertEqual(result[0].high, max(item.high for item in klines))
        self.assertEqual(result[0].low, min(item.low for item in klines))
        self.assertEqual(result[0].volume, 20)

    def test_aggregate_skips_incomplete_final_window(self):
        klines = [kline(i, 100, 101) for i in range(35)]

        result = aggregate_klines(klines, timeframe_minutes=30)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].close_time, klines[29].close_time)


if __name__ == "__main__":
    unittest.main()
