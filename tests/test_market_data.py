import json
import threading
import time
import unittest

from app.binance_client import BinanceSpotStreamEvent
from app.market_data import MarketDataCoordinator


def stream_event(open_time: int, close: float, *, closed: bool = True) -> BinanceSpotStreamEvent:
    return BinanceSpotStreamEvent.parse(
        json.dumps(
            {
                "e": "kline",
                "E": open_time + 60_010,
                "s": "BTCUSDT",
                "k": {
                    "t": open_time,
                    "T": open_time + 59_999,
                    "s": "BTCUSDT",
                    "i": "1m",
                    "o": str(close - 1),
                    "c": str(close),
                    "h": str(close + 1),
                    "l": str(close - 2),
                    "v": "10",
                    "x": closed,
                },
            }
        ),
        received_at_ms=open_time + 60_020,
    )


class RecordingState:
    def __init__(self):
        self.context = ("BTCUSDT", 0)
        self.latest_open_time = None
        self.realtime = []
        self.closed_batches = []
        self.errors = []
        self.updated = threading.Event()

    def capture_symbol_context(self):
        return self.context

    def update_realtime_price(self, price, event_time_ms, received_at_ms, *, expected_context):
        self.realtime.append((price, event_time_ms, received_at_ms, expected_context))
        return expected_context == self.context

    def update_from_klines(self, klines, *, expected_context):
        self.closed_batches.append((list(klines), expected_context))
        self.latest_open_time = klines[-1].open_time
        self.updated.set()
        return True

    def latest_kline_open_time(self, *, expected_context):
        if expected_context != self.context:
            return None
        return self.latest_open_time

    def record_market_stream_status(self, status, **_kwargs):
        self.status = status

    def record_error(self, message, **_kwargs):
        self.errors.append(message)


class MarketDataCoordinatorTest(unittest.TestCase):
    def test_open_kline_only_updates_realtime_price(self):
        state = RecordingState()
        coordinator = MarketDataCoordinator(state, rest_client=None, rest_limit=10)

        coordinator.handle_stream_event(stream_event(0, 101.0, closed=False), state.context)

        self.assertEqual(len(state.realtime), 1)
        self.assertEqual(state.closed_batches, [])

    def test_closed_kline_is_processed_once_by_single_consumer(self):
        state = RecordingState()
        coordinator = MarketDataCoordinator(state, rest_client=None, rest_limit=10)
        coordinator.start_consumer()
        event = stream_event(0, 101.0)
        try:
            coordinator.handle_stream_event(event, state.context)
            coordinator.handle_stream_event(event, state.context)
            self.assertTrue(state.updated.wait(timeout=1))
            coordinator.wait_until_idle(timeout=1)
        finally:
            coordinator.stop()

        self.assertEqual(len(state.closed_batches), 1)
        self.assertEqual(state.closed_batches[0][0], [event.kline])

    def test_old_symbol_stream_event_is_discarded(self):
        state = RecordingState()
        coordinator = MarketDataCoordinator(state, rest_client=None, rest_limit=10)
        coordinator.start_consumer()
        old_context = state.context
        state.context = ("ETHUSDT", 1)
        try:
            coordinator.handle_stream_event(stream_event(0, 101.0), old_context)
            time.sleep(0.02)
            coordinator.wait_until_idle(timeout=1)
        finally:
            coordinator.stop()

        self.assertEqual(state.realtime, [])
        self.assertEqual(state.closed_batches, [])

    def test_rest_response_is_discarded_when_symbol_changes_during_request(self):
        state = RecordingState()
        requested = threading.Event()

        class SwitchingClient:
            def get_klines(self, symbol, interval, limit):
                self.requested_symbol = symbol
                state.context = ("ETHUSDT", 1)
                requested.set()
                return [stream_event(0, 101.0).kline]

        client = SwitchingClient()
        coordinator = MarketDataCoordinator(
            state,
            rest_client=client,
            poll_seconds=3_600,
            rest_limit=100,
            enable_websocket=False,
        )
        coordinator.start()
        try:
            self.assertTrue(requested.wait(timeout=1))
            coordinator.wait_until_idle(timeout=1)
        finally:
            coordinator.stop()

        self.assertEqual(client.requested_symbol, "BTCUSDT")
        self.assertEqual(state.closed_batches, [])

    def test_paused_symbol_warmup_does_not_process_stream_events(self):
        state = RecordingState()
        coordinator = MarketDataCoordinator(state, rest_client=None, rest_limit=10)
        coordinator.start_consumer()
        event = stream_event(0, 101.0)
        try:
            coordinator.pause_updates()
            coordinator.handle_stream_event(event, state.context)
            coordinator.wait_until_idle(timeout=1)
            self.assertEqual(state.realtime, [])
            self.assertEqual(state.closed_batches, [])

            coordinator.request_symbol_refresh()
            coordinator.handle_stream_event(event, state.context)
            self.assertTrue(state.updated.wait(timeout=1))
            coordinator.wait_until_idle(timeout=1)
        finally:
            coordinator.stop()

        self.assertEqual(len(state.realtime), 1)
        self.assertEqual(len(state.closed_batches), 1)

    def test_failed_strategy_update_forces_rest_retry_before_advancing_cursor(self):
        state = RecordingState()
        results = iter((False, True))
        event = stream_event(0, 101.0)

        class RecoveryClient:
            def __init__(self):
                self.requested = threading.Event()

            def get_klines(self, symbol, interval, limit):
                self.requested.set()
                return [event.kline]

        client = RecoveryClient()

        def update(klines, *, expected_context):
            state.closed_batches.append((list(klines), expected_context))
            state.latest_open_time = klines[-1].open_time
            state.updated.set()
            return next(results)

        state.update_from_klines = update
        coordinator = MarketDataCoordinator(
            state,
            rest_client=client,
            poll_seconds=3_600,
            rest_limit=10,
        )
        coordinator.start_consumer()
        try:
            coordinator.handle_stream_event(event, state.context)
            self.assertTrue(state.updated.wait(timeout=1))
            coordinator.wait_until_idle(timeout=1)
            state.updated.clear()
            coordinator._rest_thread = threading.Thread(target=coordinator._rest_loop)
            coordinator._rest_thread.start()
            self.assertTrue(client.requested.wait(timeout=1))
            self.assertTrue(state.updated.wait(timeout=1))
            coordinator.wait_until_idle(timeout=1)
        finally:
            coordinator.stop()

        self.assertEqual(len(state.closed_batches), 2)

    def test_stop_discards_queued_klines_before_they_enter_strategy(self):
        state = RecordingState()
        started = threading.Event()
        release = threading.Event()

        def blocking_update(klines, *, expected_context):
            state.closed_batches.append((list(klines), expected_context))
            started.set()
            release.wait(timeout=1)
            return True

        state.update_from_klines = blocking_update
        coordinator = MarketDataCoordinator(state, rest_client=None, rest_limit=10)
        coordinator.start_consumer()
        coordinator.handle_stream_event(stream_event(0, 101.0), state.context)
        self.assertTrue(started.wait(timeout=1))
        coordinator.handle_stream_event(stream_event(60_000, 102.0), state.context)

        stopping = threading.Thread(target=coordinator.stop)
        stopping.start()
        try:
            self.assertTrue(coordinator._stop_requested.wait(timeout=1))
        finally:
            release.set()
            stopping.join(timeout=2)

        self.assertFalse(stopping.is_alive())
        self.assertEqual(len(state.closed_batches), 1)


if __name__ == "__main__":
    unittest.main()
