from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
import math
from threading import RLock
from typing import Any

from app.models import FearGreedContext, ObservationSignal, SimulatedOrder
from app.profile_admission import ProfileAdmissionPolicy
from app.shadow_models import MarketEvent, ShadowEvaluationMetrics
from app.stake_progression import StakeProgressionCredit
from app.state import MonitorState
from app.wave_state import WaveSnapshot


MINUTE_MS = 60_000
MINUTES_PER_DAY = 24 * 60
SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))
SHADOW_RUNTIME_CHECKPOINT_VERSION = "SHADOW_RUNTIME_CHECKPOINT_V1"


class FrozenFearGreedProvider:
    """Per-arm provider whose value advances only with an accepted market event."""

    def __init__(self, context: FearGreedContext):
        self._lock = RLock()
        self._context = self._validated_copy(context)

    @staticmethod
    def _validated_copy(context: FearGreedContext) -> FearGreedContext:
        if not isinstance(context, FearGreedContext):
            raise TypeError("context must be FearGreedContext")
        return deepcopy(context)

    def update(self, context: FearGreedContext) -> None:
        frozen = self._validated_copy(context)
        with self._lock:
            self._context = frozen

    def get_context(self) -> FearGreedContext:
        with self._lock:
            return deepcopy(self._context)


@dataclass
class _ArmRuntime:
    arm_id: str
    policy: ProfileAdmissionPolicy
    provider: FrozenFearGreedProvider
    clock: list[int]
    state: MonitorState


