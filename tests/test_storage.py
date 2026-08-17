import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing, contextmanager
from dataclasses import fields, replace
from unittest import mock
from pathlib import Path

from app.decision_context import (
    CONTEXT_VERSION,
    DecisionContext,
    RuntimeConfigSnapshot,
    runtime_config_snapshot,
)
from app.models import ObservationSignal, Signal, SimulatedOrder
from app.simulator import AccountSimulator
from app.stake_progression import TWO_STAGE_VERSION, StakeProgressionCredit
from app.storage import SQLiteMonitorStore
from app.storage_capacity import (
    COMPACT_ONLY_BYTES,
    CoreStorageCapacityError,
    capacity_for_bytes,
)
from app.wave_state import WaveSnapshot


def signal(direction="LONG", timeframe_minutes=10):
    return Signal(
        direction=direction,
        timeframe_minutes=timeframe_minutes,
        level="A",
        reason="persist me",
        price=100.0,
        open_time=0,
        score=82.0,
        threshold=70.0,
        calculated_threshold=81.5,
        threshold_segment="WD-12",
        session_allowed=True,
        session_sample_size=37,
        session_win_rate=0.6757,
        session_ev=2.1622,
        session_edge_min=10.0,
        regime="FEAR_RISING",
    )


def observation(
    key: str,
    *,
    family: str = "short_extension",
    tag: str = "normal_down_short_extension_observe",
    direction: str = "SHORT",
    segment: str = "WD-02",
    status: str = "SETTLED",
    result: str | None = "WIN",
    opened_at: int = 1_000,
) -> ObservationSignal:
    pnl = 0.0
    if status == "SETTLED":
        pnl = 8.0 if result == "WIN" else -10.0
    return ObservationSignal(
        observation_key=key,
        strategy_family=family,
        strategy_tag=tag,
        direction=direction,
        timeframe_minutes=10,
        level="A",
        reason="observe signal",
        entry_price=100.0,
        opened_at=opened_at,
        expires_at=opened_at + 600_000,
        threshold_segment=segment,
        score=-90.0 if direction == "SHORT" else 90.0,
        threshold=80.0,
        edge=10.0,
        regime="FEAR_FALLING",
        source_decision="SHORT_OBSERVE_ONLY",
        status=status,
        result=result,
        exit_price=99.0 if result == "WIN" else 101.0,
        settled_at=opened_at + 600_000 if status == "SETTLED" else None,
        pnl=pnl,
    )


def progression_order(
    order_id: int,
    *,
    status: str = "OPEN",
    step: int = 1,
    source_order_id: int | None = None,
    version: str = TWO_STAGE_VERSION,
) -> SimulatedOrder:
    settled = status == "SETTLED"
    return SimulatedOrder(
        id=order_id,
        direction="LONG",
        timeframe_minutes=10,
        level="A",
        reason="progression persistence",
        entry_price=100.0,
        opened_at=1_000,
        expires_at=601_000,
        status=status,
        result="WIN" if settled else None,
        exit_price=101.0 if settled else None,
        settled_at=601_000 if settled else None,
        pnl=8.0 if settled else 0.0,
        stake_progression_step=step,
        stake_progression_source_order_id=source_order_id,
        stake_progression_version=version,
    )


def decision_context(
    snapshot: RuntimeConfigSnapshot,
    **overrides,
) -> DecisionContext:
    arguments = {
        "decision_id": "decision-1",
        "context_version": CONTEXT_VERSION,
        "runtime_config_hash": snapshot.hash,
        "strategy_build_id": snapshot.strategy_build_id,
        "symbol": "BTCUSDT",
        "closed_kline_at_ms": 1_700_000_000_000,
        "candidate_origin": "strategy",
        "inputs": {
            "identity": {
                "direction": "LONG",
                "profile_key": "profile-a",
            },
            "market": {"close": 100.25},
        },
        "decision_trace": (
            {
                "stage": "qualification",
                "result": "PASS",
                "decisive_values": {"score": 82.0},
                "reason_code": "QUALIFIED",
            },
        ),
        "first_decisive_block": "",
        "final_decision": "OPEN",
        "final_reason": "accepted",
        "open_allowed": True,
        "observation_allowed": False,
    }
    arguments.update(overrides)
    return DecisionContext(**arguments)


def snapshot_with_payload(
    canonical_payload: str,
    *,
    strategy_build_id: str = "build-7",
    runtime_config_hash: str | None = None,
) -> RuntimeConfigSnapshot:
    return RuntimeConfigSnapshot(
        hash=runtime_config_hash
        or hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest(),
        canonical_payload=canonical_payload,
        strategy_build_id=strategy_build_id,
    )


def insert_runtime_config_row(
    db_path: Path,
    snapshot: RuntimeConfigSnapshot,
    **overrides,
) -> None:
    values = {
        "runtime_config_hash": snapshot.hash,
        "context_version": CONTEXT_VERSION,
        "strategy_build_id": snapshot.strategy_build_id,
        "canonical_payload": snapshot.canonical_payload,
        "payload_bytes": len(snapshot.canonical_payload.encode("utf-8")),
        "created_at_ms": 123,
    }
    values.update(overrides)
    columns = tuple(values)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(
            f"""
            insert into runtime_config_snapshots({", ".join(columns)})
            values ({", ".join("?" for _ in columns)})
            """,
            tuple(values[column] for column in columns),
        )
        connection.commit()


def decision_storage_rows(db_path: Path):
    with closing(sqlite3.connect(db_path)) as connection:
        runtime_rows = connection.execute(
            "select * from runtime_config_snapshots order by runtime_config_hash"
        ).fetchall()
        decision_rows = connection.execute(
            "select * from decision_contexts order by symbol, decision_id"
        ).fetchall()
    return runtime_rows, decision_rows


