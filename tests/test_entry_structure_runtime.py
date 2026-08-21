import threading
import unittest
from dataclasses import replace
from unittest.mock import patch

from app.daily_profile_selector import profile_key as daily_profile_key
from app.entry_structure_shadow import EntryStructureGate, StructureConfig
from app.models import Kline, ObservationSignal, Signal, SimulatedOrder
from app.result_sequence_guard import ResultSequenceGuardConfig
from app.state import MonitorState
from app.wave_state import WaveSnapshot


class EntryStructureRuntimeTest(unittest.TestCase):
    class Uncopyable:
        def __deepcopy__(self, memo):
            raise RuntimeError("snapshot copy failed")

    class DetectorWithUncopyableDetail:
        config = StructureConfig()

        def detect(self, symbol, closed_klines):
            return {
                "version": "ENTRY_STRUCTURE_SHADOW_V1",
                "mode": "SHADOW_ONLY",
                "status": "READY",
                "symbol": symbol,
                "evaluated_at": closed_klines[-1].close_time,
                "levels": [],
                "nearest_support": None,
                "nearest_resistance": None,
                "detail": EntryStructureRuntimeTest.Uncopyable(),
            }

    class EmptyStateMachine:
        config = StructureConfig()

        def evaluate(self, detected, closed_klines):
            return []

    class ErrorDetector:
        config = StructureConfig()

        def detect(self, symbol, closed_klines):
            raise RuntimeError("detector failed")

    class NestedGate:
        def attach(self, signal, market_snapshot, candidate_origin):
            evaluated_at = (
                market_snapshot.get("evaluated_at", signal.open_time)
                if isinstance(market_snapshot, dict)
                else signal.open_time
            )
            return {
                "entry_structure_version": "ENTRY_STRUCTURE_SHADOW_V1",
                "entry_structure_mode": "SHADOW_ONLY",
                "entry_structure_evaluated_at": evaluated_at,
                "entry_structure_state": "NO_NEARBY_LEVEL",
                "entry_structure_bias": "NEUTRAL",
                "entry_structure_reason_code": "STRUCTURE_NO_NEARBY_LEVEL",
                "candidate_origin": candidate_origin,
                "candidate_direction": signal.direction,
                "detail": {"levels": [99.0, 101.0]},
            }

    class NoStructureGate:
        def attach(self, signal, market_snapshot, candidate_origin):
            return {}

    class CountingGate:
        def __init__(self):
            self.calls = []

        def attach(self, signal, market_snapshot, candidate_origin):
            self.calls.append((candidate_origin, signal.direction))
            return {
                "entry_structure_version": "ENTRY_STRUCTURE_SHADOW_V1",
                "entry_structure_mode": "SHADOW_ONLY",
                "entry_structure_evaluated_at": market_snapshot.get(
                    "evaluated_at",
                    0,
                ),
                "entry_structure_state": "INSUFFICIENT_DATA",
                "entry_structure_bias": "NEUTRAL",
                "entry_structure_reason_code": "STRUCTURE_INSUFFICIENT_DATA",
                "candidate_origin": candidate_origin,
                "candidate_direction": signal.direction,
            }

    class DirectionAwareGate:
        def attach(self, signal, market_snapshot, candidate_origin):
            direction = signal.direction
            return {
                "entry_structure_version": "ENTRY_STRUCTURE_SHADOW_V1",
                "entry_structure_mode": "SHADOW_ONLY",
                "entry_structure_evaluated_at": market_snapshot.get(
                    "evaluated_at",
                    0,
                ),
                "entry_structure_state": "SUPPORT_REJECTED",
                "entry_structure_bias": (
                    "CONFIRMED" if direction in {"LONG", "SHORT"} else "NEUTRAL"
                ),
                "entry_structure_reason_code": "STRUCTURE_SUPPORT_REJECTED",
                "candidate_origin": candidate_origin,
                "candidate_direction": direction,
            }

    class IntegerKeyGate:
        def attach(self, signal, market_snapshot, candidate_origin):
            return {
                "entry_structure_version": "ENTRY_STRUCTURE_SHADOW_V1",
                "entry_structure_mode": "SHADOW_ONLY",
                "entry_structure_evaluated_at": market_snapshot.get(
                    "evaluated_at",
                    0,
                ),
                "entry_structure_state": "NO_NEARBY_LEVEL",
                "entry_structure_bias": "NEUTRAL",
                "entry_structure_reason_code": "STRUCTURE_NO_NEARBY_LEVEL",
                "candidate_origin": candidate_origin,
                "candidate_direction": signal.direction,
                "detail": {1: "integer-key"},
            }

    class BlockingDetector:
        config = StructureConfig()

        def __init__(self):
            self.calls = 0
            self.started = threading.Event()
            self.release = threading.Event()
            self._lock = threading.Lock()

        def detect(self, symbol, closed_klines):
            with self._lock:
                self.calls += 1
            self.started.set()
            if not self.release.wait(5):
                raise TimeoutError("detector release timed out")
            return {
                "version": "ENTRY_STRUCTURE_SHADOW_V1",
                "mode": "SHADOW_ONLY",
                "status": "READY",
                "symbol": symbol,
                "evaluated_at": closed_klines[-1].close_time,
                "levels": [],
                "nearest_support": None,
                "nearest_resistance": None,
                "detail": {"levels": [99.0, 101.0]},
            }

    class RecordingWebhook:
        def __init__(self):
            self.calls = []

        def send_signal(self, symbol, signal, message=None, amount=None):
            self.calls.append(
                (
                    symbol,
                    signal.direction,
                    signal.timeframe_minutes,
                    signal.reason if message is None else message,
                    amount,
                )
            )

        def status(self):
            return {"enabled": True, "last_error": None}

    @staticmethod
    def bars(count=30, base_time=10 * 86_400_000):
        return [
            Kline(
                open_time=base_time + index * 60_000,
                open=100.0,
                high=100.2,
                low=99.8,
                close=100.0 + index / 100.0,
                volume=100.0,
                close_time=base_time + (index + 1) * 60_000 - 1,
            )
            for index in range(count)
        ]

    @staticmethod
    def signal(latest):
        return Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="runtime structure fixture",
            price=latest.close,
            open_time=latest.open_time,
            score=90.0,
            threshold=70.0,
            threshold_segment="WD-08",
            session_allowed=True,
            strategy_family="drop_reclaim",
            strategy_tag="runtime_structure",
            observe_direction="LONG",
        )

    @staticmethod
    def settled_observation(index, opened_at):
        return ObservationSignal(
            observation_key=f"runtime-history-{index}",
            strategy_family="drop_reclaim",
            strategy_tag="runtime_structure",
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="runtime profile history",
            entry_price=100.0,
            opened_at=opened_at,
            expires_at=opened_at + 600_000,
            threshold_segment="WD-08",
            score=81.0,
            threshold=70.0,
            edge=11.0,
            source_decision="SESSION_BLOCKED",
            status="SETTLED",
            result="WIN",
            exit_price=101.0,
            settled_at=opened_at + 600_000,
            pnl=8.0,
        )

    @staticmethod
    def new_state(**kwargs):
        return MonitorState(
            symbol="BTCUSDT",
            min_order_gap_ms=0,
            enable_rolling_edge_guard=False,
            result_sequence_guard_config=ResultSequenceGuardConfig(enabled=False),
            **kwargs,
        )

    def test_uncopyable_raw_snapshot_degrades_to_error_without_blocking_order(self):
        bars = self.bars()
        signal = self.signal(bars[-1])
        state = self.new_state(
            enable_observation_profile_promotion=False,
        )
        self.addCleanup(state.close)
        state._entry_structure_detector = self.DetectorWithUncopyableDetail()
        state._entry_structure_state_machine = self.EmptyStateMachine()

        with (
            patch("app.state.analyze_volume_price", return_value=signal),
            patch("app.state.analyze_observation_signals", return_value=[]),
            patch("app.state.choose_trade_signal", return_value=signal),
        ):
            updated = state.update_from_klines(bars)

        self.assertTrue(updated)
        self.assertEqual(state.order_decision, "OPENED")
        self.assertEqual(len(state.simulator.orders), 1)
        self.assertEqual(
            state.simulator.orders[0].entry_structure_shadow[
                "entry_structure_state"
            ],
            "ERROR",
        )

    def test_each_formal_and_research_candidate_maps_structure_once(self):
        bars = self.bars()
        latest = bars[-1]
        formal = self.signal(latest)
        research = replace(
            formal,
            direction="WAIT",
            observe_direction="SHORT",
            observe_only=True,
            strategy_family="short_extension",
            strategy_tag="runtime_short_observe",
            score=-60.0,
            threshold=70.0,
            session_allowed=False,
        )
        state = self.new_state(enable_observation_profile_promotion=False)
        self.addCleanup(state.close)
        gate = self.CountingGate()
        state._entry_structure_gate = gate

        with (
            patch("app.state.analyze_volume_price", return_value=formal),
            patch("app.state.analyze_observation_signals", return_value=[research]),
            patch("app.state.choose_trade_signal", return_value=formal),
        ):
            state.update_from_klines(bars)

        self.assertEqual(
            gate.calls,
            [
                ("NATIVE_ACTIONABLE", "LONG"),
                ("RESEARCH_OBSERVATION", "SHORT"),
            ],
        )

    def test_daily_promoted_wait_uses_final_direction_and_promoted_origin(self):
        bars = self.bars()
        latest = bars[-1]
        state = self.new_state(
            enable_daily_profile_selector=True,
            enable_observation_profile_promotion=False,
        )
        self.addCleanup(state.close)
        primary = replace(
            self.signal(latest),
            direction="WAIT",
            observe_direction="LONG",
            strategy_tag="unselected",
            score=0.0,
            daily_profile_selected=False,
        )
        candidate = replace(
            self.signal(latest),
            direction="WAIT",
            observe_direction="SHORT",
            strategy_family="short_extension",
            strategy_tag="runtime_short_observe",
            score=-60.0,
            threshold=70.0,
            session_allowed=False,
            observe_only=True,
            daily_profile_selected=False,
        )
        key = daily_profile_key(
            10,
            candidate.strategy_family,
            candidate.strategy_tag,
            "SHORT",
            candidate.threshold_segment,
        )
        state.active_daily_profile_selection = {
            "version": "DPS-RUNTIME",
            "status": "READY",
            "selected_profiles": [
                {"key": key, "sample_size": 20, "win_rate": 0.7, "ev": 2.0}
            ],
        }
        promoted, required = state._select_daily_profile_signal(
            primary,
            [candidate],
            latest.close_time,
        )
        state.klines = bars

        state._maybe_open_order(
            promoted,
            latest,
            daily_profile_required=required,
        )

        selected = state.selected_signal
        self.assertEqual(selected.direction, "SHORT")
        self.assertEqual(selected.candidate_origin, "PROFILE_PROMOTED_WAIT")
        self.assertEqual(
            selected.entry_structure_shadow["candidate_origin"],
            "PROFILE_PROMOTED_WAIT",
        )
        self.assertEqual(
            selected.entry_structure_shadow["candidate_direction"],
            "SHORT",
        )
        self.assertEqual(
            selected.decision_inputs["identity"]["candidate_origin"],
            "PROFILE_PROMOTED_WAIT",
        )

    def test_actionable_candidate_uses_real_policy_without_scanning_legacy_profile(self):
        bars = self.bars()
        latest = bars[-1]
        state = self.new_state(
            enable_daily_profile_selector=False,
            enable_observation_profile_promotion=True,
            observation_profile_min_samples=12,
            observation_profile_min_win_rate=0.72,
            observation_profile_min_ev=4.0,
            observation_profile_min_edge=10.0,
        )
        self.addCleanup(state.close)
        state.klines = bars
        history_start = latest.close_time - 2 * 86_400_000
        observations = [
            self.settled_observation(index, history_start + index * 660_000)
            for index in range(12)
        ]
        observations[0].pnl = object()
        state.observations.extend(observations)
        signal = replace(
            self.signal(latest),
            score=81.0,
            threshold=70.0,
            session_allowed=True,
        )

        decision = state._maybe_open_order(signal, latest)

        self.assertEqual(decision, "OPENED")
        self.assertEqual(state.selected_signal.candidate_origin, "NATIVE_ACTIONABLE")
        self.assertEqual(
            state.selected_signal.decision_inputs["identity"]["candidate_origin"],
            "NATIVE_ACTIONABLE",
        )
        self.assertEqual(
            state.simulator.orders[0].entry_structure_shadow["candidate_origin"],
            "NATIVE_ACTIONABLE",
        )

    def test_wave_blocked_runtime_structures_use_final_direction(self):
        bars = self.bars()
        latest = bars[-1]
        for direction, allowed_direction in (("LONG", "SHORT"), ("SHORT", "LONG")):
            with self.subTest(direction=direction):
                state = self.new_state(
                    enable_wave_guard=True,
                    enable_observation_profile_promotion=False,
                )
                self.addCleanup(state.close)
                state.klines = bars
                state._entry_structure_gate = self.DirectionAwareGate()
                signal = replace(
                    self.signal(latest),
                    direction=direction,
                    observe_direction=direction,
                    score=90.0 if direction == "LONG" else -90.0,
                    candidate_origin="NATIVE_ACTIONABLE",
                )
                wave = WaveSnapshot(
                    state="UP_LEG" if allowed_direction == "LONG" else "DOWN_LEG",
                    raw_state="UP_LEG" if allowed_direction == "LONG" else "DOWN_LEG",
                    window=8,
                    efficiency=0.8,
                    direction_ratio=0.8,
                    atr_strength=2.0,
                    range_position=0.5,
                    confirmations=2,
                    confirmed_at=latest.close_time - 60_000,
                    allowed_directions=(allowed_direction,),
                )

                guarded = state._apply_wave_guard(signal, wave)
                decision = state._maybe_open_order(guarded, latest)

                self.assertEqual(guarded.direction, "WAIT")
                self.assertEqual(decision, "WAVE_DIRECTION_BLOCKED")
                final_signal = state.selected_signal
                observation = state.observations[-1]
                for candidate in (final_signal, observation):
                    self.assertEqual(candidate.direction, direction)
                    self.assertEqual(
                        candidate.entry_structure_shadow["candidate_direction"],
                        direction,
                    )
                    self.assertEqual(
                        candidate.entry_structure_shadow["entry_structure_bias"],
                        "CONFIRMED",
                    )

    def test_native_candidate_evaluates_order_policy_once(self):
        bars = self.bars()
        latest = bars[-1]
        state = self.new_state(
            enable_daily_profile_selector=False,
            enable_observation_profile_promotion=True,
        )
        self.addCleanup(state.close)
        state.klines = bars
        base_evaluate = state.order_policy.evaluate
        policy_calls = []

        def count_evaluate(_policy, *args, **kwargs):
            policy_calls.append(args[0].direction)
            return base_evaluate(*args, **kwargs)

        with patch(
            "app.order_policy.OrderPolicy.evaluate",
            autospec=True,
            side_effect=count_evaluate,
        ):
            decision = state._maybe_open_order(self.signal(latest), latest)

        self.assertEqual(decision, "OPENED")
        self.assertEqual(policy_calls, ["LONG"])

    def test_blocked_candidate_keeps_one_structure_value_across_runtime_objects(self):
        bars = self.bars()
        latest = bars[-1]
        state = self.new_state(
            max_open_orders=1,
            enable_observation_profile_promotion=False,
        )
        self.addCleanup(state.close)
        state.klines = bars
        state._entry_structure_gate = self.NestedGate()
        state.simulator.orders.append(
            SimulatedOrder(
                id=99,
                direction="SHORT",
                timeframe_minutes=10,
                level="A",
                reason="existing",
                entry_price=100.0,
                opened_at=latest.close_time - 60_000,
                expires_at=latest.close_time + 540_000,
            )
        )

        decision = state._maybe_open_order(self.signal(latest), latest)

        self.assertEqual(decision, "HOLD_OPEN_ORDER")
        selected = state.selected_signal
        observation = next(
            item for item in state.observations if item.decision_id == selected.decision_id
        )
        structures = (
            selected.entry_structure_shadow,
            selected.decision_inputs["entry_structure"],
            selected.decision_inputs["signal"]["entry_structure_shadow"],
            observation.entry_structure_shadow,
        )
        for structure in structures[1:]:
            self.assertEqual(structure, structures[0])
            self.assertIsNot(structure, structures[0])
            self.assertIsNot(structure["detail"], structures[0]["detail"])
            self.assertIsNot(
                structure["detail"]["levels"],
                structures[0]["detail"]["levels"],
            )
        structures[0]["detail"]["levels"].append(102.0)
        for structure in structures[1:]:
            self.assertEqual(structure["detail"]["levels"], [99.0, 101.0])

    def test_structure_modes_preserve_order_identity_and_webhook_payload(self):
        def run(mode):
            bars = self.bars()
            latest = bars[-1]
            webhook = self.RecordingWebhook()
            state = self.new_state(
                webhook=webhook,
                enable_observation_profile_promotion=False,
            )
            self.addCleanup(state.close)
            state.klines = bars
            if mode == "error":
                state._entry_structure_detector = self.ErrorDetector()
                state._entry_structure_state_machine = self.EmptyStateMachine()
                state._entry_structure_gate = EntryStructureGate(
                    state._entry_structure_detector,
                    state._entry_structure_state_machine,
                )
            elif mode == "disabled":
                state._entry_structure_gate = self.NoStructureGate()

            decision = state._maybe_open_order(self.signal(latest), latest)
            order = state.simulator.orders[0]
            identity = (
                order.id,
                order.direction,
                order.timeframe_minutes,
                order.reason,
                order.entry_price,
                order.opened_at,
                order.expires_at,
                order.stake,
                order.win_return,
                order.stake_progression_step,
                order.candidate_origin,
                order.decision_id,
            )
            return decision, identity, list(webhook.calls)

        baseline = run("disabled")
        self.assertEqual(run("normal"), baseline)
        self.assertEqual(run("error"), baseline)

    def test_stale_signal_structure_is_replaced_by_current_closed_kline_snapshot(self):
        bars = self.bars()
        latest = bars[-1]
        state = self.new_state(enable_observation_profile_promotion=False)
        self.addCleanup(state.close)
        state.klines = bars
        state._entry_structure_gate = self.NestedGate()
        state._entry_structure_market_cache[
            (state.symbol, state._symbol_generation, latest.close_time)
        ] = {
            "version": "ENTRY_STRUCTURE_SHADOW_V1",
            "mode": "SHADOW_ONLY",
            "status": "READY",
            "evaluated_at": latest.close_time,
            "states": [],
        }
        stale = {
            "entry_structure_version": "ENTRY_STRUCTURE_SHADOW_V1",
            "entry_structure_mode": "SHADOW_ONLY",
            "entry_structure_evaluated_at": latest.close_time - 60_000,
            "entry_structure_state": "SUPPORT_REJECTED",
            "entry_structure_bias": "CONFIRMED",
            "entry_structure_reason_code": "STALE",
            "candidate_origin": "NATIVE_ACTIONABLE",
            "candidate_direction": "LONG",
        }

        state._maybe_open_order(
            replace(self.signal(latest), entry_structure_shadow=stale),
            latest,
        )

        structure = state.selected_signal.entry_structure_shadow
        self.assertEqual(
            structure["entry_structure_evaluated_at"],
            latest.close_time,
        )
        self.assertEqual(structure["entry_structure_state"], "NO_NEARBY_LEVEL")

    def test_uncopyable_existing_structure_degrades_without_blocking_order(self):
        bars = self.bars()
        latest = bars[-1]
        state = self.new_state(enable_observation_profile_promotion=False)
        self.addCleanup(state.close)
        state.klines = bars
        existing = {
            "entry_structure_version": "ENTRY_STRUCTURE_SHADOW_V1",
            "entry_structure_mode": "SHADOW_ONLY",
            "entry_structure_evaluated_at": latest.close_time,
            "entry_structure_state": "SUPPORT_REJECTED",
            "entry_structure_bias": "CONFIRMED",
            "entry_structure_reason_code": "STRUCTURE_SUPPORT_REJECTED",
            "candidate_origin": "NATIVE_ACTIONABLE",
            "candidate_direction": "LONG",
            "detail": self.Uncopyable(),
        }

        decision = state._maybe_open_order(
            replace(self.signal(latest), entry_structure_shadow=existing),
            latest,
        )

        self.assertEqual(decision, "OPENED")
        self.assertEqual(
            state.selected_signal.entry_structure_shadow["entry_structure_state"],
            "ERROR",
        )
        self.assertEqual(
            state.simulator.orders[0].entry_structure_shadow["entry_structure_state"],
            "ERROR",
        )

    def test_non_json_existing_structure_degrades_without_blocking_order(self):
        bars = self.bars()
        latest = bars[-1]
        state = self.new_state(enable_observation_profile_promotion=False)
        self.addCleanup(state.close)
        state.klines = bars
        existing = {
            "entry_structure_version": "ENTRY_STRUCTURE_SHADOW_V1",
            "entry_structure_mode": "SHADOW_ONLY",
            "entry_structure_evaluated_at": latest.close_time,
            "entry_structure_state": "NO_NEARBY_LEVEL",
            "entry_structure_bias": "NEUTRAL",
            "entry_structure_reason_code": "STRUCTURE_NO_NEARBY_LEVEL",
            "candidate_origin": "NATIVE_ACTIONABLE",
            "candidate_direction": "LONG",
            "detail": {"levels": {99.0, 101.0}},
        }

        decision = state._maybe_open_order(
            replace(self.signal(latest), entry_structure_shadow=existing),
            latest,
        )

        self.assertEqual(decision, "OPENED")
        self.assertEqual(
            state.selected_signal.entry_structure_shadow["entry_structure_state"],
            "ERROR",
        )
        self.assertEqual(
            state.simulator.orders[0].entry_structure_shadow["entry_structure_state"],
            "ERROR",
        )

    def test_integer_key_structure_degrades_without_blocking_order(self):
        bars = self.bars()
        latest = bars[-1]
        state = self.new_state(enable_observation_profile_promotion=False)
        self.addCleanup(state.close)
        state.klines = bars
        state._entry_structure_gate = self.IntegerKeyGate()

        decision = state._maybe_open_order(self.signal(latest), latest)

        self.assertEqual(decision, "OPENED")
        self.assertEqual(len(state.simulator.orders), 1)
        for structure in (
            state.selected_signal.entry_structure_shadow,
            state.simulator.orders[0].entry_structure_shadow,
        ):
            self.assertEqual(structure["entry_structure_state"], "ERROR")
            self.assertEqual(structure["candidate_direction"], "LONG")

    def test_structure_snapshot_singleflight_does_not_block_state_lock_or_reset(self):
        bars = self.bars()
        state = self.new_state(enable_observation_profile_promotion=False)
        self.addCleanup(state.close)
        detector = self.BlockingDetector()
        state._entry_structure_detector = detector
        state._entry_structure_state_machine = self.EmptyStateMachine()
        context = state.capture_symbol_context()
        cache_key = (context[0], context[1], bars[-1].close_time)
        results = []

        def detect_snapshot():
            results.append(state._entry_structure_market_snapshot(bars, context))

        first = threading.Thread(target=detect_snapshot)
        second = threading.Thread(target=detect_snapshot)
        first.start()
        self.assertTrue(detector.started.wait(1))
        second.start()

        snapshot_done = threading.Event()
        snapshot_thread = threading.Thread(
            target=lambda: (state.snapshot(), snapshot_done.set())
        )
        snapshot_thread.start()
        snapshot_responsive = snapshot_done.wait(1)

        reset_done = threading.Event()
        reset_thread = threading.Thread(
            target=lambda: (state.reset_symbol("ETHUSDT"), reset_done.set())
        )
        reset_thread.start()
        reset_responsive = reset_done.wait(1)

        detector.release.set()
        for thread in (first, second, snapshot_thread, reset_thread):
            thread.join(2)

        self.assertTrue(snapshot_responsive)
        self.assertTrue(reset_responsive)
        self.assertTrue(all(not thread.is_alive() for thread in (first, second)))
        self.assertEqual(detector.calls, 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(state.capture_symbol_context(), ("ETHUSDT", 1))
        self.assertNotIn(cache_key, state._entry_structure_market_cache)

    def test_structure_snapshot_returns_isolated_owner_waiter_and_cache_copies(self):
        bars = self.bars()
        state = self.new_state(enable_observation_profile_promotion=False)
        self.addCleanup(state.close)
        detector = self.BlockingDetector()
        state._entry_structure_detector = detector
        state._entry_structure_state_machine = self.EmptyStateMachine()
        context = state.capture_symbol_context()
        cache_key = (context[0], context[1], bars[-1].close_time)
        results = {}

        def detect_snapshot(name):
            results[name] = state._entry_structure_market_snapshot(bars, context)

        owner = threading.Thread(target=detect_snapshot, args=("owner",))
        waiter = threading.Thread(target=detect_snapshot, args=("waiter",))
        owner.start()
        self.assertTrue(detector.started.wait(1))
        waiter.start()
        detector.release.set()
        owner.join(2)
        waiter.join(2)
        results["cache_hit"] = state._entry_structure_market_snapshot(bars, context)

        self.assertEqual(detector.calls, 1)
        self.assertTrue(all(not thread.is_alive() for thread in (owner, waiter)))
        self.assertIsNot(results["owner"], results["waiter"])
        self.assertIsNot(results["owner"], results["cache_hit"])
        self.assertIsNot(results["waiter"], results["cache_hit"])
        results["owner"]["detail"]["levels"].append(102.0)
        results["waiter"]["detail"]["levels"].append(103.0)
        results["cache_hit"]["detail"]["levels"].append(104.0)

        fresh = state._entry_structure_market_snapshot(bars, context)

        self.assertEqual(fresh["detail"]["levels"], [99.0, 101.0])
        self.assertEqual(
            state._entry_structure_market_cache[cache_key]["detail"]["levels"],
            [99.0, 101.0],
        )

    def test_uncopyable_cached_structure_returns_error_without_mutating_cache(self):
        bars = self.bars()
        latest = bars[-1]
        state = self.new_state(enable_observation_profile_promotion=False)
        self.addCleanup(state.close)
        context = state.capture_symbol_context()
        cache_key = (context[0], context[1], latest.close_time)
        cached = {
            "version": "ENTRY_STRUCTURE_SHADOW_V1",
            "mode": "SHADOW_ONLY",
            "status": "READY",
            "evaluated_at": latest.close_time,
            "states": [],
            "detail": self.Uncopyable(),
        }
        state._entry_structure_market_cache[cache_key] = cached

        returned = state._entry_structure_market_snapshot(bars, context)

        self.assertEqual(returned["status"], "ERROR")
        self.assertIn("CACHE_COPY_ERROR", returned["reason_code"])
        self.assertIs(state._entry_structure_market_cache[cache_key], cached)
        self.assertIsInstance(cached["detail"], self.Uncopyable)

    def test_candidate_error_fallback_replaces_invalid_structure_with_clean_error(self):
        bars = self.bars()
        latest = bars[-1]
        state = self.new_state(enable_observation_profile_promotion=False)
        self.addCleanup(state.close)
        state.klines = bars
        invalid_structure = {
            "entry_structure_evaluated_at": latest.close_time,
            "candidate_origin": "NATIVE_ACTIONABLE",
            "candidate_direction": "LONG",
            "detail": {1: "integer-key"},
        }
        signal = replace(
            self.signal(latest),
            candidate_origin="NATIVE_ACTIONABLE",
            entry_structure_shadow=invalid_structure,
        )
        run = state._new_decision_run(
            signal,
            latest,
            candidate_origin="NATIVE_ACTIONABLE",
            candidate_ordinal=0,
        )

        inputs = state._fallback_candidate_error_inputs(
            run,
            signal,
            latest,
            freeze_error=RuntimeError("freeze failed"),
        )

        self.assertEqual(
            inputs["entry_structure"],
            inputs["signal"]["entry_structure_shadow"],
        )
        self.assertIsNot(
            inputs["entry_structure"],
            inputs["signal"]["entry_structure_shadow"],
        )
        self.assertEqual(inputs["entry_structure"]["entry_structure_state"], "ERROR")
        self.assertEqual(
            inputs["signal"]["entry_structure_shadow"]["entry_structure_state"],
            "ERROR",
        )
        self.assertNotIn("detail", inputs["entry_structure"])
        self.assertNotIn("detail", inputs["signal"]["entry_structure_shadow"])


if __name__ == "__main__":
    unittest.main()
