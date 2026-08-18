import unittest

from app.models import Kline


def bars_from_ranges(ranges):
    result = []
    previous_close = float(ranges[0][2])
    for index, (low, high, close) in enumerate(ranges):
        result.append(
            Kline(
                open_time=index * 60_000,
                open=previous_close,
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


if __name__ == "__main__":
    unittest.main()
