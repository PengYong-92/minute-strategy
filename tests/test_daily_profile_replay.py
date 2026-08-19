import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.daily_profile_selector import DailyProfileSelectorConfig
from app.models import ObservationSignal
from app.storage import SQLiteMonitorStore
from scripts import replay_daily_profile_selector as replay_module
from tests.test_storage import ENTRY_STRUCTURE_FIXTURE, structured_atomic_bundle


SHANGHAI = ZoneInfo("Asia/Shanghai")


def timestamp(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=SHANGHAI).timestamp() * 1000)


def observation(
    key: str,
    result: str,
    opened_at: int,
    *,
    direction: str = "SHORT",
    tag: str = "generic_short_observe",
    settled_at: int | None = None,
) -> ObservationSignal:
    expires_at = opened_at + 10 * 60_000
    return ObservationSignal(
        observation_key=key,
        strategy_family="short_observe",
        strategy_tag=tag,
        direction=direction,
        timeframe_minutes=10,
        level="B",
        reason="test",
        entry_price=100.0,
        opened_at=opened_at,
        expires_at=expires_at,
        threshold_segment="WD-01",
        status="SETTLED",
        result=result,
        settled_at=expires_at if settled_at is None else settled_at,
        pnl=8.0 if result == "WIN" else -10.0,
        decision_id=f"decision-{key}",
        entry_structure_shadow={
            "version": "ENTRY_STRUCTURE_SHADOW_V1",
            "entry_structure_mode": "SHADOW_ONLY",
            "entry_structure_state": "CONFIRMED",
            "would_block": False,
        },
    )


def selector_config() -> DailyProfileSelectorConfig:
    return DailyProfileSelectorConfig(
        lookback_days=1,
        stable_lookback_days=1,
        min_samples=1,
        weekend_min_samples=1,
        min_win_rate=0.0,
        min_ev=-100.0,
        exit_win_rate=0.0,
        exit_ev=-100.0,
        max_active_profiles=0,
    )


