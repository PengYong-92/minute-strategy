import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from app.models import SimulatedOrder
from app.server import make_handler
from app.state import MonitorState
from app.storage import SQLiteMonitorStore


class OrdersApiTest(unittest.TestCase):
    def test_orders_api_pages_and_filters_persisted_orders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            for order in [
                SimulatedOrder(
                    id=1,
                    direction="LONG",
                    timeframe_minutes=10,
                    level="S",
                    reason="long win",
                    entry_price=100.0,
                    opened_at=1_000,
                    expires_at=601_000,
                    threshold_segment="WD-08",
                    status="SETTLED",
                    result="WIN",
                    exit_price=101.0,
                    settled_at=601_000,
                    pnl=8.0,
                ),
                SimulatedOrder(
                    id=2,
                    direction="SHORT",
                    timeframe_minutes=10,
                    level="S",
                    reason="short open",
                    entry_price=100.0,
                    opened_at=2_000,
                    expires_at=602_000,
                    threshold_segment="WD-23",
                    status="OPEN",
                ),
            ]:
                store.save_order(order, "BTCUSDT")
            state = MonitorState(symbol="BTCUSDT", storage=store)
            server = _serve(state)
            try:
                payload = _get_json(
                    f"http://127.0.0.1:{server.server_port}/api/orders"
                    "?page=1&page_size=10&direction=SHORT&level=S&segment=WD-23&result=OPEN"
                )
            finally:
                server.shutdown()
                server.server_close()

        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["page_size"], 10)
        self.assertEqual(payload["orders"][0]["id"], 2)
        self.assertEqual(payload["orders"][0]["direction"], "SHORT")


def _serve(state):
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
