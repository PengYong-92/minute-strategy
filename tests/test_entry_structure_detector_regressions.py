import itertools
import math
import random
import unittest
from unittest.mock import patch

from app.entry_structure_shadow import (
    StructureConfig,
    StructureDetector,
    _cluster_pivots,
    _merge_round_levels,
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
    }


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


if __name__ == "__main__":
    unittest.main()
