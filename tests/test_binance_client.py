import json
import unittest

from app.binance_client import BinanceKlineClient, BinanceSpotStreamEvent


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

    def test_parse_spot_stream_event_keeps_live_and_closed_kline_fields(self):
        payload = json.dumps(
            {
                "e": "kline",
                "E": 1710000060123,
                "s": "BTCUSDT",
                "k": {
                    "t": 1710000000000,
                    "T": 1710000059999,
                    "s": "BTCUSDT",
                    "i": "1m",
                    "o": "100.00",
                    "c": "100.80",
                    "h": "101.50",
                    "l": "99.50",
                    "v": "123.456",
                    "x": False,
                },
            }
        )

        event = BinanceSpotStreamEvent.parse(payload, received_at_ms=1710000060200)

        self.assertEqual(event.symbol, "BTCUSDT")
        self.assertEqual(event.interval, "1m")
        self.assertEqual(event.event_time_ms, 1710000060123)
        self.assertEqual(event.received_at_ms, 1710000060200)
        self.assertFalse(event.is_closed)
        self.assertEqual(event.kline.close, 100.8)
        self.assertEqual(event.kline.close_time, 1710000059999)

    def test_parse_spot_stream_event_rejects_wrong_type_or_interval(self):
        with self.assertRaises(ValueError):
            BinanceSpotStreamEvent.parse(json.dumps({"e": "aggTrade"}))
        with self.assertRaises(ValueError):
            BinanceSpotStreamEvent.parse(
                json.dumps(
                    {
                        "e": "kline",
                        "E": 1,
                        "s": "BTCUSDT",
                        "k": {
                            "t": 0,
                            "T": 59_999,
                            "i": "5m",
                            "o": "1",
                            "c": "1",
                            "h": "1",
                            "l": "1",
                            "v": "1",
                            "x": True,
                        },
                    }
                )
            )

    def test_parse_combined_mini_ticker_event_for_realtime_price(self):
        event = BinanceSpotStreamEvent.parse(
            json.dumps(
                {
                    "stream": "btcusdt@miniTicker",
                    "data": {
                        "e": "24hrMiniTicker",
                        "E": 1710000060123,
                        "s": "BTCUSDT",
                        "c": "100.81",
                    },
                }
            ),
            received_at_ms=1710000060200,
        )

        self.assertEqual(event.symbol, "BTCUSDT")
        self.assertEqual(event.event_type, "24hrMiniTicker")
        self.assertEqual(event.price, 100.81)
        self.assertIsNone(event.kline)
        self.assertFalse(event.is_closed)


if __name__ == "__main__":
    unittest.main()
