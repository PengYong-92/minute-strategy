import unittest

from app.binance_client import BinanceKlineClient


class BinanceClientTest(unittest.TestCase):
    def test_parse_rest_kline_row(self):
        row = [
            1710000000000,
            "100.00",
            "101.50",
            "99.50",
            "100.80",
            "123.456",
            1710000059999,
            "12345.6",
            100,
            "60.0",
            "6000.0",
            "0",
        ]

        kline = BinanceKlineClient.parse_kline(row)

        self.assertEqual(kline.open_time, 1710000000000)
        self.assertEqual(kline.open, 100.0)
        self.assertEqual(kline.high, 101.5)
        self.assertEqual(kline.low, 99.5)
        self.assertEqual(kline.close, 100.8)
        self.assertEqual(kline.volume, 123.456)
        self.assertEqual(kline.close_time, 1710000059999)

    def test_filter_closed_klines_removes_unclosed_current_kline(self):
        closed = BinanceKlineClient.parse_kline(
            [1000, "100", "101", "99", "100.5", "10", 1999, "0", 0, "0", "0", "0"]
        )
        unclosed = BinanceKlineClient.parse_kline(
            [2000, "101", "102", "100", "101.5", "11", 2999, "0", 0, "0", "0", "0"]
        )

        result = BinanceKlineClient.filter_closed_klines([closed, unclosed], now_ms=2500)

        self.assertEqual(result, [closed])


if __name__ == "__main__":
    unittest.main()
