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
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        value = value.replace(tzinfo=SHANGHAI)
    return int(value.timestamp() * 1000)


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
    pnl: float | None = None,
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
        pnl=(8.0 if result == "WIN" else -10.0) if pnl is None else pnl,
    )


def result_rows(
    cutoff: int,
    results: list[str],
    *,
    prefix: str,
    age_days: int = 1,
    tag: str = "generic_short_observe",
    segment: str = "WD-02",
    pnl_by_result: dict[str, float] | None = None,
    interval_ms: int = 20 * 60_000,
    duration_ms: int = 10 * 60_000,
) -> list[ObservationSignal]:
    end = cutoff - age_days * 86_400_000
    start = end - len(results) * interval_ms
    return [
        observation(
            f"{prefix}-{index}",
            result,
            start + index * interval_ms,
            tag=tag,
            segment=segment,
            expires_at=start + index * interval_ms + duration_ms,
            pnl=(pnl_by_result or {}).get(result),
        )
        for index, result in enumerate(results)
    ]


def candidate(snapshot: dict, tag: str = "generic_short_observe") -> dict:
    return next(item for item in snapshot["candidates"] if item["strategy_tag"] == tag)


def prior_selection(cutoff: int, item: dict, *, selected: bool = True) -> dict:
    return {
        "effective_from": selection_window(cutoff - 86_400_000)["effective_from"],
        "candidates": [dict(item)],
        "selected_profiles": [dict(item)] if selected else [],
    }


