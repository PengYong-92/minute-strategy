import sqlite3
from dataclasses import dataclass


SHADOW_SCHEMA_VERSION = 3
_MIGRATION_SAVEPOINT = "shadow_storage_schema_v3"


class ShadowSchemaConflictError(RuntimeError):
    pass


class UnsupportedShadowSchemaVersionError(RuntimeError):
    pass


@dataclass(frozen=True)
class _TableSpec:
    name: str
    create_sql: str
    columns: tuple[str, ...]


_TABLE_SPECS = (
    _TableSpec(
        "shadow_parameter_snapshots",
        """
        create table if not exists shadow_parameter_snapshots (
            parameter_hash text primary key,
            analyzer_hash text not null,
            parameter_family text not null,
            canonical_payload text not null,
            payload_bytes integer not null check(payload_bytes >= 0),
            created_at_ms integer not null
        )
        """,
        (
            "parameter_hash", "analyzer_hash", "parameter_family",
            "canonical_payload", "payload_bytes", "created_at_ms",
        ),
    ),
    _TableSpec(
        "shadow_experiments",
        """
        create table if not exists shadow_experiments (
            experiment_id text primary key,
            symbol text not null,
            parameter_family text not null,
            generation integer not null check(generation >= 0),
            status text not null,
            champion_arm_id text,
            created_at_ms integer not null,
            started_at_ms integer,
            completed_at_ms integer
        )
        """,
        (
            "experiment_id", "symbol", "parameter_family", "generation", "status",
            "champion_arm_id", "created_at_ms", "started_at_ms", "completed_at_ms",
        ),
    ),
    _TableSpec(
        "shadow_arms",
        """
        create table if not exists shadow_arms (
            arm_id text primary key,
            experiment_id text not null references shadow_experiments(experiment_id),
            parameter_hash text not null references shadow_parameter_snapshots(parameter_hash),
            parent_arm_id text,
            role text not null check(role in ('CHAMPION', 'CHALLENGER')),
            status text not null,
            effective_from_ms integer not null,
            retired_at_ms integer,
            created_at_ms integer not null,
            unique(experiment_id, parameter_hash)
        )
        """,
        (
            "arm_id", "experiment_id", "parameter_hash", "parent_arm_id", "role",
            "status", "effective_from_ms", "retired_at_ms", "created_at_ms",
        ),
    ),
    _TableSpec(
        "shadow_event_cursors",
        """
        create table if not exists shadow_event_cursors (
            arm_id text primary key references shadow_arms(arm_id),
            last_event_id text not null,
            last_closed_at_ms integer not null,
            last_bundle_hash text not null,
            gap_count integer not null default 0 check(gap_count >= 0),
            updated_at_ms integer not null
        )
        """,
        (
            "arm_id", "last_event_id", "last_closed_at_ms", "last_bundle_hash",
            "gap_count", "updated_at_ms",
        ),
    ),
    _TableSpec(
        "shadow_event_gaps",
        """
        create table if not exists shadow_event_gaps (
            arm_id text not null references shadow_arms(arm_id),
            event_id text not null,
            closed_at_ms integer not null,
            reason text not null,
            detected_at_ms integer not null,
            primary key(arm_id, event_id)
        ) without rowid
        """,
        ("arm_id", "event_id", "closed_at_ms", "reason", "detected_at_ms"),
    ),
    _TableSpec(
        "shadow_decisions",
        """
        create table if not exists shadow_decisions (
            arm_id text not null references shadow_arms(arm_id),
            event_id text not null,
            decision_index integer not null check(decision_index >= 0),
            closed_at_ms integer not null,
            decision text not null,
            direction text not null default '',
            profile_key text not null default '',
            terminal_at_ms integer,
            detail_payload text,
            payload_hash text not null,
            compacted_at_ms integer,
            primary key(arm_id, event_id, decision_index)
        ) without rowid
        """,
        (
            "arm_id", "event_id", "decision_index", "closed_at_ms", "decision",
            "direction", "profile_key", "terminal_at_ms", "detail_payload",
            "payload_hash", "compacted_at_ms",
        ),
    ),
    _TableSpec(
        "shadow_orders",
        """
        create table if not exists shadow_orders (
            arm_id text not null references shadow_arms(arm_id),
            order_id integer not null check(order_id > 0),
            decision_event_id text not null,
            direction text not null check(direction in ('LONG', 'SHORT')),
            status text not null check(status in ('OPEN', 'SETTLED')),
            result text check(result is null or result in ('WIN', 'LOSS')),
            opened_at_ms integer not null,
            expires_at_ms integer not null,
            settled_at_ms integer,
            entry_price real not null,
            exit_price real,
            stake real not null,
            pnl real not null default 0.0,
            detail_payload text,
            immutable_hash text not null,
            compacted_at_ms integer,
            updated_at_ms integer not null,
            primary key(arm_id, order_id)
        ) without rowid
        """,
        (
            "arm_id", "order_id", "decision_event_id", "direction", "status", "result",
            "opened_at_ms", "expires_at_ms", "settled_at_ms", "entry_price",
            "exit_price", "stake", "pnl", "detail_payload", "immutable_hash",
            "compacted_at_ms", "updated_at_ms",
        ),
    ),
    _TableSpec(
        "shadow_observations",
        """
        create table if not exists shadow_observations (
            arm_id text not null references shadow_arms(arm_id),
            observation_key text not null,
            decision_event_id text not null,
            strategy_family text not null,
            strategy_tag text not null,
            direction text not null check(direction in ('LONG', 'SHORT')),
            timeframe_minutes integer not null check(timeframe_minutes > 0),
            level text not null,
            reason text not null,
            entry_price real not null,
            opened_at integer not null,
            expires_at integer not null,
            threshold_segment text not null,
            status text not null check(status in ('OPEN', 'SETTLED')),
            result text check(result is null or result in ('WIN', 'LOSS')),
            exit_price real,
            settled_at integer,
            pnl real not null default 0.0,
            profile_key text not null,
            detail_payload text,
            immutable_hash text not null,
            compacted_at_ms integer,
            updated_at_ms integer not null,
            primary key(arm_id, observation_key)
        ) without rowid
        """,
        (
            "arm_id", "observation_key", "decision_event_id", "strategy_family",
            "strategy_tag", "direction", "timeframe_minutes", "level", "reason",
            "entry_price", "opened_at", "expires_at", "threshold_segment",
            "status", "result", "exit_price", "settled_at", "pnl",
            "profile_key", "detail_payload", "immutable_hash", "compacted_at_ms",
            "updated_at_ms",
        ),
    ),
    _TableSpec(
        "shadow_daily_rollups",
        """
        create table if not exists shadow_daily_rollups (
            arm_id text not null references shadow_arms(arm_id),
            trading_day text not null,
            payload text not null,
            payload_hash text not null,
            updated_at_ms integer not null,
            primary key(arm_id, trading_day)
        ) without rowid
        """,
        ("arm_id", "trading_day", "payload", "payload_hash", "updated_at_ms"),
    ),
    _TableSpec(
        "shadow_evaluations",
        """
        create table if not exists shadow_evaluations (
            evaluation_id text primary key,
            experiment_id text not null references shadow_experiments(experiment_id),
            arm_id text not null references shadow_arms(arm_id),
            evaluated_at_ms integer not null,
            decision text not null,
            metrics_payload text not null,
            payload_hash text not null
        )
        """,
        (
            "evaluation_id", "experiment_id", "arm_id", "evaluated_at_ms",
            "decision", "metrics_payload", "payload_hash",
        ),
    ),
    _TableSpec(
        "shadow_lifecycle_history",
        """
        create table if not exists shadow_lifecycle_history (
            history_id text primary key,
            experiment_id text not null references shadow_experiments(experiment_id),
            event_type text not null check(event_type in ('PROMOTION', 'ROLLBACK')),
            from_arm_id text references shadow_arms(arm_id),
            to_arm_id text references shadow_arms(arm_id),
            effective_at_ms integer not null,
            reason text not null,
            evidence_payload text not null,
            payload_hash text not null
        )
        """,
        (
            "history_id", "experiment_id", "event_type", "from_arm_id", "to_arm_id",
            "effective_at_ms", "reason", "evidence_payload", "payload_hash",
        ),
    ),
    _TableSpec(
        "shadow_runtime_state",
        """
        create table if not exists shadow_runtime_state (
            arm_id text primary key references shadow_arms(arm_id),
            state_version integer not null check(state_version > 0),
            state_payload text not null,
            payload_hash text not null,
            updated_at_ms integer not null
        )
        """,
        ("arm_id", "state_version", "state_payload", "payload_hash", "updated_at_ms"),
    ),
    _TableSpec(
        "shadow_formal_policy_receipts",
        """
        create table if not exists shadow_formal_policy_receipts (
            receipt_id text primary key,
            symbol text not null,
            experiment_id text not null references shadow_experiments(experiment_id),
            request_id text not null unique,
            action_type text not null check(action_type in ('PROMOTION', 'ROLLBACK', 'RESTORE')),
            generation integer not null check(generation >= 0),
            from_arm_id text references shadow_arms(arm_id),
            to_arm_id text not null references shadow_arms(arm_id),
            analyzer_hash text not null,
            parameter_hash text not null references shadow_parameter_snapshots(parameter_hash),
            policy_hash text not null,
            policy_payload text not null,
            applied_at_ms integer not null,
            payload_hash text not null
        )
        """,
        (
            "receipt_id", "symbol", "experiment_id", "request_id", "action_type",
            "generation", "from_arm_id", "to_arm_id", "analyzer_hash", "parameter_hash",
            "policy_hash", "policy_payload", "applied_at_ms", "payload_hash",
        ),
    ),
    _TableSpec(
        "shadow_decision_rollups",
        """
        create table if not exists shadow_decision_rollups (
            arm_id text not null references shadow_arms(arm_id),
            trading_day text not null,
            decision text not null,
            direction text not null,
            profile_key text not null,
            first_decisive_block text not null,
            occurrences integer not null check(occurrences > 0),
            minimum_score real not null,
            maximum_score real not null,
            minimum_threshold real not null,
            maximum_threshold real not null,
            updated_at_ms integer not null,
            primary key(
                arm_id, trading_day, decision, direction, profile_key,
                first_decisive_block
            )
        ) without rowid
        """,
        (
            "arm_id", "trading_day", "decision", "direction", "profile_key",
            "first_decisive_block", "occurrences", "minimum_score",
            "maximum_score", "minimum_threshold", "maximum_threshold",
            "updated_at_ms",
        ),
    ),
)


