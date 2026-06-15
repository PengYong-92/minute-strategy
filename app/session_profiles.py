from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence


@dataclass(frozen=True)
class GeneratedSessionEdge:
    sample_size: int
    win_rate: float
    ev: float


def build_session_edges(orders: Sequence[dict], min_sample_size: int = 5) -> dict[str, GeneratedSessionEdge]:
    groups: dict[str, list[dict]] = {}
    for order in orders:
        if not order.get("result"):
            continue
        key = f"{order['timeframe_minutes']}|{order.get('threshold_segment', 'GLOBAL')}"
        groups.setdefault(key, []).append(order)
    return {
        key: _edge(group)
        for key, group in groups.items()
        if len(group) >= min_sample_size
    }


def stable_segments(
    orders: Sequence[dict],
    min_months: int = 2,
    min_sample_size: int = 5,
    min_win_rate: float = 0.60,
    min_ev: float = 0.0,
) -> set[str]:
    by_segment_month: dict[tuple[str, str], list[dict]] = {}
    for order in orders:
        if not order.get("result"):
            continue
        segment = f"{order['timeframe_minutes']}|{order.get('threshold_segment', 'GLOBAL')}"
        month = datetime.fromtimestamp(order["entry_time"] / 1000, timezone.utc).strftime("%Y-%m")
        by_segment_month.setdefault((segment, month), []).append(order)

    passed_months: dict[str, set[str]] = {}
    for (segment, month), group in by_segment_month.items():
        edge = _edge(group)
        if edge.sample_size >= min_sample_size and edge.win_rate >= min_win_rate and edge.ev >= min_ev:
            passed_months.setdefault(segment, set()).add(month)
    return {segment for segment, months in passed_months.items() if len(months) >= min_months}


def _edge(orders: Sequence[dict]) -> GeneratedSessionEdge:
    wins = sum(1 for order in orders if order.get("result") == "WIN")
    sample_size = len(orders)
    pnl = sum(float(order.get("pnl", 0.0)) for order in orders)
    return GeneratedSessionEdge(
        sample_size=sample_size,
        win_rate=round(wins / sample_size, 4) if sample_size else 0.0,
        ev=round(pnl / sample_size, 4) if sample_size else 0.0,
    )
