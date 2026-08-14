import queue
import threading
import time
from collections.abc import Sequence

from app.binance_client import BinanceKlineClient, BinanceSpotStreamEvent
from app.binance_stream import BinanceSpotWebSocketClient
from app.models import Kline


class MarketDataCoordinator:
    def __init__(
        self,
        state,
        rest_client: BinanceKlineClient | None,
        *,
        stream_client: BinanceSpotWebSocketClient | None = None,
        poll_seconds: float = 10,
        rest_limit: int = 300,
        enable_websocket: bool = True,
        reconnect_max_seconds: float = 30,
    ):
        self.state = state
        self.rest_client = rest_client
        self.stream_client = stream_client or BinanceSpotWebSocketClient()
        self.poll_seconds = max(1.0, float(poll_seconds))
        self.rest_limit = max(1, int(rest_limit))
        self.enable_websocket = bool(enable_websocket)
        self.reconnect_max_seconds = max(1.0, float(reconnect_max_seconds))
        self._queue: queue.Queue = queue.Queue()
        self._stop_requested = threading.Event()
        self._stop_event = threading.Event()
        self._refresh_event = threading.Event()
        self._rest_wakeup = threading.Event()
        self._updates_enabled = threading.Event()
        self._updates_enabled.set()
        self._consumer_thread: threading.Thread | None = None
        self._stream_thread: threading.Thread | None = None
        self._rest_thread: threading.Thread | None = None
        self._stream_lock = threading.Lock()
        self._processing_lock = threading.Lock()
        self._stream_connected = False
        self._last_stream_event_at = 0.0
        self._cursor: dict[tuple[str, int], int | None] = {}
        self._retry_lock = threading.Lock()
        self._pending_retry: dict[tuple[str, int], list[Kline]] = {}
        self._retry_required = threading.Event()

    def start(self) -> None:
        self.start_consumer()
        self._rest_thread = threading.Thread(
            target=self._rest_loop,
            name="binance-rest-recovery",
            daemon=True,
        )
        self._rest_thread.start()
        if self.enable_websocket:
            self._stream_thread = threading.Thread(
                target=self._stream_loop,
                name="binance-websocket",
                daemon=True,
            )
            self._stream_thread.start()
        else:
            self.state.record_market_stream_status("REST_ONLY")

    def start_consumer(self) -> None:
        if self._consumer_thread and self._consumer_thread.is_alive():
            return
        self._consumer_thread = threading.Thread(
            target=self._consume,
            name="market-data-consumer",
            daemon=True,
        )
        self._consumer_thread.start()

    def handle_stream_event(
        self,
        event: BinanceSpotStreamEvent,
        context: tuple[str, int],
    ) -> None:
        if (
            not self._context_active(context)
            or event.symbol != context[0]
        ):
            return
        with self._stream_lock:
            self._last_stream_event_at = time.monotonic()
        self.state.update_realtime_price(
            event.price,
            event.event_time_ms,
            event.received_at_ms,
            expected_context=context,
        )
        if event.event_type == "kline" and event.is_closed and event.kline is not None:
            self._enqueue_klines([event.kline], context)

    def pause_updates(self) -> None:
        self._updates_enabled.clear()
        self._refresh_event.set()
        self._rest_wakeup.set()
        self.stream_client.close()
        with self._processing_lock:
            self._clear_all_retries()
            self._cursor.clear()

    def request_symbol_refresh(self) -> None:
        self._updates_enabled.set()
        self._clear_all_retries()
        self._refresh_event.set()
        self._rest_wakeup.set()
        self.stream_client.close()

    def wait_until_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        return self._queue.unfinished_tasks == 0

    def stop(self) -> None:
        self._stop_requested.set()
        self._refresh_event.set()
        self._rest_wakeup.set()
        self.stream_client.close()
        with self._processing_lock:
            self._stop_event.set()
            self._updates_enabled.clear()
            self._clear_all_retries()
            self._cursor.clear()
        for thread in (self._stream_thread, self._rest_thread):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=2)
        self._queue.put(None)
        if self._consumer_thread and self._consumer_thread is not threading.current_thread():
            self._consumer_thread.join(timeout=2)

    def _enqueue_klines(
        self,
        klines: Sequence[Kline],
        context: tuple[str, int],
    ) -> None:
        if (
            klines
            and not self._stop_event.is_set()
            and self._updates_enabled.is_set()
        ):
            self._queue.put((context, list(klines)))

    def _consume(self) -> None:
        while True:
            item = self._queue.get()
            context = None
            try:
                if item is None:
                    return
                context, klines = item
                self._process_klines(context, klines)
            except Exception as exc:  # noqa: BLE001 - 数据源异常不能结束策略消费线程。
                self.state.record_error(
                    f"market data update failed: {exc}",
                    expected_context=context,
                )
            finally:
                self._queue.task_done()

    def _process_klines(
        self,
        context: tuple[str, int],
        klines: Sequence[Kline],
    ) -> None:
        if not self._context_active(context):
            return
        with self._retry_lock:
            pending_retry = list(self._pending_retry.get(context, ()))
        klines = [*pending_retry, *klines]
        if context in self._cursor:
            cursor = self._cursor[context]
        else:
            accessor = getattr(self.state, "latest_kline_open_time", None)
            cursor = accessor(expected_context=context) if accessor else None
            self._cursor[context] = cursor

        ordered = sorted(
            {item.open_time: item for item in klines}.values(),
            key=lambda item: item.open_time,
        )
        fresh = [item for item in ordered if cursor is None or item.open_time > cursor]
        if not fresh:
            return

        if cursor is not None and fresh[0].open_time > cursor + 60_000:
            recovered = self._download_closed_klines(context)
            ordered = sorted(
                {item.open_time: item for item in (*recovered, *fresh)}.values(),
                key=lambda item: item.open_time,
            )
            fresh = [item for item in ordered if item.open_time > cursor]
        if not fresh or not self._context_active(context):
            return

        with self._processing_lock:
            if not self._context_active(context):
                return
            processed = self.state.update_from_klines(
                fresh,
                expected_context=context,
            )
        if processed and self._context_active(context):
            self._cursor[context] = fresh[-1].open_time
            self._clear_retry(context)
        elif self._context_active(context):
            self._mark_retry(context, fresh)

    def _download_closed_klines(self, context: tuple[str, int]) -> list[Kline]:
        if self.rest_client is None or not self._context_active(context):
            return []
        return self.rest_client.get_klines(
            context[0],
            interval="1m",
            limit=self.rest_limit,
        )

    def _context_active(self, context: tuple[str, int]) -> bool:
        return (
            not self._stop_requested.is_set()
            and not self._stop_event.is_set()
            and self._updates_enabled.is_set()
            and context == self.state.capture_symbol_context()
        )

    def _mark_retry(
        self,
        context: tuple[str, int],
        klines: Sequence[Kline],
    ) -> None:
        with self._retry_lock:
            existing = self._pending_retry.get(context, [])
            self._pending_retry[context] = sorted(
                {item.open_time: item for item in (*existing, *klines)}.values(),
                key=lambda item: item.open_time,
            )
            self._retry_required.set()
        self._rest_wakeup.set()

    def _clear_retry(self, context: tuple[str, int]) -> None:
        with self._retry_lock:
            self._pending_retry.pop(context, None)
            if not self._pending_retry:
                self._retry_required.clear()

    def _clear_all_retries(self) -> None:
        with self._retry_lock:
            self._pending_retry.clear()
            self._retry_required.clear()

    def _rest_loop(self) -> None:
        while not self._stop_event.is_set():
            self._rest_wakeup.clear()
            if self._stop_event.is_set():
                return
            if not self._updates_enabled.is_set():
                self._rest_wakeup.wait(self.poll_seconds)
                continue
            context = self.state.capture_symbol_context()
            stream_healthy = self.enable_websocket and self._stream_is_healthy()
            retry_required = self._retry_required.is_set()
            if retry_required or not stream_healthy:
                try:
                    self._enqueue_klines(self._download_closed_klines(context), context)
                except Exception as exc:  # noqa: BLE001 - REST 是断线补偿，不可终止服务。
                    self.state.record_error(
                        f"REST recovery failed: {exc}",
                        expected_context=context,
                    )

            wait_seconds = (
                1.0
                if stream_healthy and not retry_required
                else self.poll_seconds
            )
            self._rest_wakeup.wait(wait_seconds)

    def _stream_is_healthy(self) -> bool:
        with self._stream_lock:
            return self._stream_connected and (
                time.monotonic() - self._last_stream_event_at <= 5.0
            )

    def _stream_was_recently_active(self) -> bool:
        with self._stream_lock:
            return bool(
                self._last_stream_event_at
                and time.monotonic() - self._last_stream_event_at <= 5.0
            )

    def _record_stream_status(self, status: str, context: tuple[str, int]) -> None:
        with self._stream_lock:
            self._stream_connected = status == "CONNECTED"
            if status == "CONNECTING":
                self._last_stream_event_at = 0.0
        self.state.record_market_stream_status(status, expected_context=context)
        if status != "CONNECTED":
            self._rest_wakeup.set()

    def _stream_loop(self) -> None:
        backoff = 1.0
        while not self._stop_event.is_set():
            if not self._updates_enabled.wait(timeout=0.5):
                continue
            if self._stop_event.is_set():
                return
            context = self.state.capture_symbol_context()
            self._refresh_event.clear()
            self._record_stream_status("CONNECTING", context)
            try:
                self.stream_client.run(
                    context[0],
                    on_event=lambda event: self.handle_stream_event(event, context),
                    on_status=lambda status: self._record_stream_status(status, context),
                    should_continue=lambda: (
                        not self._stop_requested.is_set()
                        and not self._stop_event.is_set()
                        and self._updates_enabled.is_set()
                        and context == self.state.capture_symbol_context()
                    ),
                )
                if self._stream_was_recently_active():
                    backoff = 1.0
            except Exception as exc:  # noqa: BLE001 - 自动转 REST 并重连。
                self.state.record_error(
                    f"WebSocket failed: {exc}",
                    expected_context=context,
                )
                self._record_stream_status("RECONNECTING", context)

            if self._stop_event.is_set():
                return
            self._rest_wakeup.set()
            if self._refresh_event.wait(backoff):
                backoff = 1.0
                continue
            backoff = min(self.reconnect_max_seconds, backoff * 2)
