from dataclasses import dataclass
from collections.abc import Mapping, Sequence

from app.models import Kline, Signal, SimulatedOrder
from app.strategy import max_trade_edge_for


@dataclass(frozen=True)
class OrderGate:
    code: str
    open_allowed: bool = False
    signal_key: tuple[int, int, str] | None = None


@dataclass(frozen=True)
class OrderPolicy:
    max_open_orders: int = 2
    max_open_long_orders: int = 1
    max_open_short_orders: int = 2
    min_order_gap_ms: int = 2 * 60_000

    def evaluate(
        self,
        signal: Signal,
        latest: Kline,
        orders: Sequence[SimulatedOrder],
        last_order_opened_at: int | None | Mapping[str, int | None],
        opened_signal_keys: set[tuple[int, int, str]],
    ) -> OrderGate:
        if not signal.actionable:
            return OrderGate(code=self.wait_decision(signal))

        direction = signal.direction.upper()
        open_orders = [order for order in orders if order.status == "OPEN"]
        open_count = len(open_orders)
        if open_count >= self.max_open_orders:
            return OrderGate(code="HOLD_OPEN_ORDER")

        direction_last_opened_at = (
            last_order_opened_at.get(direction)
            if isinstance(last_order_opened_at, Mapping)
            else last_order_opened_at
        )
        if direction_last_opened_at is not None:
            gap = latest.close_time - direction_last_opened_at
            if gap < self.min_order_gap_ms:
                return OrderGate(code="COOLDOWN")

        direction_open_count = sum(
            1 for order in open_orders if order.direction.upper() == direction
        )
        direction_limit = (
            self.max_open_long_orders
            if direction == "LONG"
            else self.max_open_short_orders
        )
        if direction_open_count >= direction_limit:
            return OrderGate(code=f"HOLD_{direction}_OPEN_ORDER")

        signal_key = (signal.open_time, signal.timeframe_minutes, signal.direction)
        if signal_key in opened_signal_keys:
            return OrderGate(code="DUPLICATE_SIGNAL", signal_key=signal_key)

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
