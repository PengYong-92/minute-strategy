import json
import time
from urllib.parse import urlencode
from urllib.request import urlopen

from app.models import Kline


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
