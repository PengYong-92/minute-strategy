import tempfile
import unittest
from pathlib import Path

from app.models import Signal, SimulatedOrder
from app.simulator import AccountSimulator
from app.storage import SQLiteMonitorStore


def signal(direction="LONG", timeframe_minutes=10):
    return Signal(
        direction=direction,
        timeframe_minutes=timeframe_minutes,
        level="A",
        reason="persist me",
        price=100.0,
        open_time=0,
        score=82.0,
        threshold=70.0,
        threshold_segment="WD-12",
        session_allowed=True,
        session_sample_size=37,
        session_win_rate=0.6757,
        session_ev=2.1622,
        session_edge_min=10.0,
        regime="FEAR_RISING",
    )


class SQLiteMonitorStoreTest(unittest.TestCase):
    def test_persists_and_restores_simulated_orders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            simulator = AccountSimulator()
            order = simulator.open_order(signal(), entry_price=100.0, opened_at=1_000)
            simulator.settle_expired_orders(current_time=601_000, current_price=101.0)

            store.save_order(order, symbol="BTCUSDT")
            restored = store.load_orders("BTCUSDT")

        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].id, order.id)
        self.assertEqual(restored[0].status, "SETTLED")
        self.assertEqual(restored[0].result, "WIN")
        self.assertEqual(restored[0].pnl, 8.0)

    def test_persists_signal_audit_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")

            store.save_signal("BTCUSDT", signal(), decision="OPENED", created_at_ms=1_234)
            rows = store.load_recent_signals("BTCUSDT", limit=5)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "BTCUSDT")
        self.assertEqual(rows[0]["decision"], "OPENED")
        self.assertEqual(rows[0]["direction"], "LONG")
        self.assertEqual(rows[0]["regime"], "FEAR_RISING")

    def test_persists_order_entry_snapshot_and_updates_settlement(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            simulator = AccountSimulator()
            order = simulator.open_order(signal(), entry_price=100.0, opened_at=1_000)
            entry_snapshot = {
                "signal": signal().to_dict(),
                "rolling_edge": {"status": "NORMAL", "sample_size": 21, "win_rate": 0.619},
                "latest_kline": {"close": 100.0, "close_time": 1_000},
                "stake_config": {"stake": 10.0, "win_return": 18.0},
            }

            store.save_order_entry_snapshot(order, "BTCUSDT", entry_snapshot)
            simulator.settle_expired_orders(current_time=601_000, current_price=99.0)
            store.update_order_entry_snapshot_settlement(order, "BTCUSDT")
            rows = store.load_order_entry_snapshots("BTCUSDT")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "BTCUSDT")
        self.assertEqual(rows[0]["order_id"], 1)
        self.assertEqual(rows[0]["direction"], "LONG")
        self.assertEqual(rows[0]["threshold_segment"], "WD-12")
        self.assertEqual(rows[0]["result"], "LOSS")
        self.assertEqual(rows[0]["pnl"], -10.0)
        self.assertEqual(rows[0]["entry_payload"]["rolling_edge"]["sample_size"], 21)
        self.assertEqual(rows[0]["settlement_payload"]["exit_price"], 99.0)

    def test_pages_and_filters_orders_for_dashboard(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            fixtures = [
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
                    level="A",
                    reason="short loss",
                    entry_price=100.0,
                    opened_at=2_000,
                    expires_at=602_000,
                    threshold_segment="WD-23",
                    status="SETTLED",
                    result="LOSS",
                    exit_price=101.0,
                    settled_at=602_000,
                    pnl=-10.0,
                ),
                SimulatedOrder(
                    id=3,
                    direction="SHORT",
                    timeframe_minutes=10,
                    level="S",
                    reason="short open",
                    entry_price=100.0,
                    opened_at=3_000,
                    expires_at=603_000,
                    threshold_segment="WD-23",
                    status="OPEN",
                    result=None,
                ),
            ]
            for order in fixtures:
                store.save_order(order, "BTCUSDT")
            for order_id in range(4, 14):
                store.save_order(
                    SimulatedOrder(
                        id=order_id,
                        direction="LONG",
                        timeframe_minutes=10,
                        level="B",
                        reason="page filler",
                        entry_price=100.0,
                        opened_at=order_id * 1_000,
                        expires_at=order_id * 1_000 + 600_000,
                        threshold_segment="WD-12",
                        status="SETTLED",
                        result="WIN",
                        exit_price=101.0,
                        settled_at=order_id * 1_000 + 600_000,
                        pnl=8.0,
                    ),
                    "BTCUSDT",
                )

            first_page = store.page_orders("BTCUSDT", page=1, page_size=10)
            filtered = store.page_orders(
                "BTCUSDT",
                page=1,
                page_size=10,
                direction="SHORT",
                level="S",
                segment="WD-23",
                result="OPEN",
            )

        self.assertEqual(first_page["total"], 13)
        self.assertEqual(first_page["page"], 1)
        self.assertEqual(first_page["page_size"], 10)
        self.assertEqual(first_page["total_pages"], 2)
        self.assertEqual([item["id"] for item in first_page["orders"]], [13, 12, 11, 10, 9, 8, 7, 6, 5, 4])
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["orders"][0]["id"], 3)
        self.assertEqual(filtered["orders"][0]["direction"], "SHORT")


if __name__ == "__main__":
    unittest.main()
