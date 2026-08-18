import unittest
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.daily_profile_selector import DailyProfileSelectorConfig
from app.models import ObservationSignal
from scripts import replay_daily_profile_selector as replay_module
from scripts.replay_daily_profile_selector import replay_daily_profile_selection


SHANGHAI = ZoneInfo("Asia/Shanghai")


def timestamp(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=SHANGHAI).timestamp() * 1000)


def observation(key: str, result: str, opened_at: int, *, tag: str = "generic_short_observe") -> ObservationSignal:
    expires_at = opened_at + 10 * 60_000
    return ObservationSignal(
        observation_key=key,
        strategy_family="short_observe",
        strategy_tag=tag,
        direction="SHORT",
        timeframe_minutes=10,
        level="B",
        reason="test",
        entry_price=100.0,
        opened_at=opened_at,
        expires_at=expires_at,
        threshold_segment="WD-01",
        status="SETTLED",
        result=result,
        settled_at=expires_at,
        pnl=8.0 if result == "WIN" else -10.0,
    )


class DailyProfileReplayTest(unittest.TestCase):
    def test_cli_uses_production_dual_window_defaults_and_explicit_compatibility(self):
        cases = (
            ([], None, 14, 2, "default"),
            (["--lookback-days", "15", "--degraded-runs-to-exit", "5"], None, 15, 5, "degraded_runs_to_exit"),
            (
                [
                    "--lookback-days", "15",
                    "--stable-lookback-days", "21",
                    "--degraded-runs-to-exit", "5",
                    "--joint-failures-to-exit", "3",
                ],
                21,
                21,
                3,
                "joint_failures_to_exit",
            ),
        )
        for cli_args, raw_stable, effective_stable, failures, source in cases:
            with self.subTest(cli_args=cli_args):
                with (
                    patch.object(replay_module, "load_observations", return_value=[]),
                    patch.object(
                        replay_module,
                        "replay_daily_profile_selection",
                        return_value={},
                    ) as replay,
                    redirect_stdout(StringIO()),
                ):
                    exit_code = replay_module.main(
                        ["--db-path", "/tmp/replay.sqlite3", *cli_args]
                    )

                config = replay.call_args.args[1]
                self.assertEqual(exit_code, 0)
                self.assertEqual(config.stable_lookback_days, raw_stable)
                self.assertEqual(
                    config.effective_stable_lookback_days,
                    effective_stable,
                )
                self.assertEqual(config.joint_failures_to_exit, failures)
                self.assertEqual(config.joint_failures_source, source)

    def test_full_lookback_requires_entire_effective_stable_window(self):
        cutoff = timestamp("2026-07-30T07:50:00")
        activation = timestamp("2026-07-30T08:00:00")
        config = DailyProfileSelectorConfig(min_samples=1)
        cases = (
            (7 * 86_400_000, 0),
            (14 * 86_400_000 - 10 * 60_000, 0),
            (14 * 86_400_000, 1),
        )
        for history_ms, expected_evaluations in cases:
            with self.subTest(history_ms=history_ms):
                rows = [
                    observation("first", "WIN", cutoff - history_ms),
                    observation("last", "WIN", activation),
                ]

                result = replay_daily_profile_selection(
                    rows,
                    config,
                    require_full_lookback=True,
                )

                self.assertEqual(
                    result["schedule_stats"]["evaluations"],
                    expected_evaluations,
                )

    def test_replay_supports_five_overlapping_orders_two_minutes_apart(self):
        rows = []
        for index in range(5):
            tag = f"profile-{index}"
            rows.append(observation(f"train-{index}", "WIN", timestamp("2026-07-20T01:00:00"), tag=tag))
            rows.append(
                observation(
                    f"trade-{index}",
                    "WIN",
                    timestamp("2026-07-20T08:00:00") + index * 2 * 60_000,
                    tag=tag,
                )
            )
        config = DailyProfileSelectorConfig(
            min_samples=1,
            min_win_rate=1.0,
            min_ev=8.0,
            exit_win_rate=1.0,
            exit_ev=7.9,
        )

        result = replay_daily_profile_selection(rows, config, require_full_lookback=False)

        self.assertEqual(result["trades"]["orders"], 5)
        self.assertEqual(result["rejections"]["hold_open_order"], 0)

    def test_replay_trains_before_cutoff_and_scores_after_activation(self):
        rows = [
            observation("train-1", "WIN", timestamp("2026-07-20T01:00:00")),
            observation("train-2", "WIN", timestamp("2026-07-20T01:10:00")),
            observation("cutoff-spanning", "LOSS", timestamp("2026-07-20T07:45:00")),
            observation("test-loss", "LOSS", timestamp("2026-07-20T08:00:00")),
        ]
        config = DailyProfileSelectorConfig(
            min_samples=2,
            min_win_rate=1.0,
            min_ev=8.0,
            exit_win_rate=1.0,
            exit_ev=7.9,
        )

        result = replay_daily_profile_selection(rows, config, require_full_lookback=False)

        self.assertEqual(result["schedule"][0]["selected_profiles"][0]["sample_size"], 2)
        self.assertEqual(result["trades"]["orders"], 1)
        self.assertEqual(result["trades"]["wins"], 0)
        self.assertEqual(result["trades"]["pnl"], -10.0)
        self.assertEqual(result["leakage_violations"], 0)


if __name__ == "__main__":
    unittest.main()
