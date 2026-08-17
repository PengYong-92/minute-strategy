import sqlite3
from dataclasses import dataclass


SCHEMA_VERSION = 2
_MIGRATION_SAVEPOINT = "storage_schema_v2"


class SchemaConflictError(RuntimeError):
    pass


class UnsupportedSchemaVersionError(RuntimeError):
    pass


@dataclass(frozen=True)
class _ColumnSpec:
    name: str
    declaration: str
    data_type: str
    not_null: int = 0
    default: str | None = None
    primary_key: int = 0
    hidden: int = 0


@dataclass(frozen=True)
class _TableSpec:
    name: str
    create_sql: str
    columns: tuple[_ColumnSpec, ...]


@dataclass(frozen=True)
class _IndexTermSpec:
    name: str
    descending: int = 0
    collation: str = "BINARY"
    key: int = 1


@dataclass(frozen=True)
class _IndexSpec:
    name: str
    table: str
    create_sql: str
    terms: tuple[_IndexTermSpec, ...]
    unique: int = 0
    partial: int = 0
    predicate: tuple[str, str] | None = None


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
        terms=(
            _IndexTermSpec("symbol"),
            _IndexTermSpec("closed_kline_at_ms"),
        ),
    ),
    _IndexSpec(
        name="idx_decision_contexts_symbol_profile_closed_kline",
        table="decision_contexts",
        create_sql="""
            create index if not exists
                idx_decision_contexts_symbol_profile_closed_kline
            on decision_contexts(symbol, profile_key, closed_kline_at_ms)
        """,
        terms=(
            _IndexTermSpec("symbol"),
            _IndexTermSpec("profile_key"),
            _IndexTermSpec("closed_kline_at_ms"),
        ),
    ),
    _IndexSpec(
        name="idx_observation_signals_symbol_candidate_origin_opened",
        table="observation_signals",
        create_sql="""
            create index if not exists
                idx_observation_signals_symbol_candidate_origin_opened
            on observation_signals(symbol, candidate_origin, opened_at)
        """,
        terms=(
            _IndexTermSpec("symbol"),
            _IndexTermSpec("candidate_origin"),
            _IndexTermSpec("opened_at"),
        ),
    ),
    _IndexSpec(
        name="idx_observation_signals_symbol_adaptive_state_opened",
        table="observation_signals",
        create_sql="""
            create index if not exists
                idx_observation_signals_symbol_adaptive_state_opened
            on observation_signals(symbol, adaptive_state, opened_at)
        """,
        terms=(
            _IndexTermSpec("symbol"),
            _IndexTermSpec("adaptive_state"),
            _IndexTermSpec("opened_at"),
        ),
    ),
    _IndexSpec(
        name="idx_observation_signals_symbol_entry_structure_bias_opened",
        table="observation_signals",
        create_sql="""
            create index if not exists
                idx_observation_signals_symbol_entry_structure_bias_opened
            on observation_signals(symbol, entry_structure_bias, opened_at)
        """,
        terms=(
            _IndexTermSpec("symbol"),
            _IndexTermSpec("entry_structure_bias"),
            _IndexTermSpec("opened_at"),
        ),
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
        terms=(
            _IndexTermSpec("symbol"),
            _IndexTermSpec("aggregation_key"),
        ),
        unique=1,
        partial=1,
        predicate=("aggregation_key", "notnull"),
    ),
)


