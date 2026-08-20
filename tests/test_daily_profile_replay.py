import hashlib
import json
import random
import sqlite3
import tempfile
import unittest
from collections import Counter
from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.adaptive_profile_state import (
    AdaptiveGlobalProfileWindowReplay,
    AdaptiveProfileWindowReplay,
    rebuild_adaptive_profile_states,
)
from app.daily_profile_selector import DailyProfileSelectorConfig
from app.models import ObservationSignal, Signal
from app.profile_admission import candidate_policy
from app.simulator import AccountSimulator
from app.storage import SQLiteMonitorStore
from scripts import replay_daily_profile_selector as replay_module
from tests.test_storage import ENTRY_STRUCTURE_FIXTURE, structured_atomic_bundle
from tests.test_storage_schema import _create_e0_v3_lifecycle_fixture


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
    def cli_args(self, *extra):
        return [
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
            *extra,
        ]

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

    def adaptive_timeline(self, states):
        by_profile = {}
        settled_times = []
        for profile, settled_at, status, n12_wins, n20_ev in states:
            n12_sample_size = 11 if status == "WARMUP" else 12
            state = replay_module._adaptive_state((), profile, settled_at + 1)
            state.update(
                {
                    "status": status,
                    "transition": f"TEST->{status}",
                    "evaluated_at": settled_at + 1,
                    "n12": {
                        "sample_size": n12_sample_size,
                        "wins": n12_wins,
                        "losses": n12_sample_size - n12_wins,
                        "win_rate": n12_wins / n12_sample_size,
                        "pnl": 1.0,
                        "ev": 1.0 / n12_sample_size,
                    },
                    "n20": {
                        "sample_size": 20,
                        "wins": 12,
                        "losses": 8,
                        "win_rate": 0.6,
                        "pnl": n20_ev * 20,
                        "ev": n20_ev,
                    },
                }
            )
            by_profile.setdefault(profile, []).append((settled_at, state))
            settled_times.append(settled_at)
        return {
            "by_profile": by_profile,
            "times_by_profile": {
                profile: [item[0] for item in entries]
                for profile, entries in by_profile.items()
            },
            "settled_times": sorted(settled_times),
        }

    def test_replay_fast_lane_uses_shared_admission(self):
        opened_at = timestamp("2026-07-20T08:00:00")
        fast = observation("fast-active", "WIN", opened_at, tag="fast-active")
        fast_second = observation(
            "fast-second",
            "WIN",
            opened_at + 2 * 60_000,
            tag="fast-active",
        )
        long_active = observation(
            "long-active",
            "WIN",
            opened_at,
            direction="LONG",
            tag="long-active",
        )
        warmup = observation("short-warmup", "WIN", opened_at, tag="short-warmup")
        paused = observation("short-paused", "WIN", opened_at, tag="short-paused")
        rows = [fast, long_active, warmup, paused, fast_second]
        profiles = {
            item.observation_key: replay_module._observation_profile_key(item)
            for item in rows
        }
        snapshot = {
            "effective_from": opened_at,
            "effective_until": opened_at + 20 * 60_000,
            "selected_profiles": [],
            "candidates": [
                {
                    "key": profile,
                    "sample_size": 20,
                    "win_rate": 0.6,
                    "ev": 1.0,
                    "selection_state": "RANKED_OUT",
                }
                for profile in sorted(set(profiles.values()))
            ],
        }
        timeline = self.adaptive_timeline(
            [
                (profiles["fast-active"], opened_at - 1, "ACTIVE", 7, 1.0),
                (profiles["long-active"], opened_at - 1, "ACTIVE", 7, 1.0),
                (profiles["short-warmup"], opened_at - 1, "WARMUP", 7, 1.0),
                (profiles["short-paused"], opened_at - 1, "PAUSED", 7, 1.0),
            ]
        )

        result = replay_module._execute_replay(
            replay_module._prepare_execution_windows(rows, [snapshot]),
            self.production_execution().normalized(),
            timeline,
            apply_adaptive=True,
            include_structure_shadow=False,
            admission_policy=candidate_policy(),
        )

        self.assertEqual(
            [item["observation_key"] for item in result["trade_rows"]],
            ["fast-active"],
        )
        trade = result["trade_rows"][0]
        self.assertEqual(trade["admission_channel"], "FAST")
        self.assertEqual(trade["admission_code"], "FAST_ADMITTED")
        self.assertFalse(trade["progression_allowed"])
        self.assertEqual(result["admission_codes"]["FAST_DIRECTION_BLOCKED"], 1)
        self.assertEqual(result["admission_codes"]["FAST_STATE_BLOCKED"], 1)
        self.assertEqual(result["admission_codes"]["ADAPTIVE_PAUSED"], 1)
        self.assertEqual(result["admission_codes"]["FAST_SECOND_ORDER_BLOCKED"], 1)

    def test_blocked_resident_falls_through(self):
        opened_at = timestamp("2026-07-20T08:00:00")
        blocked = observation("resident-blocked", "LOSS", opened_at, tag="resident-blocked")
        fallback = observation("resident-fallback", "WIN", opened_at, tag="resident-fallback")
        rows = [blocked, fallback]
        blocked_profile = replay_module._observation_profile_key(blocked)
        fallback_profile = replay_module._observation_profile_key(fallback)
        snapshot = {
            "effective_from": opened_at,
            "effective_until": opened_at + 20 * 60_000,
            "selected_profiles": [
                {
                    "key": blocked_profile,
                    "sample_size": 20,
                    "win_rate": 0.7,
                    "ev": 1.0,
                    "qualification_state": "QUALIFIED",
                    "selection_state": "SELECTED",
                },
                {
                    "key": fallback_profile,
                    "sample_size": 20,
                    "win_rate": 0.6,
                    "ev": 1.0,
                    "qualification_state": "QUALIFIED",
                    "selection_state": "SELECTED",
                },
            ],
            "candidates": [],
        }
        timeline = self.adaptive_timeline(
            [
                (blocked_profile, opened_at - 1, "ACTIVE", 9, 1.0),
                (fallback_profile, opened_at - 1, "ACTIVE", 7, 1.0),
            ]
        )

        result = replay_module._execute_replay(
            replay_module._prepare_execution_windows(rows, [snapshot]),
            self.production_execution().normalized(),
            timeline,
            apply_adaptive=True,
            include_structure_shadow=False,
            admission_policy=candidate_policy(),
        )

        self.assertTrue(result["trade_rows"], result)
        self.assertEqual(result["trade_rows"][0]["observation_key"], "resident-fallback")
        self.assertEqual(result["trade_rows"][0]["admission_channel"], "RESIDENT")
        self.assertEqual(result["trade_rows"][0]["admission_code"], "RESIDENT_ADMITTED")
        self.assertEqual(result["admission_codes"]["RESIDENT_N12_OVERHEATED"], 1)

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
        self.assertIn("必须与 --win-return 相等", help_text)
        self.assertIn("当前生产兼容参数不生效", help_text)
        self.assertIn("只接受显式空字符串", help_text)

    def test_cli_passes_explicit_production_settings_to_replay(self):
        args = self.cli_args()
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

    def test_cli_rejects_invalid_and_non_finite_numbers_without_traceback(self):
        cases = (
            ("--min-win-rate", "2"),
            ("--min-win-rate", "nan"),
            ("--min-ev", "inf"),
            ("--min-order-gap-minutes", "inf"),
            ("--stake", "nan"),
            ("--win-return", "inf"),
            ("--stake-progression-second-stake", "-inf"),
        )
        for flag, value in cases:
            with self.subTest(flag=flag, value=value):
                stderr = StringIO()
                with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                    replay_module.main(self.cli_args(flag, value))
                self.assertEqual(raised.exception.code, 2)
                self.assertNotIn("Traceback", stderr.getvalue())
                self.assertIn("error:", stderr.getvalue())

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
            store.close()
            before = hashlib.sha256(db_path.read_bytes()).hexdigest()

            actual = replay_module.load_replay_observations(db_path, context.symbol)

            after = hashlib.sha256(db_path.read_bytes()).hexdigest()

        self.assertEqual(len(actual), 1)
        loaded = actual[0]
        self.assertEqual(loaded.observation_key, settled.observation_key)
        self.assertEqual(loaded.status, "SETTLED")
        self.assertEqual(loaded.result, "WIN")
        self.assertEqual(loaded.settled_at, settled.expires_at)
        self.assertEqual(loaded.exit_price, settled.entry_price + 1.0)
        self.assertEqual(loaded.pnl, 8.0)
        self.assertEqual(loaded.entry_structure_shadow, ENTRY_STRUCTURE_FIXTURE)
        self.assertEqual(loaded.decision_inputs, context.to_dict()["inputs"])
        self.assertEqual(after, before)

    def test_read_only_loader_keeps_legacy_payload_fixture_compatible(self):
        self.assertTrue(hasattr(replay_module, "load_replay_observations"))
        legacy_rows = [
            observation("legacy-train", "WIN", timestamp("2026-07-20T01:00:00")),
            observation("legacy-trade", "WIN", timestamp("2026-07-20T08:00:00")),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "legacy.sqlite3"
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute(
                    """
                    create table observation_signals (
                        symbol text not null,
                        observation_key text not null,
                        status text not null,
                        result text,
                        opened_at integer not null,
                        expires_at integer not null,
                        settled_at integer,
                        payload text not null
                    )
                    """
                )
                connection.executemany(
                    """
                    insert into observation_signals(
                        symbol, observation_key, status, result, opened_at,
                        expires_at, settled_at, payload
                    ) values (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            "BTCUSDT",
                            item.observation_key,
                            item.status,
                            item.result,
                            item.opened_at,
                            item.expires_at,
                            item.settled_at,
                            json.dumps(item.to_dict()),
                        )
                        for item in legacy_rows
                    ],
                )
                connection.commit()
            before = hashlib.sha256(db_path.read_bytes()).hexdigest()

            actual = replay_module.load_replay_observations(db_path, "btcusdt")
            try:
                report = self.replay(actual)
            except TypeError as error:
                self.fail(f"legacy compact replay failed: {error}")

            after = hashlib.sha256(db_path.read_bytes()).hexdigest()

        self.assertEqual(
            [item.to_dict() for item in actual],
            [item.to_dict() for item in legacy_rows],
        )
        self.assertEqual([item.pnl for item in actual], [8.0, 8.0])
        self.assertIn("acceptance", report)
        self.assertEqual(after, before)

    def test_read_only_loader_hydrates_v3_compact_lifecycle_schema(self):
        source = observation(
            "v3-observation",
            "WIN",
            1_000,
            direction="LONG",
            tag="v3-compact",
            settled_at=601_000,
        )
        profile = "10|short_observe|v3-compact|LONG|WD-01"
        signal_payload = source.to_dict()
        signal_payload["profile_key"] = profile
        context_inputs = {
            "identity": {
                "direction": "LONG",
                "profile_key": profile,
                "strategy_family": source.strategy_family,
                "strategy_tag": source.strategy_tag,
                "timeframe_minutes": source.timeframe_minutes,
                "threshold_segment": source.threshold_segment,
                "level": source.level,
            },
            "market": {
                "candidate_time_ms": source.opened_at,
                "entry_price": source.entry_price,
            },
            "score": {},
            "signal": signal_payload,
            "entry_structure": source.entry_structure_shadow,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "v3.sqlite3"
            with closing(sqlite3.connect(db_path)) as connection:
                _create_e0_v3_lifecycle_fixture(connection)
                connection.execute(
                    "update decision_contexts set input_payload = ?, direction = ?, "
                    "profile_key = ? where decision_id = ?",
                    (
                        json.dumps(
                            context_inputs,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        "LONG",
                        profile,
                        "v3-decision",
                    ),
                )
                connection.commit()
            before = hashlib.sha256(db_path.read_bytes()).hexdigest()

            try:
                actual = replay_module.load_replay_observations(db_path, "BTCUSDT")
            except Exception as error:  # noqa: BLE001 - turn loader errors into a TDD failure.
                self.fail(f"V3 compact replay load failed: {error}")

            after = hashlib.sha256(db_path.read_bytes()).hexdigest()

        self.assertEqual(len(actual), 1)
        loaded = actual[0]
        expected = {
            "observation_key": "v3-observation",
            "decision_id": "v3-decision",
            "direction": "LONG",
            "profile_key": profile,
            "strategy_tag": "v3-compact",
            "opened_at": 1_000,
            "expires_at": 601_000,
            "status": "SETTLED",
            "result": "WIN",
            "settled_at": 601_000,
            "exit_price": 101.0,
            "pnl": 8.0,
            "entry_structure_shadow": source.entry_structure_shadow,
        }
        for field_name, expected_value in expected.items():
            with self.subTest(field=field_name):
                self.assertEqual(getattr(loaded, field_name), expected_value)
        self.assertEqual(loaded.decision_inputs["identity"], context_inputs["identity"])
        self.assertEqual(loaded.decision_inputs["market"], context_inputs["market"])
        self.assertEqual(
            loaded.decision_inputs["entry_structure"],
            context_inputs["entry_structure"],
        )
        self.assertEqual(
            loaded.decision_inputs["signal"]["profile_key"],
            profile,
        )
        self.assertEqual(after, before)

    def test_replay_is_settlement_causal_and_cutoff_safe(self):
        start = timestamp("2026-07-19T22:00:00")
        rows = [
            observation(
                f"adaptive-{index:02d}",
                "WIN" if index in {0, 1, 3, 5, 7, 9, 11} else "LOSS",
                start + index * 11 * 60_000,
            )
            for index in range(12)
        ]
        rows.extend(
            [
                observation(
                    "settled-after-0750",
                    "WIN",
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
        self.assertEqual(twelfth["adaptive_transition_after"], "WARMUP->ACTIVE")
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

    def test_adaptive_snapshot_excludes_settlement_at_opened_at(self):
        opened_at = timestamp("2026-07-20T08:00:00")
        row = observation("same-time", "WIN", opened_at)
        profile = replay_module._observation_profile_key(row)
        timeline = self.adaptive_timeline(
            [(profile, opened_at, "ACTIVE", 7, 1.0)]
        )

        state, evaluated_through = replay_module._adaptive_state_before(
            timeline,
            profile,
            opened_at,
        )

        self.assertEqual(state["status"], "WARMUP")
        self.assertEqual(evaluated_through, 0)

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

    def test_adaptive_incremental_work_is_linear_without_arbitrary_event_cap(self):
        def work_for(count):
            rows = [
                observation(
                    f"bounded-{index}",
                    "WIN" if index % 2 else "LOSS",
                    timestamp("2026-07-01T00:00:00") + index * 60_000,
                )
                for index in range(count)
            ]
            workload = {}
            replay_module._build_adaptive_event_rows(rows, workload=workload)
            return workload

        small = work_for(1_024)
        large = work_for(2_048)

        self.assertEqual(large["adaptive_events"], 2_048)
        self.assertEqual(large["adaptive_incremental_adds"], 2_048)
        self.assertEqual(large["adaptive_window_rebuilds"], 0)
        self.assertEqual(large["adaptive_window_rebuild_input_rows"], 0)
        self.assertEqual(
            large["adaptive_incremental_adds"],
            small["adaptive_incremental_adds"] * 2,
        )
        self.assertGreater(large["adaptive_max_window_events"], 256)
        self.assertIn("adaptive_jump_cache_entries", large)
        self.assertIn("adaptive_jump_cache_entry_bound", large)
        self.assertIn("adaptive_retained_index_events", large)
        self.assertIn("adaptive_dynamic_claim_heap_entries", large)
        self.assertIn("adaptive_dynamic_claim_heap_entry_bound", large)
        self.assertIn("adaptive_dynamic_claim_heap_compactions", large)
        self.assertIn("adaptive_dynamic_claim_heap_compaction_input_rows", large)
        self.assertIn("adaptive_active_profile_trackers", large)
        self.assertIn("adaptive_retained_profile_tracker_events", large)
        self.assertIn("adaptive_retained_profile_tracker_event_bound", large)
        self.assertLessEqual(
            large["adaptive_jump_cache_entries"],
            large["adaptive_jump_cache_entry_bound"],
        )
        self.assertFalse(hasattr(replay_module, "ADAPTIVE_REBUILD_MAX_EVENTS"))

    def test_adaptive_public_window_replay_matches_full_rebuild_beyond_256_events(self):
        start = timestamp("2026-07-01T00:00:00")
        rows = []
        for index in range(340):
            row = observation(
                f"dense-{index:03d}",
                "WIN" if index % 5 not in {0, 1} else "LOSS",
                start + index * 3 * 60_000,
            )
            if index >= 40 and index % 37 == 0:
                row = replace(
                    row,
                    observation_key=rows[index - 19].observation_key,
                    decision_id=f"conflicting-observation-{index}",
                )
            elif index >= 40 and index % 41 == 0:
                row = replace(
                    row,
                    decision_id=rows[index - 23].decision_id,
                )
            elif index >= 40 and index % 43 == 0:
                owner = rows[index - 29]
                row = replace(
                    row,
                    observation_key=owner.observation_key,
                    decision_id=owner.decision_id,
                )
            rows.append(row)

        key = replay_module._observation_profile_key(rows[0])
        tracker = AdaptiveProfileWindowReplay(key, lookback_ms=15 * 86_400_000)
        prefix = []
        for index, row in enumerate(rows):
            prefix.append(row)
            evaluated_at = int(row.settled_at) + 1
            expected = rebuild_adaptive_profile_states(prefix, evaluated_at)[key]
            actual = tracker.advance(row, evaluated_at)
            with self.subTest(index=index):
                self.assertEqual(actual, expected)

        self.assertEqual(tracker.workload["incremental_adds"], len(rows))
        self.assertEqual(tracker.workload["window_rebuilds"], 0)
        self.assertEqual(tracker.workload["max_window_events"], len(rows))

    def test_adaptive_event_rows_match_global_window_rebuild_across_profiles(self):
        start = timestamp("2026-07-01T00:00:00")
        owner = replace(
            observation(
                "shared-observation",
                "WIN",
                start,
                direction="LONG",
                tag="generic_long_observe",
            ),
            decision_id="long-owner-decision",
            threshold_segment="WD-01",
        )
        rows = [owner]
        rows.append(
            replace(
                observation(
                    "shared-observation",
                    "LOSS",
                    start + 11 * 60_000,
                    direction="SHORT",
                    tag="generic_short_observe",
                ),
                decision_id="short-conflicting-decision",
                threshold_segment="WE-04",
            )
        )
        short_owner = replace(
            observation(
                "short-owner-observation",
                "WIN",
                start + 22 * 60_000,
                direction="SHORT",
                tag="short_pulse_observe",
            ),
            decision_id="shared-decision",
            threshold_segment="WD-08",
        )
        rows.append(short_owner)
        rows.append(
            replace(
                observation(
                    "long-conflicting-observation",
                    "LOSS",
                    start + 33 * 60_000,
                    direction="LONG",
                    tag="long_reversal_observe",
                ),
                decision_id="shared-decision",
                threshold_segment="WD-15",
            )
        )
        rows.extend(
            replace(
                observation(
                    f"normal-{index:02d}",
                    "WIN" if index % 3 else "LOSS",
                    start + (44 + index * 11) * 60_000,
                    direction="LONG" if index % 2 else "SHORT",
                    tag=(
                        "generic_long_observe"
                        if index % 2
                        else "generic_short_observe"
                    ),
                ),
                threshold_segment="WD-01" if index % 2 else "WE-04",
            )
            for index in range(24)
        )

        ordered = sorted(rows, key=replay_module._settlement_event_key)
        actual_rows = replay_module._build_adaptive_event_rows(ordered)
        lookback_ms = 15 * 86_400_000
        prefix = []
        for index, (event, actual) in enumerate(zip(ordered, actual_rows)):
            prefix.append(event)
            evaluated_at = int(event.settled_at) + 1
            window = [
                item
                for item in prefix
                if evaluated_at - lookback_ms <= int(item.settled_at) < evaluated_at
            ]
            key = replay_module._observation_profile_key(event)
            expected = rebuild_adaptive_profile_states(window, evaluated_at).get(key)
            if expected is None:
                expected = replay_module._adaptive_state((), key, evaluated_at)
            with self.subTest(index=index, profile=key):
                self.assertEqual(actual["adaptive_state_after"], expected["status"])
                self.assertEqual(actual["n12_after"], expected["n12"])
                self.assertEqual(actual["n20_after"], expected["n20"])
                self.assertEqual(actual["adaptive_evaluated_at"], evaluated_at)

    def test_adaptive_event_rows_use_production_global_tie_order(self):
        settled_at = timestamp("2026-07-01T01:00:00")
        earlier_opened = replace(
            observation(
                "tied-global-identity",
                "LOSS",
                timestamp("2026-07-01T00:30:00"),
                direction="SHORT",
                tag="z_short_observe",
                settled_at=settled_at,
            ),
            decision_id="short-tied-decision",
            threshold_segment="WE-04",
        )
        later_opened = replace(
            observation(
                "tied-global-identity",
                "WIN",
                timestamp("2026-07-01T00:40:00"),
                direction="LONG",
                tag="a_long_observe",
                settled_at=settled_at,
            ),
            decision_id="long-tied-decision",
            threshold_segment="WD-15",
        )

        actual = replay_module._build_adaptive_event_rows(
            [earlier_opened, later_opened]
        )
        expected_events = sorted(
            [earlier_opened, later_opened],
            key=replay_module.adaptive_replay_event_sort_key,
        )

        self.assertEqual(
            [row["profile_key"] for row in actual],
            [
                replay_module._observation_profile_key(event)
                for event in expected_events
            ],
        )
        accepted_key = replay_module._observation_profile_key(expected_events[0])
        expected = rebuild_adaptive_profile_states(
            expected_events,
            settled_at + 1,
        )[accepted_key]
        accepted_row = actual[0]
        self.assertEqual(accepted_row["n12_after"], expected["n12"])

    def test_adaptive_global_identity_owner_transfer_matches_window_rebuild(self):
        start = timestamp("2026-01-01T00:00:00")
        rows = []
        for index in range(230):
            direction = "LONG" if index % 2 else "SHORT"
            row = replace(
                observation(
                    f"transfer-{index:03d}",
                    "WIN" if index % 4 else "LOSS",
                    start + index * 2 * 60 * 60_000,
                    direction=direction,
                    tag=(
                        "long_transfer_observe"
                        if direction == "LONG"
                        else "short_transfer_observe"
                    ),
                ),
                threshold_segment="WD-15" if direction == "LONG" else "WE-04",
            )
            rows.append(row)
        rows[20] = replace(
            rows[20],
            observation_key="expiring-cross-profile-observation",
            decision_id="expiring-observation-owner",
        )
        rows[30] = replace(
            rows[30],
            observation_key="expiring-cross-profile-observation",
            decision_id="waiting-observation-claim",
            direction="LONG",
            strategy_tag="long_waiting_observe",
            threshold_segment="WD-15",
        )
        rows[40] = replace(
            rows[40],
            observation_key="expiring-decision-owner",
            decision_id="expiring-cross-tag-decision",
        )
        rows[50] = replace(
            rows[50],
            observation_key="waiting-decision-claim",
            decision_id="expiring-cross-tag-decision",
            direction="LONG",
            strategy_tag="long_decision_waiting_observe",
            threshold_segment="WD-15",
        )

        ordered = sorted(rows, key=replay_module._settlement_event_key)
        workload = {}
        actual_rows = replay_module._build_adaptive_event_rows(
            ordered,
            workload=workload,
        )
        lookback_ms = 15 * 86_400_000
        left = 0
        for index, (event, actual) in enumerate(zip(ordered, actual_rows)):
            evaluated_at = int(event.settled_at) + 1
            while (
                left <= index
                and int(ordered[left].settled_at) < evaluated_at - lookback_ms
            ):
                left += 1
            expected_states = rebuild_adaptive_profile_states(
                ordered[left : index + 1],
                evaluated_at,
            )
            key = replay_module._observation_profile_key(event)
            expected = expected_states.get(key)
            if expected is None:
                expected = replay_module._adaptive_state((), key, evaluated_at)
            with self.subTest(index=index, profile=key):
                self.assertEqual(actual["adaptive_state_after"], expected["status"])
                self.assertEqual(actual["n12_after"], expected["n12"])
                self.assertEqual(actual["n20_after"], expected["n20"])
        self.assertEqual(workload["adaptive_window_rebuilds"], 2)
        self.assertLessEqual(workload["adaptive_window_rebuild_input_rows"], 400)

    def test_adaptive_global_owner_expiry_prefers_earlier_conflict_over_same_binding(self):
        start = timestamp("2026-01-01T00:00:00")
        owner = replace(
            observation("owner", "WIN", start),
            observation_key="reviewer-shared-observation",
            decision_id="reviewer-owner-decision",
        )
        earlier_conflict = replace(
            observation(
                "earlier-conflict",
                "LOSS",
                start + 86_400_000,
                direction="LONG",
                tag="reviewer_long_conflict",
            ),
            observation_key="reviewer-shared-observation",
            decision_id="reviewer-conflict-decision",
            threshold_segment="WD-15",
        )
        later_same_binding = replace(
            observation("later-same-binding", "LOSS", start + 2 * 86_400_000),
            observation_key="reviewer-shared-observation",
            decision_id="reviewer-owner-decision",
        )
        rows = [
            owner,
            earlier_conflict,
            later_same_binding,
            observation("filler-short", "WIN", start + 3 * 86_400_000),
            observation(
                "filler-long",
                "WIN",
                start + 4 * 86_400_000,
                direction="LONG",
                tag="reviewer_long_conflict",
            ),
            replace(
                observation(
                    "conflict-follow-up",
                    "WIN",
                    start + 15 * 86_400_000 + 60 * 60_000,
                    direction="LONG",
                    tag="reviewer_long_conflict",
                ),
                observation_key="reviewer-shared-observation",
                decision_id="reviewer-conflict-decision",
                threshold_segment="WD-15",
            ),
            observation(
                "conflict-profile-unique",
                "WIN",
                start + 15 * 86_400_000 + 2 * 60 * 60_000,
                direction="LONG",
                tag="reviewer_long_conflict",
            ),
        ]
        ordered = sorted(rows, key=replay_module.adaptive_replay_event_sort_key)
        tracker = AdaptiveGlobalProfileWindowReplay(
            lookback_ms=15 * 86_400_000,
        )
        prefix = []
        for index, row in enumerate(ordered):
            prefix.append(row)
            evaluated_at = int(row.settled_at) + 1
            window = [
                event
                for event in prefix
                if evaluated_at - 15 * 86_400_000
                <= int(event.settled_at)
                < evaluated_at
            ]
            key = replay_module._observation_profile_key(row)
            expected = rebuild_adaptive_profile_states(window, evaluated_at).get(key)
            if expected is None:
                expected = replay_module._adaptive_state((), key, evaluated_at)
            actual = tracker.advance(row, evaluated_at)
            with self.subTest(index=index):
                self.assertEqual(actual, expected)
                self.assertEqual(
                    tracker._accepted_profile_counts,
                    Counter(
                        entry.profile_key
                        for entry in tracker._events
                        if entry.accepted
                    ),
                )
        self.assertGreater(
            tracker.workload_report()["global_identity_rebuilds"],
            0,
        )

    def test_adaptive_global_retires_one_time_profile_trackers_with_window(self):
        lookback_ms = 100 * 60_000

        def workload_for(count):
            start = timestamp("2026-01-01T00:00:00")
            rows = [
                observation(
                    f"one-time-profile-{index:04d}",
                    "WIN" if index % 3 else "LOSS",
                    start + index * 60_000,
                    tag=f"one_time_short_observe_{index:04d}",
                )
                for index in range(count)
            ]
            tracker = AdaptiveGlobalProfileWindowReplay(
                lookback_ms=lookback_ms,
            )
            checkpoints = {99, 100, count // 2, count - 1}
            for index, row in enumerate(rows):
                evaluated_at = int(row.settled_at) + 1
                actual = tracker.advance(row, evaluated_at)
                active_limit = min(index + 1, 100)
                retained_events = sum(
                    len(profile_tracker._events)
                    for profile_tracker in tracker._trackers.values()
                )
                self.assertLessEqual(len(tracker._trackers), active_limit)
                self.assertLessEqual(retained_events, active_limit)
                if index not in checkpoints:
                    continue
                window = [
                    event
                    for event in rows[: index + 1]
                    if evaluated_at - lookback_ms
                    <= int(event.settled_at)
                    < evaluated_at
                ]
                key = replay_module._observation_profile_key(row)
                expected = rebuild_adaptive_profile_states(window, evaluated_at)[key]
                with self.subTest(count=count, index=index):
                    self.assertEqual(actual, expected)
            workload = tracker.workload_report()
            workload["actual_tracker_count"] = len(tracker._trackers)
            workload["actual_tracker_events"] = sum(
                len(profile_tracker._events)
                for profile_tracker in tracker._trackers.values()
            )
            return workload

        small = workload_for(2_000)
        large = workload_for(4_000)

        for workload in (small, large):
            self.assertEqual(workload["active_profile_trackers"], 100)
            self.assertEqual(workload["actual_tracker_count"], 100)
            self.assertEqual(workload["retained_profile_tracker_events"], 100)
            self.assertEqual(workload["actual_tracker_events"], 100)
            self.assertLessEqual(
                workload["retained_profile_tracker_events"],
                workload["retained_profile_tracker_event_bound"],
            )
            self.assertLessEqual(
                workload["retained_profile_tracker_event_bound"],
                workload["max_global_window_events"],
            )
        self.assertEqual(
            large["active_profile_trackers"],
            small["active_profile_trackers"],
        )
        self.assertEqual(
            large["retained_profile_tracker_events"],
            small["retained_profile_tracker_events"],
        )

    def test_adaptive_global_random_multi_profile_windows_match_full_rebuild(self):
        rng = random.Random(20260820)
        start = timestamp("2026-01-01T00:00:00")
        rows = []
        for index in range(360):
            direction = "LONG" if index % 3 == 0 else "SHORT"
            row = replace(
                observation(
                    f"random-global-{index:03d}",
                    "WIN" if rng.random() >= 0.42 else "LOSS",
                    start + index * 90 * 60_000,
                    direction=direction,
                    tag=(
                        "random_long_observe"
                        if direction == "LONG"
                        else "random_short_observe"
                    ),
                ),
                threshold_segment="WD-15" if direction == "LONG" else "WE-04",
            )
            if index >= 20:
                mode = rng.randrange(12)
                source = rows[index - rng.randint(5, 20)]
                if mode == 0:
                    row = replace(
                        row,
                        observation_key=source.observation_key,
                        decision_id=source.decision_id,
                        strategy_family=source.strategy_family,
                        strategy_tag=source.strategy_tag,
                        direction=source.direction,
                        threshold_segment=source.threshold_segment,
                    )
                elif mode == 1:
                    row = replace(row, observation_key=source.observation_key)
                elif mode == 2:
                    row = replace(row, decision_id=source.decision_id)
            rows.append(row)

        ordered = sorted(rows, key=replay_module.adaptive_replay_event_sort_key)
        tracker = AdaptiveGlobalProfileWindowReplay(
            lookback_ms=15 * 86_400_000,
        )
        left = 0
        for index, row in enumerate(ordered):
            evaluated_at = int(row.settled_at) + 1
            while (
                left <= index
                and int(ordered[left].settled_at)
                < evaluated_at - 15 * 86_400_000
            ):
                left += 1
            key = replay_module._observation_profile_key(row)
            expected = rebuild_adaptive_profile_states(
                ordered[left : index + 1],
                evaluated_at,
            ).get(key)
            if expected is None:
                expected = replay_module._adaptive_state((), key, evaluated_at)
            actual = tracker.advance(row, evaluated_at)
            with self.subTest(index=index):
                self.assertEqual(actual, expected)
                self.assertEqual(
                    tracker._accepted_profile_counts,
                    Counter(
                        entry.profile_key
                        for entry in tracker._events
                        if entry.accepted
                    ),
                )

    def test_adaptive_global_window_skips_rebuild_when_all_claims_expire_together(self):
        start = timestamp("2026-01-01T00:00:00")
        owner = replace(
            observation("expiring-owner", "WIN", start),
            observation_key="joint-expiry-identity",
            decision_id="joint-expiry-owner",
        )
        conflict = replace(
            observation("expiring-conflict", "LOSS", start + 60_000),
            observation_key="joint-expiry-identity",
            decision_id="joint-expiry-conflict",
            direction="LONG",
            strategy_tag="long_joint_expiry_observe",
            threshold_segment="WD-15",
        )
        later = observation(
            "after-joint-expiry",
            "WIN",
            start + 16 * 86_400_000,
        )
        tracker = AdaptiveGlobalProfileWindowReplay(
            lookback_ms=15 * 86_400_000,
        )
        for row in sorted(
            [owner, conflict, later],
            key=replay_module.adaptive_replay_event_sort_key,
        ):
            tracker.advance(row, int(row.settled_at) + 1)

        self.assertEqual(tracker.workload_report()["global_identity_rebuilds"], 0)

    def test_adaptive_public_window_replay_rejects_event_before_window(self):
        row = observation(
            "outside-window",
            "WIN",
            timestamp("2026-07-01T00:00:00"),
        )
        lookback_ms = 15 * 86_400_000
        tracker = AdaptiveProfileWindowReplay(
            replay_module._observation_profile_key(row),
            lookback_ms=lookback_ms,
        )

        with self.assertRaisesRegex(ValueError, "outside the adaptive lookback window"):
            tracker.advance(row, int(row.settled_at) + lookback_ms + 2)

    def test_adaptive_advance_rejects_cutoff_before_last_state_evaluation(self):
        start = timestamp("2026-07-01T00:00:00")
        first = observation("cutoff-first", "WIN", start)
        second = observation("cutoff-second", "WIN", start + 11 * 60_000)
        tracker = AdaptiveProfileWindowReplay(
            replay_module._observation_profile_key(first),
            lookback_ms=15 * 86_400_000,
        )
        tracker.advance(first, int(first.settled_at) + 1)
        tracker.state_at(int(second.settled_at) + 60_000)

        with self.assertRaisesRegex(ValueError, "evaluated_at must advance monotonically"):
            tracker.advance(second, int(second.settled_at) + 1)

    def test_adaptive_duplicate_identity_work_is_near_linear_across_window(self):
        def workload_for(count):
            start = timestamp("2026-01-01T00:00:00")
            rows = [
                observation(
                    f"duplicate-owner-{index // 2:04d}",
                    "WIN" if index % 3 else "LOSS",
                    start + index * 11 * 60_000,
                )
                for index in range(count)
            ]
            tracker = AdaptiveProfileWindowReplay(
                replay_module._observation_profile_key(rows[0]),
                lookback_ms=15 * 86_400_000,
            )
            for row in rows:
                tracker.advance(row, int(row.settled_at) + 1)
            return tracker.workload

        small = workload_for(2_000)
        self.assertLessEqual(small["window_rebuild_input_rows"], 2_000 * 4)
        large = workload_for(4_000)
        self.assertLessEqual(
            large["window_rebuild_input_rows"],
            max(1, small["window_rebuild_input_rows"]) * 2.5,
        )
        self.assertLessEqual(
            large["dynamic_work_units"],
            4_000 * ((4_000).bit_length() + 4),
        )

    def test_adaptive_global_duplicate_identity_transfers_owner_without_rebuild(self):
        def workload_for(count):
            start = timestamp("2026-01-01T00:00:00")
            rows = [
                observation(
                    f"global-duplicate-owner-{index // 2:04d}",
                    "WIN" if index % 3 else "LOSS",
                    start + index * 11 * 60_000,
                )
                for index in range(count)
            ]
            tracker = AdaptiveGlobalProfileWindowReplay(
                lookback_ms=15 * 86_400_000,
            )
            for row in rows:
                tracker.advance(row, int(row.settled_at) + 1)
            self.assertEqual(
                tracker._accepted_profile_counts,
                Counter(
                    entry.profile_key
                    for entry in tracker._events
                    if entry.accepted
                ),
            )
            return tracker.workload_report()

        small = workload_for(2_000)
        large = workload_for(4_000)

        self.assertEqual(small["global_identity_rebuild_input_rows"], 0)
        self.assertEqual(large["global_identity_rebuild_input_rows"], 0)
        self.assertGreater(large["global_identity_fast_transfers"], 0)
        self.assertLessEqual(
            large["global_identity_claim_index_entries"],
            large["max_global_window_events"] * 2,
        )

    def test_adaptive_descending_opened_pair_claim_heaps_are_geometrically_bounded(self):
        lookback_ms = 100 * 60_000

        def workload_for(count):
            start = timestamp("2026-01-01T00:00:00")
            rows = [
                observation(
                    "descending-opened-shared-pair",
                    "WIN" if index % 3 else "LOSS",
                    start - index * 60_000,
                    settled_at=start + (index + 20) * 60_000,
                )
                for index in range(count)
            ]
            key = replay_module._observation_profile_key(rows[0])
            tracker = AdaptiveProfileWindowReplay(key, lookback_ms=lookback_ms)
            checkpoints = {99, 100, count // 2, count - 1}
            for index, row in enumerate(rows):
                evaluated_at = int(row.settled_at) + 1
                actual = tracker.advance(row, evaluated_at)
                actual_heap_entries = sum(
                    len(heap) for heap in tracker._pair_claim_heaps.values()
                )
                self.assertEqual(
                    tracker.workload["dynamic_claim_heap_entries"],
                    actual_heap_entries,
                )
                self.assertLessEqual(
                    actual_heap_entries,
                    tracker.workload["dynamic_claim_heap_entry_bound"],
                    f"heap bound exceeded at event {index}",
                )
                if index not in checkpoints:
                    continue
                window = [
                    event
                    for event in rows[: index + 1]
                    if evaluated_at - lookback_ms
                    <= int(event.settled_at)
                    < evaluated_at
                ]
                expected = rebuild_adaptive_profile_states(window, evaluated_at)[key]
                with self.subTest(count=count, index=index):
                    self.assertEqual(actual, expected)
            return dict(tracker.workload)

        small = workload_for(2_000)
        large = workload_for(4_000)

        for count, workload in ((2_000, small), (4_000, large)):
            with self.subTest(count=count):
                self.assertEqual(workload["max_window_events"], 100)
                self.assertLessEqual(
                    workload["dynamic_claim_heap_entries"],
                    workload["dynamic_claim_heap_entry_bound"],
                )
                self.assertLessEqual(
                    workload["dynamic_claim_heap_entry_bound"],
                    workload["max_window_events"] * 2,
                )
                self.assertGreater(
                    workload["dynamic_claim_heap_compactions"],
                    0,
                )
                self.assertLessEqual(
                    workload["dynamic_claim_heap_compaction_input_rows"],
                    count * 2,
                )
                self.assertGreaterEqual(
                    workload["dynamic_work_units"],
                    workload["dynamic_claim_heap_compaction_input_rows"],
                )
        self.assertLessEqual(
            large["dynamic_claim_heap_compaction_input_rows"],
            small["dynamic_claim_heap_compaction_input_rows"] * 2.5,
        )
        self.assertLessEqual(
            large["window_rebuild_input_rows"],
            small["window_rebuild_input_rows"] * 2.2,
        )

    def test_adaptive_single_opened_inversion_recovers_without_window_rebuilds(self):
        def workload_for(count):
            start = timestamp("2026-01-01T00:00:00")
            rows = [
                observation(
                    f"inversion-{index:04d}",
                    "WIN" if index % 3 else "LOSS",
                    start + index * 11 * 60_000,
                )
                for index in range(count)
            ]
            inverted_opened_at = rows[99].opened_at - 60_000
            rows[100] = replace(
                rows[100],
                opened_at=inverted_opened_at,
                expires_at=inverted_opened_at + 10 * 60_000,
            )
            tracker = AdaptiveProfileWindowReplay(
                replay_module._observation_profile_key(rows[0]),
                lookback_ms=15 * 86_400_000,
            )
            for row in rows:
                tracker.advance(row, int(row.settled_at) + 1)
            return tracker.workload

        small = workload_for(2_000)
        self.assertLessEqual(small["window_rebuild_input_rows"], 2_000 * 4)
        large = workload_for(4_000)
        self.assertLessEqual(
            large["window_rebuild_input_rows"],
            max(1, small["window_rebuild_input_rows"]) * 2.5,
        )
        self.assertLessEqual(
            large["dynamic_work_units"],
            4_000 * ((4_000).bit_length() + 4),
        )

    def test_adaptive_duplicate_and_inversion_paths_match_full_window_rebuild(self):
        start = timestamp("2026-01-01T00:00:00")
        duplicate_rows = [
            observation(
                f"differential-duplicate-{index // 2:04d}",
                "WIN" if index % 4 else "LOSS",
                start + index * 11 * 60_000,
            )
            for index in range(2_200)
        ]
        inversion_rows = [
            observation(
                f"differential-inversion-{index:04d}",
                "WIN" if index % 3 else "LOSS",
                start + index * 11 * 60_000,
            )
            for index in range(2_200)
        ]
        inverted_opened_at = inversion_rows[99].opened_at - 60_000
        inversion_rows[100] = replace(
            inversion_rows[100],
            opened_at=inverted_opened_at,
            expires_at=inverted_opened_at + 10 * 60_000,
        )
        checkpoints = {1_963, 1_964, 2_000, 2_199}
        lookback_ms = 15 * 86_400_000

        for name, rows in (
            ("duplicate", duplicate_rows),
            ("inversion", inversion_rows),
        ):
            key = replay_module._observation_profile_key(rows[0])
            tracker = AdaptiveProfileWindowReplay(key, lookback_ms=lookback_ms)
            for index, row in enumerate(rows):
                evaluated_at = int(row.settled_at) + 1
                actual = tracker.advance(row, evaluated_at)
                if index not in checkpoints:
                    continue
                window = [
                    event
                    for event in rows[: index + 1]
                    if evaluated_at - lookback_ms
                    <= int(event.settled_at)
                    < evaluated_at
                ]
                expected = rebuild_adaptive_profile_states(window, evaluated_at)[key]
                with self.subTest(path=name, index=index):
                    self.assertEqual(actual, expected)

    def test_adaptive_window_work_is_near_linear_across_fifteen_day_boundary(self):
        def workload_for(count, spacing_minutes):
            start = timestamp("2026-01-01T00:00:00")
            rows = [
                observation(
                    f"work-{spacing_minutes}-{index:04d}",
                    "WIN" if index % 3 else "LOSS",
                    start + index * spacing_minutes * 60_000,
                )
                for index in range(count)
            ]
            key = replay_module._observation_profile_key(rows[0])
            tracker = AdaptiveProfileWindowReplay(
                key,
                lookback_ms=15 * 86_400_000,
            )
            for row in rows:
                tracker.advance(row, int(row.settled_at) + 1)
            return tracker.workload

        non_overlapping_small = workload_for(2_000, 11)
        self.assertLessEqual(
            non_overlapping_small["window_rebuild_input_rows"],
            2_000 * 4,
        )
        non_overlapping_large = workload_for(4_000, 11)
        self.assertEqual(non_overlapping_large["window_rebuild_input_rows"], 0)
        self.assertLessEqual(
            non_overlapping_large["retained_index_events"],
            non_overlapping_large["max_window_events"] * 2,
        )
        self.assertLessEqual(
            non_overlapping_large["jump_cache_entries"],
            non_overlapping_large["retained_index_events"]
            * (non_overlapping_large["retained_index_events"].bit_length() + 1),
        )
        self.assertLessEqual(
            non_overlapping_large["bounded_work_units"],
            non_overlapping_small["bounded_work_units"] * 2.3,
        )

        overlapping_small = workload_for(3_000, 8)
        overlapping_large = workload_for(6_000, 8)
        self.assertLessEqual(
            overlapping_small["window_rebuild_input_rows"],
            3_000 * 4,
        )
        self.assertEqual(overlapping_large["window_rebuild_input_rows"], 0)
        self.assertLessEqual(
            overlapping_large["retained_index_events"],
            overlapping_large["max_window_events"] * 2,
        )
        self.assertLessEqual(
            overlapping_large["bounded_work_units"],
            overlapping_small["bounded_work_units"] * 2.3,
        )

    def test_adaptive_overlapping_fast_path_matches_rebuild_after_expiry(self):
        start = timestamp("2026-01-01T00:00:00")
        rows = [
            observation(
                f"overlap-expiry-{index:04d}",
                "WIN" if index % 5 not in {0, 1} else "LOSS",
                start + index * 6 * 60_000,
            )
            for index in range(3_800)
        ]
        key = replay_module._observation_profile_key(rows[0])
        lookback_ms = 15 * 86_400_000
        tracker = AdaptiveProfileWindowReplay(key, lookback_ms=lookback_ms)
        checkpoints = {3_599, 3_600, 3_650, 3_799}
        for index, row in enumerate(rows):
            evaluated_at = int(row.settled_at) + 1
            actual = tracker.advance(row, evaluated_at)
            if index not in checkpoints:
                continue
            window = [
                event
                for event in rows[: index + 1]
                if evaluated_at - lookback_ms <= int(event.settled_at) < evaluated_at
            ]
            expected = rebuild_adaptive_profile_states(window, evaluated_at)[key]
            with self.subTest(index=index):
                self.assertEqual(actual, expected)

        self.assertEqual(tracker.workload["window_rebuild_input_rows"], 0)

    def test_adaptive_window_rebuild_work_scales_with_horizon_not_total_history(self):
        def workload_for(days):
            start = timestamp("2026-01-01T00:00:00")
            rows = [
                observation(
                    f"horizon-{days}-{index:04d}",
                    "WIN" if index % 2 else "LOSS",
                    start + index * 3 * 60 * 60_000,
                )
                for index in range(days * 8)
            ]
            workload = {}
            replay_module._build_adaptive_event_rows(rows, workload=workload)
            return workload

        short = workload_for(90)
        long = workload_for(180)

        self.assertLessEqual(
            long["adaptive_max_window_events"],
            short["adaptive_max_window_events"] + 1,
        )
        self.assertEqual(
            long["adaptive_incremental_adds"],
            short["adaptive_incremental_adds"] * 2,
        )
        self.assertLessEqual(
            long["adaptive_window_rebuild_input_rows"],
            short["adaptive_window_rebuild_input_rows"] * 2.25,
        )

    def test_adaptive_public_window_replay_matches_full_rebuild_after_expiry(self):
        start = timestamp("2026-07-01T00:00:00")
        rows = []
        for index in range(275):
            row = observation(
                f"expiry-{index:03d}",
                "WIN" if index % 3 else "LOSS",
                start + index * 90 * 60_000,
            )
            if index >= 180 and index % 29 == 0:
                row = replace(
                    row,
                    observation_key=rows[index - 170].observation_key,
                    decision_id=f"expiry-conflict-{index}",
                )
            rows.append(row)

        key = replay_module._observation_profile_key(rows[0])
        lookback_ms = 15 * 86_400_000
        tracker = AdaptiveProfileWindowReplay(key, lookback_ms=lookback_ms)
        prefix = []
        for index, row in enumerate(rows):
            prefix.append(row)
            evaluated_at = int(row.settled_at) + 1
            window = [
                event
                for event in prefix
                if evaluated_at - lookback_ms <= int(event.settled_at) < evaluated_at
            ]
            expected = rebuild_adaptive_profile_states(window, evaluated_at)[key]
            actual = tracker.advance(row, evaluated_at)
            with self.subTest(index=index):
                self.assertEqual(actual, expected)

        self.assertGreater(tracker.workload["window_rebuilds"], 0)
        self.assertGreater(tracker.workload["window_rebuild_input_rows"], 0)

    def test_schedule_passes_only_bounded_lookback_rows_to_selector(self):
        start = timestamp("2026-06-01T01:00:00")
        rows = [
            observation(f"day-{index}", "WIN", start + index * 86_400_000)
            for index in range(45)
        ]
        config = selector_config()
        input_sizes = []
        original = replay_module.build_daily_selection

        def recording_selection(observations, *args, **kwargs):
            input_sizes.append(len(observations))
            return original(observations, *args, **kwargs)

        with patch.object(
            replay_module,
            "build_daily_selection",
            side_effect=recording_selection,
        ):
            replay_module._build_schedule(
                sorted(rows, key=replay_module._settlement_event_key),
                config,
                require_full_lookback=False,
            )

        self.assertGreater(len(input_sizes), 40)
        self.assertLessEqual(max(input_sizes), 2)

    def test_leakage_check_builds_each_sample_key_index_once(self):
        class CountingKeys:
            def __init__(self, values):
                self.values = values
                self.iterations = 0

            def __iter__(self):
                self.iterations += 1
                return iter(self.values)

        rows = [observation(f"leak-{index}", "WIN", index * 1_000_000) for index in range(20)]
        keys = CountingKeys([item.observation_key for item in rows])
        snapshots = [{"sample_keys": keys, "lookback_end": 100_000_000}]

        self.assertEqual(replay_module._count_leakage_violations(rows, snapshots), 0)
        self.assertEqual(keys.iterations, 1)

    def test_replay_prepares_execution_windows_once_for_all_three_runs(self):
        rows = [
            observation("plan-train", "WIN", timestamp("2026-07-20T01:00:00")),
            observation("plan-one", "WIN", timestamp("2026-07-20T08:00:00")),
            observation("plan-two", "LOSS", timestamp("2026-07-20T08:12:00")),
        ]
        self.assertTrue(hasattr(replay_module, "_prepare_execution_windows"))
        original = replay_module._prepare_execution_windows
        with patch.object(
            replay_module,
            "_prepare_execution_windows",
            wraps=original,
        ) as prepare:
            report = self.replay(rows)

        self.assertEqual(prepare.call_count, 1)
        self.assertLessEqual(report["workload"]["execution_plan_rows"], len(rows))
        self.assertEqual(
            report["workload"]["execution_replay_rows"],
            report["workload"]["execution_plan_rows"] * 3,
        )

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

    def test_three_loss_pause_uses_settlement_ledger_without_rescanning_trades(self):
        rows = [
            observation("loss-ledger-train", "WIN", timestamp("2026-07-20T01:00:00")),
            observation("loss-ledger-1", "LOSS", timestamp("2026-07-20T08:00:00")),
            observation("loss-ledger-2", "LOSS", timestamp("2026-07-20T08:11:00")),
            observation("loss-ledger-3", "LOSS", timestamp("2026-07-20T08:22:00")),
            observation("loss-ledger-blocked", "WIN", timestamp("2026-07-20T08:33:00")),
        ]

        report = self.replay(rows)
        baseline = report["baseline"]

        self.assertEqual(baseline["guard_rejections"]["three_loss_pause"], 1)
        self.assertNotIn(
            "loss-ledger-blocked",
            {item["observation_key"] for item in baseline["trade_rows"]},
        )
        self.assertFalse(hasattr(replay_module, "_has_three_segment_losses"))

    def test_high_precision_progression_matches_account_simulator(self):
        stake = 10.12345
        win_return = 18.98765
        signal = Signal(
            direction="SHORT",
            timeframe_minutes=10,
            level="B",
            reason="high precision",
            price=100.0,
            open_time=0,
        )
        simulator = AccountSimulator(
            stake=stake,
            win_return=win_return,
            enable_stake_progression=True,
            stake_progression_max_active=1,
            max_open_orders=2,
        )
        first = simulator.open_order(signal, 100.0, 0)
        simulator.settle_expired_orders(first.expires_at, 99.0)
        expected_second = simulator.open_order(signal, 99.0, first.expires_at)
        rows = [
            observation("precision-train", "WIN", timestamp("2026-07-20T01:00:00")),
            observation("precision-first", "WIN", timestamp("2026-07-20T08:00:00")),
            observation("precision-second", "LOSS", timestamp("2026-07-20T08:10:00")),
        ]

        report = self.replay(
            rows,
            stake=stake,
            win_return=win_return,
            stake_progression_second_stake=win_return,
        )

        actual_second = next(
            item
            for item in report["baseline"]["trade_rows"]
            if item["observation_key"] == "precision-second"
        )
        self.assertEqual(report["execution"]["stake"], stake)
        self.assertEqual(report["execution"]["win_return"], win_return)
        self.assertEqual(expected_second.win_return, 35.6134)
        self.assertEqual(actual_second["stake"], expected_second.stake)
        self.assertEqual(actual_second["win_return"], expected_second.win_return)

    def test_disabled_progression_still_uses_ledger_base_terms(self):
        stake = 10.12345
        win_return = 18.98765
        signal = Signal(
            direction="SHORT",
            timeframe_minutes=10,
            level="B",
            reason="disabled high precision",
            price=100.0,
            open_time=0,
        )
        simulator = AccountSimulator(
            stake=stake,
            win_return=win_return,
            enable_stake_progression=False,
            max_open_orders=2,
        )
        expected_first = simulator.open_order(signal, 100.0, 0)
        simulator.settle_expired_orders(expected_first.expires_at, 99.0)
        expected_second = simulator.open_order(
            signal,
            99.0,
            expected_first.expires_at,
        )
        rows = [
            observation("disabled-train", "WIN", timestamp("2026-07-20T01:00:00")),
            observation("disabled-first", "WIN", timestamp("2026-07-20T08:00:00")),
            observation("disabled-second", "LOSS", timestamp("2026-07-20T08:10:00")),
        ]

        report = self.replay(
            rows,
            stake=stake,
            win_return=win_return,
            stake_progression_enabled=False,
            stake_progression_second_stake=win_return,
        )

        actual = {
            item["observation_key"]: item
            for item in report["baseline"]["trade_rows"]
        }
        actual_first = actual["disabled-first"]
        actual_second = actual["disabled-second"]
        self.assertEqual(
            (actual_first["stake"], actual_first["win_return"], actual_first["pnl"]),
            (expected_first.stake, expected_first.win_return, expected_first.pnl),
        )
        self.assertEqual(
            (actual_second["stake"], actual_second["win_return"]),
            (expected_second.stake, expected_second.win_return),
        )
        self.assertTrue(actual_first["progression_allowed"])
        self.assertTrue(actual_second["progression_allowed"])
        self.assertEqual(actual_first["progression_version"], "")
        self.assertEqual(actual_second["progression_version"], "")

    def test_watch_bypasses_ledger_and_keeps_original_base_terms(self):
        stake = 10.12345
        win_return = 18.98765
        row = observation("watch-precision", "WIN", 1_000)
        profile = replay_module._observation_profile_key(row)
        watch = replay_module._adaptive_state((), profile, row.opened_at)
        watch["status"] = "WATCH"
        timeline = {
            "by_profile": {profile: [(row.opened_at - 1, watch)]},
            "settled_times": [row.opened_at - 1],
        }
        snapshot = {
            "effective_from": 0,
            "effective_until": row.expires_at + 1,
            "selected_profiles": [
                {
                    "key": profile,
                    "sample_size": 1,
                    "win_rate": 1.0,
                    "ev": 8.0,
                }
            ],
        }
        execution = self.production_execution(
            stake=stake,
            win_return=win_return,
            stake_progression_second_stake=win_return,
        ).normalized()
        simulator = AccountSimulator(
            stake=stake,
            win_return=win_return,
            enable_stake_progression=True,
            max_open_orders=2,
        )
        signal = Signal(
            direction="SHORT",
            timeframe_minutes=10,
            level="B",
            reason="watch precision",
            price=100.0,
            open_time=0,
        )
        expected, _credit = simulator.open_order_with_credit(
            signal,
            100.0,
            row.opened_at,
            allow_progression=False,
        )

        result = replay_module._execute_replay(
            replay_module._prepare_execution_windows([row], [snapshot]),
            execution,
            timeline,
            apply_adaptive=True,
            include_structure_shadow=False,
        )

        actual = result["trade_rows"][0]
        self.assertFalse(actual["progression_allowed"])
        self.assertEqual(actual["progression_version"], "TWO_STAGE_V1")
        self.assertEqual(actual["stake"], expected.stake)
        self.assertEqual(actual["win_return"], expected.win_return)

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
            "profile_admission_search",
            "aggregate_gates_passed",
            "stability_proven",
            "release_allowed",
            "equivalence_scope",
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
        self.assertEqual(report["profile_admission_search"]["evaluated_count"], 32)
        self.assertEqual(
            report["aggregate_gates_passed"],
            report["profile_admission_search"]["aggregate_gates_passed"],
        )
        self.assertEqual(
            report["equivalence_scope"]["scope"],
            "PROFILE_ADMISSION_LAYER_ONLY",
        )

    def test_report_removes_duplicate_sections_and_compacts_event_audit(self):
        start = timestamp("2026-07-19T20:00:00")
        rows = [
            observation(
                f"compact-{index}",
                "WIN" if index % 3 else "LOSS",
                start + index * 11 * 60_000,
            )
            for index in range(100)
        ]

        report = self.replay(rows)

        self.assertNotIn("daily_snapshots", report)
        self.assertNotIn("trade_rows", report)
        self.assertIn("trade_rows", report["candidate"])
        self.assertIn("report_schema_version", report)
        for event in report["events"]:
            self.assertNotIn("n12_before", event)
            self.assertNotIn("n20_before", event)
            self.assertIn("n12_after", event)
            self.assertIn("n20_after", event)

        compact = json.loads(json.dumps(report))
        compact.pop("profile_admission_search")
        legacy = json.loads(json.dumps(compact))
        legacy["daily_snapshots"] = legacy["schedule"]
        legacy["trade_rows"] = legacy["candidate"]["trade_rows"]
        for event in legacy["events"]:
            event["n12_before"] = event["n12_after"]
            event["n20_before"] = event["n20_after"]
        compact_size = len(json.dumps(compact, separators=(",", ":")))
        legacy_size = len(json.dumps(legacy, separators=(",", ":")))
        self.assertLess(compact_size, legacy_size * 0.8)

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
            "oos_windows": [
                {"pnl": 1.0, "ev": 1.0},
                {"pnl": 2.0, "ev": 2.0},
                {"pnl": -1.0, "ev": -1.0},
            ],
        }
        candidate = {
            "total": {**summary, "orders": 9, "max_drawdown": 9.0},
            "by_direction": {
                "LONG": {**summary, "orders": 8},
                "SHORT": {**summary, "orders": 7},
            },
            "base_first_orders": 9,
            "oos_windows": [
                {"pnl": 1.0, "ev": 1.0},
                {"pnl": 2.0, "ev": 2.0},
                {"pnl": -1.0, "ev": -1.0},
            ],
        }

        accepted = replay_module.evaluate_release_gates(baseline, candidate)
        self.assertTrue(accepted["passed"])
        self.assertTrue(accepted["gates"]["total_ev"]["passed"])
        self.assertTrue(accepted["gates"]["long_ev"]["passed"])
        self.assertTrue(accepted["gates"]["short_ev"]["passed"])
        self.assertTrue(accepted["gates"]["maximum_drawdown_not_worse"]["passed"])
        self.assertTrue(accepted["gates"]["longest_loss_streak_not_worse"]["passed"])

        candidate["by_direction"]["SHORT"]["pnl"] = -0.01
        candidate["by_direction"]["SHORT"]["ev"] = -0.0
        candidate["total"]["max_drawdown"] = 10.01
        rejected = replay_module.evaluate_release_gates(baseline, candidate)
        self.assertFalse(rejected["passed"])
        self.assertFalse(rejected["gates"]["short_ev"]["passed"])
        self.assertFalse(rejected["gates"]["maximum_drawdown_not_worse"]["passed"])

    def test_release_gates_reject_tiny_negative_raw_ev_and_oos_pnl(self):
        trades = []
        for direction_index, direction in enumerate(("LONG", "SHORT")):
            for trade_index, (result, pnl) in enumerate(
                (
                    ("WIN", 1.0),
                    ("WIN", 1.0),
                    ("WIN", 1.0),
                    ("LOSS", -1.5),
                    ("LOSS", -1.5001),
                )
            ):
                opened_at = direction_index * 10 + trade_index
                trades.append(
                    {
                        "observation_key": f"{direction}-{trade_index}",
                        "order_id": len(trades) + 1,
                        "direction": direction,
                        "result": result,
                        "pnl": pnl,
                        "opened_at": opened_at,
                        "settled_at": opened_at + 1,
                    }
                )
        candidate_total = replay_module.summarize_trades(trades)
        candidate_directions = {
            direction: replay_module.summarize_trades(
                [item for item in trades if item["direction"] == direction]
            )
            for direction in ("LONG", "SHORT")
        }
        baseline_total = {**candidate_total, "pnl": 1.0, "ev": 0.1}
        baseline_directions = {
            direction: {**summary, "pnl": 0.5, "ev": 0.1}
            for direction, summary in candidate_directions.items()
        }
        baseline = {
            "total": baseline_total,
            "by_direction": baseline_directions,
            "base_first_orders": 10,
            "oos_windows": [],
        }
        candidate = {
            "total": candidate_total,
            "by_direction": candidate_directions,
            "base_first_orders": 10,
            "oos_windows": [
                {
                    "start_at": 0,
                    "end_at": 100,
                    **replay_module.summarize_trades(trades),
                },
                {
                    "start_at": 100,
                    "end_at": 200,
                    **replay_module.summarize_trades([]),
                },
                {
                    "start_at": 200,
                    "end_at": 300,
                    **replay_module.summarize_trades([]),
                },
            ],
            "trade_rows": trades,
        }

        result = replay_module.evaluate_release_gates(baseline, candidate)

        self.assertEqual(candidate["total"]["ev"], -0.0)
        self.assertEqual(candidate["by_direction"]["LONG"]["pnl"], -0.0001)
        self.assertEqual(candidate["by_direction"]["SHORT"]["pnl"], -0.0001)
        self.assertEqual(candidate["by_direction"]["LONG"]["ev"], -0.0)
        self.assertEqual(candidate["by_direction"]["SHORT"]["ev"], -0.0)
        self.assertFalse(result["gates"]["total_ev"]["passed"])
        self.assertFalse(result["gates"]["long_ev"]["passed"])
        self.assertFalse(result["gates"]["short_ev"]["passed"])
        self.assertEqual(candidate["oos_windows"][0]["ev"], -0.0)
        self.assertEqual(result["gates"]["positive_oos_windows"]["actual"], 0.0)
        self.assertFalse(result["gates"]["positive_oos_windows"]["passed"])

    def test_release_gates_reject_unrounded_direction_win_rate_below_threshold(self):
        direction_orders = 2509
        direction_wins = 1394
        rounded_rate = round(direction_wins / direction_orders, 6)
        self.assertEqual(rounded_rate, 0.5556)
        total = {
            "orders": direction_orders * 2,
            "wins": direction_wins + direction_orders,
            "losses": direction_orders - direction_wins,
            "win_rate": round(
                (direction_wins + direction_orders) / (direction_orders * 2), 6
            ),
            "pnl": 10.0,
            "ev": 1.0,
            "max_drawdown": 1.0,
            "max_loss_streak": 1,
        }
        long_summary = {
            **total,
            "orders": direction_orders,
            "wins": direction_wins,
            "losses": direction_orders - direction_wins,
            "win_rate": rounded_rate,
            "pnl": 5.0,
        }
        short_summary = {
            **total,
            "orders": direction_orders,
            "wins": direction_orders,
            "losses": 0,
            "win_rate": 1.0,
            "pnl": 5.0,
        }
        baseline = {
            "total": dict(total),
            "by_direction": {
                "LONG": dict(long_summary),
                "SHORT": dict(short_summary),
            },
            "base_first_orders": 100,
            "oos_windows": [],
        }
        candidate = {
            "total": dict(total),
            "by_direction": {
                "LONG": dict(long_summary),
                "SHORT": dict(short_summary),
            },
            "base_first_orders": 100,
            "oos_windows": [
                {"pnl": 1.0, "ev": 1.0},
                {"pnl": 1.0, "ev": 1.0},
                {"pnl": -1.0, "ev": -1.0},
            ],
        }

        result = replay_module.evaluate_release_gates(baseline, candidate)

        self.assertEqual(result["gates"]["long_win_rate"]["actual"], 0.5556)
        self.assertFalse(result["gates"]["long_win_rate"]["passed"])

    def test_release_gates_reject_retention_rounded_up_to_threshold(self):
        def summary(orders):
            return {
                "orders": orders,
                "wins": orders,
                "losses": 0,
                "win_rate": 1.0,
                "pnl": 1.0,
                "ev": 1.0,
                "max_drawdown": 0.0,
                "max_loss_streak": 0,
            }

        baseline = {
            "total": summary(2_500_001),
            "by_direction": {
                "LONG": summary(2_500_001),
                "SHORT": summary(1),
            },
            "base_first_orders": 2_000_001,
            "oos_windows": [],
        }
        candidate = {
            "total": summary(2_000_000),
            "by_direction": {
                "LONG": summary(1_750_000),
                "SHORT": summary(1),
            },
            "base_first_orders": 1_700_000,
            "oos_windows": [
                {"pnl": 1.0, "ev": 1.0},
                {"pnl": 1.0, "ev": 1.0},
                {"pnl": -1.0, "ev": -1.0},
            ],
        }

        result = replay_module.evaluate_release_gates(baseline, candidate)

        for gate_name, displayed_threshold in (
            ("total_order_retention", 0.8),
            ("long_order_retention", 0.7),
            ("base_first_order_retention", 0.85),
        ):
            with self.subTest(gate=gate_name):
                self.assertEqual(
                    result["gates"][gate_name]["actual"], displayed_threshold
                )
                self.assertFalse(result["gates"][gate_name]["passed"])

    def test_profile_admission_search_evaluates_fixed_grid_and_blocks_unproven_release(self):
        start_at = timestamp("2026-07-20T08:00:00")
        end_at = start_at + 86_400_000
        rows = [
            observation(
                f"search-{index:02d}",
                "LOSS" if index % 3 == 2 else "WIN",
                start_at + index * 12 * 60_000,
                direction="LONG" if index % 2 == 0 else "SHORT",
                tag="search-long" if index % 2 == 0 else "search-short",
            )
            for index in range(50)
        ]
        profiles = {
            replay_module._observation_profile_key(item)
            for item in rows
        }
        selected_profiles = [
            {
                "key": profile,
                "sample_size": 20,
                "win_rate": 0.7,
                "ev": 1.0,
                "qualification_state": "QUALIFIED",
                "selection_state": "SELECTED",
            }
            for profile in sorted(profiles)
        ]
        snapshot = {
            "effective_from": start_at,
            "effective_until": end_at,
            "selected_profiles": selected_profiles,
            "candidates": selected_profiles,
        }
        timeline = self.adaptive_timeline(
            [
                (profile, start_at - 1, "ACTIVE", 7, 1.0)
                for profile in sorted(profiles)
            ]
        )
        windows = replay_module._prepare_execution_windows(rows, [snapshot])
        execution = self.production_execution().normalized()
        baseline = replay_module._execution_report(
            replay_module._execute_replay(
                windows,
                execution,
                timeline,
                apply_adaptive=False,
                include_structure_shadow=False,
            ),
            start_at,
            end_at,
        )

        first = replay_module.search_profile_admission_policies(
            windows,
            execution,
            timeline,
            baseline,
            start_at,
            end_at,
        )
        second = replay_module.search_profile_admission_policies(
            windows,
            execution,
            timeline,
            baseline,
            start_at,
            end_at,
        )

        self.assertEqual(first["grid_size"], 32)
        self.assertEqual(first["evaluated_count"], 32)
        self.assertEqual(len(first["configurations"]), 32)
        self.assertTrue(
            all(
                len(item["full_day_metrics"]) == 1
                for item in first["configurations"]
            )
        )
        self.assertEqual(
            [item["policy_hash"] for item in first["configurations"]],
            [item["policy_hash"] for item in second["configurations"]],
        )
        self.assertTrue(
            first["aggregate_gates_passed"],
            first["best_candidate"]["failed_gates"],
        )
        self.assertFalse(first["stability_proven"])
        self.assertFalse(first["release_allowed"])
        self.assertIsNone(first["release_policy"])
        self.assertLess(first["stability"]["complete_forward_days"], 7)
        self.assertEqual(
            first["equivalence_scope"]["scope"],
            "PROFILE_ADMISSION_LAYER_ONLY",
        )

    def test_forward_stability_requires_seven_days_of_actual_performance(self):
        start_at = timestamp("2026-07-20T08:00:00")

        def forward_trades(good_days):
            trades = []
            for day in range(7):
                wins = 35 if day < good_days else 25
                for index in range(50):
                    won = index < wins
                    trades.append(
                        {
                            "observation_key": f"forward-{day}-{index}",
                            "direction": "LONG" if index % 2 == 0 else "SHORT",
                            "opened_at": start_at + day * 86_400_000 + index * 60_000,
                            "settled_at": start_at + day * 86_400_000 + (index + 10) * 60_000,
                            "result": "WIN" if won else "LOSS",
                            "pnl": 8.0 if won else -10.0,
                        }
                    )
            return trades

        failed = replay_module._forward_stability_report(
            {"policy_hash": "frozen", "trade_rows": forward_trades(3)},
            frozen_policy_hash="frozen",
            forward_start_at=start_at,
            oos_end=start_at + 7 * 86_400_000,
        )

        self.assertFalse(failed["passed"])
        self.assertEqual(failed["complete_forward_days"], 7)
        self.assertFalse(failed["gates"]["qualifying_win_rate_days"]["passed"])
        self.assertFalse(failed["gates"]["positive_ev_days"]["passed"])
        self.assertFalse(failed["gates"]["combined_win_rate"]["passed"])
        self.assertTrue(failed["gates"]["orders_per_day"]["passed"])

        passed = replay_module._forward_stability_report(
            {"policy_hash": "frozen", "trade_rows": forward_trades(7)},
            frozen_policy_hash="frozen",
            forward_start_at=start_at,
            oos_end=start_at + 7 * 86_400_000,
        )
        self.assertTrue(passed["passed"])

    def test_profile_admission_search_has_no_release_policy_when_aggregate_gates_fail(self):
        execution = self.production_execution().normalized()
        start_at = timestamp("2026-07-20T08:00:00")
        end_at = start_at + 86_400_000
        baseline_result = replay_module._execute_replay(
            (),
            execution,
            {"by_profile": {}, "times_by_profile": {}, "settled_times": []},
            apply_adaptive=False,
            include_structure_shadow=False,
        )
        baseline = replay_module._execution_report(
            baseline_result,
            start_at,
            end_at,
        )

        report = replay_module.search_profile_admission_policies(
            (),
            execution,
            {"by_profile": {}, "times_by_profile": {}, "settled_times": []},
            baseline,
            start_at,
            end_at,
        )

        self.assertFalse(report["aggregate_gates_passed"])
        self.assertFalse(report["release_allowed"])
        self.assertIsNone(report["release_policy"])
        self.assertTrue(report["best_candidate"]["failed_gates"])

    def test_profile_admission_search_gates_use_unrounded_values(self):
        total_orders = 5_000
        long_orders = 2_509
        long_wins = 1_394
        short_orders = total_orders - long_orders
        short_wins = 3_000 - long_wins

        def summary(orders, wins, pnl):
            return {
                "orders": orders,
                "wins": wins,
                "losses": orders - wins,
                "win_rate": round(wins / orders, 6),
                "pnl": round(pnl, 4),
                "ev": round(pnl / orders, 4),
                "max_drawdown": 1.0,
                "max_loss_streak": 1,
            }

        baseline = {
            "total": summary(total_orders, 3_000, 100.0),
            "by_direction": {
                "LONG": summary(long_orders, long_wins, 50.0),
                "SHORT": summary(short_orders, short_wins, 50.0),
            },
            "trade_rows": [
                {"direction": "LONG", "result": "LOSS", "pnl": -1.0},
                {"direction": "LONG", "result": "WIN", "pnl": 1.0},
            ],
        }
        candidate = {
            "total": summary(total_orders, 3_000, -0.000001),
            "by_direction": {
                "LONG": summary(long_orders, long_wins, 1.0),
                "SHORT": summary(short_orders, short_wins, 1.0),
            },
            "oos_windows": [
                {"orders": 1, "wins": 1, "pnl": 1.0},
                {"orders": 1, "wins": 1, "pnl": 1.0},
                {"orders": 1, "wins": 0, "pnl": -1.0},
            ],
            "trade_rows": [
                {
                    "direction": "LONG",
                    "result": "LOSS",
                    "pnl": -1.0000001,
                },
                {
                    "direction": "LONG",
                    "result": "WIN",
                    "pnl": 1.00000009,
                },
            ],
        }
        active_days = total_orders / 44.9999996

        result = replay_module.evaluate_profile_admission_search_gates(
            baseline,
            candidate,
            active_oos_days=active_days,
        )

        self.assertEqual(result["gates"]["long_win_rate"]["actual"], 0.5556)
        self.assertFalse(result["gates"]["long_win_rate"]["passed"])
        self.assertEqual(result["gates"]["orders_per_day"]["actual"], 45.0)
        self.assertFalse(result["gates"]["orders_per_day"]["passed"])
        self.assertEqual(result["gates"]["total_ev"]["actual"], -0.0)
        self.assertFalse(result["gates"]["total_ev"]["passed"])
        self.assertEqual(
            result["gates"]["maximum_drawdown_not_worse"]["actual"],
            1.0,
        )
        self.assertFalse(
            result["gates"]["maximum_drawdown_not_worse"]["passed"]
        )

    def test_profile_admission_search_ranking_is_deterministic_lexicographic(self):
        def row(
            name,
            passed,
            window_rate,
            orders_per_day,
            drawdown,
            streak,
            complexity,
            policy_hash,
            raw_drawdown=None,
        ):
            return {
                "name": name,
                "aggregate_gates_passed": passed,
                "minimum_window_win_rate_raw": window_rate,
                "orders_per_day_raw": orders_per_day,
                "total": {
                    "max_drawdown": drawdown,
                    "max_loss_streak": streak,
                },
                "policy_complexity": complexity,
                "policy_hash": policy_hash,
                "maximum_drawdown_raw": (
                    drawdown if raw_drawdown is None else raw_drawdown
                ),
            }

        configurations = [
            row("failed", False, 0.99, 50.0, 0.0, 0, 0, "0"),
            row("farther", True, 0.60, 48.0, 1.0, 1, 1, "2"),
            row("nearer", True, 0.60, 49.0, 2.0, 2, 2, "3", 2.0000002),
            row("best-window", True, 0.61, 45.0, 9.0, 9, 9, "4"),
            row("raw-drawdown", True, 0.60, 49.0, 2.0, 2, 2, "f", 2.0000001),
        ]

        ranked = replay_module.rank_profile_admission_configurations(configurations)

        self.assertEqual(
            [item["name"] for item in ranked],
            ["best-window", "raw-drawdown", "nearer", "farther", "failed"],
        )
        self.assertEqual(
            ranked,
            replay_module.rank_profile_admission_configurations(
                list(reversed(configurations))
            ),
        )

    def test_passing_configurations_rank_by_win_rate_orders_then_drawdown(self):
        self.assertTrue(hasattr(replay_module, "rank_passing_configurations"))
        reports = [
            {"name": "drawdown-high", "passed": True, "total": {"wins": 62, "win_rate": 0.62, "orders": 100, "max_drawdown": 12}},
            {"name": "more-orders", "passed": True, "total": {"wins": 124, "win_rate": 0.62, "orders": 200, "max_drawdown": 20}},
            {"name": "best-rate", "passed": True, "total": {"wins": 63, "win_rate": 0.63, "orders": 100, "max_drawdown": 30}},
            {"name": "drawdown-low", "passed": True, "total": {"wins": 62, "win_rate": 0.62, "orders": 100, "max_drawdown": 10}},
            {"name": "rejected", "passed": False, "total": {"wins": 999, "win_rate": 0.99, "orders": 999, "max_drawdown": 0}},
        ]

        ranked = replay_module.rank_passing_configurations(reports)

        self.assertEqual(
            [item["name"] for item in ranked],
            ["best-rate", "more-orders", "drawdown-low", "drawdown-high"],
        )

    def test_passing_configuration_sort_uses_unrounded_win_ratio(self):
        reports = [
            {
                "name": "lower-but-more-orders",
                "passed": True,
                "total": {
                    "wins": 6_000_001,
                    "orders": 10_000_000,
                    "win_rate": 0.6,
                    "max_drawdown": 1.0,
                },
            },
            {
                "name": "higher-raw-rate",
                "passed": True,
                "total": {
                    "wins": 3_000_001,
                    "orders": 5_000_000,
                    "win_rate": 0.6,
                    "max_drawdown": 2.0,
                },
            },
        ]

        ranked = replay_module.rank_passing_configurations(reports)

        self.assertEqual(ranked[0]["name"], "higher-raw-rate")

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
