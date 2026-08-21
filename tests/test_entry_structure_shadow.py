import unittest

from app.models import Kline, Signal


def bars_from_ranges(ranges):
    result = []
    previous_close = float(ranges[0][2])
    for index, (low, high, close) in enumerate(ranges):
        result.append(
            Kline(
                open_time=index * 60_000,
                open=min(max(previous_close, float(low)), float(high)),
                high=float(high),
                low=float(low),
                close=float(close),
                volume=100.0,
                close_time=(index + 1) * 60_000,
            )
        )
        previous_close = float(close)
    return result


def pivot_fixture():
    ranges = [(125.0, 127.0, 126.0) for _ in range(36)]
    ranges[5] = (122.0, 126.0, 125.0)
    ranges[6] = (120.0, 125.0, 124.0)
    ranges[7] = (122.0, 126.0, 125.0)
    ranges[19] = (122.1, 126.0, 125.0)
    ranges[20] = (120.1, 125.0, 124.0)
    ranges[21] = (122.1, 126.0, 125.0)
    return bars_from_ranges(ranges)


def mixed_pivot_fixture():
    ranges = [(99.0, 101.0, 100.0) for _ in range(36)]
    for index, low in ((5, 95.0), (13, 95.1)):
        ranges[index] = (low, 100.0, 99.0)
    for index, high in ((21, 105.0), (29, 104.9)):
        ranges[index] = (100.0, high, 101.0)
    return bars_from_ranges(ranges)


class EntryStructureDetectorTest(unittest.TestCase):
    def test_pivot_is_causal_and_cluster_requires_independent_confirmations(self):
        from app.entry_structure_shadow import StructureDetector

        detector = StructureDetector()
        fixture = pivot_fixture()
        before_confirmation = detector.detect("BTCUSDT", fixture[:23])
        after_confirmation = detector.detect("BTCUSDT", fixture[:24])

        before_indexes = {
            index
            for level in before_confirmation["support"]
            for index in level["pivot_indexes"]
        }
        after_indexes = {
            index
            for level in after_confirmation["support"]
            for index in level["pivot_indexes"]
        }
        self.assertNotIn(20, before_indexes)
        self.assertIn(20, after_indexes)
        self.assertEqual(after_confirmation["support"][0]["pivot_count"], 2)
        self.assertGreaterEqual(after_confirmation["support"][0]["pivot_gap"], 5)

    def test_support_and_resistance_pivots_never_mix(self):
        from app.entry_structure_shadow import StructureDetector

        snapshot = StructureDetector().detect("BTCUSDT", mixed_pivot_fixture())

        self.assertTrue(snapshot["support"])
        self.assertTrue(snapshot["resistance"])
        self.assertTrue(
            all(level["kind"] == "SUPPORT" for level in snapshot["support"])
        )
        self.assertTrue(
            all(level["kind"] == "RESISTANCE" for level in snapshot["resistance"])
        )
        self.assertTrue(
            all(
                pivot["kind"] == "LOW"
                for level in snapshot["support"]
                for pivot in level["pivots"]
            )
        )
        self.assertTrue(
            all(
                pivot["kind"] == "HIGH"
                for level in snapshot["resistance"]
                for pivot in level["pivots"]
            )
        )

    def test_cluster_width_is_at_most_quarter_atr_and_touches_do_not_add_pivots(self):
        from app.entry_structure_shadow import StructureDetector

        ranges = [(99.0, 101.0, 100.0) for _ in range(32)]
        ranges[5] = (95.0, 100.0, 99.0)
        ranges[13] = (95.2, 100.0, 99.0)
        ranges[21] = (95.1, 100.0, 99.0)
        ranges[25] = (95.0, 100.0, 99.4)
        ranges[26] = (95.05, 100.0, 99.5)  # ordinary touch, not a pivot low
        snapshot = StructureDetector().detect("BTCUSDT", bars_from_ranges(ranges))
        support = snapshot["support"][0]

        self.assertLessEqual(
            support["upper"] - support["lower"],
            0.25 * snapshot["atr"] + 1e-12,
        )
        self.assertEqual(support["pivot_count"], 3)
        self.assertGreater(support["touch_count"], support["pivot_count"])

    def test_round_level_rules_and_deterministic_rebuild(self):
        from app.entry_structure_shadow import StructureDetector, round_steps

        self.assertEqual(round_steps("BTCUSDT"), (100.0, 500.0, 1000.0))
        self.assertEqual(round_steps("ETHUSDT"), (10.0, 50.0, 100.0))

        ranges = [(64_920.0, 65_080.0, 65_000.0) for _ in range(240)]
        fixture = bars_from_ranges(ranges)
        detector = StructureDetector()
        first = detector.detect("BTCUSDT", fixture)
        second = detector.detect("BTCUSDT", fixture)

        self.assertEqual(first, second)
        self.assertNotIn(65_000.0, {item["lower"] for item in first["levels"]})

    def test_round_level_requires_merged_zone_or_two_independent_rejections(self):
        from app.entry_structure_shadow import StructureDetector

        ranges = [(65_020.0, 65_080.0, 65_040.0) for _ in range(40)]
        ranges[5] = (64_980.0, 65_030.0, 65_020.0)
        ranges[13] = (64_980.0, 65_030.0, 65_020.0)
        snapshot = StructureDetector().detect("BTCUSDT", bars_from_ranges(ranges))
        round_levels = [
            item
            for item in snapshot["levels"]
            if item.get("round_level_price") == 65_000.0
        ]

        self.assertTrue(round_levels)
        self.assertTrue(all(item["source"] == "ROUND" for item in round_levels))
        self.assertTrue(all(item["lower"] == item["upper"] for item in round_levels))
        self.assertGreaterEqual(round_levels[0]["touch_count"], 2)

    def test_round_level_merges_with_real_zone_without_own_rejections(self):
        from app.entry_structure_shadow import StructureDetector

        ranges = [(65_020.0, 65_080.0, 65_040.0) for _ in range(32)]
        ranges[5] = (64_999.0, 65_050.0, 65_000.0)
        ranges[13] = (65_000.0, 65_050.0, 65_000.0)

        snapshot = StructureDetector().detect("BTCUSDT", bars_from_ranges(ranges))
        merged = [
            item
            for item in snapshot["support"]
            if item["source"] == "MERGED"
            and item["round_level_price"] == 65_000.0
        ]

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["pivot_count"], 2)

    def test_detector_uses_only_last_240_closed_bars(self):
        from app.entry_structure_shadow import StructureDetector

        old = pivot_fixture()
        recent = bars_from_ranges([(199.0, 201.0, 200.0) for _ in range(240)])
        combined = [
            Kline(
                open_time=index * 60_000,
                open=item.open,
                high=item.high,
                low=item.low,
                close=item.close,
                volume=item.volume,
                close_time=(index + 1) * 60_000,
            )
            for index, item in enumerate([*old, *recent])
        ]

        snapshot = StructureDetector().detect("BTCUSDT", combined)

        self.assertEqual(snapshot["bars"], 240)
        self.assertFalse(
            any(level["lower"] < 150.0 for level in snapshot["levels"])
        )


