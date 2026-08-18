import json
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
from unittest import mock

from app import order_profile
from app.models import SimulatedOrder
from app.state import strategy_source_build_id
from app.storage import SQLiteMonitorStore
from app.storage_capacity import capacity_for_bytes
from app.storage_schema import SCHEMA_VERSION, SchemaConflictError
from tests.test_storage import atomic_bundle_fixture, signal


class _FetchHookCursor:
    def __init__(self, cursor, hook):
        self._cursor = cursor
        self._hook = hook

    def fetchone(self):
        row = self._cursor.fetchone()
        self._hook()
        return row


class _FreshnessHookConnection:
    def __init__(self, connection, commit_between_reads):
        self._connection = connection
        self._commit_between_reads = commit_between_reads
        self._committed = False

    def execute(self, sql, parameters=()):
        normalized = " ".join(sql.lower().split())
        legacy_revision_read = normalized.startswith(
            "select revision from profile_summary_revisions where symbol = ?"
        )
        if not legacy_revision_read and "profile_summary_revisions" in normalized:
            self._commit_once()
        cursor = self._connection.execute(sql, parameters)
        if legacy_revision_read:
            return _FetchHookCursor(cursor, self._commit_once)
        return cursor

    def _commit_once(self):
        if self._committed:
            return
        self._committed = True
        self._commit_between_reads()


