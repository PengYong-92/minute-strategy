import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.models import SimulatedOrder
from app.storage import SQLiteMonitorStore

try:
    from app.storage_schema import SCHEMA_VERSION, migrate
except ModuleNotFoundError:
    SCHEMA_VERSION = None
    migrate = None


ORDER_PAYLOAD = '{"legacy":"order","raw":"\\u0000"}'
OBSERVATION_PAYLOAD = b"\x00\xfflegacy-observation"
ENTRY_PAYLOAD = '{"entry":[1,2,3]}'
SETTLEMENT_PAYLOAD = b"\x10\x11settled"
AUDIT_PAYLOAD = '{"audit": "exact spacing"}'


def _create_legacy_schema(
    connection: sqlite3.Connection,
    *,
    order_decision_id_declaration: str | None = None,
) -> None:
    order_extra = (
        f", decision_id {order_decision_id_declaration}"
        if order_decision_id_declaration
        else ""
    )
    connection.executescript(
        f"""
        create table orders (
            symbol text not null,
            order_id integer not null,
            status text not null,
            result text,
            opened_at integer not null,
            settled_at integer,
            payload text not null,
            updated_at_ms integer not null default (strftime('%s','now') * 1000)
            {order_extra},
            primary key(symbol, order_id)
        );

        create table observation_signals (
            symbol text not null,
            observation_key text not null,
            status text not null,
            result text,
            direction text not null,
            strategy_family text not null,
            strategy_tag text not null,
            timeframe_minutes integer not null,
            threshold_segment text not null,
            opened_at integer not null,
            expires_at integer not null,
            settled_at integer,
            payload text not null,
            created_at_ms integer not null default (strftime('%s','now') * 1000),
            updated_at_ms integer not null default (strftime('%s','now') * 1000),
            primary key(symbol, observation_key)
        );

        create table order_entry_snapshots (
            symbol text not null,
            order_id integer not null,
            direction text not null,
            timeframe_minutes integer not null,
            opened_at integer not null,
            expires_at integer not null,
            entry_price real not null,
            stake real not null,
            win_return real not null,
            stake_progression_step integer not null,
            threshold_segment text not null,
            regime text not null,
            score real not null,
            threshold real not null,
            edge real not null,
            result text,
            settled_at integer,
            exit_price real,
            pnl real not null default 0.0,
            entry_payload text not null,
            settlement_payload text,
            created_at_ms integer not null default (strftime('%s','now') * 1000),
            updated_at_ms integer not null default (strftime('%s','now') * 1000),
            primary key(symbol, order_id)
        );

        create table signal_audit (
            id integer primary key autoincrement,
            symbol text not null,
            created_at_ms integer not null,
            decision text not null,
            direction text not null,
            timeframe_minutes integer not null,
            threshold_segment text not null,
            regime text not null,
            score real not null,
            threshold real not null,
            reason text not null,
            payload text not null
        );
        """
    )


def _create_runtime_config_snapshots(
    connection: sqlite3.Connection,
    *,
    hash_declaration: str = "runtime_config_hash text primary key",
    context_declaration: str = "context_version text not null",
    table_constraint: str | None = None,
    table_options: str = "",
) -> None:
    columns = [
        hash_declaration,
        context_declaration,
        "strategy_build_id text not null",
        "canonical_payload text not null",
        "payload_bytes integer not null",
        "created_at_ms integer not null",
    ]
    if table_constraint is not None:
        columns.append(table_constraint)
    connection.execute(
        "create table runtime_config_snapshots ("
        + ", ".join(columns)
        + f") {table_options}"
    )


def _insert_legacy_sentinels(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        insert into orders(
            symbol, order_id, status, result, opened_at, settled_at, payload
        ) values (?, ?, ?, ?, ?, ?, ?)
        """,
        ("BTCUSDT", 7, "OPEN", None, 1_000, None, ORDER_PAYLOAD),
    )
    connection.execute(
        """
        insert into observation_signals(
            symbol, observation_key, status, result, direction,
            strategy_family, strategy_tag, timeframe_minutes,
            threshold_segment, opened_at, expires_at, settled_at, payload
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "BTCUSDT",
            "legacy-observation",
            "OPEN",
            None,
            "LONG",
            "legacy",
            "sentinel",
            10,
            "WD-00",
            1_000,
            601_000,
            None,
            sqlite3.Binary(OBSERVATION_PAYLOAD),
        ),
    )
    connection.execute(
        """
        insert into order_entry_snapshots(
            symbol, order_id, direction, timeframe_minutes, opened_at,
            expires_at, entry_price, stake, win_return,
            stake_progression_step, threshold_segment, regime, score,
            threshold, edge, result, settled_at, exit_price, pnl,
            entry_payload, settlement_payload
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "BTCUSDT",
            7,
            "LONG",
            10,
            1_000,
            601_000,
            100.0,
            10.0,
            18.0,
            1,
            "WD-00",
            "UNKNOWN",
            80.0,
            70.0,
            10.0,
            None,
            None,
            None,
            0.0,
            ENTRY_PAYLOAD,
            sqlite3.Binary(SETTLEMENT_PAYLOAD),
        ),
    )
    connection.execute(
        """
        insert into signal_audit(
            symbol, created_at_ms, decision, direction, timeframe_minutes,
            threshold_segment, regime, score, threshold, reason, payload
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "BTCUSDT",
            1_000,
            "OPENED",
            "LONG",
            10,
            "WD-00",
            "UNKNOWN",
            80.0,
            70.0,
            "legacy audit",
            AUDIT_PAYLOAD,
        ),
    )
    connection.commit()


