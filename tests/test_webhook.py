import json
import unittest

from app.models import Signal
from app.webhook import (
    DEFAULT_IMPORT_TOKEN,
    DEFAULT_WEBHOOK_URL,
    WebhookSignalProxy,
    time_increment_for_minutes,
)


def signal(direction="LONG", timeframe_minutes=30, reason="策略开单"):
    return Signal(
        direction=direction,
        timeframe_minutes=timeframe_minutes,
        level="A",
        reason=reason,
        price=100.0,
        open_time=0,
        score=82.0,
        threshold=70.0,
        threshold_segment="WD-12",
        session_allowed=True,
    )


class WebhookSignalProxyTest(unittest.TestCase):
    def test_builds_payload_for_external_signal_ingest(self):
        proxy = WebhookSignalProxy()

        payload = proxy.build_payload("BTCUSDT", signal("LONG", 30), "外部信号", amount=18.0)

        self.assertEqual(proxy.url, DEFAULT_WEBHOOK_URL)
        self.assertEqual(payload["importToken"], DEFAULT_IMPORT_TOKEN)
        self.assertEqual(payload["direction"], "LONG")
        self.assertEqual(payload["symbol"], "BTCUSDT")
        self.assertEqual(payload["timeIncrements"], "THIRTY_MINUTE")
        self.assertEqual(payload["message"], "外部信号")
        self.assertEqual(payload["amount"], 18.0)

    def test_maps_supported_timeframes(self):
        self.assertEqual(time_increment_for_minutes(10), "TEN_MINUTE")
        self.assertEqual(time_increment_for_minutes(30), "THIRTY_MINUTE")
        self.assertIsNone(time_increment_for_minutes(15))

    def test_send_uses_json_post_transport(self):
        calls = []

        def transport(url, body, timeout):
            calls.append((url, json.loads(body.decode("utf-8")), timeout))

        proxy = WebhookSignalProxy(transport=transport, timeout_seconds=3)

        proxy.send_signal("BTCUSDT", signal("SHORT", 10), "策略触发", amount=32.4)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], DEFAULT_WEBHOOK_URL)
        self.assertEqual(calls[0][1]["direction"], "SHORT")
        self.assertEqual(calls[0][1]["timeIncrements"], "TEN_MINUTE")
        self.assertEqual(calls[0][1]["amount"], 32.4)
        self.assertEqual(calls[0][2], 3)

    def test_send_defaults_message_to_signal_reason(self):
        calls = []

        def transport(url, body, timeout):
            calls.append(json.loads(body.decode("utf-8")))

        proxy = WebhookSignalProxy(transport=transport)
        opening_reason = "低位放量上涨：增量买盘推动，动态评分偏多"

        proxy.send_signal("btcusdt", signal("LONG", 10, reason=opening_reason))

        self.assertEqual(calls[0]["symbol"], "BTCUSDT")
        self.assertEqual(calls[0]["message"], opening_reason)


if __name__ == "__main__":
    unittest.main()