_INDEX_SQL = (
    "create index if not exists shadow_arms_experiment_status_idx "
    "on shadow_arms(experiment_id, status, role)",
    "create index if not exists shadow_orders_arm_status_expiry_idx "
    "on shadow_orders(arm_id, status, expires_at_ms)",
    "create index if not exists shadow_decisions_terminal_idx "
    "on shadow_decisions(terminal_at_ms, compacted_at_ms)",
    "create index if not exists shadow_orders_terminal_idx "
    "on shadow_orders(status, settled_at_ms, compacted_at_ms)",
    "create index if not exists shadow_observations_arm_status_expiry_idx "
    "on shadow_observations(arm_id, status, expires_at)",
    "create index if not exists shadow_observations_terminal_idx "
    "on shadow_observations(status, settled_at, compacted_at_ms)",
    "create index if not exists shadow_gaps_closed_idx "
    "on shadow_event_gaps(arm_id, closed_at_ms)",
    "create index if not exists shadow_evaluations_arm_time_idx "
    "on shadow_evaluations(arm_id, evaluated_at_ms)",
    "create index if not exists shadow_lifecycle_effective_idx "
    "on shadow_lifecycle_history(effective_at_ms)",
    "create index if not exists shadow_formal_receipts_symbol_time_idx "
    "on shadow_formal_policy_receipts(symbol, applied_at_ms desc)",
)


