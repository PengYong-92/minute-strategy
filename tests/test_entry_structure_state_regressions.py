from copy import deepcopy
import json
import math
import time
import unittest

from app.entry_structure_shadow import StructureConfig
from app.models import Kline
from tests.test_entry_structure_shadow import (
    candidate,
    detected_level,
    state_bars,
)


def at_last_bar(detected, bars):
    result = deepcopy(detected)
    result["evaluated_at"] = bars[-1].close_time
    return result


def evidence(kind, state, breakout_direction="NONE"):
    return {
        **detected_level(kind)["levels"][0],
        "state": state,
        "breakout_direction": breakout_direction,
        "breakout_closed_bars": 0,
        "breakout_buffer_atr": 0.10,
        "retest_status": "NOT_APPLICABLE",
    }


class StructureStateRegressionTest(unittest.TestCase):
    def test_config_validates_state_machine_thresholds(self):
        config = StructureConfig()
        self.assertEqual(
            (
                config.approach_atr,
                config.breakout_atr,
                config.breakout_confirm_bars,
                config.retest_window_bars,
                config.invalidation_atr,
                config.invalidation_bars,
            ),
            (0.35, 0.10, 2, 5, 0.35, 3),
        )
        for kwargs in (
            {"approach_atr": float("nan")},
            {"breakout_atr": 0.0},
            {"breakout_confirm_bars": True},
            {"retest_window_bars": 0},
            {"invalidation_atr": float("inf")},
            {"invalidation_bars": -1},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises((TypeError, ValueError)):
                    StructureConfig(**kwargs)

    def test_state_machine_uses_exact_causal_prefix(self):
        from app.entry_structure_shadow import StructureStateMachine

        machine = StructureStateMachine()
        prefix = state_bars([98.8, 98.7])
        future = state_bars(
            [98.8, 98.7, 100.5],
            lows=[98.5, 98.4, 99.8],
            highs=[99.0, 99.0, 101.0],
        )
        detected = at_last_bar(detected_level("SUPPORT"), prefix)

        prefix_result = machine.evaluate(detected, prefix)
        future_result = machine.evaluate(detected, future)

        self.assertEqual(prefix_result, future_result)
        self.assertEqual(prefix_result[0]["state"], "BREAKOUT_CONFIRMED")

    def test_state_machine_rejects_non_exact_time_and_future_level(self):
        from app.entry_structure_shadow import StructureStateMachine

        machine = StructureStateMachine()
        bars = state_bars([100.0, 100.0])
        between = detected_level("SUPPORT", evaluated_at=90_000)
        future_level = detected_level(
            "SUPPORT",
            evaluated_at=120_000,
            confirmed_at=180_000,
        )

        self.assertEqual(machine.evaluate(between, bars)[0]["state"], "ERROR")
        self.assertEqual(machine.evaluate(future_level, bars)[0]["state"], "ERROR")

    def test_approach_rejection_and_breakout_boundaries_are_inclusive(self):
        from app.entry_structure_shadow import StructureStateMachine

        machine = StructureStateMachine()
        cases = [
            (104.5, 104.5, 104.5, "APPROACHING_SUPPORT"),
            (104.5001, 104.5001, 104.5001, "NO_NEARBY_LEVEL"),
            (101.5, 100.5, 102.0, "SUPPORT_REJECTED"),
            (101.4999, 100.5, 102.0, "APPROACHING_SUPPORT"),
            (99.0, 98.8, 99.2, "BREAKOUT_PENDING"),
            (99.0001, 98.8, 99.2, "APPROACHING_SUPPORT"),
        ]
        for close, low, high, expected in cases:
            with self.subTest(close=close, expected=expected):
                bars = state_bars([close], lows=[low], highs=[high])
                result = machine.evaluate(
                    at_last_bar(detected_level("SUPPORT"), bars),
                    bars,
                )[0]
                self.assertEqual(result["state"], expected)

    def test_resistance_states_mirror_support_states(self):
        from app.entry_structure_shadow import StructureStateMachine

        machine = StructureStateMachine()
        cases = [
            ([96.5], [96.5], [96.5], "APPROACHING_RESISTANCE"),
            ([99.5], [99.0], [100.5], "RESISTANCE_REJECTED"),
            ([102.0], [101.8], [102.2], "BREAKOUT_PENDING"),
            ([102.0, 102.1], [101.8, 101.9], [102.2, 102.3], "BREAKOUT_CONFIRMED"),
            ([104.6, 104.7, 104.8], None, None, "LEVEL_INVALIDATED"),
        ]
        for closes, lows, highs, expected in cases:
            with self.subTest(expected=expected):
                bars = state_bars(closes, lows=lows, highs=highs)
                result = machine.evaluate(
                    at_last_bar(detected_level("RESISTANCE"), bars),
                    bars,
                )[0]
                self.assertEqual(result["state"], expected)

    def test_resistance_retest_and_false_breakout_mirror_support(self):
        from app.entry_structure_shadow import StructureStateMachine

        machine = StructureStateMachine()
        cases = [
            (
                [102.2, 102.3, 101.6],
                [102.0, 102.0, 100.8],
                [102.5, 102.6, 102.0],
                "RETEST_HELD",
                "HELD",
            ),
            (
                [102.2, 102.3, 100.5],
                [102.0, 102.0, 100.0],
                [102.5, 102.6, 101.2],
                "FALSE_BREAKOUT",
                "FAILED",
            ),
        ]
        for closes, lows, highs, expected, retest_status in cases:
            with self.subTest(expected=expected):
                bars = state_bars(closes, lows=lows, highs=highs)
                result = machine.evaluate(
                    at_last_bar(detected_level("RESISTANCE"), bars),
                    bars,
                )[0]
                self.assertEqual(result["state"], expected)
                self.assertEqual(result["retest_status"], retest_status)
                self.assertEqual(result["breakout_direction"], "UP")

    def test_retest_only_counts_through_fifth_bar(self):
        from app.entry_structure_shadow import StructureStateMachine

        machine = StructureStateMachine()
        base_closes = [98.8, 98.7, 98.6, 98.5, 98.4, 98.3]
        base_lows = [98.5, 98.4, 98.3, 98.2, 98.1, 98.0]
        base_highs = [99.0] * 6
        fifth = state_bars(
            [*base_closes, 99.4],
            lows=[*base_lows, 99.0],
            highs=[*base_highs, 100.5],
        )
        sixth = state_bars(
            [*base_closes, 98.2, 99.4],
            lows=[*base_lows, 98.0, 99.0],
            highs=[*base_highs, 99.0, 100.5],
        )

        fifth_result = machine.evaluate(
            at_last_bar(detected_level("SUPPORT"), fifth), fifth
        )[0]
        sixth_result = machine.evaluate(
            at_last_bar(detected_level("SUPPORT"), sixth), sixth
        )[0]

        self.assertEqual(fifth_result["state"], "RETEST_HELD")
        self.assertEqual(sixth_result["state"], "APPROACHING_SUPPORT")

    def test_held_can_fail_inside_window_but_expires_after_window(self):
        from app.entry_structure_shadow import StructureStateMachine

        reclaimed = state_bars(
            [98.8, 98.7, 99.4, 100.5],
            lows=[98.5, 98.4, 99.0, 99.8],
            highs=[99.0, 99.0, 100.5, 101.0],
        )
        expired = state_bars(
            [98.8, 98.7, 99.4, 98.6, 98.5, 98.4, 98.3, 104.5],
            lows=[98.5, 98.4, 99.0, 98.3, 98.2, 98.1, 98.0, 104.3],
            highs=[99.0, 99.0, 100.5, 99.0, 99.0, 99.0, 99.0, 104.7],
        )
        machine = StructureStateMachine()

        reclaimed_result = machine.evaluate(
            at_last_bar(detected_level("SUPPORT"), reclaimed), reclaimed
        )[0]
        expired_result = machine.evaluate(
            at_last_bar(detected_level("SUPPORT"), expired), expired
        )[0]

        self.assertEqual(reclaimed_result["state"], "FALSE_BREAKOUT")
        self.assertEqual(expired_result["state"], "APPROACHING_SUPPORT")

    def test_false_breakout_is_one_bar_event_and_current_bar_reclassifies(self):
        from app.entry_structure_shadow import StructureStateMachine

        cases = [
            (98.7, 98.4, 99.0, "BREAKOUT_PENDING"),
            (101.6, 100.5, 102.0, "SUPPORT_REJECTED"),
            (104.5, 104.3, 104.7, "APPROACHING_SUPPORT"),
            (110.0, 109.8, 110.2, "NO_NEARBY_LEVEL"),
        ]
        for close, low, high, expected in cases:
            with self.subTest(expected=expected):
                bars = state_bars(
                    [98.8, 100.5, close],
                    lows=[98.5, 99.8, low],
                    highs=[99.0, 101.0, high],
                )
                result = StructureStateMachine().evaluate(
                    at_last_bar(detected_level("SUPPORT"), bars), bars
                )[0]
                self.assertEqual(result["state"], expected)

    def test_confirmed_held_and_retest_pending_expire_then_reclassify(self):
        from app.entry_structure_shadow import StructureStateMachine

        histories = [
            (
                [98.8, 98.7, 98.6, 98.5, 98.4, 98.3, 98.2, 110.0],
                [98.5, 98.4, 98.3, 98.2, 98.1, 98.0, 97.9, 109.8],
                [99.0, 99.0, 99.0, 99.0, 99.0, 99.0, 99.0, 110.2],
            ),
            (
                [98.8, 98.7, 99.4, 98.6, 98.5, 98.4, 98.3, 110.0],
                [98.5, 98.4, 99.0, 98.3, 98.2, 98.1, 98.0, 109.8],
                [99.0, 99.0, 100.5, 99.0, 99.0, 99.0, 99.0, 110.2],
            ),
            (
                [98.8, 98.7, 99.8, 98.6, 98.5, 98.4, 98.3, 110.0],
                [98.5, 98.4, 99.0, 98.3, 98.2, 98.1, 98.0, 109.8],
                [99.0, 99.0, 100.5, 99.0, 99.0, 99.0, 99.0, 110.2],
            ),
        ]
        machine = StructureStateMachine()
        for closes, lows, highs in histories:
            with self.subTest(initial_event=closes[2]):
                bars = state_bars(closes, lows=lows, highs=highs)
                result = machine.evaluate(
                    at_last_bar(detected_level("SUPPORT"), bars), bars
                )[0]
                self.assertEqual(result["state"], "NO_NEARBY_LEVEL")

    def test_new_breakout_overrides_older_false_breakout(self):
        from app.entry_structure_shadow import StructureStateMachine

        bars = state_bars(
            [98.8, 100.5, 98.8, 98.7],
            lows=[98.5, 99.8, 98.5, 98.4],
            highs=[99.0, 101.0, 99.0, 99.0],
        )
        result = StructureStateMachine().evaluate(
            at_last_bar(detected_level("SUPPORT"), bars), bars
        )[0]

        self.assertEqual(result["state"], "BREAKOUT_CONFIRMED")
        self.assertEqual(result["breakout_closed_bars"], 2)

    def test_breakout_confirmation_requires_consecutive_buffered_closes(self):
        from app.entry_structure_shadow import StructureStateMachine

        bars = state_bars(
            [98.8, 99.5, 98.7],
            lows=[98.5, 99.2, 98.4],
            highs=[99.0, 99.8, 99.0],
        )
        result = StructureStateMachine().evaluate(
            at_last_bar(detected_level("SUPPORT"), bars), bars
        )[0]

        self.assertEqual(result["state"], "BREAKOUT_PENDING")
        self.assertEqual(result["breakout_closed_bars"], 1)

    def test_breakout_confirm_bars_one_confirms_first_breakout(self):
        from app.entry_structure_shadow import StructureStateMachine

        bars = state_bars([98.8], lows=[98.5], highs=[99.0])
        machine = StructureStateMachine(
            StructureConfig(breakout_confirm_bars=1)
        )
        result = machine.evaluate(
            at_last_bar(detected_level("SUPPORT"), bars), bars
        )[0]

        self.assertEqual(result["state"], "BREAKOUT_CONFIRMED")
        self.assertEqual(result["breakout_closed_bars"], 1)
        self.assertEqual(result["retest_status"], "AWAITING")

    def test_confirmed_gap_reclaim_is_false_breakout_without_intersection(self):
        from app.entry_structure_shadow import StructureStateMachine

        bars = state_bars(
            [98.8, 98.7, 102.0],
            lows=[98.5, 98.4, 101.5],
            highs=[99.0, 99.0, 102.2],
        )
        result = StructureStateMachine().evaluate(
            at_last_bar(detected_level("SUPPORT"), bars), bars
        )[0]

        self.assertEqual(result["state"], "FALSE_BREAKOUT")
        self.assertEqual(result["retest_status"], "FAILED")

    def test_complete_direction_mapping_and_inconsistent_evidence(self):
        from app.entry_structure_shadow import map_direction_bias

        cases = [
            ("APPROACHING_SUPPORT", "SUPPORT", "NONE", "NEUTRAL", "CONFLICT"),
            ("APPROACHING_RESISTANCE", "RESISTANCE", "NONE", "CONFLICT", "NEUTRAL"),
            ("SUPPORT_REJECTED", "SUPPORT", "NONE", "CONFIRMED", "CONFLICT"),
            ("RESISTANCE_REJECTED", "RESISTANCE", "NONE", "CONFLICT", "CONFIRMED"),
            ("BREAKOUT_PENDING", "RESISTANCE", "UP", "PENDING", "CONFLICT"),
            ("BREAKOUT_PENDING", "SUPPORT", "DOWN", "CONFLICT", "PENDING"),
            ("BREAKOUT_CONFIRMED", "RESISTANCE", "UP", "CONFIRMED", "CONFLICT"),
            ("BREAKOUT_CONFIRMED", "SUPPORT", "DOWN", "CONFLICT", "CONFIRMED"),
            ("RETEST_PENDING", "RESISTANCE", "UP", "PENDING", "CONFLICT"),
            ("RETEST_PENDING", "SUPPORT", "DOWN", "CONFLICT", "PENDING"),
            ("RETEST_HELD", "RESISTANCE", "UP", "CONFIRMED", "CONFLICT"),
            ("RETEST_HELD", "SUPPORT", "DOWN", "CONFLICT", "CONFIRMED"),
            ("FALSE_BREAKOUT", "RESISTANCE", "UP", "CONFLICT", "CONFIRMED"),
            ("FALSE_BREAKOUT", "SUPPORT", "DOWN", "CONFIRMED", "CONFLICT"),
            ("LEVEL_INVALIDATED", "SUPPORT", "DOWN", "NEUTRAL", "NEUTRAL"),
            ("NO_NEARBY_LEVEL", "SUPPORT", "NONE", "NEUTRAL", "NEUTRAL"),
            ("INSUFFICIENT_DATA", "SUPPORT", "NONE", "NEUTRAL", "NEUTRAL"),
            ("ERROR", "SUPPORT", "NONE", "NEUTRAL", "NEUTRAL"),
        ]
        for state, kind, breakout, long_bias, short_bias in cases:
            item = evidence(kind, state, breakout)
            with self.subTest(state=state, direction="LONG"):
                self.assertEqual(map_direction_bias("LONG", item)["bias"], long_bias)
            with self.subTest(state=state, direction="SHORT"):
                self.assertEqual(map_direction_bias("SHORT", item)["bias"], short_bias)

        bad = evidence("SUPPORT", "BREAKOUT_CONFIRMED", "UP")
        mapped = map_direction_bias("LONG", bad)
        self.assertEqual(mapped["bias"], "NEUTRAL")
        self.assertEqual(mapped["state"], "ERROR")
        self.assertEqual(mapped["reason_code"], "STRUCTURE_EVIDENCE_INCONSISTENT")

    def test_rank_uses_stable_id_as_final_tie_break(self):
        from app.entry_structure_shadow import EntryStructureGate

        tied = [
            {"id": "z", "bias": "CONFLICT", "distance_atr": 0.1, "touch_count": 2, "last_confirmed_at": 3},
            {"id": "a", "bias": "CONFLICT", "distance_atr": 0.1, "touch_count": 2, "last_confirmed_at": 3},
        ]
        self.assertEqual(EntryStructureGate().rank(tied)[0]["id"], "a")

    def test_attach_is_immutable_deterministic_and_json_finite(self):
        from app.entry_structure_shadow import EntryStructureGate

        support = evidence("SUPPORT", "SUPPORT_REJECTED")
        market = {
            **detected_level("SUPPORT"),
            "states": [support],
            "nearest_support": support,
            "nearest_resistance": None,
        }
        original_market = deepcopy(market)
        signal = candidate("LONG")

        first = EntryStructureGate().attach(signal, market, "RESEARCH_OBSERVATION")
        second = EntryStructureGate().attach(signal, market, "RESEARCH_OBSERVATION")

        self.assertEqual(first, second)
        self.assertEqual(market, original_market)
        self.assertEqual(signal.entry_structure_shadow, {})
        json.dumps(first, allow_nan=False, sort_keys=True)
        self.assertTrue(
            all(
                not isinstance(value, float) or math.isfinite(value)
                for value in first.values()
            )
        )

    def test_gate_handles_non_mapping_detector_results_and_attach_inputs(self):
        from app.entry_structure_shadow import EntryStructureGate, StructureDetector

        class MalformedDetector(StructureDetector):
            def __init__(self, result):
                super().__init__()
                self.result = result

            def detect(self, symbol, closed_klines):
                return self.result

        for malformed in (None, []):
            with self.subTest(malformed=malformed):
                gate = EntryStructureGate(detector=MalformedDetector(malformed))
                payload = gate.evaluate(candidate(), "BTCUSDT", state_bars([100.0]))
                self.assertEqual(
                    (payload["entry_structure_state"], payload["entry_structure_bias"]),
                    ("ERROR", "NEUTRAL"),
                )
                self.assertEqual(
                    payload["entry_structure_reason_code"],
                    "DETECTOR_RESULT_INVALID",
                )

        attached = EntryStructureGate().attach(
            candidate(), None, "NATIVE_ACTIONABLE"
        )
        self.assertEqual(
            (attached["entry_structure_state"], attached["entry_structure_bias"]),
            ("ERROR", "NEUTRAL"),
        )

    def test_error_details_never_leak_exception_message(self):
        from app.entry_structure_shadow import EntryStructureGate, StructureDetector

        class SecretDetector(StructureDetector):
            def detect(self, symbol, closed_klines):
                raise RuntimeError("token=abc123 /private/secret/path")

        payload = EntryStructureGate(detector=SecretDetector()).evaluate(
            candidate(), "BTCUSDT", state_bars([100.0])
        )
        serialized = json.dumps(payload, sort_keys=True)

        self.assertEqual(payload["error_detail"], "RuntimeError")
        self.assertEqual(
            payload["entry_structure_reason_code"],
            "DETECTOR_ERROR_RUNTIMEERROR",
        )
        self.assertNotIn("abc123", serialized)
        self.assertNotIn("/private/secret/path", serialized)

    def test_custom_breakout_buffer_is_used_by_all_error_paths(self):
        from app.entry_structure_shadow import (
            EntryStructureGate,
            StructureDetector,
            StructureStateMachine,
        )

        config = StructureConfig(breakout_atr=0.20)
        machine = StructureStateMachine(config)
        machine_error = machine.evaluate(None, [])

        class BrokenDetector(StructureDetector):
            def detect(self, symbol, closed_klines):
                raise RuntimeError("hidden")

        gate_error = EntryStructureGate(
            detector=BrokenDetector(config),
            state_machine=machine,
        ).evaluate(candidate(), "BTCUSDT", [])
        attach_error = EntryStructureGate(
            detector=StructureDetector(config),
            state_machine=machine,
        ).attach(candidate(), None, "NATIVE_ACTIONABLE")

        self.assertEqual(machine_error[0]["breakout_buffer_atr"], 0.20)
        self.assertEqual(gate_error["breakout_buffer_atr"], 0.20)
        self.assertEqual(attach_error["breakout_buffer_atr"], 0.20)

    def test_malformed_evidence_degrades_to_error_neutral(self):
        from app.entry_structure_shadow import EntryStructureGate, map_direction_bias

        for mutation in (
            {"distance_atr": float("nan")},
            {"distance_atr": float("inf")},
            {"distance_price": float("nan")},
            {"distance_bps": float("inf")},
            {"touch_count": -1},
            {"lower": None},
            {"lower": 10**400},
            {"distance_atr": 10**400},
        ):
            item = {**evidence("SUPPORT", "SUPPORT_REJECTED"), **mutation}
            with self.subTest(mutation=mutation):
                mapped = map_direction_bias("LONG", item)
                self.assertEqual((mapped["state"], mapped["bias"]), ("ERROR", "NEUTRAL"))

        malformed = {
            **detected_level("SUPPORT"),
            "states": [{**evidence("SUPPORT", "SUPPORT_REJECTED"), "distance_atr": float("nan")}],
        }
        payload = EntryStructureGate().attach(candidate(), malformed, "NATIVE_ACTIONABLE")
        self.assertEqual(
            (payload["entry_structure_state"], payload["entry_structure_bias"]),
            ("ERROR", "NEUTRAL"),
        )
        json.dumps(payload, allow_nan=False)

    def test_reverse_causal_scan_handles_large_cache_without_changing_result(self):
        from app.entry_structure_shadow import StructureStateMachine

        class CountingBars:
            def __init__(self, values):
                self.values = values
                self.reads = 0

            def __len__(self):
                return len(self.values)

            def __getitem__(self, index):
                self.reads += 1
                return self.values[index]

        count = 140_000
        bars = [
            Kline(
                open_time=index * 60_000,
                open=100.0,
                high=100.2,
                low=99.8,
                close=100.0,
                volume=1.0,
                close_time=(index + 1) * 60_000,
            )
            for index in range(count)
        ]
        evaluated_index = count - 11
        evaluated_at = bars[evaluated_index].close_time
        detected = detected_level("SUPPORT", evaluated_at=evaluated_at)
        machine = StructureStateMachine()
        cached = CountingBars(bars)

        started = time.perf_counter()
        large_result = machine.evaluate(detected, cached)
        elapsed = time.perf_counter() - started
        scoped_result = machine.evaluate(
            detected,
            bars[evaluated_index - 239 : evaluated_index + 1],
        )

        self.assertEqual(large_result, scoped_result)
        self.assertLess(cached.reads, 1_000)
        self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()
