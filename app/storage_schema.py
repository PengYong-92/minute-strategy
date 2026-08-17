import re
import sqlite3
from dataclasses import dataclass


SCHEMA_VERSION = 2
_MIGRATION_SAVEPOINT = "storage_schema_v2"


class SchemaConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class _ColumnSpec:
    name: str
    declaration: str
    data_type: str
    not_null: int = 0
    default: str | None = None
    primary_key: int = 0


@dataclass(frozen=True)
class _TableSpec:
    name: str
    create_sql: str
    columns: tuple[_ColumnSpec, ...]


@dataclass(frozen=True)
class _IndexSpec:
    name: str
    table: str
    create_sql: str
    columns: tuple[str, ...]
    unique: int = 0
    partial: int = 0
    where: str | None = None


_TABLE_SPECS = (
    _TableSpec(
        name="runtime_config_snapshots",
        create_sql="""
            create table if not exists runtime_config_snapshots (
                runtime_config_hash text primary key,
                context_version text not null,
                strategy_build_id text not null,
                canonical_payload text not null,
                payload_bytes integer not null,
                created_at_ms integer not null
            )
        """,
        columns=(
            _ColumnSpec("runtime_config_hash", "text primary key", "TEXT", primary_key=1),
            _ColumnSpec("context_version", "text not null", "TEXT", not_null=1),
            _ColumnSpec("strategy_build_id", "text not null", "TEXT", not_null=1),
            _ColumnSpec("canonical_payload", "text not null", "TEXT", not_null=1),
            _ColumnSpec("payload_bytes", "integer not null", "INTEGER", not_null=1),
            _ColumnSpec("created_at_ms", "integer not null", "INTEGER", not_null=1),
        ),
    ),
    _TableSpec(
        name="decision_contexts",
        create_sql="""
            create table if not exists decision_contexts (
                symbol text not null,
                decision_id text not null,
                context_version text not null,
                runtime_config_hash text not null,
                strategy_build_id text not null,
                created_at_ms integer not null,
                closed_kline_at_ms integer not null,
                direction text not null default '',
                profile_key text not null default '',
                candidate_origin text not null default '',
                input_payload text not null,
                outcome_payload text not null,
                primary key(symbol, decision_id)
            )
        """,
        columns=(
            _ColumnSpec("symbol", "text not null", "TEXT", not_null=1, primary_key=1),
            _ColumnSpec("decision_id", "text not null", "TEXT", not_null=1, primary_key=2),
            _ColumnSpec("context_version", "text not null", "TEXT", not_null=1),
            _ColumnSpec("runtime_config_hash", "text not null", "TEXT", not_null=1),
            _ColumnSpec("strategy_build_id", "text not null", "TEXT", not_null=1),
            _ColumnSpec("created_at_ms", "integer not null", "INTEGER", not_null=1),
            _ColumnSpec("closed_kline_at_ms", "integer not null", "INTEGER", not_null=1),
            _ColumnSpec("direction", "text not null default ''", "TEXT", not_null=1, default="''"),
            _ColumnSpec(
                "profile_key",
                "text not null default ''",
                "TEXT",
                not_null=1,
                default="''",
            ),
            _ColumnSpec(
                "candidate_origin",
                "text not null default ''",
                "TEXT",
                not_null=1,
                default="''",
            ),
            _ColumnSpec("input_payload", "text not null", "TEXT", not_null=1),
            _ColumnSpec("outcome_payload", "text not null", "TEXT", not_null=1),
        ),
    ),
)