class SQLiteMonitorStoreTest(unittest.TestCase):
    def assert_save_rejects_invalid_runtime_reference(
        self,
        snapshot: RuntimeConfigSnapshot,
        **runtime_overrides,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            insert_runtime_config_row(db_path, snapshot, **runtime_overrides)
            before = decision_storage_rows(db_path)

            with self.assertRaises(ValueError):
                store.save_decision_context(decision_context(snapshot))

            after = decision_storage_rows(db_path)
        self.assertEqual(after, before)
        self.assertEqual(len(after[0]), 1)
        self.assertEqual(after[1], [])

    def test_runtime_config_snapshot_round_trip_is_json_safe_and_independent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            snapshot = runtime_config_snapshot(
                {"threshold": 81.5, "guard": {"enabled": True}},
                strategy_build_id="build-7",
            )

            store.save_runtime_config_snapshot(snapshot)
            first = store.load_runtime_config_snapshot(snapshot.hash)
            second = store.load_runtime_config_snapshot(snapshot.hash)

        self.assertIsNotNone(first)
        self.assertEqual(
            set(first),
            {
                "runtime_config_hash",
                "context_version",
                "strategy_build_id",
                "canonical_payload",
                "payload_bytes",
                "created_at_ms",
            },
        )
        self.assertEqual(first["runtime_config_hash"], snapshot.hash)
        self.assertEqual(first["context_version"], CONTEXT_VERSION)
        self.assertEqual(first["strategy_build_id"], "build-7")
        self.assertEqual(first["canonical_payload"], snapshot.canonical_payload)
        self.assertEqual(
            first["payload_bytes"],
            len(snapshot.canonical_payload.encode("utf-8")),
        )
        self.assertIs(type(first["created_at_ms"]), int)
        self.assertEqual(json.loads(json.dumps(first, allow_nan=False)), first)
        self.assertEqual(second, first)
        self.assertIsNot(second, first)
        first["strategy_build_id"] = "mutated"
        self.assertEqual(second["strategy_build_id"], "build-7")

    def test_runtime_config_repeated_save_keeps_one_unchanged_row(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            snapshot = runtime_config_snapshot(
                {"threshold": 81.5},
                strategy_build_id="build-7",
            )

            store.save_runtime_config_snapshot(snapshot)
            original = store.load_runtime_config_snapshot(snapshot.hash)
            store.save_runtime_config_snapshot(snapshot)
            repeated = store.load_runtime_config_snapshot(snapshot.hash)
            with closing(sqlite3.connect(db_path)) as connection:
                count = connection.execute(
                    "select count(*) from runtime_config_snapshots"
                ).fetchone()[0]

        self.assertEqual(count, 1)
        self.assertEqual(repeated, original)

    def test_runtime_config_same_hash_preserves_first_seen_build(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            first_store = SQLiteMonitorStore(db_path)
            second_store = SQLiteMonitorStore(db_path)
            first = runtime_config_snapshot(
                {"threshold": 81.5},
                strategy_build_id="build-a",
            )
            second = runtime_config_snapshot(
                {"threshold": 81.5},
                strategy_build_id="build-b",
            )

            first_store.save_runtime_config_snapshot(first)
            second_store.save_runtime_config_snapshot(second)
            second_build_context = decision_context(second)
            second_store.save_decision_context(second_build_context)
            restored = second_store.load_runtime_config_snapshot(first.hash)
            restored_context = second_store.load_decision_context(
                second_build_context.symbol,
                second_build_context.decision_id,
            )
            with closing(sqlite3.connect(db_path)) as connection:
                count = connection.execute(
                    "select count(*) from runtime_config_snapshots"
                ).fetchone()[0]

        self.assertEqual(first.hash, second.hash)
        self.assertEqual(count, 1)
        self.assertEqual(restored["strategy_build_id"], "build-a")
        self.assertEqual(restored_context, second_build_context.to_dict())
        self.assertEqual(restored_context["strategy_build_id"], "build-b")

    def test_runtime_config_rejects_exact_credential_keys_recursively(self):
        credential_payloads = {
            "root api key": {"api_key": "plaintext"},
            "nested api secret": {"service": {"api_secret": "plaintext"}},
            "list webhook token": {
                "services": [{"enabled": True}, {"webhook_token": "plaintext"}]
            },
            "deep webhook url": {
                "services": [{"notifications": [{"webhook_url": "https://secret"}]}]
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)

            for label, payload in credential_payloads.items():
                with self.subTest(label=label):
                    canonical_payload = json.dumps(
                        payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                    snapshot = snapshot_with_payload(canonical_payload)

                    with self.assertRaises(ValueError):
                        store.save_runtime_config_snapshot(snapshot)

                    self.assertEqual(decision_storage_rows(db_path), ([], []))

    def test_runtime_config_credential_boundary_uses_exact_key_semantics(self):
        payload = {
            "apiKey": "allowed",
            "api_key_name": "allowed",
            "notes": [
                "api_secret",
                {"webhook_url_enabled": True},
                {"description": "webhook_token"},
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            snapshot = snapshot_with_payload(json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ))

            store.save_runtime_config_snapshot(snapshot)
            restored = store.load_runtime_config_snapshot(snapshot.hash)

        self.assertEqual(restored["canonical_payload"], snapshot.canonical_payload)

    def test_runtime_config_rejects_malformed_hash_or_payload_without_writing(self):
        payloads = {
            "malformed hash": snapshot_with_payload(
                '{"threshold":81.5}',
                runtime_config_hash="A" * 64,
            ),
            "noncanonical json": snapshot_with_payload('{"b":2, "a":1}'),
            "nan": snapshot_with_payload('{"threshold":NaN}'),
            "invalid json": snapshot_with_payload("{"),
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)

            for label, snapshot in payloads.items():
                with self.subTest(label=label):
                    with self.assertRaises((TypeError, ValueError)):
                        store.save_runtime_config_snapshot(snapshot)
                    with closing(sqlite3.connect(db_path)) as connection:
                        count = connection.execute(
                            "select count(*) from runtime_config_snapshots"
                        ).fetchone()[0]
                    self.assertEqual(count, 0)

    def test_runtime_config_rejects_injected_collision_without_overwriting(self):
        valid = runtime_config_snapshot(
            {"threshold": 81.5},
            strategy_build_id="build-7",
        )
        conflicts = {
            "payload": {
                "context_version": CONTEXT_VERSION,
                "canonical_payload": '{"threshold":80.0}',
                "payload_bytes": len('{"threshold":80.0}'.encode("utf-8")),
            },
            "version": {
                "context_version": "DECISION_CONTEXT_V1",
                "canonical_payload": valid.canonical_payload,
                "payload_bytes": len(valid.canonical_payload.encode("utf-8")),
            },
            "bytes": {
                "context_version": CONTEXT_VERSION,
                "canonical_payload": valid.canonical_payload,
                "payload_bytes": 1,
            },
        }
        for label, conflict in conflicts.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                db_path = Path(temp_dir) / "monitor.sqlite3"
                store = SQLiteMonitorStore(db_path)
                injected = (
                    valid.hash,
                    conflict["context_version"],
                    "injected-build",
                    conflict["canonical_payload"],
                    conflict["payload_bytes"],
                    123,
                )
                with closing(sqlite3.connect(db_path)) as connection:
                    connection.execute(
                        """
                        insert into runtime_config_snapshots(
                            runtime_config_hash, context_version, strategy_build_id,
                            canonical_payload, payload_bytes, created_at_ms
                        ) values (?, ?, ?, ?, ?, ?)
                        """,
                        injected,
                    )
                    connection.commit()

                with self.assertRaises(ValueError):
                    store.save_runtime_config_snapshot(valid)

                with closing(sqlite3.connect(db_path)) as connection:
                    persisted = connection.execute(
                        """
                        select runtime_config_hash, context_version, strategy_build_id,
                               canonical_payload, payload_bytes, created_at_ms
                        from runtime_config_snapshots
                        """
                    ).fetchone()
                self.assertEqual(persisted, injected)

    def test_decision_context_round_trip_matches_context_dict(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            snapshot = runtime_config_snapshot(
                {"threshold": 81.5},
                strategy_build_id="build-7",
            )
            context = decision_context(snapshot)

            store.save_runtime_config_snapshot(snapshot)
            store.save_decision_context(context)
            restored = store.load_decision_context(
                context.symbol,
                context.decision_id,
            )
            with closing(sqlite3.connect(db_path)) as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    "select * from decision_contexts"
                ).fetchone()

        self.assertEqual(restored, context.to_dict())
        self.assertEqual(json.loads(json.dumps(restored, allow_nan=False)), restored)
        self.assertEqual(row["created_at_ms"], context.closed_kline_at_ms)
        self.assertEqual(row["direction"], "LONG")
        self.assertEqual(row["profile_key"], "profile-a")
        self.assertEqual(row["candidate_origin"], context.candidate_origin)
        self.assertEqual(row["input_payload"], json.dumps(
            context.to_dict()["inputs"],
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ))
        self.assertEqual(
            set(json.loads(row["outcome_payload"])),
            {
                "decision_trace",
                "first_decisive_block",
                "final_decision",
                "final_reason",
                "open_allowed",
                "observation_allowed",
            },
        )

    def test_decision_context_repeated_save_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            snapshot = runtime_config_snapshot({"threshold": 81.5})
            context = decision_context(snapshot)
            store.save_runtime_config_snapshot(snapshot)

            store.save_decision_context(context)
            with closing(sqlite3.connect(db_path)) as connection:
                original = connection.execute(
                    "select * from decision_contexts"
                ).fetchone()
            store.save_decision_context(context)
            with closing(sqlite3.connect(db_path)) as connection:
                repeated = connection.execute(
                    "select * from decision_contexts"
                ).fetchone()
                count = connection.execute(
                    "select count(*) from decision_contexts"
                ).fetchone()[0]

        self.assertEqual(count, 1)
        self.assertEqual(repeated, original)

    def test_decision_context_rejects_changed_frozen_data_without_overwriting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            snapshot = runtime_config_snapshot(
                {"threshold": 81.5},
                strategy_build_id="build-7",
            )
            context = decision_context(snapshot)
            store.save_runtime_config_snapshot(snapshot)
            store.save_decision_context(context)
            with closing(sqlite3.connect(db_path)) as connection:
                original = connection.execute(
                    "select * from decision_contexts"
                ).fetchone()

            variants = {
                "input": decision_context(
                    snapshot,
                    inputs={
                        "identity": {
                            "direction": "LONG",
                            "profile_key": "profile-a",
                        },
                        "market": {"close": 101.0},
                    },
                ),
                "outcome": decision_context(snapshot, final_reason="rejected"),
                "metadata": decision_context(
                    snapshot,
                    candidate_origin="manual-replay",
                ),
            }
            for label, changed in variants.items():
                with self.subTest(label=label):
                    with self.assertRaises(ValueError):
                        store.save_decision_context(changed)
                    with closing(sqlite3.connect(db_path)) as connection:
                        persisted = connection.execute(
                            "select * from decision_contexts"
                        ).fetchone()
                        count = connection.execute(
                            "select count(*) from decision_contexts"
                        ).fetchone()[0]
                    self.assertEqual(count, 1)
                    self.assertEqual(persisted, original)

    def test_decision_context_rejects_missing_or_wrong_version_config_reference(self):
        snapshot = runtime_config_snapshot({"threshold": 81.5})
        for label in ("missing", "wrong version"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                db_path = Path(temp_dir) / "monitor.sqlite3"
                store = SQLiteMonitorStore(db_path)
                if label == "wrong version":
                    with closing(sqlite3.connect(db_path)) as connection:
                        connection.execute(
                            """
                            insert into runtime_config_snapshots(
                                runtime_config_hash, context_version,
                                strategy_build_id, canonical_payload,
                                payload_bytes, created_at_ms
                            ) values (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                snapshot.hash,
                                "DECISION_CONTEXT_V1",
                                snapshot.strategy_build_id,
                                snapshot.canonical_payload,
                                len(snapshot.canonical_payload.encode("utf-8")),
                                123,
                            ),
                        )
                        connection.commit()

                with self.assertRaises(ValueError):
                    store.save_decision_context(decision_context(snapshot))

                with closing(sqlite3.connect(db_path)) as connection:
                    count = connection.execute(
                        "select count(*) from decision_contexts"
                    ).fetchone()[0]
                self.assertEqual(count, 0)

    def test_decision_context_save_rejects_runtime_payload_hash_mismatch(self):
        snapshot = runtime_config_snapshot({"threshold": 81.5})
        mismatched_payload = '{"threshold":80.0}'

        self.assert_save_rejects_invalid_runtime_reference(
            snapshot,
            canonical_payload=mismatched_payload,
            payload_bytes=len(mismatched_payload.encode("utf-8")),
        )

    def test_decision_context_save_rejects_runtime_payload_byte_mismatch(self):
        snapshot = runtime_config_snapshot({"threshold": 81.5})

        self.assert_save_rejects_invalid_runtime_reference(
            snapshot,
            payload_bytes=1,
        )

    def test_decision_context_save_rejects_malformed_or_noncanonical_runtime_json(self):
        for label, payload in {
            "malformed": "{",
            "noncanonical": '{"threshold": 81.5}',
        }.items():
            with self.subTest(label=label):
                self.assert_save_rejects_invalid_runtime_reference(
                    snapshot_with_payload(payload),
                )

    def test_decision_context_load_rejects_corrupt_runtime_reference_without_changes(self):
        corruptions = {
            "payload": ("canonical_payload", '{"threshold":80.0}'),
            "hash": ("runtime_config_hash", "A" * 64),
            "bytes": ("payload_bytes", 1),
            "version": ("context_version", "DECISION_CONTEXT_V1"),
            "timestamp": ("created_at_ms", -1),
        }
        for label, (column, value) in corruptions.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                db_path = Path(temp_dir) / "monitor.sqlite3"
                store = SQLiteMonitorStore(db_path)
                snapshot = runtime_config_snapshot({"threshold": 81.5})
                context = decision_context(snapshot)
                store.save_runtime_config_snapshot(snapshot)
                store.save_decision_context(context)
                self.assertEqual(
                    store.load_decision_context(context.symbol, context.decision_id),
                    context.to_dict(),
                )

                with closing(sqlite3.connect(db_path)) as connection:
                    connection.execute(
                        f"update runtime_config_snapshots set {column} = ?",
                        (value,),
                    )
                    if column == "runtime_config_hash":
                        connection.execute(
                            "update decision_contexts set runtime_config_hash = ?",
                            (value,),
                        )
                    connection.commit()
                before_rejection = decision_storage_rows(db_path)

                with self.assertRaises(ValueError):
                    store.load_decision_context(context.symbol, context.decision_id)

                self.assertEqual(decision_storage_rows(db_path), before_rejection)
                self.assertEqual(len(before_rejection[0]), 1)
                self.assertEqual(len(before_rejection[1]), 1)

    def test_decision_context_load_detects_malformed_stored_json(self):
        snapshot = runtime_config_snapshot({"threshold": 81.5})
        corruptions = {
            "invalid input json": ("input_payload", "{"),
            "input is not an object": ("input_payload", "[]"),
            "outcome is not an object": ("outcome_payload", "[]"),
            "trace is not a list": (
                "outcome_payload",
                json.dumps(
                    {
                        "decision_trace": {},
                        "first_decisive_block": "",
                        "final_decision": "OPEN",
                        "final_reason": "accepted",
                        "open_allowed": True,
                        "observation_allowed": False,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        }
        for label, (column, value) in corruptions.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                db_path = Path(temp_dir) / "monitor.sqlite3"
                store = SQLiteMonitorStore(db_path)
                context = decision_context(snapshot)
                store.save_runtime_config_snapshot(snapshot)
                store.save_decision_context(context)
                with closing(sqlite3.connect(db_path)) as connection:
                    connection.execute(
                        f"update decision_contexts set {column} = ?",
                        (value,),
                    )
                    connection.commit()

                with self.assertRaises(ValueError):
                    store.load_decision_context(
                        context.symbol,
                        context.decision_id,
                    )

    def test_decision_context_rejects_invalid_identity_metadata_without_writing(self):
        snapshot = runtime_config_snapshot({"threshold": 81.5})
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            store.save_runtime_config_snapshot(snapshot)
            invalid = decision_context(
                snapshot,
                inputs={
                    "identity": {
                        "direction": ["LONG"],
                        "profile_key": "profile-a",
                    }
                },
            )

            with self.assertRaises((TypeError, ValueError)):
                store.save_decision_context(invalid)

            with closing(sqlite3.connect(db_path)) as connection:
                count = connection.execute(
                    "select count(*) from decision_contexts"
                ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_decision_context_post_insert_mismatch_rolls_back_transaction(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            snapshot = runtime_config_snapshot({"threshold": 81.5})
            context = decision_context(snapshot)
            store.save_runtime_config_snapshot(snapshot)
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute(
                    """
                    create trigger mutate_decision_after_insert
                    after insert on decision_contexts
                    begin
                        update decision_contexts
                        set candidate_origin = 'trigger-mutated'
                        where symbol = new.symbol and decision_id = new.decision_id;
                    end
                    """
                )
                connection.commit()

            try:
                with self.assertRaises(ValueError):
                    store.save_decision_context(context)
                runtime_rows, decision_rows = decision_storage_rows(db_path)
            finally:
                with closing(sqlite3.connect(db_path)) as connection:
                    connection.execute("drop trigger mutate_decision_after_insert")
                    connection.commit()

        self.assertEqual(len(runtime_rows), 1)
        self.assertEqual(decision_rows, [])

    def test_two_store_instances_save_snapshot_and_context_concurrently(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            first_store = SQLiteMonitorStore(db_path)
            second_store = SQLiteMonitorStore(db_path)
            first_snapshot = runtime_config_snapshot(
                {"threshold": 81.5},
                strategy_build_id="build-a",
            )
            second_snapshot = runtime_config_snapshot(
                {"threshold": 81.5},
                strategy_build_id="build-b",
            )
            context = decision_context(second_snapshot)
            barrier = threading.Barrier(3)
            errors = []
            error_lock = threading.Lock()

            def persist(store, snapshot):
                try:
                    barrier.wait(timeout=5)
                    store.save_runtime_config_snapshot(snapshot)
                    store.save_decision_context(context)
                except BaseException as error:
                    with error_lock:
                        errors.append(error)

            threads = [
                threading.Thread(target=persist, args=(first_store, first_snapshot)),
                threading.Thread(target=persist, args=(second_store, second_snapshot)),
            ]
            for thread in threads:
                thread.start()
            barrier.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=10)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            restored_snapshot = first_store.load_runtime_config_snapshot(
                first_snapshot.hash
            )
            restored_context = second_store.load_decision_context(
                context.symbol,
                context.decision_id,
            )
            runtime_rows, decision_rows = decision_storage_rows(db_path)

        self.assertEqual(len(runtime_rows), 1)
        self.assertEqual(len(decision_rows), 1)
        self.assertIn(restored_snapshot["strategy_build_id"], {"build-a", "build-b"})
        self.assertEqual(restored_context, context.to_dict())

    def test_two_store_instances_persist_decision_context_deterministically(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            first_store = SQLiteMonitorStore(db_path)
            second_store = SQLiteMonitorStore(db_path)
            snapshot = runtime_config_snapshot(
                {"threshold": 81.5},
                strategy_build_id="build-7",
            )
            context = decision_context(snapshot)

            first_store.save_runtime_config_snapshot(snapshot)
            first_store.save_decision_context(context)
            second_store.save_runtime_config_snapshot(snapshot)
            second_store.save_decision_context(context)
            first = first_store.load_decision_context(
                context.symbol,
                context.decision_id,
            )
            second = second_store.load_decision_context(
                context.symbol,
                context.decision_id,
            )
            with closing(sqlite3.connect(db_path)) as connection:
                counts = connection.execute(
                    """
                    select
                        (select count(*) from runtime_config_snapshots),
                        (select count(*) from decision_contexts)
                    """
                ).fetchone()

        self.assertEqual(counts, (1, 1))
        self.assertEqual(first, context.to_dict())
        self.assertEqual(second, first)

    def test_persists_and_restores_wave_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            snapshot = WaveSnapshot(
                state="UP_LEG",
                raw_state="UP_LEG",
                window=8,
                efficiency=0.9,
                direction_ratio=0.85,
                atr_strength=1.7,
                range_position=0.92,
                confirmations=2,
                confirmed_at=960_000,
                allowed_directions=("LONG",),
            )

            store.save_wave_runtime("BTCUSDT", snapshot, evaluated_at=24_000_000)
            restored = store.load_wave_runtime("BTCUSDT")

        self.assertIsNotNone(restored)
        self.assertEqual(restored["evaluated_at"], 24_000_000)
        self.assertEqual(restored["snapshot"], snapshot)

    def test_persists_and_restores_simulated_orders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            simulator = AccountSimulator()
            order = simulator.open_order(signal(), entry_price=100.0, opened_at=1_000)
            simulator.settle_expired_orders(current_time=601_000, current_price=101.0)

            store.save_order(order, symbol="BTCUSDT")
            restored = store.load_orders("BTCUSDT")

        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].id, order.id)
        self.assertEqual(restored[0].status, "SETTLED")
        self.assertEqual(restored[0].result, "WIN")
        self.assertEqual(restored[0].pnl, 8.0)
        self.assertEqual(restored[0].calculated_threshold, 81.5)

    def test_persists_direction_pulse_shadow_on_orders_and_observations(self):
        shadow = {
            "version": "DIRECTION_PULSE_V1_SHADOW",
            "mode": "SHADOW_ONLY",
            "direction": "SHORT",
            "order_slot": "SECOND",
            "windows": {"12": {"status": "WATCH", "would_block": True}},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            saved_order = progression_order(1)
            saved_order.direction_pulse_shadow = shadow
            saved_observation = observation("pulse-shadow")
            saved_observation.direction_pulse_shadow = shadow

            store.save_order(saved_order, "BTCUSDT")
            store.save_observation(saved_observation, "BTCUSDT")
            restored_order = store.load_orders("BTCUSDT")[0]
            restored_observation = store.load_observations("BTCUSDT")[0]

        self.assertEqual(restored_order.direction_pulse_shadow, shadow)
        self.assertEqual(restored_observation.direction_pulse_shadow, shadow)

    def test_saves_observation_settlements_in_one_batch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            observations = [observation(f"batch-{index}") for index in range(3)]

            store.save_observations(observations, "BTCUSDT")
            restored = store.load_observations("BTCUSDT")

        self.assertEqual({item.observation_key for item in restored}, {
            "batch-0",
            "batch-1",
            "batch-2",
        })

    def test_persists_and_restores_wave_batch_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            saved = progression_order(1)
            saved.wave_state = "DOWN_LEG"
            saved.wave_raw_state = "DOWN_LEG"
            saved.wave_batch_id = "123|DOWN_LEG|SHORT|WD-05|DPS-1"
            saved.wave_guard_mode = "RECOVERY"
            saved.wave_guard_status = "WAVE_RECOVERY_READY"
            saved.wave_guard_reason = "冷却结束，仅允许恢复单"

            store.save_order(saved, "BTCUSDT")
            restored = store.load_orders("BTCUSDT")[0]

        self.assertEqual(restored.wave_state, "DOWN_LEG")
        self.assertEqual(restored.wave_batch_id, saved.wave_batch_id)
        self.assertEqual(restored.wave_guard_mode, "RECOVERY")
        self.assertEqual(restored.wave_guard_status, "WAVE_RECOVERY_READY")
        self.assertEqual(restored.wave_guard_reason, "冷却结束，仅允许恢复单")

    def test_persists_and_restores_profile_degradation_probe_metadata(self):
        self.assertIn(
            "profile_degradation_probe",
            {field.name for field in fields(SimulatedOrder)},
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            saved = progression_order(1)
            saved.profile_degradation_probe = True
            saved.profile_degradation_triggered_at = 987_654

            store.save_order(saved, "BTCUSDT")
            restored = store.load_orders("BTCUSDT")[0]

        self.assertTrue(restored.profile_degradation_probe)
        self.assertEqual(restored.profile_degradation_triggered_at, 987_654)

    def test_persists_and_restores_shadow_quality_score_payloads(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            scored_signal = replace(
                signal(),
                quality_score=68.5,
                quality_score_version="QS_V1_SHADOW",
                quality_score_mode="SHADOW_ONLY",
                quality_score_context="LONG_FIRST",
                quality_score_components={"wave_state": 6.0},
                quality_score_inputs={"slot": "FIRST"},
            )
            order = AccountSimulator().open_order(
                scored_signal,
                entry_price=100.0,
                opened_at=1_000,
            )
            scored_observation = replace(
                observation("quality-score"),
                quality_score=72.0,
                quality_score_version="QS_V1_SHADOW",
                quality_score_mode="SHADOW_ONLY",
                quality_score_context="SHORT_SECOND",
                quality_score_components={"volume": 5.0},
                quality_score_inputs={"slot": "SECOND"},
            )

            store.save_order(order, "BTCUSDT")
            store.save_observation(scored_observation, "BTCUSDT")
            restored_order = store.load_orders("BTCUSDT")[0]
            restored_observation = store.load_observations("BTCUSDT")[0]

        self.assertEqual(restored_order.quality_score, 68.5)
        self.assertEqual(restored_order.quality_score_context, "LONG_FIRST")
        self.assertEqual(restored_order.quality_score_components["wave_state"], 6.0)
        self.assertEqual(restored_observation.quality_score, 72.0)
        self.assertEqual(restored_observation.quality_score_context, "SHORT_SECOND")
        self.assertEqual(restored_observation.quality_score_inputs["slot"], "SECOND")

    def test_persists_and_restores_profile_health_audit_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            audited_signal = replace(
                signal(),
                profile_health_status="WATCH",
                profile_health_sample_size=12,
                profile_health_win_rate=0.5,
                profile_health_ev=-1.0,
                profile_health_evaluated_at=1_723_689_600_000,
            )
            order = AccountSimulator().open_order(audited_signal, 100.0, 1_000)
            audited_observation = replace(
                observation("profile-health"),
                profile_health_status="DEGRADED",
                profile_health_sample_size=15,
                profile_health_win_rate=0.466667,
                profile_health_ev=-1.6,
                profile_health_evaluated_at=1_723_689_600_000,
            )

            store.save_order(order, "BTCUSDT")
            store.save_observation(audited_observation, "BTCUSDT")
            restored_order = store.load_orders("BTCUSDT")[0]
            restored_observation = store.load_observations("BTCUSDT")[0]

        self.assertEqual(restored_order.profile_health_status, "WATCH")
        self.assertEqual(restored_order.profile_health_sample_size, 12)
        self.assertEqual(restored_order.profile_health_ev, -1.0)
        self.assertEqual(restored_observation.profile_health_status, "DEGRADED")
        self.assertEqual(restored_observation.profile_health_sample_size, 15)
        self.assertEqual(restored_observation.profile_health_win_rate, 0.466667)

    def test_cancels_multiple_pending_credits_atomically(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            first = StakeProgressionCredit(source_order_id=1, created_at=1_000)
            second = StakeProgressionCredit(source_order_id=2, created_at=2_000)
            store.save_stake_progression_credit("BTCUSDT", first)
            store.save_stake_progression_credit("BTCUSDT", second)
            missing = StakeProgressionCredit(source_order_id=3, created_at=3_000, status="CANCELLED")

            with self.assertRaises(ValueError):
                store.cancel_stake_progression_credits(
                    "BTCUSDT",
                    [replace(first, status="CANCELLED"), missing],
                )
            unchanged = store.load_stake_progression_credits("BTCUSDT")
            self.assertEqual([item.status for item in unchanged], ["PENDING", "PENDING"])

            store.cancel_stake_progression_credits(
                "BTCUSDT",
                [replace(first, status="CANCELLED"), replace(second, status="CANCELLED")],
            )
            cancelled = store.load_stake_progression_credits("BTCUSDT")

        self.assertEqual([item.status for item in cancelled], ["CANCELLED", "CANCELLED"])

    def test_persists_signal_audit_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")

            store.save_signal("BTCUSDT", signal(), decision="OPENED", created_at_ms=1_234)
            rows = store.load_recent_signals("BTCUSDT", limit=5)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "BTCUSDT")
        self.assertEqual(rows[0]["decision"], "OPENED")
        self.assertEqual(rows[0]["direction"], "LONG")
        self.assertEqual(rows[0]["regime"], "FEAR_RISING")

    def test_signal_audit_summary_groups_guard_hits_by_profile_and_slot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            guarded = replace(
                signal(),
                profile_key="10|drop_reclaim|long_observe|LONG|WD-08",
                daily_profile_version="DPS-1",
                order_slot="SECOND",
            )
            store.save_signal(
                "BTCUSDT",
                guarded,
                decision="RESULT_SEQUENCE_GUARD_BLOCKED",
                created_at_ms=1_234,
                audit_context={
                    "result_sequence_guard": {"status": "COOLDOWN"},
                    "profile_degradation_guard": {"status": "NORMAL"},
                },
            )
            store.save_signal(
                "BTCUSDT",
                guarded,
                decision="OPENED",
                created_at_ms=2_234,
                audit_context={
                    "result_sequence_guard": {"status": "NORMAL"},
                    "profile_degradation_guard": {"status": "RECOVERY_READY"},
                },
            )

            summary = store.signal_audit_summary("BTCUSDT")

        self.assertEqual(summary["sample_count"], 2)
        decisions = {item["key"]: item["count"] for item in summary["by_decision"]}
        self.assertEqual(decisions["RESULT_SEQUENCE_GUARD_BLOCKED"], 1)
        self.assertEqual(decisions["OPENED"], 1)
        contexts = {item["key"]: item for item in summary["by_profile_dps_slot"]}
        context = contexts[
            "10|drop_reclaim|long_observe|LONG|WD-08|DPS-1|SECOND"
        ]
        self.assertEqual(context["signals"], 2)
        self.assertEqual(context["blocked"], 1)
        sequence_status = {
            item["key"]: item["count"]
            for item in summary["by_result_sequence_status"]
        }
        self.assertEqual(sequence_status["COOLDOWN"], 1)
        self.assertEqual(sequence_status["NORMAL"], 1)

    def test_signal_audit_v2_aggregates_only_identical_ordinary_heartbeats(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            base = replace(
                signal(direction="WAIT"),
                score=11.0,
                profile_key="10|volume_price|wait|WAIT|WD-12",
                context_version="DECISION_CONTEXT_V2",
                runtime_config_hash="a" * 64,
                first_decisive_block="SCORE",
                quality_score_inputs={"must_not_repeat": "quality-inputs"},
                direction_pulse_shadow={"must_not_repeat": "pulse"},
            )
            audit = {
                "result_sequence_guard": {"status": "NORMAL", "history": [1, 2, 3]},
                "profile_degradation_guard": {"status": "NORMAL"},
                "profile_guard": {"full_config": "must-not-repeat"},
            }

            self.assertTrue(
                store.save_signal(
                    "BTCUSDT",
                    base,
                    decision="BELOW_THRESHOLD",
                    created_at_ms=60_000,
                    audit_context=audit,
                    has_formal_candidate=False,
                )
            )
            self.assertTrue(
                store.save_signal(
                    "BTCUSDT",
                    replace(base, score=19.0, rsi=41.0),
                    decision="BELOW_THRESHOLD",
                    created_at_ms=120_000,
                    audit_context=audit,
                    has_formal_candidate=False,
                )
            )
            with store._connect() as connection:
                rows = connection.execute(
                    "select * from signal_audit order by id"
                ).fetchall()

            recent = store.load_recent_signals("BTCUSDT", limit=5)

        self.assertEqual(len(rows), 1)
        row = dict(rows[0])
        self.assertEqual(row["record_version"], "SIGNAL_AUDIT_V2")
        self.assertEqual(row["event_kind"], "HEARTBEAT")
        self.assertEqual(row["occurrences"], 2)
        self.assertEqual((row["first_at_ms"], row["last_at_ms"]), (60_000, 120_000))
        self.assertEqual((row["score_min"], row["score_max"]), (11.0, 19.0))
        payload = json.loads(row["payload"])
        self.assertEqual(payload["metrics"]["score"], 19.0)
        self.assertEqual(payload["metrics"]["rsi"], 41.0)
        self.assertNotIn("quality-inputs", row["payload"])
        self.assertNotIn("must-not-repeat", row["payload"])
        self.assertEqual(recent[0]["occurrences"], 2)
        self.assertEqual(recent[0]["last_at_ms"], 120_000)

    def test_signal_audit_v2_separates_bucket_guard_state_and_observation_candidates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            wait_signal = replace(
                signal(direction="WAIT"),
                profile_key="10|volume_price|wait|WAIT|WD-12",
                context_version="DECISION_CONTEXT_V2",
                runtime_config_hash="b" * 64,
                first_decisive_block="SCORE",
            )
            normal = {"result_sequence_guard": {"status": "NORMAL"}}
            cooldown = {"result_sequence_guard": {"status": "COOLDOWN"}}

            calls = (
                (60_000, normal, False, False, None),
                (600_000, normal, False, False, None),
                (660_000, cooldown, False, False, None),
                (720_000, cooldown, False, True, "OBSERVATION_CANDIDATE"),
                (780_000, cooldown, True, False, None),
            )
            for created_at_ms, audit, has_formal, force_independent, event_kind in calls:
                store.save_signal(
                    "BTCUSDT",
                    wait_signal,
                    decision="BELOW_THRESHOLD",
                    created_at_ms=created_at_ms,
                    audit_context=audit,
                    has_formal_candidate=has_formal,
                    force_independent=force_independent,
                    event_kind=event_kind,
                )
            with store._connect() as connection:
                rows = connection.execute(
                    "select aggregation_key, occurrences, event_kind from signal_audit order by id"
                ).fetchall()

        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row["occurrences"] == 1 for row in rows))
        self.assertIsNone(rows[2]["aggregation_key"])
        self.assertEqual(rows[2]["event_kind"], "STATE_CHANGE")
        self.assertIsNone(rows[-2]["aggregation_key"])
        self.assertIsNone(rows[-1]["aggregation_key"])
        self.assertEqual(rows[-2]["event_kind"], "OBSERVATION_CANDIDATE")
        self.assertEqual(rows[-1]["event_kind"], "DECISION")

    def test_signal_audit_v2_regime_change_is_a_state_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            base = replace(
                signal(direction="WAIT"),
                profile_key="10|volume_price|wait|WAIT|WD-12",
                context_version="DECISION_CONTEXT_V2",
                runtime_config_hash="d" * 64,
                regime="FEAR_RISING",
            )
            store.save_signal(
                "BTCUSDT",
                base,
                decision="WAIT",
                created_at_ms=60_000,
            )
            store.save_signal(
                "BTCUSDT",
                replace(base, regime="GREED_FALLING"),
                decision="WAIT",
                created_at_ms=120_000,
            )
            with store._connect() as connection:
                rows = connection.execute(
                    "select regime, event_kind, aggregation_key from signal_audit order by id"
                ).fetchall()

        self.assertEqual([row["regime"] for row in rows], ["FEAR_RISING", "GREED_FALLING"])
        self.assertEqual(rows[1]["event_kind"], "STATE_CHANGE")
        self.assertIsNone(rows[1]["aggregation_key"])

    def test_signal_audit_v2_has_one_complete_compact_guard_snapshot(self):
        audit = {
            "result_sequence_guard": {
                "status": "PAUSED", "code": "RESULT_SEQUENCE_GUARD_BLOCKED",
                "blocked": True, "scope": "DIRECTION", "direction": "LONG",
                "consecutive_losses": 3, "last_settled_at": 100, "pause_until": 200,
                "history": ["must-not-repeat"],
            },
            "wave_batch_guard": {
                "status": "BATCH_LOCKED", "code": "WAVE_BATCH_LOSS_LOCKED",
                "mode": "BATCH_LOCKED", "blocked": True, "allow_progression": False,
                "current_batch_id": "batch-1", "batch_orders": 2, "batch_wins": 0,
                "batch_losses": 1, "failed_batches": 2, "pause_until": 300,
                "config": {"must-not-repeat": True},
            },
            "profile_degradation_guard": {
                "status": "COOLDOWN", "code": "PROFILE_DEGRADATION_BLOCKED",
                "blocked": True, "allow_progression": False, "profile_key": "profile-a",
                "daily_profile_version": "DPS-1", "consecutive_losses": 3,
                "last_loss_settled_at": 400, "pause_until": 500,
                "probe_order_id": 7, "triggered_at": 400,
            },
            "profile_health_guard": {
                "status": "DEGRADED", "code": "PROFILE_HEALTH_BLOCKED",
                "blocked": True, "direction": "LONG", "evaluated_at": 600,
                "next_evaluation_at": 700, "sample_size": 12, "wins": 4,
                "losses": 8, "win_rate": 0.333333, "pnl": -48.0, "ev": -4.0,
                "allow_second_order": False, "allow_progression": False,
            },
            "rolling_edge": {
                "status": "DEGRADED", "code": "ROLLING_EDGE_BLOCKED",
                "sample_size": 6, "wins": 2, "losses": 4, "win_rate": 0.3333,
                "pnl": -24.0, "ev": -4.0, "blocked": True,
                "edge": 10.0, "threshold": 0.5,
            },
            "time_period_guard": {
                "enabled": True, "blocked": True, "code": "TIME_PERIOD_SHADOW_ONLY",
                "local_hour": 14, "window": "12:00-18:00",
            },
            "profile_guard": {
                "status": "WOULD_BLOCK", "code": "PROFILE_GUARD_BLOCKED",
                "enabled": True, "observe_only": False, "blocked": True,
                "hit_keys": ["risk-a"], "full_config": "must-not-repeat",
            },
        }
        pulse = {
            "version": "DIRECTION_PULSE_V1_SHADOW",
            "mode": "SHADOW_ONLY",
            "direction": "LONG",
            "order_slot": "FIRST",
            "evaluated_at": 800,
            "windows": {
                "12": {"status": "WATCH", "hypothetical_action": "BLOCK_SECOND"},
                "16": {"status": "NORMAL", "hypothetical_action": "ALLOW"},
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.save_signal(
                "BTCUSDT",
                replace(
                    signal(),
                    risk_flags="RISK_A",
                    direction_pulse_shadow=pulse,
                    quality_score_inputs={"must-not-repeat": True},
                ),
                decision="PROFILE_HEALTH_BLOCKED",
                created_at_ms=1_000,
                audit_context=audit,
                force_independent=True,
            )
            with store._connect() as connection:
                row = connection.execute(
                    "select payload from signal_audit"
                ).fetchone()

        raw_payload = row["payload"]
        payload = json.loads(raw_payload)
        self.assertIn("guards", payload)
        self.assertNotIn("guards", payload["state_code"])
        self.assertEqual(len(payload["state_code"]["guard_state_hash"]), 64)
        self.assertEqual(payload["state_code"]["regime"], "FEAR_RISING")
        self.assertEqual(payload["state_code"]["risk_flags"], "RISK_A")
        self.assertEqual(payload["guards"]["result_sequence"]["scope"], "DIRECTION")
        self.assertEqual(payload["guards"]["result_sequence"]["pause_until"], 200)
        self.assertEqual(payload["guards"]["wave_batch"]["batch_losses"], 1)
        self.assertEqual(payload["guards"]["profile_degradation"]["probe_order_id"], 7)
        self.assertEqual(payload["guards"]["profile_health"]["allow_second_order"], False)
        self.assertEqual(payload["guards"]["rolling_edge"]["threshold"], 0.5)
        self.assertEqual(payload["guards"]["time_period"]["local_hour"], 14)
        self.assertEqual(payload["guards"]["profile_guard"]["hit_keys"], ["risk-a"])
        self.assertEqual(payload["guards"]["wave_signal"]["status"], "UNKNOWN")
        self.assertEqual(payload["direction_pulse"]["status"], "WATCH")
        self.assertEqual(payload["direction_pulse"]["code"], "BLOCK_SECOND")
        self.assertNotIn("must-not-repeat", raw_payload)
        self.assertNotIn("full_config", raw_payload)
        self.assertNotIn("history", raw_payload)
        self.assertNotIn("quality_score_inputs", raw_payload)
        self.assertLess(len(raw_payload.encode("utf-8")), 5_000)

    def test_signal_audit_summary_weights_v2_occurrences_and_legacy_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            with store._connect() as connection:
                connection.execute(
                    """
                    insert into signal_audit(
                        symbol, created_at_ms, decision, direction, timeframe_minutes,
                        threshold_segment, regime, score, threshold, reason, payload
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "BTCUSDT", 1, "WAIT", "WAIT", 10, "WD-12", "UNKNOWN",
                        0.0, 70.0, "legacy", json.dumps({"profile_key": "legacy"}),
                    ),
                )
            heartbeat = replace(
                signal(direction="WAIT"),
                profile_key="v2-profile",
                context_version="DECISION_CONTEXT_V2",
                runtime_config_hash="c" * 64,
            )
            for created_at_ms in range(10_000, 19_000, 1_000):
                store.save_signal(
                    "BTCUSDT",
                    heartbeat,
                    decision="WAIT",
                    created_at_ms=created_at_ms,
                    has_formal_candidate=False,
                )

            summary = store.signal_audit_summary("BTCUSDT")

        self.assertEqual(summary["sample_count"], 10)
        self.assertEqual(summary["storage_rows"], 2)
        decisions = {item["key"]: item["count"] for item in summary["by_decision"]}
        self.assertEqual(decisions["WAIT"], 10)
        contexts = {item["key"]: item for item in summary["by_profile_dps_slot"]}
        self.assertEqual(contexts["v2-profile|STATIC|UNKNOWN"]["signals"], 9)

    def test_compact_only_skips_ordinary_heartbeat_but_keeps_core_audit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            compact = capacity_for_bytes(COMPACT_ONLY_BYTES)
            with mock.patch("app.storage.capacity_from_connection", return_value=compact):
                skipped = store.save_signal(
                    "BTCUSDT",
                    signal(direction="WAIT"),
                    decision="WAIT",
                    created_at_ms=1_000,
                    has_formal_candidate=False,
                )
                saved = store.save_signal(
                    "BTCUSDT",
                    signal(),
                    decision="OPENED",
                    created_at_ms=2_000,
                    has_formal_candidate=True,
                )
            rows = store.load_recent_signals("BTCUSDT", limit=10)

        self.assertFalse(skipped)
        self.assertTrue(saved)
        self.assertEqual([row["decision"] for row in rows], ["OPENED"])

    def test_two_store_instances_aggregate_same_heartbeat_concurrently(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "monitor.sqlite3"
            stores = [SQLiteMonitorStore(path), SQLiteMonitorStore(path)]
            barrier = threading.Barrier(2)
            failures = []

            def save(store):
                try:
                    barrier.wait(timeout=5)
                    store.save_signal(
                        "BTCUSDT",
                        replace(signal(direction="WAIT"), regime="FEAR_FLAT"),
                        decision="WAIT",
                        created_at_ms=60_000,
                    )
                except Exception as error:  # noqa: BLE001 - capture thread evidence.
                    failures.append(error)

            threads = [threading.Thread(target=save, args=(store,)) for store in stores]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            rows = stores[0].load_recent_signals("BTCUSDT", limit=10)

        self.assertEqual(failures, [])
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["occurrences"], 2)

    def test_save_signal_translates_full_by_write_class_and_preserves_other_errors(self):
        normal_capacity = capacity_for_bytes(0)

        def run(error, *, decision):
            with tempfile.TemporaryDirectory() as temp_dir:
                store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
                connection = mock.MagicMock()

                def execute(statement, *args):
                    if "insert into signal_audit" in " ".join(statement.split()).lower():
                        raise error
                    cursor = mock.MagicMock()
                    cursor.fetchone.return_value = None
                    return cursor

                connection.execute.side_effect = execute

                @contextmanager
                def failing_connect():
                    yield connection

                with mock.patch.object(store, "_connect", failing_connect), mock.patch(
                    "app.storage.capacity_from_connection", return_value=normal_capacity
                ):
                    return store.save_signal(
                        "BTCUSDT",
                        signal(direction="WAIT" if decision == "WAIT" else "LONG"),
                        decision=decision,
                        created_at_ms=1_000,
                    )

        self.assertFalse(run(sqlite3.OperationalError("database or disk is full"), decision="WAIT"))
        with self.assertRaises(CoreStorageCapacityError):
            run(sqlite3.OperationalError("database or disk is full"), decision="OPENED")
        with self.assertRaisesRegex(sqlite3.OperationalError, "syntax error"):
            run(sqlite3.OperationalError("syntax error"), decision="WAIT")

    def test_persists_order_entry_snapshot_and_updates_settlement(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            simulator = AccountSimulator()
            order = simulator.open_order(signal(), entry_price=100.0, opened_at=1_000)
            entry_snapshot = {
                "signal": signal().to_dict(),
                "rolling_edge": {"status": "NORMAL", "sample_size": 21, "win_rate": 0.619},
                "latest_kline": {"close": 100.0, "close_time": 1_000},
                "stake_config": {"stake": 10.0, "win_return": 18.0},
            }

            store.save_order_entry_snapshot(order, "BTCUSDT", entry_snapshot)
            simulator.settle_expired_orders(current_time=601_000, current_price=99.0)
            store.update_order_entry_snapshot_settlement(order, "BTCUSDT")
            rows = store.load_order_entry_snapshots("BTCUSDT")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "BTCUSDT")
        self.assertEqual(rows[0]["order_id"], 1)
        self.assertEqual(rows[0]["direction"], "LONG")
        self.assertEqual(rows[0]["threshold_segment"], "WD-12")
        self.assertEqual(rows[0]["result"], "LOSS")
        self.assertEqual(rows[0]["pnl"], -10.0)
        self.assertEqual(rows[0]["entry_payload"]["rolling_edge"]["sample_size"], 21)
        self.assertEqual(rows[0]["settlement_payload"]["exit_price"], 99.0)

    def test_pages_and_filters_orders_for_dashboard(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            fixtures = [
                SimulatedOrder(
                    id=1,
                    direction="LONG",
                    timeframe_minutes=10,
                    level="S",
                    reason="long win",
                    entry_price=100.0,
                    opened_at=1_000,
                    expires_at=601_000,
                    threshold_segment="WD-08",
                    status="SETTLED",
                    result="WIN",
                    exit_price=101.0,
                    settled_at=601_000,
                    pnl=8.0,
                ),
                SimulatedOrder(
                    id=2,
                    direction="SHORT",
                    timeframe_minutes=10,
                    level="A",
                    reason="short loss",
                    entry_price=100.0,
                    opened_at=2_000,
                    expires_at=602_000,
                    threshold_segment="WD-23",
                    status="SETTLED",
                    result="LOSS",
                    exit_price=101.0,
                    settled_at=602_000,
                    pnl=-10.0,
                ),
                SimulatedOrder(
                    id=3,
                    direction="SHORT",
                    timeframe_minutes=10,
                    level="S",
                    reason="short open",
                    entry_price=100.0,
                    opened_at=3_000,
                    expires_at=603_000,
                    threshold_segment="WD-23",
                    status="OPEN",
                    result=None,
                ),
            ]
            for order in fixtures:
                store.save_order(order, "BTCUSDT")
            for order_id in range(4, 14):
                store.save_order(
                    SimulatedOrder(
                        id=order_id,
                        direction="LONG",
                        timeframe_minutes=10,
                        level="B",
                        reason="page filler",
                        entry_price=100.0,
                        opened_at=order_id * 1_000,
                        expires_at=order_id * 1_000 + 600_000,
                        threshold_segment="WD-12",
                        status="SETTLED",
                        result="WIN",
                        exit_price=101.0,
                        settled_at=order_id * 1_000 + 600_000,
                        pnl=8.0,
                    ),
                    "BTCUSDT",
                )

            first_page = store.page_orders("BTCUSDT", page=1, page_size=10)
            filtered = store.page_orders(
                "BTCUSDT",
                page=1,
                page_size=10,
                direction="SHORT",
                level="S",
                segment="WD-23",
                result="OPEN",
            )

        self.assertEqual(first_page["total"], 13)
        self.assertEqual(first_page["page"], 1)
        self.assertEqual(first_page["page_size"], 10)
        self.assertEqual(first_page["total_pages"], 2)
        self.assertEqual([item["id"] for item in first_page["orders"]], [13, 12, 11, 10, 9, 8, 7, 6, 5, 4])
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["orders"][0]["id"], 3)
        self.assertEqual(filtered["orders"][0]["direction"], "SHORT")

    def test_persists_and_pages_observation_signals(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.save_observation(
                observation("1|10|SHORT|normal_down_short_extension_observe"),
                "BTCUSDT",
            )

            rows = store.load_observations("BTCUSDT")
            page = store.page_observations(
                "BTCUSDT",
                direction="SHORT",
                family="short_extension",
                tag="normal_down_short_extension_observe",
                segment="WD-02",
                result="WIN",
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].strategy_family, "short_extension")
        self.assertEqual(rows[0].result, "WIN")
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["observations"][0]["strategy_tag"], "normal_down_short_extension_observe")

    def test_persists_daily_profile_selection_idempotently_and_loads_effective_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            first = {
                "version": "DPS-1",
                "status": "READY",
                "evaluated_at": 900,
                "effective_from": 1_000,
                "effective_until": 2_000,
                "selected_profiles": [{"key": "10|family|tag|LONG|WD-01"}],
            }
            replacement = {
                **first,
                "version": "DPS-1-REPLACED",
                "selected_profiles": [{"key": "10|family|tag|SHORT|WD-01", "direction": "SHORT"}],
            }
            second = {
                "version": "DPS-2",
                "status": "READY",
                "evaluated_at": 1_900,
                "effective_from": 2_000,
                "effective_until": 3_000,
                "selected_profiles": [],
            }

            store.save_daily_profile_selection("BTCUSDT", first)
            store.save_daily_profile_selection("BTCUSDT", replacement)
            store.save_daily_profile_selection("BTCUSDT", second)

            active_first = store.load_daily_profile_selection("BTCUSDT", 1_500)
            active_second = store.load_daily_profile_selection("BTCUSDT", 2_500)
            latest = store.load_latest_daily_profile_selection("BTCUSDT")
            missing = store.load_daily_profile_selection("ETHUSDT", 1_500)

        self.assertEqual(active_first["version"], "DPS-1-REPLACED")
        self.assertEqual(active_first["selected_profiles"][0]["direction"], "SHORT")
        self.assertEqual(active_second["version"], "DPS-2")
        self.assertEqual(latest["version"], "DPS-2")
        self.assertIsNone(missing)

    def test_loads_complete_latest_observation_profile_window(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            for idx in range(650):
                store.save_observation(
                    observation(
                        f"profile-{idx}",
                        result="LOSS" if idx < 150 else "WIN",
                        opened_at=idx * 600_000,
                    ),
                    "BTCUSDT",
                )

            rows = store.load_observations_for_profile("BTCUSDT", lookback_days=7)

        self.assertEqual(len(rows), 650)
        self.assertEqual(sum(item.result == "LOSS" for item in rows), 150)

    def test_profile_restore_loads_one_day_buffer_for_scheduled_daily_cutoff(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            latest = 9 * 86_400_000
            store.save_observation(observation("latest", opened_at=latest), "BTCUSDT")
            store.save_observation(
                observation("cutoff-buffer", opened_at=latest - 7 * 86_400_000 - 12 * 60 * 60_000),
                "BTCUSDT",
            )
            store.save_observation(
                observation("too-old", opened_at=latest - 8 * 86_400_000 - 60_000),
                "BTCUSDT",
            )

            rows = store.load_observations_for_profile("BTCUSDT", lookback_days=7)

        self.assertEqual({item.observation_key for item in rows}, {"latest", "cutoff-buffer"})

    def test_summarizes_observation_signals_by_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            for idx in range(30):
                store.save_observation(
                    observation(
                        f"promote-{idx}",
                        result="WIN" if idx < 24 else "LOSS",
                        opened_at=idx * 1_000,
                    ),
                    "BTCUSDT",
                )
            for idx in range(35):
                store.save_observation(
                    observation(
                        f"block-{idx}",
                        family="low_rise_observe",
                        tag="low_volume_rise_observe",
                        direction="LONG",
                        segment="WD-18",
                        result="WIN" if idx < 15 else "LOSS",
                        opened_at=100_000 + idx * 1_000,
                    ),
                    "BTCUSDT",
                )
            store.save_observation(
                observation("open-1", status="OPEN", result=None, opened_at=200_000),
                "BTCUSDT",
            )

            summary = store.observation_summary("BTCUSDT")

        self.assertEqual(summary["total"]["signals"], 66)
        self.assertEqual(summary["total"]["settled"], 65)
        self.assertEqual(summary["total"]["open"], 1)
        self.assertEqual(summary["action_counts"]["PROMOTE_WATCH"], 1)
        self.assertEqual(summary["action_counts"]["BLOCK_WATCH"], 1)
        promote = next(item for item in summary["groups"] if item["strategy_family"] == "short_extension")
        blocked = next(item for item in summary["groups"] if item["strategy_family"] == "low_rise_observe")
        self.assertEqual(promote["direction"], "SHORT")
        self.assertEqual(promote["settled"], 30)
        self.assertEqual(promote["wins"], 24)
        self.assertEqual(promote["action"], "PROMOTE_WATCH")
        self.assertEqual(promote["confidence"], "MEDIUM")
        self.assertEqual(blocked["action"], "BLOCK_WATCH")
        self.assertLess(blocked["ev"], 0)

    def test_observation_queries_cover_rows_beyond_legacy_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.save_observations(
                [
                    observation(
                        f"row-{idx:04d}",
                        result="WIN" if idx % 2 == 0 else "LOSS",
                        opened_at=idx * 1_000,
                    )
                    for idx in range(5_101)
                ],
                "BTCUSDT",
            )

            summary = store.observation_summary("BTCUSDT", window="all")
            last_page = store.page_observations(
                "BTCUSDT", page=256, page_size=20
            )

        self.assertEqual(summary["total"]["settled"], 5_101)
        self.assertEqual(summary["total"]["signals"], 5_101)
        self.assertEqual(last_page["total"], 5_101)
        self.assertEqual(last_page["page"], 256)
        self.assertEqual(len(last_page["observations"]), 1)
        self.assertEqual(last_page["observations"][0]["observation_key"], "row-0000")

    def test_observation_summary_uses_stable_anchor_and_time_windows(self):
        day = 86_400_000
        anchor = 40 * day
        fixtures = [
            ("anchor", anchor),
            ("seven-edge", anchor - 7 * day),
            ("seven-old", anchor - 7 * day - 1),
            ("fourteen-edge", anchor - 14 * day),
            ("fourteen-old", anchor - 14 * day - 1),
            ("thirty-edge", anchor - 30 * day),
            ("thirty-old", anchor - 30 * day - 1),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.save_observations(
                [observation(key, opened_at=opened_at) for key, opened_at in fixtures],
                "BTCUSDT",
            )

            seven = store.observation_summary("BTCUSDT", window="7d")
            default = store.observation_summary("BTCUSDT")
            thirty = store.observation_summary("BTCUSDT", window="30d")
            all_rows = store.observation_summary("BTCUSDT", window="all")

        self.assertEqual(seven["total"]["signals"], 2)
        self.assertEqual(default["window"], "14d")
        self.assertEqual(default["total"]["signals"], 4)
        self.assertEqual(thirty["total"]["signals"], 6)
        self.assertEqual(all_rows["total"]["signals"], 7)
        self.assertEqual(seven["anchor"], anchor)
        self.assertEqual(seven["cutoff"], anchor - 7 * day)

    def test_observation_summary_keeps_legacy_positional_limit_with_keyword_window(self):
        day = 86_400_000
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.save_observations(
                [
                    observation("latest", opened_at=40 * day),
                    observation("old", opened_at=10 * day),
                ],
                "BTCUSDT",
            )

            legacy_default = store.observation_summary("BTCUSDT", 5_000)
            explicit_all = store.observation_summary(
                "BTCUSDT", 5_000, window="all"
            )

        self.assertEqual(legacy_default["window"], "14d")
        self.assertEqual(legacy_default["total"]["signals"], 1)
        self.assertEqual(explicit_all["window"], "all")
        self.assertEqual(explicit_all["total"]["signals"], 2)

    def test_observation_summary_preserves_zero_pnl_result_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.save_observations(
                [
                    replace(observation("win", result="WIN"), pnl=0.0),
                    replace(observation("loss", result="LOSS", opened_at=2_000), pnl=0.0),
                ],
                "BTCUSDT",
            )

            summary = store.observation_summary("BTCUSDT", window="all")

        self.assertEqual(summary["total"]["pnl"], -2.0)
        self.assertEqual(summary["total"]["ev"], -1.0)

    def test_observation_sql_filters_and_options_use_the_full_symbol_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.save_observations(
                [
                    observation(f"base-{idx}", opened_at=idx)
                    for idx in range(5_005)
                ]
                + [
                    observation(
                        "rare",
                        family="rare_family",
                        tag="rare_tag",
                        direction="LONG",
                        segment="WE-23",
                        status="OPEN",
                        result=None,
                        opened_at=-1,
                    )
                ],
                "BTCUSDT",
            )

            page = store.page_observations(
                "BTCUSDT",
                page=1,
                page_size=10,
                direction="LONG",
                family="rare_family",
                tag="rare_tag",
                segment="WE-23",
                result="OPEN",
            )

        self.assertEqual(page["total"], 1)
        self.assertEqual(page["observations"][0]["observation_key"], "rare")
        self.assertIn("RARE_FAMILY", page["filter_options"]["family"])
        self.assertIn("RARE_TAG", page["filter_options"]["tag"])
        self.assertIn("WE-23", page["filter_options"]["segment"])

    def test_summarizes_order_entry_snapshots_for_weak_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            for order_id in (1, 2):
                order = SimulatedOrder(
                    id=order_id,
                    direction="LONG",
                    timeframe_minutes=10,
                    level="A",
                    reason="放量急跌反抽：synthetic",
                    entry_price=100.0,
                    opened_at=order_id * 1_000,
                    expires_at=order_id * 1_000 + 600_000,
                    threshold_segment="WD-18",
                    score=85.0,
                    threshold=70.0,
                    status="SETTLED",
                    result="LOSS",
                    exit_price=99.0,
                    settled_at=order_id * 1_000 + 600_000,
                    pnl=-10.0,
                )
                entry_signal = signal()
                entry_signal = entry_signal.__class__(
                    **{
                        **entry_signal.to_dict(),
                        "level": "A",
                        "reason": "放量急跌反抽：synthetic",
                        "threshold_segment": "WD-18",
                        "price_change_pct": -0.0015,
                        "price_position": 0.45,
                        "rsi": 46.0,
                        "mtf_10m_bias": 0.1,
                        "mtf_30m_bias": 0.2,
                        "regime": "FEAR_FALLING",
                    }
                )
                store.save_order_entry_snapshot(
                    order,
                    "BTCUSDT",
                    {
                        "signal": entry_signal.to_dict(),
                        "fear_greed": {"value": 12, "trend": "falling"},
                        "profile_guard_shadow": {
                            "status": "WOULD_BLOCK",
                            "hit_keys": ["HIGH_RSI_REBOUND", "WEAK_SEGMENT_WD00_WD18_WD22"],
                            "active_keys": ["HIGH_RSI_REBOUND", "WEAK_SEGMENT_WD00_WD18_WD22"],
                            "min_history": 15,
                            "min_group_size": 2,
                            "variant": "recommended_key_subset",
                            "selection_policy": {
                                "name": "STABILITY_BAND",
                                "reason": "最高稳定分组合已满足稳定带",
                                "selected_keys": ["HIGH_RSI_REBOUND", "WEAK_SEGMENT_WD00_WD18_WD22"],
                                "score_best_keys": ["HIGH_RSI_REBOUND", "WEAK_SEGMENT_WD00_WD18_WD22"],
                            },
                        },
                        "profile_guard_selection_policy": {
                            "name": "STABILITY_BAND",
                            "reason": "最高稳定分组合已满足稳定带",
                            "selected_keys": ["HIGH_RSI_REBOUND", "WEAK_SEGMENT_WD00_WD18_WD22"],
                            "score_best_keys": ["HIGH_RSI_REBOUND", "WEAK_SEGMENT_WD00_WD18_WD22"],
                        },
                        "profile_guard_default_shadow": {
                            "status": "PASS",
                            "hit_keys": [],
                            "active_keys": ["HIGH_RSI_REBOUND", "WEAK_SEGMENT_WD00_WD18_WD22"],
                            "min_history": 15,
                            "min_group_size": 2,
                            "variant": "walk_forward_combined",
                        },
                    },
                )
                store.update_order_entry_snapshot_settlement(order, "BTCUSDT")

            summary = store.order_profile_summary("BTCUSDT")

        self.assertEqual(summary["total"]["orders"], 2)
        self.assertEqual(summary["total"]["losses"], 2)
        hint_names = {item["key"] for item in summary["risk_hints"]}
        self.assertIn("LEVEL_A_REBOUND", hint_names)
        self.assertIn("WEAK_SEGMENT_WD00_WD18_WD22", hint_names)
        self.assertIn("HIGH_RSI_REBOUND", hint_names)
        self.assertEqual(summary["profile_guard_shadow"]["would_block"]["orders"], 2)
        self.assertEqual(summary["profile_guard_shadow"]["would_block"]["losses"], 2)
        self.assertEqual(summary["profile_guard_policy"]["by_policy"][0]["key"], "STABILITY_BAND")
        self.assertEqual(summary["profile_guard_shadow_compare"]["recommended_block_default_pass"]["orders"], 2)

    def test_upgrades_legacy_database_and_restores_order_progression_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            legacy_order = progression_order(1).to_dict()
            legacy_order["threshold"] = 79.0
            legacy_order.pop("calculated_threshold")
            legacy_order.pop("stake_progression_source_order_id")
            legacy_order.pop("stake_progression_version")
            legacy_order.pop("profile_degradation_probe", None)
            legacy_order.pop("profile_degradation_triggered_at", None)
            for key in list(legacy_order):
                if key.startswith("wave_"):
                    legacy_order.pop(key)
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    """
                    create table orders (
                        symbol text not null,
                        order_id integer not null,
                        status text not null,
                        result text,
                        opened_at integer not null,
                        settled_at integer,
                        payload text not null,
                        updated_at_ms integer not null default (strftime('%s','now') * 1000),
                        primary key(symbol, order_id)
                    )
                    """
                )
                connection.execute(
                    """
                    insert into orders(symbol, order_id, status, result, opened_at, settled_at, payload)
                    values (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("BTCUSDT", 1, "OPEN", None, 1_000, None, json.dumps(legacy_order)),
                )

            store = SQLiteMonitorStore(db_path)
            restored = store.load_orders("BTCUSDT")
            with sqlite3.connect(db_path) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "select name from sqlite_master where type = 'table'"
                    )
                }
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        insert into stake_progression_runtime(
                            symbol, version, activated_at, enabled
                        ) values ('INVALID', 'V1', 0, 2)
                        """
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        insert into stake_progression_credits(
                            symbol, version, credit_id, source_order_id, status, created_at
                        ) values ('INVALID', 'V1', 'bad', 1, 'UNKNOWN', 0)
                        """
                    )

        self.assertIn("stake_progression_runtime", tables)
        self.assertIn("stake_progression_credits", tables)
        self.assertEqual(restored[0].stake_progression_source_order_id, None)
        self.assertEqual(restored[0].stake_progression_version, "")
        self.assertEqual(restored[0].wave_state, "UNKNOWN")
        self.assertEqual(restored[0].wave_batch_id, "")
        self.assertEqual(restored[0].wave_guard_mode, "NORMAL")
        self.assertEqual(restored[0].calculated_threshold, 79.0)
        self.assertIs(
            getattr(restored[0], "profile_degradation_probe", None),
            False,
        )
        self.assertEqual(
            getattr(restored[0], "profile_degradation_triggered_at", None),
            0,
        )

    def test_round_trips_all_credit_states_in_stable_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            credits = [
                StakeProgressionCredit(
                    source_order_id=3,
                    created_at=30,
                    credit_id="z-pending",
                    direction="SHORT",
                ),
                StakeProgressionCredit(
                    source_order_id=2,
                    created_at=10,
                    credit_id="b-consumed",
                    status="CONSUMED",
                    consumed_order_id=20,
                    consumed_at=40,
                    direction="LONG",
                ),
                StakeProgressionCredit(
                    source_order_id=1,
                    created_at=10,
                    credit_id="a-cancelled",
                    status="CANCELLED",
                    direction="SHORT",
                ),
            ]
            for credit in credits:
                store.save_stake_progression_credit("BTCUSDT", credit)

            restored = store.load_stake_progression_credits("btcusdt")

        self.assertEqual(
            [credit.credit_id for credit in restored],
            ["a-cancelled", "b-consumed", "z-pending"],
        )
        self.assertEqual(
            [credit.to_dict() for credit in restored],
            [credits[2].to_dict(), credits[1].to_dict(), credits[0].to_dict()],
        )

    def test_upgrades_legacy_progression_credit_table_with_direction_column(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    """
                    create table stake_progression_credits (
                        symbol text not null,
                        version text not null,
                        credit_id text not null,
                        source_order_id integer not null,
                        status text not null,
                        created_at integer not null,
                        consumed_order_id integer,
                        consumed_at integer,
                        primary key(symbol, version, source_order_id),
                        unique(symbol, version, credit_id),
                        unique(symbol, version, consumed_order_id)
                    )
                    """
                )
                connection.execute(
                    """
                    insert into stake_progression_credits(
                        symbol, version, credit_id, source_order_id, status, created_at
                    ) values ('BTCUSDT', ?, 'legacy:1', 1, 'PENDING', 1000)
                    """,
                    (TWO_STAGE_VERSION,),
                )

            store = SQLiteMonitorStore(db_path)
            restored = store.load_stake_progression_credits("BTCUSDT")

        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].direction, "")

    def test_prepare_runtime_preserves_activation_boundary_across_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            first = SQLiteMonitorStore(db_path)

            initial = first.prepare_stake_progression(
                "BTCUSDT", TWO_STAGE_VERSION, True, 1_000
            )
            restarted = SQLiteMonitorStore(db_path)
            restored = restarted.prepare_stake_progression(
                "btcusdt", TWO_STAGE_VERSION, True, 9_000
            )

        self.assertEqual(initial, 1_000)
        self.assertEqual(restored, 1_000)

    def test_disable_then_enable_cancels_pending_and_sets_new_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.prepare_stake_progression("BTCUSDT", TWO_STAGE_VERSION, True, 1_000)
            store.save_stake_progression_credit(
                "BTCUSDT",
                StakeProgressionCredit(source_order_id=1, created_at=1_100),
            )

            disabled_at = store.prepare_stake_progression(
                "BTCUSDT", TWO_STAGE_VERSION, False, 2_000
            )
            disabled_again_at = store.prepare_stake_progression(
                "BTCUSDT", TWO_STAGE_VERSION, False, 3_000
            )
            enabled_at = store.prepare_stake_progression(
                "BTCUSDT", TWO_STAGE_VERSION, True, 4_000
            )
            restored = store.load_stake_progression_credits("BTCUSDT")

        self.assertEqual((disabled_at, disabled_again_at, enabled_at), (1_000, 1_000, 4_000))
        self.assertEqual(restored[0].status, "CANCELLED")

    def test_version_change_cancels_all_pending_and_replaces_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.prepare_stake_progression("BTCUSDT", TWO_STAGE_VERSION, True, 1_000)
            store.save_stake_progression_credit(
                "BTCUSDT",
                StakeProgressionCredit(source_order_id=1, created_at=1_100),
            )

            activated_at = store.prepare_stake_progression(
                "BTCUSDT", "TWO_STAGE_V2", True, 2_000
            )
            old_credits = store.load_stake_progression_credits(
                "BTCUSDT", TWO_STAGE_VERSION
            )

        self.assertEqual(activated_at, 2_000)
        self.assertEqual(old_credits[0].status, "CANCELLED")

    def test_prepare_and_cancellation_are_isolated_by_symbol(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            for symbol, source_order_id in (("BTCUSDT", 1), ("ETHUSDT", 2)):
                store.prepare_stake_progression(symbol, TWO_STAGE_VERSION, True, 1_000)
                store.save_stake_progression_credit(
                    symbol,
                    StakeProgressionCredit(
                        source_order_id=source_order_id,
                        created_at=1_100,
                    ),
                )

            store.prepare_stake_progression("BTCUSDT", TWO_STAGE_VERSION, False, 2_000)
            btc = store.load_stake_progression_credits("BTCUSDT")
            eth = store.load_stake_progression_credits("ETHUSDT")
            eth_activation = store.prepare_stake_progression(
                "ETHUSDT", TWO_STAGE_VERSION, True, 9_000
            )

        self.assertEqual(btc[0].status, "CANCELLED")
        self.assertEqual(eth[0].status, "PENDING")
        self.assertEqual(eth_activation, 1_000)

    def test_prepare_rejects_invalid_runtime_parameters(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            invalid = [
                ("", TWO_STAGE_VERSION, True, 0),
                ("BTCUSDT", " ", True, 0),
                ("BTCUSDT", TWO_STAGE_VERSION, True, -1),
            ]
            for args in invalid:
                with self.subTest(args=args), self.assertRaises(ValueError):
                    store.prepare_stake_progression(*args)

    def test_saves_settled_order_and_credit_atomically(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            order = progression_order(1, status="SETTLED")
            credit = StakeProgressionCredit(
                source_order_id=order.id,
                created_at=order.settled_at,
            )

            store.save_settled_order_with_credit(order, "BTCUSDT", credit)
            store.save_settled_order_with_credit(order, "BTCUSDT", credit)
            orders = store.load_orders("BTCUSDT")
            credits = store.load_stake_progression_credits("BTCUSDT")

        self.assertEqual([item.id for item in orders], [1])
        self.assertEqual([item.to_dict() for item in credits], [credit.to_dict()])

    def test_saves_open_second_stage_order_and_consumption_idempotently(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            pending = StakeProgressionCredit(source_order_id=1, created_at=1_000)
            consumed = StakeProgressionCredit(
                source_order_id=1,
                created_at=1_000,
                status="CONSUMED",
                consumed_order_id=2,
                consumed_at=2_000,
            )
            order = progression_order(2, step=2, source_order_id=1)
            store.save_stake_progression_credit("BTCUSDT", pending)

            store.save_open_order_with_credit(order, "BTCUSDT", consumed)
            store.save_open_order_with_credit(order, "BTCUSDT", consumed)
            orders = store.load_orders("BTCUSDT")
            credits = store.load_stake_progression_credits("BTCUSDT")

        self.assertEqual([item.id for item in orders], [2])
        self.assertEqual(credits[0].status, "CONSUMED")
        self.assertEqual(credits[0].consumed_order_id, 2)

    def test_atomic_methods_reject_invalid_credit_links_without_partial_orders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            settled_order = progression_order(10, status="SETTLED")
            invalid_settlements = [
                StakeProgressionCredit(source_order_id=11, created_at=1_000),
                StakeProgressionCredit(
                    source_order_id=10,
                    created_at=1_000,
                    status="CONSUMED",
                    consumed_order_id=20,
                    consumed_at=2_000,
                ),
                StakeProgressionCredit(
                    source_order_id=10,
                    created_at=1_000,
                    version="OTHER_VERSION",
                ),
            ]
            for index, credit in enumerate(invalid_settlements):
                symbol = f"SETTLED{index}"
                with self.subTest(kind="settled", index=index), self.assertRaises(ValueError):
                    store.save_settled_order_with_credit(settled_order, symbol, credit)
                self.assertEqual(store.load_orders(symbol), [])

            open_order = progression_order(20, step=2, source_order_id=10)
            invalid_opens = [
                StakeProgressionCredit(source_order_id=10, created_at=1_000),
                StakeProgressionCredit(
                    source_order_id=10,
                    created_at=1_000,
                    status="CONSUMED",
                    consumed_order_id=21,
                    consumed_at=2_000,
                ),
                StakeProgressionCredit(
                    source_order_id=11,
                    created_at=1_000,
                    status="CONSUMED",
                    consumed_order_id=20,
                    consumed_at=2_000,
                ),
                StakeProgressionCredit(
                    source_order_id=10,
                    created_at=1_000,
                    version="OTHER_VERSION",
                    status="CONSUMED",
                    consumed_order_id=20,
                    consumed_at=2_000,
                ),
                None,
            ]
            for index, credit in enumerate(invalid_opens):
                symbol = f"OPEN{index}"
                with self.subTest(kind="open", index=index), self.assertRaises(ValueError):
                    store.save_open_order_with_credit(open_order, symbol, credit)
                self.assertEqual(store.load_orders(symbol), [])

            base_order = progression_order(30, version="")
            store.save_open_order_with_credit(base_order, "BASE", None)
            base_orders = store.load_orders("BASE")

        self.assertEqual(base_orders[0].id, 30)

    def test_credit_upsert_failure_rolls_back_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            order = progression_order(1, status="SETTLED")
            credit = StakeProgressionCredit(source_order_id=1, created_at=601_000)

            with mock.patch.object(
                store,
                "_upsert_progression_credit",
                side_effect=RuntimeError("credit write failed"),
                create=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "credit write failed"):
                    store.save_settled_order_with_credit(order, "BTCUSDT", credit)

            self.assertEqual(store.load_orders("BTCUSDT"), [])

    def test_atomic_methods_reject_cross_direction_credit_links(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            settled_long = progression_order(1, status="SETTLED")
            wrong_settlement_credit = StakeProgressionCredit(
                source_order_id=settled_long.id,
                created_at=settled_long.settled_at,
                direction="SHORT",
            )

            with self.assertRaisesRegex(ValueError, "direction"):
                store.save_settled_order_with_credit(
                    settled_long,
                    "BTCUSDT",
                    wrong_settlement_credit,
                )

            pending = StakeProgressionCredit(
                source_order_id=10,
                created_at=1_000,
                direction="LONG",
            )
            consumed = replace(
                pending,
                status="CONSUMED",
                consumed_order_id=2,
                consumed_at=2_000,
            )
            short_order = replace(
                progression_order(2, step=2, source_order_id=10),
                direction="SHORT",
            )
            store.save_stake_progression_credit("BTCUSDT", pending)

            with self.assertRaisesRegex(ValueError, "direction"):
                store.save_open_order_with_credit(short_order, "BTCUSDT", consumed)
            restored = store.load_stake_progression_credits("BTCUSDT")

        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].status, "PENDING")
        self.assertEqual(restored[0].direction, "LONG")

    def test_settlement_credit_rejects_second_stage_order_without_partial_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            order = progression_order(
                2,
                status="SETTLED",
                step=2,
                source_order_id=1,
            )
            credit = StakeProgressionCredit(source_order_id=2, created_at=601_000)

            with self.assertRaises(ValueError):
                store.save_settled_order_with_credit(order, "BTCUSDT", credit)

            self.assertEqual(store.load_orders("BTCUSDT"), [])
            self.assertEqual(store.load_stake_progression_credits("BTCUSDT"), [])

    def test_open_credit_rejects_base_order_without_partial_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            order = progression_order(2, step=1, source_order_id=1)
            credit = StakeProgressionCredit(
                source_order_id=1,
                created_at=1_000,
                status="CONSUMED",
                consumed_order_id=2,
                consumed_at=2_000,
            )

            with self.assertRaises(ValueError):
                store.save_open_order_with_credit(order, "BTCUSDT", credit)

            self.assertEqual(store.load_orders("BTCUSDT"), [])
            self.assertEqual(store.load_stake_progression_credits("BTCUSDT"), [])

    def test_consumed_credit_rejects_competing_order_without_partial_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            original = StakeProgressionCredit(
                source_order_id=1,
                created_at=1_000,
                credit_id="credit-1",
                status="CONSUMED",
                consumed_order_id=2,
                consumed_at=2_000,
            )
            store.save_open_order_with_credit(
                progression_order(2, step=2, source_order_id=1),
                "BTCUSDT",
                original,
            )
            competing = StakeProgressionCredit(
                source_order_id=1,
                created_at=1_000,
                credit_id="credit-1",
                status="CONSUMED",
                consumed_order_id=3,
                consumed_at=3_000,
            )

            with self.assertRaises(ValueError):
                store.save_open_order_with_credit(
                    progression_order(3, step=2, source_order_id=1),
                    "BTCUSDT",
                    competing,
                )

            orders = store.load_orders("BTCUSDT")
            credits = store.load_stake_progression_credits("BTCUSDT")

        self.assertEqual([order.id for order in orders], [2])
        self.assertEqual(credits[0].to_dict(), original.to_dict())

    def test_cancelled_credit_rejects_consumption_without_partial_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            cancelled = StakeProgressionCredit(
                source_order_id=1,
                created_at=1_000,
                credit_id="credit-1",
                status="CANCELLED",
            )
            store.save_stake_progression_credit("BTCUSDT", cancelled)
            competing = StakeProgressionCredit(
                source_order_id=1,
                created_at=1_000,
                credit_id="credit-1",
                status="CONSUMED",
                consumed_order_id=3,
                consumed_at=3_000,
            )

            with self.assertRaises(ValueError):
                store.save_open_order_with_credit(
                    progression_order(3, step=2, source_order_id=1),
                    "BTCUSDT",
                    competing,
                )

            self.assertEqual(store.load_orders("BTCUSDT"), [])
            credits = store.load_stake_progression_credits("BTCUSDT")

        self.assertEqual(credits[0].to_dict(), cancelled.to_dict())

    def test_late_settlement_pending_snapshot_preserves_consumed_credit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            consumed = StakeProgressionCredit(
                source_order_id=1,
                created_at=601_000,
                status="CONSUMED",
                consumed_order_id=2,
                consumed_at=602_000,
            )
            store.save_stake_progression_credit("BTCUSDT", consumed)
            late_pending = StakeProgressionCredit(
                source_order_id=1,
                created_at=601_000,
            )

            store.save_settled_order_with_credit(
                progression_order(1, status="SETTLED"),
                "BTCUSDT",
                late_pending,
            )
            orders = store.load_orders("BTCUSDT")
            credits = store.load_stake_progression_credits("BTCUSDT")

        self.assertEqual([order.id for order in orders], [1])
        self.assertEqual(credits[0].to_dict(), consumed.to_dict())

    def test_late_pending_snapshot_cannot_downgrade_consumed_credit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            consumed = StakeProgressionCredit(
                source_order_id=1,
                created_at=1_000,
                status="CONSUMED",
                consumed_order_id=2,
                consumed_at=2_000,
            )
            late_pending = StakeProgressionCredit(
                source_order_id=1,
                created_at=1_000,
            )

            store.save_stake_progression_credit("BTCUSDT", consumed)
            store.save_stake_progression_credit("BTCUSDT", late_pending)
            restored = store.load_stake_progression_credits("BTCUSDT")

        self.assertEqual(restored[0].status, "CONSUMED")
        self.assertEqual(restored[0].consumed_order_id, 2)
        self.assertEqual(restored[0].consumed_at, 2_000)

    def test_credit_terminal_states_cannot_transition(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            consumed = StakeProgressionCredit(
                source_order_id=1,
                created_at=1_000,
                status="CONSUMED",
                consumed_order_id=2,
                consumed_at=2_000,
            )
            cancelled = StakeProgressionCredit(
                source_order_id=1,
                created_at=1_000,
                status="CANCELLED",
            )
            store.save_stake_progression_credit("CONSUMED", consumed)
            store.save_stake_progression_credit("CONSUMED", cancelled)
            store.save_stake_progression_credit("CANCELLED", cancelled)
            store.save_stake_progression_credit("CANCELLED", consumed)

            still_consumed = store.load_stake_progression_credits("CONSUMED")
            still_cancelled = store.load_stake_progression_credits("CANCELLED")

        self.assertEqual(still_consumed[0].status, "CONSUMED")
        self.assertEqual(still_cancelled[0].status, "CANCELLED")


if __name__ == "__main__":
    unittest.main()
