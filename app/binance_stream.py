import threading
from collections.abc import Callable

from app.binance_client import BinanceSpotStreamEvent


class BinanceSpotWebSocketClient:
    def __init__(
        self,
        base_url: str = "wss://stream.binance.com:9443",
        ping_interval: int = 20,
        ping_timeout: int = 10,
    ):
        self.base_url = base_url.rstrip("/")
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self._lock = threading.Lock()
        self._app = None

    def stream_url(self, symbol: str) -> str:
        stream_symbol = symbol.strip().lower()
        streams = f"{stream_symbol}@kline_1m/{stream_symbol}@miniTicker"
        return f"{self.base_url}/stream?streams={streams}"

    def run(
        self,
        symbol: str,
        *,
        on_event: Callable[[BinanceSpotStreamEvent], None],
        on_status: Callable[[str], None],
        should_continue: Callable[[], bool],
    ) -> None:
        try:
            import websocket
        except ImportError as exc:
            raise RuntimeError(
                "缺少 websocket-client 依赖，请先安装 requirements.txt"
            ) from exc

        expected_symbol = symbol.upper()

        def on_open(app) -> None:
            if not should_continue():
                app.close()
                return
            on_status("CONNECTED")

        def on_message(app, message) -> None:
            if not should_continue():
                app.close()
                return
            try:
                event = BinanceSpotStreamEvent.parse(message)
            except (KeyError, TypeError, ValueError):
                return
            if event.symbol == expected_symbol:
                on_event(event)

        def on_error(_app, _error) -> None:
            if should_continue():
                on_status("ERROR")

        def on_close(_app, _code, _message) -> None:
            if should_continue():
                on_status("DISCONNECTED")

        app = websocket.WebSocketApp(
            self.stream_url(symbol),
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        with self._lock:
            self._app = app
        try:
            app.run_forever(
                ping_interval=self.ping_interval,
                ping_timeout=self.ping_timeout,
            )
        finally:
            with self._lock:
                if self._app is app:
                    self._app = None

    def close(self) -> None:
        with self._lock:
            app = self._app
        if app is not None:
            app.close()