_ADDED_COLUMN_SPECS = {
    "orders": (
        _ColumnSpec("decision_id", "text", "TEXT"),
        _ColumnSpec("runtime_config_hash", "text", "TEXT"),
    ),
    "observation_signals": (
        _ColumnSpec("decision_id", "text", "TEXT"),
        _ColumnSpec("runtime_config_hash", "text", "TEXT"),
        _ColumnSpec("context_version", "text", "TEXT"),
        _ColumnSpec("candidate_origin", "text", "TEXT"),
        _ColumnSpec("qualification_state", "text", "TEXT"),
        _ColumnSpec("adaptive_state", "text", "TEXT"),
        _ColumnSpec("entry_structure_state", "text", "TEXT"),
        _ColumnSpec("entry_structure_bias", "text", "TEXT"),
        _ColumnSpec("active_level_source", "text", "TEXT"),
    ),
    "order_entry_snapshots": (
        _ColumnSpec("decision_id", "text", "TEXT"),
        _ColumnSpec("context_version", "text", "TEXT"),
        _ColumnSpec("runtime_config_hash", "text", "TEXT"),
    ),
    "signal_audit": (
        _ColumnSpec("record_version", "text", "TEXT"),
        _ColumnSpec("decision_id", "text", "TEXT"),
        _ColumnSpec("runtime_config_hash", "text", "TEXT"),
        _ColumnSpec("event_kind", "text", "TEXT"),
        _ColumnSpec("first_at_ms", "integer", "INTEGER"),
        _ColumnSpec("last_at_ms", "integer", "INTEGER"),
        _ColumnSpec(
            "occurrences",
            "integer not null default 1",
            "INTEGER",
            not_null=1,
            default="1",
        ),
        _ColumnSpec("score_min", "real", "REAL"),
        _ColumnSpec("score_max", "real", "REAL"),
        _ColumnSpec("aggregation_key", "text", "TEXT"),
    ),
}


_INDEX_SPECS = (
    _IndexSpec(
        name="idx_decision_contexts_symbol_closed_kline",
        table="decision_contexts",
        create_sql="""
            create index if not exists
                idx_decision_contexts_symbol_closed_kline
            on decision_contexts(symbol, closed_kline_at_ms)
        """,
        columns=("symbol", "closed_kline_at_ms"),
    ),
    _IndexSpec(
        name="idx_decision_contexts_symbol_profile_closed_kline",
        table="decision_contexts",
        create_sql="""
            create index if not exists
                idx_decision_contexts_symbol_profile_closed_kline
            on decision_contexts(symbol, profile_key, closed_kline_at_ms)
        """,
        columns=("symbol", "profile_key", "closed_kline_at_ms"),
    ),
    _IndexSpec(
        name="idx_observation_signals_symbol_candidate_origin_opened",
        table="observation_signals",
        create_sql="""
            create index if not exists
                idx_observation_signals_symbol_candidate_origin_opened
            on observation_signals(symbol, candidate_origin, opened_at)
        """,
        columns=("symbol", "candidate_origin", "opened_at"),
    ),
    _IndexSpec(
        name="idx_observation_signals_symbol_adaptive_state_opened",
        table="observation_signals",
        create_sql="""
            create index if not exists
                idx_observation_signals_symbol_adaptive_state_opened
            on observation_signals(symbol, adaptive_state, opened_at)
        """,
        columns=("symbol", "adaptive_state", "opened_at"),
    ),
    _IndexSpec(
        name="idx_observation_signals_symbol_entry_structure_bias_opened",
        table="observation_signals",
        create_sql="""
            create index if not exists
                idx_observation_signals_symbol_entry_structure_bias_opened
            on observation_signals(symbol, entry_structure_bias, opened_at)
        """,
        columns=("symbol", "entry_structure_bias", "opened_at"),
    ),
    _IndexSpec(
        name="ux_signal_audit_symbol_aggregation_key",
        table="signal_audit",
        create_sql="""
            create unique index if not exists
                ux_signal_audit_symbol_aggregation_key
            on signal_audit(symbol, aggregation_key)
            where aggregation_key is not null
        """,
        columns=("symbol", "aggregation_key"),
        unique=1,
        partial=1,
        where="AGGREGATION_KEY IS NOT NULL",
    ),
)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _schema_object(
    connection: sqlite3.Connection,
    name: str,
) -> tuple[str, str, str | None] | None:
    row = connection.execute(
        """
        select type, tbl_name, sql
        from sqlite_master
        where name = ? collate nocase
        """,
        (name,),
    ).fetchone()
    if row is None:
        return None
    return str(row[0]), str(row[1]), row[2]


