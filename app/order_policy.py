from dataclasses import dataclass
from typing import Sequence

from app.models import Kline, Signal, SimulatedOrder
from app.strategy import max_trade_edge_for


@dataclass(frozen=True)
class OrderGate:
    code: str
    open_allowed: bool = False
    risk_pause: str = ""
    signal_key: tuple[int, int, str] | None = None


@dataclass(frozen=True)
class OrderPolicy:
    max_open_orders: int = 2
    min_order_gap_ms: int = 2 * 60_000

    def evaluate(
        self,
        signal: Signal,
        latest: Kline,
        orders: Sequence[SimulatedOrder],
        last_order_opened_at: int | None,
        opened_signal_keys: set[tuple[int, int, str]],
    ) -> OrderGate:
        if not signal.actionable:
            return OrderGate(code=self.wait_decision(signal))

        open_count = sum(1 for order in orders if order.status == "OPEN")
        if open_count >= self.max_open_orders:
            return OrderGate(code="HOLD_OPEN_ORDER")

        if last_order_opened_at is not None:
            gap = latest.close_time - last_order_opened_at
            if gap < self.min_order_gap_ms:
                return OrderGate(code="COOLDOWN")

        signal_key = (signal.open_time, signal.timeframe_minutes, signal.direction)
        if signal_key in opened_signal_keys:
            return OrderGate(code="DUPLICATE_SIGNAL", signal_key=signal_key)

        risk_pause = self.risk_pause_reason(signal, latest, orders)
        if risk_pause:
            return OrderGate(code="RISK_PAUSED", risk_pause=risk_pause, signal_key=signal_key)

        return OrderGate(code="OPENED", open_allowed=True, signal_key=signal_key)

    @staticmethod
    def wait_decision(signal: Signal) -> str:
        if not signal.session_allowed and abs(signal.score) >= signal.threshold:
            return "SESSION_BLOCKED"
        edge = abs(signal.score) - signal.threshold
        if edge >= signal.session_edge_min and edge >= max_trade_edge_for(
            signal.timeframe_minutes, signal.threshold_segment, signal.direction
        ):
            return "OVERHEATED"
        if edge >= 0:
            return "EDGE_TOO_SMALL"
        return "BELOW_THRESHOLD"

    @staticmethod
    def risk_pause_reason(signal: Signal, latest: Kline, orders: Sequence[SimulatedOrder]) -> str:
        day = latest.close_time // 86_400_000
        settled_today = [
            order
            for order in orders
            if order.status == "SETTLED"
            and order.settled_at is not None
            and order.settled_at // 86_400_000 == day
        ]
        segment_orders = [
            order
            for order in settled_today
            if order.threshold_segment == signal.threshold_segment
        ]
        consecutive_losses = 0
        for order in sorted(segment_orders, key=lambda item: item.settled_at or 0, reverse=True):
            if order.result != "LOSS":
                break
            consecutive_losses += 1
        if consecutive_losses >= 3:
            return f"{signal.threshold_segment} 连续亏损 {consecutive_losses} 单，暂停该时段"
        return ""
