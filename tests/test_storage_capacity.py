import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import FrozenInstanceError
from pathlib import Path

from app.storage import SQLiteMonitorStore
from app.storage_capacity import (
    COMPACT_ONLY_BYTES,
    CORE_RESERVE_BYTES,
    MAX_DATABASE_BYTES,
    WARNING_BYTES,
    CoreStorageCapacityError,
    OrdinaryAuditCapacityError,
    StorageCapacity,
    StorageCapacityConfigurationError,
    StorageWriteClass,
    capacity_for_bytes,
    classify_capacity,
    configure_max_page_count,
    ensure_write_allowed,
    raise_for_sqlite_write_error,
)


class CapacityBoundaryTest(unittest.TestCase):
    def test_capacity_constants_are_exact_binary_sizes(self):
        self.assertEqual(MAX_DATABASE_BYTES, 3 * 1024**3)
        self.assertEqual(WARNING_BYTES, int(2.5 * 1024**3))
        self.assertEqual(COMPACT_ONLY_BYTES, int(2.75 * 1024**3))
        self.assertEqual(CORE_RESERVE_BYTES, 256 * 1024**2)
        self.assertEqual(MAX_DATABASE_BYTES - COMPACT_ONLY_BYTES, CORE_RESERVE_BYTES)

    def test_capacity_status_uses_exact_boundaries(self):
        cases = (
            (0, "NORMAL"),
            (WARNING_BYTES - 1, "NORMAL"),
            (WARNING_BYTES, "WARNING"),
            (COMPACT_ONLY_BYTES - 1, "WARNING"),
            (COMPACT_ONLY_BYTES, "COMPACT_ONLY"),
            (MAX_DATABASE_BYTES - 1, "COMPACT_ONLY"),
            (MAX_DATABASE_BYTES, "HARD_LIMIT"),
            (MAX_DATABASE_BYTES + 1, "HARD_LIMIT"),
        )
        for database_bytes, expected in cases:
            with self.subTest(database_bytes=database_bytes):
                self.assertEqual(classify_capacity(database_bytes), expected)

    def test_negative_database_size_is_rejected(self):
        with self.assertRaises(ValueError):
            classify_capacity(-1)

    def test_capacity_snapshot_is_frozen_and_exposes_contract_fields(self):
        capacity = capacity_for_bytes(WARNING_BYTES)

        self.assertEqual(
            capacity,
            StorageCapacity(
                status="WARNING",
                database_bytes=WARNING_BYTES,
                max_database_bytes=MAX_DATABASE_BYTES,
                core_reserve_bytes=CORE_RESERVE_BYTES,
                ordinary_audit_allowed=True,
                core_write_allowed=True,
            ),
        )
        with self.assertRaises(FrozenInstanceError):
            capacity.status = "NORMAL"

    def test_ordinary_audit_preserves_core_reserve(self):
        before_reserve = capacity_for_bytes(COMPACT_ONLY_BYTES - 1)
        at_reserve = capacity_for_bytes(COMPACT_ONLY_BYTES)

        self.assertTrue(before_reserve.ordinary_audit_allowed)
        self.assertTrue(before_reserve.core_write_allowed)
        self.assertFalse(at_reserve.ordinary_audit_allowed)
        self.assertTrue(at_reserve.core_write_allowed)

    def test_core_writes_stop_only_at_hard_limit(self):
        below_limit = capacity_for_bytes(MAX_DATABASE_BYTES - 1)
        at_limit = capacity_for_bytes(MAX_DATABASE_BYTES)

        self.assertTrue(below_limit.core_write_allowed)
        self.assertFalse(below_limit.ordinary_audit_allowed)
        self.assertFalse(at_limit.core_write_allowed)
        self.assertFalse(at_limit.ordinary_audit_allowed)

    def test_write_allowance_distinguishes_ordinary_audit_and_core(self):
        compact = capacity_for_bytes(COMPACT_ONLY_BYTES)

        with self.assertRaises(OrdinaryAuditCapacityError) as audit_error:
            ensure_write_allowed(compact, StorageWriteClass.ORDINARY_AUDIT)
        self.assertEqual(audit_error.exception.capacity, compact)
        self.assertEqual(
            audit_error.exception.write_class,
            StorageWriteClass.ORDINARY_AUDIT,
        )
        ensure_write_allowed(compact, StorageWriteClass.CORE)

        hard_limit = capacity_for_bytes(MAX_DATABASE_BYTES)
        with self.assertRaises(CoreStorageCapacityError) as core_error:
            ensure_write_allowed(hard_limit, StorageWriteClass.CORE)
        self.assertEqual(core_error.exception.capacity, hard_limit)
        self.assertEqual(core_error.exception.write_class, StorageWriteClass.CORE)


