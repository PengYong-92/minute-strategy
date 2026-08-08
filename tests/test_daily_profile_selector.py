import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.daily_profile_selector import (
    DailyProfileSelectorConfig,
    build_daily_selection,
    profile_key,
    selection_window,
)
from app.models import ObservationSignal


SHANGHAI = ZoneInfo("Asia/Shanghai")


def timestamp(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=SHANGHAI).timestamp() * 1000)


def observation(
    key: str,
    result: str,
    opened_at: int,
    *,
    family: str = "short_observe",
    tag: str = "generic_short_observe",
    direction: str = "SHORT",
    segment: str = "WD-02",
    expires_at: int | None = None,
) -> ObservationSignal:
    expiry = expires_at if expires_at is not None else opened_at + 10 * 60_000
    return ObservationSignal(
        observation_key=key,
        strategy_family=family,
        strategy_tag=tag,
        direction=direction,
        timeframe_minutes=10,
        level="B",
        reason="观察",
        entry_price=100.0,
        opened_at=opened_at,
        expires_at=expiry,
        threshold_segment=segment,
        status="SETTLED",
        result=result,
        settled_at=expiry,
        pnl=8.0 if result == "WIN" else -10.0,
    )


class DailyProfileSelectorTest(unittest.TestCase):
    def test_weekend_profiles_use_attainable_sample_floor_in_seven_day_window(self):
        cutoff = timestamp("2026-08-08T07:50:00")
        results = ["WIN"] * 7 + ["LOSS"] * 3
        rows = []
        for index, result in enumerate(results):
            opened_at = cutoff - (index + 2) * 600_000
            rows.append(
                observation(
                    f"weekend-{index}",
                    result,
                    opened_at,
                    segment="WE-05",
                )
            )
            rows.append(
                observation(
                    f"weekday-{index}",
                    result,
                    opened_at,
                    segment="WD-05",
                )
            )

        snapshot = build_daily_selection(
            rows,
            cutoff,
            config=DailyProfileSelectorConfig(
                lookback_days=7,
                min_samples=20,
                weekend_min_samples=10,
            ),
        )

        self.assertEqual(
            [item["threshold_segment"] for item in snapshot["selected_profiles"]],
            ["WE-05"],
        )
        weekday = next(
            item for item in snapshot["candidates"] if item["threshold_segment"] == "WD-05"
        )
        self.assertEqual(weekday["selection_state"], "INSUFFICIENT_SAMPLES")
        self.assertIn("< 20", weekday["selection_reason"])

    def test_default_rule_has_no_profile_cap_and_rejects_below_60_percent(self):
        cutoff = timestamp("2026-07-30T07:50:00")
        rows = []
        for tag_index in range(5):
            for sample_index, result in enumerate(["WIN"] * 13 + ["LOSS"] * 8):
                rows.append(
                    observation(
                        f"qualified-{tag_index}-{sample_index}",
                        result,
                        cutoff - 86_400_000 + sample_index * 600_000,
                        tag=f"qualified-{tag_index}",
                    )
                )
        for sample_index, result in enumerate(["WIN"] * 12 + ["LOSS"] * 9):
            rows.append(
                observation(
                    f"below-floor-{sample_index}",
                    result,
                    cutoff - 43_200_000 + sample_index * 600_000,
                    tag="below-floor",
                )
            )

        snapshot = build_daily_selection(rows, cutoff)

        self.assertEqual(len(snapshot["selected_profiles"]), 5)
        self.assertTrue(all(item["win_rate"] >= 0.60 for item in snapshot["selected_profiles"]))
        rejected = next(item for item in snapshot["candidates"] if item["strategy_tag"] == "below-floor")
        self.assertEqual(rejected["selection_state"], "LOW_WIN_RATE")
        self.assertIn("60.00%", rejected["selection_reason"])

    def test_profile_key_includes_timeframe_family_tag_direction_and_segment(self):
        self.assertEqual(
            profile_key(10, "short_observe", "generic_short_observe", "SHORT", "WD-02"),
            "10|short_observe|generic_short_observe|SHORT|WD-02",
        )

    def test_selection_window_uses_shanghai_0750_cutoff_and_0800_activation(self):
        before = selection_window(timestamp("2026-07-30T07:49:59"), lookback_days=7)
        current = selection_window(timestamp("2026-07-30T07:50:00"), lookback_days=7)
        after_restart = selection_window(timestamp("2026-07-30T13:00:00"), lookback_days=7)

        self.assertEqual(before["lookback_end"], timestamp("2026-07-29T07:50:00"))
        self.assertEqual(before["effective_from"], timestamp("2026-07-29T08:00:00"))
        self.assertEqual(current["lookback_end"], timestamp("2026-07-30T07:50:00"))
        self.assertEqual(current["effective_from"], timestamp("2026-07-30T08:00:00"))
        self.assertEqual(current, after_restart)
        self.assertEqual(current["effective_until"], timestamp("2026-07-31T08:00:00"))
        self.assertEqual(current["lookback_start"], timestamp("2026-07-23T07:50:00"))

    def test_build_selection_groups_exact_profiles_and_deduplicates_overlaps(self):
        cutoff = timestamp("2026-07-30T07:50:00")
        start = cutoff - 2 * 60 * 60_000
        rows = [
            observation("a", "WIN", start),
            observation("a-overlap", "WIN", start + 5 * 60_000),
            observation("b", "WIN", start + 10 * 60_000),
            observation("c", "LOSS", start + 20 * 60_000),
            observation("other-tag", "WIN", start + 30 * 60_000, tag="other"),
            observation("outside", "WIN", cutoff - 8 * 86_400_000),
        ]
        config = DailyProfileSelectorConfig(min_samples=3, min_win_rate=0.6, min_ev=0.8)

        snapshot = build_daily_selection(rows, cutoff, config=config)

        target = next(item for item in snapshot["candidates"] if item["strategy_tag"] == "generic_short_observe")
        self.assertEqual(target["sample_size"], 3)
        self.assertEqual(target["wins"], 2)
        self.assertAlmostEqual(target["win_rate"], 2 / 3, places=6)
        self.assertEqual(target["ev"], 2.0)
        self.assertEqual(snapshot["selected_profiles"], [target])

    def test_selection_ranks_by_win_rate_ev_samples_and_caps_result(self):
        cutoff = timestamp("2026-07-30T07:50:00")
        rows = []
        for index, (tag, results) in enumerate(
            [
                ("first", ["WIN", "WIN", "LOSS"]),
                ("second", ["WIN", "WIN", "LOSS", "LOSS", "WIN"]),
                ("third", ["WIN", "WIN", "LOSS"]),
            ]
        ):
            for offset, result in enumerate(results):
                rows.append(
                    observation(
                        f"{tag}-{offset}",
                        result,
                        cutoff - (index + 2) * 3_600_000 + offset * 600_000,
                        tag=tag,
                    )
                )
        config = DailyProfileSelectorConfig(min_samples=3, min_win_rate=0.6, min_ev=0.0, max_active_profiles=2)

        snapshot = build_daily_selection(rows, cutoff, config=config)

        self.assertEqual([item["strategy_tag"] for item in snapshot["selected_profiles"]], ["first", "third"])

    def test_active_profile_exits_only_after_two_consecutive_degraded_runs(self):
        cutoff = timestamp("2026-07-30T07:50:00")
        rows = [
            observation(f"row-{index}", result, cutoff - (12 - index) * 600_000)
            for index, result in enumerate(["WIN", "WIN", "LOSS", "LOSS"])
        ]
        config = DailyProfileSelectorConfig(
            min_samples=4,
            min_win_rate=0.6,
            min_ev=0.8,
            exit_win_rate=0.56,
            exit_ev=0.0,
            degraded_runs_to_exit=2,
        )
        key = profile_key(10, "short_observe", "generic_short_observe", "SHORT", "WD-02")
        previous = {"selected_profiles": [{"key": key, "degraded_runs": 0}]}

        first = build_daily_selection(rows, cutoff, config=config, previous_snapshot=previous)
        second = build_daily_selection(rows, cutoff + 86_400_000, config=config, previous_snapshot=first)

        self.assertEqual(first["selected_profiles"][0]["degraded_runs"], 1)
        self.assertEqual(second["selected_profiles"], [])
        second_candidate = next(item for item in second["candidates"] if item["key"] == key)
        self.assertEqual(second_candidate["selection_state"], "DEGRADED_EXIT")

    def test_active_profile_inside_hysteresis_band_remains_selected(self):
        cutoff = timestamp("2026-07-30T07:50:00")
        results = ["WIN"] * 7 + ["LOSS"] * 5
        rows = [
            observation(f"row-{index}", result, cutoff - (20 - index) * 600_000)
            for index, result in enumerate(results)
        ]
        config = DailyProfileSelectorConfig(
            min_samples=12,
            min_win_rate=0.6,
            min_ev=0.8,
            exit_win_rate=0.57,
            exit_ev=0.0,
        )
        key = profile_key(10, "short_observe", "generic_short_observe", "SHORT", "WD-02")
        previous = {"selected_profiles": [{"key": key, "degraded_runs": 0}]}

        snapshot = build_daily_selection(rows, cutoff, config=config, previous_snapshot=previous)

        self.assertEqual(snapshot["selected_profiles"][0]["key"], key)
        self.assertEqual(snapshot["selected_profiles"][0]["degraded_runs"], 0)
        self.assertEqual(snapshot["selected_profiles"][0]["selection_state"], "RETAINED")


if __name__ == "__main__":
    unittest.main()
