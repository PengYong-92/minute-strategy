from copy import deepcopy
import itertools
import math
import random
import time
import unittest
from unittest.mock import patch

from app.entry_structure_shadow import (
    StructureConfig,
    StructureDetector,
    _cluster_pivots,
    _distance,
    _merge_round_levels,
    _round_candidates,
)
from app.models import Kline


def make_bars(count=24, *, price=100.0):
    return [
        Kline(
            open_time=index * 60_000,
            open=price,
            high=price + 1.0,
            low=price - 1.0,
            close=price,
            volume=100.0,
            close_time=(index + 1) * 60_000,
        )
        for index in range(count)
    ]


def pivot(index, price, *, kind="LOW"):
    return {
        "kind": kind,
        "index": index,
        "price": price,
        "confirmed_at": (index + 4) * 60_000,
    }


def pivot_level(
    level_id,
    lower,
    upper,
    *,
    kind="SUPPORT",
    pivot_count=2,
    confirmed_at=100,
):
    return {
        "id": level_id,
        "kind": kind,
        "source": "PIVOT",
        "lower": lower,
        "upper": upper,
        "pivot_count": pivot_count,
        "pivot_gap": 5,
        "pivot_indexes": (1, 6),
        "pivots": (),
        "touch_count": pivot_count,
        "touch_indexes": (),
        "first_confirmed_at": 1,
        "last_confirmed_at": confirmed_at,
        "round_level_price": None,
        "round_level_step": None,
    }


def round_level(
    level_id,
    price,
    *,
    kind="SUPPORT",
    independently_qualified=False,
):
    return {
        "id": level_id,
        "kind": kind,
        "source": "ROUND",
        "lower": price,
        "upper": price,
        "pivot_count": 0,
        "pivot_gap": 0,
        "pivot_indexes": (),
        "pivots": (),
        "touch_count": 2 if independently_qualified else 0,
        "touch_indexes": (),
        "first_confirmed_at": 1 if independently_qualified else 0,
        "last_confirmed_at": 2 if independently_qualified else 0,
        "round_level_price": price,
        "round_level_step": 100.0,
        "_independently_qualified": independently_qualified,
        "_emit_independently": independently_qualified,
    }


def replace_bar(bar, **overrides):
    values = {
        "open_time": bar.open_time,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "close_time": bar.close_time,
    }
    values.update(overrides)
    return Kline(**values)


