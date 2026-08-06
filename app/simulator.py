from bisect import bisect_left
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from app.models import Kline, Signal, SimulatedOrder
from app.stake_progression import (
    TWO_STAGE_VERSION,
    OrderTerms,
    StakeProgressionCredit,
    TwoStageStakeProgression,
)


@dataclass(frozen=True)
class SettlementEvent:
    order: SimulatedOrder
    progression_credit: StakeProgressionCredit | None


class AccountSimulator:
    def __init__(
        self,
        stake: float = 10.0,
        win_return: float = 18.0,
        orders: list[SimulatedOrder] | None = None,
        enable_stake_progression: bool = False,
        stake_progression_max_orders: int = 2,
        stake_progression_base_only_segments: list[str] | tuple[str, ...] | set[str] | None = None,
        stake_progression_max_active: int = 1,
        max_open_orders: int = 2,
        stake_progression_activated_at: int = 0,
        stake_progression_credits: Iterable[StakeProgressionCredit] = (),
        active_second_order_ids: Iterable[int] | None = None,
    ):
        self.stake = stake
        self.win_return = win_return
        self.orders: list[SimulatedOrder] = list(orders or [])
        self.enable_stake_progression = enable_stake_progression
        self.stake_progression_max_orders = 2
        self.stake_progression_base_only_segments: set[str] = set()
        self._ignored_stake_progression_max_orders = max(
            1,
            int(stake_progression_max_orders),
        )
        self._ignored_stake_progression_base_only_segments = tuple(
            str(item).strip().upper()
            for item in (stake_progression_base_only_segments or [])
            if str(item).strip()
        )
        self.balance = round(sum(order.pnl for order in self.orders if order.status == "SETTLED"), 4)
        self._next_id = max([order.id for order in self.orders], default=0) + 1

        restored_open_second_ids = {
            order.id
            for order in self.orders
            if order.status == "OPEN"
            and order.stake_progression_step == 2
            and order.stake_progression_version == TWO_STAGE_VERSION
        }
        if active_second_order_ids is None:
            restored_active_ids = sorted(restored_open_second_ids)
        else:
            restored_active_ids = list(active_second_order_ids)
            try:
                explicit_active_ids = {int(order_id) for order_id in restored_active_ids}
            except (TypeError, ValueError) as error:
                raise ValueError("invalid active_second_order_ids") from error
            if explicit_active_ids != restored_open_second_ids:
                raise ValueError(
                    "active_second_order_ids must exactly match restored OPEN "
                    "current-version second-stage orders"
                )
        self.stake_progression = TwoStageStakeProgression(
            base_stake=self.stake,
            base_win_return=self.win_return,
            enabled=self.enable_stake_progression,
            max_active=stake_progression_max_active,
            max_open_orders=max_open_orders,
            activated_at=stake_progression_activated_at,
            credits=stake_progression_credits,
            active_second_order_ids=restored_active_ids,
        )
        self.stake_progression_credits = self.stake_progression.credits

    def open_order(self, signal: Signal, entry_price: float, opened_at: int) -> SimulatedOrder:
        order, _ = self.open_order_with_credit(signal, entry_price, opened_at)
        return order

    def open_order_with_credit(
        self,
        signal: Signal,
        entry_price: float,
        opened_at: int,
        *,
        allow_progression: bool = True,
    ) -> tuple[SimulatedOrder, StakeProgressionCredit | None]:
        normalized_opened_at = int(opened_at)
        if normalized_opened_at < 0:
            raise ValueError("opened_at must be >= 0")
        normalized_timeframe = int(signal.timeframe_minutes)
        expires_at = normalized_opened_at + normalized_timeframe * 60_000
        order_fields = {
            "direction": signal.direction,
            "timeframe_minutes": normalized_timeframe,
            "level": signal.level,
            "reason": signal.reason,
            "entry_price": float(entry_price),
            "opened_at": normalized_opened_at,
            "expires_at": expires_at,
            "threshold_segment": signal.threshold_segment,
            "score": signal.score,
            "threshold": signal.threshold,
            "session_allowed": signal.session_allowed,
            "session_sample_size": signal.session_sample_size,
            "session_win_rate": signal.session_win_rate,
            "session_ev": signal.session_ev,
            "session_edge_min": signal.session_edge_min,
            "regime": signal.regime,
            "strategy_family": signal.strategy_family,
            "strategy_tag": signal.strategy_tag,
            "profile_key": signal.profile_key,
            "daily_profile_selected": signal.daily_profile_selected,
            "daily_profile_version": signal.daily_profile_version,
            "wave_state": signal.wave_state,
            "wave_raw_state": signal.wave_raw_state,
            "wave_window": signal.wave_window,
            "wave_efficiency": signal.wave_efficiency,
            "wave_direction_ratio": signal.wave_direction_ratio,
            "wave_atr_strength": signal.wave_atr_strength,
            "wave_confirmations": signal.wave_confirmations,
            "wave_confirmed_at": signal.wave_confirmed_at,
            "wave_batch_id": signal.wave_batch_id,
            "wave_guard_mode": signal.wave_guard_mode,
            "wave_guard_status": signal.wave_guard_status,
            "wave_guard_reason": signal.wave_guard_reason,
        }
        if allow_progression:
            terms, credit = self.stake_progression.assign(
                self._next_id,
                normalized_opened_at,
            )
        else:
            terms = OrderTerms(
                stake=self.stake,
                win_return=self.win_return,
                step=1,
                source_order_id=None,
            )
            credit = None
        try:
            order = SimulatedOrder(
                id=self._next_id,
                **order_fields,
                stake=terms.stake,
                win_return=terms.win_return,
                stake_progression_step=terms.step,
                stake_progression_source_order_id=terms.source_order_id,
                stake_progression_version=(
                    TWO_STAGE_VERSION if self.enable_stake_progression else ""
                ),
            )
        except Exception:
            if allow_progression:
                self.stake_progression.rollback_assignment(self._next_id)
            raise
        self._next_id += 1
        self.orders.append(order)
        return order, credit

    def rollback_open_order(self, order_id: int) -> SimulatedOrder:
        normalized_order_id = int(order_id)
        if not self.orders:
            raise ValueError("cannot rollback an order when no orders exist")
        order = self.orders[-1]
        if order.id != normalized_order_id or order.id != self._next_id - 1:
            raise ValueError("only the latest created order can be rolled back")
        if order.status != "OPEN":
            raise ValueError("only an OPEN order can be rolled back")

        self.orders.pop()
        self._next_id = order.id
        self.stake_progression.rollback_assignment(order.id)
        return order

    def settle_expired_orders(self, current_time: int, current_price: float) -> list[SimulatedOrder]:
        return [
            event.order
            for event in self.settle_expired_order_events(current_time, current_price)
        ]

    def settle_expired_order_events(
        self,
        current_time: int,
        current_price: float,
    ) -> list[SettlementEvent]:
        events = []
        for order in self.orders:
            if order.status != "OPEN" or current_time != order.expires_at:
                continue
            events.append(self._settle_order(order, current_time, current_price))
        return events

    def settle_expired_orders_from_klines(self, klines: Sequence[Kline]) -> list[SimulatedOrder]:
        return [
            event.order
            for event in self.settle_expired_order_events_from_klines(klines)
        ]

    def settle_expired_order_events_from_klines(
        self,
        klines: Sequence[Kline],
    ) -> list[SettlementEvent]:
        ordered = sorted(klines, key=lambda item: item.close_time)
        close_times = [item.close_time for item in ordered]
        events = []
        for order in self.orders:
            if order.status != "OPEN":
                continue
            index = bisect_left(close_times, order.expires_at)
            if index >= len(ordered):
                continue
            exit_kline = ordered[index]
            if exit_kline.close_time != order.expires_at:
                continue
            events.append(
                self._settle_order(
                    order,
                    exit_kline.close_time,
                    exit_kline.close,
                )
            )
        return events

    def _settle_order(
        self,
        order: SimulatedOrder,
        settled_at: int,
        exit_price: float,
    ) -> SettlementEvent:
        won = self._is_win(order.direction, order.entry_price, exit_price)
        result = "WIN" if won else "LOSS"
        pnl = (
            round(order.win_return - order.stake, 4)
            if won
            else round(-order.stake, 4)
        )
        balance = round(self.balance + pnl, 4)
        credit = self.stake_progression.settle(
            order.id,
            order.opened_at,
            order.stake_progression_step,
            result,
            settled_at,
            allow_credit=order.wave_guard_mode != "RECOVERY",
        )

        order.status = "SETTLED"
        order.result = result
        order.exit_price = exit_price
        order.settled_at = settled_at
        order.pnl = pnl
        self.balance = balance
        return SettlementEvent(
            order=replace(order),
            progression_credit=replace(credit) if credit is not None else None,
        )

    def stats(self) -> dict:
        settled = [order for order in self.orders if order.status == "SETTLED"]
        wins = [order for order in settled if order.result == "WIN"]
        losses = [order for order in settled if order.result == "LOSS"]
        progression = self.stake_progression.status()
        stats = {
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
        }
        stats.update(progression)
        stats.update(
            {
                "stake_progression_enabled": progression["enabled"],
                "stake_progression_max_orders": progression["max_orders"],
                "stake_progression_max_active": progression["max_active"],
                "stake_progression_base_only_segments": [],
            }
        )
        return stats

    @staticmethod
    def _is_win(direction: str, entry_price: float, current_price: float) -> bool:
        if direction == "LONG":
            return current_price > entry_price
        if direction == "SHORT":
            return current_price < entry_price
        return False