def _column_details(connection: sqlite3.Connection, table: str) -> dict[str, tuple]:
    return {
        row[1]: (str(row[2]).upper(), row[3], row[4], row[5])
        for row in connection.execute(f"pragma table_info({table})")
    }


def _schema_snapshot(connection: sqlite3.Connection) -> tuple[int, tuple[tuple, ...]]:
    schema_version = connection.execute("pragma schema_version").fetchone()[0]
    objects = tuple(
        connection.execute(
            """
            select type, name, tbl_name, sql
            from sqlite_master
            where name not like 'sqlite_%'
            order by type, name
            """
        ).fetchall()
    )
    return schema_version, objects


def _legacy_payload_snapshot(connection: sqlite3.Connection) -> dict[str, tuple]:
    return {
        "orders": connection.execute(
            "select count(*), typeof(payload), payload from orders"
        ).fetchone(),
        "observation_signals": connection.execute(
            "select count(*), typeof(payload), payload from observation_signals"
        ).fetchone(),
        "order_entry_snapshots": connection.execute(
            """
            select count(*), typeof(entry_payload), entry_payload,
                   typeof(settlement_payload), settlement_payload
            from order_entry_snapshots
            """
        ).fetchone(),
        "signal_audit": connection.execute(
            "select count(*), typeof(payload), payload from signal_audit"
        ).fetchone(),
    }


