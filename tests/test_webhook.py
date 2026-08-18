import json
import threading
import unittest
from dataclasses import replace
from unittest.mock import patch

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
    def test_entry_structure_shadow_never_changes_webhook_payload(self):
        proxy = WebhookSignalProxy()
        baseline = signal("LONG", 10)
        structured = replace(
            baseline,
            entry_structure_shadow={
                "entry_structure_state": "RESISTANCE_REJECTED",
                "entry_structure_bias": "CONFLICT",
                "detail": {"levels": [100.0]},
            },
        )

        self.assertEqual(
            proxy.build_payload("BTCUSDT", structured, amount=10.0),
            proxy.build_payload("BTCUSDT", baseline, amount=10.0),
        )

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
        sent = threading.Event()

        def transport(url, body, timeout):
            calls.append((url, json.loads(body.decode("utf-8")), timeout))
            sent.set()

        proxy = WebhookSignalProxy(transport=transport, timeout_seconds=3)

        proxy.send_signal("BTCUSDT", signal("SHORT", 10), "策略触发", amount=32.4)

        self.assertTrue(sent.wait(timeout=1))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], DEFAULT_WEBHOOK_URL)
        self.assertEqual(calls[0][1]["direction"], "SHORT")
        self.assertEqual(calls[0][1]["timeIncrements"], "TEN_MINUTE")
        self.assertEqual(calls[0][1]["amount"], 32.4)
        self.assertEqual(calls[0][2], 3)

    def test_send_defaults_message_to_signal_reason(self):
        calls = []
        sent = threading.Event()

        def transport(url, body, timeout):
            calls.append(json.loads(body.decode("utf-8")))
            sent.set()

        proxy = WebhookSignalProxy(transport=transport)
        opening_reason = "低位放量上涨：增量买盘推动，动态评分偏多"

        proxy.send_signal("btcusdt", signal("LONG", 10, reason=opening_reason))

        self.assertTrue(sent.wait(timeout=1))
        self.assertEqual(calls[0]["symbol"], "BTCUSDT")
        self.assertEqual(calls[0]["message"], opening_reason)

    def test_status_does_not_retain_payload_or_delivery_result(self):
        proxy = WebhookSignalProxy(import_token="secret-token")

        self.assertEqual(
            proxy.status(),
            {
                "enabled": True,
                "url": DEFAULT_WEBHOOK_URL,
                "last_error": None,
                "last_payload": None,
                "last_sent_at_ms": None,
            },
        )

    def test_send_returns_without_waiting_for_transport(self):
        started = threading.Event()
        release = threading.Event()
        returned = threading.Event()

        def transport(url, body, timeout):
            started.set()
            release.wait()

        proxy = WebhookSignalProxy(transport=transport)
        caller = threading.Thread(
            target=lambda: (
                proxy.send_signal("BTCUSDT", signal("LONG", 10), amount=10.0),
                returned.set(),
            )
        )

        caller.start()
        self.assertTrue(started.wait(timeout=1))
        returned_before_release = returned.wait(timeout=0.5)
        release.set()
        caller.join(timeout=1)

        self.assertTrue(returned_before_release)

    def test_each_signal_starts_without_waiting_for_previous_signal(self):
        started = {"LONG": threading.Event(), "SHORT": threading.Event()}
        release = threading.Event()
        first_returned = threading.Event()
        second_returned = threading.Event()

        def transport(url, body, timeout):
            direction = json.loads(body.decode("utf-8"))["direction"]
            started[direction].set()
            release.wait()

        proxy = WebhookSignalProxy(transport=transport)
        first_caller = threading.Thread(
            target=lambda: (
                proxy.send_signal("BTCUSDT", signal("LONG", 10), amount=10.0),
                first_returned.set(),
            )
        )
        second_caller = threading.Thread(
            target=lambda: (
                proxy.send_signal("BTCUSDT", signal("SHORT", 10), amount=10.0),
                second_returned.set(),
            )
        )

        first_caller.start()
        self.assertTrue(started["LONG"].wait(timeout=1))
        first_api_returned = first_returned.wait(timeout=0.5)
        second_caller.start()
        second_started_while_first_blocked = started["SHORT"].wait(timeout=0.5)
        second_api_returned = second_returned.wait(timeout=0.5)
        release.set()
        first_caller.join(timeout=1)
        second_caller.join(timeout=1)

        self.assertTrue(first_api_returned)
        self.assertTrue(second_started_while_first_blocked)
        self.assertTrue(second_api_returned)

    def test_background_transport_failure_is_discarded_without_status(self):
        attempted = threading.Event()

        def transport(url, body, timeout):
            attempted.set()
            raise OSError("receiver unavailable")

        proxy = WebhookSignalProxy(transport=transport)

        proxy.send_signal("BTCUSDT", signal("LONG", 10), amount=10.0)

        self.assertTrue(attempted.wait(timeout=1))
        self.assertEqual(
            proxy.status(),
            {
                "enabled": True,
                "url": DEFAULT_WEBHOOK_URL,
                "last_error": None,
                "last_payload": None,
                "last_sent_at_ms": None,
            },
        )

    def test_payload_serialization_failure_is_discarded(self):
        proxy = WebhookSignalProxy()

        with patch("app.webhook.json.dumps", side_effect=TypeError("not serializable")):
            proxy.send_signal("BTCUSDT", signal("LONG", 10), amount=10.0)

        self.assertEqual(
            proxy.status(),
            {
                "enabled": True,
                "url": DEFAULT_WEBHOOK_URL,
                "last_error": None,
                "last_payload": None,
                "last_sent_at_ms": None,
            },
        )

    def test_default_transport_does_not_read_response_body(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                raise AssertionError("response body must not be read")

        with patch("app.webhook.urllib.request.urlopen", return_value=Response()) as urlopen:
            WebhookSignalProxy._post_json(DEFAULT_WEBHOOK_URL, b"{}", 3.0)

        self.assertEqual(urlopen.call_args.kwargs["timeout"], 3.0)


if __name__ == "__main__":
    unittest.main()