_PRIMARY_KEYS = {
    "shadow_parameter_snapshots": ("parameter_hash",),
    "shadow_experiments": ("experiment_id",),
    "shadow_arms": ("arm_id",),
    "shadow_event_cursors": ("arm_id",),
    "shadow_event_gaps": ("arm_id", "event_id"),
    "shadow_decisions": ("arm_id", "event_id", "decision_index"),
    "shadow_orders": ("arm_id", "order_id"),
    "shadow_observations": ("arm_id", "observation_key"),
    "shadow_daily_rollups": ("arm_id", "trading_day"),
    "shadow_evaluations": ("evaluation_id",),
    "shadow_lifecycle_history": ("history_id",),
    "shadow_runtime_state": ("arm_id",),
    "shadow_formal_policy_receipts": ("receipt_id",),
    "shadow_decision_rollups": (
        "arm_id", "trading_day", "decision", "direction", "profile_key",
        "first_decisive_block",
    ),
}


def _validate_table(connection: sqlite3.Connection, spec: _TableSpec) -> None:
    row = connection.execute(
        "select type from sqlite_master where name = ?", (spec.name,)
    ).fetchone()
    if row is None or str(row[0]).casefold() != "table":
        raise ShadowSchemaConflictError(
            f"shadow SQLite schema conflict: {spec.name} is not a table"
        )
    column_rows = connection.execute(f"pragma table_xinfo({spec.name})").fetchall()
    actual = tuple(str(column[1]) for column in column_rows)
    if actual != spec.columns:
        raise ShadowSchemaConflictError(
            f"shadow SQLite schema conflict for {spec.name}: "
            f"expected columns {spec.columns}, found {actual}"
        )
    primary_key = tuple(
        str(column[1])
        for column in sorted(column_rows, key=lambda item: int(item[5]))
        if int(column[5]) > 0
    )
    if primary_key != _PRIMARY_KEYS[spec.name]:
        raise ShadowSchemaConflictError(
            f"shadow SQLite schema conflict for {spec.name}: "
            f"expected primary key {_PRIMARY_KEYS[spec.name]}, found {primary_key}"
        )


def migrate_shadow_schema(connection: sqlite3.Connection) -> None:
    connection.execute("pragma foreign_keys = on")
    version = int(connection.execute("pragma user_version").fetchone()[0])
    if version > SHADOW_SCHEMA_VERSION:
        raise UnsupportedShadowSchemaVersionError(
            f"Unsupported shadow SQLite schema version {version}; "
            f"maximum supported version is {SHADOW_SCHEMA_VERSION}"
        )

    caller_in_transaction = connection.in_transaction
    connection.execute(f"savepoint {_MIGRATION_SAVEPOINT}")
    try:
        for spec in _TABLE_SPECS:
            connection.execute(spec.create_sql)
            _validate_table(connection, spec)
        for statement in _INDEX_SQL:
            connection.execute(statement)
        connection.execute(f"pragma user_version = {SHADOW_SCHEMA_VERSION}")
        connection.execute(f"release savepoint {_MIGRATION_SAVEPOINT}")
    except Exception:
        connection.execute(f"rollback to savepoint {_MIGRATION_SAVEPOINT}")
        connection.execute(f"release savepoint {_MIGRATION_SAVEPOINT}")
        if not caller_in_transaction and connection.in_transaction:
            connection.rollback()
        raise