def _normalized_type(value: object) -> str:
    return " ".join(str(value or "").strip().upper().split())


def _normalized_default(value: object) -> str | None:
    if value is None:
        return None
    return " ".join(str(value).strip().split())


def _column_rows(
    connection: sqlite3.Connection,
    table: str,
) -> dict[str, tuple]:
    quoted_table = _quote_identifier(table)
    return {
        str(row[1]).casefold(): row
        for row in connection.execute(
            f"pragma table_info({quoted_table})"
        ).fetchall()
    }


def _column_signature(row: tuple) -> tuple[str, int, str | None, int]:
    return (
        _normalized_type(row[2]),
        int(row[3]),
        _normalized_default(row[4]),
        int(row[5]),
    )


def _expected_column_signature(
    spec: _ColumnSpec,
) -> tuple[str, int, str | None, int]:
    return (
        spec.data_type,
        spec.not_null,
        spec.default,
        spec.primary_key,
    )


def _raise_column_conflict(table: str, spec: _ColumnSpec, row: tuple) -> None:
    raise SchemaConflictError(
        f"SQLite schema conflict for {table}.{spec.name}: "
        f"expected {_expected_column_signature(spec)}, "
        f"found {_column_signature(row)}"
    )


def _validate_column(table: str, spec: _ColumnSpec, row: tuple) -> None:
    if _column_signature(row) != _expected_column_signature(spec):
        _raise_column_conflict(table, spec, row)


def _require_table(connection: sqlite3.Connection, table: str) -> None:
    schema_object = _schema_object(connection, table)
    if schema_object is None:
        raise SchemaConflictError(
            f"SQLite schema conflict for {table}: required table is missing"
        )
    if schema_object[0].casefold() != "table":
        raise SchemaConflictError(
            f"SQLite schema conflict for {table}: "
            f"expected table, found {schema_object[0]}"
        )


def _validate_table(
    connection: sqlite3.Connection,
    spec: _TableSpec,
) -> None:
    _require_table(connection, spec.name)
    actual = _column_rows(connection, spec.name)
    expected = {column.name.casefold(): column for column in spec.columns}
    if actual.keys() != expected.keys():
        missing = sorted(expected.keys() - actual.keys())
        extra = sorted(actual.keys() - expected.keys())
        raise SchemaConflictError(
            f"SQLite schema conflict for {spec.name}: "
            f"missing columns={missing}, extra columns={extra}"
        )
    for name, column_spec in expected.items():
        _validate_column(spec.name, column_spec, actual[name])


def _ensure_added_columns(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[_ColumnSpec, ...],
) -> None:
    _require_table(connection, table)
    existing = _column_rows(connection, table)
    for spec in columns:
        key = spec.name.casefold()
        row = existing.get(key)
        if row is not None:
            _validate_column(table, spec, row)
            continue
        connection.execute(
            f"alter table {_quote_identifier(table)} "
            f"add column {_quote_identifier(spec.name)} {spec.declaration}"
        )
        existing = _column_rows(connection, table)
        _validate_column(table, spec, existing[key])


def _strip_outer_parentheses(value: str) -> str:
    while value.startswith("(") and value.endswith(")"):
        depth = 0
        wraps_entire_value = True
        for index, character in enumerate(value):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0 and index != len(value) - 1:
                    wraps_entire_value = False
                    break
        if not wraps_entire_value or depth != 0:
            break
        value = value[1:-1].strip()
    return value


def _normalized_where(sql: str | None) -> str | None:
    if not sql:
        return None
    match = re.search(r"\bwhere\b(.*)\Z", sql.strip().rstrip(";"), re.IGNORECASE | re.DOTALL)
    if match is None:
        return None
    expression = " ".join(match.group(1).strip().split())
    return _strip_outer_parentheses(expression).upper()


