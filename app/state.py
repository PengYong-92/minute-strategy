import json
import math
import threading
import time
from bisect import bisect_left
from collections.abc import Mapping
from copy import deepcopy
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Sequence

from app.decision_context import (
    CONTEXT_VERSION,
    DecisionContext,
    DecisionContextBuilder,
    runtime_config_snapshot,
)
from app.adaptive_profile_state import (
    evaluate_adaptive_profile_state,
    rebuild_adaptive_profile_states,
)

from app.daily_profile_selector import (
    QUALIFICATION_VERSION,
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
from app.entry_structure_shadow import (
    EntryStructureGate,
    StructureDetector,
    StructureStateMachine,
)
from app.models import (
    FearGreedContext,
    Kline,
    ObservationSignal,
    Signal,
    bind_canonical_quality_score_inputs,
    decision_linked_storage_payload,
)
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
from app.profile_admission import (
    ProfileAdmissionContext,
    ProfileAdmissionDecision,
    ProfileAdmissionPolicy,
    baseline_policy,
    evaluate_profile_admission,
    select_admitted_candidate,
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
from app.storage_capacity import CORE_RESERVE_BYTES, MAX_DATABASE_BYTES
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
from app.source_fingerprint import python_source_fingerprint
from app.wave_state import WaveSnapshot, advance_wave, analyze_wave
from app.wave_batch_guard import (
    WaveBatchGuardConfig,
    WaveBatchGuardDecision,
    evaluate_wave_batch_guard,
)


DAY_MS = 86_400_000
REALTIME_PRICE_STALE_MS = 5_000
ORDER_GATE_TRACE_VERSION = "ORDER_GATE_TRACE_V1"


@dataclass
class _DecisionRun:
    builder: DecisionContextBuilder
    identity: dict[str, object]
    admission_snapshot: dict[str, object]
    trace_records: list[dict[str, object]] = field(default_factory=list)

    def trace(
        self,
        stage: str,
        result: str,
        reason_code: str,
        decisive_values: object = None,
    ) -> None:
        self.trace_records.append(
            {
                "stage": stage,
                "result": result,
                "reason_code": reason_code,
                "decisive_values": deepcopy(decisive_values),
            }
        )

    def extend(self, records: Sequence[dict[str, object]]) -> None:
        self.trace_records.extend(deepcopy(list(records)))


@dataclass
class _EntryStructureFlight:
    event: threading.Event = field(default_factory=threading.Event)
    result: dict[str, object] | None = None


def strategy_source_build_id(
    paths: Sequence[Path] | None = None,
    *,
    source_root: Path | None = None,
) -> str:
    root = Path(source_root or Path(__file__).resolve().parent)
    return python_source_fingerprint(
        root,
        prefix="minute-strategy-src",
        paths=paths,
    )


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
        profile_admission_policy: ProfileAdmissionPolicy | None = None,
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
        self.last_error: str | None = None
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
        self.profile_admission_policy = profile_admission_policy or baseline_policy()
        if not isinstance(self.profile_admission_policy, ProfileAdmissionPolicy):
            raise TypeError("profile_admission_policy must be ProfileAdmissionPolicy")
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
        self._entry_structure_detector = StructureDetector()
        self._entry_structure_state_machine = StructureStateMachine(
            self._entry_structure_detector.config
        )
        self._entry_structure_gate = EntryStructureGate(
            self._entry_structure_detector,
            self._entry_structure_state_machine,
        )
        self._entry_structure_market_cache: dict[
            tuple[str, int, int], dict[str, object]
        ] = {}
        self._entry_structure_cache_lock = threading.Lock()
        self._entry_structure_inflight: dict[
            tuple[str, int, int], _EntryStructureFlight
        ] = {}
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
        adaptive_observations = self._load_adaptive_profile_observations_fail_safe(
            restored_observations,
            evaluated_at=int(self._now_ms()),
        )
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
        self._adaptive_profile_observations = list(adaptive_observations)
        self.adaptive_profile_states: dict[str, dict] = {}
        self._rebuild_all_adaptive_profile_states(int(self._now_ms()))
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
        entry_structure_market = self._entry_structure_market_snapshot(
            merged_klines,
            operation_context,
        )
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
                    self._record_observation_candidates(
                        observation_signals,
                        latest,
                        entry_structure_market,
                    )
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
            self.last_error = None
            restored_orders = self.storage.load_orders(self.symbol) if self.storage else []
            restored_observations = self._load_restored_observations()
            adaptive_observations = self._load_adaptive_profile_observations_fail_safe(
                restored_observations,
                evaluated_at=int(self._now_ms()),
            )
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
            self._adaptive_profile_observations = list(adaptive_observations)
            self.adaptive_profile_states = {}
            self._rebuild_all_adaptive_profile_states(int(self._now_ms()))
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
            self.updated_at_ms = int(time.time() * 1000)
            self.time_period_guard = evaluate_time_period_guard(
                self.updated_at_ms,
                self.time_period_guard_config,
            ).to_dict()
            self._realtime_price = None
            self._realtime_price_event_time_ms = 0
            self._realtime_price_received_at_ms = 0
            self._market_stream_status = "STARTING"
            with self._entry_structure_cache_lock:
                self._entry_structure_market_cache.clear()
            self._opened_signal_keys.clear()
            self._last_order_opened_at = self._latest_order_opened_at_by_direction(
                restored_orders
            )
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
                except Exception as exc:  # noqa: BLE001 - 页面可继续返回 PREPARING。
                    self._profile_summary_prepare_error = exc

    def _matches_symbol_context(self, expected_context: tuple[str, int] | None) -> bool:
        return expected_context is None or expected_context == (
            self.symbol,
            self._symbol_generation,
        )

    @staticmethod
    def _entry_structure_error_market(
        latest: Kline,
        stage: str,
        error: Exception | None = None,
    ) -> dict[str, object]:
        error_type = type(error).__name__ if error is not None else "InvalidResult"
        return {
            "version": "ENTRY_STRUCTURE_SHADOW_V1",
            "mode": "SHADOW_ONLY",
            "status": "ERROR",
            "evaluated_at": int(latest.close_time),
            "states": [],
            "reason_code": f"{stage.upper()}_ERROR_{error_type.upper()}",
            "error_detail": error_type,
        }

    def _entry_structure_market_snapshot(
        self,
        closed_klines: Sequence[Kline],
        operation_context: tuple[str, int],
    ) -> dict[str, object]:
        latest = closed_klines[-1]
        cache_key = (
            str(operation_context[0]).upper(),
            int(operation_context[1]),
            int(latest.close_time),
        )
        with self._entry_structure_cache_lock:
            cache = self._entry_structure_market_cache
            if not isinstance(cache, dict):
                cache = {}
                self._entry_structure_market_cache = cache
            cached = cache.get(cache_key)
            if cached is None:
                flight = self._entry_structure_inflight.get(cache_key)
                owner = flight is None
                if flight is None:
                    flight = _EntryStructureFlight()
                    self._entry_structure_inflight[cache_key] = flight

        if cached is not None:
            return self._copy_entry_structure_market(
                cached,
                latest,
                "CACHE_COPY",
            )

        if not owner:
            flight.event.wait()
            if flight.result is not None:
                return self._copy_entry_structure_market(
                    flight.result,
                    latest,
                    "SINGLEFLIGHT_COPY",
                )
            return self._entry_structure_error_market(
                latest,
                "SINGLEFLIGHT_RESULT",
            )

        try:
            detected = self._entry_structure_detector.detect(
                cache_key[0],
                closed_klines,
            )
        except Exception as exc:  # noqa: BLE001 - 影子检测失败不得影响主流程。
            market = self._entry_structure_error_market(
                latest,
                "DETECTOR",
                exc,
            )
        else:
            if not isinstance(detected, dict):
                market = self._entry_structure_error_market(
                    latest,
                    "DETECTOR_RESULT",
                )
            else:
                try:
                    states = self._entry_structure_state_machine.evaluate(
                        detected,
                        closed_klines,
                    )
                except Exception as exc:  # noqa: BLE001 - 状态影子失败不得影响主流程。
                    market = self._entry_structure_error_market(
                        latest,
                        "STATE_MACHINE",
                        exc,
                    )
                else:
                    if not isinstance(states, (list, tuple)):
                        market = self._entry_structure_error_market(
                            latest,
                            "STATE_MACHINE_RESULT",
                        )
                    else:
                        try:
                            market = {
                                **deepcopy(detected),
                                "states": deepcopy(list(states)),
                            }
                        except Exception as exc:  # noqa: BLE001 - 原始影子快照必须故障隔离。
                            market = self._entry_structure_error_market(
                                latest,
                                "SNAPSHOT_FREEZE",
                                exc,
                            )

        with self._entry_structure_cache_lock:
            flight.result = market
            if operation_context == (self.symbol, self._symbol_generation):
                cache = self._entry_structure_market_cache
                cache[cache_key] = market
                while len(cache) > 256:
                    cache.pop(next(iter(cache)))
            if self._entry_structure_inflight.get(cache_key) is flight:
                self._entry_structure_inflight.pop(cache_key, None)
            flight.event.set()
        return self._copy_entry_structure_market(
            market,
            latest,
            "OWNER_COPY",
        )

    def _copy_entry_structure_market(
        self,
        market: dict[str, object],
        latest: Kline,
        stage: str,
    ) -> dict[str, object]:
        try:
            return deepcopy(market)
        except Exception as exc:  # noqa: BLE001 - 缓存副本失败不得污染或阻断主流程。
            return self._entry_structure_error_market(latest, stage, exc)

    def _current_entry_structure_market(self, latest: Kline) -> dict[str, object]:
        context = (self.symbol, self._symbol_generation)
        closed_klines = list(self.klines)
        if not closed_klines or closed_klines[-1].close_time != latest.close_time:
            closed_klines = [item for item in closed_klines if item.close_time < latest.close_time]
            closed_klines.append(latest)
        return self._entry_structure_market_snapshot(closed_klines, context)

    @staticmethod
    def _normalize_entry_structure_payload(
        payload: dict[str, object],
        latest: Kline,
        candidate_origin: str,
        candidate_direction: str,
    ) -> dict[str, object]:
        source = deepcopy(payload)
        evaluated_at = int(
            source.get(
                "entry_structure_evaluated_at",
                source.get("evaluated_at", latest.close_time),
            )
        )
        state = str(
            source.get(
                "entry_structure_state",
                source.get("state", source.get("status", "NOT_AVAILABLE")),
            )
        )
        bias = str(
            source.get(
                "entry_structure_bias",
                source.get("bias", "NOT_AVAILABLE"),
            )
        )
        reason = str(
            source.get(
                "entry_structure_reason_code",
                source.get("reason_code", source.get("reason", "NOT_EVALUATED")),
            )
        )
        version = str(
            source.get(
                "entry_structure_version",
                source.get("version", "ENTRY_STRUCTURE_SHADOW_V1"),
            )
        )
        normalized = {
            "entry_structure_version": version,
            "entry_structure_mode": "SHADOW_ONLY",
            "entry_structure_evaluated_at": evaluated_at,
            "entry_structure_state": state,
            "entry_structure_bias": bias,
            "entry_structure_reason_code": reason,
            "version": version,
            "mode": "SHADOW_ONLY",
            "status": state,
            "evaluated_at": evaluated_at,
            "state": state,
            "bias": bias,
            "reason_code": reason,
            "reason": reason,
            "audit_only": True,
            "candidate_origin": candidate_origin,
            "candidate_direction": candidate_direction,
            "active_level_source": "NOT_AVAILABLE",
            "active_level_lower": None,
            "active_level_upper": None,
            "active_level_touch_count": 0,
            "active_level_confirmed_at": 0,
            "nearest_support_lower": None,
            "nearest_support_upper": None,
            "nearest_resistance_lower": None,
            "nearest_resistance_upper": None,
            "support_distance_price": None,
            "support_distance_bps": None,
            "support_distance_atr": None,
            "resistance_distance_price": None,
            "resistance_distance_bps": None,
            "resistance_distance_atr": None,
            "breakout_direction": "NOT_AVAILABLE",
            "breakout_closed_bars": 0,
            "breakout_buffer_atr": None,
            "retest_status": "NOT_AVAILABLE",
            "round_level_price": None,
            "round_level_step": None,
            **source,
        }
        normalized.update(
            {
                "entry_structure_version": version,
                "entry_structure_mode": "SHADOW_ONLY",
                "entry_structure_evaluated_at": evaluated_at,
                "entry_structure_state": state,
                "entry_structure_bias": bias,
                "entry_structure_reason_code": reason,
                "version": version,
                "mode": "SHADOW_ONLY",
                "status": state,
                "evaluated_at": evaluated_at,
                "state": state,
                "bias": bias,
                "reason_code": reason,
                "reason": reason,
                "audit_only": True,
                "candidate_origin": candidate_origin,
                "candidate_direction": candidate_direction,
            }
        )
        return normalized

    @classmethod
    def _canonical_entry_structure_value(cls, value: object) -> object:
        if isinstance(value, Mapping):
            normalized = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError("entry structure mapping keys must be strings")
                normalized[key] = cls._canonical_entry_structure_value(item)
            return normalized
        if isinstance(value, (list, tuple)):
            return [cls._canonical_entry_structure_value(item) for item in value]
        if value is None or isinstance(value, (bool, int, str)):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("entry structure numbers must be finite")
            return value
        raise TypeError(
            f"unsupported entry structure value type: {type(value).__name__}"
        )

    @classmethod
    def _freeze_entry_structure_payload(
        cls,
        payload: dict[str, object],
    ) -> dict[str, object]:
        frozen = deepcopy(payload)
        canonical = cls._canonical_entry_structure_value(frozen)
        if not isinstance(canonical, dict):
            raise TypeError("entry structure payload must be a mapping")
        json.dumps(canonical, allow_nan=False)
        return canonical

    def _entry_structure_error_snapshot(
        self,
        latest: Kline,
        candidate_origin: str,
        candidate_direction: str,
        stage: str,
        error: Exception | None = None,
    ) -> dict[str, object]:
        payload = self._normalize_entry_structure_payload(
            self._entry_structure_error_market(latest, stage, error),
            latest,
            candidate_origin,
            candidate_direction,
        )
        return self._freeze_entry_structure_payload(payload)

    def _attach_entry_structure_snapshot(
        self,
        signal: Signal,
        latest: Kline,
        candidate_origin: str,
        market_snapshot: dict[str, object] | None = None,
    ) -> Signal:
        direction = self._signal_direction(signal)
        existing = signal.entry_structure_shadow
        if isinstance(existing, dict) and existing:
            try:
                existing_origin = str(existing.get("candidate_origin", ""))
                existing_direction = str(existing.get("candidate_direction", ""))
                existing_evaluated_at = existing.get(
                    "entry_structure_evaluated_at",
                    existing.get("evaluated_at"),
                )
                reusable = (
                    existing_origin in {"", candidate_origin}
                    and existing_direction in {"", direction}
                    and type(existing_evaluated_at) is int
                    and existing_evaluated_at == int(latest.close_time)
                )
                payload = (
                    self._freeze_entry_structure_payload(
                        self._normalize_entry_structure_payload(
                            existing,
                            latest,
                            candidate_origin,
                            direction,
                        )
                    )
                    if reusable
                    else None
                )
            except Exception as exc:  # noqa: BLE001 - 输入影子不得影响真实准入。
                payload = self._entry_structure_error_snapshot(
                    latest,
                    candidate_origin,
                    direction,
                    "EXISTING_SNAPSHOT",
                    exc,
                )
            if payload is not None:
                return replace(
                    signal,
                    candidate_origin=candidate_origin,
                    entry_structure_shadow=payload,
                )
        try:
            market = market_snapshot or self._current_entry_structure_market(latest)
            payload = self._entry_structure_gate.attach(
                replace(signal, direction=direction),
                market,
                candidate_origin,
            )
            if not isinstance(payload, dict):
                payload = self._entry_structure_error_market(
                    latest,
                    "ATTACH_RESULT",
                )
            payload = self._freeze_entry_structure_payload(
                self._normalize_entry_structure_payload(
                    payload,
                    latest,
                    candidate_origin,
                    direction,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 影子映射不得影响真实准入。
            payload = self._entry_structure_error_snapshot(
                latest,
                candidate_origin,
                direction,
                "ATTACH_SNAPSHOT",
                exc,
            )
        return replace(
            signal,
            candidate_origin=candidate_origin,
            entry_structure_shadow=payload,
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
                    "daily_selector": {
                        **asdict(self.daily_profile_selector_config),
                        "effective_stable_lookback_days": (
                            self.daily_profile_selector_config.effective_stable_lookback_days
                        ),
                        "stable_lookback_source": (
                            self.daily_profile_selector_config.stable_lookback_source
                        ),
                        "effective_joint_failures_to_exit": (
                            self.daily_profile_selector_config.joint_failures_to_exit
                        ),
                    },
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
                    "admission": self._profile_admission_status(),
                },
                "trade_score_threshold": self.trade_score_threshold,
                "module_versions": {
                    "decision_context": "DECISION_CONTEXT_V2",
                    "order_gate_trace": ORDER_GATE_TRACE_VERSION,
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

    def _new_decision_run(
        self,
        signal: Signal,
        latest: Kline,
        *,
        candidate_origin: str,
        candidate_ordinal: int,
    ) -> _DecisionRun:
        config = self._decision_runtime_config()
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
            profile_key=str(signal.profile_key or ""),
            candidate_ordinal=int(candidate_ordinal),
            candidate_identity=candidate_identity,
        )
        identity = {
            **candidate_identity,
            "candidate_identity": deepcopy(candidate_identity),
            "symbol": self.symbol,
            "decision_id": builder.decision_id,
            "context_version": CONTEXT_VERSION,
            "runtime_config_hash": config.hash,
            "strategy_build_id": config.strategy_build_id,
            "level": str(signal.level or ""),
        }
        return _DecisionRun(
            builder=builder,
            identity=identity,
            admission_snapshot=self._sample_candidate_admission(signal, latest),
        )

    def _sample_candidate_admission(
        self,
        signal: Signal,
        latest: Kline,
    ) -> dict[str, object]:
        direction = self._signal_direction(signal)
        open_orders = [
            order for order in self.simulator.orders if order.status == "OPEN"
        ]
        direction_open_count = sum(
            1
            for order in open_orders
            if str(order.direction or "").upper() == direction
        )
        last_opened_at = (
            self._last_order_opened_at.get(direction)
            if isinstance(self._last_order_opened_at, dict)
            else self._last_order_opened_at
        )
        candidate_time = int(latest.close_time)
        minimum_gap = int(self.order_policy.min_order_gap_ms)
        elapsed = (
            max(0, candidate_time - int(last_opened_at))
            if last_opened_at is not None
            else 0
        )
        remaining = (
            max(0, minimum_gap - elapsed)
            if last_opened_at is not None
            else 0
        )
        earliest_open_at = (
            int(last_opened_at) + minimum_gap
            if last_opened_at is not None
            else candidate_time
        )
        capacity = self._sample_storage_capacity(candidate_time)
        return {
            "global_open_count": len(open_orders),
            "global_open_limit": self.order_policy.max_open_orders,
            "direction": direction,
            "direction_open_count": direction_open_count,
            "direction_open_limit": (
                self.order_policy.max_open_long_orders
                if direction == "LONG"
                else self.order_policy.max_open_short_orders
            ),
            "cooldown": {
                "last_opened_at": last_opened_at,
                "candidate_time": candidate_time,
                "minimum_gap_ms": minimum_gap,
                "earliest_open_at": earliest_open_at,
                "elapsed_ms": elapsed,
                "remaining_ms": remaining,
                "would_pass": last_opened_at is None or remaining == 0,
            },
            "storage_capacity": capacity,
        }

    def _sample_storage_capacity(self, sampled_at_ms: int) -> dict[str, object]:
        if self.storage is None:
            return {
                "status": "IN_MEMORY",
                "database_bytes": 0,
                "max_database_bytes": 0,
                "core_reserve_bytes": 0,
                "ordinary_audit_allowed": True,
                "core_write_allowed": True,
                "observation_write_allowed": True,
                "compact_audit_allowed": True,
                "sampled_at_ms": int(sampled_at_ms),
                "error_type": "",
                "error": "",
            }
        loader = getattr(self.storage, "storage_capacity", None)
        if loader is None:
            return {
                "status": "UNAVAILABLE",
                "database_bytes": 0,
                "max_database_bytes": MAX_DATABASE_BYTES,
                "core_reserve_bytes": CORE_RESERVE_BYTES,
                "ordinary_audit_allowed": False,
                "core_write_allowed": True,
                "observation_write_allowed": True,
                "compact_audit_allowed": True,
                "sampled_at_ms": int(sampled_at_ms),
                "error_type": "",
                "error": "storage_capacity is not implemented",
            }
        try:
            capacity = loader()
        except Exception as exc:  # noqa: BLE001 - 容量采样仅审计，不影响 gate。
            return {
                "status": "ERROR",
                "database_bytes": 0,
                "max_database_bytes": MAX_DATABASE_BYTES,
                "core_reserve_bytes": CORE_RESERVE_BYTES,
                "ordinary_audit_allowed": False,
                "core_write_allowed": False,
                "observation_write_allowed": False,
                "compact_audit_allowed": False,
                "sampled_at_ms": int(sampled_at_ms),
                "error_type": type(exc).__name__,
                "error": str(exc)[:200],
            }
        return {
            "status": str(capacity.status),
            "database_bytes": int(capacity.database_bytes),
            "max_database_bytes": int(capacity.max_database_bytes),
            "core_reserve_bytes": int(capacity.core_reserve_bytes),
            "ordinary_audit_allowed": bool(capacity.ordinary_audit_allowed),
            "core_write_allowed": bool(capacity.core_write_allowed),
            "observation_write_allowed": bool(capacity.core_write_allowed),
            "compact_audit_allowed": bool(capacity.core_write_allowed),
            "sampled_at_ms": int(sampled_at_ms),
            "error_type": "",
            "error": "",
        }

    @staticmethod
    def _signal_context_payload(signal: Signal) -> dict[str, object]:
        payload = signal.to_dict()
        for key in (
            "decision_id",
            "context_version",
            "runtime_config_hash",
            "strategy_build_id",
            "decision_inputs",
            "decision_trace",
            "first_decisive_block",
            "quality_score_inputs",
        ):
            payload.pop(key, None)
        return payload

    def _canonical_decision_inputs(
        self,
        run: _DecisionRun,
        signal: Signal,
        latest: Kline,
        *,
        audit_context: dict,
        selected_order_terms: dict[str, object] | None,
        observation_allowed: bool,
    ) -> dict[str, object]:
        strategy_inputs = (
            signal.decision_inputs
            if isinstance(signal.decision_inputs, dict)
            else {}
        )
        strategy_score = deepcopy(strategy_inputs.get("score", {}))
        thresholds = deepcopy(strategy_inputs.get("thresholds", {}))
        volume_price = deepcopy(strategy_inputs.get("volume_price", {}))
        indicators = deepcopy(strategy_inputs.get("indicators", {}))

        bounded_window_size = max(
            10,
            min(60, int(signal.analysis_window_minutes or 0)),
            min(60, int(signal.threshold_window_minutes or 0)),
        )
        recent = list(self.klines[-bounded_window_size:])
        if not recent or recent[-1].close_time != latest.close_time:
            recent = [item for item in recent if item.close_time < latest.close_time]
            recent.append(latest)
        analysis_window = recent[-10:]
        threshold_window_size = max(1, min(60, int(signal.threshold_window_minutes or 10)))
        threshold_window = recent[-threshold_window_size:]

        score_control_fields = {
            "raw_direction",
            "raw_score",
            "signed_score",
            "score_abs",
            "edge",
            "final_direction",
            "actionable",
        }
        score_components = {
            key: value
            for key, value in strategy_score.items()
            if key not in score_control_fields
        }
        threshold_adjustment_fields = (
            "session_threshold_adjustment",
            "fear_greed_adjustment",
            "regime_adjustment",
            "normal_down_short_override_applied",
            "normal_down_short_threshold_adjustment",
        )
        score = {
            "raw_direction": strategy_score.get("raw_direction", signal.direction),
            "signed_score": strategy_score.get("signed_score", signal.score),
            "raw_score": strategy_score.get("raw_score", abs(signal.score)),
            "score_abs": strategy_score.get("score_abs", abs(signal.score)),
            "final_direction": strategy_score.get("final_direction", signal.direction),
            "actionable": strategy_score.get("actionable", signal.actionable),
            **thresholds,
            "base_threshold": thresholds.get("base_threshold", signal.threshold),
            "dynamic_threshold": thresholds.get(
                "pre_override_threshold",
                signal.threshold,
            ),
            "calculated_threshold": thresholds.get(
                "calculated_threshold",
                signal.calculated_threshold or signal.threshold,
            ),
            "edge": strategy_score.get(
                "edge",
                abs(signal.score) - signal.threshold,
            ),
            "threshold_adjustments": {
                key: thresholds[key]
                for key in threshold_adjustment_fields
                if key in thresholds
            },
            "components": score_components,
            "quality_score": signal.quality_score,
            "quality_score_version": signal.quality_score_version,
            "quality_score_mode": signal.quality_score_mode,
            "quality_score_context": signal.quality_score_context,
            "quality_score_components": deepcopy(signal.quality_score_components),
            "quality_score_inputs": deepcopy(signal.quality_score_inputs),
        }
        volume_price = {
            "current_volume": volume_price.get("current_volume", latest.volume),
            "volume_baseline": volume_price.get("volume_baseline", 0.0),
            "volume_ratio": volume_price.get("volume_ratio", signal.volume_ratio),
            "high_volume_threshold": volume_price.get(
                "high_volume_threshold",
                signal.volume_threshold,
            ),
            "low_volume_threshold": volume_price.get("low_volume_threshold", 0.0),
            "move_threshold_pct": volume_price.get(
                "move_threshold_pct",
                signal.move_threshold_pct,
            ),
            **volume_price,
        }
        indicators = {
            **indicators,
            "macd_line": indicators.get("macd_line", 0.0),
            "macd_signal_line": indicators.get("macd_signal_line", 0.0),
            "macd_histogram": indicators.get(
                "macd_histogram",
                signal.macd_histogram,
            ),
            "macd_histogram_delta": indicators.get(
                "macd_histogram_delta",
                signal.macd_histogram_delta,
            ),
            "atr14": indicators.get("atr", 0.0),
            "macd_histogram_atr": indicators.get("macd_histogram_atr", 0.0),
            "macd_delta_atr": indicators.get("macd_delta_atr", 0.0),
            "macd_histogram_threshold": indicators.get(
                "macd_histogram_threshold",
                signal.macd_histogram_threshold,
            ),
            "macd_delta_threshold": indicators.get(
                "macd_delta_threshold",
                signal.macd_delta_threshold,
            ),
            "rsi": indicators.get("rsi", signal.rsi),
            "rsi_lower_threshold": indicators.get(
                "rsi_lower_threshold",
                signal.rsi_lower_threshold,
            ),
            "rsi_upper_threshold": indicators.get(
                "rsi_upper_threshold",
                signal.rsi_upper_threshold,
            ),
            "bollinger_position": indicators.get(
                "bollinger_position",
                signal.bollinger_position,
            ),
            "bollinger_width": indicators.get(
                "bollinger_width",
                signal.bollinger_width,
            ),
            "bollinger_lower_threshold": indicators.get(
                "bollinger_lower_threshold",
                signal.bollinger_lower_threshold,
            ),
            "bollinger_upper_threshold": indicators.get(
                "bollinger_upper_threshold",
                signal.bollinger_upper_threshold,
            ),
            "indicator_profile_segment": indicators.get(
                "indicator_profile_segment",
                signal.indicator_profile_segment,
            ),
            "indicator_profile_sample_size": indicators.get(
                "indicator_profile_sample_size",
                signal.indicator_profile_sample_size,
            ),
            "indicator_profile_version": indicators.get(
                "indicator_profile_version",
                "INDICATOR_PROFILE_V1",
            ),
            "indicator_profile_match_codes": deepcopy(
                indicators.get("indicator_profile_match_codes", [])
            ),
        }

        fear_greed = (
            self.fear_greed.to_dict()
            if self.fear_greed is not None
            else {
                "value": signal.fear_greed_value,
                "classification": signal.fear_greed_classification,
                "average_30d": signal.fear_greed_average_30d,
                "trend": signal.fear_greed_trend,
                "updated_at_ms": 0,
                "source": "signal",
            }
        )
        fear_greed["adjustment"] = signal.fear_greed_adjustment
        adaptive = deepcopy(signal.adaptive_profile_state)
        n12_n20 = deepcopy(adaptive.get("n12_n20", adaptive)) if adaptive else {}
        n12_n20.setdefault("version", "ADAPTIVE_PROFILE_PENDING")
        n12_n20.setdefault("status", "NOT_AVAILABLE")
        n12_n20.setdefault("evaluated_at", 0)
        n12_n20.setdefault("n12", {"status": "NOT_AVAILABLE", "sample_size": 0})
        n12_n20.setdefault("n20", {"status": "NOT_AVAILABLE", "sample_size": 0})

        structure = deepcopy(signal.entry_structure_shadow)

        trace_by_stage = {
            str(record["stage"]): record
            for record in run.trace_records
        }
        guard_stages = (
            "WAVE_GUARD",
            "DAILY_PROFILE",
            "ADAPTIVE_PROFILE",
            "PROFILE_HEALTH_SHORT_WINDOW",
            "SCORE",
            "SESSION",
            "CAPACITY",
            "COOLDOWN",
            "DIRECTION_CAPACITY",
            "DUPLICATE",
            "SHORT_MODE",
            "WAVE_BATCH",
            "PROFILE_DEGRADATION",
            "RESULT_SEQUENCE",
            "ROLLING_EDGE",
            "PROFILE_HEALTH",
            "TIME_PERIOD",
            "PROFILE_HEALTH_SECOND_ORDER",
        )
        admission = {
            **deepcopy(run.admission_snapshot),
            "profile_admission_candidates": deepcopy(
                adaptive.get("admission_candidates", [])
            ),
            "guards": deepcopy(audit_context),
            "guard_results": {
                stage: deepcopy(
                    trace_by_stage.get(
                        stage,
                        {
                            "stage": stage,
                            "result": "NOT_EVALUATED",
                            "reason_code": "NOT_EVALUATED",
                            "decisive_values": {},
                        },
                    )
                )
                for stage in guard_stages
            },
            "trace_snapshot": deepcopy(run.trace_records),
            "open_allowed": bool(selected_order_terms),
            "observation_allowed": bool(observation_allowed),
            "stake": {
                "amount": self.stake,
                "win_return": self.win_return,
                "progression_enabled": self.enable_stake_progression,
                "progression_max_orders": self.stake_progression_max_orders,
                "progression_max_active": self.stake_progression_max_active,
                "progression_status": self._stake_progression_status(),
                "selected_order_terms": deepcopy(selected_order_terms or {}),
            },
        }
        return {
            "identity": deepcopy(run.identity),
            "market": {
                "closed_kline_at_ms": int(latest.close_time),
                "candidate_time_ms": int(latest.close_time),
                "entry_price": float(latest.close),
                "closed_kline": latest.to_dict(),
                "analysis_10m_window": [item.to_dict() for item in analysis_window],
                "threshold_window": [item.to_dict() for item in threshold_window],
                "analysis_window_minutes": signal.analysis_window_minutes,
                "threshold_window_minutes": signal.threshold_window_minutes,
                "price_change_pct": signal.price_change_pct,
                "price_position": signal.price_position,
                "window_close_strength": signal.close_strength,
                "candle_strength": volume_price.get("candle_strength", 0.0),
                "upper_wick_ratio": volume_price.get("upper_wick_ratio", 0.0),
                "lower_wick_ratio": volume_price.get("lower_wick_ratio", 0.0),
            },
            "score": score,
            "volume_price": volume_price,
            "indicators": indicators,
            "context": {
                "mtf_10m_bias": indicators.get("mtf_10m_bias", signal.mtf_10m_bias),
                "mtf_30m_bias": indicators.get("mtf_30m_bias", signal.mtf_30m_bias),
                "regime": signal.regime,
                "fear_greed": fear_greed,
                "daily_7d_14d": {
                    "version": signal.daily_profile_version,
                    "selected": signal.daily_profile_selected,
                    "selection": deepcopy(self.active_daily_profile_selection),
                    "7d": deepcopy(
                        adaptive.get("fast_7d", {"status": "NOT_AVAILABLE"})
                    ),
                    "14d": deepcopy(
                        adaptive.get("stable_14d", {"status": "NOT_AVAILABLE"})
                    ),
                },
                "n12_n20": n12_n20,
                "wave": {
                    "state": signal.wave_state,
                    "raw_state": signal.wave_raw_state,
                    "window": signal.wave_window,
                    "efficiency": signal.wave_efficiency,
                    "direction_ratio": signal.wave_direction_ratio,
                    "atr_strength": signal.wave_atr_strength,
                    "confirmations": signal.wave_confirmations,
                    "confirmed_at": signal.wave_confirmed_at,
                    "batch_id": signal.wave_batch_id,
                },
                "direction_pulse": deepcopy(signal.direction_pulse_shadow),
                "profile_summary_cache": {
                    "source_revision": self.profile_guard_audit.get("source_revision"),
                    "current_revision": self.profile_guard_audit.get("current_revision"),
                    "stale": bool(self.profile_guard_audit.get("stale", False)),
                    "cache_status": self.profile_guard_audit.get("cache_status", "UNKNOWN"),
                },
            },
            "admission": admission,
            "entry_structure": structure,
            "signal": self._signal_context_payload(signal),
            "audit_snapshot": deepcopy(audit_context),
        }

    def _reuse_committed_decision(
        self,
        signal: Signal,
        latest: Kline,
        run: _DecisionRun,
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
            self._candidate_error_outcome(
                signal,
                latest,
                run,
                stage="DECISION_REPLAY",
                reason="已提交决策读取失败",
                error=exc,
            )
            return "STORAGE_ERROR"
        if context is None:
            return None
        frozen_signal_payload = context["inputs"].get("signal")
        if not isinstance(frozen_signal_payload, dict):
            error = ValueError("stored decision has no frozen signal")
            self._decision_storage_failed = True
            self._set_storage_error("已提交决策信号读取失败", error)
            self._candidate_error_outcome(
                signal,
                latest,
                run,
                stage="DECISION_REPLAY",
                reason="已提交决策信号读取失败",
                error=error,
            )
            return "STORAGE_ERROR"
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
            self._candidate_error_outcome(
                signal,
                latest,
                run,
                stage="DECISION_REPLAY",
                reason="已提交决策信号读取失败",
                error=exc,
            )
            return "STORAGE_ERROR"
        frozen_identity = context["inputs"].get("identity")
        frozen_candidate_identity = (
            frozen_identity.get("candidate_identity", frozen_identity)
            if isinstance(frozen_identity, dict)
            else None
        )
        if (
            not isinstance(frozen_identity, dict)
            or frozen_candidate_identity != candidate_identity
            or self._candidate_identity(
                frozen_signal,
                candidate_ordinal=int(candidate_identity.get("candidate_ordinal", -1)),
                candidate_origin=str(candidate_identity.get("candidate_origin", "")),
                closed_kline_at_ms=context["closed_kline_at_ms"],
            )
            != frozen_candidate_identity
        ):
            self._decision_storage_failed = True
            self._set_storage_error(
                "已提交决策信号读取失败",
                ValueError("frozen signal does not match candidate identity"),
            )
            self._candidate_error_outcome(
                signal,
                latest,
                run,
                stage="DECISION_REPLAY",
                reason="已提交决策信号读取失败",
                error=ValueError("frozen signal does not match candidate identity"),
            )
            return "STORAGE_ERROR"
        final_decision = str(context["final_decision"])
        if final_decision == "OPENED":
            try:
                persisted_orders = self.storage.load_orders(self.symbol)
            except Exception as exc:  # noqa: BLE001 - 已提交订单必须可恢复。
                self._decision_storage_failed = True
                self._set_storage_error("已提交订单读取失败", exc)
                self._candidate_error_outcome(
                    signal,
                    latest,
                    run,
                    stage="DECISION_REPLAY",
                    reason="已提交订单读取失败",
                    error=exc,
                )
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
                self._candidate_error_outcome(
                    signal,
                    latest,
                    run,
                    stage="DECISION_REPLAY",
                    reason="已提交订单读取失败",
                    error=ValueError("OPENED decision must map to exactly one order"),
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
        canonical_inputs = deepcopy(context["inputs"])
        quality_score_inputs = canonical_inputs.get("score", {}).get(
            "quality_score_inputs",
            {},
        )
        enriched_signal = replace(
            frozen_signal,
            decision_id=str(context["decision_id"]),
            context_version=str(context["context_version"]),
            runtime_config_hash=str(context["runtime_config_hash"]),
            strategy_build_id=str(context["strategy_build_id"]),
            candidate_origin=str(context["candidate_origin"]),
            quality_score_inputs=quality_score_inputs,
            decision_inputs=canonical_inputs,
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

    def _candidate_error_outcome(
        self,
        signal: Signal,
        latest: Kline,
        run: _DecisionRun,
        *,
        stage: str,
        reason: str,
        error: BaseException,
        base_context: DecisionContext | None = None,
    ) -> Signal:
        if not (
            run.trace_records
            and run.trace_records[-1]["stage"] == stage
            and run.trace_records[-1]["result"] == "BLOCK"
        ):
            run.trace(
                stage,
                "BLOCK",
                "STORAGE_ERROR",
                {
                    "error_type": type(error).__name__,
                    "error": str(error)[:200],
                },
            )
        if base_context is not None:
            inputs = base_context.to_dict()["inputs"]
        else:
            try:
                inputs = self._canonical_decision_inputs(
                    run,
                    signal,
                    latest,
                    audit_context=self._current_signal_audit_context(),
                    selected_order_terms=None,
                    observation_allowed=False,
                )
            except Exception as freeze_error:  # noqa: BLE001 - 错误 outcome 本身必须可冻结。
                inputs = self._fallback_candidate_error_inputs(
                    run,
                    signal,
                    latest,
                    freeze_error=freeze_error,
                )
        admission = inputs.setdefault("admission", {})
        admission["open_allowed"] = False
        admission["observation_allowed"] = False
        stake = admission.setdefault("stake", {})
        stake["selected_order_terms"] = {}
        stake["commit_status"] = "NOT_COMMITTED"
        context = DecisionContext(
            decision_id=run.builder.decision_id,
            context_version=CONTEXT_VERSION,
            runtime_config_hash=str(run.identity["runtime_config_hash"]),
            strategy_build_id=str(run.identity["strategy_build_id"]),
            symbol=self.symbol,
            closed_kline_at_ms=int(latest.close_time),
            candidate_origin=str(run.identity["candidate_origin"]),
            inputs=inputs,
            decision_trace=tuple(run.trace_records),
            first_decisive_block=next(
                (
                    str(record["stage"])
                    for record in run.trace_records
                    if record["result"] == "BLOCK"
                ),
                "",
            ),
            final_decision="STORAGE_ERROR",
            final_reason=reason,
            open_allowed=False,
            observation_allowed=False,
        )
        payload = context.to_dict()
        canonical_inputs = payload["inputs"]
        canonical_structure = canonical_inputs.get("entry_structure", {})
        quality_score_inputs = canonical_inputs.get("score", {}).get(
            "quality_score_inputs",
            {},
        )
        failed_signal = replace(
            signal,
            decision_id=context.decision_id,
            context_version=context.context_version,
            runtime_config_hash=context.runtime_config_hash,
            strategy_build_id=context.strategy_build_id,
            candidate_origin=context.candidate_origin,
            quality_score_inputs=quality_score_inputs,
            entry_structure_shadow=(
                deepcopy(canonical_structure)
                if isinstance(canonical_structure, dict)
                else {}
            ),
            decision_inputs=canonical_inputs,
            decision_trace=payload["decision_trace"],
            first_decisive_block=context.first_decisive_block,
        )
        self.selected_signal = failed_signal
        return failed_signal

    def _fallback_candidate_error_inputs(
        self,
        run: _DecisionRun,
        signal: Signal,
        latest: Kline,
        *,
        freeze_error: BaseException,
    ) -> dict[str, object]:
        guard_stages = (
            "WAVE_GUARD",
            "DAILY_PROFILE",
            "ADAPTIVE_PROFILE",
            "PROFILE_HEALTH_SHORT_WINDOW",
            "SCORE",
            "SESSION",
            "CAPACITY",
            "COOLDOWN",
            "DIRECTION_CAPACITY",
            "DUPLICATE",
            "SHORT_MODE",
            "WAVE_BATCH",
            "PROFILE_DEGRADATION",
            "RESULT_SEQUENCE",
            "ROLLING_EDGE",
            "PROFILE_HEALTH",
            "TIME_PERIOD",
            "PROFILE_HEALTH_SECOND_ORDER",
        )
        trace_by_stage = {
            str(record["stage"]): record
            for record in run.trace_records
        }
        admission = {
            **deepcopy(run.admission_snapshot),
            "guards": self._current_signal_audit_context(),
            "guard_results": {
                stage: deepcopy(
                    trace_by_stage.get(
                        stage,
                        {
                            "stage": stage,
                            "result": "NOT_EVALUATED",
                            "reason_code": "NOT_EVALUATED",
                            "decisive_values": {},
                        },
                    )
                )
                for stage in guard_stages
            },
            "trace_snapshot": deepcopy(run.trace_records),
            "open_allowed": False,
            "observation_allowed": False,
            "stake": {
                "amount": self.stake,
                "win_return": self.win_return,
                "progression_enabled": self.enable_stake_progression,
                "progression_max_orders": self.stake_progression_max_orders,
                "progression_max_active": self.stake_progression_max_active,
                "progression_status": self._stake_progression_status(),
                "selected_order_terms": {},
                "commit_status": "NOT_COMMITTED",
            },
        }
        entry_structure = self._entry_structure_error_snapshot(
            latest,
            str(run.identity["candidate_origin"]),
            self._signal_direction(signal),
            "DECISION_FREEZE",
            freeze_error,
        )
        fallback_signal = replace(
            signal,
            entry_structure_shadow=deepcopy(entry_structure),
        )
        return {
            "identity": deepcopy(run.identity),
            "market": {
                "closed_kline_at_ms": int(latest.close_time),
                "candidate_time_ms": int(latest.close_time),
                "entry_price": float(latest.close),
                "closed_kline": latest.to_dict(),
                "analysis_10m_window": [],
                "threshold_window": [],
                "analysis_window_minutes": signal.analysis_window_minutes,
                "threshold_window_minutes": signal.threshold_window_minutes,
                "price_change_pct": signal.price_change_pct,
                "price_position": signal.price_position,
                "window_close_strength": signal.close_strength,
                "candle_strength": 0.0,
                "upper_wick_ratio": 0.0,
                "lower_wick_ratio": 0.0,
            },
            "score": {
                "signed_score": (
                    -abs(signal.score)
                    if self._signal_direction(signal) == "SHORT"
                    else abs(signal.score)
                ),
                "raw_score": signal.score,
                "base_threshold": signal.threshold,
                "dynamic_threshold": signal.threshold,
                "calculated_threshold": signal.calculated_threshold or signal.threshold,
                "edge": abs(signal.score) - signal.threshold,
                "threshold_adjustments": {},
                "components": {},
                "quality_score": signal.quality_score,
                "quality_score_version": signal.quality_score_version,
                "quality_score_mode": signal.quality_score_mode,
                "quality_score_context": signal.quality_score_context,
                "quality_score_components": deepcopy(signal.quality_score_components),
                "quality_score_inputs": deepcopy(signal.quality_score_inputs),
            },
            "volume_price": {},
            "indicators": {},
            "context": {
                "mtf_10m_bias": signal.mtf_10m_bias,
                "mtf_30m_bias": signal.mtf_30m_bias,
                "regime": signal.regime,
                "fear_greed": {},
                "daily_7d_14d": {
                    "version": signal.daily_profile_version,
                    "selected": signal.daily_profile_selected,
                    "selection": deepcopy(self.active_daily_profile_selection),
                    "7d": {"status": "NOT_AVAILABLE"},
                    "14d": {"status": "NOT_AVAILABLE"},
                },
                "n12_n20": {
                    "version": "ADAPTIVE_PROFILE_PENDING",
                    "status": "NOT_AVAILABLE",
                    "evaluated_at": 0,
                    "n12": {"status": "NOT_AVAILABLE", "sample_size": 0},
                    "n20": {"status": "NOT_AVAILABLE", "sample_size": 0},
                },
                "wave": {},
                "direction_pulse": deepcopy(signal.direction_pulse_shadow),
                "profile_summary_cache": {
                    "source_revision": None,
                    "current_revision": None,
                    "stale": False,
                    "cache_status": "NOT_AVAILABLE",
                },
            },
            "admission": admission,
            "entry_structure": deepcopy(entry_structure),
            "signal": self._signal_context_payload(fallback_signal),
            "audit_snapshot": {
                "freeze_error_type": type(freeze_error).__name__,
                "freeze_error": str(freeze_error)[:200],
            },
        }

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
        run: _DecisionRun | None = None,
        selected_order_terms: dict[str, object] | None = None,
    ):
        config = self._decision_runtime_config()
        direction = self._signal_direction(signal)
        normalized_decision = str(decision or "UNKNOWN").upper()
        open_allowed = normalized_decision == "OPENED"
        if run is None:
            run = self._new_decision_run(
                signal,
                latest,
                candidate_origin=candidate_origin,
                candidate_ordinal=candidate_ordinal,
            )
        if not run.trace_records:
            if normalized_decision == "OPENED":
                run.trace(
                    "ADMISSION",
                    "PASS",
                    "OPENED",
                    {"selected_order_terms": deepcopy(selected_order_terms or {})},
                )
            elif normalized_decision == "RESEARCH_OBSERVE":
                run.trace(
                    "OBSERVATION",
                    "PASS",
                    "RESEARCH_OBSERVE",
                    {"observation_allowed": bool(observation_allowed)},
                )
            else:
                run.trace(
                    self._trace_stage_for_decision(normalized_decision),
                    "BLOCK",
                    normalized_decision,
                    {"final_decision": normalized_decision},
                )
        inputs = self._canonical_decision_inputs(
            run,
            signal,
            latest,
            audit_context=audit_context,
            selected_order_terms=selected_order_terms,
            observation_allowed=bool(observation_allowed),
        )
        run.builder.capture_inputs(inputs)
        for record in run.trace_records:
            run.builder.trace(
                str(record["stage"]),
                str(record["result"]),
                record["decisive_values"],
                str(record["reason_code"]),
            )
        context = run.builder.finish(
            normalized_decision,
            str(final_reason or ""),
            open_allowed,
            bool(observation_allowed),
            selected_order_terms=selected_order_terms,
        )
        context_payload = context.to_dict()
        canonical_inputs = context_payload["inputs"]
        quality_score_inputs = canonical_inputs["score"]["quality_score_inputs"]
        enriched_signal = replace(
            signal,
            direction=direction or signal.direction,
            decision_id=context.decision_id,
            context_version=context.context_version,
            runtime_config_hash=context.runtime_config_hash,
            strategy_build_id=context.strategy_build_id,
            candidate_origin=context.candidate_origin,
            quality_score_inputs=quality_score_inputs,
            decision_inputs=canonical_inputs,
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

    @staticmethod
    def _trace_stage_for_decision(decision: str) -> str:
        return {
            "BELOW_THRESHOLD": "SCORE",
            "OVERHEATED": "SCORE",
            "EDGE_TOO_SMALL": "SCORE",
            "SESSION_BLOCKED": "SESSION",
            "DAILY_PROFILE_NOT_SELECTED": "DAILY_PROFILE",
            "ADAPTIVE_PROFILE_PAUSED": "ADAPTIVE_PROFILE",
            "ADAPTIVE_PROFILE_SECOND_BLOCKED": "ADAPTIVE_PROFILE",
            "WAVE_DIRECTION_BLOCKED": "WAVE_GUARD",
            "PROFILE_GUARD_BLOCKED": "PROFILE_HEALTH",
            "HOLD_OPEN_ORDER": "CAPACITY",
            "COOLDOWN": "COOLDOWN",
        }.get(decision, "ADMISSION")

    def _maybe_open_order(
        self,
        signal: Signal,
        latest: Kline,
        *,
        daily_profile_required: bool = False,
    ) -> str:
        with self._lock:
            operation_context = (self.symbol, self._symbol_generation)
            closed_klines = list(self.klines)
        if not closed_klines or closed_klines[-1].close_time != latest.close_time:
            closed_klines = [
                item for item in closed_klines if item.close_time < latest.close_time
            ]
            closed_klines.append(latest)
        self._entry_structure_market_snapshot(closed_klines, operation_context)
        with self._lock:
            if not self._matches_symbol_context(operation_context):
                return "STALE_CONTEXT"
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
        admission_context: ProfileAdmissionContext | None = None
        admission_decision: ProfileAdmissionDecision | None = None
        self.profile_guard_audit = self._empty_profile_guard_audit()
        if not self.enable_profile_guard:
            self._update_profile_summary_audit(
                self._profile_guard_shadow_source()
            )
        signal = self._attach_direction_pulse_shadow(signal, current_time=latest.close_time)
        signal = self._attach_adaptive_profile_state(
            signal,
            current_time=latest.close_time,
            daily_profile_required=daily_profile_required,
        )
        precomputed_gate = None
        if (
            not self.enable_daily_profile_selector
            and self.enable_observation_profile_promotion
        ):
            precomputed_gate = self.order_policy.evaluate(
                signal,
                latest,
                self.simulator.orders,
                self._last_order_opened_at,
                self._opened_signal_keys,
            )
            promotion_candidate = self._observation_profile_promoted_signal(
                signal,
                latest,
                precomputed_gate.code,
            )
            if promotion_candidate is not None:
                signal = self._attach_quality_score(
                    promotion_candidate,
                    current_time=latest.close_time,
                )
                precomputed_gate = self.order_policy.evaluate(
                    signal,
                    latest,
                    self.simulator.orders,
                    self._last_order_opened_at,
                    self._opened_signal_keys,
                )
        candidate_origin = self._formal_candidate_origin(signal)
        if signal.candidate_origin != candidate_origin:
            signal = replace(signal, candidate_origin=candidate_origin)
        candidate_ordinal = self._profile_admission_candidate_ordinal(signal)
        if daily_profile_required and self._signal_direction(signal) in {"LONG", "SHORT"}:
            signal, admission_context, admission_decision = (
                self._recompute_profile_admission(
                    signal,
                    current_time=latest.close_time,
                    candidate_origin=candidate_origin,
                    candidate_ordinal=candidate_ordinal,
                )
            )
        signal = self._attach_entry_structure_snapshot(
            signal,
            latest,
            candidate_origin,
        )
        self.selected_signal = signal
        run = self._new_decision_run(
            signal,
            latest,
            candidate_origin=candidate_origin,
            candidate_ordinal=candidate_ordinal,
        )
        reused_decision = self._reuse_committed_decision(signal, latest, run)
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
                self._candidate_error_outcome(
                    signal,
                    latest,
                    run,
                    stage="PERSISTENCE",
                    reason="资格取消持久化失败",
                    error=RuntimeError(self.last_error or "progression credit cancellation failed"),
                )
                return "STORAGE_ERROR"
            self._wave_bootstrap_cancel_pending = False
        elif batch_decision.mode != "RECOVERY":
            stale_source_ids = self._stale_progression_credit_source_ids(signal)
            if stale_source_ids and not self._cancel_pending_progression_credits(
                stale_source_ids,
                self._signal_direction(signal),
            ):
                self._candidate_error_outcome(
                    signal,
                    latest,
                    run,
                    stage="PERSISTENCE",
                    reason="资格取消持久化失败",
                    error=RuntimeError(self.last_error or "progression credit cancellation failed"),
                )
                return "STORAGE_ERROR"
            self._wave_bootstrap_cancel_pending = False
        else:
            self._wave_bootstrap_cancel_pending = False
        if signal.wave_guard_mode == "DIRECTION_BLOCKED":
            run.trace(
                "WAVE_GUARD",
                "BLOCK",
                "WAVE_BLOCKED",
                {
                    "mode": signal.wave_guard_mode,
                    "state": signal.wave_state,
                    "direction": signal.observe_direction,
                    "status": signal.wave_guard_status,
                },
            )
            return self._block_order(
                signal,
                latest,
                "WAVE_DIRECTION_BLOCKED",
                f"1分钟波段 {signal.wave_state} 不允许 {signal.observe_direction}",
                should_observe=should_observe,
                run=run,
            )
        run.trace(
            "WAVE_GUARD",
            "PASS",
            "WAVE_ALLOWED",
            {
                "mode": signal.wave_guard_mode,
                "state": signal.wave_state,
                "direction": self._signal_direction(signal),
                "status": signal.wave_guard_status,
            },
        )
        qualification_state = str(
            signal.adaptive_profile_state.get("qualification_state", "")
        )
        if (
            daily_profile_required
            and admission_decision is not None
            and admission_decision.code == "DAILY_PROFILE_NOT_SELECTED"
        ):
            run.trace(
                "DAILY_PROFILE",
                "BLOCK",
                "PROFILE_NOT_SELECTED",
                {
                    "required": daily_profile_required,
                    "selected": signal.daily_profile_selected,
                    "profile_key": signal.profile_key,
                    "version": signal.daily_profile_version,
                    "qualification_state": qualification_state,
                    "admission": self._profile_admission_payload(
                        admission_context,
                        admission_decision,
                    ),
                },
            )
            return self._block_order(
                signal,
                latest,
                "DAILY_PROFILE_NOT_SELECTED",
                "当前信号未进入今日启用画像，仅记录观察",
                should_observe=should_observe,
                run=run,
            )
        run.trace(
            "DAILY_PROFILE",
            "PASS",
            "DAILY_PROFILE_ALLOWED",
            {
                "required": daily_profile_required,
                "selected": signal.daily_profile_selected,
                "profile_key": signal.profile_key,
                "version": signal.daily_profile_version,
                "qualification_state": qualification_state,
                "admission": (
                    self._profile_admission_payload(
                        admission_context,
                        admission_decision,
                    )
                    if admission_context is not None and admission_decision is not None
                    else None
                ),
            },
        )

        adaptive = signal.adaptive_profile_state
        adaptive_status = str(
            admission_context.adaptive_state
            if daily_profile_required and admission_context is not None
            else "NOT_APPLICABLE"
        )
        adaptive_values = {
            "profile_key": str(adaptive.get("profile_key", signal.profile_key)),
            "qualification_state": str(adaptive.get("qualification_state", "")),
            "status": adaptive_status,
            "evaluated_at": int(adaptive.get("evaluated_at", 0) or 0),
            "n12": deepcopy(adaptive.get("n12", {})),
            "n20": deepcopy(adaptive.get("n20", {})),
            "order_slot": signal.order_slot,
            "order_slot_scope": signal.order_slot_scope,
            "admission": (
                self._profile_admission_payload(
                    admission_context,
                    admission_decision,
                )
                if admission_context is not None and admission_decision is not None
                else None
            ),
        }
        if (
            admission_decision is not None
            and signal.order_slot == "SECOND"
            and not admission_decision.allow_second_order
        ):
            block_code = self._profile_admission_runtime_code(admission_decision.code)
            run.trace(
                "ADAPTIVE_PROFILE",
                "BLOCK",
                block_code,
                adaptive_values,
            )
            return self._block_order(
                signal,
                latest,
                block_code,
                "当前画像准入决定禁止同方向第二席位",
                should_observe=True,
                run=run,
            )
        if admission_decision is not None and not admission_decision.allowed:
            block_code = self._profile_admission_runtime_code(admission_decision.code)
            run.trace(
                "ADAPTIVE_PROFILE",
                "BLOCK",
                block_code,
                adaptive_values,
            )
            return self._block_order(
                signal,
                latest,
                block_code,
                f"当前画像准入被阻止：{admission_decision.code}",
                should_observe=True,
                run=run,
            )
        run.trace(
            "ADAPTIVE_PROFILE",
            "PASS",
            (
                admission_decision.code
                if admission_decision is not None
                else "ADAPTIVE_PROFILE_NOT_APPLICABLE"
            ),
            adaptive_values,
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
            run.trace(
                "PROFILE_HEALTH_SHORT_WINDOW",
                "BLOCK",
                "PROFILE_HEALTH_BLOCKED",
                health_decision.to_dict(),
            )
            return self._block_order(
                signal,
                latest,
                "PROFILE_HEALTH_BLOCKED",
                health_decision.reason,
                should_observe=should_observe,
                run=run,
            )
        run.trace(
            "PROFILE_HEALTH_SHORT_WINDOW",
            "PASS",
            "PROFILE_HEALTH_ALLOWED",
            health_decision.to_dict(),
        )

        signal, gate = self._admit_order_candidate(
            signal,
            latest,
            precomputed_gate=precomputed_gate,
        )
        run.extend(gate.decision_trace)
        if not gate.open_allowed:
            if not self._persist_blocked_decision(
                signal,
                latest,
                gate.code,
                signal.reason,
                should_observe=should_observe,
                run=run,
            ):
                return "STORAGE_ERROR"
            return gate.code
        if (
            signal.direction == "SHORT"
            and not signal.daily_profile_selected
            and not (
                admission_decision is not None
                and admission_decision.channel == "FAST"
            )
            and signal.threshold_segment.upper() not in self.live_short_segments
        ):
            run.trace(
                "SHORT_MODE",
                "BLOCK",
                "SHORT_OBSERVE_ONLY",
                {
                    "direction": signal.direction,
                    "segment": signal.threshold_segment,
                    "daily_profile_selected": signal.daily_profile_selected,
                    "live_short_segments": sorted(self.live_short_segments),
                    "admission_channel": (
                        admission_decision.channel if admission_decision else "NONE"
                    ),
                },
            )
            return self._block_order(
                signal,
                latest,
                "SHORT_OBSERVE_ONLY",
                "SHORT观察模式：仅记录信号，不开模拟订单，不推送Webhook",
                should_observe=True,
                run=run,
            )
        run.trace(
            "SHORT_MODE",
            "PASS",
            "SHORT_MODE_ALLOWED",
            {
                "direction": signal.direction,
                "segment": signal.threshold_segment,
                "daily_profile_selected": signal.daily_profile_selected,
                "admission_channel": (
                    admission_decision.channel if admission_decision else "NONE"
                ),
            },
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
            run.trace(
                "WAVE_BATCH",
                "BLOCK",
                batch_decision.code,
                self.wave_batch_guard,
            )
            return self._block_order(
                signal,
                latest,
                batch_decision.code,
                batch_decision.reason,
                should_observe=should_observe,
                run=run,
            )
        run.trace(
            "WAVE_BATCH",
            "PASS",
            batch_decision.code,
            self.wave_batch_guard,
        )
        if batch_decision.mode == "RECOVERY":
            signal = replace(signal, wave_guard_mode="RECOVERY")
            self.selected_signal = signal
        if profile_decision.status in {"COOLDOWN", "RECOVERY_PENDING"}:
            run.trace(
                "PROFILE_DEGRADATION",
                "BLOCK",
                "PROFILE_DEGRADATION_BLOCKED",
                self.profile_degradation_guard,
            )
            return self._block_order(
                signal,
                latest,
                "PROFILE_DEGRADATION_BLOCKED",
                profile_decision.reason,
                should_observe=should_observe,
                run=run,
            )
        run.trace(
            "PROFILE_DEGRADATION",
            "PASS",
            "PROFILE_DEGRADATION_ALLOWED",
            self.profile_degradation_guard,
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
            run.trace(
                "RESULT_SEQUENCE",
                "BLOCK",
                "RESULT_SEQUENCE_GUARD_BLOCKED",
                self.result_sequence_guard,
            )
            return self._block_order(
                signal,
                latest,
                "RESULT_SEQUENCE_GUARD_BLOCKED",
                sequence_decision.reason,
                should_observe=should_observe,
                run=run,
            )
        run.trace(
            "RESULT_SEQUENCE",
            "PASS",
            "RESULT_SEQUENCE_ALLOWED",
            self.result_sequence_guard,
        )
        if self.enable_rolling_edge_guard and self.rolling_edge["status"] == "DEGRADED":
            run.trace(
                "ROLLING_EDGE",
                "BLOCK",
                "ROLLING_EDGE_BLOCKED",
                self.rolling_edge,
            )
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
                run=run,
            )
        run.trace(
            "ROLLING_EDGE",
            "PASS",
            "ROLLING_EDGE_ALLOWED",
            self.rolling_edge,
        )
        if self.enable_profile_guard:
            try:
                profile_guard = self._profile_guard_shadow(signal)
            except Exception as exc:  # noqa: BLE001 - 正式守卫不可用 stale 静默放行。
                self._decision_storage_failed = True
                self._set_storage_error("画像守卫精确摘要读取失败", exc)
                self._candidate_error_outcome(
                    signal,
                    latest,
                    run,
                    stage="PERSISTENCE",
                    reason="画像守卫精确摘要读取失败",
                    error=exc,
                )
                return "STORAGE_ERROR"
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
                "cache_status": str(
                    profile_guard.get("cache_status") or "UNKNOWN"
                ),
                "source_revision": profile_guard.get("source_revision"),
                "current_revision": profile_guard.get("current_revision"),
                "stale": bool(profile_guard.get("stale", False)),
            }
            if profile_guard["status"] == "WOULD_BLOCK":
                run.trace(
                    "PROFILE_HEALTH",
                    "BLOCK",
                    "PROFILE_GUARD_BLOCKED",
                    self.profile_guard_audit,
                )
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
                    run=run,
                )
        run.trace(
            "PROFILE_HEALTH",
            "PASS",
            (
                "PROFILE_GUARD_PASS"
                if self.enable_profile_guard
                else "PROFILE_GUARD_DISABLED"
            ),
            self.profile_guard_audit,
        )

        if time_period_decision.blocked:
            run.trace(
                "TIME_PERIOD",
                "BLOCK",
                time_period_decision.code,
                time_period_decision.to_dict(),
            )
            return self._block_order(
                signal,
                latest,
                time_period_decision.code,
                time_period_decision.reason,
                should_observe=True,
                run=run,
            )
        run.trace(
            "TIME_PERIOD",
            "PASS",
            time_period_decision.code,
            time_period_decision.to_dict(),
        )

        if signal.order_slot == "SECOND" and not health_decision.allow_second_order:
            run.trace(
                "PROFILE_HEALTH_SECOND_ORDER",
                "BLOCK",
                "PROFILE_HEALTH_SECOND_ORDER_BLOCKED",
                health_decision.to_dict(),
            )
            return self._block_order(
                signal,
                latest,
                "PROFILE_HEALTH_SECOND_ORDER_BLOCKED",
                health_decision.reason,
                should_observe=should_observe,
                run=run,
            )
        run.trace(
            "PROFILE_HEALTH_SECOND_ORDER",
            "PASS",
            "PROFILE_HEALTH_SECOND_ORDER_ALLOWED",
            {
                "order_slot": signal.order_slot,
                "allow_second_order": health_decision.allow_second_order,
                "status": health_decision.status,
            },
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
                and (
                    admission_decision.allow_progression
                    if admission_decision is not None
                    else True
                )
            ),
            run=run,
        )

    def _block_order(
        self,
        signal: Signal,
        latest: Kline,
        code: str,
        reason: str,
        *,
        should_observe: bool,
        run: _DecisionRun,
    ) -> str:
        if not self._persist_blocked_decision(
            signal,
            latest,
            code,
            reason,
            should_observe=should_observe,
            run=run,
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
        run: _DecisionRun,
    ) -> bool:
        if not signal.actionable and not should_observe:
            audit_context = self._current_signal_audit_context()
            _config, _context, enriched_signal, _audit = self._decision_artifacts(
                signal,
                latest,
                code,
                final_reason=reason,
                candidate_origin=self._formal_candidate_origin(signal),
                candidate_ordinal=0,
                observation_allowed=False,
                audit_context=audit_context,
                event_kind="DECISIVE_BLOCK",
                run=run,
            )
            self.selected_signal = enriched_signal
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
                run=run,
                entry_structure_attached=True,
            )
            if self._decision_storage_failed:
                return False
            if recorded:
                return True
        audit_context = self._current_signal_audit_context()
        config, context, enriched_signal, audit = self._decision_artifacts(
            signal,
            latest,
            code,
            final_reason=reason,
            candidate_origin=self._formal_candidate_origin(signal),
            candidate_ordinal=0,
            observation_allowed=False,
            audit_context=audit_context,
            event_kind="DECISIVE_BLOCK",
            run=run,
        )
        self.selected_signal = enriched_signal
        if not self.storage:
            return True
        try:
            self.storage.save_decision_bundle(
                config=config,
                context=context,
                audit=audit,
            )
        except Exception as exc:  # noqa: BLE001 - 核心决策包失败必须阻断本轮。
            self._decision_storage_failed = True
            self._set_storage_error("决策持久化失败", exc)
            self._candidate_error_outcome(
                enriched_signal,
                latest,
                run,
                stage="PERSISTENCE",
                reason="决策持久化失败",
                error=exc,
                base_context=context,
            )
            return False
        self._bundled_decision_ids.add(context.decision_id)
        return True

    def _admit_order_candidate(
        self,
        signal: Signal,
        latest: Kline,
        *,
        precomputed_gate: OrderGate | None = None,
    ) -> tuple[Signal, OrderGate]:
        gate = precomputed_gate or self.order_policy.evaluate(
            signal,
            latest,
            self.simulator.orders,
            self._last_order_opened_at,
            self._opened_signal_keys,
        )
        signal = self._attach_quality_score(signal, current_time=latest.close_time)
        return signal, gate

    def _execute_open_order(
        self,
        signal: Signal,
        latest: Kline,
        gate: OrderGate,
        *,
        should_observe: bool,
        allow_progression: bool,
        run: _DecisionRun,
    ) -> str:
        audit_context = self._current_signal_audit_context()
        pending_observation = (
            self._new_observation(signal, latest, "OPENED")
            if should_observe
            else None
        )
        order, consumed_credit = self.simulator.open_order_with_credit(
            signal,
            entry_price=latest.close,
            opened_at=latest.close_time,
            allow_progression=allow_progression,
        )
        selected_order_terms = {
            "stake": order.stake,
            "win_return": order.win_return,
            "progression_step": order.stake_progression_step,
            "progression_source_order_id": order.stake_progression_source_order_id,
            "progression_version": order.stake_progression_version,
            "progression_limits": {
                "max_orders": self.stake_progression_max_orders,
                "max_active": self.stake_progression_max_active,
            },
            "allow_progression": allow_progression,
            "expires_at": order.expires_at,
            "timeframe_minutes": order.timeframe_minutes,
            "order_slot": order.order_slot,
            "order_slot_scope": order.order_slot_scope,
            "direction": order.direction,
            "entry_price": order.entry_price,
        }
        run.trace(
            "ADMISSION",
            "PASS",
            "OPENED",
            {"selected_order_terms": selected_order_terms},
        )
        try:
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
                run=run,
                selected_order_terms=selected_order_terms,
            )
        except Exception as exc:  # noqa: BLE001 - 冻结失败必须回滚并形成失败 outcome。
            self.simulator.rollback_open_order(order.id)
            self._decision_storage_failed = True
            self._set_storage_error("决策冻结失败", exc)
            self._candidate_error_outcome(
                signal,
                latest,
                run,
                stage="PERSISTENCE",
                reason="决策冻结失败",
                error=exc,
            )
            return "STORAGE_ERROR"
        self.selected_signal = signal
        order.decision_id = signal.decision_id
        order.context_version = signal.context_version
        order.runtime_config_hash = signal.runtime_config_hash
        order.strategy_build_id = signal.strategy_build_id
        order.candidate_origin = signal.candidate_origin
        order.decision_inputs = deepcopy(signal.decision_inputs)
        order.decision_trace = deepcopy(signal.decision_trace)
        order.first_decisive_block = signal.first_decisive_block
        bind_canonical_quality_score_inputs(order)
        if pending_observation is not None:
            context_payload = context.to_dict()
            pending_observation = replace(
                pending_observation,
                decision_id=context.decision_id,
                context_version=context.context_version,
                runtime_config_hash=context.runtime_config_hash,
                strategy_build_id=context.strategy_build_id,
                candidate_origin=context.candidate_origin,
                entry_structure_shadow=deepcopy(signal.entry_structure_shadow),
                quality_score_inputs=deepcopy(signal.quality_score_inputs),
                decision_inputs=context_payload["inputs"],
                decision_trace=context_payload["decision_trace"],
                first_decisive_block=context.first_decisive_block,
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
                self._candidate_error_outcome(
                    signal,
                    latest,
                    run,
                    stage="PERSISTENCE",
                    reason="开单持久化失败",
                    error=exc,
                    base_context=context,
                )
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
        latest_effective_from = self._snapshot_time_ms(latest, "effective_from")
        if (
            latest is None
            or not self._daily_profile_snapshot_is_safe(latest, target, current_time)
            or latest_effective_from != target["effective_from"]
            or not self._daily_profile_config_matches(latest, config)
        ):
            previous = self._load_daily_profile_selection_as_of(
                target["lookback_end"],
                current_time,
                target["effective_from"],
                target["effective_until"],
            )
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
        if (
            self._daily_profile_snapshot_is_safe(
                self.active_daily_profile_selection,
                target,
                current_time,
            )
            and self._selection_is_effective(self.active_daily_profile_selection, current_time)
        ):
            return
        self.active_daily_profile_selection = None
        loader = getattr(self.storage, "load_daily_profile_selection", None) if self.storage else None
        active = loader(self.symbol, current_time) if loader is not None else None
        self.active_daily_profile_selection = (
            active
            if self._daily_profile_snapshot_is_safe(active, target, current_time)
            else None
        )

    @staticmethod
    def _daily_profile_config_matches(
        snapshot: dict,
        config: DailyProfileSelectorConfig,
    ) -> bool:
        stored = snapshot.get("config")
        if not isinstance(stored, dict):
            return False
        return all(stored.get(key) == value for key, value in config.__dict__.items())

    def _attach_adaptive_profile_state(
        self,
        signal: Signal,
        *,
        current_time: int,
        daily_profile_required: bool,
    ) -> Signal:
        if not daily_profile_required:
            return signal
        direction = self._signal_direction(signal)
        if direction not in {"LONG", "SHORT"}:
            return signal
        key = daily_profile_key(
            signal.timeframe_minutes,
            signal.strategy_family,
            signal.strategy_tag,
            direction,
            signal.threshold_segment,
        )
        selection = self.active_daily_profile_selection or {}
        qualification = next(
            (
                item
                for item in selection.get("selected_profiles", [])
                if isinstance(item, dict) and str(item.get("key", "")) == key
            ),
            None,
        )
        state = deepcopy(self.adaptive_profile_states.get(key))
        if state is None:
            state = evaluate_adaptive_profile_state((), key, int(current_time))
        state.update(
            {
                "profile_key": key,
                "qualification_state": (
                    str(qualification.get("qualification_state") or "QUALIFIED")
                    if qualification is not None
                    else "NOT_QUALIFIED"
                ),
                "qualification_version": str(
                    (qualification or {}).get("version") or QUALIFICATION_VERSION
                ),
                "qualification_evaluated_at": int(
                    selection.get("evaluated_at", 0) or 0
                ),
                "daily_profile_version": str(selection.get("version", "")),
                "joint_failure_runs": int(
                    (qualification or {}).get("joint_failure_runs", 0) or 0
                ),
                "fast_7d": deepcopy((qualification or {}).get("fast_7d", {})),
                "stable_14d": deepcopy(
                    (qualification or {}).get("stable_14d", {})
                ),
            }
        )
        previous_adaptive = signal.adaptive_profile_state
        if isinstance(previous_adaptive, dict):
            for key in ("admission", "admission_candidates"):
                if key in previous_adaptive:
                    state[key] = deepcopy(previous_adaptive[key])
        return replace(signal, adaptive_profile_state=state)

    def _profile_admission_status(self) -> dict[str, object]:
        return {
            "enabled": self.profile_admission_policy.fast_enabled,
            "policy": self.profile_admission_policy.to_dict(),
            "policy_hash": self.profile_admission_policy.policy_hash,
            "policy_version": self.profile_admission_policy.version,
            "stability_proven": False,
            "release_allowed": False,
            "release_status": "BLOCKED",
            "release_reason": "前向稳定性尚未证明，当前策略仅用于冻结审计",
        }

    @staticmethod
    def _profile_admission_payload(
        context: ProfileAdmissionContext,
        decision: ProfileAdmissionDecision,
    ) -> dict[str, object]:
        return {
            "context": context.to_dict(),
            "decision": decision.to_dict(),
            "stability_proven": False,
            "release_allowed": False,
        }

    @staticmethod
    def _profile_admission_runtime_code(code: str) -> str:
        return {
            "ADAPTIVE_PAUSED": "ADAPTIVE_PROFILE_PAUSED",
            "WATCH_SECOND_ORDER_BLOCKED": "ADAPTIVE_PROFILE_SECOND_BLOCKED",
        }.get(code, code)

    def _profile_admission_context(
        self,
        signal: Signal,
        *,
        current_time: int,
        candidate_origin: str,
        candidate_ordinal: int,
    ) -> ProfileAdmissionContext:
        direction = self._signal_direction(signal)
        key = daily_profile_key(
            signal.timeframe_minutes,
            signal.strategy_family,
            signal.strategy_tag,
            direction,
            signal.threshold_segment,
        )
        selected_profiles = (self.active_daily_profile_selection or {}).get(
            "selected_profiles",
            [],
        )
        selected_profile = next(
            (
                (rank, item)
                for rank, item in enumerate(selected_profiles, start=1)
                if isinstance(item, dict) and str(item.get("key", "")) == key
            ),
            None,
        )
        daily_rank, qualification = selected_profile or (None, {})
        qualification_state = str(
            qualification.get("qualification_state") or (
                "QUALIFIED" if selected_profile is not None else "NOT_QUALIFIED"
            )
        )
        daily_selected = bool(
            signal.daily_profile_selected
            and selected_profile is not None
            and qualification_state in {"QUALIFIED", "QUALIFICATION_WATCH"}
        )
        adaptive = signal.adaptive_profile_state
        if not isinstance(adaptive, dict) or str(adaptive.get("profile_key", "")) != key:
            adaptive = deepcopy(self.adaptive_profile_states.get(key))
            if adaptive is None:
                adaptive = evaluate_adaptive_profile_state((), key, int(current_time))
        n12 = adaptive.get("n12", {}) if isinstance(adaptive.get("n12"), dict) else {}
        n20 = adaptive.get("n20", {}) if isinstance(adaptive.get("n20"), dict) else {}
        open_order_count = sum(
            1
            for order in self.simulator.orders
            if order.status == "OPEN"
            and str(order.direction or "").upper() == direction
        )
        return ProfileAdmissionContext(
            profile_key=key,
            direction=direction,
            order_slot="SECOND" if open_order_count > 0 else "FIRST",
            daily_selected=daily_selected,
            qualification_state=qualification_state,
            daily_rank=daily_rank,
            daily_win_rate=float(qualification.get("win_rate", 0.0) or 0.0),
            adaptive_state=str(adaptive.get("status", "WARMUP") or "WARMUP"),
            adaptive_transition=str(adaptive.get("transition", "") or ""),
            adaptive_evaluated_at=int(adaptive.get("evaluated_at", 0) or 0),
            n12_sample_size=int(n12.get("sample_size", 0) or 0),
            n12_wins=int(n12.get("wins", 0) or 0),
            n20_sample_size=int(n20.get("sample_size", 0) or 0),
            n20_ev=float(n20.get("ev", 0.0) or 0.0),
            candidate_origin=candidate_origin,
            candidate_ordinal=candidate_ordinal,
        )

    def _recompute_profile_admission(
        self,
        signal: Signal,
        *,
        current_time: int,
        candidate_origin: str,
        candidate_ordinal: int = 0,
    ) -> tuple[Signal, ProfileAdmissionContext, ProfileAdmissionDecision]:
        context = self._profile_admission_context(
            signal,
            current_time=current_time,
            candidate_origin=candidate_origin,
            candidate_ordinal=candidate_ordinal,
        )
        decision = evaluate_profile_admission(context, self.profile_admission_policy)
        adaptive = deepcopy(signal.adaptive_profile_state)
        adaptive["admission"] = self._profile_admission_payload(context, decision)
        return (
            replace(
                signal,
                profile_key=context.profile_key,
                daily_profile_selected=context.daily_selected,
                order_slot=context.order_slot,
                order_slot_scope="DIRECTION_V2",
                adaptive_profile_state=adaptive,
            ),
            context,
            decision,
        )

    @staticmethod
    def _profile_admission_candidate_ordinal(signal: Signal) -> int:
        adaptive = signal.adaptive_profile_state
        admission = adaptive.get("admission") if isinstance(adaptive, dict) else None
        context = admission.get("context") if isinstance(admission, dict) else None
        value = context.get("candidate_ordinal") if isinstance(context, dict) else 0
        return value if type(value) is int and value >= 0 else 0

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

        raw_candidates = [primary_signal, *observation_candidates]
        prepared: dict[int, tuple[Signal, ProfileAdmissionContext]] = {}
        eligible_contexts: list[ProfileAdmissionContext] = []
        for candidate_ordinal, signal in enumerate(raw_candidates):
            direction = (signal.observe_direction or signal.direction).upper()
            if direction not in {"LONG", "SHORT"}:
                continue
            candidate_origin = self._origin_before_profile_promotion(signal)
            key = daily_profile_key(
                signal.timeframe_minutes,
                signal.strategy_family,
                signal.strategy_tag,
                direction,
                signal.threshold_segment,
            )
            candidate = replace(
                signal,
                direction=direction,
                observe_direction=direction,
                profile_key=key,
                daily_profile_selected=any(
                    isinstance(item, dict)
                    and str(item.get("key", "")) == key
                    and str(item.get("qualification_state") or "QUALIFIED")
                    in {"QUALIFIED", "QUALIFICATION_WATCH"}
                    for item in snapshot.get("selected_profiles", [])
                ),
                daily_profile_version=str(snapshot.get("version", "")),
                candidate_origin=candidate_origin,
            )
            candidate = self._attach_adaptive_profile_state(
                candidate,
                current_time=current_time,
                daily_profile_required=True,
            )
            context = self._profile_admission_context(
                candidate,
                current_time=current_time,
                candidate_origin=candidate_origin,
                candidate_ordinal=candidate_ordinal,
            )
            candidate = replace(
                candidate,
                daily_profile_selected=context.daily_selected,
                order_slot=context.order_slot,
                order_slot_scope="DIRECTION_V2",
            )
            prepared[candidate_ordinal] = (candidate, context)
            if context.daily_selected or candidate_ordinal > 0:
                eligible_contexts.append(context)

        selected = select_admitted_candidate(
            eligible_contexts,
            self.profile_admission_policy,
        )
        candidate_admissions = [
            self._profile_admission_payload(
                context,
                evaluate_profile_admission(context, self.profile_admission_policy),
            )
            for context in eligible_contexts
        ]
        if selected is not None:
            signal, _context = prepared[selected.context.candidate_ordinal]
            selected_profile = next(
                (
                    item
                    for item in snapshot.get("selected_profiles", [])
                    if isinstance(item, dict)
                    and str(item.get("key", "")) == selected.context.profile_key
                ),
                {},
            )
            calculated_threshold = (
                signal.calculated_threshold
                if signal.calculated_threshold > 0
                else signal.threshold
            )
            if selected.decision.channel == "RESIDENT":
                reason = (
                    f"{self._without_dynamic_threshold_block(signal)}；"
                    f"每日画像启用 {snapshot.get('version', '')} "
                    f"N{selected_profile.get('sample_size', 0)} "
                    f"胜率{float(selected_profile.get('win_rate', 0.0)):.2%} "
                    f"EV{float(selected_profile.get('ev', 0.0)):.2f}U"
                )
            else:
                reason = (
                    f"{self._without_dynamic_threshold_block(signal)}；"
                    f"画像快速通道 {selected.decision.code}"
                )
            adaptive = deepcopy(signal.adaptive_profile_state)
            adaptive["admission"] = self._profile_admission_payload(
                selected.context,
                selected.decision,
            )
            adaptive["admission_candidates"] = candidate_admissions
            return (
                replace(
                    signal,
                    reason=reason,
                    calculated_threshold=calculated_threshold,
                    session_allowed=True,
                    session_sample_size=int(selected_profile.get("sample_size", 0) or 0),
                    session_win_rate=float(selected_profile.get("win_rate", 0.0) or 0.0),
                    session_ev=float(selected_profile.get("ev", 0.0) or 0.0),
                    observe_only=False,
                    adaptive_profile_state=adaptive,
                ),
                True,
            )
        adaptive = deepcopy(primary_signal.adaptive_profile_state)
        adaptive["admission_candidates"] = candidate_admissions
        return replace(primary_signal, adaptive_profile_state=adaptive), True

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

    def _load_daily_profile_selection_as_of(
        self,
        evaluation_key: int,
        evaluated_at: int,
        effective_from: int,
        effective_until: int,
    ) -> dict | None:
        if not self.storage:
            candidate = self.daily_profile_selection
        else:
            loader = getattr(self.storage, "load_daily_profile_selection_as_of", None)
            if loader is not None:
                candidate = loader(
                    self.symbol,
                    evaluation_key,
                    evaluated_at_ms=evaluated_at,
                )
            else:
                candidate = self._load_latest_daily_profile_selection()
        return (
            candidate
            if self._is_snapshot_as_of(
                candidate,
                evaluation_key=evaluation_key,
                evaluated_at=evaluated_at,
                effective_from=effective_from,
                effective_until=effective_until,
            )
            else None
        )

    @staticmethod
    def _is_snapshot_as_of(
        snapshot: dict | None,
        *,
        evaluation_key: int,
        evaluated_at: int,
        effective_from: int,
        effective_until: int,
    ) -> bool:
        if not isinstance(snapshot, dict) or not snapshot:
            return False
        limits = (
            ("evaluation_key", evaluation_key),
            ("lookback_start", evaluation_key),
            ("lookback_end", evaluation_key),
            ("evaluated_at", evaluated_at),
            ("evaluation_time", evaluated_at),
            ("effective_from", effective_from),
            ("effective_until", effective_until),
        )
        found = False
        for field, limit in limits:
            if field not in snapshot:
                continue
            found = True
            value = MonitorState._snapshot_time_ms(snapshot, field)
            if value is None or value > limit:
                return False
        return found

    @staticmethod
    def _snapshot_time_ms(snapshot: dict | None, field: str) -> int | None:
        if not isinstance(snapshot, dict) or field not in snapshot:
            return None
        value = snapshot[field]
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            return int(value)
        except ValueError:
            return None

    @classmethod
    def _daily_profile_snapshot_is_safe(
        cls,
        snapshot: dict | None,
        target: dict,
        evaluated_at: int,
    ) -> bool:
        return cls._is_snapshot_as_of(
            snapshot,
            evaluation_key=target["lookback_end"],
            evaluated_at=evaluated_at,
            effective_from=target["effective_from"],
            effective_until=target["effective_until"],
        )

    @classmethod
    def _selection_is_effective(cls, snapshot: dict | None, current_time: int) -> bool:
        if not snapshot:
            return False
        effective_from = cls._snapshot_time_ms(snapshot, "effective_from")
        effective_until = cls._snapshot_time_ms(snapshot, "effective_until")
        return (
            snapshot.get("status") in {"READY", "FALLBACK"}
            and effective_from is not None
            and effective_until is not None
            and effective_from <= current_time
            and effective_until > current_time
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
                "status": "ERROR",
                "evaluated_at": evaluated_at,
                "evaluation_key": target["lookback_end"],
                **target,
                "selected_profiles": [],
                "selected_count": 0,
                "reason": f"每日画像评估失败，沿用静态基准：{error}",
                "error": error,
            }

        def safe_rows(name: str) -> list[dict]:
            rows = previous.get(name, [])
            if not isinstance(rows, list):
                return []

            def is_safe(item: dict) -> bool:
                for field, limit in (
                    ("evaluation_key", target["lookback_end"]),
                    ("lookback_end", target["lookback_end"]),
                    ("evaluated_at", evaluated_at),
                ):
                    if item.get(field) is None:
                        continue
                    try:
                        if int(item[field]) > limit:
                            return False
                    except (TypeError, ValueError):
                        return False
                return True

            return [
                dict(item)
                for item in rows
                if isinstance(item, dict)
                and is_safe(item)
            ]

        fallback = dict(previous)
        selected_profiles = safe_rows("selected_profiles")
        fallback.update(
            {
                "version": f"{previous.get('version', 'DPS')}-FALLBACK",
                "status": "FALLBACK",
                "evaluated_at": evaluated_at,
                "evaluation_key": target["lookback_end"],
                **target,
                "candidates": safe_rows("candidates"),
                "selected_profiles": selected_profiles,
                "selected_count": len(selected_profiles),
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
        adaptive_formal_block = decision in {
            "ADAPTIVE_PROFILE_PAUSED",
            "ADAPTIVE_PROFILE_SECOND_BLOCKED",
        }
        key = self._observation_key(
            signal,
            direction,
            exact_profile=adaptive_formal_block,
        )
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
                and (
                    not adaptive_formal_block
                    or item.strategy_tag == signal.strategy_tag
                )
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
            adaptive_profile_state=deepcopy(signal.adaptive_profile_state),
            entry_structure_shadow=deepcopy(signal.entry_structure_shadow),
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
        run: _DecisionRun | None = None,
        entry_structure_attached: bool = False,
    ) -> bool:
        direction = signal.observe_direction or signal.direction
        if direction not in {"LONG", "SHORT"}:
            return False
        signal = replace(
            signal,
            direction=direction,
            candidate_origin=candidate_origin,
        )
        if not entry_structure_attached:
            signal = self._attach_entry_structure_snapshot(
                signal,
                latest,
                candidate_origin,
            )
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
            run=run,
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
            entry_structure_shadow=deepcopy(enriched_signal.entry_structure_shadow),
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
                if primary_decision and run is not None:
                    self._candidate_error_outcome(
                        enriched_signal,
                        latest,
                        run,
                        stage="PERSISTENCE",
                        reason="决策持久化失败",
                        error=exc,
                        base_context=context,
                    )
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

    def _record_observation_candidates(
        self,
        signals: Sequence[Signal],
        latest: Kline,
        market_snapshot: dict[str, object] | None = None,
    ) -> None:
        for candidate_ordinal, signal in enumerate(signals, start=1):
            direction = signal.observe_direction or signal.direction
            if direction not in {"LONG", "SHORT"}:
                continue
            signal = replace(
                signal,
                direction=direction,
                candidate_origin="RESEARCH_OBSERVATION",
            )
            signal = self._attach_entry_structure_snapshot(
                signal,
                latest,
                "RESEARCH_OBSERVATION",
                market_snapshot,
            )
            if self._has_open_research_observation(signal, latest):
                continue
            self._record_observation(
                signal,
                latest,
                "RESEARCH_OBSERVE",
                candidate_origin="RESEARCH_OBSERVATION",
                candidate_ordinal=candidate_ordinal,
                entry_structure_attached=True,
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
                affected_profile_keys = {
                    self._adaptive_profile_key(item) for item in settled
                }
                self._refresh_adaptive_profile_keys(
                    affected_profile_keys,
                    max(int(item.settled_at) for item in settled) + 1,
                )
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
                    self.daily_profile_selector_config.effective_stable_lookback_days,
                ),
            )
        return self.storage.load_observations(self.symbol)

    def _load_adaptive_profile_observations(
        self,
        fallback: Sequence[ObservationSignal],
        *,
        evaluated_at: int,
        profile_keys: set[str] | None = None,
    ) -> list[ObservationSignal]:
        loader = (
            getattr(self.storage, "load_adaptive_profile_observations", None)
            if self.storage
            else None
        )
        if loader is not None:
            return loader(
                self.symbol,
                lookback_days=15,
                evaluated_at=evaluated_at,
                profile_keys=profile_keys,
            )
        return self._filter_adaptive_profile_observations(
            fallback,
            evaluated_at=evaluated_at,
            profile_keys=profile_keys,
        )

    def _load_adaptive_profile_observations_fail_safe(
        self,
        fallback: Sequence[ObservationSignal],
        *,
        evaluated_at: int,
    ) -> list[ObservationSignal]:
        try:
            return self._load_adaptive_profile_observations(
                fallback,
                evaluated_at=evaluated_at,
            )
        except Exception as exc:  # noqa: BLE001 - 恢复阶段使用已加载的目标币种历史降级。
            self.last_error = f"自适应画像数据加载失败: {exc}"
            try:
                return self._filter_adaptive_profile_observations(
                    fallback,
                    evaluated_at=evaluated_at,
                )
            except Exception as fallback_exc:  # noqa: BLE001 - 保持构造/重置状态完整。
                self.last_error = (
                    f"自适应画像数据加载失败: {exc}; "
                    f"fallback failed: {fallback_exc}"
                )
                return []

    def _filter_adaptive_profile_observations(
        self,
        fallback: Sequence[ObservationSignal],
        *,
        evaluated_at: int,
        profile_keys: set[str] | None = None,
    ) -> list[ObservationSignal]:
        cutoff = evaluated_at - 15 * DAY_MS
        rows = []
        for observation in fallback:
            if (
                observation.status != "SETTLED"
                or observation.result not in {"WIN", "LOSS"}
                or observation.settled_at is None
                or observation.settled_at < cutoff
                or observation.settled_at >= evaluated_at
            ):
                continue
            key = self._adaptive_profile_key(observation)
            if profile_keys and key not in profile_keys:
                continue
            rows.append(observation)
        return rows

    @staticmethod
    def _adaptive_profile_key(observation: ObservationSignal) -> str:
        return daily_profile_key(
            observation.timeframe_minutes,
            observation.strategy_family,
            observation.strategy_tag,
            observation.direction,
            observation.threshold_segment,
        )

    def _rebuild_all_adaptive_profile_states(self, evaluated_at: int) -> bool:
        try:
            states = rebuild_adaptive_profile_states(
                self._adaptive_profile_observations,
                evaluated_at,
            )
        except Exception as exc:  # noqa: BLE001 - 启动失败保留可见错误和安全空状态。
            self.last_error = f"自适应画像状态重建失败: {exc}"
            return False
        self.adaptive_profile_states = states
        return True

    def _refresh_adaptive_profile_keys(
        self,
        profile_keys: set[str],
        evaluated_at: int,
    ) -> bool:
        keys = {str(item) for item in profile_keys if str(item)}
        if not keys:
            return True
        try:
            rows = self._load_adaptive_profile_observations(
                self.observations,
                evaluated_at=evaluated_at,
                profile_keys=keys,
            )
            rebuilt = rebuild_adaptive_profile_states(rows, evaluated_at)
            next_states = {}
            for key in keys:
                next_states[key] = rebuilt.get(key) or evaluate_adaptive_profile_state(
                    (),
                    key,
                    evaluated_at,
                )
        except Exception as exc:  # noqa: BLE001 - 结算已提交，刷新失败只保留旧状态。
            self.last_error = f"自适应画像状态刷新失败: {exc}"
            return False
        self._adaptive_profile_observations = [
            item
            for item in self._adaptive_profile_observations
            if self._adaptive_profile_key(item) not in keys
        ]
        self._adaptive_profile_observations.extend(rows)
        self.adaptive_profile_states.update(next_states)
        adaptive_error_prefixes = (
            "自适应画像数据加载失败:",
            "自适应画像状态重建失败:",
            "自适应画像状态刷新失败:",
        )
        if self.last_error and self.last_error.startswith(adaptive_error_prefixes):
            self.last_error = None
        return True

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
    def _observation_key(
        signal: Signal,
        direction: str,
        *,
        exact_profile: bool = False,
    ) -> str:
        if not exact_profile:
            return (
                f"{signal.open_time}|{signal.timeframe_minutes}|"
                f"{direction}|{signal.strategy_tag}"
            )
        return "|".join(
            (
                str(signal.open_time),
                str(signal.timeframe_minutes),
                signal.strategy_family,
                signal.strategy_tag,
                direction,
                signal.threshold_segment,
            )
        )

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
        snapshot["signal"] = decision_linked_storage_payload(
            signal,
            retain_extended_views=True,
        )
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
        source = self._profile_guard_shadow_source()
        result = profile_guard_shadow(
            signal,
            source,
            use_recommended=True,
        )
        if isinstance(source, dict):
            result.update(
                {
                    key: source.get(key)
                    for key in (
                        "cache_status",
                        "source_revision",
                        "current_revision",
                        "stale",
                    )
                }
            )
        return result

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
            if (
                self.enable_profile_guard
                and isinstance(self._cached_profile_source, dict)
                and bool(self._cached_profile_source.get("stale", False))
            ):
                exact_loader = getattr(
                    self.storage,
                    "exact_order_profile_summary",
                    None,
                )
                if exact_loader is None:
                    raise RuntimeError(
                        "stale profile summary requires exact fallback"
                    )
                self._cached_profile_source = exact_loader(
                    self.symbol,
                    profile_guard_min_history=self.profile_guard_min_history,
                    profile_guard_min_group_size=self.profile_guard_min_group_size,
                )
            return self._cached_profile_source
        except Exception:
            if self.enable_profile_guard:
                raise
            self._cached_profile_source = None
            return None

    def _update_profile_summary_audit(self, source: dict | None) -> None:
        if not isinstance(source, dict):
            return
        self.profile_guard_audit.update(
            {
                "cache_status": str(source.get("cache_status") or "UNKNOWN"),
                "source_revision": source.get("source_revision"),
                "current_revision": source.get("current_revision"),
                "stale": bool(source.get("stale", False)),
            }
        )

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
        candidates = [
            self._profile_api_labels(item)
            for item in latest.get("candidates", [])
            if isinstance(item, dict)
        ]
        qualifications = {str(item.get("key", "")): item for item in candidates}
        immediate_profiles = []
        for key, value in sorted(self.adaptive_profile_states.items()):
            item = deepcopy(value)
            item.setdefault("profile_key", key)
            qualification = qualifications.get(key, {})
            for field in ("qualification_state", "fast_7d", "stable_14d"):
                if field in qualification:
                    item[field] = deepcopy(qualification[field])
            immediate_profiles.append(self._profile_api_labels(item))
        config = self.daily_profile_selector_config
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
            "fast_7d": deepcopy(
                latest.get(
                    "fast_7d",
                    {
                        "lookback_days": config.lookback_days,
                        "lookback_start": None,
                        "lookback_end": None,
                    },
                )
            ),
            "stable_14d": deepcopy(
                latest.get(
                    "stable_14d",
                    {
                        "lookback_days": config.effective_stable_lookback_days,
                        "lookback_start": None,
                        "lookback_end": None,
                    },
                )
            ),
            "candidates": candidates,
            "immediate_state": {"profiles": immediate_profiles},
            "evaluation_time_label": self._shanghai_clock_label(
                config.evaluation_hour,
                config.evaluation_minute,
            ),
            "activation_time_label": self._shanghai_clock_label(
                config.activation_hour,
                config.activation_minute,
            ),
            "config": {
                **config.__dict__,
                "effective_stable_lookback_days": (
                    self.daily_profile_selector_config.effective_stable_lookback_days
                ),
                "stable_lookback_source": (
                    self.daily_profile_selector_config.stable_lookback_source
                ),
                "effective_joint_failures_to_exit": (
                    self.daily_profile_selector_config.joint_failures_to_exit
                ),
            },
        }

    @staticmethod
    def _shanghai_clock_label(hour: int, minute: int) -> str:
        return f"{int(hour):02d}:{int(minute):02d} Asia/Shanghai"

    @staticmethod
    def _profile_api_labels(profile: dict) -> dict:
        item = deepcopy(profile)
        segment = str(
            item.get("threshold_segment")
            or str(item.get("profile_key") or item.get("key") or "").rsplit("|", 1)[-1]
        ).upper()
        if len(segment) == 5 and segment[:3] in {"WD-", "WE-"} and segment[3:].isdigit():
            utc_hour = int(segment[3:])
            if 0 <= utc_hour <= 23:
                shanghai_hour = (utc_hour + 8) % 24
                next_day = utc_hour >= 16
                item["threshold_segment"] = segment
                item["utc_segment_label"] = f"{utc_hour:02d}:00-{utc_hour:02d}:59 UTC"
                item["shanghai_segment_label"] = (
                    f"{'次日' if next_day else '当日'}{shanghai_hour:02d}:00-"
                    f"{shanghai_hour:02d}:59 Asia/Shanghai"
                )
                item["shanghai_segment_crosses_day"] = next_day
        return item

    def _entry_structure_status(self) -> dict:
        if self.selected_signal and self.selected_signal.entry_structure_shadow:
            return deepcopy(self.selected_signal.entry_structure_shadow)
        return {
            "entry_structure_version": "UNKNOWN",
            "entry_structure_mode": "SHADOW_ONLY",
            "entry_structure_evaluated_at": 0,
            "entry_structure_state": "UNKNOWN",
            "entry_structure_bias": "UNKNOWN",
            "entry_structure_reason_code": "STRUCTURE_NOT_EVALUATED",
            "candidate_origin": "UNKNOWN",
            "active_level_source": "UNKNOWN",
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

    def close(self) -> None:
        failure = None
        try:
            self.wait_for_storage_writes()
        except Exception as exc:  # noqa: BLE001 - 关闭完成后保留原异步失败。
            failure = exc
        finally:
            self._storage_executor.shutdown(wait=True)
            closer = getattr(self.storage, "close", None) if self.storage else None
            if closer is not None:
                closer()
        if failure is not None:
            raise failure

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
            "cache_status": "UNKNOWN",
            "source_revision": None,
            "current_revision": None,
            "stale": False,
        }

    def _send_webhook(self, signal: Signal, order=None) -> None:
        if not self.webhook:
            return
        try:
            self.webhook.send_signal(self.symbol, signal, amount=order.stake if order else None)
        except Exception:  # noqa: BLE001 - 最低延迟模式明确静默丢弃分发异常。
            return

    def snapshot(self) -> dict:
        storage_capacity = self._sample_storage_capacity(int(self._now_ms()))
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
                "profile_admission": self._profile_admission_status(),
                "storage_capacity": storage_capacity,
                "entry_structure_shadow": self._entry_structure_status(),
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
        candidate_origin: str = "",
        qualification_state: str = "",
        adaptive_state: str = "",
        entry_structure_state: str = "",
        entry_structure_bias: str = "",
        active_level_source: str = "",
    ) -> dict:
        if self.storage:
            page_payload = self.storage.page_observations(
                self.symbol,
                page=page,
                page_size=page_size,
                direction=direction,
                family=family,
                tag=tag,
                segment=segment,
                result=result,
                origin=candidate_origin,
                qualification_state=qualification_state,
                adaptive_state=adaptive_state,
                entry_structure_state=entry_structure_state,
                entry_structure_bias=entry_structure_bias,
                active_level_source=active_level_source,
            )
            return self._normalize_observation_page(
                page_payload,
                candidate_origin=candidate_origin,
            )
        with self._lock:
            observations = list(self.observations)
        extended_filters = {
            "candidate_origin": candidate_origin,
            "qualification_state": qualification_state,
            "adaptive_state": adaptive_state,
            "entry_structure_state": entry_structure_state,
            "entry_structure_bias": entry_structure_bias,
            "active_level_source": active_level_source,
        }
        normalized_filters = {
            field: str(value or "").strip().upper()
            for field, value in extended_filters.items()
        }
        normalized_observations = [
            (item, self._normalize_observation_row(item.to_dict()))
            for item in observations
        ]
        diagnostic_filter_options = {
            field: sorted({row[field] for _, row in normalized_observations})
            for field in extended_filters
        }
        observations = [
            item
            for item, row in normalized_observations
            if all(
                not expected
                or row[field] == expected
                for field, expected in normalized_filters.items()
            )
        ]
        page_payload = page_observation_list(
            observations,
            page=page,
            page_size=page_size,
            direction=direction,
            family=family,
            tag=tag,
            segment=segment,
            result=result,
        )
        page_payload["filters"].update(normalized_filters)
        page_payload["filter_options"].update(diagnostic_filter_options)
        return self._normalize_observation_page(
            page_payload,
            candidate_origin=candidate_origin,
        )

    @classmethod
    def _normalize_observation_page(
        cls,
        payload: dict,
        *,
        candidate_origin: str = "",
    ) -> dict:
        normalized = dict(payload)
        normalized["observations"] = [
            cls._normalize_observation_row(item)
            for item in payload.get("observations", [])
        ]
        filters = dict(payload.get("filters", {}))
        filters["candidate_origin"] = str(
            filters.get("candidate_origin")
            or filters.get("origin")
            or candidate_origin
            or ""
        ).strip().upper()
        normalized["filters"] = filters
        filter_options = dict(payload.get("filter_options", {}))
        origins = filter_options.get("candidate_origin") or filter_options.get("origin") or []
        filter_options["candidate_origin"] = list(origins)
        normalized["filter_options"] = filter_options
        return normalized

    @staticmethod
    def _normalize_observation_row(row: dict) -> dict:
        normalized = deepcopy(row)
        adaptive = normalized.get("adaptive_profile_state")
        adaptive = adaptive if isinstance(adaptive, Mapping) else {}
        structure = normalized.get("entry_structure_shadow")
        structure = structure if isinstance(structure, Mapping) else {}

        def api_value(value: object) -> str:
            text = str(value or "").strip().upper()
            return text or "UNKNOWN"

        normalized["context_version"] = str(
            normalized.get("context_version") or "LEGACY"
        )
        normalized["candidate_origin"] = api_value(normalized.get("candidate_origin"))
        normalized["qualification_state"] = api_value(
            adaptive.get("qualification_state")
        )
        normalized["adaptive_state"] = api_value(
            adaptive.get("state") or adaptive.get("status")
        )
        normalized["entry_structure_state"] = api_value(
            structure.get("entry_structure_state") or structure.get("state")
        )
        normalized["entry_structure_bias"] = api_value(
            structure.get("entry_structure_bias") or structure.get("bias")
        )
        normalized["active_level_source"] = api_value(
            structure.get("active_level_source")
        )
        return normalized

    def observation_summary(self, *, window: str = "14d") -> dict:
        normalized_window = str(window or "14d").strip().lower()
        if normalized_window not in {"7d", "14d", "30d", "all"}:
            normalized_window = "14d"
        if self.storage:
            summary = self.storage.observation_summary(
                self.symbol,
                window=normalized_window,
            )
        else:
            with self._lock:
                observations = list(self.observations)
            anchor = max((item.opened_at for item in observations), default=None)
            cutoff = None
            if anchor is not None and normalized_window != "all":
                cutoff = anchor - int(normalized_window[:-1]) * DAY_MS
                observations = [item for item in observations if item.opened_at >= cutoff]
            summary = summarize_observations(observations)
            summary.update(
                {"window": normalized_window, "cutoff": cutoff, "anchor": anchor}
            )
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