def state_bars(closes, *, lows=None, highs=None):
    lows = lows or [close - 0.2 for close in closes]
    highs = highs or [close + 0.2 for close in closes]
    return bars_from_ranges(
        [(low, high, close) for low, high, close in zip(lows, highs, closes)]
    )


def detected_level(
    kind="SUPPORT",
    *,
    distance_atr=0.0,
    touches=3,
    confirmed_at=10,
    evaluated_at=60_000,
):
    level = {
        "id": f"{kind.lower()}-level",
        "kind": kind,
        "source": "PIVOT",
        "lower": 100.0,
        "upper": 101.0,
        "pivot_count": 2,
        "touch_count": touches,
        "last_confirmed_at": confirmed_at,
        "distance_price": distance_atr * 10.0,
        "distance_bps": distance_atr * 100.0,
        "distance_atr": distance_atr,
        "round_level_price": None,
        "round_level_step": None,
    }
    return {
        "version": "ENTRY_STRUCTURE_SHADOW_V1",
        "mode": "SHADOW_ONLY",
        "status": "READY",
        "evaluated_at": evaluated_at,
        "bars": 40,
        "atr": 10.0,
        "levels": [level],
        "support": [level] if kind == "SUPPORT" else [],
        "resistance": [level] if kind == "RESISTANCE" else [],
        "nearest_support": level if kind == "SUPPORT" else None,
        "nearest_resistance": level if kind == "RESISTANCE" else None,
    }


def candidate(direction="LONG"):
    return Signal(
        direction=direction,
        timeframe_minutes=10,
        level="A",
        reason="test",
        price=100.0,
        open_time=0,
    )