_SQL_KEYWORDS = frozenset(
    """
    abort action add after all alter analyze and as asc attach autoincrement
    before begin between binary blob by cascade case cast check collate column
    commit conflict constraint create cross current current_date current_time
    current_timestamp database default deferrable deferred delete desc detach
    distinct do drop each else end escape except exclude exclusive exists
    explain fail filter first following foreign from full generated glob group
    groups having if ignore immediate in index indexed initially inner insert
    instead integer intersect into is isnull join key last left like limit
    match materialized natural no not nothing notnull null nulls of offset on
    or order others outer over partition plan pragma preceding primary query
    raise range real recursive references regexp reindex release rename replace
    restrict returning right rollback row rows savepoint select set strict
    table temp temporary text then ties to transaction trigger unbounded union
    unique update using vacuum values view virtual when where window with without
    """.split()
)
_CREATE_TABLE_PREFIX = (
    ("keyword", "create"),
    ("keyword", "table"),
)
_IF_NOT_EXISTS = (
    ("keyword", "if"),
    ("keyword", "not"),
    ("keyword", "exists"),
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


def _sql_tokens(sql: str) -> tuple[tuple[str, str], ...]:
    tokens = []
    index = 0
    while index < len(sql):
        character = sql[index]
        if character.isspace():
            index += 1
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            index = len(sql) if newline < 0 else newline + 1
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end < 0:
                raise ValueError("unterminated SQL comment")
            index = end + 2
            continue
        if character == "'":
            value = []
            index += 1
            while index < len(sql):
                if sql[index] == "'":
                    if index + 1 < len(sql) and sql[index + 1] == "'":
                        value.append("'")
                        index += 2
                        continue
                    index += 1
                    break
                value.append(sql[index])
                index += 1
            else:
                raise ValueError("unterminated SQL string literal")
            tokens.append(("literal", "".join(value)))
            continue
        if character in ('"', "`", "["):
            close = "]" if character == "[" else character
            value = []
            index += 1
            while index < len(sql):
                if sql[index] == close:
                    if index + 1 < len(sql) and sql[index + 1] == close:
                        value.append(close)
                        index += 2
                        continue
                    index += 1
                    break
                value.append(sql[index])
                index += 1
            else:
                raise ValueError("unterminated quoted SQL identifier")
            tokens.append(("identifier", "".join(value).casefold()))
            continue
        if character.isalpha() or character == "_":
            end = index + 1
            while end < len(sql) and (
                sql[end].isalnum() or sql[end] in ("_", "$")
            ):
                end += 1
            value = sql[index:end].casefold()
            kind = "keyword" if value in _SQL_KEYWORDS else "identifier"
            tokens.append((kind, value))
            index = end
            continue
        if character.isdigit():
            end = index + 1
            while end < len(sql) and (
                sql[end].isalnum() or sql[end] in (".", "_", "+", "-")
            ):
                end += 1
            tokens.append(("number", sql[index:end].casefold()))
            index = end
            continue
        tokens.append(("symbol", character))
        index += 1
    return tuple(tokens)


def _without_optional_semicolon(
    tokens: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    if tokens and tokens[-1] == ("symbol", ";"):
        return tokens[:-1]
    return tokens


def _canonical_create_table_sql(sql: str) -> tuple[tuple[str, str], ...]:
    tokens = list(_without_optional_semicolon(_sql_tokens(sql)))
    if tuple(tokens[:2]) == _CREATE_TABLE_PREFIX:
        if tuple(tokens[2:5]) == _IF_NOT_EXISTS:
            del tokens[2:5]

    canonical = []
    index = 0
    while index < len(tokens):
        if (
            tokens[index] == ("keyword", "collate")
            and index + 1 < len(tokens)
            and tokens[index + 1][1] == "binary"
        ):
            index += 2
            continue
        canonical.append(tokens[index])
        index += 1
    return tuple(canonical)


def _column_rows_by_name(
    connection: sqlite3.Connection,
    table: str,
) -> dict[str, tuple]:
    quoted_table = _quote_identifier(table)
    return {
        str(row[1]).casefold(): row
        for row in connection.execute(
            f"pragma table_xinfo({quoted_table})"
        ).fetchall()
    }


def _ordered_column_rows(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[tuple, ...]:
    quoted_table = _quote_identifier(table)
    return tuple(connection.execute(f"pragma table_xinfo({quoted_table})"))


def _column_signature(row: tuple) -> tuple[str, int, str | None, int, int]:
    return (
        _normalized_type(row[2]),
        int(row[3]),
        _normalized_default(row[4]),
        int(row[5]),
        int(row[6]),
    )


def _expected_column_signature(
    spec: _ColumnSpec,
) -> tuple[str, int, str | None, int, int]:
    return (
        spec.data_type,
        spec.not_null,
        spec.default,
        spec.primary_key,
        spec.hidden,
    )


def _ordered_column_signature(row: tuple) -> tuple:
    return (
        int(row[0]),
        str(row[1]).casefold(),
        *_column_signature(row),
    )


def _expected_ordered_column_signature(
    position: int,
    spec: _ColumnSpec,
) -> tuple:
    return (
        position,
        spec.name.casefold(),
        *_expected_column_signature(spec),
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


def _owned_table_triggers(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in connection.execute(
            """
            select name from sqlite_master
            where type = 'trigger' and tbl_name = ? collate nocase
            union all
            select name from sqlite_temp_master
            where type = 'trigger' and tbl_name = ? collate nocase
            order by name
            """,
            (table, table),
        )
    )


def _validate_table(
    connection: sqlite3.Connection,
    spec: _TableSpec,
) -> None:
    _require_table(connection, spec.name)
    schema_object = _schema_object(connection, spec.name)
    create_sql = None if schema_object is None else schema_object[2]
    try:
        actual_create_sql = (
            None if create_sql is None else _canonical_create_table_sql(create_sql)
        )
        expected_create_sql = _canonical_create_table_sql(spec.create_sql)
    except ValueError as error:
        raise SchemaConflictError(
            f"SQLite schema conflict for {spec.name}: invalid CREATE TABLE SQL"
        ) from error
    if actual_create_sql != expected_create_sql:
        raise SchemaConflictError(
            f"SQLite schema conflict for {spec.name}: "
            "CREATE TABLE SQL does not match the V2 definition"
        )

    actual = tuple(
        _ordered_column_signature(row)
        for row in _ordered_column_rows(connection, spec.name)
    )
    expected = tuple(
        _expected_ordered_column_signature(position, column)
        for position, column in enumerate(spec.columns)
    )
    if actual != expected:
        raise SchemaConflictError(
            f"SQLite schema conflict for {spec.name}: "
            f"expected ordered table_xinfo={expected}, found {actual}"
        )
    triggers = _owned_table_triggers(connection, spec.name)
    if triggers:
        raise SchemaConflictError(
            f"SQLite schema conflict for {spec.name}: "
            f"owned table has triggers={triggers}"
        )


def _ensure_added_columns(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[_ColumnSpec, ...],
) -> None:
    _require_table(connection, table)
    existing = _column_rows_by_name(connection, table)
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
        existing = _column_rows_by_name(connection, table)
        _validate_column(table, spec, existing[key])


def _strip_outer_token_parentheses(
    tokens: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    while (
        len(tokens) >= 2
        and tokens[0] == ("symbol", "(")
        and tokens[-1] == ("symbol", ")")
    ):
        depth = 0
        wraps_entire_value = True
        for index, token in enumerate(tokens):
            if token == ("symbol", "("):
                depth += 1
            elif token == ("symbol", ")"):
                depth -= 1
                if depth == 0 and index != len(tokens) - 1:
                    wraps_entire_value = False
                    break
        if not wraps_entire_value or depth != 0:
            break
        tokens = tokens[1:-1]
    return tokens


def _canonical_partial_predicate(sql: str | None) -> tuple[str, str] | None:
    if not sql:
        return None
    tokens = _without_optional_semicolon(_sql_tokens(sql))
    where_positions = [
        index
        for index, token in enumerate(tokens)
        if token == ("keyword", "where")
    ]
    if len(where_positions) != 1:
        return None
    expression = _strip_outer_token_parentheses(
        tokens[where_positions[0] + 1 :]
    )
    operators = (
        (("keyword", "notnull"),),
        (
            ("keyword", "not"),
            ("keyword", "null"),
        ),
        (
            ("keyword", "is"),
            ("keyword", "not"),
            ("keyword", "null"),
        ),
    )
    for operator in operators:
        if len(expression) <= len(operator):
            continue
        if expression[-len(operator) :] != operator:
            continue
        column = _strip_outer_token_parentheses(
            expression[: -len(operator)]
        )
        if column == (("identifier", "aggregation_key"),):
            return ("aggregation_key", "notnull")
    return None


def _index_term_signature(row: tuple) -> tuple[str | None, int, str, int]:
    name = None if row[2] is None else str(row[2]).casefold()
    return (name, int(row[3]), str(row[4] or "").upper(), int(row[5]))


def _expected_index_term_signature(
    spec: _IndexTermSpec,
) -> tuple[str, int, str, int]:
    return (
        spec.name.casefold(),
        spec.descending,
        spec.collation.upper(),
        spec.key,
    )


def _index_key_terms(
    connection: sqlite3.Connection,
    index: str,
) -> tuple[tuple[str | None, int, str, int], ...]:
    quoted_index = _quote_identifier(index)
    return tuple(
        _index_term_signature(row)
        for row in connection.execute(f"pragma index_xinfo({quoted_index})")
        if int(row[5]) == 1
    )


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
    key_terms = _index_key_terms(connection, spec.name)
    expected_terms = tuple(
        _expected_index_term_signature(term) for term in spec.terms
    )
    actual = (
        int(index_row[2]),
        key_terms,
        int(index_row[4]),
        _canonical_partial_predicate(sql),
    )
    expected = (spec.unique, expected_terms, spec.partial, spec.predicate)
    if actual != expected:
        raise SchemaConflictError(
            f"SQLite schema conflict for {spec.name}: "
            f"expected {expected}, found {actual}"
        )


def _validate_owned_table_index_inventory(
    connection: sqlite3.Connection,
    table_spec: _TableSpec,
) -> None:
    quoted_table = _quote_identifier(table_spec.name)
    rows = tuple(connection.execute(f"pragma index_list({quoted_table})"))
    required = {
        spec.name.casefold(): spec
        for spec in _INDEX_SPECS
        if spec.table.casefold() == table_spec.name.casefold()
    }
    found_required = set()
    primary_key_rows = []
    unexpected = []
    for row in rows:
        name = str(row[1])
        origin = str(row[3]).casefold()
        if origin == "pk":
            primary_key_rows.append(row)
        elif name.casefold() in required and origin == "c":
            found_required.add(name.casefold())
        else:
            unexpected.append((name, origin))

    missing = sorted(required.keys() - found_required)
    if missing or unexpected:
        raise SchemaConflictError(
            f"SQLite schema conflict for {table_spec.name}: "
            f"index inventory missing={missing}, unexpected={unexpected}"
        )

    primary_key_columns = tuple(
        column
        for column in sorted(
            (column for column in table_spec.columns if column.primary_key),
            key=lambda column: column.primary_key,
        )
    )
    if len(primary_key_rows) != 1:
        raise SchemaConflictError(
            f"SQLite schema conflict for {table_spec.name}: "
            f"expected one primary-key autoindex, found {len(primary_key_rows)}"
        )
    primary_key_row = primary_key_rows[0]
    actual_primary_key = (
        int(primary_key_row[2]),
        str(primary_key_row[3]).casefold(),
        int(primary_key_row[4]),
        _index_key_terms(connection, str(primary_key_row[1])),
    )
    expected_primary_key = (
        1,
        "pk",
        0,
        tuple(
            _expected_index_term_signature(_IndexTermSpec(column.name))
            for column in primary_key_columns
        ),
    )
    if actual_primary_key != expected_primary_key:
        raise SchemaConflictError(
            f"SQLite schema conflict for {table_spec.name}: "
            f"expected PK index {expected_primary_key}, found {actual_primary_key}"
        )


def _validate_v2_schema(connection: sqlite3.Connection) -> None:
    for table_spec in _TABLE_SPECS:
        _validate_table(connection, table_spec)
    for table, column_specs in _ADDED_COLUMN_SPECS.items():
        _require_table(connection, table)
        actual = _column_rows_by_name(connection, table)
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
    for table_spec in _TABLE_SPECS:
        _validate_owned_table_index_inventory(connection, table_spec)


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
    if version > SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            f"Unsupported SQLite schema version {version}; "
            f"maximum supported version is {SCHEMA_VERSION}"
        )
    if version == SCHEMA_VERSION:
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
