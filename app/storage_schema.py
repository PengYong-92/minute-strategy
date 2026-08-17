import sqlite3
from collections.abc import Sequence


SCHEMA_VERSION = 2
_MIGRATION_SAVEPOINT = "storage_schema_v2"


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f"pragma table_info({table})").fetchall()
    }


def _add_columns_if_absent(
    connection: sqlite3.Connection,
    table: str,
    columns: Sequence[tuple[str, str]],
) -> None:
    existing = _column_names(connection, table)
    for name, declaration in columns:
        if name in existing:
            continue
        connection.execute(
            f"alter table {table} add column {name} {declaration}"
        )
        existing.add(name)


def migrate(connection: sqlite3.Connection) -> None:
    version = int(connection.execute("pragma user_version").fetchone()[0])
    if version >= SCHEMA_VERSION:
        return

    connection.execute(f"savepoint {_MIGRATION_SAVEPOINT}")
    try:
        connection.execute(
            """
            create table if not exists runtime_config_snapshots (
                runtime_config_hash text primary key,
                context_version text not null,
                strategy_build_id text not null,
                canonical_payload text not null,
                payload_bytes integer not null,
                created_at_ms integer not null
            )
            """
        )
        connection.execute(
            """
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
            """
        )

        _add_columns_if_absent(
            connection,
            "orders",
            (
                ("decision_id", "text"),
                ("runtime_config_hash", "text"),
            ),
        )
        _add_columns_if_absent(
            connection,
            "observation_signals",
            (
                ("decision_id", "text"),
                ("runtime_config_hash", "text"),
                ("context_version", "text"),
                ("candidate_origin", "text"),
                ("qualification_state", "text"),
                ("adaptive_state", "text"),
                ("entry_structure_state", "text"),
                ("entry_structure_bias", "text"),
                ("active_level_source", "text"),
            ),
        )
        _add_columns_if_absent(
            connection,
            "order_entry_snapshots",
            (
                ("decision_id", "text"),
                ("context_version", "text"),
                ("runtime_config_hash", "text"),
            ),
        )
        _add_columns_if_absent(
            connection,
            "signal_audit",
            (
                ("record_version", "text"),
                ("decision_id", "text"),
                ("runtime_config_hash", "text"),
                ("event_kind", "text"),
                ("first_at_ms", "integer"),
                ("last_at_ms", "integer"),
                ("occurrences", "integer not null default 1"),
                ("score_min", "real"),
                ("score_max", "real"),
                ("aggregation_key", "text"),
            ),
        )

        connection.execute(
            """
            create index if not exists
                idx_decision_contexts_symbol_closed_kline
            on decision_contexts(symbol, closed_kline_at_ms)
            """
        )
        connection.execute(
            """
            create index if not exists
                idx_decision_contexts_symbol_profile_closed_kline
            on decision_contexts(symbol, profile_key, closed_kline_at_ms)
            """
        )
        connection.execute(
            """
            create index if not exists
                idx_observation_signals_symbol_candidate_origin_opened
            on observation_signals(symbol, candidate_origin, opened_at)
            """
        )
        connection.execute(
            """
            create index if not exists
                idx_observation_signals_symbol_adaptive_state_opened
            on observation_signals(symbol, adaptive_state, opened_at)
            """
        )
        connection.execute(
            """
            create index if not exists
                idx_observation_signals_symbol_entry_structure_bias_opened
            on observation_signals(symbol, entry_structure_bias, opened_at)
            """
        )
        connection.execute(
            """
            create unique index if not exists
                ux_signal_audit_symbol_aggregation_key
            on signal_audit(symbol, aggregation_key)
            where aggregation_key is not null
            """
        )
        connection.execute(f"pragma user_version = {SCHEMA_VERSION}")
    except Exception:
        connection.execute(f"rollback to savepoint {_MIGRATION_SAVEPOINT}")
        connection.execute(f"release savepoint {_MIGRATION_SAVEPOINT}")
        raise
    else:
        connection.execute(f"release savepoint {_MIGRATION_SAVEPOINT}")