class EntryStructureStateTest(unittest.TestCase):
    def test_structure_state_transition_matrix(self):
        from app.entry_structure_shadow import StructureStateMachine

        machine = StructureStateMachine()
        cases = [
            (
                detected_level("SUPPORT", distance_atr=0.35),
                state_bars([104.5]),
                "APPROACHING_SUPPORT",
            ),
            (
                detected_level("SUPPORT"),
                state_bars([101.6], lows=[100.5], highs=[102.0]),
                "SUPPORT_REJECTED",
            ),
            (
                detected_level("SUPPORT"),
                state_bars([98.8]),
                "BREAKOUT_PENDING",
            ),
            (
                detected_level("SUPPORT", evaluated_at=120_000),
                state_bars([98.8, 98.7]),
                "BREAKOUT_CONFIRMED",
            ),
            (
                detected_level("SUPPORT", evaluated_at=180_000),
                state_bars(
                    [98.8, 98.7, 99.4],
                    lows=[98.5, 98.4, 99.0],
                    highs=[99.0, 99.0, 100.5],
                ),
                "RETEST_HELD",
            ),
            (
                detected_level("SUPPORT", evaluated_at=120_000),
                state_bars([98.8, 100.5], lows=[98.5, 99.8], highs=[99.0, 101.0]),
                "FALSE_BREAKOUT",
            ),
            (
                detected_level("SUPPORT", evaluated_at=180_000),
                state_bars([96.4, 96.3, 96.2]),
                "LEVEL_INVALIDATED",
            ),
        ]
        for detected, bars, expected in cases:
            with self.subTest(expected=expected):
                result = machine.evaluate(detected, bars)
                self.assertEqual(result[0]["state"], expected)

    def test_resistance_rejection_and_retest_pending_are_mirrored(self):
        from app.entry_structure_shadow import StructureStateMachine

        machine = StructureStateMachine()
        rejected = machine.evaluate(
            detected_level("RESISTANCE"),
            state_bars([99.4], lows=[99.0], highs=[100.5]),
        )[0]
        retest = machine.evaluate(
            detected_level("RESISTANCE", evaluated_at=180_000),
            state_bars(
                [102.2, 102.3, 101.3],
                lows=[102.0, 102.0, 100.8],
                highs=[102.5, 102.6, 101.8],
            ),
        )[0]

        self.assertEqual(rejected["state"], "RESISTANCE_REJECTED")
        self.assertEqual(retest["state"], "RETEST_PENDING")

    def test_direction_mapping_matches_documented_table(self):
        from app.entry_structure_shadow import map_direction_bias

        cases = [
            ("LONG", "APPROACHING_RESISTANCE", "RESISTANCE", "NONE", "CONFLICT"),
            ("SHORT", "APPROACHING_RESISTANCE", "RESISTANCE", "NONE", "NEUTRAL"),
            ("LONG", "SUPPORT_REJECTED", "SUPPORT", "NONE", "CONFIRMED"),
            ("SHORT", "SUPPORT_REJECTED", "SUPPORT", "NONE", "CONFLICT"),
            ("LONG", "BREAKOUT_PENDING", "RESISTANCE", "UP", "PENDING"),
            ("SHORT", "BREAKOUT_PENDING", "RESISTANCE", "UP", "CONFLICT"),
            ("LONG", "BREAKOUT_CONFIRMED", "RESISTANCE", "UP", "CONFIRMED"),
            ("SHORT", "BREAKOUT_CONFIRMED", "RESISTANCE", "UP", "CONFLICT"),
            ("LONG", "FALSE_BREAKOUT", "RESISTANCE", "UP", "CONFLICT"),
            ("SHORT", "FALSE_BREAKOUT", "RESISTANCE", "UP", "CONFIRMED"),
            ("LONG", "LEVEL_INVALIDATED", "SUPPORT", "DOWN", "NEUTRAL"),
        ]
        for direction, state, kind, breakout_direction, expected in cases:
            with self.subTest(direction=direction, state=state):
                evidence = {
                    **detected_level(kind)["levels"][0],
                    "state": state,
                    "breakout_direction": breakout_direction,
                }
                self.assertEqual(
                    map_direction_bias(direction, evidence)["bias"],
                    expected,
                )

        inconsistent = {
            **detected_level("RESISTANCE")["levels"][0],
            "state": "SUPPORT_REJECTED",
            "breakout_direction": "NONE",
        }
        mapped = map_direction_bias("LONG", inconsistent)
        self.assertEqual(mapped["bias"], "NEUTRAL")
        self.assertEqual(mapped["reason_code"], "STRUCTURE_EVIDENCE_INCONSISTENT")

    def test_conservative_priority_and_tie_breaks(self):
        from app.entry_structure_shadow import EntryStructureGate

        gate = EntryStructureGate()
        evidence = [
            {"id": "neutral", "bias": "NEUTRAL", "distance_atr": 0.01, "touch_count": 9, "last_confirmed_at": 99},
            {"id": "confirmed", "bias": "CONFIRMED", "distance_atr": 0.01, "touch_count": 9, "last_confirmed_at": 99},
            {"id": "pending", "bias": "PENDING", "distance_atr": 0.01, "touch_count": 9, "last_confirmed_at": 99},
            {"id": "conflict", "bias": "CONFLICT", "distance_atr": 0.50, "touch_count": 1, "last_confirmed_at": 1},
        ]
        equal_bias = [
            {"id": "far", "bias": "CONFLICT", "distance_atr": 0.2, "touch_count": 9, "last_confirmed_at": 99},
            {"id": "near-fewer", "bias": "CONFLICT", "distance_atr": 0.1, "touch_count": 2, "last_confirmed_at": 99},
            {"id": "near-more-older", "bias": "CONFLICT", "distance_atr": 0.1, "touch_count": 3, "last_confirmed_at": 50},
            {"id": "nearest-more-touches-newer", "bias": "CONFLICT", "distance_atr": 0.1, "touch_count": 3, "last_confirmed_at": 100},
        ]

        self.assertEqual(
            [item["bias"] for item in gate.rank(evidence)],
            ["CONFLICT", "PENDING", "CONFIRMED", "NEUTRAL"],
        )
        self.assertEqual(
            gate.rank(equal_bias)[0]["id"],
            "nearest-more-touches-newer",
        )

    def test_gate_payload_is_shadow_only_and_keeps_both_nearest_levels(self):
        from app.entry_structure_shadow import EntryStructureGate

        support = {
            **detected_level("SUPPORT", distance_atr=0.2)["levels"][0],
            "state": "SUPPORT_REJECTED",
            "breakout_direction": "NONE",
            "breakout_closed_bars": 0,
            "retest_status": "NOT_APPLICABLE",
        }
        resistance = {
            **detected_level("RESISTANCE", distance_atr=0.1)["levels"][0],
            "state": "APPROACHING_RESISTANCE",
            "breakout_direction": "NONE",
            "breakout_closed_bars": 0,
            "retest_status": "NOT_APPLICABLE",
        }
        market = {
            **detected_level("SUPPORT"),
            "states": [support, resistance],
            "nearest_support": support,
            "nearest_resistance": resistance,
        }

        payload = EntryStructureGate().attach(
            candidate("LONG"),
            market,
            "NATIVE_ACTIONABLE",
        )

        self.assertEqual(payload["entry_structure_mode"], "SHADOW_ONLY")
        self.assertEqual(payload["entry_structure_bias"], "CONFLICT")
        self.assertEqual(payload["active_level_upper"], 101.0)
        self.assertEqual(payload["active_level_confirmed_at"], 10)
        self.assertEqual(payload["nearest_support_lower"], 100.0)
        self.assertEqual(payload["nearest_resistance_upper"], 101.0)
        self.assertEqual(payload["support_distance_atr"], 0.2)
        self.assertEqual(payload["resistance_distance_atr"], 0.1)
        self.assertIsNone(payload["round_level_price"])
        self.assertIsNone(payload["round_level_step"])
        self.assertEqual(payload["candidate_origin"], "NATIVE_ACTIONABLE")

    def test_insufficient_data_and_detector_exception_are_safe_neutral(self):
        from app.entry_structure_shadow import EntryStructureGate, StructureDetector

        insufficient = EntryStructureGate().evaluate(
            candidate(),
            "BTCUSDT",
            state_bars([100.0] * 5),
        )

        class BrokenDetector(StructureDetector):
            def detect(self, symbol, closed_klines):
                raise RuntimeError("broken detector")

        failed = EntryStructureGate(detector=BrokenDetector()).evaluate(
            candidate(),
            "BTCUSDT",
            state_bars([100.0] * 30),
        )

        self.assertEqual(
            (insufficient["entry_structure_mode"], insufficient["entry_structure_state"], insufficient["entry_structure_bias"]),
            ("SHADOW_ONLY", "INSUFFICIENT_DATA", "NEUTRAL"),
        )
        self.assertEqual(
            (failed["entry_structure_mode"], failed["entry_structure_state"], failed["entry_structure_bias"]),
            ("SHADOW_ONLY", "ERROR", "NEUTRAL"),
        )
        self.assertEqual(
            failed["entry_structure_reason_code"],
            "DETECTOR_ERROR_RUNTIMEERROR",
        )
        self.assertEqual(failed["error_detail"], "RuntimeError")
        self.assertNotIn("broken detector", str(failed))


if __name__ == "__main__":
    unittest.main()
