import json
import threading
import urllib.request
from dataclasses import dataclass
from typing import Callable

from app.models import Signal


DEFAULT_WEBHOOK_URL = "https://event.easy-tx.com/api/signals/ingest"
DEFAULT_IMPORT_TOKEN = ""

Transport = Callable[[str, bytes, float], None]


def time_increment_for_minutes(minutes: int) -> str | None:
    return {
        10: "TEN_MINUTE",
        30: "THIRTY_MINUTE",
    }.get(minutes)


@dataclass
class WebhookSignalProxy:
    url: str = DEFAULT_WEBHOOK_URL
    import_token: str = DEFAULT_IMPORT_TOKEN
    timeout_seconds: float = 5.0
    enabled: bool = True
    transport: Transport | None = None

    def build_payload(self, symbol: str, signal: Signal, message: str | None = None, amount: float | None = None) -> dict:
        payload = {
            "importToken": self.import_token,
            "direction": signal.direction,
            "symbol": symbol.upper(),
            "message": signal.reason if message is None else message,
        }
        if amount is not None:
            payload["amount"] = round(float(amount), 4)
        time_increment = time_increment_for_minutes(signal.timeframe_minutes)
        if time_increment:
            payload["timeIncrements"] = time_increment
        return payload

    def send_signal(self, symbol: str, signal: Signal, message: str | None = None, amount: float | None = None) -> None:
        if not self.enabled:
            return
        if signal.direction not in {"LONG", "SHORT"}:
            return

        try:
            payload = self.build_payload(symbol, signal, message, amount=amount)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            transport = self.transport or self._post_json
            threading.Thread(
                target=self._send_once,
                args=(transport, self.url, body, self.timeout_seconds),
                name="webhook-signal-sender",
                daemon=True,
            ).start()
        except Exception:  # noqa: BLE001 - 最低延迟模式明确接受线程启动失败时丢弃信号。
            return

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "url": self.url,
            "last_error": None,
            "last_payload": None,
            "last_sent_at_ms": None,
        }

    @staticmethod
    def _send_once(transport: Transport, url: str, body: bytes, timeout: float) -> None:
        try:
            transport(url, body, timeout)
        except Exception:  # noqa: BLE001 - 不确认、不重试、不记录，失败信号直接丢弃。
            return

    @staticmethod
    def _post_json(url: str, body: bytes, timeout: float) -> None:
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "codex-event-monitor/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout):
            pass
