import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.shadow_storage_schema import (
    SHADOW_SCHEMA_VERSION,
    ShadowSchemaConflictError,
    UnsupportedShadowSchemaVersionError,
    migrate_shadow_schema,
)


EXPECTED_TABLES = {
    "shadow_parameter_snapshots",
    "shadow_experiments",
    "shadow_arms",
    "shadow_event_cursors",
    "shadow_event_gaps",
    "shadow_decisions",
    "shadow_orders",
    "shadow_observations",
    "shadow_daily_rollups",
    "shadow_evaluations",
    "shadow_lifecycle_history",
    "shadow_runtime_state",
    "shadow_formal_policy_receipts",
    "shadow_decision_rollups",
}


class ShadowStorageSchemaTests(unittest.TestCase):
    def test_migrate_creates_only_the_shadow_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            shadow_path = Path(temp_dir) / "monitor.shadow.sqlite3"
            formal_path = Path(temp_dir) / "monitor.sqlite3"
            with closing(sqlite3.connect(formal_path)) as formal:
                formal.execute("create table formal_sentinel(value text not null)")
                formal.execute("insert into formal_sentinel values ('unchanged')")
                formal.commit()

            with closing(sqlite3.connect(shadow_path)) as connection:
                migrate_shadow_schema(connection)
                tables = {
                    row[0]
                    for row in connection.execute(
                        "select name from sqlite_master where type='table'"
                    )
                    if not row[0].startswith("sqlite_")
                }
                version = connection.execute("pragma user_version").fetchone()[0]
                foreign_keys = connection.execute("pragma foreign_keys").fetchone()[0]

            self.assertEqual(tables, EXPECTED_TABLES)
            self.assertEqual(version, SHADOW_SCHEMA_VERSION)
            self.assertEqual(foreign_keys, 1)
            with closing(sqlite3.connect(formal_path)) as formal:
                self.assertEqual(
                    formal.execute("select value from formal_sentinel").fetchone()[0],
                    "unchanged",
                )
                self.assertEqual(formal.execute("pragma user_version").fetchone()[0], 0)

    def test_migrate_is_idempotent_and_preserves_rows(self):
        with closing(sqlite3.connect(":memory:")) as connection:
            migrate_shadow_schema(connection)
            connection.execute(
                """
                insert into shadow_parameter_snapshots(
                    parameter_hash, analyzer_hash, parameter_family,
                    canonical_payload, payload_bytes, created_at_ms
                ) values ('p1', 'a1', 'PROFILE', '{}', 2, 1000)
                """
            )
            migrate_shadow_schema(connection)
            count = connection.execute(
                "select count(*) from shadow_parameter_snapshots"
            ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_v1_database_adds_observations_without_losing_existing_rows(self):
        with closing(sqlite3.connect(":memory:")) as connection:
            migrate_shadow_schema(connection)
            connection.execute(
                """
                insert into shadow_parameter_snapshots(
                    parameter_hash, analyzer_hash, parameter_family,
                    canonical_payload, payload_bytes, created_at_ms
                ) values ('p1', 'a1', 'PROFILE', '{}', 2, 1000)
                """
            )
            connection.execute("drop table shadow_observations")
            connection.execute("pragma user_version = 1")

            migrate_shadow_schema(connection)

            self.assertEqual(
                connection.execute("pragma user_version").fetchone()[0],
                SHADOW_SCHEMA_VERSION,
            )
            self.assertIsNotNone(
                connection.execute(
                    "select name from sqlite_master where name='shadow_observations'"
                ).fetchone()
            )
            self.assertEqual(
                connection.execute(
                    "select count(*) from shadow_parameter_snapshots"
                ).fetchone()[0],
                1,
            )

    def test_v2_database_adds_formal_policy_receipts_without_losing_rows(self):
        with closing(sqlite3.connect(":memory:")) as connection:
            migrate_shadow_schema(connection)
            connection.execute(
                """
                insert into shadow_parameter_snapshots(
                    parameter_hash, analyzer_hash, parameter_family,
                    canonical_payload, payload_bytes, created_at_ms
                ) values ('p1', 'a1', 'PROFILE', '{}', 2, 1000)
                """
            )
            connection.execute("drop table shadow_formal_policy_receipts")
            connection.execute("drop table shadow_decision_rollups")
            connection.execute("pragma user_version = 2")

            migrate_shadow_schema(connection)

            self.assertEqual(
                connection.execute("pragma user_version").fetchone()[0],
                SHADOW_SCHEMA_VERSION,
            )
            self.assertIsNotNone(
                connection.execute(
                    "select name from sqlite_master "
                    "where name='shadow_formal_policy_receipts'"
                ).fetchone()
            )
            self.assertIsNotNone(
                connection.execute(
                    "select name from sqlite_master "
                    "where name='shadow_decision_rollups'"
                ).fetchone()
            )
            self.assertEqual(
                connection.execute(
                    "select count(*) from shadow_parameter_snapshots"
                ).fetchone()[0],
                1,
            )

    def test_newer_schema_version_is_rejected_without_changes(self):
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute(
                f"pragma user_version = {SHADOW_SCHEMA_VERSION + 1}"
            )
            connection.execute("create table future_table(value text)")
            with self.assertRaises(UnsupportedShadowSchemaVersionError):
                migrate_shadow_schema(connection)
            self.assertIsNotNone(
                connection.execute(
                    "select name from sqlite_master where name='future_table'"
                ).fetchone()
            )

    def test_conflicting_owned_table_is_rejected_atomically(self):
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute(
                "create table shadow_orders(arm_id text, wrong_column text)"
            )
            with self.assertRaises(ShadowSchemaConflictError):
                migrate_shadow_schema(connection)
            self.assertEqual(connection.execute("pragma user_version").fetchone()[0], 0)
            self.assertIsNone(
                connection.execute(
                    "select name from sqlite_master "
                    "where name='shadow_parameter_snapshots'"
                ).fetchone()
            )

    def test_schema_enforces_arm_scoped_event_and_order_idempotency(self):
        with closing(sqlite3.connect(":memory:")) as connection:
            migrate_shadow_schema(connection)
            decision_indexes = connection.execute(
                "pragma index_list(shadow_decisions)"
            ).fetchall()
            order_indexes = connection.execute(
                "pragma index_list(shadow_orders)"
            ).fetchall()
            observation_indexes = connection.execute(
                "pragma index_list(shadow_observations)"
            ).fetchall()
            self.assertTrue(any(row[2] for row in decision_indexes))
            self.assertTrue(any(row[2] for row in order_indexes))
            self.assertTrue(any(row[2] for row in observation_indexes))

    def test_observation_schema_contains_reconstruction_columns(self):
        expected = (
            "arm_id",
            "observation_key",
            "decision_event_id",
            "strategy_family",
            "strategy_tag",
            "direction",
            "timeframe_minutes",
            "level",
            "reason",
            "entry_price",
            "opened_at",
            "expires_at",
            "threshold_segment",
            "status",
            "result",
            "exit_price",
            "settled_at",
            "pnl",
            "profile_key",
            "detail_payload",
            "immutable_hash",
            "compacted_at_ms",
            "updated_at_ms",
        )
        with closing(sqlite3.connect(":memory:")) as connection:
            migrate_shadow_schema(connection)
            actual = tuple(
                row[1]
                for row in connection.execute(
                    "pragma table_xinfo(shadow_observations)"
                )
            )
        self.assertEqual(actual, expected)

    def test_table_with_right_columns_but_wrong_primary_key_is_rejected(self):
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute(
                """
                create table shadow_event_cursors (
                    arm_id text,
                    last_event_id text not null,
                    last_closed_at_ms integer not null,
                    last_bundle_hash text not null,
                    gap_count integer not null default 0,
                    updated_at_ms integer not null
                )
                """
            )
            with self.assertRaises(ShadowSchemaConflictError):
                migrate_shadow_schema(connection)


if __name__ == "__main__":
    unittest.main()