class SQLiteCapacityIntegrationTest(unittest.TestCase):
    @staticmethod
    def pragma(path: Path, name: str) -> int:
        with closing(sqlite3.connect(path)) as connection:
            return int(connection.execute(f"pragma {name}").fetchone()[0])

    def test_store_sets_max_page_count_from_actual_default_page_size(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(path)
            page_size = self.pragma(path, "page_size")

            with store._connect() as connection:
                effective = connection.execute(
                    "pragma max_page_count"
                ).fetchone()[0]
            self.assertEqual(effective, MAX_DATABASE_BYTES // page_size)
            capacity = store.storage_capacity()
            self.assertEqual(
                capacity.database_bytes,
                self.pragma(path, "page_count") * page_size,
            )
            self.assertEqual(capacity.status, "NORMAL")

    def test_store_uses_non_default_page_size_for_cap(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "monitor.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("pragma page_size = 8192")
                connection.execute("create table seed(value integer)")
                connection.commit()

            self.assertEqual(self.pragma(path, "page_size"), 8192)
            store = SQLiteMonitorStore(path)
            with store._connect() as connection:
                effective = connection.execute(
                    "pragma max_page_count"
                ).fetchone()[0]
            self.assertEqual(effective, MAX_DATABASE_BYTES // 8192)

    def test_every_store_connection_restores_the_page_cap(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(path)
            page_size = self.pragma(path, "page_size")
            expected = MAX_DATABASE_BYTES // page_size
            with store._connect() as first_connection:
                first = first_connection.execute(
                    "pragma max_page_count"
                ).fetchone()[0]
            with store._connect() as second_connection:
                second = second_connection.execute(
                    "pragma max_page_count"
                ).fetchone()[0]

            self.assertEqual(first, expected)
            self.assertEqual(second, expected)

    def test_configure_max_page_count_verifies_effective_value(self):
        class WrongEffectiveConnection:
            def execute(self, statement):
                values = {
                    "pragma page_size": 8192,
                    f"pragma max_page_count = {MAX_DATABASE_BYTES // 8192}": 7,
                    "pragma max_page_count": 7,
                }

                class Result:
                    def __init__(self, value):
                        self.value = value

                    def fetchone(self):
                        return (self.value,)

                return Result(values[statement])

        with self.assertRaises(StorageCapacityConfigurationError):
            configure_max_page_count(WrongEffectiveConnection())


class SQLiteFullTranslationTest(unittest.TestCase):
    def test_sqlite_full_error_code_becomes_core_capacity_failure(self):
        error = sqlite3.OperationalError("opaque sqlite failure")
        error.sqlite_errorcode = sqlite3.SQLITE_FULL

        with self.assertRaises(CoreStorageCapacityError) as raised:
            raise_for_sqlite_write_error(error, StorageWriteClass.CORE)

        self.assertIs(raised.exception.__cause__, error)
        self.assertIsNone(raised.exception.capacity)

    def test_sqlite_full_message_fallback_becomes_capacity_failure(self):
        error = sqlite3.OperationalError(
            "database or disk is full (SQLITE_FULL)"
        )

        with self.assertRaises(OrdinaryAuditCapacityError) as raised:
            raise_for_sqlite_write_error(
                error,
                StorageWriteClass.ORDINARY_AUDIT,
            )

        self.assertIs(raised.exception.__cause__, error)

    def test_non_sqlite_full_message_is_preserved(self):
        error = RuntimeError("database or disk is full")

        with self.assertRaises(RuntimeError) as raised:
            raise_for_sqlite_write_error(error, StorageWriteClass.CORE)

        self.assertIs(raised.exception, error)

    def test_non_full_sqlite_error_is_preserved(self):
        error = sqlite3.OperationalError("database is locked")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY

        with self.assertRaises(sqlite3.OperationalError) as raised:
            raise_for_sqlite_write_error(error, StorageWriteClass.CORE)

        self.assertIs(raised.exception, error)

    def test_non_full_error_code_wins_over_full_like_message(self):
        error = sqlite3.OperationalError("database or disk is full")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY

        with self.assertRaises(sqlite3.OperationalError) as raised:
            raise_for_sqlite_write_error(error, StorageWriteClass.CORE)

        self.assertIs(raised.exception, error)


if __name__ == "__main__":
    unittest.main()
