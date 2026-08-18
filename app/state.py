import hashlib
import threading
import time
from bisect import bisect_left
from copy import deepcopy
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, fields, replace
from pathlib import Path
from typing import Sequence

from app.decision_context import DecisionContextBuilder, runtime_config_snapshot

from app.daily_profile_selector import (
    DailyProfileSelectorConfig,
    build_daily_selection,
    profile_key as daily_profile_key,
    selection_window,
)
from app.direction_pulse_shadow import (
    attach_candidate_shadow,
    empty_direction_pulse_shadow,
    evaluate_direction_pulse_shadow,
)
from app.models import FearGreedContext, Kline, ObservationSignal, Signal
from app.order_policy import OrderGate, OrderPolicy
from app.order_profile import profile_guard_shadow, summarize_order_samples_with_guard
from app.profile_degradation_guard import (
    ProfileDegradationGuardConfig,
    ProfileDegradationGuardDecision,
    evaluate_profile_degradation_guard,
)
from app.profile_health_guard import (
    EVALUATION_INTERVAL_HOURS,
    HEALTHY_MIN_WIN_RATE,
    LOOKBACK_HOURS,
    MIN_SAMPLES,
    WATCH_MIN_WIN_RATE,
    ProfileHealthGuardConfig,
    ProfileHealthGuardDecision,
    evaluate_profile_health_guard,
)
from app.quality_score import attach_shadow_quality_score
from app.result_sequence_guard import (
    ResultSequenceGuardConfig,
    ResultSequenceGuardDecision,
    evaluate_result_sequence_guard,
)
from app.rolling_edge import RollingEdgeConfig, RollingEdgeSnapshot, rolling_edge_snapshot, should_degrade
from app.simulator import AccountSimulator, SettlementEvent
from app.stake_progression import TWO_STAGE_VERSION
from app.storage import (
    DecisionAudit,
    SQLiteMonitorStore,
    page_observation_list,
    page_order_list,
    summarize_observations,
)
from app.time_period_guard import (
    TimePeriodGuardConfig,
    evaluate_time_period_guard,
)
from app.strategy import (
    LIVE_TRADE_TIMEFRAMES,
    analyze_observation_signals,
    analyze_volume_price,
    choose_trade_signal,
    max_trade_edge_for,
)
from app.wave_state import WaveSnapshot, advance_wave, analyze_wave
from app.wave_batch_guard import (
    WaveBatchGuardConfig,
    WaveBatchGuardDecision,
    evaluate_wave_batch_guard,
)


DAY_MS = 86_400_000
REALTIME_PRICE_STALE_MS = 5_000
TRANSITIONAL_DECISION_BUILD_ID = "TASK7_TRANSITIONAL_V1"

_STRATEGY_SOURCE_NAMES = (
    "daily_profile_selector.py",
    "decision_context.py",
    "direction_pulse_shadow.py",
    "models.py",
    "order_policy.py",
    "order_profile.py",
    "profile_degradation_guard.py",
    "profile_health_guard.py",
    "result_sequence_guard.py",
    "rolling_edge.py",
    "simulator.py",
    "stake_progression.py",
    "state.py",
    "strategy.py",
    "time_period_guard.py",
    "wave_batch_guard.py",
    "wave_state.py",
)


def strategy_source_build_id(paths: Sequence[Path] | None = None) -> str:
    source_paths = tuple(paths) if paths is not None else tuple(
        Path(__file__).resolve().parent / name for name in _STRATEGY_SOURCE_NAMES
    )
    digest = hashlib.sha256()
    for path in sorted(source_paths, key=lambda item: item.name):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"minute-strategy-src-{digest.hexdigest()[:16]}"


DEFAULT_STRATEGY_BUILD_ID = strategy_source_build_id()


