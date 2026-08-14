import unittest

from app.binance_stream import BinanceSpotWebSocketClient


class BinanceSpotWebSocketClientTest(unittest.TestCase):
    def test_combined_stream_uses_one_minute_kline_and_one_second_ticker(self):
        client = BinanceSpotWebSocketClient(base_url="wss://example.test")

        self.assertEqual(
            client.stream_url("BTCUSDT"),
            "wss://example.test/stream?streams=btcusdt@kline_1m/btcusdt@miniTicker",
        )


if __name__ == "__main__":
    unittest.main()
