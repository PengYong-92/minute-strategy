import json
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable

from app.models import FearGreedContext


FNG_URL = "https://api.alternative.me/fng/?limit=30&format=json"


@dataclass
class FearGreedProvider:
    ttl_seconds: int = 3600
    timeout_seconds: int = 5
    url: str = FNG_URL
    now_ms: Callable[[], int] | None = None

    def __post_init__(self) -> None:
        self._cached: FearGreedContext | None = None
        self._last_fetch_ms = 0

    def get_context(self) -> FearGreedContext:
        current_ms = self._now_ms()
        if self._cached and current_ms - self._last_fetch_ms < self.ttl_seconds * 1000:
            return self._cached

        try:
            context = self._fetch(current_ms)
        except Exception:  # noqa: BLE001 - 使用旧缓存比中断监控更安全。
            if self._cached:
                return self._cached
            return FearGreedContext(value=50, classification="Neutral", trend="unknown", updated_at_ms=current_ms)

        self._cached = context
        self._last_fetch_ms = current_ms
        return context

    def _fetch(self, current_ms: int) -> FearGreedContext:
        request = urllib.request.Request(self.url, headers={"User-Agent": "codex-event-monitor/1.0"})
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))

        rows = payload.get("data", [])
        if not rows:
            raise ValueError("empty Fear & Greed response")

        latest = rows[0]
        value = int(latest["value"])
        values = [int(row["value"]) for row in rows if str(row.get("value", "")).isdigit()]
        average_30d = round(sum(values) / len(values), 2) if values else float(value)
        trend = _trend(values)
        updated_at_ms = int(latest.get("timestamp", "0")) * 1000 or current_ms
        return FearGreedContext(
            value=value,
            classification=str(latest.get("value_classification", "")),
            average_30d=average_30d,
            trend=trend,
            updated_at_ms=updated_at_ms,
        )

    def _now_ms(self) -> int:
        if self.now_ms:
            return self.now_ms()
        return int(time.time() * 1000)


def _trend(values: list[int]) -> str:
    if len(values) < 2:
        return "unknown"
    latest = values[0]
    average = sum(values) / len(values)
    if latest >= average + 3:
        return "rising"
    if latest <= average - 3:
        return "falling"
    return "flat"