class Task7ProfileMaterializationReviewTest(unittest.TestCase):
    @staticmethod
    def _seed_profile_rows(
        store: SQLiteMonitorStore,
        *,
        count: int,
    ) -> SimulatedOrder:
        entry_payload = json.dumps({"signal": signal().to_dict()})
        rows = []
        for order_id in range(1, count + 1):
            is_open = order_id == count
            won = order_id % 3 != 0
            rows.append(
                (
                    "BTCUSDT",
                    order_id,
                    "LONG",
                    10,
                    order_id * 60_000,
                    order_id * 60_000 + 600_000,
                    100.0,
                    10.0,
                    18.0,
                    1,
                    "WD-12",
                    "FEAR_RISING",
                    82.0,
                    70.0,
                    12.0,
                    None if is_open else ("WIN" if won else "LOSS"),
                    None if is_open else order_id * 60_000 + 600_000,
                    None if is_open else (101.0 if won else 99.0),
                    0.0 if is_open else (8.0 if won else -10.0),
                    entry_payload,
                    None,
                )
            )
        with store._connect() as connection:
            connection.executemany(
                """
                insert into order_entry_snapshots(
                    symbol, order_id, direction, timeframe_minutes, opened_at,
                    expires_at, entry_price, stake, win_return,
                    stake_progression_step, threshold_segment, regime, score,
                    threshold, edge, result, settled_at, exit_price, pnl,
                    entry_payload, settlement_payload
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            store._bump_profile_summary_revision(connection, "BTCUSDT")
        open_order = SimulatedOrder(
            id=count,
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="5000 row formal guard",
            entry_price=100.0,
            opened_at=count * 60_000,
            expires_at=count * 60_000 + 600_000,
            threshold_segment="WD-12",
            score=82.0,
            threshold=70.0,
            regime="FEAR_RISING",
        )
        store.save_order(open_order, "BTCUSDT")
        return open_order

    def test_freshness_and_payload_are_read_from_one_sqlite_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            reader = SQLiteMonitorStore(db_path)
            writer = SQLiteMonitorStore(db_path)
            key = reader._profile_summary_key("BTCUSDT", 5000, 15, 2)
            with reader._connect() as connection:
                reader._bump_profile_summary_revision(connection, "BTCUSDT")
            self.assertTrue(
                reader._write_profile_summary_materialization(
                    key,
                    1,
                    {"snapshot_count": 1, "marker": "revision-1"},
                )
            )

            def commit_new_revision():
                with writer._connect() as connection:
                    writer._bump_profile_summary_revision(connection, "BTCUSDT")

            original_connect = reader._connect

            @contextmanager
            def hooked_connect():
                with original_connect() as connection:
                    yield _FreshnessHookConnection(connection, commit_new_revision)

            with mock.patch.object(reader, "_connect", hooked_connect):
                with mock.patch.object(
                    reader,
                    "_schedule_profile_summary_rebuild",
                    return_value=None,
                ):
                    snapshot = reader.profile_summary_snapshot("BTCUSDT")

            self.assertEqual(snapshot["current_revision"], 2)
            self.assertEqual(snapshot["source_revision"], 1)
            self.assertEqual(snapshot["cache_status"], "STALE")
            self.assertTrue(snapshot["stale"])
            reader.close()
            writer.close()

    def test_formal_guard_atomic_freshness_never_returns_old_ready_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            reader = SQLiteMonitorStore(db_path)
            writer = SQLiteMonitorStore(db_path)
            key = reader._profile_summary_key("BTCUSDT", 5000, 15, 2)
            with reader._connect() as connection:
                reader._bump_profile_summary_revision(connection, "BTCUSDT")
            reader._write_profile_summary_materialization(
                key,
                1,
                {"profile_guard": {"marker": "revision-1"}},
            )
            committed = False

            def commit_new_revision():
                nonlocal committed
                if committed:
                    return
                committed = True
                with writer._connect() as connection:
                    writer._bump_profile_summary_revision(connection, "BTCUSDT")

            original_connect = reader._connect

            @contextmanager
            def hooked_connect():
                with original_connect() as connection:
                    yield _FreshnessHookConnection(connection, commit_new_revision)

            with (
                mock.patch.object(reader, "_connect", hooked_connect),
                mock.patch.object(
                    reader,
                    "_schedule_profile_summary_rebuild",
                    return_value=None,
                ),
                self.assertRaisesRegex(RuntimeError, "not ready"),
            ):
                reader.exact_order_profile_summary("BTCUSDT")
            self.assertEqual(writer.profile_summary_revision("BTCUSDT"), 2)
            reader.close()
            writer.close()

    def test_open_snapshot_does_not_carry_exact_guard_when_limit_evicts_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            first = SimulatedOrder(
                id=1,
                direction="LONG",
                timeframe_minutes=10,
                level="A",
                reason="window first",
                entry_price=100.0,
                opened_at=1_000,
                expires_at=601_000,
                status="SETTLED",
                result="LOSS",
                settled_at=601_000,
                exit_price=99.0,
                pnl=-10.0,
            )
            second = replace(
                first,
                id=2,
                opened_at=2_000,
                expires_at=602_000,
                settled_at=602_000,
                result="WIN",
                exit_price=101.0,
                pnl=8.0,
            )
            for order in (first, second):
                store.save_order_entry_snapshot(
                    order,
                    "BTCUSDT",
                    {"signal": signal().to_dict()},
                )
                store.update_order_entry_snapshot_settlement(order, "BTCUSDT")
            store.prepare_order_profile_summary("BTCUSDT", limit=2)
            store.wait_for_profile_summary_rebuilds(timeout=10)
            third = replace(
                first,
                id=3,
                opened_at=3_000,
                expires_at=603_000,
                status="OPEN",
                result=None,
                settled_at=None,
                exit_price=None,
                pnl=0.0,
            )
            with mock.patch.object(
                store,
                "_schedule_profile_summary_rebuild",
                return_value=None,
            ):
                store.save_order_entry_snapshot(
                    third,
                    "BTCUSDT",
                    {"signal": signal().to_dict()},
                )
                with self.assertRaisesRegex(RuntimeError, "not ready"):
                    store.exact_order_profile_summary("BTCUSDT", limit=2)
            store.close()

    def test_algorithm_fingerprint_is_part_of_materialization_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            first = SQLiteMonitorStore(
                db_path,
                profile_algorithm_fingerprint="order-profile-v1",
            )
            order = SimulatedOrder(
                id=1,
                direction="LONG",
                timeframe_minutes=10,
                level="A",
                reason="algorithm identity",
                entry_price=100.0,
                opened_at=1_000,
                expires_at=601_000,
            )
            first.save_order_entry_snapshot(
                order,
                "BTCUSDT",
                {"signal": signal().to_dict()},
            )
            first.prepare_order_profile_summary("BTCUSDT")
            first.wait_for_profile_summary_rebuilds(timeout=10)

            second = SQLiteMonitorStore(
                db_path,
                profile_algorithm_fingerprint="order-profile-v2",
            )
            with mock.patch.object(
                second,
                "_schedule_profile_summary_rebuild",
                return_value=None,
            ):
                preparing = second.profile_summary_snapshot("BTCUSDT")
            self.assertEqual(preparing["cache_status"], "PREPARING")
            self.assertIsNone(preparing["source_revision"])

            second.prepare_order_profile_summary("BTCUSDT")
            second.wait_for_profile_summary_rebuilds(timeout=10)
            with closing(sqlite3.connect(db_path)) as connection:
                versions = connection.execute(
                    "select algorithm_fingerprint from profile_summary_materializations "
                    "where symbol = ? order by algorithm_fingerprint",
                    ("BTCUSDT",),
                ).fetchall()
            self.assertEqual(versions, [("order-profile-v1",), ("order-profile-v2",)])
            first.close()
            second.close()

    def test_profile_materialization_is_schema_v3_managed(self):
        self.assertEqual(SCHEMA_VERSION, 3)
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            store.close()
            with closing(sqlite3.connect(db_path)) as connection:
                self.assertEqual(connection.execute("pragma user_version").fetchone()[0], 3)
                columns = {
                    row[1]
                    for row in connection.execute(
                        "pragma table_info(profile_summary_materializations)"
                    )
                }
            self.assertIn("summary_schema_version", columns)
            self.assertIn("algorithm_fingerprint", columns)

    def test_compact_only_rejects_rebuildable_materialization_then_recovers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            with mock.patch(
                "app.storage.capacity_from_connection",
                return_value=capacity_for_bytes(int(2.8 * 1024**3)),
            ):
                first = store.prepare_order_profile_summary("BTCUSDT")
                store.wait_for_profile_summary_rebuilds(timeout=10)
                blocked = store.profile_summary_snapshot("BTCUSDT")

            self.assertEqual(first["cache_status"], "PREPARING")
            self.assertIn(blocked["cache_status"], {"PREPARING", "STALE"})
            store.profile_summary_snapshot("BTCUSDT")
            store.wait_for_profile_summary_rebuilds(timeout=10)
            self.assertEqual(
                store.profile_summary_snapshot("BTCUSDT")["cache_status"],
                "READY",
            )
            store.close()

    def test_cross_store_rebuild_uses_one_database_lease(self):
        class LeaseStore(SQLiteMonitorStore):
            compute_calls = 0
            compute_started = threading.Event()
            compute_release = threading.Event()
            compute_lock = threading.Lock()

            def _compute_profile_summary(self, key, samples):
                with self.compute_lock:
                    type(self).compute_calls += 1
                    type(self).compute_started.set()
                type(self).compute_release.wait(timeout=5)
                return super()._compute_profile_summary(key, samples)

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            first = LeaseStore(db_path)
            second = LeaseStore(db_path)
            first.prepare_order_profile_summary("BTCUSDT")
            self.assertTrue(LeaseStore.compute_started.wait(timeout=5))
            second.prepare_order_profile_summary("BTCUSDT")
            time.sleep(0.1)
            self.assertEqual(LeaseStore.compute_calls, 1)
            LeaseStore.compute_release.set()
            first.wait_for_profile_summary_rebuilds(timeout=10)
            second.wait_for_profile_summary_rebuilds(timeout=10)
            self.assertEqual(LeaseStore.compute_calls, 1)
            first.close()
            second.close()

    def test_expired_database_lease_is_recovered_without_waiting_full_lease_ttl(self):
        class CountingStore(SQLiteMonitorStore):
            def __init__(self, path):
                self.compute_calls = 0
                super().__init__(path)

            def _compute_profile_summary(self, key, samples):
                self.compute_calls += 1
                return super()._compute_profile_summary(key, samples)

        with tempfile.TemporaryDirectory() as temp_dir:
            store = CountingStore(Path(temp_dir) / "monitor.sqlite3")
            key = store._profile_summary_key("BTCUSDT", 5000, 15, 2)
            with store._connect() as connection:
                connection.execute(
                    """
                    insert into profile_summary_leases(
                        symbol, summary_schema_version, algorithm_fingerprint,
                        snapshot_limit, profile_guard_min_history,
                        profile_guard_min_group_size, source_revision,
                        owner_id, expires_at_ms
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (*key, 0, "crashed-owner", int(time.time() * 1000) + 100),
                )
            store.prepare_order_profile_summary("BTCUSDT")
            store.wait_for_profile_summary_rebuilds(timeout=3)
            self.assertEqual(store.compute_calls, 1)
            self.assertEqual(
                store.profile_summary_snapshot("BTCUSDT")["cache_status"],
                "READY",
            )
            store.close()

    def test_profile_rebuild_cas_retry_count_is_bounded(self):
        class LosingCasStore(SQLiteMonitorStore):
            def __init__(self, path):
                self.compute_calls = 0
                super().__init__(path)

            def _compute_profile_summary(self, key, samples):
                self.compute_calls += 1
                return super()._compute_profile_summary(key, samples)

            def _write_profile_summary_materialization(self, *args, **kwargs):
                return False

        with tempfile.TemporaryDirectory() as temp_dir:
            store = LosingCasStore(Path(temp_dir) / "monitor.sqlite3")
            store.prepare_order_profile_summary("BTCUSDT")
            store.wait_for_profile_summary_rebuilds(timeout=5)
            self.assertEqual(store.compute_calls, 4)
            store.close(timeout=0.5)

    def test_materialization_rows_are_bounded_across_algorithm_versions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            for version in range(40):
                store.profile_algorithm_fingerprint = f"algorithm-{version:02d}"
                key = store._profile_summary_key("BTCUSDT", 5000, 15, 2)
                self.assertTrue(
                    store._write_profile_summary_materialization(
                        key,
                        0,
                        {"profile_guard": {}, "version": version},
                    )
                )
            with closing(sqlite3.connect(db_path)) as connection:
                full_rows = connection.execute(
                    "select count(*) from profile_summary_materializations"
                ).fetchone()[0]
                guard_rows = connection.execute(
                    "select count(*) from profile_guard_materializations"
                ).fetchone()[0]
            self.assertEqual(full_rows, 32)
            self.assertEqual(guard_rows, 32)
            store.close()

    def test_store_rejects_unbounded_profile_parameter_combinations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            with mock.patch.object(
                store,
                "_schedule_profile_summary_rebuild",
                return_value=None,
            ):
                for limit in range(1, 17):
                    store.profile_summary_snapshot("BTCUSDT", limit=limit)
                with self.assertRaisesRegex(ValueError, "parameter combinations"):
                    store.profile_summary_snapshot("BTCUSDT", limit=17)
            store.close()

    def test_close_has_a_bounded_timeout_and_cancels_worker_retry(self):
        class BlockingStore(SQLiteMonitorStore):
            def __init__(self, path):
                self.compute_started = threading.Event()
                self.compute_release = threading.Event()
                super().__init__(path)

            def _compute_profile_summary(self, key, samples):
                self.compute_started.set()
                self.compute_release.wait(timeout=5)
                return super()._compute_profile_summary(key, samples)

        with tempfile.TemporaryDirectory() as temp_dir:
            store = BlockingStore(Path(temp_dir) / "monitor.sqlite3")
            store.prepare_order_profile_summary("BTCUSDT")
            self.assertTrue(store.compute_started.wait(timeout=5))
            started = time.monotonic()
            store.close(timeout=0.1)
            elapsed = time.monotonic() - started
            store.compute_release.set()
            self.assertLess(elapsed, 1.0)

    def test_exact_guard_never_computes_full_summary_on_calling_thread(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            calling_thread = threading.get_ident()
            original = store._compute_profile_summary

            def assert_background(key, samples):
                self.assertNotEqual(threading.get_ident(), calling_thread)
                return original(key, samples)

            with mock.patch.object(
                store,
                "_compute_profile_summary",
                side_effect=assert_background,
            ):
                exact = store.exact_order_profile_summary("BTCUSDT")
            self.assertEqual(exact["cache_status"], "READY")
            store.close()

    def test_background_guard_payload_matches_full_algorithm_fields(self):
        samples = [
            {
                **order_profile.sample_from_signal(signal()),
                "order_id": index,
                "opened_at": index * 60_000,
                "result": "WIN" if index % 3 else "LOSS",
                "pnl": 8.0 if index % 3 else -10.0,
            }
            for index in range(1, 45)
        ]
        expected = order_profile.summarize_order_samples_with_guard(samples)[
            "profile_guard"
        ]
        actual = order_profile.summarize_profile_guard_materialization(samples)
        self.assertEqual(actual, expected)

    def test_formal_guard_settlement_promotes_precomputed_exact_branch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            open_order = self._seed_profile_rows(store, count=5000)
            store.prepare_order_profile_summary("BTCUSDT")
            store.wait_for_profile_summary_rebuilds(timeout=30)

            settled = replace(
                open_order,
                status="SETTLED",
                result="WIN",
                settled_at=open_order.expires_at,
                exit_price=101.0,
                pnl=8.0,
            )
            key = store._profile_summary_key("BTCUSDT", 5000, 15, 2)
            _revision, samples = store._profile_summary_rebuild_input(key)
            expected_samples = [dict(sample) for sample in samples]
            expected_samples[-1].update(
                {
                    "result": "WIN",
                    "pnl": 8.0,
                    "settled_at": settled.settled_at,
                    "exit_price": settled.exit_price,
                }
            )
            expected_guard = order_profile.summarize_profile_guard_materialization(
                expected_samples,
                min_history=15,
                min_group_size=2,
            )

            calling_thread = threading.get_ident()
            load_threads = []
            summarize_threads = []
            original_load = store._profile_summary_rebuild_input
            original_summarize = store._compute_profile_summary

            def tracked_load(*args, **kwargs):
                load_threads.append(threading.get_ident())
                return original_load(*args, **kwargs)

            def tracked_summarize(*args, **kwargs):
                summarize_threads.append(threading.get_ident())
                return original_summarize(*args, **kwargs)

            with (
                mock.patch.object(
                    store,
                    "_profile_summary_rebuild_input",
                    side_effect=tracked_load,
                ),
                mock.patch.object(
                    store,
                    "_compute_profile_summary",
                    side_effect=tracked_summarize,
                ),
            ):
                started = time.monotonic()
                store.save_settled_order_with_credit(
                    settled,
                    "BTCUSDT",
                    None,
                )
                exact = store.exact_order_profile_summary("BTCUSDT")
                elapsed = time.monotonic() - started

            self.assertNotIn(calling_thread, load_threads)
            self.assertNotIn(calling_thread, summarize_threads)
            self.assertLess(elapsed, 0.75)
            self.assertEqual(exact["profile_guard"], expected_guard)
            self.assertEqual(exact["source_revision"], 2)
            self.assertEqual(exact["current_revision"], 2)
            store.close()

    def test_two_sequential_settlements_promote_exact_conditional_guard_branches(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            second = self._seed_profile_rows(store, count=100)
            first = replace(
                second,
                id=99,
                opened_at=99 * 60_000,
                expires_at=99 * 60_000 + 600_000,
            )
            with store._connect() as connection:
                connection.execute(
                    """
                    update order_entry_snapshots
                    set result = null, settled_at = null, exit_price = null,
                        pnl = 0.0
                    where symbol = 'BTCUSDT' and order_id = 99
                    """
                )
            store.save_order(first, "BTCUSDT")
            store.prepare_order_profile_summary("BTCUSDT")
            store.wait_for_profile_summary_rebuilds(timeout=30)

            settled_first = replace(
                first,
                status="SETTLED",
                result="LOSS",
                settled_at=first.expires_at,
                exit_price=99.0,
                pnl=-10.0,
            )
            settled_second = replace(
                second,
                status="SETTLED",
                result="WIN",
                settled_at=second.expires_at,
                exit_price=101.0,
                pnl=8.0,
            )
            with mock.patch.object(
                store,
                "_schedule_profile_summary_rebuild",
                return_value=None,
            ):
                store.save_settled_order_with_credit(
                    settled_first,
                    "BTCUSDT",
                    None,
                )
                store.save_settled_order_with_credit(
                    settled_second,
                    "BTCUSDT",
                    None,
                )
                exact = store.exact_order_profile_summary("BTCUSDT")
            key = store._profile_summary_key("BTCUSDT", 5000, 15, 2)
            revision, samples = store._profile_summary_rebuild_input(key)
            expected = order_profile.summarize_profile_guard_materialization(
                samples,
                min_history=15,
                min_group_size=2,
            )
            self.assertEqual(revision, 3)
            self.assertEqual(exact["source_revision"], 3)
            self.assertEqual(exact["profile_guard"], expected)
            store.close()


class Task7DecisionLifecycleReviewTest(unittest.TestCase):
    @staticmethod
    def _downgrade_profile_tables_to_v2(db_path: Path) -> None:
        with closing(sqlite3.connect(db_path)) as connection:
            connection.execute("drop table profile_summary_leases")
            connection.execute("drop table profile_guard_settlement_successors")
            connection.execute("drop table profile_guard_settlement_branches")
            connection.execute("drop table profile_guard_materializations")
            connection.execute("drop table profile_summary_materializations")
            connection.execute(
                """
                create table profile_summary_materializations (
                    symbol text not null,
                    snapshot_limit integer not null,
                    profile_guard_min_history integer not null,
                    profile_guard_min_group_size integer not null,
                    source_revision integer not null check(source_revision >= 0),
                    payload text not null,
                    updated_at_ms integer not null default (strftime('%s','now') * 1000),
                    primary key(
                        symbol, snapshot_limit, profile_guard_min_history,
                        profile_guard_min_group_size
                    )
                )
                """
            )
            connection.execute(
                "drop index ux_signal_audit_symbol_decision_id"
            )
            connection.execute(
                "drop index ux_observation_signals_symbol_decision_id"
            )
            connection.execute("pragma user_version = 2")
            connection.commit()

    def test_v2_profile_cache_upgrades_without_losing_core_rows_or_revision(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            order = SimulatedOrder(
                id=1,
                direction="LONG",
                timeframe_minutes=10,
                level="A",
                reason="v2 migration",
                entry_price=100.0,
                opened_at=1_000,
                expires_at=601_000,
            )
            store.save_order_entry_snapshot(
                order,
                "BTCUSDT",
                {"signal": signal().to_dict()},
            )
            store.close()
            self._downgrade_profile_tables_to_v2(db_path)
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute(
                    """
                    insert into profile_summary_materializations(
                        symbol, snapshot_limit, profile_guard_min_history,
                        profile_guard_min_group_size, source_revision, payload
                    ) values ('BTCUSDT', 5000, 15, 2, 1, '{"legacy":true}')
                    """
                )
                connection.commit()

            upgraded = SQLiteMonitorStore(db_path)
            with mock.patch.object(
                upgraded,
                "_schedule_profile_summary_rebuild",
                return_value=None,
            ):
                snapshot = upgraded.profile_summary_snapshot("BTCUSDT")
            with closing(sqlite3.connect(db_path)) as connection:
                version = connection.execute("pragma user_version").fetchone()[0]
                entry_count = connection.execute(
                    "select count(*) from order_entry_snapshots"
                ).fetchone()[0]
                revision = connection.execute(
                    "select revision from profile_summary_revisions where symbol = 'BTCUSDT'"
                ).fetchone()[0]
                old_payloads = connection.execute(
                    "select count(*) from profile_summary_materializations"
                ).fetchone()[0]
            self.assertEqual(version, 3)
            self.assertEqual((entry_count, revision, old_payloads), (1, 1, 0))
            self.assertEqual(snapshot["cache_status"], "PREPARING")
            upgraded.close()

    def test_v2_malformed_profile_materialization_primary_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            store.close()
            self._downgrade_profile_tables_to_v2(db_path)
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute("drop table profile_summary_materializations")
                connection.execute(
                    """
                    create table profile_summary_materializations(
                        symbol text primary key,
                        snapshot_limit integer not null,
                        source_revision integer not null,
                        payload text not null
                    )
                    """
                )
                connection.commit()
            with self.assertRaisesRegex(
                SchemaConflictError,
                "profile_summary_materializations",
            ):
                SQLiteMonitorStore(db_path)

    def test_v2_duplicate_audit_decision_lifecycle_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            config, context, order, audit, entry_snapshot, observed = (
                atomic_bundle_fixture()
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
            store.close()
            self._downgrade_profile_tables_to_v2(db_path)
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute(
                    """
                    insert into signal_audit(
                        symbol, created_at_ms, decision, direction,
                        timeframe_minutes, threshold_segment, regime, score,
                        threshold, reason, payload, record_version, decision_id,
                        runtime_config_hash, event_kind, first_at_ms, last_at_ms,
                        occurrences, score_min, score_max, aggregation_key
                    )
                    select symbol, created_at_ms + 1, decision, direction,
                           timeframe_minutes, threshold_segment, regime, score,
                           threshold, reason, payload, record_version, decision_id,
                           runtime_config_hash, 'CHANGED_KIND', first_at_ms,
                           last_at_ms, occurrences, score_min, score_max, null
                    from signal_audit
                    """
                )
                connection.commit()
            with self.assertRaisesRegex(
                SchemaConflictError,
                "signal_audit.*duplicate decision_id",
            ):
                SQLiteMonitorStore(db_path)

    def test_one_decision_cannot_create_a_second_audit_or_observation_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            config, context, order, audit, entry_snapshot, observed = (
                atomic_bundle_fixture()
            )
            arguments = {
                "config": config,
                "context": context,
                "order": order,
                "credit": None,
                "entry_snapshot": entry_snapshot,
                "audit": audit,
                "observation": observed,
            }
            store.save_open_order_decision(**arguments)

            with self.assertRaisesRegex(ValueError, "decision audit"):
                store.save_open_order_decision(
                    **{**arguments, "audit": replace(audit, event_kind="CHANGED_KIND")}
                )
            with self.assertRaisesRegex(ValueError, "decision observation"):
                store.save_open_order_decision(
                    **{
                        **arguments,
                        "observation": replace(
                            observed,
                            observation_key="changed-observation-key",
                        ),
                    }
                )

            with closing(sqlite3.connect(db_path)) as connection:
                audit_count = connection.execute(
                    "select count(*) from signal_audit where decision_id = ?",
                    (context.decision_id,),
                ).fetchone()[0]
                observation_count = connection.execute(
                    "select count(*) from observation_signals where decision_id = ?",
                    (context.decision_id,),
                ).fetchone()[0]
            self.assertEqual((audit_count, observation_count), (1, 1))
            store.close()

    def test_open_bundle_replay_preserves_settled_order_and_observation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            config, context, order, audit, entry_snapshot, observed = (
                atomic_bundle_fixture()
            )
            arguments = {
                "config": config,
                "context": context,
                "order": order,
                "credit": None,
                "entry_snapshot": entry_snapshot,
                "audit": audit,
                "observation": observed,
            }
            store.save_open_order_decision(**arguments)
            settled_order = replace(
                order,
                status="SETTLED",
                result="WIN",
                settled_at=order.expires_at,
                exit_price=101.0,
                pnl=8.0,
            )
            settled_observation = replace(
                observed,
                status="SETTLED",
                result="WIN",
                settled_at=observed.expires_at,
                exit_price=101.0,
                pnl=8.0,
            )
            store.save_settled_order_with_credit(
                settled_order,
                context.symbol,
                None,
            )
            store.save_observation(settled_observation, context.symbol)

            store.save_open_order_decision(**arguments)

            restored_order = store.load_orders(context.symbol)[0]
            restored_observation = store.load_observations(context.symbol)[0]
            self.assertEqual(restored_order.status, "SETTLED")
            self.assertEqual(restored_order.result, "WIN")
            self.assertEqual(restored_observation.status, "SETTLED")
            self.assertEqual(restored_observation.result, "WIN")
            store.close()

    def test_corrupt_v3_decision_unique_index_is_rejected_at_initialization(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            store.close()
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute("drop index ux_signal_audit_symbol_decision_id")
                connection.execute(
                    "create index ux_signal_audit_symbol_decision_id "
                    "on signal_audit(symbol, decision_id)"
                )
                connection.commit()
            with self.assertRaises(SchemaConflictError):
                SQLiteMonitorStore(db_path)


class Task7BuildAndUiReviewTest(unittest.TestCase):
    def test_default_source_fingerprint_recursively_covers_every_python_dependency(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "app"
            nested = root / "nested"
            generated = root / "__pycache__"
            nested.mkdir(parents=True)
            generated.mkdir()
            (root / "strategy.py").write_text("RULE = 1\n", encoding="utf-8")
            dependency = nested / "indicators.py"
            dependency.write_text("WINDOW = 14\n", encoding="utf-8")
            (generated / "ignored.py").write_text("CACHE = 1\n", encoding="utf-8")

            initial = strategy_source_build_id(source_root=root)
            stable = strategy_source_build_id(source_root=root)
            dependency.write_text("WINDOW = 21\n", encoding="utf-8")
            changed = strategy_source_build_id(source_root=root)
            (generated / "ignored.py").write_text("CACHE = 2\n", encoding="utf-8")
            ignored = strategy_source_build_id(source_root=root)

            self.assertEqual(initial, stable)
            self.assertNotEqual(initial, changed)
            self.assertEqual(changed, ignored)

    def test_order_profile_ui_distinguishes_preparing_stale_and_ready_revisions(self):
        app_js = (Path(__file__).parents[1] / "app/static/app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("profileCacheStatusLabel", app_js)
        self.assertIn('PREPARING: "准备中"', app_js)
        self.assertIn('STALE: "已陈旧"', app_js)
        self.assertIn('READY: "已就绪"', app_js)
        self.assertIn("source_revision", app_js)
        self.assertIn("current_revision", app_js)

    def test_order_profile_ui_renders_cache_state_without_mislabeling_data(self):
        app_js = Path(__file__).parents[1] / "app/static/app.js"
        script = r"""
const fs = require("fs");
const vm = require("vm");
const elements = new Map();
const element = (id) => {
  if (!elements.has(id)) elements.set(id, {
    id, textContent: "", innerHTML: "", value: "", className: "",
    disabled: false, addEventListener: () => {},
  });
  return elements.get(id);
};
const context = {
  console, URLSearchParams, Set, Object, Array, Number, String, Math, Date,
  document: { getElementById: element },
  fetch: () => new Promise(() => {}),
  setInterval: () => 0,
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8") +
  "\nthis.renderOrderProfileForTest = renderOrderProfile;" +
  "\nthis.renderObservationSummaryForTest = renderObservationSummary;", context);
context.renderObservationSummaryForTest({
  total: { signals: 3, settled: 2, win_rate: 0.5, ev: -1 },
  groups: [], action_counts: {},
});
const snapshots = [
  { cache_status: "PREPARING", source_revision: null, current_revision: 2 },
  { cache_status: "STALE", source_revision: 1, current_revision: 2,
    total: { orders: 1, win_rate: 1, ev: 8 }, risk_hints: [] },
  { cache_status: "READY", source_revision: 2, current_revision: 2,
    total: { orders: 1, win_rate: 1, ev: 8 }, risk_hints: [] },
];
const rendered = snapshots.map((snapshot) => {
  context.renderOrderProfileForTest(snapshot);
  return {
    info: element("order-profile-info").textContent,
    table: element("order-profile-summary").innerHTML,
    observationInfo: element("observation-summary-info").textContent,
  };
});
process.stdout.write(JSON.stringify(rendered));
"""
        completed = subprocess.run(
            ["node", "-e", script, str(app_js)],
            check=True,
            capture_output=True,
            text=True,
        )
        preparing, stale, ready = json.loads(completed.stdout)
        self.assertIn("准备中", preparing["info"])
        self.assertIn("正在准备", preparing["table"])
        self.assertIn("准备中", preparing["observationInfo"])
        self.assertIn("已陈旧", stale["info"])
        self.assertIn("旧版本", stale["table"])
        self.assertIn("摘要版本 1 / 当前版本 2", stale["info"])
        self.assertIn("摘要版本 1 / 当前版本 2", stale["observationInfo"])
        self.assertIn("已就绪", ready["info"])
        self.assertNotIn("旧版本", ready["table"])


if __name__ == "__main__":
    unittest.main()
