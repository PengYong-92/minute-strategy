from app.models import Signal, SimulatedOrder


class AccountSimulator:
    def __init__(
        self,
        stake: float = 10.0,
        win_return: float = 18.0,
        orders: list[SimulatedOrder] | None = None,
        enable_stake_progression: bool = False,
        stake_progression_max_orders: int = 3,
    ):
        self.stake = stake
        self.win_return = win_return
        self.orders: list[SimulatedOrder] = list(orders or [])
        self.enable_stake_progression = enable_stake_progression
        self.stake_progression_max_orders = max(1, int(stake_progression_max_orders))
        self.balance = round(sum(order.pnl for order in self.orders if order.status == "SETTLED"), 4)
        self._next_id = max([order.id for order in self.orders], default=0) + 1

    def open_order(self, signal: Signal, entry_price: float, opened_at: int) -> SimulatedOrder:
        stake, win_return, progression_step = self._next_order_terms()
        order = SimulatedOrder(
            id=self._next_id,
            direction=signal.direction,
            timeframe_minutes=signal.timeframe_minutes,
            level=signal.level,
            reason=signal.reason,
            entry_price=entry_price,
            opened_at=opened_at,
            expires_at=opened_at + signal.timeframe_minutes * 60_000,
            threshold_segment=signal.threshold_segment,
            score=signal.score,
            threshold=signal.threshold,
            session_allowed=signal.session_allowed,
            session_sample_size=signal.session_sample_size,
            session_win_rate=signal.session_win_rate,
            session_ev=signal.session_ev,
            session_edge_min=signal.session_edge_min,
            regime=signal.regime,
            stake=stake,
            win_return=win_return,
            stake_progression_step=progression_step,
        )
        self._next_id += 1
        self.orders.append(order)
        return order

    def settle_expired_orders(self, current_time: int, current_price: float) -> list[SimulatedOrder]:
        settled = []
        for order in self.orders:
            if order.status != "OPEN" or current_time < order.expires_at:
                continue

            won = self._is_win(order.direction, order.entry_price, current_price)
            order.status = "SETTLED"
            order.result = "WIN" if won else "LOSS"
            order.exit_price = current_price
            order.settled_at = current_time
            order.pnl = round(order.win_return - order.stake, 4) if won else round(-order.stake, 4)
            self.balance = round(self.balance + order.pnl, 4)
            settled.append(order)
        return settled

    def stats(self) -> dict:
        settled = [order for order in self.orders if order.status == "SETTLED"]
        wins = [order for order in settled if order.result == "WIN"]
        losses = [order for order in settled if order.result == "LOSS"]
        return {
            "balance": round(self.balance, 4),
            "total_orders": len(self.orders),
            "open_orders": len([order for order in self.orders if order.status == "OPEN"]),
            "settled_orders": len(settled),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(settled), 4) if settled else 0.0,
            "stake": self.stake,
            "win_return": self.win_return,
            "win_net": round(self.win_return - self.stake, 4),
            "loss_net": -self.stake,
            "next_stake": self._next_order_terms()[0],
            "next_win_return": self._next_order_terms()[1],
            "stake_progression_enabled": self.enable_stake_progression,
            "stake_progression_max_orders": self.stake_progression_max_orders,
        }

    def _next_order_terms(self) -> tuple[float, float, int]:
        if not self.enable_stake_progression:
            return round(self.stake, 4), round(self.win_return, 4), 1

        next_stake = float(self.stake)
        step = 1
        for order in sorted(
            (item for item in self.orders if item.status == "SETTLED" and item.result in {"WIN", "LOSS"}),
            key=lambda item: ((item.settled_at or item.opened_at), item.id),
        ):
            if order.result != "WIN":
                next_stake = float(self.stake)
                step = 1
                continue
            if step >= self.stake_progression_max_orders:
                next_stake = float(self.stake)
                step = 1
            else:
                next_stake = float(order.win_return)
                step += 1

        win_return = self._win_return_for_stake(next_stake)
        return round(next_stake, 4), win_return, step

    def _win_return_for_stake(self, stake: float) -> float:
        if self.stake <= 0:
            return round(self.win_return, 4)
        return round(stake * (self.win_return / self.stake), 4)

    @staticmethod
    def _is_win(direction: str, entry_price: float, current_price: float) -> bool:
        if direction == "LONG":
            return current_price > entry_price
        if direction == "SHORT":
            return current_price < entry_price
        return False