class MonitorState:
    def __init__(
        self,
        symbol: str,
        max_open_orders: int = 2,
        max_open_long_orders: int = 1,
        max_open_short_orders: int = 2,
        min_order_gap_ms: int = 2 * 60_000,
        fear_greed_provider=None,
        max_klines: int = 140_000,
        storage_path: str | Path | None = None,
        storage: SQLiteMonitorStore | None = None,
        webhook=None,
        rolling_edge_config: RollingEdgeConfig | None = None,
        enable_rolling_edge_guard: bool = True,
        result_sequence_guard_config: ResultSequenceGuardConfig | None = None,
        stake: float = 10.0,
        win_return: float = 18.0,
        enable_stake_progression: bool = True,
        stake_progression_max_orders: int = 2,
        stake_progression_base_only_segments: Sequence[str] | None = None,
        stake_progression_max_active: int = 1,
        enable_profile_guard: bool = False,
        profile_guard_min_history: int = 15,
        profile_guard_min_group_size: int = 2,
        enable_observation_profile_promotion: bool = True,
        observation_profile_lookback_days: int = 7,
        observation_profile_min_samples: int = 12,
        observation_profile_min_win_rate: float = 0.72,
        observation_profile_min_ev: float = 4.0,
        observation_profile_min_edge: float = 10.0,
        live_short_segments: Sequence[str] | None = ("WD-02", "WD-23"),
        enable_daily_profile_selector: bool = False,
        daily_profile_selector_config: DailyProfileSelectorConfig | None = None,
        trade_score_threshold: float | None = None,
        enable_wave_guard: bool = False,
        wave_batch_guard_config: WaveBatchGuardConfig | None = None,
        now_ms=None,
        profile_degradation_guard_config: ProfileDegradationGuardConfig | None = None,
        time_period_guard_config: TimePeriodGuardConfig | None = None,
        profile_health_guard_config: ProfileHealthGuardConfig | None = None,
        strategy_build_id: str = DEFAULT_STRATEGY_BUILD_ID,
    ):
        self.symbol = symbol.upper()
        self._symbol_generation = 0
        self.order_policy = OrderPolicy(
            max_open_orders=max(1, int(max_open_orders)),
            max_open_long_orders=max(1, int(max_open_long_orders)),
            max_open_short_orders=max(1, int(max_open_short_orders)),
            min_order_gap_ms=max(0, int(min_order_gap_ms)),
        )
        self.max_klines = max_klines
        self.storage = storage or (SQLiteMonitorStore(storage_path) if storage_path else None)
        self.strategy_build_id = str(strategy_build_id).strip()
        if not self.strategy_build_id:
            raise ValueError("strategy_build_id must not be empty")
        self.webhook = webhook
        self.rolling_edge_config = rolling_edge_config or RollingEdgeConfig()
        self.enable_rolling_edge_guard = enable_rolling_edge_guard
        self.result_sequence_guard_config = (
            result_sequence_guard_config or ResultSequenceGuardConfig()
        ).normalized()
        self.profile_degradation_guard_config = (
            profile_degradation_guard_config or ProfileDegradationGuardConfig()
        ).normalized()
        self.time_period_guard_config = time_period_guard_config or TimePeriodGuardConfig()
        self.profile_health_guard_config = (
            profile_health_guard_config or ProfileHealthGuardConfig()
        )
        self.stake = stake
        self.win_return = win_return
        self.enable_stake_progression = enable_stake_progression
        self.stake_progression_max_orders = 2
        self.stake_progression_max_active = max(1, int(stake_progression_max_active))
        self._ignored_stake_progression_max_orders = stake_progression_max_orders
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self.stake_progression_base_only_segments = tuple(
            item.strip().upper()
            for item in (stake_progression_base_only_segments or [])
            if str(item).strip()
        )
        self.enable_profile_guard = enable_profile_guard
        self.profile_guard_min_history = max(1, int(profile_guard_min_history))
        self.profile_guard_min_group_size = max(1, int(profile_guard_min_group_size))
        self.enable_observation_profile_promotion = enable_observation_profile_promotion
        self.observation_profile_lookback_days = max(1, int(observation_profile_lookback_days))
        self.observation_profile_min_samples = max(1, int(observation_profile_min_samples))
        self.observation_profile_min_win_rate = min(1.0, max(0.0, float(observation_profile_min_win_rate)))
        self.observation_profile_min_ev = float(observation_profile_min_ev)
        self.observation_profile_min_edge = max(0.0, float(observation_profile_min_edge))
        self.live_short_segments = {
            str(item).strip().upper()
            for item in (live_short_segments or [])
            if str(item).strip()
        }
        self.enable_daily_profile_selector = bool(enable_daily_profile_selector)
        if trade_score_threshold is not None and not 0.0 <= float(trade_score_threshold) <= 95.0:
            raise ValueError("trade_score_threshold must be between 0 and 95")
        self.trade_score_threshold = (
            None if trade_score_threshold is None else float(trade_score_threshold)
        )
        self.enable_wave_guard = bool(enable_wave_guard)
        self.wave_batch_guard_config = (
            wave_batch_guard_config or WaveBatchGuardConfig(enabled=False)
        ).normalized()
        self.daily_profile_selector_config = (
            daily_profile_selector_config or DailyProfileSelectorConfig()
        ).normalized()
        self.stake_progression_recovery_warning = ""
        self._storage_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="monitor-storage")
        self._storage_futures: list[Future] = []
        self._storage_futures_condition = threading.Condition()
        self._storage_write_failures: list[Exception] = []
        self._observation_audit_collector: list[
            tuple[Signal, ObservationSignal, str, dict]
        ] | None = None
        self._bundled_decision_ids: set[str] = set()
        self._decision_storage_failed = False
        self._decision_runtime_config_cache = None
        self._profile_source_cached = False
        self._cached_profile_source = None
        self._profile_summary_prepare_error = None
        profile_preparer = (
            getattr(self.storage, "prepare_order_profile_summary", None)
            if self.storage
            else None
        )
        if profile_preparer is not None:
            try:
                profile_preparer(
                    self.symbol,
                    profile_guard_min_history=self.profile_guard_min_history,
                    profile_guard_min_group_size=self.profile_guard_min_group_size,
                )
            except Exception as exc:  # noqa: BLE001 - 预计算失败时热路径保持无全表回退。
                self._profile_summary_prepare_error = exc
        restored_orders = self.storage.load_orders(self.symbol) if self.storage else []
        restored_observations = self._load_restored_observations()
        self.simulator = self._build_simulator(self.symbol, restored_orders)
        self._pending_settlement_events: list[tuple[str, SettlementEvent]] = []
        self.fear_greed_provider = fear_greed_provider
        self.fear_greed = None
        self.warmup: dict | None = None
        self.risk_pause: str = ""
        self.klines: list[Kline] = []
        restored_wave_runtime = self._load_wave_runtime()
        self.wave_state = (
            restored_wave_runtime["snapshot"]
            if restored_wave_runtime is not None
            else analyze_wave(())
        )
        self._wave_evaluated_at = (
            int(restored_wave_runtime["evaluated_at"])
            if restored_wave_runtime is not None
            else 0
        )
        self._wave_runtime_bootstrap_required = (
            self.storage is not None and restored_wave_runtime is None
        )
        self._wave_bootstrap_cancel_pending = bool(
            self._wave_runtime_bootstrap_required
            and self.simulator.stake_progression.pending_credits()
        )
        self.signals: list[Signal] = []
        self.observations: list[ObservationSignal] = list(reversed(restored_observations))
        self._direction_pulse_history = self._load_direction_pulse_history(restored_observations)
        self.direction_pulse_shadow = empty_direction_pulse_shadow(current_time=self._now_ms())
        self._refresh_direction_pulse_shadow(self._now_ms(), report_error=False)
        self.daily_profile_selection = self._load_latest_daily_profile_selection()
        self.active_daily_profile_selection: dict | None = None
        self.selected_signal: Signal | None = None
        self.order_decision = "WAIT"
        self.rolling_edge: dict = self._empty_rolling_edge()
        self.result_sequence_guard: dict = self._empty_result_sequence_guard()
        self.wave_batch_guard: dict = self._empty_wave_batch_guard()
        self.profile_degradation_guard: dict = self._empty_profile_degradation_guard()
        self.profile_health_guard: dict = self._empty_profile_health_guard()
        self.profile_guard_audit: dict = self._empty_profile_guard_audit()
        self.time_period_guard: dict = evaluate_time_period_guard(
            self._now_ms(),
            self.time_period_guard_config,
        ).to_dict()
        self.last_error: str | None = None
        self.updated_at_ms = 0
        self._realtime_price: float | None = None
        self._realtime_price_event_time_ms = 0
        self._realtime_price_received_at_ms = 0
        self._market_stream_status = "STARTING"
        self._opened_signal_keys: set[tuple[int, int, str]] = set()
        self._last_order_opened_at = self._latest_order_opened_at_by_direction(
            restored_orders
        )
        self._lock = threading.RLock()

    def capture_symbol_context(self) -> tuple[str, int]:
        with self._lock:
            return self.symbol, self._symbol_generation

    def update_realtime_price(
        self,
        price: float,
        event_time_ms: int,
        received_at_ms: int,
        *,
        expected_context: tuple[str, int] | None = None,
    ) -> bool:
        with self._lock:
            if not self._matches_symbol_context(expected_context):
                return False
            event_time_ms = int(event_time_ms)
            if event_time_ms < self._realtime_price_event_time_ms:
                return False
            self._realtime_price = float(price)
            self._realtime_price_event_time_ms = event_time_ms
            self._realtime_price_received_at_ms = int(received_at_ms)
            self._market_stream_status = "CONNECTED"
            return True

    def record_market_stream_status(
        self,
        status: str,
        *,
        expected_context: tuple[str, int] | None = None,
    ) -> bool:
        with self._lock:
            if not self._matches_symbol_context(expected_context):
                return False
            self._market_stream_status = str(status).upper()
            return True

    def latest_kline_open_time(
        self,
        *,
        expected_context: tuple[str, int] | None = None,
    ) -> int | None:
        with self._lock:
            if not self._matches_symbol_context(expected_context) or not self.klines:
                return None
            return self.klines[-1].open_time

    def price_snapshot(self) -> dict:
        with self._lock:
            latest = self.klines[-1] if self.klines else None
            price = self._realtime_price
            event_time_ms = self._realtime_price_event_time_ms
            received_at_ms = self._realtime_price_received_at_ms
            if price is None and latest is not None:
                price = latest.close
                event_time_ms = latest.close_time
                received_at_ms = self.updated_at_ms
            stale = bool(
                received_at_ms
                and int(self._now_ms()) - received_at_ms > REALTIME_PRICE_STALE_MS
            )
            return {
                "symbol": self.symbol,
                "latest_price": price,
                "event_time_ms": event_time_ms or None,
                "received_at_ms": received_at_ms or None,
                "stale": stale,
                "stream_status": self._market_stream_status,
            }

    def update_from_klines(
        self,
        klines: Sequence[Kline],
        *,
        expected_context: tuple[str, int] | None = None,
    ) -> bool:
        if not klines:
            return False

        with self._lock:
            operation_context = expected_context or (
                self.symbol,
                self._symbol_generation,
            )
            if not self._matches_symbol_context(operation_context):
                return False
            existing = list(self.klines)
            previous_wave = self.wave_state
            previous_wave_evaluated_at = self._wave_evaluated_at
            if (
                self._wave_runtime_bootstrap_required
                and previous_wave.state in {"UNKNOWN", "TURN_UP", "TURN_DOWN"}
            ):
                previous_wave = None
                previous_wave_evaluated_at = 0

        merged_klines = self._merge_klines(existing, klines)
        latest = merged_klines[-1]
        wave_state, wave_evaluated_at = advance_wave(
            merged_klines,
            previous=previous_wave,
            evaluated_at=previous_wave_evaluated_at,
        )
        bootstrap_ready = (
            wave_state.state not in {"UNKNOWN", "TURN_UP", "TURN_DOWN"}
            and wave_evaluated_at > 0
        )
        if self._wave_runtime_bootstrap_required and bootstrap_ready:
            wave_state = self._bootstrap_wave_anchor(wave_state, wave_evaluated_at)
        fear_greed = self._fear_greed_context()
        new_signals = [
            analyze_volume_price(merged_klines, timeframe_minutes=minutes, fear_greed=fear_greed)
            for minutes in LIVE_TRADE_TIMEFRAMES
        ]
        observation_signals = [
            signal
            for minutes in LIVE_TRADE_TIMEFRAMES
            for signal in analyze_observation_signals(merged_klines, timeframe_minutes=minutes, fear_greed=fear_greed)
        ]
        selected_signal = choose_trade_signal(merged_klines, fear_greed=fear_greed)

        with self._lock:
            if not self._matches_symbol_context(operation_context):
                return False
            self.fear_greed = fear_greed
            self.klines = merged_klines
            self.wave_state = wave_state
            self._wave_evaluated_at = wave_evaluated_at
            self.updated_at_ms = int(time.time() * 1000)
            self.last_error = None
            if (
                self._wave_runtime_bootstrap_required
                and bootstrap_ready
                and self._wave_bootstrap_cancel_pending
            ):
                if not self._cancel_pending_progression_credits():
                    return False
                self._wave_bootstrap_cancel_pending = False
            if self.storage and (
                not self._wave_runtime_bootstrap_required or bootstrap_ready
            ):
                if not self._persist_wave_runtime(
                    wave_state,
                    wave_evaluated_at,
                ):
                    return False
                self._wave_runtime_bootstrap_required = False
            if self.storage and not self._flush_pending_settlement_events():
                return False
            settlement_events = self.simulator.settle_expired_order_events_from_klines(merged_klines)
            if self.storage:
                self._pending_settlement_events.extend(
                    (self.symbol, event) for event in settlement_events
                )
                if not self._flush_pending_settlement_events():
                    return False
            self._settle_observations(latest.close_time, latest.close, merged_klines)

            self._refresh_daily_profile_selection(latest.close_time)
            new_signals = [
                self._attach_wave_metadata(signal, wave_state) for signal in new_signals
            ]
            observation_signals = [
                self._attach_wave_metadata(signal, wave_state)
                for signal in observation_signals
            ]
            new_signals = [
                self._attach_quality_score(signal, current_time=latest.close_time)
                for signal in new_signals
            ]
            observation_signals = [
                self._attach_quality_score(signal, current_time=latest.close_time)
                for signal in observation_signals
            ]
            self.signals = new_signals
            self._bundled_decision_ids = set()
            self._observation_audit_collector = []
            try:
                selected_signal, daily_profile_required = self._select_daily_profile_signal(
                    selected_signal,
                    observation_signals,
                    latest.close_time,
                )
                if not selected_signal.candidate_origin:
                    selected_signal = replace(
                        selected_signal,
                        candidate_origin=self._origin_before_profile_promotion(
                            selected_signal
                        ),
                    )
                has_formal_candidate = bool(selected_signal.actionable)
                selected_signal = self._apply_wave_guard(selected_signal, wave_state)
                selected_signal = self._attach_quality_score(
                    selected_signal,
                    current_time=latest.close_time,
                )
                self.selected_signal = selected_signal
                self.order_decision = self._maybe_open_order(
                    selected_signal,
                    latest,
                    daily_profile_required=daily_profile_required,
                )
                if not self._decision_storage_failed:
                    self._record_observation_candidates(observation_signals, latest)
                observation_audits = tuple(self._observation_audit_collector)
            finally:
                self._observation_audit_collector = None
            if self.storage and not self._decision_storage_failed:
                final_signal = self.selected_signal or selected_signal
                if final_signal.decision_id not in self._bundled_decision_ids:
                    self._save_signal(
                        final_signal,
                        self.order_decision,
                        self.updated_at_ms,
                        has_formal_candidate=has_formal_candidate,
                        force_independent=bool(observation_audits),
                        event_kind=(
                            "ORDER_OPENED" if self.order_decision == "OPENED" else None
                        ),
                    )
                for (
                    observation_signal,
                    _observation,
                    source_decision,
                    audit_context,
                ) in observation_audits:
                    if observation_signal.decision_id in self._bundled_decision_ids:
                        continue
                    self._save_signal(
                        observation_signal,
                        source_decision,
                        self.updated_at_ms,
                        force_independent=True,
                        event_kind="OBSERVATION_CANDIDATE",
                        audit_context_override=audit_context,
                    )
            return self.order_decision != "STORAGE_ERROR"

    def seed_klines(
        self,
        klines: Sequence[Kline],
        warmup_report: dict | None = None,
        *,
        expected_context: tuple[str, int] | None = None,
    ) -> None:
        with self._lock:
            if not self._matches_symbol_context(expected_context):
                return
            self.klines = self._merge_klines(self.klines, klines)
            previous_wave = self.wave_state
            previous_wave_evaluated_at = self._wave_evaluated_at
            if (
                self._wave_runtime_bootstrap_required
                and previous_wave.state in {"UNKNOWN", "TURN_UP", "TURN_DOWN"}
            ):
                previous_wave = None
                previous_wave_evaluated_at = 0
            self.wave_state, self._wave_evaluated_at = advance_wave(
                self.klines,
                previous=previous_wave,
                evaluated_at=previous_wave_evaluated_at,
            )
            bootstrap_ready = (
                self.wave_state.state not in {"UNKNOWN", "TURN_UP", "TURN_DOWN"}
                and self._wave_evaluated_at > 0
            )
            if self._wave_runtime_bootstrap_required and bootstrap_ready:
                self.wave_state = self._bootstrap_wave_anchor(
                    self.wave_state,
                    self._wave_evaluated_at,
                )
            self.warmup = warmup_report
            self.updated_at_ms = int(time.time() * 1000)
            if (
                self._wave_runtime_bootstrap_required
                and bootstrap_ready
                and self._wave_bootstrap_cancel_pending
            ):
                if not self._cancel_pending_progression_credits():
                    return
                self._wave_bootstrap_cancel_pending = False
            if self.storage and (
                not self._wave_runtime_bootstrap_required or bootstrap_ready
            ):
                persisted = self._persist_wave_runtime(
                    self.wave_state,
                    self._wave_evaluated_at,
                )
                if persisted:
                    self._wave_runtime_bootstrap_required = False

    def record_error(
        self,
        message: str,
        *,
        expected_context: tuple[str, int] | None = None,
    ) -> bool:
        with self._lock:
            if not self._matches_symbol_context(expected_context):
                return False
            self.last_error = message
            self.updated_at_ms = int(time.time() * 1000)
            return True

    def _fear_greed_context(self) -> FearGreedContext:
        if self.fear_greed_provider is None:
            return FearGreedContext(
                value=50,
                classification="Neutral",
                average_30d=50.0,
                trend="unknown",
                updated_at_ms=int(time.time() * 1000),
                source="neutral",
            )
        return self.fear_greed_provider.get_context()

    def reset_symbol(self, symbol: str) -> None:
        with self._lock:
            self._symbol_generation += 1
            self.symbol = symbol.upper()
            restored_orders = self.storage.load_orders(self.symbol) if self.storage else []
            restored_observations = self._load_restored_observations()
            self.simulator = self._build_simulator(self.symbol, restored_orders)
            self.fear_greed = None
            self.warmup = None
            self.risk_pause = ""
            self.klines = []
            restored_wave_runtime = self._load_wave_runtime()
            self.wave_state = (
                restored_wave_runtime["snapshot"]
                if restored_wave_runtime is not None
                else analyze_wave(())
            )
            self._wave_evaluated_at = (
                int(restored_wave_runtime["evaluated_at"])
                if restored_wave_runtime is not None
                else 0
            )
            self._wave_runtime_bootstrap_required = (
                self.storage is not None and restored_wave_runtime is None
            )
            self._wave_bootstrap_cancel_pending = bool(
                self._wave_runtime_bootstrap_required
                and self.simulator.stake_progression.pending_credits()
            )
            self.signals = []
            self.observations = list(reversed(restored_observations))
            self._direction_pulse_history = self._load_direction_pulse_history(restored_observations)
            self.direction_pulse_shadow = empty_direction_pulse_shadow(current_time=self._now_ms())
            self._refresh_direction_pulse_shadow(self._now_ms(), report_error=False)
            self.daily_profile_selection = self._load_latest_daily_profile_selection()
            self.active_daily_profile_selection = None
            self.selected_signal = None
            self.order_decision = "WAIT"
            self.rolling_edge = self._empty_rolling_edge()
            self.result_sequence_guard = self._empty_result_sequence_guard()
            self.wave_batch_guard = self._empty_wave_batch_guard()
            self.profile_degradation_guard = self._empty_profile_degradation_guard()
            self.profile_health_guard = self._empty_profile_health_guard()
            self.profile_guard_audit = self._empty_profile_guard_audit()
            self.last_error = None
            self.updated_at_ms = int(time.time() * 1000)
            self.time_period_guard = evaluate_time_period_guard(
                self.updated_at_ms,
                self.time_period_guard_config,
            ).to_dict()
            self._realtime_price = None
            self._realtime_price_event_time_ms = 0
            self._realtime_price_received_at_ms = 0
            self._market_stream_status = "STARTING"
            self._opened_signal_keys.clear()
            self._last_order_opened_at = self._latest_order_opened_at_by_direction(
                restored_orders
            )

    def _matches_symbol_context(self, expected_context: tuple[str, int] | None) -> bool:
        return expected_context is None or expected_context == (
            self.symbol,
            self._symbol_generation,
        )

    @staticmethod
    def _latest_order_opened_at_by_direction(orders) -> dict[str, int | None]:
        return {
            direction: max(
                (
                    int(order.opened_at)
                    for order in orders
                    if order.direction.upper() == direction
                ),
                default=None,
            )
            for direction in ("LONG", "SHORT")
        }

    def _build_simulator(self, symbol: str, restored_orders) -> AccountSimulator:
        activated_at = 0
        credits = []
        prepare = getattr(self.storage, "prepare_stake_progression", None) if self.storage else None
        load_credits = (
            getattr(self.storage, "load_stake_progression_credits", None)
            if self.storage
            else None
        )
        if prepare is not None and load_credits is not None:
            activated_at = prepare(
                symbol,
                TWO_STAGE_VERSION,
                self.enable_stake_progression,
                self._now_ms(),
            )
            credits = load_credits(symbol, TWO_STAGE_VERSION)
            orders_by_id = {order.id: order for order in restored_orders}
            credits = [
                replace(
                    credit,
                    direction=orders_by_id[credit.source_order_id].direction,
                )
                if not credit.direction and credit.source_order_id in orders_by_id
                else credit
                for credit in credits
            ]

        pending_ids = {
            credit.credit_id for credit in credits if credit.status == "PENDING"
        }
        active_second_order_ids = {
            order.id
            for order in restored_orders
            if order.status == "OPEN"
            and order.stake_progression_step == 2
            and order.stake_progression_version == TWO_STAGE_VERSION
        }
        simulator = AccountSimulator(
            stake=self.stake,
            win_return=self.win_return,
            orders=restored_orders,
            enable_stake_progression=self.enable_stake_progression,
            stake_progression_max_orders=2,
            stake_progression_base_only_segments=self.stake_progression_base_only_segments,
            stake_progression_max_active=self.stake_progression_max_active,
            max_open_orders=self.order_policy.max_open_orders,
            stake_progression_activated_at=activated_at,
            stake_progression_credits=credits,
            active_second_order_ids=active_second_order_ids,
        )
        cancelled = [
            credit
            for credit in simulator.stake_progression.credits
            if credit.credit_id in pending_ids and credit.status == "CANCELLED"
        ]
        self.stake_progression_recovery_warning = ""
        if cancelled:
            reason = "滚单已禁用" if not self.enable_stake_progression else "并发上限缩小"
            self.stake_progression_recovery_warning = (
                f"恢复时取消 {len(cancelled)} 个待用资格：{reason}"
            )
            self.storage.cancel_stake_progression_credits(
                symbol,
                [replace(credit) for credit in cancelled],
            )
        return simulator

    def _merge_klines(self, existing: Sequence[Kline], incoming: Sequence[Kline]) -> list[Kline]:
        merged = {item.open_time: item for item in existing}
        for item in incoming:
            merged[item.open_time] = item
        ordered = sorted(merged.values(), key=lambda item: item.open_time)
        if self.max_klines > 0 and len(ordered) > self.max_klines:
            return ordered[-self.max_klines :]
        return ordered

    def _decision_runtime_config(self):
        if self._decision_runtime_config_cache is None:
            config = {
                "strategy_build_id": self.strategy_build_id,
                "data_windows": {
                    "max_klines": self.max_klines,
                    "live_trade_timeframes": list(LIVE_TRADE_TIMEFRAMES),
                    "observation_profile_lookback_days": (
                        self.observation_profile_lookback_days
                    ),
                    "daily_profile_lookback_days": (
                        self.daily_profile_selector_config.lookback_days
                    ),
                },
                "order_policy": asdict(self.order_policy),
                "stake": {
                    "amount": self.stake,
                    "win_return": self.win_return,
                    "progression_enabled": self.enable_stake_progression,
                    "progression_max_orders": self.stake_progression_max_orders,
                    "progression_max_active": self.stake_progression_max_active,
                    "progression_base_only_segments": list(
                        self.stake_progression_base_only_segments
                    ),
                },
                "guards": {
                    "rolling_edge_enabled": self.enable_rolling_edge_guard,
                    "rolling_edge": asdict(self.rolling_edge_config),
                    "result_sequence": asdict(self.result_sequence_guard_config),
                    "profile_degradation": asdict(
                        self.profile_degradation_guard_config
                    ),
                    "profile_health": asdict(self.profile_health_guard_config),
                    "time_period": asdict(self.time_period_guard_config),
                    "wave_enabled": self.enable_wave_guard,
                    "wave_batch": asdict(self.wave_batch_guard_config),
                    "profile_enabled": self.enable_profile_guard,
                    "profile_min_history": self.profile_guard_min_history,
                    "profile_min_group_size": self.profile_guard_min_group_size,
                },
                "profiles": {
                    "daily_selector_enabled": self.enable_daily_profile_selector,
                    "daily_selector": asdict(self.daily_profile_selector_config),
                    "observation_promotion_enabled": (
                        self.enable_observation_profile_promotion
                    ),
                    "observation_lookback_days": (
                        self.observation_profile_lookback_days
                    ),
                    "observation_min_samples": self.observation_profile_min_samples,
                    "observation_min_win_rate": (
                        self.observation_profile_min_win_rate
                    ),
                    "observation_min_ev": self.observation_profile_min_ev,
                    "observation_min_edge": self.observation_profile_min_edge,
                    "live_short_segments": sorted(self.live_short_segments),
                },
                "trade_score_threshold": self.trade_score_threshold,
                "module_versions": {
                    "decision_context": "DECISION_CONTEXT_V2",
                    "transitional_trace": TRANSITIONAL_DECISION_BUILD_ID,
                },
            }
            self._decision_runtime_config_cache = runtime_config_snapshot(
                config,
                strategy_build_id=self.strategy_build_id,
            )
        return self._decision_runtime_config_cache

    @staticmethod
    def _origin_before_profile_promotion(signal: Signal) -> str:
        if signal.candidate_origin in {
            "NATIVE_ACTIONABLE",
            "PROFILE_PROMOTED_WAIT",
        }:
            return signal.candidate_origin
        direction = str(signal.direction or "").upper()
        if direction in {"LONG", "SHORT"} and abs(signal.score) >= signal.threshold:
            return "NATIVE_ACTIONABLE"
        return "PROFILE_PROMOTED_WAIT"

    def _formal_candidate_origin(self, signal: Signal) -> str:
        return self._origin_before_profile_promotion(signal)

    def _candidate_identity(
        self,
        signal: Signal,
        *,
        candidate_ordinal: int,
        candidate_origin: str | None = None,
        closed_kline_at_ms: int | None = None,
    ) -> dict[str, object]:
        direction = self._signal_direction(signal)
        order_slot = str(signal.order_slot or "")
        order_slot_scope = str(signal.order_slot_scope or "")
        if closed_kline_at_ms is not None:
            prior_open_count = sum(
                1
                for order in self.simulator.orders
                if order.direction == direction
                and order.opened_at < int(closed_kline_at_ms)
                and (
                    order.settled_at is None
                    or order.settled_at > int(closed_kline_at_ms)
                )
            )
            order_slot = "SECOND" if prior_open_count > 0 else "FIRST"
            order_slot_scope = "DIRECTION_V2"
        return {
            "candidate_origin": str(
                candidate_origin or self._formal_candidate_origin(signal)
            ),
            "candidate_ordinal": int(candidate_ordinal),
            "direction": direction,
            "profile_key": str(signal.profile_key or ""),
            "strategy_family": str(signal.strategy_family or ""),
            "strategy_tag": str(signal.strategy_tag or ""),
            "order_slot": order_slot,
            "order_slot_scope": order_slot_scope,
            "timeframe_minutes": int(signal.timeframe_minutes),
            "threshold_segment": str(signal.threshold_segment or ""),
        }

    def _reuse_committed_decision(
        self,
        signal: Signal,
        latest: Kline,
    ) -> str | None:
        if not self.storage:
            return None
        loader = getattr(
            self.storage,
            "load_decision_context_for_candidate",
            None,
        )
        if loader is None:
            return None
        config = self._decision_runtime_config()
        candidate_origin = self._formal_candidate_origin(signal)
        candidate_identity = self._candidate_identity(
            signal,
            candidate_ordinal=0,
            candidate_origin=candidate_origin,
            closed_kline_at_ms=latest.close_time,
        )
        try:
            context = loader(
                self.symbol,
                closed_kline_at_ms=int(latest.close_time),
                candidate_origin=candidate_origin,
                profile_key=str(signal.profile_key or ""),
                runtime_config_hash=config.hash,
                strategy_build_id=config.strategy_build_id,
                candidate_identity=candidate_identity,
            )
        except Exception as exc:  # noqa: BLE001 - 决策身份读取失败必须阻断本轮。
            self._decision_storage_failed = True
            self._set_storage_error("已提交决策读取失败", exc)
            return "STORAGE_ERROR"
        if context is None:
            return None
        frozen_signal_payload = context["inputs"].get("signal")
        if not isinstance(frozen_signal_payload, dict):
            return None
        accepted_signal_fields = {field.name for field in fields(Signal)}
        try:
            frozen_signal = Signal(
                **{
                    key: deepcopy(value)
                    for key, value in frozen_signal_payload.items()
                    if key in accepted_signal_fields
                }
            )
        except (TypeError, ValueError) as exc:
            self._decision_storage_failed = True
            self._set_storage_error("已提交决策信号读取失败", exc)
            return "STORAGE_ERROR"
        frozen_identity = context["inputs"].get("identity")
        if (
            not isinstance(frozen_identity, dict)
            or frozen_identity != candidate_identity
            or self._candidate_identity(
                frozen_signal,
                candidate_ordinal=int(frozen_identity.get("candidate_ordinal", -1)),
                candidate_origin=str(frozen_identity.get("candidate_origin", "")),
                closed_kline_at_ms=context["closed_kline_at_ms"],
            )
            != frozen_identity
        ):
            self._decision_storage_failed = True
            self._set_storage_error(
                "已提交决策信号读取失败",
                ValueError("frozen signal does not match candidate identity"),
            )
            return "STORAGE_ERROR"
        final_decision = str(context["final_decision"])
        if final_decision == "OPENED":
            try:
                persisted_orders = self.storage.load_orders(self.symbol)
            except Exception as exc:  # noqa: BLE001 - 已提交订单必须可恢复。
                self._decision_storage_failed = True
                self._set_storage_error("已提交订单读取失败", exc)
                return "STORAGE_ERROR"
            matching_orders = [
                order
                for order in persisted_orders
                if order.decision_id == context["decision_id"]
            ]
            if len(matching_orders) != 1:
                self._decision_storage_failed = True
                self._set_storage_error(
                    "已提交订单读取失败",
                    ValueError("OPENED decision must map to exactly one order"),
                )
                return "STORAGE_ERROR"
            if not any(
                order.decision_id == context["decision_id"]
                for order in self.simulator.orders
            ):
                self.simulator = self._build_simulator(
                    self.symbol,
                    persisted_orders,
                )
                self._last_order_opened_at = (
                    self._latest_order_opened_at_by_direction(persisted_orders)
                )
                restored = matching_orders[0]
                self._opened_signal_keys.add(
                    (
                        int(signal.open_time),
                        restored.timeframe_minutes,
                        restored.direction,
                    )
                )
        enriched_signal = replace(
            frozen_signal,
            decision_id=str(context["decision_id"]),
            context_version=str(context["context_version"]),
            runtime_config_hash=str(context["runtime_config_hash"]),
            strategy_build_id=str(context["strategy_build_id"]),
            candidate_origin=str(context["candidate_origin"]),
            decision_inputs=deepcopy(context["inputs"]),
            decision_trace=deepcopy(context["decision_trace"]),
            first_decisive_block=str(context["first_decisive_block"]),
        )
        audit_snapshot = context["inputs"].get("audit_snapshot", {})
        if isinstance(audit_snapshot, dict):
            restored_guards = (
                ("rolling_edge", "rolling_edge"),
                ("result_sequence_guard", "result_sequence_guard"),
                ("wave_batch_guard", "wave_batch_guard"),
                ("profile_degradation_guard", "profile_degradation_guard"),
                ("profile_health_guard", "profile_health_guard"),
                ("time_period_guard", "time_period_guard"),
                ("profile_guard", "profile_guard_audit"),
            )
            for source_key, attribute_name in restored_guards:
                value = audit_snapshot.get(source_key)
                if isinstance(value, dict):
                    setattr(self, attribute_name, deepcopy(value))
        self.risk_pause = "" if final_decision == "OPENED" else str(
            context.get("final_reason") or ""
        )
        self.selected_signal = enriched_signal
        self._bundled_decision_ids.add(enriched_signal.decision_id)
        return final_decision

    def _decision_artifacts(
        self,
        signal: Signal,
        latest: Kline,
        decision: str,
        *,
        final_reason: str,
        candidate_origin: str,
        candidate_ordinal: int,
        observation_allowed: bool,
        audit_context: dict,
        event_kind: str,
    ):
        config = self._decision_runtime_config()
        direction = self._signal_direction(signal)
        profile_key = str(signal.profile_key or "")
        candidate_identity = self._candidate_identity(
            signal,
            candidate_ordinal=candidate_ordinal,
            candidate_origin=candidate_origin,
            closed_kline_at_ms=latest.close_time,
        )
        builder = DecisionContextBuilder.new(
            self.symbol,
            int(latest.close_time),
            candidate_origin,
            config.hash,
            strategy_build_id=config.strategy_build_id,
            profile_key=profile_key,
            candidate_ordinal=int(candidate_ordinal),
            candidate_identity=candidate_identity,
        )
        builder.capture_inputs(
            {
                "identity": candidate_identity,
                "market": {"latest_closed_kline": latest.to_dict()},
                "signal": signal.to_dict(),
                "strategy_inputs": deepcopy(signal.decision_inputs),
                "audit_snapshot": deepcopy(audit_context),
                "transition": {
                    "version": TRANSITIONAL_DECISION_BUILD_ID,
                    "complete_gate_trace": False,
                },
            }
        )
        normalized_decision = str(decision or "UNKNOWN").upper()
        open_allowed = normalized_decision == "OPENED"
        is_block = not open_allowed and normalized_decision != "RESEARCH_OBSERVE"
        builder.trace(
            "TRANSITIONAL_OUTCOME",
            "BLOCK" if is_block else "PASS",
            {"final_decision": normalized_decision},
            normalized_decision,
        )
        context = builder.finish(
            normalized_decision,
            str(final_reason or ""),
            open_allowed,
            bool(observation_allowed),
        )
        context_payload = context.to_dict()
        enriched_signal = replace(
            signal,
            direction=direction or signal.direction,
            decision_id=context.decision_id,
            context_version=context.context_version,
            runtime_config_hash=context.runtime_config_hash,
            strategy_build_id=context.strategy_build_id,
            candidate_origin=context.candidate_origin,
            decision_inputs=context_payload["inputs"],
            decision_trace=context_payload["decision_trace"],
            first_decisive_block=context.first_decisive_block,
        )
        audit = DecisionAudit(
            signal=enriched_signal,
            decision=normalized_decision,
            created_at_ms=int(latest.close_time),
            audit_context=deepcopy(audit_context),
            event_kind=event_kind,
        )
        return config, context, enriched_signal, audit

    def _maybe_open_order(
        self,
        signal: Signal,
        latest: Kline,
        *,
        daily_profile_required: bool = False,
    ) -> str:
        with self._lock:
            return self._maybe_open_order_locked(
                signal,
                latest,
                daily_profile_required=daily_profile_required,
            )

    def _maybe_open_order_locked(
        self,
        signal: Signal,
        latest: Kline,
        *,
        daily_profile_required: bool = False,
    ) -> str:
        self._decision_storage_failed = False
        self._profile_source_cached = False
        self._cached_profile_source = None
        self.risk_pause = ""
        self.profile_guard_audit = self._empty_profile_guard_audit()
        signal = self._attach_direction_pulse_shadow(signal, current_time=latest.close_time)
        candidate_origin = self._formal_candidate_origin(signal)
        if signal.candidate_origin != candidate_origin:
            signal = replace(signal, candidate_origin=candidate_origin)
        self.selected_signal = signal
        reused_decision = self._reuse_committed_decision(signal, latest)
        if reused_decision is not None:
            return reused_decision
        time_period_decision = evaluate_time_period_guard(
            latest.close_time,
            self.time_period_guard_config,
        )
        self.time_period_guard = time_period_decision.to_dict()
        self.rolling_edge = self._rolling_edge_status(signal, latest)
        should_observe = self._should_record_observation(signal)
        batch_decision = evaluate_wave_batch_guard(
            self.simulator.orders,
            current_time=latest.close_time,
            current_batch_id=signal.wave_batch_id,
            config=self.wave_batch_guard_config,
        )
        self.wave_batch_guard = self._wave_batch_guard_to_dict(batch_decision)
        profile_decision = self._refresh_profile_degradation_guard(
            signal,
            latest.close_time,
        )
        preserve_progression_credit = (
            batch_decision.mode == "RECOVERY"
            and profile_decision.status == "RECOVERY_READY"
        )
        if batch_decision.blocked or (
            batch_decision.mode == "RECOVERY" and not preserve_progression_credit
        ):
            direction = self._signal_direction(signal)
            if direction and not self._cancel_pending_progression_credits(
                direction=direction
            ):
                return "STORAGE_ERROR"
            self._wave_bootstrap_cancel_pending = False
        elif batch_decision.mode != "RECOVERY":
            stale_source_ids = self._stale_progression_credit_source_ids(signal)
            if stale_source_ids and not self._cancel_pending_progression_credits(
                stale_source_ids,
                self._signal_direction(signal),
            ):
                return "STORAGE_ERROR"
            self._wave_bootstrap_cancel_pending = False
        else:
            self._wave_bootstrap_cancel_pending = False
        if signal.wave_guard_mode == "DIRECTION_BLOCKED":
            return self._block_order(
                signal,
                latest,
                "WAVE_DIRECTION_BLOCKED",
                f"1分钟波段 {signal.wave_state} 不允许 {signal.observe_direction}",
                should_observe=should_observe,
            )
        if daily_profile_required and not signal.daily_profile_selected:
            return self._block_order(
                signal,
                latest,
                "DAILY_PROFILE_NOT_SELECTED",
                "当前信号未进入今日启用画像，仅记录观察",
                should_observe=should_observe,
            )

        health_decision = self._refresh_profile_health_guard(
            signal,
            latest.close_time,
        )
        signal = replace(
            signal,
            profile_health_status=health_decision.status,
            profile_health_sample_size=health_decision.sample_size,
            profile_health_win_rate=health_decision.win_rate,
            profile_health_ev=health_decision.ev,
            profile_health_evaluated_at=health_decision.evaluated_at,
        )
        self.selected_signal = signal
        if health_decision.blocked:
            return self._block_order(
                signal,
                latest,
                "PROFILE_HEALTH_BLOCKED",
                health_decision.reason,
                should_observe=should_observe,
            )

        signal, gate = self._admit_order_candidate(signal, latest)
        if not gate.open_allowed:
            if not self._persist_blocked_decision(
                signal,
                latest,
                gate.code,
                signal.reason,
                should_observe=should_observe,
            ):
                return "STORAGE_ERROR"
            return gate.code
        if (
            signal.direction == "SHORT"
            and not signal.daily_profile_selected
            and signal.threshold_segment.upper() not in self.live_short_segments
        ):
            return self._block_order(
                signal,
                latest,
                "SHORT_OBSERVE_ONLY",
                "SHORT观察模式：仅记录信号，不开模拟订单，不推送Webhook",
                should_observe=True,
            )
        if signal.direction == "SHORT" and signal.observe_only:
            signal = replace(
                signal,
                observe_only=False,
                reason=f"{signal.reason}；{signal.threshold_segment} SHORT小口放行",
            )
            self.selected_signal = signal
        signal = replace(
            signal,
            wave_guard_status=batch_decision.code,
            wave_guard_reason=batch_decision.reason,
        )
        self.selected_signal = signal
        if batch_decision.blocked:
            return self._block_order(
                signal,
                latest,
                batch_decision.code,
                batch_decision.reason,
                should_observe=should_observe,
            )
        if batch_decision.mode == "RECOVERY":
            signal = replace(signal, wave_guard_mode="RECOVERY")
            self.selected_signal = signal
        if profile_decision.status in {"COOLDOWN", "RECOVERY_PENDING"}:
            return self._block_order(
                signal,
                latest,
                "PROFILE_DEGRADATION_BLOCKED",
                profile_decision.reason,
                should_observe=should_observe,
            )
        if profile_decision.status == "RECOVERY_READY":
            signal = replace(
                signal,
                reason=f"{signal.reason}；画像退化试探单",
                profile_degradation_probe=True,
                profile_degradation_triggered_at=profile_decision.triggered_at,
            )
            self.selected_signal = signal
        sequence_decision = evaluate_result_sequence_guard(
            self.simulator.orders,
            current_time=latest.close_time,
            direction=signal.direction,
            config=self.result_sequence_guard_config,
        )
        self.result_sequence_guard = self._result_sequence_guard_to_dict(sequence_decision)
        if sequence_decision.blocked:
            return self._block_order(
                signal,
                latest,
                "RESULT_SEQUENCE_GUARD_BLOCKED",
                sequence_decision.reason,
                should_observe=should_observe,
            )
        if self.enable_rolling_edge_guard and self.rolling_edge["status"] == "DEGRADED":
            return self._block_order(
                signal,
                latest,
                "ROLLING_EDGE_BLOCKED",
                (
                    f"滚动优势衰退 {self.rolling_edge['key']} "
                    f"样本 {self.rolling_edge['sample_size']} "
                    f"胜率 {self.rolling_edge['win_rate']:.2%} "
                    f"EV {self.rolling_edge['ev']:.2f}，暂停开单"
                ),
                should_observe=should_observe,
            )
        if self.enable_profile_guard:
            profile_guard = self._profile_guard_shadow(signal)
            profile_guard_blocked = profile_guard["status"] == "WOULD_BLOCK"
            self.profile_guard_audit = {
                "status": str(profile_guard.get("status") or "NOT_EVALUATED"),
                "code": (
                    "PROFILE_GUARD_BLOCKED"
                    if profile_guard_blocked
                    else "PROFILE_GUARD_PASS"
                ),
                "enabled": True,
                "observe_only": False,
                "blocked": profile_guard_blocked,
                "hit_keys": list(profile_guard.get("hit_keys") or []),
            }
            if profile_guard["status"] == "WOULD_BLOCK":
                return self._block_order(
                    signal,
                    latest,
                    "PROFILE_GUARD_BLOCKED",
                    (
                        "画像守卫命中 "
                        f"{'/'.join(profile_guard['hit_keys'])}，"
                        f"H{profile_guard['min_history']}/"
                        f"G{profile_guard['min_group_size']}，暂停开单"
                    ),
                    should_observe=should_observe,
                )

        if time_period_decision.blocked:
            return self._block_order(
                signal,
                latest,
                time_period_decision.code,
                time_period_decision.reason,
                should_observe=True,
            )

        if signal.order_slot == "SECOND" and not health_decision.allow_second_order:
            return self._block_order(
                signal,
                latest,
                "PROFILE_HEALTH_SECOND_ORDER_BLOCKED",
                health_decision.reason,
                should_observe=should_observe,
            )

        return self._execute_open_order(
            signal,
            latest,
            gate,
            should_observe=should_observe,
            allow_progression=(
                batch_decision.allow_progression
                and profile_decision.allow_progression
                and health_decision.allow_progression
            ),
        )

    def _block_order(
        self,
        signal: Signal,
        latest: Kline,
        code: str,
        reason: str,
        *,
        should_observe: bool,
    ) -> str:
        if not self._persist_blocked_decision(
            signal,
            latest,
            code,
            reason,
            should_observe=should_observe,
        ):
            return "STORAGE_ERROR"
        self.risk_pause = reason
        return code

    def _persist_blocked_decision(
        self,
        signal: Signal,
        latest: Kline,
        code: str,
        reason: str,
        *,
        should_observe: bool,
    ) -> bool:
        if not signal.actionable and not should_observe:
            return True
        if should_observe:
            recorded = self._record_observation(
                signal,
                latest,
                code,
                candidate_origin=self._formal_candidate_origin(signal),
                candidate_ordinal=0,
                primary_decision=True,
                final_reason=reason,
            )
            if self._decision_storage_failed:
                return False
            if recorded:
                return True
        if not self.storage:
            return True
        audit_context = self._current_signal_audit_context()
        config, context, enriched_signal, audit = self._decision_artifacts(
            signal,
            latest,
            code,
            final_reason=reason,
            candidate_origin=self._formal_candidate_origin(signal),
            candidate_ordinal=0,
            observation_allowed=should_observe,
            audit_context=audit_context,
            event_kind="DECISIVE_BLOCK",
        )
        try:
            self.storage.save_decision_bundle(
                config=config,
                context=context,
                audit=audit,
            )
        except Exception as exc:  # noqa: BLE001 - 核心决策包失败必须阻断本轮。
            self._decision_storage_failed = True
            self._set_storage_error("决策持久化失败", exc)
            return False
        self._bundled_decision_ids.add(context.decision_id)
        self.selected_signal = enriched_signal
        return True

    def _admit_order_candidate(self, signal: Signal, latest: Kline) -> tuple[Signal, OrderGate]:
        gate = self.order_policy.evaluate(
            signal,
            latest,
            self.simulator.orders,
            self._last_order_opened_at,
            self._opened_signal_keys,
        )
        promoted_signal = None
        if not self.enable_daily_profile_selector:
            promoted_signal = self._observation_profile_promoted_signal(signal, latest, gate.code)
        if promoted_signal is not None and promoted_signal is not signal:
            signal = promoted_signal
            self.selected_signal = signal
            self.rolling_edge = self._rolling_edge_status(signal, latest)
            gate = self.order_policy.evaluate(
                signal,
                latest,
                self.simulator.orders,
                self._last_order_opened_at,
                self._opened_signal_keys,
            )
        signal = self._attach_quality_score(signal, current_time=latest.close_time)
        if promoted_signal is not None:
            self.selected_signal = signal
        return signal, gate

    def _execute_open_order(
        self,
        signal: Signal,
        latest: Kline,
        gate: OrderGate,
        *,
        should_observe: bool,
        allow_progression: bool,
    ) -> str:
        audit_context = self._current_signal_audit_context()
        pending_observation = (
            self._new_observation(signal, latest, "OPENED")
            if should_observe
            else None
        )
        config, context, signal, audit = self._decision_artifacts(
            signal,
            latest,
            "OPENED",
            final_reason=signal.reason,
            candidate_origin=self._formal_candidate_origin(signal),
            candidate_ordinal=0,
            observation_allowed=pending_observation is not None,
            audit_context=audit_context,
            event_kind="ORDER_OPENED",
        )
        self.selected_signal = signal
        if pending_observation is not None:
            context_payload = context.to_dict()
            pending_observation = replace(
                pending_observation,
                decision_id=context.decision_id,
                context_version=context.context_version,
                runtime_config_hash=context.runtime_config_hash,
                strategy_build_id=context.strategy_build_id,
                candidate_origin=context.candidate_origin,
                decision_inputs=context_payload["inputs"],
                decision_trace=context_payload["decision_trace"],
                first_decisive_block=context.first_decisive_block,
            )
        order, consumed_credit = self.simulator.open_order_with_credit(
            signal,
            entry_price=latest.close,
            opened_at=latest.close_time,
            allow_progression=allow_progression,
        )
        if pending_observation is not None:
            self.observations.append(pending_observation)
        if self.storage:
            captured_entry_context = self._capture_entry_snapshot_context(signal)
            entry_snapshot = self._build_order_entry_snapshot(
                replace(order),
                signal,
                latest,
                captured_entry_context,
            )
            try:
                created = self.storage.save_open_order_decision(
                    config=config,
                    context=context,
                    order=replace(order),
                    credit=(
                        replace(consumed_credit)
                        if consumed_credit is not None
                        else None
                    ),
                    entry_snapshot=entry_snapshot,
                    audit=audit,
                    observation=(
                        replace(pending_observation)
                        if pending_observation is not None
                        else None
                    ),
                )
                created = created is not False
            except Exception as exc:  # noqa: BLE001 - 原子写失败必须回滚内存开单。
                self.simulator.rollback_open_order(order.id)
                if pending_observation is not None:
                    self.observations.remove(pending_observation)
                self._decision_storage_failed = True
                self._set_storage_error("开单持久化失败", exc)
                return "STORAGE_ERROR"
            self._bundled_decision_ids.add(context.decision_id)
        else:
            created = True
        if (
            pending_observation is not None
            and self._observation_audit_collector is not None
        ):
            self._observation_audit_collector.append(
                (
                    signal,
                    pending_observation,
                    "OPENED",
                    audit_context,
                )
            )
        if gate.signal_key:
            self._opened_signal_keys.add(gate.signal_key)
        if not isinstance(self._last_order_opened_at, dict):
            self._last_order_opened_at = {"LONG": None, "SHORT": None}
        self._last_order_opened_at[signal.direction] = latest.close_time
        if created:
            self._send_webhook(signal, order)
        if signal.profile_degradation_probe:
            self._refresh_profile_degradation_guard(signal, latest.close_time)
        return gate.code

    def _apply_wave_guard(self, signal: Signal, wave: WaveSnapshot) -> Signal:
        guarded = self._attach_wave_metadata(signal, wave)
        if not self.enable_wave_guard:
            return replace(
                guarded,
                wave_guard_mode="DISABLED",
                wave_guard_status="DISABLED",
                wave_guard_reason="1分钟波段方向守卫已关闭",
            )
        direction = signal.direction.upper()
        if direction not in {"LONG", "SHORT"}:
            return replace(
                guarded,
                wave_guard_status="SIGNAL_WAIT",
                wave_guard_reason="实时量价和指标信号尚未成立",
            )
        if direction in wave.allowed_directions:
            return replace(
                guarded,
                wave_guard_status="DIRECTION_ALLOWED",
                wave_guard_reason=f"1分钟波段 {wave.state} 允许 {direction}",
            )
        block_reason = f"1分钟波段方向冲突：{wave.state} 不允许 {direction}，等待"
        return replace(
            guarded,
            direction="WAIT",
            observe_direction=signal.observe_direction or direction,
            observe_only=True,
            wave_guard_mode="DIRECTION_BLOCKED",
            wave_guard_status="DIRECTION_BLOCKED",
            wave_guard_reason=block_reason,
            reason=f"{signal.reason}；{block_reason}",
        )

    def _attach_wave_metadata(self, signal: Signal, wave: WaveSnapshot) -> Signal:
        direction = signal.direction.upper()
        profile_version = str(
            (self.active_daily_profile_selection or {}).get("version", "") or "STATIC"
        )
        batch_id = ""
        if direction in {"LONG", "SHORT"} and wave.confirmed_at > 0:
            batch_id = (
                f"{wave.confirmed_at}|{wave.state}|{direction}|"
                f"{signal.threshold_segment}|{profile_version}"
            )
        return replace(
            signal,
            wave_state=wave.state,
            wave_raw_state=wave.raw_state,
            wave_window=wave.window,
            wave_efficiency=round(wave.efficiency, 6),
            wave_direction_ratio=round(wave.direction_ratio, 6),
            wave_atr_strength=round(wave.atr_strength, 6),
            wave_confirmations=wave.confirmations,
            wave_confirmed_at=wave.confirmed_at,
            wave_batch_id=batch_id,
            wave_guard_mode="NORMAL",
            wave_guard_status="PENDING",
            wave_guard_reason="等待波段方向与批次守卫判断",
        )

    def _attach_quality_score(
        self,
        signal: Signal,
        *,
        current_time: int | None = None,
    ) -> Signal:
        direction = self._signal_direction(signal)
        open_order_count = sum(
            1
            for order in self.simulator.orders
            if order.status == "OPEN" and order.direction == direction
        )
        slotted_signal = replace(
            signal,
            order_slot="SECOND" if open_order_count > 0 else "FIRST",
            order_slot_scope="DIRECTION_V2",
        )
        try:
            scored_signal = attach_shadow_quality_score(
                slotted_signal,
                open_order_count=open_order_count,
            )
        except Exception as exc:  # noqa: BLE001 - 影子记录故障不得影响开单主流程。
            self.record_error(f"影子质量评分失败: {exc}")
            scored_signal = slotted_signal
        return self._attach_direction_pulse_shadow(
            scored_signal,
            current_time=current_time,
        )

    def _attach_direction_pulse_shadow(
        self,
        signal: Signal,
        *,
        current_time: int | None = None,
    ) -> Signal:
        direction = self._signal_direction(signal)
        open_order_count = sum(
            1
            for order in self.simulator.orders
            if order.status == "OPEN" and order.direction == direction
        )
        order_slot = "SECOND" if open_order_count > 0 else "FIRST"
        try:
            snapshot = self.direction_pulse_shadow
            evaluated_at = int(snapshot.get("evaluated_at", 0) or 0)
            if current_time is not None and evaluated_at > int(current_time):
                snapshot = evaluate_direction_pulse_shadow(
                    self._direction_pulse_source_observations(),
                    current_time=int(current_time),
                )
            candidate_shadow = attach_candidate_shadow(
                snapshot,
                direction=direction,
                order_slot=order_slot,
            )
        except Exception as exc:  # noqa: BLE001 - 影子故障不得中断真实开单。
            self.record_error(f"方向脉冲影子附加失败: {exc}")
            candidate_shadow = (
                dict(signal.direction_pulse_shadow)
                if isinstance(signal.direction_pulse_shadow, dict)
                else {}
            )
        return replace(
            signal,
            order_slot=order_slot,
            order_slot_scope="DIRECTION_V2",
            direction_pulse_shadow=candidate_shadow,
        )

    def _refresh_direction_pulse_shadow(
        self,
        current_time: int,
        *,
        report_error: bool = True,
    ) -> bool:
        try:
            snapshot = evaluate_direction_pulse_shadow(
                self._direction_pulse_source_observations(),
                current_time=current_time,
            )
        except Exception as exc:  # noqa: BLE001 - 影子故障不得中断真实行情处理。
            if report_error:
                self.record_error(f"方向脉冲影子刷新失败: {exc}")
            return False
        self.direction_pulse_shadow = snapshot
        return True

    def _flush_pending_settlement_events(self) -> bool:
        while self._pending_settlement_events:
            symbol, event = self._pending_settlement_events[0]
            try:
                self.storage.save_settled_order_with_credit(
                    replace(event.order),
                    symbol,
                    replace(event.progression_credit)
                    if event.progression_credit is not None
                    else None,
                )
            except Exception as exc:  # noqa: BLE001 - 保留事件供下次更新幂等重试。
                self._set_storage_error("结算持久化失败", exc)
                return False
            self._pending_settlement_events.pop(0)
        return True

    def _set_storage_error(self, operation: str, error: Exception) -> None:
        self.last_error = f"{operation}: {error}"
        self.risk_pause = "存储写入失败，暂停开单"
        self.order_decision = "STORAGE_ERROR"

    def _refresh_daily_profile_selection(self, current_time: int) -> None:
        if not self.enable_daily_profile_selector:
            return
        config = self.daily_profile_selector_config
        target = selection_window(
            current_time,
            lookback_days=config.lookback_days,
            evaluation_hour=config.evaluation_hour,
            evaluation_minute=config.evaluation_minute,
            activation_hour=config.activation_hour,
            activation_minute=config.activation_minute,
        )
        latest = self.daily_profile_selection
        if (
            latest is None
            or int(latest.get("effective_from", -1)) != target["effective_from"]
            or not self._daily_profile_config_matches(latest, config)
        ):
            previous = latest or self._load_latest_daily_profile_selection()
            try:
                next_snapshot = build_daily_selection(
                    self.observations,
                    current_time,
                    config=config,
                    previous_snapshot=previous,
                )
                saver = getattr(self.storage, "save_daily_profile_selection", None) if self.storage else None
                if saver is not None:
                    saver(self.symbol, next_snapshot)
            except Exception as exc:  # noqa: BLE001 - 评估失败必须沿用上次有效画像。
                next_snapshot = self._daily_profile_fallback(previous, target, current_time, str(exc))
            self.daily_profile_selection = next_snapshot
            latest = next_snapshot

        if self._selection_is_effective(latest, current_time):
            self.active_daily_profile_selection = latest
            return
        if self._selection_is_effective(self.active_daily_profile_selection, current_time):
            return
        loader = getattr(self.storage, "load_daily_profile_selection", None) if self.storage else None
        active = loader(self.symbol, current_time) if loader is not None else None
        self.active_daily_profile_selection = active

    @staticmethod
    def _daily_profile_config_matches(
        snapshot: dict,
        config: DailyProfileSelectorConfig,
    ) -> bool:
        stored = snapshot.get("config")
        if not isinstance(stored, dict):
            return False
        return all(stored.get(key) == value for key, value in config.__dict__.items())

    def _select_daily_profile_signal(
        self,
        primary_signal: Signal,
        observation_candidates: Sequence[Signal],
        current_time: int,
    ) -> tuple[Signal, bool]:
        if not self.enable_daily_profile_selector:
            return primary_signal, False
        snapshot = self.active_daily_profile_selection
        if not snapshot or snapshot.get("status") not in {"READY", "FALLBACK"}:
            return primary_signal, False

        candidates: list[tuple[Signal, str, str]] = [
            (
                primary_signal,
                (primary_signal.observe_direction or primary_signal.direction).upper(),
                self._origin_before_profile_promotion(primary_signal),
            )
        ]
        candidates.extend(
            (
                signal,
                (signal.observe_direction or signal.direction).upper(),
                self._origin_before_profile_promotion(signal),
            )
            for signal in observation_candidates
        )
        for selected_profile in snapshot.get("selected_profiles", []):
            selected_key = str(selected_profile.get("key", ""))
            for signal, direction, candidate_origin in candidates:
                if direction not in {"LONG", "SHORT"}:
                    continue
                key = daily_profile_key(
                    signal.timeframe_minutes,
                    signal.strategy_family,
                    signal.strategy_tag,
                    direction,
                    signal.threshold_segment,
                )
                if key != selected_key:
                    continue
                calculated_threshold = (
                    signal.calculated_threshold
                    if signal.calculated_threshold > 0
                    else signal.threshold
                )
                return (
                    replace(
                        signal,
                        direction=direction,
                        reason=(
                            f"{self._without_dynamic_threshold_block(signal)}；"
                            f"每日画像启用 {snapshot.get('version', '')} "
                            f"N{selected_profile.get('sample_size', 0)} "
                            f"胜率{float(selected_profile.get('win_rate', 0.0)):.2%} "
                            f"EV{float(selected_profile.get('ev', 0.0)):.2f}U"
                        ),
                        calculated_threshold=calculated_threshold,
                        session_allowed=True,
                        session_sample_size=int(selected_profile.get("sample_size", 0)),
                        session_win_rate=float(selected_profile.get("win_rate", 0.0)),
                        session_ev=float(selected_profile.get("ev", 0.0)),
                        observe_direction=direction,
                        observe_only=False,
                        profile_key=key,
                        daily_profile_selected=True,
                        daily_profile_version=str(snapshot.get("version", "")),
                        candidate_origin=candidate_origin,
                    ),
                    True,
                )
        return primary_signal, True

    @staticmethod
    def _without_dynamic_threshold_block(signal: Signal) -> str:
        suffix = (
            f"；分数 {abs(signal.score):.1f} < "
            f"动态阈值 {signal.threshold:.1f}，不开单"
        )
        if signal.reason.endswith(suffix):
            return signal.reason[: -len(suffix)]
        return signal.reason

    def _load_latest_daily_profile_selection(self) -> dict | None:
        if not self.storage:
            return None
        loader = getattr(self.storage, "load_latest_daily_profile_selection", None)
        return loader(self.symbol) if loader is not None else None

    @staticmethod
    def _selection_is_effective(snapshot: dict | None, current_time: int) -> bool:
        if not snapshot:
            return False
        return (
            snapshot.get("status") in {"READY", "FALLBACK"}
            and int(snapshot.get("effective_from", 0)) <= current_time
            and int(snapshot.get("effective_until", 0)) > current_time
        )

    @staticmethod
    def _daily_profile_fallback(
        previous: dict | None,
        target: dict,
        evaluated_at: int,
        error: str,
    ) -> dict:
        if not previous:
            return {
                "version": "DPS-FAILED",
                "status": "FAILED",
                "evaluated_at": evaluated_at,
                **target,
                "selected_profiles": [],
                "selected_count": 0,
                "reason": f"每日画像评估失败，沿用静态基准：{error}",
                "error": error,
            }
        fallback = dict(previous)
        fallback.update(
            {
                "version": f"{previous.get('version', 'DPS')}-FALLBACK",
                "status": "FALLBACK",
                "evaluated_at": evaluated_at,
                **target,
                "reason": f"每日画像评估失败，沿用上一版本：{error}",
                "error": error,
            }
        )
        return fallback

    def _observation_profile_promoted_signal(
        self,
        signal: Signal,
        latest: Kline,
        gate_code: str,
    ) -> Signal | None:
        if not self.enable_observation_profile_promotion or gate_code != "SESSION_BLOCKED":
            return None
        if not signal.actionable:
            return None
        direction = signal.direction.upper()
        if direction not in {"LONG", "SHORT"}:
            return None
        if direction == "SHORT" and signal.threshold_segment.upper() not in self.live_short_segments:
            return None

        edge = abs(signal.score) - signal.threshold
        if edge < self.observation_profile_min_edge:
            return None
        if edge >= max_trade_edge_for(signal.timeframe_minutes, signal.threshold_segment, direction):
            return None

        profile = self._observation_profile(signal, direction, latest.close_time)
        if (
            profile["sample_size"] < self.observation_profile_min_samples
            or profile["win_rate"] < self.observation_profile_min_win_rate
            or profile["ev"] < self.observation_profile_min_ev
        ):
            return None

        profile_key = f"{signal.strategy_family}|{direction}|{signal.threshold_segment}"
        return replace(
            signal,
            direction=direction,
            reason=(
                f"{signal.reason}；观察画像放行 "
                f"{profile_key} N{profile['sample_size']} "
                f"胜率{profile['win_rate']:.2%} EV{profile['ev']:.2f}U"
            ),
            session_allowed=True,
            session_sample_size=profile["sample_size"],
            session_win_rate=profile["win_rate"],
            session_ev=profile["ev"],
            session_edge_min=self.observation_profile_min_edge,
            observe_only=False,
            profile_key=profile_key,
            candidate_origin="PROFILE_PROMOTED_WAIT",
        )

    def _observation_profile(self, signal: Signal, direction: str, current_time: int) -> dict:
        cutoff = current_time - self.observation_profile_lookback_days * DAY_MS
        matching = [
            item
            for item in self.observations
            if item.status == "SETTLED"
            and item.result in {"WIN", "LOSS"}
            and item.settled_at is not None
            and item.settled_at <= current_time
            and item.opened_at >= cutoff
            and item.timeframe_minutes == signal.timeframe_minutes
            and item.strategy_family == signal.strategy_family
            and item.direction == direction
            and item.threshold_segment == signal.threshold_segment
        ]
        samples = []
        next_independent_at = 0
        for item in sorted(matching, key=lambda row: (row.opened_at, row.observation_key)):
            if item.opened_at < next_independent_at:
                continue
            samples.append(item)
            next_independent_at = item.expires_at
        wins = sum(1 for item in samples if item.result == "WIN")
        pnl = sum(float(item.pnl) for item in samples)
        sample_size = len(samples)
        return {
            "sample_size": sample_size,
            "wins": wins,
            "losses": sample_size - wins,
            "win_rate": wins / sample_size if sample_size else 0.0,
            "pnl": round(pnl, 4),
            "ev": round(pnl / sample_size, 4) if sample_size else 0.0,
        }

    def _should_record_observation(self, signal: Signal) -> bool:
        if signal.observe_only or signal.observe_direction:
            return True
        return signal.direction == "SHORT"

    def _new_observation(
        self,
        signal: Signal,
        latest: Kline,
        decision: str,
    ) -> ObservationSignal | None:
        if not signal.quality_score_version:
            signal = self._attach_quality_score(signal, current_time=latest.close_time)
        direction = signal.observe_direction or signal.direction
        if direction not in {"LONG", "SHORT"}:
            return None
        key = self._observation_key(signal, direction)
        existing = next((item for item in self.observations if item.observation_key == key), None)
        if existing is not None:
            return None
        overlapping = next(
            (
                item
                for item in self.observations
                if item.status == "OPEN"
                and item.timeframe_minutes == signal.timeframe_minutes
                and item.strategy_family == signal.strategy_family
                and item.direction == direction
                and item.threshold_segment == signal.threshold_segment
                and latest.close_time < item.expires_at
            ),
            None,
        )
        if overlapping is not None:
            return None
        return ObservationSignal(
            observation_key=key,
            strategy_family=signal.strategy_family,
            strategy_tag=signal.strategy_tag,
            direction=direction,
            timeframe_minutes=signal.timeframe_minutes,
            level=signal.level,
            reason=signal.reason,
            entry_price=latest.close,
            opened_at=latest.close_time,
            expires_at=latest.close_time + signal.timeframe_minutes * 60_000,
            threshold_segment=signal.threshold_segment,
            score=signal.score,
            threshold=signal.threshold,
            edge=round(abs(signal.score) - signal.threshold, 4),
            regime=signal.regime,
            source_decision=decision,
            observe_only=True,
            wave_state=signal.wave_state,
            wave_raw_state=signal.wave_raw_state,
            wave_window=signal.wave_window,
            wave_efficiency=signal.wave_efficiency,
            wave_direction_ratio=signal.wave_direction_ratio,
            wave_atr_strength=signal.wave_atr_strength,
            wave_confirmations=signal.wave_confirmations,
            wave_confirmed_at=signal.wave_confirmed_at,
            wave_batch_id=signal.wave_batch_id,
            wave_guard_mode=signal.wave_guard_mode,
            wave_guard_status=signal.wave_guard_status,
            wave_guard_reason=signal.wave_guard_reason,
            profile_key=signal.profile_key,
            daily_profile_version=signal.daily_profile_version,
            order_slot=signal.order_slot,
            order_slot_scope=signal.order_slot_scope,
            quality_score=signal.quality_score,
            quality_score_version=signal.quality_score_version,
            quality_score_mode=signal.quality_score_mode,
            quality_score_context=signal.quality_score_context,
            quality_score_components=dict(signal.quality_score_components),
            quality_score_inputs=dict(signal.quality_score_inputs),
            direction_pulse_shadow=dict(signal.direction_pulse_shadow),
            profile_health_status=signal.profile_health_status,
            profile_health_sample_size=signal.profile_health_sample_size,
            profile_health_win_rate=signal.profile_health_win_rate,
            profile_health_ev=signal.profile_health_ev,
            profile_health_evaluated_at=signal.profile_health_evaluated_at,
        )

    def _record_observation(
        self,
        signal: Signal,
        latest: Kline,
        decision: str,
        *,
        candidate_origin: str = "RESEARCH_OBSERVATION",
        candidate_ordinal: int = 1,
        primary_decision: bool = False,
        final_reason: str | None = None,
    ) -> bool:
        observation = self._new_observation(signal, latest, decision)
        if observation is None:
            return False
        direction = observation.direction
        audit_context = (
            self._current_signal_audit_context()
            if primary_decision
            else self._observation_signal_audit_context(signal, direction)
        )
        event_kind = "DECISIVE_BLOCK" if primary_decision else "OBSERVATION_CANDIDATE"
        config, context, enriched_signal, audit = self._decision_artifacts(
            replace(signal, direction=direction),
            latest,
            decision,
            final_reason=signal.reason if final_reason is None else final_reason,
            candidate_origin=candidate_origin,
            candidate_ordinal=candidate_ordinal,
            observation_allowed=True,
            audit_context=audit_context,
            event_kind=event_kind,
        )
        context_payload = context.to_dict()
        observation = replace(
            observation,
            decision_id=context.decision_id,
            context_version=context.context_version,
            runtime_config_hash=context.runtime_config_hash,
            strategy_build_id=context.strategy_build_id,
            candidate_origin=context.candidate_origin,
            decision_inputs=context_payload["inputs"],
            decision_trace=context_payload["decision_trace"],
            first_decisive_block=context.first_decisive_block,
        )
        self.observations.append(observation)
        if self.storage:
            try:
                self.storage.save_decision_bundle(
                    config=config,
                    context=context,
                    audit=audit,
                    observation=replace(observation),
                )
            except Exception as exc:  # noqa: BLE001 - 核心决策包失败必须回滚观察。
                self.observations.remove(observation)
                self._decision_storage_failed = True
                self._set_storage_error("决策持久化失败", exc)
                return False
            self._bundled_decision_ids.add(context.decision_id)
        if primary_decision:
            self.selected_signal = enriched_signal
        if self._observation_audit_collector is not None:
            self._observation_audit_collector.append(
                (
                    enriched_signal,
                    observation,
                    str(decision or "RESEARCH_OBSERVE"),
                    audit_context,
                )
            )
        return True

    def _record_observation_candidates(self, signals: Sequence[Signal], latest: Kline) -> None:
        for candidate_ordinal, signal in enumerate(signals, start=1):
            if self._has_open_research_observation(signal, latest):
                continue
            self._record_observation(
                signal,
                latest,
                "RESEARCH_OBSERVE",
                candidate_origin="RESEARCH_OBSERVATION",
                candidate_ordinal=candidate_ordinal,
            )

    def _has_open_research_observation(self, signal: Signal, latest: Kline) -> bool:
        direction = signal.observe_direction or signal.direction
        if direction not in {"LONG", "SHORT"}:
            return False
        for observation in self.observations:
            if observation.status != "OPEN":
                continue
            if observation.strategy_tag != signal.strategy_tag or observation.direction != direction:
                continue
            if latest.close_time < observation.expires_at:
                return True
        return False

    def _settle_observations(
        self,
        current_time: int,
        current_price: float,
        klines: Sequence[Kline] | None = None,
    ) -> list[ObservationSignal]:
        ordered_klines = sorted(klines or [], key=lambda item: item.close_time)
        close_times = [item.close_time for item in ordered_klines]
        settled = []
        previous_states = []
        for observation in self.observations:
            if observation.status != "OPEN" or current_time < observation.expires_at:
                continue
            settled_at = current_time
            exit_price = current_price
            if ordered_klines:
                index = bisect_left(close_times, observation.expires_at)
                if index >= len(ordered_klines):
                    continue
                exit_kline = ordered_klines[index]
                if exit_kline.close_time != observation.expires_at:
                    continue
                settled_at = exit_kline.close_time
                exit_price = exit_kline.close
            elif current_time != observation.expires_at:
                continue
            previous_states.append((observation, deepcopy(observation.__dict__)))
            won = self._is_observation_win(observation.direction, observation.entry_price, exit_price)
            observation.status = "SETTLED"
            observation.result = "WIN" if won else "LOSS"
            observation.exit_price = exit_price
            observation.settled_at = settled_at
            observation.pnl = 8.0 if won else -10.0
            settled.append(observation)
        if settled:
            persisted = True
            if self.storage:
                try:
                    save_many = getattr(self.storage, "save_observations", None)
                    if save_many is not None:
                        save_many([replace(item) for item in settled], self.symbol)
                    else:
                        for item in settled:
                            self.storage.save_observation(replace(item), self.symbol)
                except Exception as exc:  # noqa: BLE001 - 影子持久化失败不阻断开单。
                    self.record_error(f"方向脉冲观察结算持久化失败: {exc}")
                    persisted = False
            if persisted:
                self._refresh_direction_pulse_shadow(current_time)
            else:
                for observation, previous_state in previous_states:
                    observation.__dict__.clear()
                    observation.__dict__.update(previous_state)
                settled = []
        return settled

    def _load_restored_observations(self) -> list[ObservationSignal]:
        if not self.storage:
            return []
        profile_loader = getattr(self.storage, "load_observations_for_profile", None)
        if profile_loader is not None:
            return profile_loader(
                self.symbol,
                lookback_days=max(
                    self.observation_profile_lookback_days,
                    self.daily_profile_selector_config.lookback_days,
                ),
            )
        return self.storage.load_observations(self.symbol)

    def _load_direction_pulse_history(
        self,
        fallback: Sequence[ObservationSignal],
    ) -> list[ObservationSignal]:
        if not self.storage:
            return list(fallback)
        loader = getattr(self.storage, "load_observations", None)
        if loader is None:
            return list(fallback)
        try:
            try:
                return loader(self.symbol, limit=5000)
            except TypeError:
                return loader(self.symbol)
        except Exception:  # noqa: BLE001 - 失败时退回正式画像已经加载的观察历史。
            return list(fallback)

    def _direction_pulse_source_observations(self) -> list[ObservationSignal]:
        merged: dict[str, ObservationSignal] = {}
        for index, observation in enumerate(
            [*self._direction_pulse_history, *self.observations]
        ):
            key = str(getattr(observation, "observation_key", "") or f"row-{index}")
            merged[key] = observation
        return list(merged.values())

    def _load_wave_runtime(self) -> dict | None:
        loader = getattr(self.storage, "load_wave_runtime", None) if self.storage else None
        if loader is None:
            return None
        runtime = loader(self.symbol)
        if not runtime or not isinstance(runtime.get("snapshot"), WaveSnapshot):
            return None
        if int(runtime.get("evaluated_at", 0)) <= 0:
            return None
        return runtime

    def _bootstrap_wave_anchor(
        self,
        snapshot: WaveSnapshot,
        evaluated_at: int,
    ) -> WaveSnapshot:
        if snapshot.state not in {"UP_LEG", "DOWN_LEG", "RANGE_HIGH", "RANGE_LOW"}:
            return snapshot
        candidates = []
        for order in self.simulator.orders:
            if order.wave_guard_mode == "RECOVERY" or order.wave_state != snapshot.state:
                continue
            anchor = int(order.wave_confirmed_at or 0)
            if anchor <= 0 and order.wave_batch_id:
                try:
                    anchor = int(order.wave_batch_id.split("|", 1)[0])
                except ValueError:
                    anchor = 0
            if 0 < anchor <= evaluated_at:
                candidates.append((order.opened_at, order.id, anchor))
        if not candidates:
            return snapshot
        return replace(snapshot, confirmed_at=max(candidates)[2])

    def _persist_wave_runtime(
        self,
        snapshot: WaveSnapshot,
        evaluated_at: int,
    ) -> bool:
        saver = getattr(self.storage, "save_wave_runtime", None) if self.storage else None
        if saver is None:
            return True
        try:
            saver(self.symbol, snapshot, evaluated_at)
        except Exception as exc:  # noqa: BLE001 - 波段锚点失败时禁止继续开单。
            self._set_storage_error("波段运行态持久化失败", exc)
            return False
        return True

    @staticmethod
    def _is_observation_win(direction: str, entry_price: float, current_price: float) -> bool:
        if direction == "LONG":
            return current_price > entry_price
        if direction == "SHORT":
            return current_price < entry_price
        return False

    @staticmethod
    def _observation_key(signal: Signal, direction: str) -> str:
        return f"{signal.open_time}|{signal.timeframe_minutes}|{direction}|{signal.strategy_tag}"

    def _rolling_edge_status(self, signal: Signal, latest: Kline) -> dict:
        current_item = {
            "entry_time": latest.close_time,
            "timeframe_minutes": signal.timeframe_minutes,
            "threshold_segment": signal.threshold_segment,
            "reason": signal.reason,
        }
        edge_orders = self.simulator.orders
        if self.enable_stake_progression:
            edge_orders = [
                {
                    **order.to_dict(),
                    "pnl": (
                        round(self.simulator.win_return - self.simulator.stake, 4)
                        if order.result == "WIN"
                        else round(-self.simulator.stake, 4)
                        if order.result == "LOSS"
                        else order.pnl
                    ),
                }
                for order in self.simulator.orders
            ]
        snapshot = rolling_edge_snapshot(edge_orders, current_item, self.rolling_edge_config)
        degraded = should_degrade(snapshot, self.rolling_edge_config)
        return self._rolling_edge_to_dict(snapshot, degraded, self.enable_rolling_edge_guard)

    def _rolling_edge_to_dict(
        self,
        snapshot: RollingEdgeSnapshot,
        degraded: bool,
        guard_enabled: bool,
    ) -> dict:
        return {
            "observe_only": not guard_enabled,
            "status": "DEGRADED" if degraded else "NORMAL",
            "code": "ROLLING_EDGE_BLOCKED" if degraded else "ROLLING_EDGE_NORMAL",
            "blocked": bool(guard_enabled and degraded),
            "key": snapshot.key,
            "sample_size": snapshot.sample_size,
            "wins": snapshot.wins,
            "losses": snapshot.losses,
            "win_rate": snapshot.win_rate,
            "pnl": snapshot.pnl,
            "ev": snapshot.ev,
            "edge": snapshot.ev,
            "threshold": self.rolling_edge_config.min_ev,
        }

    def _save_order(self, order) -> None:
        order_snapshot = replace(order)
        symbol = self.symbol
        self._submit_storage_write(
            lambda order=order_snapshot, symbol=symbol: self.storage.save_order(order, symbol)
        )

    def _cancel_pending_progression_credits(
        self,
        source_order_ids: Sequence[int] | set[int] | None = None,
        direction: str = "",
    ) -> bool:
        pending = self.simulator.stake_progression.pending_credits(
            source_order_ids,
            direction,
        )
        if not pending:
            return True
        cancelled_snapshots = [replace(credit, status="CANCELLED") for credit in pending]
        if self.storage:
            canceler = getattr(self.storage, "cancel_stake_progression_credits", None)
            if canceler is None:
                self._set_storage_error(
                    "资格取消持久化失败",
                    RuntimeError("storage does not support atomic credit cancellation"),
                )
                return False
            try:
                canceler(self.symbol, cancelled_snapshots)
            except Exception as exc:  # noqa: BLE001 - 未持久化前不得修改内存资格。
                self._set_storage_error("资格取消持久化失败", exc)
                return False
        self.simulator.stake_progression.cancel_pending(source_order_ids, direction)
        return True

    def _stale_progression_credit_source_ids(self, signal: Signal) -> set[int]:
        direction = self._signal_direction(signal)
        if not direction:
            return set()
        pending = self.simulator.stake_progression.pending_credits(
            direction=direction
        )
        if not pending:
            return set()
        if self._wave_bootstrap_cancel_pending:
            return {credit.source_order_id for credit in pending}
        if not self.enable_wave_guard or signal.wave_window <= 0:
            return set()
        if signal.wave_state not in {"UP_LEG", "DOWN_LEG", "RANGE_HIGH", "RANGE_LOW"}:
            return {credit.source_order_id for credit in pending}
        if signal.wave_confirmed_at <= 0:
            return {credit.source_order_id for credit in pending}

        current_profile = str(
            (self.active_daily_profile_selection or {}).get("version", "") or "STATIC"
        )
        orders_by_id = {order.id: order for order in self.simulator.orders}
        stale = set()
        for credit in pending:
            source = orders_by_id.get(credit.source_order_id)
            if source is None:
                stale.add(credit.source_order_id)
                continue
            source_profile = source.daily_profile_version
            if not source_profile and "|" in source.wave_batch_id:
                source_profile = source.wave_batch_id.rsplit("|", 1)[-1]
            source_profile = source_profile or "STATIC"
            if (
                source.wave_state != signal.wave_state
                or source.wave_confirmed_at != signal.wave_confirmed_at
                or source_profile != current_profile
            ):
                stale.add(credit.source_order_id)
        return stale

    @staticmethod
    def _signal_direction(signal: Signal) -> str:
        for value in (signal.direction, signal.observe_direction):
            direction = str(value or "").upper()
            if direction in {"LONG", "SHORT"}:
                return direction
        return ""

    def _save_settled_order_with_credit(self, order, credit) -> None:
        order_snapshot = replace(order)
        credit_snapshot = replace(credit) if credit is not None else None
        symbol = self.symbol
        self._submit_storage_write(
            lambda order=order_snapshot, symbol=symbol, credit=credit_snapshot: (
                self.storage.save_settled_order_with_credit(order, symbol, credit)
            )
        )

    def _save_open_order_with_credit(self, order, credit) -> None:
        order_snapshot = replace(order)
        credit_snapshot = replace(credit) if credit is not None else None
        symbol = self.symbol
        self._submit_storage_write(
            lambda order=order_snapshot, symbol=symbol, credit=credit_snapshot: (
                self.storage.save_open_order_with_credit(order, symbol, credit)
            )
        )

    def _save_signal(
        self,
        signal: Signal,
        decision: str,
        created_at_ms: int,
        *,
        has_formal_candidate: bool = False,
        force_independent: bool = False,
        event_kind: str | None = None,
        audit_context_override: dict | None = None,
    ) -> None:
        symbol = self.symbol
        audit_context = deepcopy(
            audit_context_override
            if audit_context_override is not None
            else self._current_signal_audit_context()
        )
        signal_snapshot = deepcopy(signal)
        self._submit_storage_write(
            lambda signal=signal_snapshot, symbol=symbol, decision=decision, created_at_ms=created_at_ms,
            audit_context=audit_context: (
                self.storage.save_signal(
                    symbol,
                    signal,
                    decision,
                    created_at_ms,
                    audit_context=audit_context,
                    has_formal_candidate=has_formal_candidate,
                    force_independent=force_independent,
                    event_kind=event_kind,
                )
            )
        )

    def _current_signal_audit_context(self) -> dict:
        return {
            "rolling_edge": dict(self.rolling_edge),
            "result_sequence_guard": dict(self.result_sequence_guard),
            "wave_batch_guard": dict(self.wave_batch_guard),
            "profile_degradation_guard": dict(self.profile_degradation_guard),
            "profile_health_guard": dict(self.profile_health_guard),
            "time_period_guard": dict(self.time_period_guard),
            "profile_guard": dict(self.profile_guard_audit),
        }

    def _observation_signal_audit_context(
        self,
        signal: Signal,
        direction: str,
    ) -> dict:
        not_evaluated = "NOT_EVALUATED"
        time_period = self.time_period_guard
        return {
            "rolling_edge": {
                "status": not_evaluated,
                "code": not_evaluated,
                "blocked": False,
                "key": "|".join(
                    [
                        str(signal.timeframe_minutes),
                        str(signal.threshold_segment or "GLOBAL"),
                        str(signal.reason or "UNKNOWN").split("：", 1)[0]
                        .split(";", 1)[0]
                        .strip()
                        or "UNKNOWN",
                    ]
                ),
            },
            "result_sequence_guard": {
                "status": not_evaluated,
                "code": not_evaluated,
                "blocked": False,
                "scope": self.result_sequence_guard_config.scope,
                "direction": direction,
            },
            "wave_batch_guard": {
                "status": not_evaluated,
                "code": not_evaluated,
                "mode": not_evaluated,
                "blocked": False,
                "current_batch_id": str(signal.wave_batch_id or ""),
            },
            "profile_degradation_guard": {
                "status": not_evaluated,
                "code": not_evaluated,
                "blocked": False,
                "profile_key": str(signal.profile_key or ""),
                "daily_profile_version": str(signal.daily_profile_version or ""),
            },
            "profile_health_guard": {
                "status": not_evaluated,
                "code": not_evaluated,
                "blocked": False,
                "direction": direction,
            },
            "time_period_guard": {
                "status": not_evaluated,
                "code": not_evaluated,
                "enabled": bool(time_period.get("enabled", False)),
                "blocked": False,
                "local_hour": int(time_period.get("local_hour", 0)),
                "window": str(time_period.get("window", "")),
            },
            "profile_guard": {
                "status": not_evaluated,
                "code": not_evaluated,
                "enabled": self.enable_profile_guard,
                "observe_only": not self.enable_profile_guard,
                "blocked": False,
                "hit_keys": [],
            },
        }

    def _save_observation(self, observation: ObservationSignal) -> None:
        observation_snapshot = replace(observation)
        symbol = self.symbol
        self._submit_storage_write(
            lambda observation=observation_snapshot, symbol=symbol: (
                self.storage.save_observation(observation, symbol)
            )
        )

    def _capture_entry_snapshot_context(self, signal: Signal) -> dict:
        profile_source = self._profile_guard_shadow_source()
        profile_guard = profile_guard_shadow(
            signal,
            profile_source,
            use_recommended=True,
        )
        profile_guard_default = profile_guard_shadow(
            signal,
            profile_source,
            use_recommended=False,
        )
        progression = self._stake_progression_status()
        return {
            "rolling_edge": dict(self.rolling_edge),
            "result_sequence_guard": dict(self.result_sequence_guard),
            "wave_batch_guard": dict(self.wave_batch_guard),
            "wave_state": self._wave_state_to_dict(),
            "profile_guard_shadow": profile_guard,
            "profile_guard_default_shadow": profile_guard_default,
            "profile_guard_selection_policy": profile_guard.get("selection_policy") or {},
            "profile_guard_config": self._profile_guard_config(),
            "observation_profile_promotion": self._observation_profile_promotion_config(),
            "daily_profile_selection": self._daily_profile_selector_status(),
            "fear_greed": self.fear_greed.to_dict() if self.fear_greed else None,
            "stake_progression": progression,
            "stake_config": {
                "stake": self.stake,
                "win_return": self.win_return,
                "stake_progression_enabled": self.enable_stake_progression,
                "stake_progression_max_orders": 2,
                "stake_progression_max_active": progression["max_active"],
            },
            "order_policy": self._order_policy_status(),
            "trade_score_threshold": self._trade_score_threshold_status(),
        }

    def _build_order_entry_snapshot(
        self,
        order,
        signal: Signal,
        latest: Kline,
        captured: dict,
    ) -> dict:
        snapshot = deepcopy(captured)
        snapshot["signal"] = signal.to_dict()
        snapshot["latest_kline"] = latest.to_dict()
        snapshot["stake_progression_source_order_id"] = (
            order.stake_progression_source_order_id
        )
        snapshot["stake_progression_version"] = order.stake_progression_version
        return snapshot

    def _save_order_entry_snapshot(self, order, signal: Signal, latest: Kline) -> None:
        order_snapshot = replace(order)
        symbol = self.symbol
        captured = self._capture_entry_snapshot_context(signal)
        entry_snapshot = self._build_order_entry_snapshot(
            order_snapshot,
            signal,
            latest,
            captured,
        )
        self._submit_storage_write(
            lambda order=order_snapshot, symbol=symbol, snapshot=entry_snapshot: (
                self.storage.save_order_entry_snapshot(order, symbol, snapshot)
            )
        )

    def _profile_guard_shadow(self, signal: Signal) -> dict:
        return profile_guard_shadow(
            signal,
            self._profile_guard_shadow_source(),
            use_recommended=True,
        )

    def _profile_guard_default_shadow(self, signal: Signal) -> dict:
        return profile_guard_shadow(
            signal,
            self._profile_guard_shadow_source(),
            use_recommended=False,
        )

    def _profile_guard_shadow_source(self) -> dict | None:
        if self._profile_source_cached:
            return self._cached_profile_source
        self._profile_source_cached = True
        if not self.storage:
            self._cached_profile_source = None
            return None
        try:
            snapshot_loader = getattr(self.storage, "profile_summary_snapshot", None)
            if snapshot_loader is not None:
                self._cached_profile_source = snapshot_loader(
                    self.symbol,
                    profile_guard_min_history=self.profile_guard_min_history,
                    profile_guard_min_group_size=self.profile_guard_min_group_size,
                )
            elif hasattr(self.storage, "order_profile_summary"):
                self._cached_profile_source = self.storage.order_profile_summary(
                    self.symbol,
                    profile_guard_min_history=self.profile_guard_min_history,
                    profile_guard_min_group_size=self.profile_guard_min_group_size,
                )
            else:
                self._cached_profile_source = None
            return self._cached_profile_source
        except Exception:  # noqa: BLE001 - 画像守卫仅用于复盘，不能影响开单保存。
            self._cached_profile_source = None
            return None

    def _profile_guard_config(self) -> dict:
        return {
            "enabled": self.enable_profile_guard,
            "observe_only": not self.enable_profile_guard,
            "min_history": self.profile_guard_min_history,
            "min_group_size": self.profile_guard_min_group_size,
        }

    def _observation_profile_promotion_config(self) -> dict:
        return {
            "enabled": self.enable_observation_profile_promotion,
            "lookback_days": self.observation_profile_lookback_days,
            "min_samples": self.observation_profile_min_samples,
            "min_win_rate": self.observation_profile_min_win_rate,
            "min_ev": self.observation_profile_min_ev,
            "min_edge": self.observation_profile_min_edge,
            "live_short_segments": sorted(self.live_short_segments),
        }

    def _daily_profile_selector_status(self) -> dict:
        latest = self.daily_profile_selection or {}
        active = self.active_daily_profile_selection or {}
        latest_is_active = bool(
            latest
            and active
            and latest.get("version") == active.get("version")
            and latest.get("effective_from") == active.get("effective_from")
        )
        return {
            "enabled": self.enable_daily_profile_selector,
            "status": latest.get("status", "DISABLED" if not self.enable_daily_profile_selector else "PENDING"),
            "version": active.get("version") or latest.get("version", ""),
            "evaluated_at": latest.get("evaluated_at"),
            "lookback_start": latest.get("lookback_start"),
            "lookback_end": latest.get("lookback_end"),
            "effective_from": active.get("effective_from") or latest.get("effective_from"),
            "effective_until": active.get("effective_until") or latest.get("effective_until"),
            "selected_profiles": list(active.get("selected_profiles", [])),
            "selected_count": len(active.get("selected_profiles", [])),
            "pending_profiles": list(latest.get("selected_profiles", [])) if not latest_is_active else [],
            "reason": latest.get("reason", ""),
            "error": latest.get("error"),
            "config": {
                **self.daily_profile_selector_config.__dict__,
            },
        }

    def _trade_score_threshold_status(self) -> dict:
        return {
            "mode": "AUTO" if self.trade_score_threshold is None else "AUDIT_ONLY",
            "value": self.trade_score_threshold,
        }

    def _order_policy_status(self) -> dict:
        last_opened = (
            self._last_order_opened_at
            if isinstance(self._last_order_opened_at, dict)
            else {"LONG": self._last_order_opened_at, "SHORT": self._last_order_opened_at}
        )
        return {
            "max_open_orders": self.order_policy.max_open_orders,
            "max_open_long_orders": self.order_policy.max_open_long_orders,
            "max_open_short_orders": self.order_policy.max_open_short_orders,
            "min_order_gap_ms": self.order_policy.min_order_gap_ms,
            "by_direction": {
                direction: {
                    "last_opened_at": last_opened.get(direction),
                    "next_allowed_at": (
                        int(last_opened[direction]) + self.order_policy.min_order_gap_ms
                        if last_opened.get(direction) is not None
                        else None
                    ),
                }
                for direction in ("LONG", "SHORT")
            },
        }

    def _update_order_entry_snapshot_settlement(
        self,
        order,
        *,
        symbol: str | None = None,
    ) -> None:
        order_snapshot = replace(order)
        captured_symbol = (symbol or self.symbol).upper()
        self._submit_storage_write(
            lambda order=order_snapshot, symbol=captured_symbol: (
                self.storage.update_order_entry_snapshot_settlement(order, symbol)
            )
        )

    def _stake_progression_status(self) -> dict:
        status = self.simulator.stake_progression.status()
        status["recovery_warning"] = self.stake_progression_recovery_warning
        return status

    def _submit_storage_write(self, func) -> None:
        if not self.storage:
            return
        with self._storage_futures_condition:
            future = self._storage_executor.submit(func)
            self._storage_futures.append(future)
        future.add_done_callback(self._storage_write_completed)

    def _storage_write_completed(self, future: Future) -> None:
        try:
            error = future.exception()
        except Exception as exc:  # noqa: BLE001 - 已取消任务也必须完成回收与上报。
            error = exc
        if error is not None:
            self.record_error(f"异步存储写入失败: {error}")
        with self._storage_futures_condition:
            if error is not None:
                self._storage_write_failures.append(error)
                self._storage_write_failures = self._storage_write_failures[-10:]
            if future in self._storage_futures:
                self._storage_futures.remove(future)
            self._storage_futures_condition.notify_all()

    def wait_for_storage_writes(self) -> None:
        with self._storage_futures_condition:
            while self._storage_futures:
                self._storage_futures_condition.wait()
            failures = list(self._storage_write_failures)
            self._storage_write_failures.clear()
        if failures:
            raise failures[0]

    def _empty_rolling_edge(self) -> dict:
        return {
            "observe_only": not self.enable_rolling_edge_guard,
            "status": "UNKNOWN",
            "code": "ROLLING_EDGE_UNKNOWN",
            "blocked": False,
            "key": "",
            "sample_size": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "pnl": 0.0,
            "ev": 0.0,
            "edge": 0.0,
            "threshold": self.rolling_edge_config.min_ev,
        }

    def _empty_result_sequence_guard(self) -> dict:
        config = self.result_sequence_guard_config
        return {
            "enabled": config.enabled,
            "status": "NORMAL" if config.enabled else "DISABLED",
            "code": "RESULT_SEQUENCE_NORMAL" if config.enabled else "DISABLED",
            "blocked": False,
            "scope": config.scope,
            "loss_streak": config.loss_streak,
            "cooldown_minutes": config.cooldown_minutes,
            "direction": "",
            "consecutive_losses": 0,
            "last_settled_at": 0,
            "pause_until": 0,
            "paused_directions": [],
            "reason": "",
        }

    def _empty_profile_degradation_guard(self) -> dict:
        config = self.profile_degradation_guard_config
        return {
            "enabled": config.cooldown_minutes > 0,
            "status": "NORMAL" if config.cooldown_minutes > 0 else "DISABLED",
            "code": (
                "PROFILE_DEGRADATION_NORMAL"
                if config.cooldown_minutes > 0
                else "DISABLED"
            ),
            "blocked": False,
            "cooldown_minutes": config.cooldown_minutes,
            "profile_key": "",
            "daily_profile_version": "",
            "consecutive_losses": 0,
            "last_loss_settled_at": 0,
            "pause_until": 0,
            "probe_order_id": 0,
            "triggered_at": 0,
            "allow_progression": True,
            "reason": "",
        }

    def _empty_profile_health_guard(self) -> dict:
        enabled = self.profile_health_guard_config.enabled
        return {
            "enabled": enabled,
            "status": "NOT_APPLICABLE" if enabled else "DISABLED",
            "code": "PROFILE_HEALTH_NOT_APPLICABLE" if enabled else "DISABLED",
            "direction": "",
            "evaluated_at": 0,
            "next_evaluation_at": 0,
            "lookback_start": 0,
            "lookback_end": 0,
            "sample_size": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "pnl": 0.0,
            "ev": 0.0,
            "blocked": False,
            "allow_second_order": True,
            "allow_progression": True,
            "lookback_hours": LOOKBACK_HOURS,
            "evaluation_interval_hours": EVALUATION_INTERVAL_HOURS,
            "min_samples": MIN_SAMPLES,
            "watch_min_win_rate": WATCH_MIN_WIN_RATE,
            "healthy_min_win_rate": HEALTHY_MIN_WIN_RATE,
            "reason": (
                "等待已启用每日画像候选"
                if enabled
                else "画像短窗健康守卫已关闭"
            ),
        }

    def _profile_health_guard_to_dict(
        self,
        decision: ProfileHealthGuardDecision,
    ) -> dict:
        return {
            **self._empty_profile_health_guard(),
            **decision.to_dict(),
            "code": (
                "PROFILE_HEALTH_BLOCKED"
                if decision.blocked
                else f"PROFILE_HEALTH_{decision.status}"
            ),
        }

    def _refresh_profile_health_guard(
        self,
        signal: Signal,
        current_time: int,
    ) -> ProfileHealthGuardDecision:
        decision = evaluate_profile_health_guard(
            self.observations,
            current_time=current_time,
            direction=self._signal_direction(signal),
            selected_profiles=(
                (self.active_daily_profile_selection or {}).get(
                    "selected_profiles",
                    [],
                )
                if signal.daily_profile_selected
                else []
            ),
            config=self.profile_health_guard_config,
        )
        self.profile_health_guard = self._profile_health_guard_to_dict(decision)
        return decision

    def _profile_degradation_guard_to_dict(
        self,
        decision: ProfileDegradationGuardDecision,
    ) -> dict:
        return {
            **self._empty_profile_degradation_guard(),
            "status": decision.status,
            "code": (
                "PROFILE_DEGRADATION_BLOCKED"
                if decision.blocked
                else f"PROFILE_DEGRADATION_{decision.status}"
            ),
            "blocked": decision.blocked,
            "profile_key": decision.profile_key,
            "daily_profile_version": decision.daily_profile_version,
            "consecutive_losses": decision.consecutive_losses,
            "last_loss_settled_at": decision.last_loss_settled_at,
            "pause_until": decision.pause_until,
            "probe_order_id": decision.probe_order_id,
            "triggered_at": decision.triggered_at,
            "allow_progression": decision.allow_progression,
            "reason": decision.reason,
        }

    def _refresh_profile_degradation_guard(
        self,
        signal: Signal,
        current_time: int,
    ) -> ProfileDegradationGuardDecision:
        if not (
            signal.daily_profile_selected
            and signal.profile_key
            and signal.daily_profile_version
        ):
            self.profile_degradation_guard = self._empty_profile_degradation_guard()
            return ProfileDegradationGuardDecision(
                status=self.profile_degradation_guard["status"]
            )
        decision = evaluate_profile_degradation_guard(
            self.simulator.orders,
            current_time=current_time,
            profile_key=signal.profile_key,
            daily_profile_version=signal.daily_profile_version,
            config=self.profile_degradation_guard_config,
        )
        self.profile_degradation_guard = self._profile_degradation_guard_to_dict(decision)
        return decision

    def _wave_state_to_dict(self) -> dict:
        return {
            "enabled": self.enable_wave_guard,
            "state": self.wave_state.state,
            "raw_state": self.wave_state.raw_state,
            "window": self.wave_state.window,
            "efficiency": self.wave_state.efficiency,
            "direction_ratio": self.wave_state.direction_ratio,
            "atr_strength": self.wave_state.atr_strength,
            "range_position": self.wave_state.range_position,
            "confirmations": self.wave_state.confirmations,
            "confirmed_at": self.wave_state.confirmed_at,
            "allowed_directions": list(self.wave_state.allowed_directions),
        }

    def _empty_wave_batch_guard(self) -> dict:
        config = self.wave_batch_guard_config
        return {
            "enabled": config.enabled,
            "code": "WAVE_BATCH_GUARD_PENDING",
            "status": "PENDING" if config.enabled else "DISABLED",
            "mode": "PENDING" if config.enabled else "DISABLED",
            "blocked": False,
            "allow_progression": True,
            "current_batch_id": "",
            "batch_orders": 0,
            "batch_wins": 0,
            "batch_losses": 0,
            "failed_batches": 0,
            "pause_until": 0,
            "reason": "",
            "config": {
                "batch_size": config.batch_size,
                "failed_batches_for_cooldown": config.failed_batches_for_cooldown,
                "failed_batch_window_ms": config.failed_batch_window_ms,
                "cooldown_ms": config.cooldown_ms,
            },
        }

    def _wave_batch_guard_to_dict(self, decision: WaveBatchGuardDecision) -> dict:
        return {
            **self._empty_wave_batch_guard(),
            "code": decision.code,
            "status": decision.mode,
            "mode": decision.mode,
            "blocked": decision.blocked,
            "allow_progression": decision.allow_progression,
            "current_batch_id": decision.current_batch_id,
            "batch_orders": decision.batch_orders,
            "batch_wins": decision.batch_wins,
            "batch_losses": decision.batch_losses,
            "failed_batches": decision.failed_batches,
            "pause_until": decision.pause_until,
            "reason": decision.reason,
        }

    def _result_sequence_guard_to_dict(
        self,
        decision: ResultSequenceGuardDecision,
    ) -> dict:
        config = self.result_sequence_guard_config
        return {
            "enabled": config.enabled,
            "status": "PAUSED" if decision.blocked else "NORMAL" if config.enabled else "DISABLED",
            "code": (
                "RESULT_SEQUENCE_GUARD_BLOCKED"
                if decision.blocked
                else "RESULT_SEQUENCE_NORMAL"
                if config.enabled
                else "DISABLED"
            ),
            "blocked": decision.blocked,
            "scope": config.scope,
            "loss_streak": config.loss_streak,
            "cooldown_minutes": config.cooldown_minutes,
            "direction": decision.direction,
            "consecutive_losses": decision.consecutive_losses,
            "last_settled_at": decision.last_settled_at,
            "pause_until": decision.pause_until,
            "paused_directions": [decision.direction] if decision.blocked else [],
            "reason": decision.reason,
        }

    def _empty_profile_guard_audit(self) -> dict:
        return {
            "status": "NOT_EVALUATED",
            "code": "PROFILE_GUARD_NOT_EVALUATED",
            "enabled": self.enable_profile_guard,
            "observe_only": not self.enable_profile_guard,
            "blocked": False,
            "hit_keys": [],
        }

    def _send_webhook(self, signal: Signal, order=None) -> None:
        if not self.webhook:
            return
        try:
            self.webhook.send_signal(self.symbol, signal, amount=order.stake if order else None)
        except Exception:  # noqa: BLE001 - 最低延迟模式明确静默丢弃分发异常。
            return

    def snapshot(self) -> dict:
        with self._lock:
            latest = self.klines[-1] if self.klines else None
            orders = list(reversed(self.simulator.orders[-100:]))
            return {
                "symbol": self.symbol,
                "updated_at_ms": self.updated_at_ms,
                "last_error": self.last_error,
                "latest_price": latest.close if latest else None,
                "latest_kline": latest.to_dict() if latest else None,
                "fear_greed": self.fear_greed.to_dict() if self.fear_greed else None,
                "warmup": self.warmup,
                "risk_pause": self.risk_pause,
                "rolling_edge": self.rolling_edge,
                "wave_state": self._wave_state_to_dict(),
                "result_sequence_guard": self.result_sequence_guard,
                "wave_batch_guard": self.wave_batch_guard,
                "profile_degradation_guard": self.profile_degradation_guard,
                "profile_health_guard": self.profile_health_guard,
                "direction_pulse_shadow": self.direction_pulse_shadow,
                "time_period_guard": self.time_period_guard,
                "profile_guard": self._profile_guard_config(),
                "observation_profile_promotion": self._observation_profile_promotion_config(),
                "daily_profile_selection": self._daily_profile_selector_status(),
                "stake_progression": self._stake_progression_status(),
                "order_policy": self._order_policy_status(),
                "trade_score_threshold": self._trade_score_threshold_status(),
                "webhook": self.webhook.status() if self.webhook else {
                    "enabled": False,
                    "url": None,
                    "last_error": None,
                    "last_payload": None,
                    "last_sent_at_ms": None,
                },
                "webhook_error": None,
                "signals": [signal.to_dict() for signal in self.signals],
                "selected_signal": self.selected_signal.to_dict() if self.selected_signal else None,
                "order_decision": self.order_decision,
                "stats": self.simulator.stats(
                    profile_period=self.active_daily_profile_selection,
                ),
                "orders": [order.to_dict() for order in orders],
                "observations": [observation.to_dict() for observation in reversed(self.observations[-50:])],
                "kline_count": len(self.klines),
            }

    def page_orders(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        direction: str = "",
        level: str = "",
        segment: str = "",
        result: str = "",
    ) -> dict:
        if self.storage:
            return self.storage.page_orders(
                self.symbol,
                page=page,
                page_size=page_size,
                direction=direction,
                level=level,
                segment=segment,
                result=result,
            )
        with self._lock:
            orders = list(self.simulator.orders)
        return page_order_list(
            orders,
            page=page,
            page_size=page_size,
            direction=direction,
            level=level,
            segment=segment,
            result=result,
        )

    def page_observations(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        direction: str = "",
        family: str = "",
        tag: str = "",
        segment: str = "",
        result: str = "",
    ) -> dict:
        if self.storage:
            return self.storage.page_observations(
                self.symbol,
                page=page,
                page_size=page_size,
                direction=direction,
                family=family,
                tag=tag,
                segment=segment,
                result=result,
            )
        with self._lock:
            observations = list(self.observations)
        return page_observation_list(
            observations,
            page=page,
            page_size=page_size,
            direction=direction,
            family=family,
            tag=tag,
            segment=segment,
            result=result,
        )

    def observation_summary(self) -> dict:
        if self.storage:
            summary = self.storage.observation_summary(self.symbol)
        else:
            with self._lock:
                observations = list(self.observations)
            summary = summarize_observations(observations)
        summary["promotion_config"] = self._observation_profile_promotion_config()
        latest = self.daily_profile_selection or {}
        active_keys = {
            item.get("key")
            for item in (self.active_daily_profile_selection or {}).get("selected_profiles", [])
        }
        candidates = {item.get("key"): item for item in latest.get("candidates", [])}
        for group in summary.get("groups", []):
            key = daily_profile_key(
                group.get("timeframe_minutes", 0),
                group.get("strategy_family", "unknown"),
                group.get("strategy_tag", "unknown"),
                group.get("direction", ""),
                group.get("threshold_segment", "GLOBAL"),
            )
            candidate = candidates.get(key, {})
            group["daily_profile_key"] = key
            group["selection_state"] = "ACTIVE" if key in active_keys else candidate.get(
                "selection_state",
                "NOT_EVALUATED",
            )
            group["selection_reason"] = (
                "今日主程序已启用"
                if key in active_keys
                else candidate.get("selection_reason", "尚未进入每日评估窗口")
            )
        summary["daily_profile_selection"] = self._daily_profile_selector_status()
        return summary

    def order_profile_summary(self) -> dict:
        if self.storage:
            return self.storage.order_profile_summary(
                self.symbol,
                profile_guard_min_history=self.profile_guard_min_history,
                profile_guard_min_group_size=self.profile_guard_min_group_size,
            )
        with self._lock:
            orders = list(self.simulator.orders)
        samples = [
            {
                "order_id": order.id,
                "direction": order.direction,
                "timeframe_minutes": order.timeframe_minutes,
                "threshold_segment": order.threshold_segment,
                "result": order.result,
                "pnl": order.pnl,
                "stake": order.stake,
                "stake_progression_step": order.stake_progression_step,
                "opened_at": order.opened_at,
                "settled_at": order.settled_at,
                "level": order.level,
                "reason": order.reason,
                "reason_setup": order.reason.split("：", 1)[0].split(";", 1)[0].strip() or "UNKNOWN",
                "score": order.score,
                "threshold": order.threshold,
                "edge": round(abs(order.score) - order.threshold, 4),
                "volume_ratio": 0.0,
                "volume_threshold": 0.0,
                "price_change_pct": 0.0,
                "price_position": 0.0,
                "rsi": 0.0,
                "bollinger_position": 0.0,
                "mtf_10m_bias": 0.0,
                "mtf_30m_bias": 0.0,
                "regime": order.regime,
                "risk_flags": "",
                "fear_greed_trend": "",
            }
            for order in orders
            if order.status == "SETTLED"
        ]
        return summarize_order_samples_with_guard(
            samples,
            profile_guard_min_history=self.profile_guard_min_history,
            profile_guard_min_group_size=self.profile_guard_min_group_size,
        )

    def signal_audit_summary(self) -> dict:
        if not self.storage or not hasattr(self.storage, "signal_audit_summary"):
            return {
                "sample_count": 0,
                "by_decision": [],
                "by_profile_dps_slot": [],
                "by_result_sequence_status": [],
                "by_profile_degradation_status": [],
                "by_wave_batch_status": [],
                "by_rolling_edge_status": [],
            }
        return self.storage.signal_audit_summary(self.symbol)
