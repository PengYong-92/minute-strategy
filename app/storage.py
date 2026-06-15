import json
import sqlite3
from dataclasses import fields
from pathlib import Path
from typing import Any

from app.models import Signal, SimulatedOrder


ORDER_PAGE_SIZES = (10, 20, 30, 50, 100)


def page_order_list(
    orders: list[SimulatedOrder],
    *,
    page: int = 1,
    page_size: int = 20,
    direction: str = "",
    level: str = "",
    segment: str = "",
    result: str = "",
) -> dict[str, Any]:
    ordered = sorted(orders, key=lambda item: (item.opened_at, item.id), reverse=True)
    filter_options = _order_filter_options(ordered)
    filters = {
        "direction": _clean_filter(direction),
        "level": _clean_filter(level),
        "segment": _clean_filter(segment),
        "result": _clean_filter(result),
    }
    filtered = [order for order in ordered if _order_matches(order, filters)]
    normalized_page_size = _normalize_page_size(page_size)
    total = len(filtered)
    total_pages = max(1, (total + normalized_page_size - 1) // normalized_page_size)
    normalized_page = min(max(1, int(page or 1)), total_pages)
    start = (normalized_page - 1) * normalized_page_size
    end = start + normalized_page_size
    return {
        "orders": [order.to_dict() for order in filtered[start:end]],
        "total": total,
        "page": normalized_page,
        "page_size": normalized_page_size,
        "total_pages": total_pages,
        "filters": filters,
        "filter_options": filter_options,
    }


def _normalize_page_size(page_size: int) -> int:
    try:
        value = int(page_size)
    except (TypeError, ValueError):
        return 20
    return value if value in ORDER_PAGE_SIZES else 20


def _clean_filter(value: str | None) -> str:
    return str(value or "").strip().upper()


def _order_matches(order: SimulatedOrder, filters: dict[str, str]) -> bool:
    if filters["direction"] and order.direction.upper() != filters["direction"]:
        return False
    if filters["level"] and order.level.upper() != filters["level"]:
        return False
    if filters["segment"] and order.threshold_segment.upper() != filters["segment"]:
        return False
    if filters["result"]:
        if filters["result"] == "OPEN":
            return order.status.upper() == "OPEN"
        return str(order.result or "").upper() == filters["result"]
    return True


def _order_filter_options(orders: list[SimulatedOrder]) -> dict[str, list[str]]:
    result_order = {"OPEN": 0, "WIN": 1, "LOSS": 2}
    level_order = {"S": 0, "A": 1, "B": 2}
    results = {
        "OPEN" if order.status.upper() == "OPEN" else str(order.result or "").upper()
        for order in orders
    }
    results.discard("")
    return {
        "direction": sorted({order.direction.upper() for order in orders if order.direction}),
        "level": sorted({order.level.upper() for order in orders if order.level}, key=lambda item: level_order.get(item, 99)),
        "segment": sorted({order.threshold_segment.upper() for order in orders if order.threshold_segment}),
        "result": sorted(results, key=lambda item: result_order.get(item, 99)),
        "page_size": list(ORDER_PAGE_SIZES),
    }


class SQLiteMonitorStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def save_order(self, order: SimulatedOrder, symbol: str) -> None:
        payload = order.to_dict()
        with self._connect() as connection:
            connection.execute(
                """
                insert into orders(symbol, order_id, status, result, opened_at, settled_at, payload)
                values (?, ?, ?, ?, ?, ?, ?)
                on conflict(symbol, order_id) do update set
                    status=excluded.status,
                    result=excluded.result,
                    opened_at=excluded.opened_at,
                    settled_at=excluded.settled_at,
                    payload=excluded.payload,
                    updated_at_ms=strftime('%s','now') * 1000
                """,
                (
                    symbol.upper(),
                    order.id,
                    order.status,
                    order.result,
                    order.opened_at,
                    order.settled_at,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

    def load_orders(self, symbol: str) -> list[SimulatedOrder]:
        accepted = {field.name for field in fields(SimulatedOrder)}
        with self._connect() as connection:
            rows = connection.execute(
                "select payload from orders where symbol = ? order by order_id",
                (symbol.upper(),),
            ).fetchall()
        orders = []
        for row in rows:
            payload = json.loads(row["payload"])
            clean_payload = {key: value for key, value in payload.items() if key in accepted}
            orders.append(SimulatedOrder(**clean_payload))
        return orders

    def page_orders(
        self,
        symbol: str,
        *,
        page: int = 1,
        page_size: int = 20,
        direction: str = "",
        level: str = "",
        segment: str = "",
        result: str = "",
    ) -> dict[str, Any]:
        return page_order_list(
            self.load_orders(symbol),
            page=page,
            page_size=page_size,
            direction=direction,
            level=level,
            segment=segment,
            result=result,
        )

    def save_signal(self, symbol: str, signal: Signal, decision: str, created_at_ms: int) -> None:
        payload = signal.to_dict()
        with self._connect() as connection:
            connection.execute(
                """
                insert into signal_audit(
                    symbol, created_at_ms, decision, direction, timeframe_minutes,
                    threshold_segment, regime, score, threshold, reason, payload
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol.upper(),
                    created_at_ms,
                    decision,
                    signal.direction,
                    signal.timeframe_minutes,
                    signal.threshold_segment,
                    signal.regime,
                    signal.score,
                    signal.threshold,
                    signal.reason,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

    def save_order_entry_snapshot(self, order: SimulatedOrder, symbol: str, entry_snapshot: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                insert into order_entry_snapshots(
                    symbol, order_id, direction, timeframe_minutes, opened_at, expires_at,
                    entry_price, stake, win_return, stake_progression_step,
                    threshold_segment, regime, score, threshold, edge,
                    result, settled_at, exit_price, pnl, entry_payload
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(symbol, order_id) do update set
                    direction=excluded.direction,
                    timeframe_minutes=excluded.timeframe_minutes,
                    opened_at=excluded.opened_at,
                    expires_at=excluded.expires_at,
                    entry_price=excluded.entry_price,
                    stake=excluded.stake,
                    win_return=excluded.win_return,
                    stake_progression_step=excluded.stake_progression_step,
                    threshold_segment=excluded.threshold_segment,
                    regime=excluded.regime,
                    score=excluded.score,
                    threshold=excluded.threshold,
                    edge=excluded.edge,
                    entry_payload=excluded.entry_payload,
                    updated_at_ms=strftime('%s','now') * 1000
                """,
                (
                    symbol.upper(),
                    order.id,
                    order.direction,
                    order.timeframe_minutes,
                    order.opened_at,
                    order.expires_at,
                    order.entry_price,
                    order.stake,
                    order.win_return,
                    order.stake_progression_step,
                    order.threshold_segment,
                    order.regime,
                    order.score,
                    order.threshold,
                    round(abs(order.score) - order.threshold, 4),
                    order.result,
                    order.settled_at,
                    order.exit_price,
                    order.pnl,
                    json.dumps(entry_snapshot, ensure_ascii=False),
                ),
            )

    def update_order_entry_snapshot_settlement(self, order: SimulatedOrder, symbol: str) -> None:
        settlement_payload = {
            "status": order.status,
            "result": order.result,
            "settled_at": order.settled_at,
            "exit_price": order.exit_price,
            "pnl": order.pnl,
        }
        with self._connect() as connection:
            connection.execute(
                """
                update order_entry_snapshots
                set result = ?,
                    settled_at = ?,
                    exit_price = ?,
                    pnl = ?,
                    settlement_payload = ?,
                    updated_at_ms = strftime('%s','now') * 1000
                where symbol = ? and order_id = ?
                """,
                (
                    order.result,
                    order.settled_at,
                    order.exit_price,
                    order.pnl,
                    json.dumps(settlement_payload, ensure_ascii=False),
                    symbol.upper(),
                    order.id,
                ),
            )

    def load_order_entry_snapshots(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select *
                from order_entry_snapshots
                where symbol = ?
                order by order_id desc
                limit ?
                """,
                (symbol.upper(), limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["entry_payload"] = json.loads(item["entry_payload"])
            item["settlement_payload"] = json.loads(item["settlement_payload"]) if item["settlement_payload"] else None
            result.append(item)
        return result

    def load_recent_signals(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select symbol, created_at_ms, decision, direction, timeframe_minutes,
                       threshold_segment, regime, score, threshold, reason
                from signal_audit
                where symbol = ?
                order by id desc
                limit ?
                """,
                (symbol.upper(), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                create table if not exists orders (
                    symbol text not null,
                    order_id integer not null,
                    status text not null,
                    result text,
                    opened_at integer not null,
                    settled_at integer,
                    payload text not null,
                    updated_at_ms integer not null default (strftime('%s','now') * 1000),
                    primary key(symbol, order_id)
                )
                """
            )
            connection.execute(
                """
                create table if not exists signal_audit (
                    id integer primary key autoincrement,
                    symbol text not null,
                    created_at_ms integer not null,
                    decision text not null,
                    direction text not null,
                    timeframe_minutes integer not null,
                    threshold_segment text not null,
                    regime text not null,
                    score real not null,
                    threshold real not null,
                    reason text not null,
                    payload text not null
                )
                """
            )
            connection.execute("create index if not exists idx_signal_audit_symbol_time on signal_audit(symbol, created_at_ms)")
            connection.execute(
                """
                create table if not exists order_entry_snapshots (
                    symbol text not null,
                    order_id integer not null,
                    direction text not null,
                    timeframe_minutes integer not null,
                    opened_at integer not null,
                    expires_at integer not null,
                    entry_price real not null,
                    stake real not null,
                    win_return real not null,
                    stake_progression_step integer not null,
                    threshold_segment text not null,
                    regime text not null,
                    score real not null,
                    threshold real not null,
                    edge real not null,
                    result text,
                    settled_at integer,
                    exit_price real,
                    pnl real not null default 0.0,
                    entry_payload text not null,
                    settlement_payload text,
                    created_at_ms integer not null default (strftime('%s','now') * 1000),
                    updated_at_ms integer not null default (strftime('%s','now') * 1000),
                    primary key(symbol, order_id)
                )
                """
            )
            connection.execute(
                "create index if not exists idx_order_entry_snapshots_symbol_opened on order_entry_snapshots(symbol, opened_at)"
            )
            connection.execute(
                "create index if not exists idx_order_entry_snapshots_symbol_result on order_entry_snapshots(symbol, result)"
            )
