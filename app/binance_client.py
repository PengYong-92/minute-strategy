import json
import time
from dataclasses import dataclass
from urllib.parse import urlencode
from urllib.request import urlopen

from app.models import Kline


@dataclass(frozen=True)
class BinanceSpotStreamEvent:
    symbol: str
    event_type: str
    event_time_ms: int
    received_at_ms: int
    price: float
    interval: str = ""
    kline: Kline | None = None
    is_closed: bool = False

    @classmethod
    def parse(
        cls,
        message: str | bytes | dict,
        *,
        received_at_ms: int | None = None,
    ) -> "BinanceSpotStreamEvent":
        if isinstance(message, bytes):
            message = message.decode("utf-8")
        payload = json.loads(message) if isinstance(message, str) else message
        if not isinstance(payload, dict):
            raise ValueError("invalid Binance stream payload")
        data = payload.get("data", payload)
        if not isinstance(data, dict):
            raise ValueError("invalid Binance stream data")

        event_type = str(data.get("e", ""))
        received = int(time.time() * 1000) if received_at_ms is None else int(received_at_ms)
        if event_type == "24hrMiniTicker":
            symbol = str(data.get("s", "")).upper()
            if not symbol:
                raise ValueError("missing Binance stream symbol")
            return cls(
                symbol=symbol,
                event_type=event_type,
                event_time_ms=int(data["E"]),
                received_at_ms=received,
                price=float(data["c"]),
            )

        if event_type != "kline":
            raise ValueError(f"unsupported Binance stream event: {event_type or 'unknown'}")
        raw_kline = data.get("k")
        if not isinstance(raw_kline, dict):
            raise ValueError("missing Binance stream kline")
        interval = str(raw_kline.get("i", ""))
        if interval != "1m":
            raise ValueError(f"unsupported Binance stream interval: {interval or 'unknown'}")
        symbol = str(data.get("s") or raw_kline.get("s") or "").upper()
        if not symbol:
            raise ValueError("missing Binance stream symbol")
        kline = Kline(
            open_time=int(raw_kline["t"]),
            open=float(raw_kline["o"]),
            high=float(raw_kline["h"]),
            low=float(raw_kline["l"]),
            close=float(raw_kline["c"]),
            volume=float(raw_kline["v"]),
            close_time=int(raw_kline["T"]),
        )
        return cls(
            symbol=symbol,
            event_type=event_type,
            event_time_ms=int(data["E"]),
            received_at_ms=received,
            price=kline.close,
            interval=interval,
            kline=kline,
            is_closed=bool(raw_kline.get("x")),
        )


class BinanceKlineClient:
    def __init__(self, base_url: str = "https://api.binance.com", timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get_klines(self, symbol: str, interval: str = "1m", limit: int = 500) -> list[Kline]:
        query = urlencode({"symbol": symbol.upper(), "interval": interval, "limit": limit})
        url = f"{self.base_url}/api/v3/klines?{query}"
        with urlopen(url, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return self.filter_closed_klines([self.parse_kline(row) for row in payload])

    @staticmethod
    def parse_kline(row: list) -> Kline:
        return Kline(
            open_time=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            close_time=int(row[6]),
        )

    @staticmethod
    def filter_closed_klines(klines: list[Kline], now_ms: int | None = None) -> list[Kline]:
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        return [kline for kline in klines if kline.close_time <= now_ms]
