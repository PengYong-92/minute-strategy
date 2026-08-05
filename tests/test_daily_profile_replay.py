import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.daily_profile_selector import DailyProfileSelectorConfig
from app.models import ObservationSignal
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