class ShadowRuntime:
    """Runs isolated, full MonitorState instances against one causal minute stream."""

    def __init__(
        self,
        *,
        symbol: str,
        generation: int,
        arms: Mapping[str, _ArmRuntime],
        seed_cursor_open_time: int | None,
    ) -> None:
        normalized_symbol = str(symbol).strip().upper()
        if not normalized_symbol:
            raise ValueError("symbol must not be empty")
        if type(generation) is not int or generation < 0:
            raise ValueError("generation must be a non-negative integer")
        if not arms:
            raise ValueError("at least one shadow arm is required")

        self.symbol = normalized_symbol
        self.generation = generation
        self._arms = dict(arms)
        self._seed_cursor_open_time = seed_cursor_open_time
        self._cursor_open_time = None
        self._cursor_event_id: str | None = None
        self._event_coverage_by_day: dict[str, dict[str, int]] = {}
        self._invalid = False
        self._invalid_reason = ""
        self._closed = False
        self._lock = RLock()

    @classmethod
    def from_seed(
        cls,
        seed: Mapping[str, object],
        *,
        generation: int,
        policies: Mapping[str, ProfileAdmissionPolicy],
    ) -> ShadowRuntime:
        if not isinstance(seed, Mapping):
            raise TypeError("seed must be a mapping")
        symbol = str(seed.get("symbol", "")).strip().upper()
        if not symbol:
            raise ValueError("seed symbol must not be empty")
        if not isinstance(policies, Mapping) or not policies:
            raise ValueError("policies must contain at least one arm")

        constructor = seed.get("constructor")
        if not isinstance(constructor, Mapping):
            raise ValueError("seed constructor must be a mapping")
        seed_klines = tuple(seed.get("klines") or ())
        seed_observations = tuple(seed.get("observations") or ())
        daily_selection = deepcopy(seed.get("daily_profile_selection"))
        seed_cursor = seed_klines[-1].open_time if seed_klines else None
        evaluated_at = seed_klines[-1].close_time if seed_klines else 0
        initial_fear = cls._seed_fear_greed(seed)

        arms: dict[str, _ArmRuntime] = {}
        initializing_state: MonitorState | None = None
        try:
            for raw_arm_id, policy in policies.items():
                arm_id = str(raw_arm_id).strip()
                if not arm_id:
                    raise ValueError("arm id must not be empty")
                if arm_id in arms:
                    raise ValueError(f"duplicate arm id: {arm_id}")
                if not isinstance(policy, ProfileAdmissionPolicy):
                    raise TypeError("every policy must be ProfileAdmissionPolicy")

                provider = FrozenFearGreedProvider(initial_fear)
                clock = [evaluated_at]
                state_args = deepcopy(dict(constructor))
                state_args.update(
                    {
                        "fear_greed_provider": provider,
                        "storage": None,
                        "storage_path": None,
                        "webhook": None,
                        "now_ms": lambda current=clock: current[0],
                        "profile_admission_policy": policy,
                    }
                )
                initializing_state = MonitorState(symbol, **state_args)
                initializing_state.seed_klines(seed_klines)
                initializing_state.seed_shadow_history(
                    seed_observations,
                    daily_selection,
                    evaluated_at=evaluated_at,
                )
                arms[arm_id] = _ArmRuntime(
                    arm_id=arm_id,
                    policy=policy,
                    provider=provider,
                    clock=clock,
                    state=initializing_state,
                )
                initializing_state = None
        except Exception:
            if initializing_state is not None:
                initializing_state.close()
            for arm in arms.values():
                arm.state.close()
            raise

        return cls(
            symbol=symbol,
            generation=generation,
            arms=arms,
            seed_cursor_open_time=seed_cursor,
        )

    @classmethod
    def from_checkpoint(
        cls,
        seed: Mapping[str, object],
        *,
        checkpoint: Mapping[str, object],
        policies: Mapping[str, ProfileAdmissionPolicy],
    ) -> ShadowRuntime:
        if checkpoint.get("version") != SHADOW_RUNTIME_CHECKPOINT_VERSION:
            raise ValueError("unsupported shadow runtime checkpoint version")
        symbol = str(checkpoint.get("symbol", "")).strip().upper()
        generation = checkpoint.get("generation")
        if type(generation) is not int or generation < 0:
            raise ValueError("checkpoint generation must be a non-negative integer")
        if symbol != str(seed.get("symbol", "")).strip().upper():
            raise ValueError("checkpoint symbol does not match seed")

        cursor_open_time = checkpoint.get("cursor_open_time")
        if cursor_open_time is not None:
            cursor_open_time = int(cursor_open_time)
        seed_klines = tuple(seed.get("klines") or ())
        seed_ahead = bool(
            cursor_open_time is not None
            and seed_klines
            and seed_klines[-1].open_time > cursor_open_time
        )
        if cursor_open_time is not None:
            seed_klines = tuple(
                item for item in seed_klines if item.open_time <= cursor_open_time
            )
        restored_seed = {**dict(seed), "klines": seed_klines}
        runtime = cls.from_seed(
            restored_seed,
            generation=generation,
            policies=policies,
        )
        try:
            arm_payloads = checkpoint.get("arms")
            if not isinstance(arm_payloads, Mapping):
                raise ValueError("checkpoint arms must be a mapping")
            if set(arm_payloads) != set(runtime._arms):
                raise ValueError("checkpoint arms do not match policies")
            evaluated_at = (
                seed_klines[-1].close_time
                if seed_klines
                else int(cursor_open_time or 0) + MINUTE_MS - 1
            )
            for arm_id, arm in runtime._arms.items():
                payload = arm_payloads[arm_id]
                if not isinstance(payload, Mapping):
                    raise ValueError("checkpoint arm must be a mapping")
                stored_policy = ProfileAdmissionPolicy(
                    **deepcopy(dict(payload.get("policy") or {}))
                )
                if stored_policy.policy_hash != arm.policy.policy_hash:
                    raise ValueError(f"checkpoint policy mismatch for arm {arm_id}")
                wave_payload = payload.get("wave")
                if not isinstance(wave_payload, Mapping):
                    raise ValueError("checkpoint arm requires wave state")
                allowed_directions = wave_payload.get("allowed_directions", ())
                wave = WaveSnapshot(
                    **{
                        **deepcopy(dict(wave_payload)),
                        "allowed_directions": tuple(allowed_directions),
                    }
                )
                orders = tuple(
                    SimulatedOrder(**deepcopy(dict(item)))
                    for item in payload.get("orders", ())
                )
                observations = tuple(
                    ObservationSignal(**deepcopy(dict(item)))
                    for item in payload.get("observations", ())
                )
                credits = tuple(
                    StakeProgressionCredit(**deepcopy(dict(item)))
                    for item in payload.get("credits", ())
                )
                arm.state.restore_shadow_execution_state(
                    orders=orders,
                    observations=observations,
                    credits=credits,
                    wave_snapshot=wave,
                    wave_evaluated_at=int(
                        payload.get("wave_evaluated_at", evaluated_at) or 0
                    ),
                    daily_profile_selection=deepcopy(
                        payload.get("daily_profile_selection")
                    ),
                    active_daily_profile_selection=deepcopy(
                        payload.get("active_daily_profile_selection")
                    ),
                    last_order_opened_at=payload.get("last_order_opened_at"),
                    opened_signal_keys=payload.get("opened_signal_keys", ()),
                    evaluated_at=evaluated_at,
                )

            runtime._cursor_open_time = cursor_open_time
            runtime._cursor_event_id = checkpoint.get("cursor_event_id") or None
            raw_days = checkpoint.get("event_coverage_by_day") or {}
            if not isinstance(raw_days, Mapping):
                raise ValueError("event_coverage_by_day must be a mapping")
            runtime._event_coverage_by_day = {
                str(day): {
                    "first": int(coverage["first"]),
                    "last": int(coverage["last"]),
                    "count": int(coverage["count"]),
                }
                for day, coverage in raw_days.items()
            }
            runtime._invalid = bool(checkpoint.get("invalid", False))
            runtime._invalid_reason = str(checkpoint.get("invalid_reason", ""))
            if seed_ahead:
                runtime._freeze("seed advanced beyond committed cursor")
            return runtime
        except Exception:
            runtime.close()
            raise

    @classmethod
    def from_persistence(
        cls,
        seed: Mapping[str, object],
        *,
        arm_id: str,
        policy: ProfileAdmissionPolicy,
        effective_from_ms: int,
        runtime_state: Mapping[str, object],
        orders: list[Mapping[str, object]],
        observations: list[Mapping[str, object]],
    ) -> ShadowRuntime:
        normalized_arm_id = str(arm_id).strip()
        if not normalized_arm_id:
            raise ValueError("arm_id must not be empty")
        if runtime_state.get("version") != SHADOW_RUNTIME_CHECKPOINT_VERSION:
            raise ValueError("unsupported persisted shadow runtime version")
        persisted_arm_id = str(runtime_state.get("arm_id", "")).strip()
        if persisted_arm_id != normalized_arm_id:
            raise ValueError("persisted arm_id does not match requested arm")
        arm_state = runtime_state.get("arm")
        if not isinstance(arm_state, Mapping):
            raise ValueError("persisted runtime requires arm state")
        if type(effective_from_ms) is not int or effective_from_ms < 0:
            raise ValueError("effective_from_ms must be a non-negative integer")
        merged_observations: dict[str, dict[str, object]] = {}
        for item in seed.get("observations") or ():
            payload = (
                item.to_dict()
                if isinstance(item, ObservationSignal)
                else deepcopy(dict(item))
            )
            key = str(payload.get("observation_key", ""))
            if key and int(payload.get("opened_at", 0) or 0) < effective_from_ms:
                merged_observations[key] = payload
        for item in observations:
            payload = deepcopy(dict(item))
            key = str(payload.get("observation_key", ""))
            if key:
                merged_observations[key] = payload
        ordered_observations = sorted(
            merged_observations.values(),
            key=lambda item: (
                int(item.get("opened_at", 0) or 0),
                str(item.get("observation_key", "")),
            ),
        )
        checkpoint = {
            key: deepcopy(runtime_state.get(key))
            for key in (
                "version",
                "symbol",
                "generation",
                "seed_cursor_open_time",
                "cursor_open_time",
                "cursor_event_id",
                "invalid",
                "invalid_reason",
                "event_coverage_by_day",
            )
        }
        checkpoint.update(
            {
                "closed": False,
                "arms": {
                    normalized_arm_id: {
                        **deepcopy(dict(arm_state)),
                        "orders": deepcopy(orders),
                        "observations": ordered_observations,
                    }
                },
            }
        )
        restored_seed = {**dict(seed), "observations": ()}
        return cls.from_checkpoint(
            restored_seed,
            checkpoint=checkpoint,
            policies={normalized_arm_id: policy},
        )

    @staticmethod
    def _seed_fear_greed(seed: Mapping[str, object]) -> FearGreedContext:
        raw = seed.get("fear_greed")
        if isinstance(raw, FearGreedContext):
            return deepcopy(raw)
        if isinstance(raw, Mapping):
            return FearGreedContext(**deepcopy(dict(raw)))
        return FearGreedContext(value=50, classification="Neutral")

    @property
    def cursor_event_id(self) -> str | None:
        with self._lock:
            return self._cursor_event_id

    @property
    def invalid(self) -> bool:
        with self._lock:
            return self._invalid

    @property
    def invalid_reason(self) -> str:
        with self._lock:
            return self._invalid_reason

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def state(self, arm_id: str) -> MonitorState:
        with self._lock:
            return self._arm(arm_id).state

    def orders(self, arm_id: str) -> tuple[SimulatedOrder, ...]:
        with self._lock:
            return tuple(deepcopy(self._arm(arm_id).state.simulator.orders))

    def process(self, event: MarketEvent) -> bool:
        return self.process_batch((event,))

    def build_analysis_frame(self, events) -> dict[str, object]:
        normalized = tuple(events)
        if not normalized:
            raise ValueError("shadow analysis frame requires events")
        with self._lock:
            if self._closed:
                raise RuntimeError("shadow runtime is closed")
            first_arm = next(iter(self._arms.values()))
            existing = tuple(first_arm.state.klines)
        merged = first_arm.state._merge_klines(  # noqa: SLF001 - 影子同源候选帧。
            existing,
            tuple(event.kline for event in normalized),
        )
        fear_greed = deepcopy(normalized[-1].fear_greed)
        from app import state as state_module

        with state_module.reuse_analysis_results():
            return {
                "latest_close_time": int(merged[-1].close_time),
                "new_signals": tuple(
                    state_module.analyze_volume_price(
                        merged,
                        timeframe_minutes=minutes,
                        fear_greed=fear_greed,
                    )
                    for minutes in state_module.LIVE_TRADE_TIMEFRAMES
                ),
                "observation_signals": tuple(
                    signal
                    for minutes in state_module.LIVE_TRADE_TIMEFRAMES
                    for signal in state_module.analyze_observation_signals(
                        merged,
                        timeframe_minutes=minutes,
                        fear_greed=fear_greed,
                    )
                ),
                "selected_signal": state_module.choose_trade_signal(
                    merged,
                    fear_greed=fear_greed,
                ),
            }

    def process_batch(self, events, *, analysis_frame=None) -> bool:
        normalized = tuple(events)
        if not normalized:
            return False
        for event in normalized:
            if not isinstance(event, MarketEvent):
                raise TypeError("every batch item must be MarketEvent")
            if event.symbol != self.symbol:
                raise ValueError(
                    f"event symbol {event.symbol} does not match runtime symbol {self.symbol}"
                )
            if event.generation != self.generation:
                raise ValueError(
                    f"event generation {event.generation} does not match runtime generation "
                    f"{self.generation}"
                )
        for previous, current in zip(normalized, normalized[1:]):
            if current.kline.open_time != previous.kline.open_time + MINUTE_MS:
                raise ValueError("shadow event batch must contain contiguous minutes")
        if analysis_frame is None:
            analysis_frame = self.build_analysis_frame(normalized)

        with self._lock:
            if self._closed:
                raise RuntimeError("shadow runtime is closed")
            if self._invalid:
                return False
            if (
                normalized[-1].event_id == self._cursor_event_id
                or (
                    self._cursor_open_time is not None
                    and normalized[-1].kline.open_time <= self._cursor_open_time
                )
            ):
                return False

            expected_open_time = (
                normalized[0].kline.open_time
                if self._cursor_open_time is None
                else self._cursor_open_time + MINUTE_MS
            )
            if normalized[0].kline.open_time != expected_open_time:
                self._freeze(
                    "minute gap: expected open_time "
                    f"{expected_open_time}, received {normalized[0].kline.open_time}"
                )
                return False

            final_event = normalized[-1]
            batch_klines = tuple(event.kline for event in normalized)
            for arm in self._arms.values():
                arm.clock[0] = final_event.kline.close_time
                arm.provider.update(final_event.fear_greed)
                try:
                    processed = arm.state.update_from_klines(
                        batch_klines,
                        expected_context=(self.symbol, 0),
                        _shadow_analysis_frame=analysis_frame,
                    )
                except Exception as exc:  # noqa: BLE001 - one failed arm invalidates the generation.
                    self._freeze(f"arm {arm.arm_id} failed: {exc}")
                    return False
                if not processed:
                    self._freeze(f"arm {arm.arm_id} rejected a valid market event")
                    return False

            self._cursor_open_time = final_event.kline.open_time
            self._cursor_event_id = final_event.event_id
            for event in normalized:
                day_key, minute_of_day = self._calendar_minute(
                    event.kline.open_time
                )
                coverage = self._event_coverage_by_day.setdefault(
                    day_key,
                    {"first": minute_of_day, "last": minute_of_day, "count": 0},
                )
                coverage["first"] = min(coverage["first"], minute_of_day)
                coverage["last"] = max(coverage["last"], minute_of_day)
                coverage["count"] += 1
            return True

    def _freeze(self, reason: str) -> None:
        self._invalid = True
        self._invalid_reason = str(reason)

    def freeze(self, reason: str) -> None:
        with self._lock:
            if self._closed:
                return
            self._freeze(reason)

    @staticmethod
    def _calendar_minute(open_time: int) -> tuple[str, int]:
        local = datetime.fromtimestamp(open_time / 1000, SHANGHAI_TIMEZONE)
        return local.date().isoformat(), local.hour * 60 + local.minute

    def daily_statistics(self, arm_id: str) -> tuple[dict[str, object], ...]:
        with self._lock:
            arm = self._arm(arm_id)
            complete_days = {
                day
                for day, coverage in self._event_coverage_by_day.items()
                if coverage["count"] == MINUTES_PER_DAY
                and coverage["first"] == 0
                and coverage["last"] == MINUTES_PER_DAY - 1
            }
            settled = [
                order
                for order in arm.state.simulator.orders
                if order.status == "SETTLED" and order.settled_at is not None
            ]
            rows = []
            for day in sorted(complete_days):
                day_orders = [
                    order
                    for order in settled
                    if self._calendar_minute(int(order.settled_at))[0] == day
                ]
                wins = sum(order.result == "WIN" for order in day_orders)
                pnl = round(sum(order.pnl for order in day_orders), 4)
                long_orders = [order for order in day_orders if order.direction == "LONG"]
                short_orders = [order for order in day_orders if order.direction == "SHORT"]
                rows.append(
                    {
                        "day": day,
                        "orders": len(day_orders),
                        "wins": wins,
                        "losses": len(day_orders) - wins,
                        "win_rate": wins / len(day_orders) if day_orders else 0.0,
                        "pnl": pnl,
                        "ev": pnl / len(day_orders) if day_orders else 0.0,
                        "long_orders": len(long_orders),
                        "long_wins": sum(order.result == "WIN" for order in long_orders),
                        "short_orders": len(short_orders),
                        "short_wins": sum(order.result == "WIN" for order in short_orders),
                    }
                )
            return tuple(rows)

    def evaluation_metrics(self, arm_id: str) -> ShadowEvaluationMetrics:
        daily = self.daily_statistics(arm_id)
        if not daily:
            return ShadowEvaluationMetrics()

        settled_orders = sum(int(row["orders"]) for row in daily)
        wins = sum(int(row["wins"]) for row in daily)
        long_orders = sum(int(row["long_orders"]) for row in daily)
        long_wins = sum(int(row["long_wins"]) for row in daily)
        short_orders = sum(int(row["short_orders"]) for row in daily)
        short_wins = sum(int(row["short_wins"]) for row in daily)
        total_pnl = round(sum(float(row["pnl"]) for row in daily), 4)
        rolling_rates = []
        for index in range(max(0, len(daily) - 2)):
            window = daily[index : index + 3]
            orders = sum(int(row["orders"]) for row in window)
            window_wins = sum(int(row["wins"]) for row in window)
            rolling_rates.append(window_wins / orders if orders else 0.0)

        completed_days = {str(row["day"]) for row in daily}
        arm_orders = [
            order
            for order in self.orders(arm_id)
            if order.status == "SETTLED"
            and order.settled_at is not None
            and self._calendar_minute(int(order.settled_at))[0] in completed_days
        ]
        max_drawdown, max_loss_streak, current_loss_streak = self._risk_metrics(arm_orders)
        return ShadowEvaluationMetrics(
            complete_days=len(daily),
            settled_orders=settled_orders,
            wins=wins,
            long_orders=long_orders,
            long_wins=long_wins,
            short_orders=short_orders,
            short_wins=short_wins,
            qualified_win_rate_days=sum(float(row["win_rate"]) >= 0.5556 for row in daily),
            positive_ev_days=sum(float(row["ev"]) > 0.0 for row in daily),
            days_beating_champion=0,
            average_orders_per_day=settled_orders / len(daily),
            worst_rolling_3d_win_rate=min(rolling_rates) if rolling_rates else 0.0,
            total_ev=total_pnl / settled_orders if settled_orders else 0.0,
            total_pnl=total_pnl,
            max_drawdown=max_drawdown,
            max_loss_streak=max_loss_streak,
            current_loss_streak=current_loss_streak,
        )

    def metrics_since(
        self,
        arm_id: str,
        since_ms: int,
    ) -> ShadowEvaluationMetrics:
        settled = [
            order
            for order in self.orders(arm_id)
            if order.status == "SETTLED"
            and order.settled_at is not None
            and int(order.opened_at) >= int(since_ms)
        ]
        if not settled:
            return ShadowEvaluationMetrics()
        ordered = sorted(
            settled,
            key=lambda item: (int(item.settled_at or 0), item.id),
        )
        wins = sum(order.result == "WIN" for order in ordered)
        long_orders = [order for order in ordered if order.direction == "LONG"]
        short_orders = [order for order in ordered if order.direction == "SHORT"]
        day_rows: dict[str, list[SimulatedOrder]] = {}
        for order in ordered:
            day = self._calendar_minute(int(order.settled_at))[0]
            day_rows.setdefault(day, []).append(order)
        daily_win_rates = [
            sum(order.result == "WIN" for order in orders) / len(orders)
            for orders in day_rows.values()
        ]
        daily_ev = [
            sum(float(order.pnl) for order in orders) / len(orders)
            for orders in day_rows.values()
        ]
        rolling_rates = []
        ordered_days = [day_rows[day] for day in sorted(day_rows)]
        for index in range(max(0, len(ordered_days) - 2)):
            window = [order for day in ordered_days[index : index + 3] for order in day]
            rolling_rates.append(
                sum(order.result == "WIN" for order in window) / len(window)
            )
        total_pnl = round(sum(float(order.pnl) for order in ordered), 4)
        max_drawdown, max_loss_streak, current_loss_streak = self._risk_metrics(ordered)
        return ShadowEvaluationMetrics(
            complete_days=len(day_rows),
            settled_orders=len(ordered),
            wins=wins,
            long_orders=len(long_orders),
            long_wins=sum(order.result == "WIN" for order in long_orders),
            short_orders=len(short_orders),
            short_wins=sum(order.result == "WIN" for order in short_orders),
            qualified_win_rate_days=sum(rate >= 0.5556 for rate in daily_win_rates),
            positive_ev_days=sum(value > 0.0 for value in daily_ev),
            average_orders_per_day=len(ordered) / len(day_rows),
            worst_rolling_3d_win_rate=(
                min(rolling_rates) if rolling_rates else min(daily_win_rates)
            ),
            total_ev=total_pnl / len(ordered),
            total_pnl=total_pnl,
            max_drawdown=max_drawdown,
            max_loss_streak=max_loss_streak,
            current_loss_streak=current_loss_streak,
        )

    @staticmethod
    def _risk_metrics(orders: list[SimulatedOrder]) -> tuple[float, int, int]:
        ordered = sorted(orders, key=lambda item: (int(item.settled_at or 0), item.id))
        equity = 0.0
        peak = 0.0
        max_drawdown = 0.0
        streak = 0
        max_streak = 0
        for order in ordered:
            equity += float(order.pnl)
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)
            if order.result == "LOSS":
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0
        return round(max_drawdown, 4), max_streak, streak

    def checkpoint(self) -> dict[str, object]:
        with self._lock:
            return {
                "version": SHADOW_RUNTIME_CHECKPOINT_VERSION,
                "symbol": self.symbol,
                "generation": self.generation,
                "seed_cursor_open_time": self._seed_cursor_open_time,
                "cursor_open_time": self._cursor_open_time,
                "cursor_event_id": self._cursor_event_id,
                "invalid": self._invalid,
                "invalid_reason": self._invalid_reason,
                "closed": self._closed,
                "event_coverage_by_day": {
                    day: dict(coverage)
                    for day, coverage in sorted(self._event_coverage_by_day.items())
                },
                "arms": {
                    arm_id: self._arm_checkpoint(arm)
                    for arm_id, arm in sorted(self._arms.items())
                },
            }

    def persistence_state(self, arm_id: str) -> dict[str, object]:
        with self._lock:
            arm = self._arm(arm_id)
            arm_state = self._arm_checkpoint(arm)
            for key in (
                "orders",
                "observations",
                "adaptive_profile_states",
                "direction_pulse_shadow",
                "balance",
            ):
                arm_state.pop(key, None)
            return {
                "version": SHADOW_RUNTIME_CHECKPOINT_VERSION,
                "symbol": self.symbol,
                "generation": self.generation,
                "seed_cursor_open_time": self._seed_cursor_open_time,
                "cursor_open_time": self._cursor_open_time,
                "cursor_event_id": self._cursor_event_id,
                "invalid": self._invalid,
                "invalid_reason": self._invalid_reason,
                "event_coverage_by_day": {
                    day: dict(coverage)
                    for day, coverage in sorted(
                        self._event_coverage_by_day.items()
                    )
                },
                "arm_id": arm.arm_id,
                "arm": arm_state,
            }

    def compact_settled_observations(
        self,
        arm_id: str,
        observation_keys: set[str],
    ) -> int:
        with self._lock:
            return self._arm(arm_id).state.compact_settled_observations(
                observation_keys
            )

    def _arm_checkpoint(self, arm: _ArmRuntime) -> dict[str, object]:
        state = arm.state
        return _jsonable(
            {
                "policy": arm.policy.to_dict(),
                "orders": state.simulator.orders,
                "observations": state.observations,
                "credits": state.simulator.stake_progression.credits,
                "wave": state.wave_state,
                "wave_evaluated_at": state._wave_evaluated_at,
                "daily_profile_selection": state.daily_profile_selection,
                "active_daily_profile_selection": state.active_daily_profile_selection,
                "adaptive_profile_states": state.adaptive_profile_states,
                "direction_pulse_shadow": state.direction_pulse_shadow,
                "last_order_opened_at": state._last_order_opened_at,
                "opened_signal_keys": sorted(state._opened_signal_keys),
                "balance": state.simulator.balance,
            }
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            arms = tuple(self._arms.values())
        failures = []
        for arm in arms:
            try:
                arm.state.close()
            except Exception as exc:  # noqa: BLE001 - close every independent state.
                failures.append(exc)
        if failures:
            raise failures[0]

    def stop(self) -> None:
        self.close()

    def _arm(self, arm_id: str) -> _ArmRuntime:
        normalized = str(arm_id).strip()
        try:
            return self._arms[normalized]
        except KeyError as exc:
            raise KeyError(f"unknown shadow arm: {normalized}") from exc


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("checkpoint contains a non-finite number")
        return value
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _jsonable(to_dict())
    raise TypeError(f"checkpoint contains unsupported value {type(value).__name__}")