def _validate_index(
    connection: sqlite3.Connection,
    spec: _IndexSpec,
) -> None:
    schema_object = _schema_object(connection, spec.name)
    if schema_object is None:
        raise SchemaConflictError(
            f"SQLite schema conflict for {spec.name}: required index is missing"
        )
    object_type, table, sql = schema_object
    if object_type.casefold() != "index" or table.casefold() != spec.table.casefold():
        raise SchemaConflictError(
            f"SQLite schema conflict for {spec.name}: "
            f"expected index on {spec.table}, found {object_type} on {table}"
        )

    quoted_table = _quote_identifier(spec.table)
    index_row = next(
        (
            row
            for row in connection.execute(f"pragma index_list({quoted_table})")
            if str(row[1]).casefold() == spec.name.casefold()
        ),
        None,
    )
    if index_row is None:
        raise SchemaConflictError(
            f"SQLite schema conflict for {spec.name}: index metadata is missing"
        )
    quoted_index = _quote_identifier(spec.name)
    columns = tuple(
        str(row[2]).casefold()
        for row in connection.execute(f"pragma index_info({quoted_index})")
    )
    expected_columns = tuple(column.casefold() for column in spec.columns)
    actual = (int(index_row[2]), columns, int(index_row[4]), _normalized_where(sql))
    expected = (spec.unique, expected_columns, spec.partial, spec.where)
    if actual != expected:
        raise SchemaConflictError(
            f"SQLite schema conflict for {spec.name}: "
            f"expected {expected}, found {actual}"
        )


def _validate_v2_schema(connection: sqlite3.Connection) -> None:
    for table_spec in _TABLE_SPECS:
        _validate_table(connection, table_spec)
    for table, column_specs in _ADDED_COLUMN_SPECS.items():
        _require_table(connection, table)
        actual = _column_rows(connection, table)
        for column_spec in column_specs:
            row = actual.get(column_spec.name.casefold())
            if row is None:
                raise SchemaConflictError(
                    f"SQLite schema conflict for {table}.{column_spec.name}: "
                    "required column is missing"
                )
            _validate_column(table, column_spec, row)
    for index_spec in _INDEX_SPECS:
        _validate_index(connection, index_spec)


def _rollback_migration(
    connection: sqlite3.Connection,
    *,
    caller_in_transaction: bool,
) -> None:
    try:
        connection.execute(f"rollback to savepoint {_MIGRATION_SAVEPOINT}")
        connection.execute(f"release savepoint {_MIGRATION_SAVEPOINT}")
    except Exception as cleanup_error:
        if not caller_in_transaction and connection.in_transaction:
            try:
                connection.rollback()
            except Exception as close_error:
                raise cleanup_error from close_error
        raise

    if not caller_in_transaction and connection.in_transaction:
        connection.rollback()


def migrate(connection: sqlite3.Connection) -> None:
    version = int(connection.execute("pragma user_version").fetchone()[0])
    if version >= SCHEMA_VERSION:
        _validate_v2_schema(connection)
        return

    caller_in_transaction = connection.in_transaction
    connection.execute(f"savepoint {_MIGRATION_SAVEPOINT}")
    try:
        for table_spec in _TABLE_SPECS:
            existing = _schema_object(connection, table_spec.name)
            if existing is not None and existing[0].casefold() != "table":
                _require_table(connection, table_spec.name)
            connection.execute(table_spec.create_sql)
            _validate_table(connection, table_spec)

        for table, column_specs in _ADDED_COLUMN_SPECS.items():
            _ensure_added_columns(connection, table, column_specs)

        for index_spec in _INDEX_SPECS:
            if _schema_object(connection, index_spec.name) is not None:
                _validate_index(connection, index_spec)
            else:
                connection.execute(index_spec.create_sql)
            _validate_index(connection, index_spec)

        _validate_v2_schema(connection)
        connection.execute(f"pragma user_version = {SCHEMA_VERSION}")
        connection.execute(f"release savepoint {_MIGRATION_SAVEPOINT}")
    except Exception as migration_error:
        try:
            _rollback_migration(
                connection,
                caller_in_transaction=caller_in_transaction,
            )
        except Exception as cleanup_error:
            raise migration_error from cleanup_error
        raise