class DailyProfileSelectorTest(unittest.TestCase):
    def test_config_normalizes_positive_dual_windows_and_failure_limit(self):
        defaults = DailyProfileSelectorConfig().normalized()
        self.assertEqual(defaults.lookback_days, 7)
        self.assertEqual(defaults.stable_lookback_days, 14)
        self.assertEqual(defaults.joint_failures_to_exit, 2)

        normalized = DailyProfileSelectorConfig(
            lookback_days=9,
            stable_lookback_days=0,
            joint_failures_to_exit=0,
        ).normalized()
        self.assertEqual(normalized.lookback_days, 9)
        self.assertEqual(normalized.stable_lookback_days, 9)
        self.assertEqual(normalized.joint_failures_to_exit, 1)

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

    def test_utc_cross_date_and_weekend_profile_keys_remain_distinct(self):
        cutoff = timestamp("2026-08-10T07:50:00")
        self.assertEqual(cutoff, timestamp("2026-08-09T23:50:00+00:00"))
        rows = result_rows(
            cutoff,
            ["WIN"] * 6 + ["LOSS"] * 4,
            prefix="weekend",
            segment="WE-23",
        )
        rows.extend(
            result_rows(
                cutoff,
                ["WIN"] * 6 + ["LOSS"] * 4,
                prefix="weekday",
                segment="WD-23",
            )
        )

        snapshot = build_daily_selection(rows, cutoff)

        self.assertEqual([item["threshold_segment"] for item in snapshot["selected_profiles"]], ["WE-23"])
        self.assertEqual(candidate(snapshot, "generic_short_observe")["threshold_segment"], "WD-23")

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

    def test_both_windows_use_strict_cutoff_and_independent_overlap_and_identity_deduplication(self):
        cutoff = timestamp("2026-07-30T07:50:00")
        fast_start = cutoff - 7 * 86_400_000
        stable_start = cutoff - 14 * 86_400_000
        rows = [
            observation("before-stable", "WIN", stable_start - 1, pnl=0.0),
            observation("stable-boundary", "WIN", stable_start, pnl=0.0),
            observation("duplicate", "WIN", stable_start + 20 * 60_000, pnl=0.0),
            observation("duplicate", "WIN", stable_start + 40 * 60_000, pnl=0.0),
            observation("fast-boundary", "WIN", fast_start, pnl=0.0),
            observation("fast-overlap", "WIN", fast_start + 5 * 60_000, pnl=0.0),
            observation("fast-independent", "WIN", fast_start + 20 * 60_000, pnl=0.0),
            observation("settles-at-cutoff", "WIN", cutoff - 5 * 60_000, expires_at=cutoff, pnl=0.0),
            observation("opens-at-cutoff", "WIN", cutoff, pnl=0.0),
            observation("future", "WIN", cutoff + 60_000, pnl=0.0),
        ]

        snapshot = build_daily_selection(
            rows,
            cutoff,
            config=DailyProfileSelectorConfig(min_samples=1, weekend_min_samples=1),
        )
        item = candidate(snapshot)

        self.assertEqual(item["fast_7d"]["sample_size"], 2)
        self.assertEqual(item["stable_14d"]["sample_size"], 4)
        self.assertEqual(item["fast_7d"]["lookback_start"], fast_start)
        self.assertEqual(item["stable_14d"]["lookback_start"], stable_start)
        self.assertEqual(item["fast_7d"]["lookback_end"], cutoff)
        self.assertEqual(item["stable_14d"]["lookback_end"], cutoff)

    def test_wd_we_threshold_boundaries_and_rejections(self):
        cutoff = timestamp("2026-07-30T07:50:00")
        rows = []
        cases = [
            ("wd-pass", ["WIN"] * 12 + ["LOSS"] * 8, "WD-02", {"WIN": 0.0, "LOSS": 0.0}),
            ("wd-short", ["WIN"] * 12 + ["LOSS"] * 7, "WD-02", {"WIN": 0.0, "LOSS": 0.0}),
            ("we-pass", ["WIN"] * 6 + ["LOSS"] * 4, "WE-02", {"WIN": 0.0, "LOSS": 0.0}),
            ("we-short", ["WIN"] * 6 + ["LOSS"] * 3, "WE-03", {"WIN": 0.0, "LOSS": 0.0}),
            ("negative-ev", ["WIN"] * 12 + ["LOSS"] * 8, "WD-02", {"WIN": 1.0, "LOSS": -2.0}),
        ]
        for tag, results, segment, pnl_by_result in cases:
            rows.extend(
                result_rows(
                    cutoff,
                    results,
                    prefix=tag,
                    tag=tag,
                    segment=segment,
                    pnl_by_result=pnl_by_result,
                )
            )

        snapshot = build_daily_selection(rows, cutoff)

        by_tag = {item["strategy_tag"]: item for item in snapshot["candidates"]}
        self.assertEqual(by_tag["wd-pass"]["selection_state"], "SELECTED")
        self.assertEqual(by_tag["we-pass"]["selection_state"], "SELECTED")
        self.assertEqual(by_tag["wd-pass"]["fast_7d"]["win_rate"], 0.6)
        self.assertEqual(by_tag["wd-pass"]["fast_7d"]["ev"], 0.0)
        self.assertEqual(by_tag["wd-short"]["selection_state"], "INSUFFICIENT_SAMPLES")
        self.assertEqual(by_tag["we-short"]["selection_state"], "INSUFFICIENT_SAMPLES")
        self.assertEqual(by_tag["negative-ev"]["selection_state"], "LOW_EV")

    def test_5999_percent_win_rate_is_below_entry_boundary(self):
        cutoff = timestamp("2026-07-30T07:50:00")
        rows = result_rows(
            cutoff,
            ["WIN"] * 5_999 + ["LOSS"] * 4_001,
            prefix="below-60",
            tag="below-60",
            age_days=0,
            interval_ms=60_000,
            duration_ms=30_000,
        )

        snapshot = build_daily_selection(rows, cutoff)
        item = candidate(snapshot, "below-60")

        self.assertEqual(item["fast_7d"]["win_rate"], 0.5999)
        self.assertEqual(item["selection_state"], "LOW_WIN_RATE")

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

    def test_dual_window_selection_matrix_and_consecutive_joint_failures(self):
        cutoff = timestamp("2026-07-30T07:50:00")
        fast_pass = result_rows(cutoff, ["WIN"] * 12 + ["LOSS"] * 8, prefix="fast-pass", pnl_by_result={"WIN": 0.0, "LOSS": 0.0})
        new_snapshot = build_daily_selection(fast_pass, cutoff)
        new_item = candidate(new_snapshot)
        self.assertEqual((new_item["selection_state"], new_item["qualification_state"], new_item["joint_failure_runs"]), ("SELECTED", "QUALIFIED", 0))

        stable_only = result_rows(cutoff, ["WIN"] * 11 + ["LOSS"] * 9, prefix="fast-fail", pnl_by_result={"WIN": 0.0, "LOSS": 0.0})
        stable_only.extend(result_rows(cutoff, ["WIN"] * 13 + ["LOSS"] * 7, prefix="stable-pass", age_days=8, pnl_by_result={"WIN": 0.0, "LOSS": 0.0}))
        not_selected = build_daily_selection(stable_only, cutoff)
        self.assertEqual(candidate(not_selected)["selection_state"], "LOW_WIN_RATE")
        retained = build_daily_selection(stable_only, cutoff, previous_snapshot=prior_selection(cutoff, new_item))
        retained_item = candidate(retained)
        self.assertEqual((retained_item["selection_state"], retained_item["qualification_state"], retained_item["joint_failure_runs"]), ("RETAINED", "QUALIFIED", 0))

        joint_failure_rows = result_rows(cutoff, ["WIN"] * 11 + ["LOSS"] * 9, prefix="joint-fail", pnl_by_result={"WIN": 0.0, "LOSS": 0.0})
        first = build_daily_selection(joint_failure_rows, cutoff, previous_snapshot=prior_selection(cutoff, new_item))
        first_item = candidate(first)
        self.assertEqual((first_item["selection_state"], first_item["qualification_state"], first_item["joint_failure_runs"]), ("QUALIFICATION_WATCH", "QUALIFICATION_WATCH", 1))

        same_day = build_daily_selection(joint_failure_rows, cutoff, previous_snapshot=first)
        same_day_item = candidate(same_day)
        self.assertEqual((same_day_item["selection_state"], same_day_item["joint_failure_runs"]), ("QUALIFICATION_WATCH", 1))

        second = build_daily_selection(joint_failure_rows, cutoff + 86_400_000, previous_snapshot=first)
        second_item = candidate(second)
        self.assertEqual(second["selected_profiles"], [])
        self.assertEqual((second_item["selection_state"], second_item["qualification_state"], second_item["joint_failure_runs"]), ("DEGRADED_EXIT", "DEGRADED_EXIT", 2))

        repeated_exit = build_daily_selection(joint_failure_rows, cutoff + 86_400_000, previous_snapshot=second)
        repeated_item = candidate(repeated_exit)
        self.assertEqual((repeated_item["selection_state"], repeated_item["joint_failure_runs"]), ("DEGRADED_EXIT", 2))

    def test_retained_profile_resets_joint_failure_runs_when_either_window_qualifies(self):
        cutoff = timestamp("2026-07-30T07:50:00")
        rows = result_rows(cutoff, ["WIN"] * 11 + ["LOSS"] * 9, prefix="fast-fail", pnl_by_result={"WIN": 0.0, "LOSS": 0.0})
        rows.extend(result_rows(cutoff, ["WIN"] * 13 + ["LOSS"] * 7, prefix="stable-pass", age_days=8, pnl_by_result={"WIN": 0.0, "LOSS": 0.0}))
        key = profile_key(10, "short_observe", "generic_short_observe", "SHORT", "WD-02")
        watched = {
            "key": key,
            "qualification_state": "QUALIFICATION_WATCH",
            "selection_state": "QUALIFICATION_WATCH",
            "joint_failure_runs": 1,
            "fast_7d": {},
            "stable_14d": {},
        }

        snapshot = build_daily_selection(rows, cutoff, previous_snapshot=prior_selection(cutoff, watched))
        item = candidate(snapshot)

        self.assertEqual(item["selection_state"], "RETAINED")
        self.assertEqual(item["qualification_state"], "QUALIFIED")
        self.assertEqual(item["joint_failure_runs"], 0)

    def test_legacy_selected_profile_migrates_before_counting_first_failure(self):
        cutoff = timestamp("2026-07-30T07:50:00")
        rows = result_rows(cutoff, ["WIN"] * 11 + ["LOSS"] * 9, prefix="failing", pnl_by_result={"WIN": 0.0, "LOSS": 0.0})
        key = profile_key(10, "short_observe", "generic_short_observe", "SHORT", "WD-02")
        legacy = {
            "effective_from": selection_window(cutoff - 86_400_000)["effective_from"],
            "selected_profiles": [{"key": key, "degraded_runs": 4}],
        }

        migrated = build_daily_selection(rows, cutoff, previous_snapshot=legacy)
        migrated_item = candidate(migrated)
        self.assertEqual(migrated_item["selection_state"], "RETAINED")
        self.assertEqual(migrated_item["qualification_state"], "QUALIFIED")
        self.assertEqual(migrated_item["joint_failure_runs"], 0)

        next_day = build_daily_selection(rows, cutoff + 86_400_000, previous_snapshot=migrated)
        next_item = candidate(next_day)
        self.assertEqual((next_item["selection_state"], next_item["joint_failure_runs"]), ("QUALIFICATION_WATCH", 1))

    def test_snapshot_keeps_legacy_metrics_and_adds_versioned_dual_window_state(self):
        cutoff = timestamp("2026-07-30T07:50:00")
        rows = result_rows(cutoff, ["WIN"] * 12 + ["LOSS"] * 8, prefix="qualified", pnl_by_result={"WIN": 0.0, "LOSS": 0.0})

        snapshot = build_daily_selection(rows, cutoff)
        item = candidate(snapshot)

        self.assertIn("version", snapshot)
        self.assertIn("reason", snapshot)
        self.assertIn("fast_7d", snapshot)
        self.assertIn("stable_14d", snapshot)
        self.assertEqual(item["sample_size"], item["fast_7d"]["sample_size"])
        self.assertEqual(item["win_rate"], item["fast_7d"]["win_rate"])
        self.assertEqual(item["ev"], item["fast_7d"]["ev"])
        self.assertIn("qualification_state", item)
        self.assertIn("selection_state", item)
        self.assertIn("joint_failure_runs", item)
        self.assertIn("reason", item)
        self.assertIn("version", item)


if __name__ == "__main__":
    unittest.main()
