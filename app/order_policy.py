from dataclasses import dataclass
from collections.abc import Mapping, Sequence

from app.models import Kline, Signal, SimulatedOrder
from app.strategy import max_trade_edge_for


@dataclass(frozen=True)
class OrderGate:
    code: str
    open_allowed: bool = False
    signal_key: tuple[int, int, str] | None = None
    decision_trace: tuple[dict[str, object], ...] = ()

    @property
    def first_decisive_block(self) -> str:
        return next(
            (
                str(record["stage"])
                for record in self.decision_trace
                if record["result"] == "BLOCK"
            ),
            "",
        )


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
        trace: list[dict[str, object]] = []
        if not signal.actionable:
            decision = self.wait_decision(signal)
            score_values = {
                "signed_score": signal.score,
                "score_abs": abs(signal.score),
                "threshold": signal.threshold,
                "edge": abs(signal.score) - signal.threshold,
                "session_edge_min": signal.session_edge_min,
                "max_trade_edge": max_trade_edge_for(
                    signal.timeframe_minutes,
                    signal.threshold_segment,
                    signal.direction,
                ),
            }
            if decision == "SESSION_BLOCKED":
                self._append_trace(trace, "SCORE", "PASS", "SCORE_PASSED", score_values)
                self._append_trace(
                    trace,
                    "SESSION",
                    "BLOCK",
                    "SESSION_BLOCKED",
                    {
                        "session_allowed": signal.session_allowed,
                        "score_abs": abs(signal.score),
                        "threshold": signal.threshold,
                    },
                )
            else:
                self._append_trace(trace, "SCORE", "BLOCK", decision, score_values)
            return OrderGate(code=decision, decision_trace=tuple(trace))

        self._append_trace(
            trace,
            "SCORE",
            "PASS",
            "SCORE_PASSED",
            {
                "signed_score": signal.score,
                "score_abs": abs(signal.score),
                "threshold": signal.threshold,
                "edge": abs(signal.score) - signal.threshold,
            },
        )
        self._append_trace(
            trace,
            "SESSION",
            "PASS",
            "SESSION_PASSED",
            {
                "session_allowed": signal.session_allowed,
                "sample_size": signal.session_sample_size,
                "win_rate": signal.session_win_rate,
                "ev": signal.session_ev,
            },
        )

        direction = signal.direction.upper()
        open_orders = [order for order in orders if order.status == "OPEN"]
        open_count = len(open_orders)
        if open_count >= self.max_open_orders:
            self._append_trace(
                trace,
                "CAPACITY",
                "BLOCK",
                "MAX_OPEN_ORDERS",
                {"open_count": open_count, "limit": self.max_open_orders},
            )
            return OrderGate(code="HOLD_OPEN_ORDER", decision_trace=tuple(trace))
        self._append_trace(
            trace,
            "CAPACITY",
            "PASS",
            "CAPACITY_AVAILABLE",
            {"open_count": open_count, "limit": self.max_open_orders},
        )

        direction_last_opened_at = (
            last_order_opened_at.get(direction)
            if isinstance(last_order_opened_at, Mapping)
            else last_order_opened_at
        )
        if direction_last_opened_at is not None:
            gap = latest.close_time - direction_last_opened_at
            if gap < self.min_order_gap_ms:
                self._append_trace(
                    trace,
                    "COOLDOWN",
                    "BLOCK",
                    "DIRECTION_COOLDOWN",
                    {
                        "direction": direction,
                        "last_opened_at": direction_last_opened_at,
                        "candidate_time": latest.close_time,
                        "elapsed_ms": gap,
                        "required_gap_ms": self.min_order_gap_ms,
                    },
                )
                return OrderGate(code="COOLDOWN", decision_trace=tuple(trace))
        self._append_trace(
            trace,
            "COOLDOWN",
            "PASS",
            "DIRECTION_COOLDOWN_CLEAR",
            {
                "direction": direction,
                "last_opened_at": direction_last_opened_at,
                "candidate_time": latest.close_time,
                "required_gap_ms": self.min_order_gap_ms,
            },
        )

        direction_open_count = sum(
            1 for order in open_orders if order.direction.upper() == direction
        )
        direction_limit = (
            self.max_open_long_orders
            if direction == "LONG"
            else self.max_open_short_orders
        )
        if direction_open_count >= direction_limit:
            self._append_trace(
                trace,
                "DIRECTION_CAPACITY",
                "BLOCK",
                "MAX_DIRECTION_OPEN_ORDERS",
                {
                    "direction": direction,
                    "open_count": direction_open_count,
                    "limit": direction_limit,
                },
            )
            return OrderGate(
                code=f"HOLD_{direction}_OPEN_ORDER",
                decision_trace=tuple(trace),
            )
        self._append_trace(
            trace,
            "DIRECTION_CAPACITY",
            "PASS",
            "DIRECTION_CAPACITY_AVAILABLE",
            {
                "direction": direction,
                "open_count": direction_open_count,
                "limit": direction_limit,
            },
        )

        signal_key = (signal.open_time, signal.timeframe_minutes, signal.direction)
        if signal_key in opened_signal_keys:
            self._append_trace(
                trace,
                "DUPLICATE",
                "BLOCK",
                "DUPLICATE_SIGNAL",
                {"signal_key": signal_key},
            )
            return OrderGate(
                code="DUPLICATE_SIGNAL",
                signal_key=signal_key,
                decision_trace=tuple(trace),
            )

        self._append_trace(
            trace,
            "DUPLICATE",
            "PASS",
            "SIGNAL_KEY_AVAILABLE",
            {"signal_key": signal_key},
        )
        self._append_trace(
            trace,
            "MECHANICAL_ADMISSION",
            "PASS",
            "OPENED",
            {"signal_key": signal_key},
        )

        return OrderGate(
            code="OPENED",
            open_allowed=True,
            signal_key=signal_key,
            decision_trace=tuple(trace),
        )

    @staticmethod
    def _append_trace(
        trace: list[dict[str, object]],
        stage: str,
        result: str,
        reason_code: str,
        decisive_values: dict[str, object],
    ) -> None:
        trace.append(
            {
                "stage": stage,
                "result": result,
                "reason_code": reason_code,
                "decisive_values": decisive_values,
            }
        )

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