class DailyProfileReplayTest(unittest.TestCase):
    def production_execution(self, **overrides):
        self.assertTrue(
            hasattr(replay_module, "ReplayExecutionConfig"),
            "ReplayExecutionConfig must make every production value explicit",
        )
        values = {
            "max_open_orders": 2,
            "max_open_long_orders": 1,
            "max_open_short_orders": 2,
            "min_order_gap_ms": 2 * 60_000,
            "stake": 10.0,
            "win_return": 18.0,
            "stake_progression_enabled": True,
            "stake_progression_max_orders": 2,
            "stake_progression_max_active": 1,
            "stake_progression_second_stake": 18.0,
            "stake_progression_base_only_segments": (),
        }
        values.update(overrides)
        return replay_module.ReplayExecutionConfig(**values)

    def replay(self, rows, **execution_overrides):
        return replay_module.replay_daily_profile_selection(
            rows,
            selector_config(),
            execution=self.production_execution(**execution_overrides),
            require_full_lookback=False,
        )

    def test_replay_rejects_omitted_production_execution_settings(self):
        with self.assertRaisesRegex(ValueError, "explicit production execution settings"):
            replay_module.replay_daily_profile_selection([], selector_config())

    def test_execution_rejects_second_stake_that_differs_from_win_return(self):
        for second_stake in (17.0, 18.00001):
            with self.subTest(second_stake=second_stake), self.assertRaisesRegex(
                ValueError,
                "stake_progression_second_stake must equal win_return",
            ):
                self.production_execution(
                    stake_progression_second_stake=second_stake
                ).normalized()

    def test_execution_rejects_nonempty_base_only_segments(self):
        with self.assertRaisesRegex(
            ValueError,
            "stake_progression_base_only_segments must be empty",
        ):
            self.production_execution(
                stake_progression_base_only_segments=("WD-01",)
            ).normalized()

    def test_cli_rejects_missing_production_settings_and_help_lists_every_setting(self):
        with redirect_stderr(StringIO()) as stderr:
            with self.assertRaises(SystemExit) as raised:
                replay_module.main(["--db-path", "/tmp/replay.sqlite3"])
        self.assertEqual(raised.exception.code, 2)
        error = stderr.getvalue()
        for flag in (
            "--max-open-orders",
            "--max-open-long-orders",
            "--max-open-short-orders",
            "--min-order-gap-minutes",
            "--stake",
            "--win-return",
            "--stake-progression",
            "--stake-progression-max-orders",
            "--stake-progression-max-active",
            "--stake-progression-second-stake",
            "--stake-progression-base-only-segments",
        ):
            self.assertIn(flag, error)

        stdout = StringIO()
        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as help_exit:
                replay_module.main(["--help"])
        self.assertEqual(help_exit.exception.code, 0)
        help_text = stdout.getvalue()
        for flag in (
            "--max-open-orders",
            "--max-open-long-orders",
            "--max-open-short-orders",
            "--min-order-gap-minutes",
            "--stake",
            "--win-return",
            "--stake-progression",
            "--no-stake-progression",
            "--stake-progression-max-orders",
            "--stake-progression-max-active",
            "--stake-progression-second-stake",
            "--stake-progression-base-only-segments",
        ):
            self.assertIn(flag, help_text)

    def test_cli_passes_explicit_production_settings_to_replay(self):
        args = [
            "--db-path", "/tmp/replay.sqlite3",
            "--max-open-orders", "2",
            "--max-open-long-orders", "1",
            "--max-open-short-orders", "2",
            "--min-order-gap-minutes", "2",
            "--stake", "10",
            "--win-return", "18",
            "--stake-progression",
            "--stake-progression-max-orders", "2",
            "--stake-progression-max-active", "1",
            "--stake-progression-second-stake", "18",
            "--stake-progression-base-only-segments", "",
        ]
        with (
            patch.object(
                replay_module,
                "load_replay_observations",
                return_value=[],
            ) as loader,
            patch.object(
                replay_module,
                "load_observations",
                side_effect=AssertionError("payload-only loader must not be used"),
                create=True,
            ),
            patch.object(
                replay_module,
                "replay_daily_profile_selection",
                return_value={},
            ) as replay,
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(replay_module.main(args), 0)

        loader.assert_called_once_with(Path("/tmp/replay.sqlite3"), "BTCUSDT")
        execution = replay.call_args.kwargs["execution"]
        self.assertEqual(execution.max_open_orders, 2)
        self.assertEqual(execution.max_open_long_orders, 1)
        self.assertEqual(execution.max_open_short_orders, 2)
        self.assertEqual(execution.min_order_gap_ms, 120_000)
        self.assertEqual(execution.stake, 10.0)
        self.assertEqual(execution.win_return, 18.0)
        self.assertTrue(execution.stake_progression_enabled)
        self.assertEqual(execution.stake_progression_max_orders, 2)
        self.assertEqual(execution.stake_progression_max_active, 1)
        self.assertEqual(execution.stake_progression_second_stake, 18.0)

    def test_read_only_loader_hydrates_v2_structured_bundle_like_storage(self):
        self.assertTrue(
            hasattr(replay_module, "load_replay_observations"),
            "replay must provide a dedicated read-only V2 loader",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            config, context, order, audit, entry_snapshot, observed = (
                structured_atomic_bundle()
            )
            store.save_open_order_decision(
                config=config,
                context=context,
                order=order,
                credit=None,
                entry_snapshot=entry_snapshot,
                audit=audit,
                observation=observed,
            )
            persisted = store.load_observations(context.symbol)[0]
            settled = replace(
                persisted,
                status="SETTLED",
                result="WIN",
                settled_at=persisted.expires_at,
                exit_price=persisted.entry_price + 1.0,
                pnl=8.0,
            )
            store.save_observation(settled, context.symbol)
            expected = store.load_adaptive_profile_observations(
                context.symbol,
                evaluated_at=settled.settled_at + 1,
            )
            store.close()
            before = hashlib.sha256(db_path.read_bytes()).hexdigest()

            actual = replay_module.load_replay_observations(db_path, context.symbol)

            after = hashlib.sha256(db_path.read_bytes()).hexdigest()

        self.assertEqual(
            [item.to_dict() for item in actual],
            [item.to_dict() for item in expected],
        )
        self.assertEqual(actual[0].entry_structure_shadow, ENTRY_STRUCTURE_FIXTURE)
        self.assertEqual(actual[0].decision_inputs, context.to_dict()["inputs"])
        self.assertEqual(after, before)

    def test_read_only_loader_keeps_legacy_payload_fixture_compatible(self):
        self.assertTrue(hasattr(replay_module, "load_replay_observations"))
        legacy = observation("legacy", "LOSS", timestamp("2026-07-20T08:00:00"))
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "legacy.sqlite3"
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    "create table observation_signals "
                    "(symbol text not null, status text not null, payload text not null)"
                )
                connection.execute(
                    "insert into observation_signals(symbol, status, payload) values (?, ?, ?)",
                    ("BTCUSDT", "SETTLED", json.dumps(legacy.to_dict())),
                )
            before = hashlib.sha256(db_path.read_bytes()).hexdigest()

            actual = replay_module.load_replay_observations(db_path, "btcusdt")

            after = hashlib.sha256(db_path.read_bytes()).hexdigest()

        self.assertEqual([item.to_dict() for item in actual], [legacy.to_dict()])
        self.assertEqual(after, before)

    def test_replay_is_settlement_causal_and_cutoff_safe(self):
        start = timestamp("2026-07-19T22:00:00")
        rows = [
            observation(f"adaptive-{index:02d}", "WIN", start + index * 11 * 60_000)
            for index in range(12)
        ]
        rows.extend(
            [
                observation(
                    "settled-after-0750",
                    "LOSS",
                    timestamp("2026-07-20T07:30:00"),
                    settled_at=timestamp("2026-07-20T07:51:00"),
                ),
                observation("candidate", "WIN", timestamp("2026-07-20T08:00:00")),
            ]
        )

        report = self.replay(list(reversed(rows)))

        event_keys = [item["observation_key"] for item in report["events"]]
        expected = [
            item.observation_key
            for item in sorted(
                rows,
                key=lambda item: (item.settled_at, item.opened_at, item.observation_key),
            )
        ]
        self.assertEqual(event_keys, expected)
        twelfth = next(item for item in report["events"] if item["observation_key"] == "adaptive-11")
        self.assertEqual(twelfth["adaptive_state_before"], "WARMUP")
        self.assertEqual(twelfth["adaptive_state_after"], "ACTIVE")
        self.assertEqual(twelfth["n12_after"]["sample_size"], 12)

        snapshot = next(
            item
            for item in report["schedule"]
            if item["evaluated_at"] == timestamp("2026-07-20T07:50:00")
        )
        self.assertNotIn("settled-after-0750", snapshot["sample_keys"])
        self.assertNotIn("candidate", snapshot["sample_keys"])
        candidate = next(
            item for item in report["candidate"]["trade_rows"] if item["observation_key"] == "candidate"
        )
        self.assertEqual(candidate["adaptive_state_before"], "ACTIVE")
        self.assertLessEqual(candidate["adaptive_evaluated_through"], candidate["opened_at"])

    def test_adaptive_event_rows_use_only_the_trailing_fifteen_days(self):
        old_start = timestamp("2026-07-01T00:00:00")
        recent = observation(
            "recent-win",
            "WIN",
            timestamp("2026-07-20T08:00:00"),
        )
        rows = [
            observation(f"old-win-{index:02d}", "WIN", old_start + index * 11 * 60_000)
            for index in range(11)
        ]
        rows.append(recent)

        events = replay_module._build_adaptive_event_rows(
            sorted(
                rows,
                key=lambda item: (
                    item.settled_at,
                    item.opened_at,
                    item.observation_key,
                ),
            )
        )

        recent_event = events[-1]
        self.assertEqual(recent_event["observation_key"], "recent-win")
        self.assertEqual(recent_event["adaptive_state_after"], "WARMUP")
        self.assertEqual(recent_event["n12_after"]["sample_size"], 1)
        self.assertEqual(recent_event["n20_after"]["sample_size"], 1)

    def test_replay_applies_direction_capacity_and_progression_on_each_settlement(self):
        rows = [
            observation("train-long", "WIN", timestamp("2026-07-20T01:00:00"), direction="LONG", tag="long"),
            observation("train-short", "WIN", timestamp("2026-07-20T01:12:00"), direction="SHORT", tag="short"),
            observation("long-first", "WIN", timestamp("2026-07-20T08:00:00"), direction="LONG", tag="long"),
            observation("long-overlap", "WIN", timestamp("2026-07-20T08:02:00"), direction="LONG", tag="long"),
            observation("short-first", "WIN", timestamp("2026-07-20T08:04:00"), direction="SHORT", tag="short"),
            observation("long-second-stage", "LOSS", timestamp("2026-07-20T08:10:00"), direction="LONG", tag="long"),
        ]

        report = self.replay(rows)
        baseline = report["baseline"]

        self.assertEqual(baseline["guard_rejections"]["direction_capacity_long"], 1)
        by_key = {item["observation_key"]: item for item in baseline["trade_rows"]}
        self.assertEqual(by_key["long-first"]["progression_step"], 1)
        self.assertEqual(by_key["long-second-stage"]["progression_step"], 2)
        self.assertEqual(by_key["long-second-stage"]["stake"], 18.0)
        self.assertEqual(
            by_key["long-second-stage"]["progression_source_order_id"],
            by_key["long-first"]["order_id"],
        )

    def test_base_first_orders_exclude_concurrent_base_second_slot(self):
        rows = [
            observation("train-short", "WIN", timestamp("2026-07-20T01:00:00")),
            observation("short-first", "LOSS", timestamp("2026-07-20T08:00:00")),
            observation("short-second-slot", "WIN", timestamp("2026-07-20T08:02:00")),
        ]

        report = self.replay(rows)
        by_key = {
            item["observation_key"]: item
            for item in report["baseline"]["trade_rows"]
        }

        self.assertEqual(by_key["short-first"]["order_slot"], "FIRST")
        self.assertEqual(by_key["short-second-slot"]["order_slot"], "SECOND")
        self.assertEqual(report["baseline"]["base_first_orders"], 1)

    def test_report_contains_baseline_candidate_metrics_and_three_oos_windows(self):
        rows = []
        for day in range(3):
            day_start = timestamp(f"2026-07-{20 + day:02d}T01:00:00")
            rows.extend(
                [
                    observation(f"train-{day}", "WIN", day_start),
                    observation(f"trade-{day}", "WIN", day_start + 7 * 60 * 60_000),
                ]
            )

        report = self.replay(rows)

        required = {
            "baseline",
            "candidate",
            "total",
            "by_direction",
            "base_first_retention",
            "maximum_drawdown",
            "longest_loss_streak",
            "daily_best",
            "daily_worst",
            "guard_rejections",
            "oos_windows",
            "acceptance",
            "passing_configuration_ranking",
            "structure_shadow_equality",
        }
        self.assertTrue(required.issubset(report))
        for name in ("baseline", "candidate"):
            section = report[name]
            self.assertIn("total", section)
            self.assertEqual(set(section["by_direction"]), {"LONG", "SHORT"})
            self.assertIn("base_first_orders", section)
            self.assertIn("daily_best", section)
            self.assertIn("daily_worst", section)
            self.assertIn("guard_rejections", section)
            self.assertEqual(len(section["oos_windows"]), 3)
            starts = [item["start_at"] for item in section["oos_windows"]]
            self.assertEqual(starts, sorted(starts))
        self.assertEqual(report["execution"]["max_open_orders"], 2)
        self.assertEqual(report["execution"]["max_open_long_orders"], 1)
        self.assertEqual(report["execution"]["max_open_short_orders"], 2)

    def test_release_gates_apply_ev_to_total_and_both_directions_and_compare_risk(self):
        self.assertTrue(hasattr(replay_module, "evaluate_release_gates"))
        summary = {
            "orders": 10,
            "wins": 7,
            "losses": 3,
            "win_rate": 0.7,
            "pnl": 20.0,
            "ev": 2.0,
            "max_drawdown": 10.0,
            "max_loss_streak": 2,
        }
        baseline = {
            "total": dict(summary),
            "by_direction": {"LONG": dict(summary), "SHORT": dict(summary)},
            "base_first_orders": 10,
            "oos_windows": [{"ev": 1.0}, {"ev": 2.0}, {"ev": -1.0}],
        }
        candidate = {
            "total": {**summary, "orders": 9, "max_drawdown": 9.0},
            "by_direction": {
                "LONG": {**summary, "orders": 8},
                "SHORT": {**summary, "orders": 7},
            },
            "base_first_orders": 9,
            "oos_windows": [{"ev": 1.0}, {"ev": 2.0}, {"ev": -1.0}],
        }

        accepted = replay_module.evaluate_release_gates(baseline, candidate)
        self.assertTrue(accepted["passed"])
        self.assertTrue(accepted["gates"]["total_ev"]["passed"])
        self.assertTrue(accepted["gates"]["long_ev"]["passed"])
        self.assertTrue(accepted["gates"]["short_ev"]["passed"])
        self.assertTrue(accepted["gates"]["maximum_drawdown_not_worse"]["passed"])
        self.assertTrue(accepted["gates"]["longest_loss_streak_not_worse"]["passed"])

        candidate["by_direction"]["SHORT"]["ev"] = -0.01
        candidate["total"]["max_drawdown"] = 10.01
        rejected = replay_module.evaluate_release_gates(baseline, candidate)
        self.assertFalse(rejected["passed"])
        self.assertFalse(rejected["gates"]["short_ev"]["passed"])
        self.assertFalse(rejected["gates"]["maximum_drawdown_not_worse"]["passed"])

    def test_passing_configurations_rank_by_win_rate_orders_then_drawdown(self):
        self.assertTrue(hasattr(replay_module, "rank_passing_configurations"))
        reports = [
            {"name": "drawdown-high", "passed": True, "total": {"win_rate": 0.62, "orders": 90, "max_drawdown": 12}},
            {"name": "more-orders", "passed": True, "total": {"win_rate": 0.62, "orders": 100, "max_drawdown": 20}},
            {"name": "best-rate", "passed": True, "total": {"win_rate": 0.63, "orders": 80, "max_drawdown": 30}},
            {"name": "drawdown-low", "passed": True, "total": {"win_rate": 0.62, "orders": 90, "max_drawdown": 10}},
            {"name": "rejected", "passed": False, "total": {"win_rate": 0.99, "orders": 999, "max_drawdown": 0}},
        ]

        ranked = replay_module.rank_passing_configurations(reports)

        self.assertEqual(
            [item["name"] for item in ranked],
            ["best-rate", "more-orders", "drawdown-low", "drawdown-high"],
        )

    def test_structure_shadow_equality_is_computed_from_independent_results(self):
        self.assertTrue(hasattr(replay_module, "build_structure_shadow_equality_report"))
        row = {
            "order_id": 1,
            "direction": "LONG",
            "opened_at": 100,
            "settled_at": 200,
            "expires_at": 200,
            "stake": 10.0,
            "win_return": 18.0,
            "progression_step": 1,
            "progression_source_order_id": None,
            "progression_version": "TWO_STAGE_V1",
            "progression_allowed": True,
        }
        equal = replay_module.build_structure_shadow_equality_report([row], [dict(row)], 1, 1)
        self.assertTrue(equal["equal"])
        self.assertEqual(equal["differences"], [])

        changed = dict(row)
        changed["stake"] = 18.0
        unequal = replay_module.build_structure_shadow_equality_report([row], [changed], 1, 1)
        self.assertFalse(unequal["equal"])
        self.assertIn("stake", unequal["differences"][0]["fields"])

        changed = dict(row)
        changed["progression_allowed"] = False
        unequal = replay_module.build_structure_shadow_equality_report([row], [changed], 1, 1)
        self.assertFalse(unequal["equal"])
        self.assertIn("progression_allowed", unequal["differences"][0]["fields"])

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

                result = replay_module.replay_daily_profile_selection(
                    rows,
                    config,
                    execution=self.production_execution(),
                    require_full_lookback=True,
                )

                self.assertEqual(
                    result["schedule_stats"]["evaluations"],
                    expected_evaluations,
                )


if __name__ == "__main__":
    unittest.main()
