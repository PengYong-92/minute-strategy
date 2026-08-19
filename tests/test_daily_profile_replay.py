import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import fields, replace
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.daily_profile_selector import DailyProfileSelectorConfig
from app.models import ObservationSignal, Signal
from app.simulator import AccountSimulator
from app.storage import (
    _LINKED_CONTEXT_COLUMNS,
    _hydrate_decision_linked_payload,
    SQLiteMonitorStore,
)
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
        self.assertIn("必须与 --win-return 相等", help_text)
        self.assertIn("当前生产兼容参数不生效", help_text)
        self.assertIn("只接受显式空字符串", help_text)

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
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    f"""
                    select observation_signals.payload,
                           observation_signals.observation_key as lifecycle_observation_key,
                           observation_signals.status as lifecycle_status,
                           observation_signals.result as lifecycle_result,
                           observation_signals.opened_at as lifecycle_opened_at,
                           observation_signals.expires_at as lifecycle_expires_at,
                           observation_signals.settled_at as lifecycle_settled_at,
                           {_LINKED_CONTEXT_COLUMNS}
                    from observation_signals
                    left join decision_contexts
                      on decision_contexts.symbol = observation_signals.symbol
                     and decision_contexts.decision_id = observation_signals.decision_id
                    where observation_signals.observation_key = 'v3-observation'
                    """
                ).fetchone()
                expected_payload = _hydrate_decision_linked_payload(
                    json.loads(row["payload"]),
                    row,
                )
                accepted = {item.name for item in fields(ObservationSignal)}
                expected = ObservationSignal(
                    **{
                        key: value
                        for key, value in expected_payload.items()
                        if key in accepted
                    }
                )
                connection.commit()
            before = hashlib.sha256(db_path.read_bytes()).hexdigest()

            try:
                actual = replay_module.load_replay_observations(db_path, "BTCUSDT")
            except Exception as error:  # noqa: BLE001 - turn loader errors into a TDD failure.
                self.fail(f"V3 compact replay load failed: {error}")

            after = hashlib.sha256(db_path.read_bytes()).hexdigest()

        self.assertEqual([item.to_dict() for item in actual], [expected.to_dict()])
        self.assertEqual(actual[0].result, "WIN")
        self.assertEqual(actual[0].settled_at, 601_000)
        self.assertEqual(actual[0].exit_price, 101.0)
        self.assertEqual(actual[0].pnl, 8.0)
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
            "by_profile": {profile: [(row.opened_at, watch)]},
            "settled_times": [row.opened_at],
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
            [row],
            [snapshot],
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