class StorageSchemaMigrationTest(unittest.TestCase):
    def _migrate(self, connection: sqlite3.Connection) -> None:
        if migrate is None:
            self.fail("app.storage_schema.migrate is not implemented")
        migrate(connection)

    def _assert_schema_conflict_preserves_database(
        self,
        connection: sqlite3.Connection,
        object_name: str,
    ) -> None:
        before_schema = _schema_snapshot(connection)
        before_version = connection.execute("pragma user_version").fetchone()[0]
        before_changes = connection.total_changes
        before_transaction = connection.in_transaction

        with self.assertRaisesRegex(
            RuntimeError,
            rf"(?i)schema conflict.*{object_name}",
        ):
            self._migrate(connection)

        self.assertEqual(connection.in_transaction, before_transaction)
        self.assertEqual(
            connection.execute("pragma user_version").fetchone()[0],
            before_version,
        )
        self.assertEqual(_schema_snapshot(connection), before_schema)
        self.assertEqual(connection.total_changes, before_changes)

    def test_zero_and_v1_databases_upgrade_to_v2(self):
        self.assertEqual(SCHEMA_VERSION, 2)
        for legacy_version in (0, 1):
            with self.subTest(legacy_version=legacy_version):
                connection = sqlite3.connect(":memory:")
                self.addCleanup(connection.close)
                _create_legacy_schema(connection)
                connection.execute(f"pragma user_version = {legacy_version}")

                self._migrate(connection)

                self.assertEqual(
                    connection.execute("pragma user_version").fetchone()[0],
                    2,
                )
                tables = {
                    row[0]
                    for row in connection.execute(
                        "select name from sqlite_master where type = 'table'"
                    )
                }
                self.assertIn("runtime_config_snapshots", tables)
                self.assertIn("decision_contexts", tables)

    def test_creates_all_v2_tables_columns_indexes_and_partial_unique_rule(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        _create_legacy_schema(connection)

        self._migrate(connection)

        runtime_columns = _column_details(connection, "runtime_config_snapshots")
        self.assertEqual(
            runtime_columns,
            {
                "runtime_config_hash": ("TEXT", 0, None, 1),
                "context_version": ("TEXT", 1, None, 0),
                "strategy_build_id": ("TEXT", 1, None, 0),
                "canonical_payload": ("TEXT", 1, None, 0),
                "payload_bytes": ("INTEGER", 1, None, 0),
                "created_at_ms": ("INTEGER", 1, None, 0),
            },
        )
        decision_columns = _column_details(connection, "decision_contexts")
        self.assertEqual(
            decision_columns,
            {
                "symbol": ("TEXT", 1, None, 1),
                "decision_id": ("TEXT", 1, None, 2),
                "context_version": ("TEXT", 1, None, 0),
                "runtime_config_hash": ("TEXT", 1, None, 0),
                "strategy_build_id": ("TEXT", 1, None, 0),
                "created_at_ms": ("INTEGER", 1, None, 0),
                "closed_kline_at_ms": ("INTEGER", 1, None, 0),
                "direction": ("TEXT", 1, "''", 0),
                "profile_key": ("TEXT", 1, "''", 0),
                "candidate_origin": ("TEXT", 1, "''", 0),
                "input_payload": ("TEXT", 1, None, 0),
                "outcome_payload": ("TEXT", 1, None, 0),
            },
        )
        expected_added_columns = {
            "orders": {
                "decision_id": ("TEXT", 0, None, 0),
                "runtime_config_hash": ("TEXT", 0, None, 0),
            },
            "observation_signals": {
                "decision_id": ("TEXT", 0, None, 0),
                "runtime_config_hash": ("TEXT", 0, None, 0),
                "context_version": ("TEXT", 0, None, 0),
                "candidate_origin": ("TEXT", 0, None, 0),
                "qualification_state": ("TEXT", 0, None, 0),
                "adaptive_state": ("TEXT", 0, None, 0),
                "entry_structure_state": ("TEXT", 0, None, 0),
                "entry_structure_bias": ("TEXT", 0, None, 0),
                "active_level_source": ("TEXT", 0, None, 0),
            },
            "order_entry_snapshots": {
                "decision_id": ("TEXT", 0, None, 0),
                "context_version": ("TEXT", 0, None, 0),
                "runtime_config_hash": ("TEXT", 0, None, 0),
            },
            "signal_audit": {
                "record_version": ("TEXT", 0, None, 0),
                "decision_id": ("TEXT", 0, None, 0),
                "runtime_config_hash": ("TEXT", 0, None, 0),
                "event_kind": ("TEXT", 0, None, 0),
                "first_at_ms": ("INTEGER", 0, None, 0),
                "last_at_ms": ("INTEGER", 0, None, 0),
                "occurrences": ("INTEGER", 1, "1", 0),
                "score_min": ("REAL", 0, None, 0),
                "score_max": ("REAL", 0, None, 0),
                "aggregation_key": ("TEXT", 0, None, 0),
            },
        }
        for table, expected in expected_added_columns.items():
            with self.subTest(table=table):
                actual = _column_details(connection, table)
                self.assertEqual(
                    {column: actual[column] for column in expected},
                    expected,
                )

        expected_indexes = {
            "decision_contexts": {
                ("symbol", "closed_kline_at_ms"): (0, 0),
                ("symbol", "profile_key", "closed_kline_at_ms"): (0, 0),
            },
            "observation_signals": {
                ("symbol", "candidate_origin", "opened_at"): (0, 0),
                ("symbol", "adaptive_state", "opened_at"): (0, 0),
                ("symbol", "entry_structure_bias", "opened_at"): (0, 0),
            },
            "signal_audit": {
                ("symbol", "aggregation_key"): (1, 1),
            },
        }
        for table, expected in expected_indexes.items():
            actual = {}
            for row in connection.execute(f"pragma index_list({table})"):
                columns = tuple(
                    info[2]
                    for info in connection.execute(
                        f"pragma index_info('{row[1]}')"
                    )
                )
                actual[columns] = (row[2], row[4])
            with self.subTest(table=table):
                for columns, flags in expected.items():
                    self.assertEqual(actual.get(columns), flags)

        self._insert_audit(connection, "BTCUSDT", "aggregate-1")
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_audit(connection, "BTCUSDT", "aggregate-1")
        self._insert_audit(connection, "ETHUSDT", "aggregate-1")
        self._insert_audit(connection, "BTCUSDT", None)
        self._insert_audit(connection, "BTCUSDT", None)

    def test_preserves_legacy_row_counts_and_exact_payload_values(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        _create_legacy_schema(connection)
        _insert_legacy_sentinels(connection)
        before = _legacy_payload_snapshot(connection)
        changes_before = connection.total_changes

        self._migrate(connection)

        self.assertEqual(_legacy_payload_snapshot(connection), before)
        self.assertEqual(connection.total_changes, changes_before)
        self.assertEqual(before["orders"], (1, "text", ORDER_PAYLOAD))
        self.assertEqual(
            before["observation_signals"],
            (1, "blob", OBSERVATION_PAYLOAD),
        )
        self.assertEqual(
            before["order_entry_snapshots"],
            (1, "text", ENTRY_PAYLOAD, "blob", SETTLEMENT_PAYLOAD),
        )
        self.assertEqual(before["signal_audit"], (1, "text", AUDIT_PAYLOAD))

    def test_old_rows_keep_nullable_v2_columns_null_and_new_audit_uses_default(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        _create_legacy_schema(connection)
        _insert_legacy_sentinels(connection)

        self._migrate(connection)

        self.assertEqual(
            connection.execute(
                "select decision_id, runtime_config_hash from orders"
            ).fetchone(),
            (None, None),
        )
        self.assertEqual(
            connection.execute(
                """
                select decision_id, runtime_config_hash, context_version,
                       candidate_origin, qualification_state, adaptive_state,
                       entry_structure_state, entry_structure_bias,
                       active_level_source
                from observation_signals
                """
            ).fetchone(),
            (None,) * 9,
        )
        self.assertEqual(
            connection.execute(
                """
                select decision_id, context_version, runtime_config_hash
                from order_entry_snapshots
                """
            ).fetchone(),
            (None, None, None),
        )
        self.assertEqual(
            connection.execute(
                """
                select record_version, decision_id, runtime_config_hash,
                       event_kind, first_at_ms, last_at_ms, score_min,
                       score_max, aggregation_key
                from signal_audit
                """
            ).fetchone(),
            (None,) * 9,
        )
        new_id = self._insert_audit(connection, "BTCUSDT", "default-check")
        self.assertEqual(
            connection.execute(
                "select occurrences from signal_audit where id = ?",
                (new_id,),
            ).fetchone()[0],
            1,
        )

    def test_second_migration_is_a_schema_no_op(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        _create_legacy_schema(connection)
        self._migrate(connection)
        before = _schema_snapshot(connection)
        changes_before = connection.total_changes
        traced = []
        connection.set_trace_callback(traced.append)

        self._migrate(connection)

        connection.set_trace_callback(None)
        self.assertEqual(_schema_snapshot(connection), before)
        self.assertEqual(connection.total_changes, changes_before)
        self.assertEqual(connection.execute("pragma user_version").fetchone()[0], 2)
        self.assertFalse(
            any(
                statement.lstrip().upper().startswith(
                    ("ALTER ", "CREATE ", "DROP ", "INSERT ", "UPDATE ", "DELETE ")
                )
                for statement in traced
            ),
            traced,
        )

    def test_incomplete_database_marked_v2_raises_without_schema_mutation(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        _create_legacy_schema(connection)
        connection.execute("pragma user_version = 2")

        self._assert_schema_conflict_preserves_database(
            connection,
            "runtime_config_snapshots",
        )

        self.assertNotIn("decision_id", _column_details(connection, "orders"))
        self.assertEqual(
            connection.execute(
                """
                select count(*) from sqlite_master
                where name in ('runtime_config_snapshots', 'decision_contexts')
                """
            ).fetchone()[0],
            0,
        )

    def test_incomplete_existing_v2_table_rolls_back_without_certification(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        _create_legacy_schema(connection)
        connection.execute(
            """
            create table runtime_config_snapshots (
                runtime_config_hash text primary key
            )
            """
        )
        connection.execute("pragma user_version = 1")

        self._assert_schema_conflict_preserves_database(
            connection,
            "runtime_config_snapshots",
        )

    def test_incompatible_extra_column_on_v2_table_is_rejected(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        _create_legacy_schema(connection)
        connection.execute(
            """
            create table runtime_config_snapshots (
                runtime_config_hash text primary key,
                context_version text not null,
                strategy_build_id text not null,
                canonical_payload text not null,
                payload_bytes integer not null,
                created_at_ms integer not null,
                extra_required_value text not null
            )
            """
        )
        connection.execute("pragma user_version = 1")

        self._assert_schema_conflict_preserves_database(
            connection,
            "runtime_config_snapshots",
        )

    def test_reordered_v2_owned_table_columns_are_rejected(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        _create_legacy_schema(connection)
        connection.execute(
            """
            create table runtime_config_snapshots (
                context_version text not null,
                runtime_config_hash text primary key,
                strategy_build_id text not null,
                canonical_payload text not null,
                payload_bytes integer not null,
                created_at_ms integer not null
            )
            """
        )
        connection.execute("pragma user_version = 1")

        self._assert_schema_conflict_preserves_database(
            connection,
            "runtime_config_snapshots",
        )

    def test_generated_column_on_v2_owned_table_is_rejected(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        _create_legacy_schema(connection)
        connection.execute(
            """
            create table runtime_config_snapshots (
                runtime_config_hash text primary key,
                context_version text not null,
                strategy_build_id text not null,
                canonical_payload text not null,
                payload_bytes integer not null,
                created_at_ms integer not null,
                payload_copy text generated always as (canonical_payload) virtual
            )
            """
        )
        connection.execute("pragma user_version = 1")

        self._assert_schema_conflict_preserves_database(
            connection,
            "runtime_config_snapshots",
        )

    def test_owned_table_create_sql_conflicts_are_rejected(self):
        cases = (
            (
                "check constraint",
                {
                    "context_declaration": (
                        "context_version text not null "
                        "check(context_version = 'BROKEN')"
                    )
                },
            ),
            (
                "nocase primary key",
                {
                    "hash_declaration": (
                        "runtime_config_hash text collate nocase primary key"
                    )
                },
            ),
            (
                "changed primary key formulation",
                {
                    "hash_declaration": "runtime_config_hash text",
                    "table_constraint": "primary key(runtime_config_hash)",
                },
            ),
            ("strict table", {"table_options": "strict"}),
            ("without rowid", {"table_options": "without rowid"}),
            (
                "extra unique constraint",
                {"table_constraint": "unique(context_version)"},
            ),
        )
        for label, options in cases:
            with self.subTest(label=label):
                connection = sqlite3.connect(":memory:")
                try:
                    _create_legacy_schema(connection)
                    _create_runtime_config_snapshots(connection, **options)
                    connection.execute("pragma user_version = 1")

                    self._assert_schema_conflict_preserves_database(
                        connection,
                        "runtime_config_snapshots",
                    )
                finally:
                    connection.close()

    def test_trigger_on_v2_owned_table_is_rejected(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        _create_legacy_schema(connection)
        _create_runtime_config_snapshots(connection)
        connection.execute(
            """
            create trigger abort_runtime_config_insert
            before insert on runtime_config_snapshots
            begin
                select raise(abort, 'blocked');
            end
            """
        )
        connection.execute("pragma user_version = 1")

        self._assert_schema_conflict_preserves_database(
            connection,
            "runtime_config_snapshots",
        )

    def test_extra_indexes_on_v2_owned_table_are_rejected(self):
        for unique in (False, True):
            with self.subTest(unique=unique):
                connection = sqlite3.connect(":memory:")
                try:
                    _create_legacy_schema(connection)
                    _create_runtime_config_snapshots(connection)
                    qualifier = "unique " if unique else ""
                    connection.execute(
                        f"create {qualifier}index extra_runtime_context "
                        "on runtime_config_snapshots(context_version)"
                    )
                    connection.execute("pragma user_version = 1")

                    self._assert_schema_conflict_preserves_database(
                        connection,
                        "runtime_config_snapshots",
                    )
                finally:
                    connection.close()

    def test_harmless_owned_table_sql_variations_are_accepted(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        _create_legacy_schema(connection)
        connection.executescript(
            """
            CREATE TABLE "runtime_config_snapshots"(
                "runtime_config_hash" TEXT COLLATE BINARY PRIMARY KEY,
                "context_version" TEXT NOT NULL,
                "strategy_build_id" TEXT NOT NULL,
                "canonical_payload" TEXT NOT NULL,
                "payload_bytes" INTEGER NOT NULL,
                "created_at_ms" INTEGER NOT NULL
            );
            CREATE TABLE `decision_contexts`(
                `symbol` TEXT NOT NULL,
                `decision_id` TEXT NOT NULL,
                `context_version` TEXT NOT NULL,
                `runtime_config_hash` TEXT NOT NULL,
                `strategy_build_id` TEXT NOT NULL,
                `created_at_ms` INTEGER NOT NULL,
                `closed_kline_at_ms` INTEGER NOT NULL,
                `direction` TEXT NOT NULL DEFAULT '',
                `profile_key` TEXT NOT NULL DEFAULT '',
                `candidate_origin` TEXT NOT NULL DEFAULT '',
                `input_payload` TEXT NOT NULL,
                `outcome_payload` TEXT NOT NULL,
                PRIMARY KEY(`symbol`, `decision_id`)
            );
            """
        )

        self._migrate(connection)
        before = _schema_snapshot(connection)
        self._migrate(connection)

        self.assertEqual(connection.execute("pragma user_version").fetchone()[0], 2)
        self.assertEqual(_schema_snapshot(connection), before)

    def test_incompatible_existing_v2_column_rolls_back_without_certification(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        _create_legacy_schema(
            connection,
            order_decision_id_declaration="integer not null default 0",
        )
        _insert_legacy_sentinels(connection)
        connection.execute("pragma user_version = 1")
        before_payloads = _legacy_payload_snapshot(connection)

        self._assert_schema_conflict_preserves_database(
            connection,
            "orders.decision_id",
        )

        self.assertEqual(_legacy_payload_snapshot(connection), before_payloads)

    def test_unrelated_legacy_extension_column_is_not_over_validated(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        _create_legacy_schema(
            connection,
            order_decision_id_declaration="text",
        )
        connection.execute(
            """
            alter table orders
            add column legacy_extension blob not null default x''
            """
        )
        connection.execute(
            "create index idx_orders_legacy_status on orders(status)"
        )

        self._migrate(connection)

        self.assertEqual(connection.execute("pragma user_version").fetchone()[0], 2)
        columns = _column_details(connection, "orders")
        self.assertIn("legacy_extension", columns)
        self.assertIn("decision_id", columns)
        self.assertIsNotNone(
            connection.execute(
                """
                select 1 from sqlite_master
                where type = 'index' and name = 'idx_orders_legacy_status'
                """
            ).fetchone()
        )

    def test_same_named_wrong_index_rolls_back_without_certification(self):
        cases = (
            (
                "wrong table",
                "create index idx_decision_contexts_symbol_closed_kline "
                "on orders(symbol, opened_at)",
            ),
            (
                "wrong columns",
                "create index idx_decision_contexts_symbol_closed_kline "
                "on decision_contexts(symbol, profile_key)",
            ),
        )
        for label, create_index in cases:
            with self.subTest(label=label):
                connection = sqlite3.connect(":memory:")
                try:
                    _create_legacy_schema(connection)
                    self._migrate(connection)
                    connection.execute(
                        "drop index idx_decision_contexts_symbol_closed_kline"
                    )
                    connection.execute(create_index)
                    connection.execute("pragma user_version = 1")

                    self._assert_schema_conflict_preserves_database(
                        connection,
                        "idx_decision_contexts_symbol_closed_kline",
                    )
                finally:
                    connection.close()

    def test_required_index_key_metadata_conflicts_are_rejected(self):
        cases = (
            (
                "partial nocase collation",
                "ux_signal_audit_symbol_aggregation_key",
                "create unique index ux_signal_audit_symbol_aggregation_key "
                "on signal_audit(symbol collate nocase, aggregation_key) "
                "where aggregation_key is not null",
            ),
            (
                "normal descending term",
                "idx_decision_contexts_symbol_closed_kline",
                "create index idx_decision_contexts_symbol_closed_kline "
                "on decision_contexts(symbol, closed_kline_at_ms desc)",
            ),
            (
                "expression term",
                "idx_decision_contexts_symbol_closed_kline",
                "create index idx_decision_contexts_symbol_closed_kline "
                "on decision_contexts(symbol, (closed_kline_at_ms + 0))",
            ),
            (
                "extra key term",
                "idx_decision_contexts_symbol_closed_kline",
                "create index idx_decision_contexts_symbol_closed_kline "
                "on decision_contexts(symbol, closed_kline_at_ms, profile_key)",
            ),
            (
                "wrong key order",
                "idx_decision_contexts_symbol_closed_kline",
                "create index idx_decision_contexts_symbol_closed_kline "
                "on decision_contexts(closed_kline_at_ms, symbol)",
            ),
        )
        for label, index_name, create_index in cases:
            with self.subTest(label=label):
                connection = sqlite3.connect(":memory:")
                try:
                    _create_legacy_schema(connection)
                    self._migrate(connection)
                    connection.execute(f"drop index {index_name}")
                    connection.execute(create_index)
                    connection.execute("pragma user_version = 1")

                    self._assert_schema_conflict_preserves_database(
                        connection,
                        index_name,
                    )
                finally:
                    connection.close()

    def test_malformed_partial_audit_index_rolls_back_without_certification(self):
        cases = (
            (
                "not unique",
                "create index ux_signal_audit_symbol_aggregation_key "
                "on signal_audit(symbol, aggregation_key) "
                "where aggregation_key is not null",
            ),
            (
                "not partial",
                "create unique index ux_signal_audit_symbol_aggregation_key "
                "on signal_audit(symbol, aggregation_key)",
            ),
        )
        for label, create_index in cases:
            with self.subTest(label=label):
                connection = sqlite3.connect(":memory:")
                try:
                    _create_legacy_schema(connection)
                    self._migrate(connection)
                    connection.execute(
                        "drop index ux_signal_audit_symbol_aggregation_key"
                    )
                    connection.execute(create_index)
                    connection.execute("pragma user_version = 1")

                    self._assert_schema_conflict_preserves_database(
                        connection,
                        "ux_signal_audit_symbol_aggregation_key",
                    )
                finally:
                    connection.close()

    def test_equivalent_partial_audit_predicates_are_accepted(self):
        predicates = (
            '"aggregation_key" IS NOT NULL',
            "(aggregation_key) is not null",
            "aggregation_key NOTNULL",
            '((("aggregation_key"))) NotNull',
            "((aggregation_key IS NOT NULL))",
        )
        for predicate in predicates:
            with self.subTest(predicate=predicate):
                connection = sqlite3.connect(":memory:")
                try:
                    _create_legacy_schema(connection)
                    self._migrate(connection)
                    connection.execute(
                        "drop index ux_signal_audit_symbol_aggregation_key"
                    )
                    connection.execute(
                        "create unique index "
                        "ux_signal_audit_symbol_aggregation_key "
                        "on signal_audit(symbol, aggregation_key) "
                        f"where {predicate}"
                    )
                    before = _schema_snapshot(connection)
                    changes_before = connection.total_changes

                    self._migrate(connection)

                    self.assertEqual(_schema_snapshot(connection), before)
                    self.assertEqual(connection.total_changes, changes_before)
                    self.assertEqual(
                        connection.execute("pragma user_version").fetchone()[0],
                        2,
                    )
                finally:
                    connection.close()

    def test_different_partial_audit_predicate_is_rejected(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        _create_legacy_schema(connection)
        self._migrate(connection)
        connection.execute("drop index ux_signal_audit_symbol_aggregation_key")
        connection.execute(
            """
            create unique index ux_signal_audit_symbol_aggregation_key
            on signal_audit(symbol, aggregation_key)
            where aggregation_key is not null and symbol is not null
            """
        )

        self._assert_schema_conflict_preserves_database(
            connection,
            "ux_signal_audit_symbol_aggregation_key",
        )

    def test_future_schema_version_is_rejected_without_mutation(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        _create_legacy_schema(connection)
        self._migrate(connection)
        connection.execute(
            "alter table runtime_config_snapshots add column future_note text"
        )
        connection.execute("create table future_v3_state(value text)")
        connection.execute("pragma user_version = 3")
        before_schema = _schema_snapshot(connection)
        changes_before = connection.total_changes
        transaction_before = connection.in_transaction

        with self.assertRaisesRegex(
            RuntimeError,
            r"(?i)unsupported.*schema version.*3",
        ) as raised:
            self._migrate(connection)

        self.assertEqual(
            type(raised.exception).__name__,
            "UnsupportedSchemaVersionError",
        )
        self.assertEqual(connection.in_transaction, transaction_before)
        self.assertEqual(connection.execute("pragma user_version").fetchone()[0], 3)
        self.assertEqual(_schema_snapshot(connection), before_schema)
        self.assertEqual(connection.total_changes, changes_before)

    def test_fresh_store_is_v2_and_existing_order_behavior_still_works(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            order = SimulatedOrder(
                id=9,
                direction="LONG",
                timeframe_minutes=10,
                level="A",
                reason="schema integration",
                entry_price=100.0,
                opened_at=1_000,
                expires_at=601_000,
            )

            store.save_order(order, "BTCUSDT")
            restored = store.load_orders("BTCUSDT")
            with sqlite3.connect(db_path) as connection:
                version = connection.execute("pragma user_version").fetchone()[0]
                tables = {
                    row[0]
                    for row in connection.execute(
                        "select name from sqlite_master where type = 'table'"
                    )
                }
                order_v2_values = connection.execute(
                    """
                    select decision_id, runtime_config_hash
                    from orders where symbol = 'BTCUSDT' and order_id = 9
                    """
                ).fetchone()

        self.assertEqual(version, 2)
        self.assertTrue(
            {
                "orders",
                "stake_progression_runtime",
                "wave_runtime",
                "stake_progression_credits",
                "signal_audit",
                "observation_signals",
                "daily_profile_selections",
                "order_entry_snapshots",
                "runtime_config_snapshots",
                "decision_contexts",
            }.issubset(tables)
        )
        self.assertEqual(order_v2_values, (None, None))
        self.assertEqual(restored, [order])

    def test_ddl_failure_rolls_back_schema_and_user_version(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        _create_legacy_schema(connection)
        connection.execute("pragma user_version = 1")
        before = _schema_snapshot(connection)

        def deny_v2_index(action, arg1, _arg2, _database, _trigger):
            if (
                action == sqlite3.SQLITE_CREATE_INDEX
                and arg1 == "idx_decision_contexts_symbol_closed_kline"
            ):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(deny_v2_index)
        with self.assertRaises(sqlite3.DatabaseError):
            self._migrate(connection)
        connection.set_authorizer(None)

        self.assertEqual(connection.execute("pragma user_version").fetchone()[0], 1)
        self.assertEqual(_schema_snapshot(connection), before)
        self.assertNotIn("decision_id", _column_details(connection, "orders"))
        self.assertEqual(
            connection.execute(
                """
                select count(*) from sqlite_master
                where type = 'table' and name in (
                    'runtime_config_snapshots', 'decision_contexts'
                )
                """
            ).fetchone()[0],
            0,
        )

    def test_release_failure_rolls_back_and_allows_retry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "locked.sqlite3"
            with sqlite3.connect(db_path) as setup:
                self.assertEqual(
                    setup.execute("pragma journal_mode = delete").fetchone()[0],
                    "delete",
                )
                _create_legacy_schema(setup)
                _insert_legacy_sentinels(setup)
                setup.execute("pragma user_version = 1")
            with sqlite3.connect(db_path) as baseline:
                before_schema = _schema_snapshot(baseline)
                before_payloads = _legacy_payload_snapshot(baseline)

            writer = sqlite3.connect(db_path, timeout=0.0)
            blocker = sqlite3.connect(db_path, timeout=0.0)
            try:
                writer.execute("pragma busy_timeout = 0")
                blocker.execute("pragma busy_timeout = 0")
                blocker.execute("begin")
                blocker.execute("select payload from orders").fetchone()

                with self.assertRaisesRegex(
                    sqlite3.OperationalError,
                    "database is locked",
                ):
                    self._migrate(writer)

                writer_version = writer.execute(
                    "pragma user_version"
                ).fetchone()[0]
                writer_tables = {
                    row[0]
                    for row in writer.execute(
                        "select name from sqlite_master where type = 'table'"
                    )
                }
                self.assertFalse(
                    writer.in_transaction,
                    (
                        "failed release left the writer transaction open: "
                        f"user_version={writer_version}, tables={sorted(writer_tables)}"
                    ),
                )
                self.assertEqual(writer_version, 1)
                self.assertEqual(_schema_snapshot(writer), before_schema)
                self.assertEqual(_legacy_payload_snapshot(writer), before_payloads)
                self.assertNotIn("decision_id", _column_details(writer, "orders"))
                self.assertNotIn("runtime_config_snapshots", writer_tables)
                self.assertNotIn("decision_contexts", writer_tables)

                with sqlite3.connect(db_path, timeout=0.0) as fresh:
                    self.assertEqual(
                        fresh.execute("pragma user_version").fetchone()[0],
                        1,
                    )
                    self.assertEqual(_schema_snapshot(fresh), before_schema)
                    self.assertEqual(
                        _legacy_payload_snapshot(fresh),
                        before_payloads,
                    )

                blocker.rollback()
                self._migrate(writer)
                self.assertFalse(writer.in_transaction)
                self.assertEqual(
                    writer.execute("pragma user_version").fetchone()[0],
                    2,
                )
                self.assertIn("decision_id", _column_details(writer, "orders"))
                self.assertEqual(_legacy_payload_snapshot(writer), before_payloads)
                with sqlite3.connect(db_path) as fresh:
                    self.assertEqual(
                        fresh.execute("pragma user_version").fetchone()[0],
                        2,
                    )
                    self.assertIn(
                        "runtime_config_snapshots",
                        {
                            row[0]
                            for row in fresh.execute(
                                """
                                select name from sqlite_master
                                where type = 'table'
                                """
                            )
                        },
                    )
            finally:
                if blocker.in_transaction:
                    blocker.rollback()
                blocker.close()
                writer.close()

    def test_failure_inside_caller_savepoint_preserves_caller_work(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        _create_legacy_schema(connection)
        connection.execute("pragma user_version = 1")
        connection.commit()
        connection.execute("savepoint caller_work")
        connection.execute(
            """
            insert into orders(
                symbol, order_id, status, result, opened_at, settled_at, payload
            ) values ('BTCUSDT', 99, 'OPEN', null, 1000, null, ?)
            """,
            (ORDER_PAYLOAD,),
        )

        def deny_v2_index(action, arg1, _arg2, _database, _trigger):
            if (
                action == sqlite3.SQLITE_CREATE_INDEX
                and arg1 == "idx_decision_contexts_symbol_closed_kline"
            ):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(deny_v2_index)
        with self.assertRaises(sqlite3.DatabaseError):
            self._migrate(connection)
        connection.set_authorizer(None)

        self.assertTrue(connection.in_transaction)
        self.assertEqual(
            connection.execute(
                "select payload from orders where order_id = 99"
            ).fetchone()[0],
            ORDER_PAYLOAD,
        )
        self.assertEqual(connection.execute("pragma user_version").fetchone()[0], 1)
        self.assertNotIn("decision_id", _column_details(connection, "orders"))
        connection.execute("release savepoint caller_work")
        self.assertFalse(connection.in_transaction)
        self.assertEqual(
            connection.execute(
                "select count(*) from orders where order_id = 99"
            ).fetchone()[0],
            1,
        )

    def test_success_inside_caller_savepoint_does_not_commit_caller_work(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        _create_legacy_schema(connection)
        connection.execute("pragma user_version = 1")
        connection.commit()
        connection.execute("savepoint caller_work")
        connection.execute(
            """
            insert into orders(
                symbol, order_id, status, result, opened_at, settled_at, payload
            ) values ('BTCUSDT', 99, 'OPEN', null, 1000, null, ?)
            """,
            (ORDER_PAYLOAD,),
        )

        self._migrate(connection)

        self.assertTrue(connection.in_transaction)
        self.assertEqual(connection.execute("pragma user_version").fetchone()[0], 2)
        self.assertIn("decision_id", _column_details(connection, "orders"))
        connection.execute("rollback to savepoint caller_work")
        connection.execute("release savepoint caller_work")
        self.assertFalse(connection.in_transaction)
        self.assertEqual(connection.execute("pragma user_version").fetchone()[0], 1)
        self.assertNotIn("decision_id", _column_details(connection, "orders"))
        self.assertEqual(
            connection.execute(
                "select count(*) from orders where order_id = 99"
            ).fetchone()[0],
            0,
        )

    def test_existing_v2_column_does_not_cause_duplicate_column_failure(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        _create_legacy_schema(
            connection,
            order_decision_id_declaration="text",
        )
        connection.execute("pragma user_version = 1")

        self._migrate(connection)

        order_columns = [
            row[1] for row in connection.execute("pragma table_info(orders)")
        ]
        self.assertEqual(order_columns.count("decision_id"), 1)
        self.assertIn("runtime_config_hash", order_columns)
        self.assertEqual(connection.execute("pragma user_version").fetchone()[0], 2)

    @staticmethod
    def _insert_audit(
        connection: sqlite3.Connection,
        symbol: str,
        aggregation_key: str | None,
    ) -> int:
        cursor = connection.execute(
            """
            insert into signal_audit(
                symbol, created_at_ms, decision, direction, timeframe_minutes,
                threshold_segment, regime, score, threshold, reason, payload,
                aggregation_key
            ) values (?, 2000, 'OPENED', 'LONG', 10, 'WD-00',
                      'UNKNOWN', 80.0, 70.0, 'new audit', '{}', ?)
            """,
            (symbol, aggregation_key),
        )
        return cursor.lastrowid


if __name__ == "__main__":
    unittest.main()