def assert_finite_numbers(test_case, value, path="root"):
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, (int, float)):
        test_case.assertTrue(math.isfinite(value), f"non-finite value at {path}")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            assert_finite_numbers(test_case, item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            assert_finite_numbers(test_case, item, f"{path}[{index}]")


class StructureConfigValidationTest(unittest.TestCase):
    def test_accepts_exact_minimum_viable_boundaries(self):
        config = StructureConfig(
            bars=6,
            atr_period=1,
            pivot_left=1,
            pivot_right=1,
            cluster_atr=0.01,
            min_pivots=2,
            min_pivot_gap=3,
            minimum_bars=6,
            rejection_atr=0.01,
        )

        self.assertEqual(config.minimum_bars, 6)

    def test_rejects_non_integer_boolean_and_non_positive_integer_fields(self):
        integer_fields = (
            "bars",
            "atr_period",
            "pivot_left",
            "pivot_right",
            "min_pivots",
            "min_pivot_gap",
            "minimum_bars",
        )
        for field in integer_fields:
            for value in (True, 1.5, 0, -1):
                with self.subTest(field=field, value=value):
                    with self.assertRaises((TypeError, ValueError)):
                        StructureConfig(**{field: value})

    def test_rejects_invalid_positive_float_fields(self):
        for field in ("cluster_atr", "rejection_atr"):
            for value in (True, "0.25", 0.0, -0.1, math.nan, math.inf):
                with self.subTest(field=field, value=value):
                    with self.assertRaises((TypeError, ValueError)):
                        StructureConfig(**{field: value})

    def test_rejects_impossible_history_combinations(self):
        invalid = (
            {"bars": 19, "minimum_bars": 20},
            {"atr_period": 20, "minimum_bars": 20},
            {
                "pivot_left": 3,
                "pivot_right": 3,
                "min_pivots": 3,
                "min_pivot_gap": 7,
                "minimum_bars": 20,
            },
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    StructureConfig(**overrides)

    def test_rejects_history_windows_above_causal_240_bar_limit(self):
        with self.assertRaises(ValueError):
            StructureConfig(bars=241)


class StructureInputSafetyTest(unittest.TestCase):
    def assert_safe_insufficient(self, snapshot, expected_bars):
        self.assertEqual(snapshot["status"], "INSUFFICIENT_DATA")
        self.assertEqual(snapshot["bars"], expected_bars)
        self.assertTrue(math.isfinite(snapshot["atr"]))
        self.assertIsInstance(snapshot["evaluated_at"], int)
        self.assertEqual(snapshot["levels"], [])
        self.assertEqual(snapshot["support"], [])
        self.assertEqual(snapshot["resistance"], [])
        self.assertIsNone(snapshot["nearest_support"])
        self.assertIsNone(snapshot["nearest_resistance"])

    def test_non_finite_scoped_ohlc_and_close_time_are_safe(self):
        fields = ("open", "high", "low", "close", "close_time")
        for field, value in itertools.product(fields, (math.nan, math.inf, -math.inf)):
            bars = make_bars()
            source = bars[-1]
            values = {
                "open_time": source.open_time,
                "open": source.open,
                "high": source.high,
                "low": source.low,
                "close": source.close,
                "volume": source.volume,
                "close_time": source.close_time,
            }
            values[field] = value
            bars[-1] = Kline(**values)

            with self.subTest(field=field, value=value):
                self.assert_safe_insufficient(
                    StructureDetector().detect("BTCUSDT", bars),
                    len(bars),
                )

    def test_non_finite_atr_is_safe(self):
        with patch("app.entry_structure_shadow._atr", return_value=math.inf):
            snapshot = StructureDetector().detect("BTCUSDT", make_bars())

        self.assert_safe_insufficient(snapshot, 24)

    def test_non_kline_and_inverted_range_are_safe(self):
        malformed = make_bars()
        malformed[-1] = object()
        self.assert_safe_insufficient(
            StructureDetector().detect("BTCUSDT", malformed),
            24,
        )

        inverted = make_bars()
        source = inverted[-1]
        inverted[-1] = Kline(
            open_time=source.open_time,
            open=source.open,
            high=99.0,
            low=101.0,
            close=source.close,
            volume=source.volume,
            close_time=source.close_time,
        )
        self.assert_safe_insufficient(
            StructureDetector().detect("BTCUSDT", inverted),
            24,
        )

    def test_open_and_close_must_stay_inside_high_low_range(self):
        for overrides in (
            {"open": 102.0},
            {"open": 98.0},
            {"close": 102.0},
            {"close": 98.0},
        ):
            bars = make_bars()
            bars[10] = replace_bar(bars[10], **overrides)

            with self.subTest(overrides=overrides):
                self.assert_safe_insufficient(
                    StructureDetector().detect("BTCUSDT", bars),
                    len(bars),
                )

    def test_time_axis_must_be_integral_ordered_and_causal(self):
        cases = []
        unordered_close = make_bars()
        unordered_close[10] = replace_bar(
            unordered_close[10],
            close_time=unordered_close[9].close_time,
        )
        cases.append(unordered_close)

        unordered_open = make_bars()
        unordered_open[10] = replace_bar(
            unordered_open[10],
            open_time=unordered_open[9].open_time,
        )
        cases.append(unordered_open)

        open_after_close = make_bars()
        open_after_close[10] = replace_bar(
            open_after_close[10],
            open_time=open_after_close[10].close_time,
        )
        cases.append(open_after_close)

        fractional_time = make_bars()
        fractional_time[10] = replace_bar(
            fractional_time[10],
            close_time=fractional_time[10].close_time + 0.5,
        )
        cases.append(fractional_time)

        overlapping = make_bars()
        overlapping[10] = replace_bar(
            overlapping[10],
            open_time=overlapping[9].close_time - 1,
        )
        cases.append(overlapping)

        for bars in cases:
            with self.subTest(bad_bar=bars[10]):
                self.assert_safe_insufficient(
                    StructureDetector().detect("BTCUSDT", bars),
                    len(bars),
                )

    def test_ready_snapshot_contains_only_finite_numbers_for_tiny_close(self):
        bars = make_bars(price=2.0)
        bars[5] = replace_bar(bars[5], low=0.5)
        bars[10] = replace_bar(bars[10], low=0.5)
        bars[-1] = replace_bar(
            bars[-1],
            open=1e-320,
            high=2e-320,
            low=1e-320,
            close=1e-320,
        )
        config = StructureConfig(
            bars=24,
            atr_period=1,
            pivot_left=1,
            pivot_right=1,
            min_pivots=2,
            min_pivot_gap=5,
            minimum_bars=8,
        )

        snapshot = StructureDetector(config).detect("BTCUSDT", bars)

        self.assertEqual(snapshot["status"], "READY")
        assert_finite_numbers(self, snapshot)
        assert_finite_numbers(
            self,
            _distance(1e-320, {"lower": 1.0, "upper": 1.0}, 1e-320),
        )


class StructureClusteringRegressionTest(unittest.TestCase):
    def test_time_ineligible_pivot_does_not_poison_later_price_window(self):
        config = StructureConfig(
            bars=20,
            atr_period=1,
            pivot_left=1,
            pivot_right=1,
            min_pivots=2,
            min_pivot_gap=5,
            minimum_bars=8,
        )
        pivots = [pivot(5, 100.0), pivot(9, 100.2), pivot(14, 100.3)]

        levels = _cluster_pivots(make_bars(20), pivots, 1.0, config)

        self.assertTrue(
            any(level["pivot_indexes"] == (9, 14) for level in levels),
            levels,
        )

    def test_small_random_windows_match_brute_force_existence(self):
        config = StructureConfig(
            bars=40,
            atr_period=1,
            pivot_left=1,
            pivot_right=1,
            min_pivots=2,
            min_pivot_gap=5,
            minimum_bars=8,
        )
        rng = random.Random(731_245)
        for case in range(40):
            pivots = [
                pivot(
                    index,
                    100.0 + rng.randrange(0, 9) * 0.1,
                )
                for index in sorted(rng.sample(range(2, 38), 7))
            ]
            valid_subsets = [
                subset
                for size in range(config.min_pivots, len(pivots) + 1)
                for subset in itertools.combinations(pivots, size)
                if max(item["price"] for item in subset)
                - min(item["price"] for item in subset)
                <= config.cluster_atr + 1e-12
                and all(
                    right["index"] - left["index"] >= config.min_pivot_gap
                    for left, right in zip(
                        sorted(subset, key=lambda item: item["index"]),
                        sorted(subset, key=lambda item: item["index"])[1:],
                    )
                )
            ]

            levels = _cluster_pivots(make_bars(40), pivots, 1.0, config)

            with self.subTest(case=case, pivots=pivots):
                self.assertEqual(bool(levels), bool(valid_subsets))
                for level in levels:
                    self.assertLessEqual(
                        level["upper"] - level["lower"],
                        config.cluster_atr + 1e-12,
                    )
                    self.assertTrue(
                        all(
                            right - left >= config.min_pivot_gap
                            for left, right in zip(
                                level["pivot_indexes"],
                                level["pivot_indexes"][1:],
                            )
                        )
                    )

    def test_pivot_level_id_is_stable_when_window_rolls_one_bar(self):
        bars = make_bars(241, price=100.0)
        bars[100] = replace_bar(bars[100], low=95.0)
        bars[110] = replace_bar(bars[110], low=95.0)
        detector = StructureDetector()

        before = detector.detect("BTCUSDT", bars[:240])
        after = detector.detect("BTCUSDT", bars[1:])
        before_level = next(
            item
            for item in before["support"]
            if item["pivot_count"] >= 2 and item["lower"] == 95.0
        )
        after_level = next(
            item
            for item in after["support"]
            if item["pivot_count"] >= 2 and item["lower"] == 95.0
        )

        self.assertNotEqual(before_level["pivot_indexes"], after_level["pivot_indexes"])
        self.assertEqual(before_level["id"], after_level["id"])

    def test_more_than_twenty_four_zones_keep_nearest_current_price_zone(self):
        config = StructureConfig(
            bars=240,
            atr_period=1,
            pivot_left=1,
            pivot_right=1,
            min_pivots=2,
            min_pivot_gap=2,
            minimum_bars=8,
        )
        bars = make_bars(240, price=100.0)
        pivots = []
        for zone in range(25):
            price = 100.0 + zone * 10.0
            first_index = 2 + zone * 8
            pivots.extend(
                (
                    pivot(first_index, price),
                    pivot(first_index + 2, price),
                )
            )

        levels = _cluster_pivots(bars, pivots, 1.0, config)

        self.assertGreater(len(levels), 24)
        self.assertTrue(
            any(level["lower"] == 100.0 for level in levels),
            levels,
        )

    def test_high_price_pivot_ids_are_distinct_and_stable_when_indexes_roll(self):
        config = StructureConfig(
            bars=240,
            atr_period=1,
            pivot_left=1,
            pivot_right=1,
            min_pivots=2,
            min_pivot_gap=5,
            minimum_bars=8,
        )
        prices = (1e17, 1e17 + 100.0)

        def clustered(index_offset):
            pivots = [
                {
                    "kind": "LOW",
                    "index": index + index_offset,
                    "price": price,
                    "confirmed_at": confirmed_at,
                }
                for price, indexes in zip(
                    prices,
                    ((50, 60), (70, 80)),
                )
                for index, confirmed_at in zip(indexes, (60_000, 70_000))
            ]
            return _cluster_pivots(
                make_bars(240, price=1e17),
                pivots,
                100.0,
                config,
            )

        before = clustered(0)
        after = clustered(-1)
        before_ids = {level["lower"]: level["id"] for level in before}
        after_ids = {level["lower"]: level["id"] for level in after}

        self.assertEqual(len(before_ids), 2)
        self.assertEqual(len(set(before_ids.values())), 2)
        self.assertEqual(before_ids, after_ids)
        self.assertIn(format(prices[1], ".17g"), before_ids[prices[1]])


class RoundCandidateRegressionTest(unittest.TestCase):
    def test_overlapping_round_steps_keep_largest_step_without_duplicates(self):
        cases = (
            ("BTCUSDT", 64_900.0, 65_600.0, {65_000.0: 1000.0, 65_500.0: 500.0}),
            ("ETHUSDT", 3_450.0, 3_550.0, {3_500.0: 100.0}),
        )
        for symbol, low, high, expected in cases:
            bars = [
                Kline(0, low, high, low, (low + high) / 2, 1.0, 60_000),
                Kline(60_000, low, high, low, (low + high) / 2, 1.0, 120_000),
            ]
            with self.subTest(symbol=symbol):
                pivot_levels = [
                    pivot_level(
                        f"{kind.lower()}-{price:g}",
                        price,
                        price,
                        kind=kind,
                    )
                    for price in expected
                    for kind in ("SUPPORT", "RESISTANCE")
                ]
                levels = _round_candidates(
                    symbol,
                    bars,
                    20.0,
                    StructureConfig(),
                    pivot_levels,
                )
                keys = [
                    (item["kind"], item["round_level_price"])
                    for item in levels
                ]
                self.assertEqual(len(keys), len(set(keys)))
                for price, step in expected.items():
                    matching = [
                        item
                        for item in levels
                        if item["round_level_price"] == price
                    ]
                    self.assertEqual(len(matching), 2)
                    self.assertEqual(
                        {item["round_level_step"] for item in matching},
                        {step},
                    )

    def test_large_historical_span_has_bounded_candidate_count_and_runtime(self):
        bars = [
            Kline(0, 100.0, 101.0, 99.0, 100.0, 1.0, 60_000),
            Kline(
                60_000,
                1_000_000.0,
                1_000_001.0,
                999_999.0,
                1_000_000.0,
                1.0,
                120_000,
            ),
        ]

        started = time.perf_counter()
        levels = _round_candidates("BTCUSDT", bars, 10.0, StructureConfig())
        elapsed = time.perf_counter() - started

        self.assertEqual(levels, [])
        self.assertLess(elapsed, 0.5)

    def test_unqualified_merge_candidates_are_bounded_by_zone_anchors(self):
        bars = [
            Kline(
                index * 60_000,
                20_000.0 + index * 1_000.0 + 50.0,
                20_000.0 + index * 1_000.0 + 60.0,
                20_000.0 + index * 1_000.0 + 40.0,
                20_000.0 + index * 1_000.0 + 50.0,
                1.0,
                (index + 1) * 60_000,
            )
            for index in range(240)
        ]
        config = StructureConfig(cluster_atr=1_000.0)

        levels = _round_candidates(
            "BTCUSDT",
            bars,
            1_000.0,
            config,
            [pivot_level("wide-zone", 500_000.0, 500_000.0)],
        )

        self.assertTrue(levels)
        self.assertTrue(
            all(not item["_independently_qualified"] for item in levels)
        )
        self.assertLessEqual(len(levels), 30)

    def test_zone_candidate_accepts_only_rounding_over_boundary(self):
        boundary_zone_price = 100.4 - (100.0 + 0.1 + 0.05) + 100.0
        config = StructureConfig(cluster_atr=0.25)

        levels = _round_candidates(
            "BTCUSDT",
            make_bars(2, price=100.0),
            1.0,
            config,
            [
                pivot_level(
                    "boundary-zone",
                    boundary_zone_price,
                    boundary_zone_price,
                )
            ],
        )

        self.assertGreater(boundary_zone_price - 100.0, 0.25)
        boundary_round = next(
            item
            for item in levels
            if item["kind"] == "SUPPORT"
            and item["round_level_price"] == 100.0
        )
        self.assertFalse(boundary_round["_independently_qualified"])
        self.assertFalse(boundary_round["_emit_independently"])

    def test_nearest_independently_qualified_resistance_survives_pruning(self):
        rng = random.Random(819)
        groups = []
        for _ in range(3):
            bases = [20_000.0 + index * 100.0 for index in range(240)]
            rng.shuffle(bases)
            bars = []
            for index, base in enumerate(bases):
                close = base + 50.0
                bars.append(
                    Kline(
                        index * 60_000,
                        close,
                        base + 60.0,
                        base + 40.0,
                        close,
                        1.0,
                        (index + 1) * 60_000,
                    )
                )
            groups.append(bars)
        bars = groups[-1]
        bars[233] = Kline(
            233 * 60_000,
            50_200.0,
            50_350.0,
            50_150.0,
            50_200.0,
            1.0,
            234 * 60_000,
        )
        bars[239] = Kline(
            239 * 60_000,
            49_800.0,
            50_350.0,
            49_500.0,
            49_650.0,
            1.0,
            240 * 60_000,
        )

        levels = _round_candidates(
            "BTCUSDT",
            bars,
            500.0,
            StructureConfig(),
        )
        qualified_resistance = [
            item
            for item in levels
            if item["kind"] == "RESISTANCE"
            and item["_independently_qualified"]
            and item["round_level_price"] > bars[-1].close
        ]

        self.assertTrue(qualified_resistance)
        nearest = min(
            qualified_resistance,
            key=lambda item: item["round_level_price"] - bars[-1].close,
        )
        self.assertEqual(nearest["round_level_price"], 50_300.0)
        self.assertEqual(nearest["touch_indexes"], (233, 239))

    def test_large_round_prices_have_collision_free_stable_ids(self):
        bars = [
            Kline(
                index * 60_000,
                123_456_750.0,
                123_456_850.0,
                123_456_650.0,
                123_456_750.0,
                1.0,
                (index + 1) * 60_000,
            )
            for index in range(2)
        ]

        levels = _round_candidates(
            "BTCUSDT",
            bars,
            100.0,
            StructureConfig(),
            [
                pivot_level(
                    f"support-{price:.15g}",
                    price,
                    price,
                )
                for price in (123_456_700.0, 123_456_800.0)
            ],
        )
        round_ids = {
            item["round_level_price"]: item["id"]
            for item in levels
            if item["kind"] == "SUPPORT"
            and item["round_level_price"]
            in (123_456_700.0, 123_456_800.0)
        }

        self.assertEqual(len(round_ids), 2)
        self.assertEqual(len(set(round_ids.values())), 2)
        self.assertEqual(
            round_ids[123_456_700.0],
            "round-support-123456700",
        )
        self.assertEqual(
            round_ids[123_456_800.0],
            "round-support-123456800",
        )

    def test_round_ids_distinguish_one_hundred_at_1e17(self):
        prices = (1e17, 1e17 + 100.0)
        bars = [
            Kline(
                index * 60_000,
                prices[0],
                prices[1],
                prices[0],
                prices[0],
                1.0,
                (index + 1) * 60_000,
            )
            for index in range(2)
        ]
        levels = _round_candidates(
            "BTCUSDT",
            bars,
            100.0,
            StructureConfig(),
            [
                pivot_level(
                    f"support-{format(price, '.17g')}",
                    price,
                    price,
                )
                for price in prices
            ],
        )
        round_ids = {
            item["round_level_price"]: item["id"]
            for item in levels
            if item["kind"] == "SUPPORT"
            and item["round_level_price"] in prices
        }

        self.assertEqual(len(round_ids), 2)
        self.assertEqual(len(set(round_ids.values())), 2)
        self.assertEqual(
            round_ids[prices[1]],
            f"round-support-{format(prices[1], '.17g')}",
        )

    def test_zone_candidates_do_not_bypass_independent_emit_limit(self):
        bars = make_bars(240, price=500_000.0)
        for index in (0, 5):
            bars[index] = replace_bar(
                bars[index],
                open=900_000.0,
                high=900_100.0,
                low=1.0,
                close=900_000.0,
            )
        for index in (10, 15):
            bars[index] = replace_bar(
                bars[index],
                open=100_000.0,
                high=900_000.0,
                low=99_900.0,
                close=100_000.0,
            )
        config = StructureConfig(cluster_atr=5.0)
        pivot_levels = [
            pivot_level(
                f"near-{kind.lower()}-zone-{index}",
                300_000.0 + index * 20_000.0,
                300_000.0 + index * 20_000.0,
                kind=kind,
            )
            for kind in ("SUPPORT", "RESISTANCE")
            for index in range(12)
        ]
        pivot_levels.append(
            pivot_level("far-zone", 100_000.0, 100_000.0)
        )

        round_levels = _round_candidates(
            "BTCUSDT",
            bars,
            1_000.0,
            config,
            pivot_levels,
        )
        qualified_by_kind = {
            kind: [
                item
                for item in round_levels
                if item["kind"] == kind
                and item["_independently_qualified"]
            ]
            for kind in ("SUPPORT", "RESISTANCE")
        }
        for kind, qualified in qualified_by_kind.items():
            with self.subTest(kind=kind):
                self.assertGreater(len(qualified), 96)
                self.assertEqual(
                    sum(
                        item.get("_emit_independently", False)
                        for item in qualified
                    ),
                    96,
                )
        self.assertTrue(
            any(
                item["round_level_price"] == 100_000.0
                and not item["_emit_independently"]
                for item in qualified_by_kind["SUPPORT"]
            )
        )

        merged = _merge_round_levels(
            pivot_levels,
            round_levels,
            1_000.0,
            config,
        )
        far_zone = next(item for item in merged if item["id"] == "far-zone")

        for kind in ("SUPPORT", "RESISTANCE"):
            with self.subTest(final_kind=kind):
                self.assertLessEqual(
                    sum(
                        item["kind"] == kind and item["source"] == "ROUND"
                        for item in merged
                    ),
                    96,
                )
        self.assertTrue(
            any(item["round_level_price"] == 500_000.0 for item in merged)
        )
        self.assertEqual(far_zone["source"], "MERGED")
        self.assertEqual(far_zone["round_level_price"], 100_000.0)
        self.assertTrue(
            all(
                "_independently_qualified" not in item
                and "_emit_independently" not in item
                for item in merged
            )
        )


class RoundMergeRegressionTest(unittest.TestCase):
    def test_round_merges_with_only_one_pivot_zone_by_documented_priority(self):
        config = StructureConfig()
        pivots = [
            pivot_level("older-many", 99.9, 100.1, pivot_count=4, confirmed_at=100),
            pivot_level("newer-many", 99.9, 100.1, pivot_count=4, confirmed_at=200),
            pivot_level("newer-few", 99.9, 100.1, pivot_count=2, confirmed_at=300),
        ]
        rounds = [round_level("round-support-100", 100.0)]

        levels = _merge_round_levels(pivots, rounds, 1.0, config)
        merged = [item for item in levels if item["source"] == "MERGED"]

        self.assertEqual([item["id"] for item in merged], ["newer-many"])
        self.assertEqual(
            sum(item.get("round_level_price") == 100.0 for item in levels),
            1,
        )

    def test_each_pivot_zone_receives_at_most_one_round(self):
        config = StructureConfig()
        pivots = [pivot_level("zone", 99.9, 100.1)]
        rounds = [
            round_level("round-support-99.8", 99.8, independently_qualified=True),
            round_level("round-support-100", 100.0, independently_qualified=True),
        ]

        levels = _merge_round_levels(pivots, rounds, 1.0, config)
        zone = next(item for item in levels if item["id"] == "zone")

        self.assertEqual(zone["round_level_price"], 100.0)
        self.assertEqual(
            [item["id"] for item in levels if item["source"] == "ROUND"],
            ["round-support-99.8"],
        )

    def test_matching_maximizes_cardinality_before_distance(self):
        config = StructureConfig()
        pivots = [
            pivot_level("p1", 100.0, 100.0),
            pivot_level("p2", 100.2, 100.2),
        ]
        rounds = [
            round_level("r1", 100.05, independently_qualified=True),
            round_level("r2", 99.9, independently_qualified=True),
        ]

        levels = _merge_round_levels(pivots, rounds, 1.0, config)
        merged = {
            item["id"]: item["round_level_price"]
            for item in levels
            if item["source"] == "MERGED"
        }

        self.assertEqual(merged, {"p1": 99.9, "p2": 100.05})

    def test_matching_minimizes_total_distance_after_cardinality(self):
        config = StructureConfig()
        pivots = [
            pivot_level("p1", 100.0, 100.0),
            pivot_level("p2", 100.2, 100.2),
        ]
        rounds = [
            round_level("r1", 100.05, independently_qualified=True),
            round_level("r2", 100.15, independently_qualified=True),
        ]

        levels = _merge_round_levels(pivots, rounds, 1.0, config)
        merged = {
            item["id"]: item["round_level_price"]
            for item in levels
            if item["source"] == "MERGED"
        }

        self.assertEqual(merged, {"p1": 100.05, "p2": 100.15})

    def test_boundary_rounding_edge_preserves_maximum_cardinality(self):
        config = StructureConfig(cluster_atr=0.25)
        boundary_round_price = 100.0 + 0.1 + 0.05
        pivots = [
            pivot_level("p1", 100.0, 100.0),
            pivot_level("p2", 100.40, 100.40),
        ]
        rounds = [
            round_level(
                "r1",
                boundary_round_price,
                independently_qualified=True,
            ),
            round_level("r2", 99.9, independently_qualified=True),
        ]

        levels = _merge_round_levels(pivots, rounds, 1.0, config)
        merged = {
            item["id"]: item["round_level_price"]
            for item in levels
            if item["source"] == "MERGED"
        }

        self.assertAlmostEqual(boundary_round_price, 100.15)
        self.assertGreater(100.40 - boundary_round_price, 0.25)
        self.assertEqual(
            merged,
            {"p1": 99.9, "p2": boundary_round_price},
        )

    def test_boundary_tolerance_only_covers_floating_point_rounding(self):
        from app.entry_structure_shadow import _within_distance_limit

        maximum_distance = 0.25
        rounding_distance = 100.40 - (100.0 + 0.1 + 0.05)

        self.assertGreater(rounding_distance, maximum_distance)
        self.assertTrue(_within_distance_limit(rounding_distance, maximum_distance))
        self.assertFalse(
            _within_distance_limit(maximum_distance + 1e-9, maximum_distance)
        )

        tiny_maximum = 1e-15
        self.assertTrue(
            _within_distance_limit(
                math.nextafter(tiny_maximum, math.inf),
                tiny_maximum,
            )
        )
        self.assertFalse(
            _within_distance_limit(tiny_maximum * 2.0, tiny_maximum)
        )

    def test_merge_is_repeatable_and_does_not_mutate_inputs(self):
        config = StructureConfig()
        pivots = [pivot_level("zone", 100.0, 100.0)]
        rounds = [
            round_level("matched", 100.0),
            round_level("unmatched", 101.0, independently_qualified=True),
        ]
        original_pivots = deepcopy(pivots)
        original_rounds = deepcopy(rounds)

        first = _merge_round_levels(pivots, rounds, 1.0, config)
        second = _merge_round_levels(pivots, rounds, 1.0, config)

        self.assertEqual(first, second)
        self.assertEqual(pivots, original_pivots)
        self.assertEqual(rounds, original_rounds)


if __name__ == "__main__":
    unittest.main()
