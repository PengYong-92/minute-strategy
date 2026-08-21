import hashlib
import json
import math
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Callable

from app.shadow_storage_schema import migrate_shadow_schema


HARD_LIMIT_BYTES = 5 * 1024**3
WARNING_BYTES = 4 * 1024**3
COMPACT_ONLY_BYTES = int(4.5 * 1024**3)
CORE_RESERVE_BYTES = 512 * 1024**2
SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))


class ShadowStorageConflictError(RuntimeError):
    pass


class ShadowStorageHardLimitError(RuntimeError):
    pass


@dataclass(frozen=True)
class ShadowStorageCapacity:
    status: str
    database_bytes: int
    max_database_bytes: int = HARD_LIMIT_BYTES
    core_reserve_bytes: int = CORE_RESERVE_BYTES
    physical_database_bytes: int | None = None
    reclaimable_bytes: int = 0

    @property
    def detail_writes_allowed(self) -> bool:
        return self.status in {"NORMAL", "WARNING"}

    @property
    def core_writes_allowed(self) -> bool:
        return True


def classify_shadow_capacity(database_bytes: int) -> str:
    if database_bytes < 0:
        raise ValueError("database_bytes must be non-negative")
    if database_bytes >= HARD_LIMIT_BYTES:
        return "HARD_LIMIT"
    if database_bytes >= COMPACT_ONLY_BYTES:
        return "COMPACT_ONLY"
    if database_bytes >= WARNING_BYTES:
        return "WARNING"
    return "NORMAL"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _payload_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _decode_json(payload: str | None) -> Any:
    if payload is None:
        return None
    return json.loads(payload)


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _finite_float(value: object) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _trading_day(closed_at_ms: int) -> str:
    return datetime.fromtimestamp(
        int(closed_at_ms) / 1_000,
        tz=SHANGHAI_TIMEZONE,
    ).date().isoformat()


class ShadowSQLiteStore:
    """Independent SQLite ledger for forward-only shadow experiments."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._bundle_step_hook: Callable[[str], None] | None = None
        if not self.path.exists() or self.path.stat().st_size == 0:
            with closing(sqlite3.connect(self.path)) as connection:
                connection.execute("pragma auto_vacuum = incremental")
                connection.commit()
        with self._connect() as connection:
            migrate_shadow_schema(connection)

    @contextmanager
    def _connect(self, *, timeout: float = 5.0) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=timeout)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("pragma foreign_keys = on")
            connection.execute("pragma busy_timeout = 5000")
            connection.execute("pragma journal_mode = wal")
            page_size = int(connection.execute("pragma page_size").fetchone()[0])
            connection.execute(
                f"pragma max_page_count = {HARD_LIMIT_BYTES // page_size}"
            )
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(
        self, *, allow_hard_limit_compaction: bool = False
    ) -> Iterator[sqlite3.Connection]:
        with self._connect() as connection:
            capacity = self._capacity_from_connection(connection)
            if not capacity.core_writes_allowed and not allow_hard_limit_compaction:
                raise ShadowStorageHardLimitError(
                    "shadow SQLite reached the 5 GiB hard limit"
                )
            connection.execute("begin immediate")
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _step(self, name: str) -> None:
        if self._bundle_step_hook is not None:
            self._bundle_step_hook(name)

    def _capacity_from_connection(
        self,
        connection: sqlite3.Connection,
    ) -> ShadowStorageCapacity:
        page_size = int(connection.execute("pragma page_size").fetchone()[0])
        page_count = int(connection.execute("pragma page_count").fetchone()[0])
        freelist_count = int(
            connection.execute("pragma freelist_count").fetchone()[0]
        )
        wal_path = Path(f"{self.path}-wal")
        try:
            wal_bytes = wal_path.stat().st_size
        except FileNotFoundError:
            wal_bytes = 0
        physical_database_bytes = page_size * page_count + wal_bytes
        reclaimable_bytes = page_size * freelist_count
        database_bytes = max(0, physical_database_bytes - reclaimable_bytes)
        return ShadowStorageCapacity(
            status=classify_shadow_capacity(database_bytes),
            database_bytes=database_bytes,
            physical_database_bytes=physical_database_bytes,
            reclaimable_bytes=reclaimable_bytes,
        )

    def storage_capacity(self) -> ShadowStorageCapacity:
        with self._connect() as connection:
            return self._capacity_from_connection(connection)

    def save_parameter_snapshot(
        self,
        *,
        parameter_hash: str,
        analyzer_hash: str,
        parameter_family: str,
        payload: Mapping[str, Any],
        created_at_ms: int,
    ) -> None:
        canonical = _canonical_json(payload)
        immutable = (analyzer_hash, parameter_family, canonical)
        with self._transaction() as connection:
            existing = connection.execute(
                """
                select analyzer_hash, parameter_family, canonical_payload
                from shadow_parameter_snapshots where parameter_hash = ?
                """,
                (parameter_hash,),
            ).fetchone()
            if existing is not None:
                actual = tuple(existing)
                if actual != immutable:
                    raise ShadowStorageConflictError(
                        f"parameter hash {parameter_hash!r} maps to different payloads"
                    )
                return
            connection.execute(
                """
                insert into shadow_parameter_snapshots(
                    parameter_hash, analyzer_hash, parameter_family,
                    canonical_payload, payload_bytes, created_at_ms
                ) values (?, ?, ?, ?, ?, ?)
                """,
                (
                    parameter_hash,
                    analyzer_hash,
                    parameter_family,
                    canonical,
                    len(canonical.encode("utf-8")),
                    int(created_at_ms),
                ),
            )

    def list_parameter_snapshots(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "select * from shadow_parameter_snapshots order by created_at_ms, parameter_hash"
            ).fetchall()
        result = []
        for row in rows:
            item = _row_dict(row)
            item["payload"] = _decode_json(item.pop("canonical_payload"))
            result.append(item)
        return result

    def save_formal_policy_receipt(
        self,
        request: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> None:
        status = str(result.get("status") or "").upper()
        if status not in {"APPLIED", "APPLIED_AT_DEADLINE"}:
            raise ValueError("formal policy receipt requires an applied result")
        action_types = {
            "PROMOTION_REQUEST": "PROMOTION",
            "ROLLBACK_REQUEST": "ROLLBACK",
            "RESTORE_CHAMPION_REQUEST": "RESTORE",
        }
        request_type = str(request.get("type") or "").upper()
        action_type = action_types.get(request_type)
        if action_type is None:
            raise ValueError(f"unsupported formal policy request type: {request_type}")
        request_id = str(request.get("request_id") or "").strip()
        experiment_id = str(request.get("experiment_id") or "").strip()
        to_arm_id = str(request.get("to_arm_id") or "").strip()
        parameter_hash = str(request.get("parameter_hash") or "").strip()
        policy = request.get("policy")
        generation = request.get("generation")
        if not all((request_id, experiment_id, to_arm_id, parameter_hash)):
            raise ValueError("formal policy receipt requires lifecycle identifiers")
        if not isinstance(policy, Mapping):
            raise ValueError("formal policy receipt requires policy payload")
        if type(generation) is not int or generation < 0:
            raise ValueError("formal policy receipt requires generation")
        normalized_policy = dict(policy)
        policy_hash = _payload_hash(normalized_policy)
        result_policy_hash = str(result.get("policy_hash") or "")
        if result_policy_hash != policy_hash:
            raise ShadowStorageConflictError(
                "applied policy hash does not match lifecycle request"
            )
        if (
            result.get("request_id") is not None
            and str(result["request_id"]) != request_id
        ):
            raise ShadowStorageConflictError(
                "applied result request id does not match lifecycle request"
            )
        applied_at_ms = int(result.get("applied_at_ms") or 0)
        if applied_at_ms <= 0:
            raise ValueError("formal policy receipt requires applied_at_ms")

        with self._transaction() as connection:
            target = connection.execute(
                """
                select e.symbol, e.generation, a.parameter_hash, p.analyzer_hash,
                       p.canonical_payload
                from shadow_experiments e
                join shadow_arms a on a.experiment_id = e.experiment_id
                join shadow_parameter_snapshots p
                  on p.parameter_hash = a.parameter_hash
                where e.experiment_id = ? and a.arm_id = ?
                """,
                (experiment_id, to_arm_id),
            ).fetchone()
            if target is None or str(target["parameter_hash"]) != parameter_hash:
                raise ShadowStorageConflictError(
                    "formal policy target does not match experiment arm"
                )
            request_symbol = str(request.get("symbol") or "").strip().upper()
            if request_symbol != str(target["symbol"]):
                raise ShadowStorageConflictError(
                    "formal policy request symbol does not match experiment"
                )
            if (
                action_type != "RESTORE"
                and generation != int(target["generation"])
            ):
                raise ShadowStorageConflictError(
                    "formal policy request generation does not match experiment"
                )
            snapshot = _decode_json(str(target["canonical_payload"]))
            snapshot_policy = (
                snapshot.get("parameters", {}).get("profile_admission_policy")
                if isinstance(snapshot, dict)
                and isinstance(snapshot.get("parameters"), dict)
                else None
            )
            if snapshot_policy != normalized_policy:
                raise ShadowStorageConflictError(
                    "formal policy does not match immutable parameter snapshot"
                )
            from_arm_id = str(request.get("from_arm_id") or "").strip() or None
            if from_arm_id is not None:
                source = connection.execute(
                    """
                    select 1 from shadow_arms
                    where experiment_id = ? and arm_id = ?
                    """,
                    (experiment_id, from_arm_id),
                ).fetchone()
                if source is None:
                    raise ShadowStorageConflictError(
                        "formal policy source does not belong to experiment"
                    )
            normalized = {
                "symbol": str(target["symbol"]),
                "experiment_id": experiment_id,
                "request_id": request_id,
                "action_type": action_type,
                "generation": generation,
                "from_arm_id": from_arm_id,
                "to_arm_id": to_arm_id,
                "analyzer_hash": str(target["analyzer_hash"]),
                "parameter_hash": parameter_hash,
                "policy_hash": policy_hash,
                "policy": normalized_policy,
                "applied_at_ms": applied_at_ms,
            }
            payload_hash = _payload_hash(normalized)
            receipt_id = f"formal-{_payload_hash({'request_id': request_id})[:32]}"
            existing = connection.execute(
                """
                select payload_hash from shadow_formal_policy_receipts
                where request_id = ?
                """,
                (request_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_hash"]) != payload_hash:
                    raise ShadowStorageConflictError(
                        f"formal policy receipt {request_id!r} changed"
                    )
                return
            connection.execute(
                """
                insert into shadow_formal_policy_receipts(
                    receipt_id, symbol, experiment_id, request_id, action_type,
                    generation, from_arm_id, to_arm_id, analyzer_hash, parameter_hash,
                    policy_hash, policy_payload, applied_at_ms, payload_hash
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    normalized["symbol"],
                    experiment_id,
                    request_id,
                    action_type,
                    generation,
                    from_arm_id,
                    to_arm_id,
                    normalized["analyzer_hash"],
                    parameter_hash,
                    policy_hash,
                    _canonical_json(normalized_policy),
                    applied_at_ms,
                    payload_hash,
                ),
            )

    def load_latest_formal_policy_receipt(
        self,
        symbol: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                select * from shadow_formal_policy_receipts
                where symbol = ?
                order by applied_at_ms desc, receipt_id desc
                limit 1
                """,
                (str(symbol).strip().upper(),),
            ).fetchone()
        if row is None:
            return None
        result = _row_dict(row)
        result["policy"] = _decode_json(result.pop("policy_payload"))
        return result

    def create_experiment(
        self,
        *,
        experiment_id: str,
        symbol: str,
        parameter_family: str,
        generation: int,
        created_at_ms: int,
        arms: Sequence[Mapping[str, Any]],
        status: str = "RUNNING",
        champion_arm_id: str | None = None,
        started_at_ms: int | None = None,
    ) -> None:
        normalized_arms = [
            {
                "arm_id": str(item["arm_id"]),
                "parameter_hash": str(item["parameter_hash"]),
                "parent_arm_id": item.get("parent_arm_id"),
                "role": str(item.get("role", "CHALLENGER")),
                "status": str(item.get("status", "PENDING")),
                "effective_from_ms": int(item["effective_from_ms"]),
                "retired_at_ms": item.get("retired_at_ms"),
                "created_at_ms": int(item.get("created_at_ms", created_at_ms)),
            }
            for item in arms
        ]
        effective_champion = champion_arm_id or next(
            (
                item["arm_id"]
                for item in normalized_arms
                if item["role"] == "CHAMPION"
            ),
            None,
        )
        with self._transaction() as connection:
            existing_experiment = connection.execute(
                "select * from shadow_experiments where experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            if existing_experiment is not None:
                expected_experiment = (
                    symbol,
                    parameter_family,
                    int(generation),
                    status,
                    effective_champion,
                    int(created_at_ms),
                    started_at_ms,
                )
                actual_experiment = (
                    existing_experiment["symbol"],
                    existing_experiment["parameter_family"],
                    existing_experiment["generation"],
                    existing_experiment["status"],
                    existing_experiment["champion_arm_id"],
                    existing_experiment["created_at_ms"],
                    existing_experiment["started_at_ms"],
                )
                stored_arms = connection.execute(
                    "select * from shadow_arms where experiment_id = ?",
                    (experiment_id,),
                ).fetchall()
                actual_arms = {
                    row["arm_id"]: (
                        row["parameter_hash"], row["parent_arm_id"], row["role"],
                        row["status"], row["effective_from_ms"], row["retired_at_ms"],
                        row["created_at_ms"],
                    )
                    for row in stored_arms
                }
                expected_arms = {
                    item["arm_id"]: (
                        item["parameter_hash"], item["parent_arm_id"], item["role"],
                        item["status"], item["effective_from_ms"], item["retired_at_ms"],
                        item["created_at_ms"],
                    )
                    for item in normalized_arms
                }
                if actual_experiment != expected_experiment or actual_arms != expected_arms:
                    raise ShadowStorageConflictError(
                        f"experiment {experiment_id!r} maps to a different definition"
                    )
                return

            reused = connection.execute(
                "select arm_id, experiment_id from shadow_arms where arm_id in ("
                + ",".join("?" for _ in normalized_arms)
                + ") limit 1",
                tuple(item["arm_id"] for item in normalized_arms),
            ).fetchone() if normalized_arms else None
            if reused is not None:
                raise ShadowStorageConflictError(
                    f"arm {reused['arm_id']!r} already belongs to "
                    f"experiment {reused['experiment_id']!r}"
                )
            connection.execute(
                """
                insert into shadow_experiments(
                    experiment_id, symbol, parameter_family, generation, status,
                    champion_arm_id, created_at_ms, started_at_ms
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    experiment_id,
                    symbol,
                    parameter_family,
                    int(generation),
                    status,
                    effective_champion,
                    int(created_at_ms),
                    started_at_ms,
                ),
            )
            for item in normalized_arms:
                connection.execute(
                    """
                    insert into shadow_arms(
                        arm_id, experiment_id, parameter_hash, parent_arm_id,
                        role, status, effective_from_ms, retired_at_ms, created_at_ms
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["arm_id"],
                        experiment_id,
                        item["parameter_hash"],
                        item.get("parent_arm_id"),
                        item["role"],
                        item["status"],
                        item["effective_from_ms"],
                        item["retired_at_ms"],
                        item["created_at_ms"],
                    ),
                )

    def load_experiment(self, experiment_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "select * from shadow_experiments where experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            if row is None:
                return None
            arms = connection.execute(
                "select * from shadow_arms where experiment_id = ? order by rowid",
                (experiment_id,),
            ).fetchall()
        result = _row_dict(row)
        result["arms"] = [_row_dict(item) for item in arms]
        return result

    def find_latest_running_experiment(
        self,
        symbol: str,
        parameter_family: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                select * from shadow_experiments
                where symbol = ? and parameter_family = ? and status = 'RUNNING'
                order by created_at_ms desc, experiment_id desc
                limit 1
                """,
                (symbol, parameter_family),
            ).fetchone()
            if row is None:
                return None
            arms = connection.execute(
                """
                select * from shadow_arms
                where experiment_id = ?
                order by created_at_ms desc, arm_id desc
                """,
                (row["experiment_id"],),
            ).fetchall()
        result = _row_dict(row)
        result["arms"] = [_row_dict(item) for item in arms]
        return result

    def experiment_cursor_bounds(
        self,
        experiment_id: str,
    ) -> dict[str, int | None]:
        with self._connect() as connection:
            row = connection.execute(
                """
                select count(c.arm_id) as cursor_count,
                       min(c.last_closed_at_ms) as minimum_closed_at_ms,
                       max(c.last_closed_at_ms) as maximum_closed_at_ms
                from shadow_arms a
                left join shadow_event_cursors c on c.arm_id = a.arm_id
                where a.experiment_id = ?
                """,
                (str(experiment_id),),
            ).fetchone()
        return {
            "cursor_count": int(row["cursor_count"] or 0),
            "minimum_closed_at_ms": (
                None
                if row["minimum_closed_at_ms"] is None
                else int(row["minimum_closed_at_ms"])
            ),
            "maximum_closed_at_ms": (
                None
                if row["maximum_closed_at_ms"] is None
                else int(row["maximum_closed_at_ms"])
            ),
        }

    def mark_experiment_status(
        self,
        experiment_id: str,
        *,
        status: str,
        completed_at_ms: int,
    ) -> None:
        normalized_status = str(status).strip().upper()
        if not normalized_status:
            raise ValueError("experiment status must not be empty")
        with self._transaction() as connection:
            changed = connection.execute(
                """
                update shadow_experiments
                set status = ?, completed_at_ms = ?
                where experiment_id = ? and status = 'RUNNING'
                """,
                (normalized_status, int(completed_at_ms), str(experiment_id)),
            )
            if changed.rowcount not in {0, 1}:
                raise ShadowStorageConflictError(
                    f"unexpected experiment update count for {experiment_id!r}"
                )

    def save_event_bundle(
        self,
        *,
        arm_id: str,
        event_id: str,
        closed_at_ms: int,
        decisions: Sequence[Mapping[str, Any]] = (),
        orders: Sequence[Mapping[str, Any]] = (),
        observations: Sequence[Mapping[str, Any]] = (),
        runtime_state: Mapping[str, Any] | None = None,
        daily_rollup: Mapping[str, Any] | None = None,
        gap: Mapping[str, Any] | None = None,
        state_version: int = 1,
    ) -> None:
        capacity = self.storage_capacity()
        if not capacity.detail_writes_allowed:
            decisions = tuple(
                decision
                for decision in decisions
                if str(decision.get("decision", "")).upper()
                in {"OPEN", "OPENED", "STORAGE_ERROR", "GAP"}
                or bool(decision.get("retain_at_capacity", False))
            )
        normalized = {
            "arm_id": arm_id,
            "event_id": event_id,
            "closed_at_ms": int(closed_at_ms),
            "decisions": list(decisions),
            "orders": list(orders),
            "observations": list(observations),
            "runtime_state": runtime_state,
            "daily_rollup": daily_rollup,
            "gap": gap,
            "state_version": int(state_version),
        }
        bundle_hash = _payload_hash(normalized)
        with self._transaction() as connection:
            cursor = connection.execute(
                "select * from shadow_event_cursors where arm_id = ?", (arm_id,)
            ).fetchone()
            if cursor is not None and cursor["last_event_id"] == event_id:
                if cursor["last_bundle_hash"] != bundle_hash:
                    raise ShadowStorageConflictError(
                        f"event {event_id!r} for arm {arm_id!r} was replayed with different data"
                    )
                return
            if cursor is not None and int(closed_at_ms) <= int(cursor["last_closed_at_ms"]):
                raise ShadowStorageConflictError(
                    f"event time {closed_at_ms} does not advance arm {arm_id!r} cursor"
                )

            self._save_decisions(
                connection, arm_id, event_id, int(closed_at_ms), decisions
            )
            self._step("decisions")
            self._save_orders(connection, arm_id, orders, int(closed_at_ms))
            self._step("orders")
            self._save_observations(
                connection, arm_id, observations, int(closed_at_ms)
            )
            self._step("observations")
            if daily_rollup is not None:
                self._save_daily_rollup(
                    connection, arm_id, daily_rollup, int(closed_at_ms)
                )
            self._step("daily_rollup")
            if runtime_state is not None:
                self._save_runtime_state(
                    connection,
                    arm_id,
                    runtime_state,
                    state_version=int(state_version),
                    updated_at_ms=int(closed_at_ms),
                )
            self._step("runtime_state")
            gap_count = int(cursor["gap_count"]) if cursor is not None else 0
            if gap is not None:
                connection.execute(
                    """
                    insert into shadow_event_gaps(
                        arm_id, event_id, closed_at_ms, reason, detected_at_ms
                    ) values (?, ?, ?, ?, ?)
                    """,
                    (
                        arm_id,
                        event_id,
                        int(closed_at_ms),
                        str(gap["reason"]),
                        int(gap.get("detected_at_ms", closed_at_ms)),
                    ),
                )
                gap_count += 1
            self._step("gap")
            connection.execute(
                """
                insert into shadow_event_cursors(
                    arm_id, last_event_id, last_closed_at_ms, last_bundle_hash,
                    gap_count, updated_at_ms
                ) values (?, ?, ?, ?, ?, ?)
                on conflict(arm_id) do update set
                    last_event_id = excluded.last_event_id,
                    last_closed_at_ms = excluded.last_closed_at_ms,
                    last_bundle_hash = excluded.last_bundle_hash,
                    gap_count = excluded.gap_count,
                    updated_at_ms = excluded.updated_at_ms
                """,
                (
                    arm_id,
                    event_id,
                    int(closed_at_ms),
                    bundle_hash,
                    gap_count,
                    int(closed_at_ms),
                ),
            )
            self._step("cursor")

    @staticmethod
    def _save_decisions(
        connection: sqlite3.Connection,
        arm_id: str,
        event_id: str,
        closed_at_ms: int,
        decisions: Sequence[Mapping[str, Any]],
    ) -> None:
        for index, decision in enumerate(decisions):
            detail = decision.get("detail", {})
            payload_hash = _payload_hash(dict(decision))
            connection.execute(
                """
                insert into shadow_decisions(
                    arm_id, event_id, decision_index, closed_at_ms, decision,
                    direction, profile_key, terminal_at_ms, detail_payload,
                    payload_hash, compacted_at_ms
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, null)
                """,
                (
                    arm_id,
                    event_id,
                    index,
                    closed_at_ms,
                    str(decision["decision"]),
                    str(decision.get("direction", "")),
                    str(decision.get("profile_key", "")),
                    decision.get("terminal_at_ms"),
                    _canonical_json(detail),
                    payload_hash,
                ),
            )

    @staticmethod
    def _order_immutable(order: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: order.get(key)
            for key in (
                "order_id",
                "decision_event_id",
                "direction",
                "opened_at_ms",
                "expires_at_ms",
                "entry_price",
                "stake",
            )
        }

    def _save_orders(
        self,
        connection: sqlite3.Connection,
        arm_id: str,
        orders: Sequence[Mapping[str, Any]],
        updated_at_ms: int,
    ) -> None:
        for order in orders:
            self._validate_order_lifecycle(order)
            order_id = int(order["order_id"])
            immutable_hash = _payload_hash(self._order_immutable(order))
            existing = connection.execute(
                "select * from shadow_orders where arm_id = ? and order_id = ?",
                (arm_id, order_id),
            ).fetchone()
            detail_payload = _canonical_json(order.get("detail", {}))
            if existing is None:
                connection.execute(
                    """
                    insert into shadow_orders(
                        arm_id, order_id, decision_event_id, direction, status,
                        result, opened_at_ms, expires_at_ms, settled_at_ms,
                        entry_price, exit_price, stake, pnl, detail_payload,
                        immutable_hash, compacted_at_ms, updated_at_ms
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, null, ?)
                    """,
                    (
                        arm_id,
                        order_id,
                        str(order["decision_event_id"]),
                        str(order["direction"]).upper(),
                        str(order["status"]).upper(),
                        order.get("result"),
                        int(order["opened_at_ms"]),
                        int(order["expires_at_ms"]),
                        order.get("settled_at_ms"),
                        float(order["entry_price"]),
                        order.get("exit_price"),
                        float(order["stake"]),
                        float(order.get("pnl", 0.0)),
                        detail_payload,
                        immutable_hash,
                        updated_at_ms,
                    ),
                )
                continue
            if existing["immutable_hash"] != immutable_hash:
                raise ShadowStorageConflictError(
                    f"order {arm_id}/{order_id} immutable fields changed"
                )
            requested_status = str(order["status"]).upper()
            if existing["status"] == "SETTLED" and requested_status != "SETTLED":
                raise ShadowStorageConflictError(
                    f"settled order {arm_id}/{order_id} cannot reopen"
                )
            if existing["status"] == "SETTLED":
                actual = (
                    existing["result"], existing["settled_at_ms"],
                    existing["exit_price"], float(existing["pnl"]),
                )
                requested = (
                    order.get("result"), order.get("settled_at_ms"),
                    order.get("exit_price"), float(order.get("pnl", 0.0)),
                )
                if actual != requested:
                    raise ShadowStorageConflictError(
                        f"settled order {arm_id}/{order_id} changed"
                    )
                continue
            connection.execute(
                """
                update shadow_orders set
                    status = ?, result = ?, settled_at_ms = ?, exit_price = ?,
                    pnl = ?, detail_payload = ?, updated_at_ms = ?
                where arm_id = ? and order_id = ?
                """,
                (
                    requested_status,
                    order.get("result"),
                    order.get("settled_at_ms"),
                    order.get("exit_price"),
                    float(order.get("pnl", 0.0)),
                    detail_payload,
                    updated_at_ms,
                    arm_id,
                    order_id,
                ),
            )

    @staticmethod
    def _validate_order_lifecycle(order: Mapping[str, Any]) -> None:
        status = str(order.get("status", "")).upper()
        result = order.get("result")
        settled_at_ms = order.get("settled_at_ms")
        exit_price = order.get("exit_price")
        pnl = float(order.get("pnl", 0.0))
        if status == "OPEN":
            if result is not None or settled_at_ms is not None or exit_price is not None or pnl != 0.0:
                raise ValueError("OPEN shadow order cannot contain settlement fields")
            return
        if status != "SETTLED":
            raise ValueError("shadow order status must be OPEN or SETTLED")
        if result not in {"WIN", "LOSS"}:
            raise ValueError("SETTLED shadow order requires WIN or LOSS result")
        if settled_at_ms is None or exit_price is None:
            raise ValueError("SETTLED shadow order requires settlement time and exit price")
        if int(settled_at_ms) < int(order["opened_at_ms"]):
            raise ValueError("shadow order cannot settle before it opens")

    @staticmethod
    def _observation_immutable(observation: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "observation_key": str(observation["observation_key"]),
            "decision_event_id": str(observation["decision_event_id"]),
            "strategy_family": str(observation["strategy_family"]),
            "strategy_tag": str(observation["strategy_tag"]),
            "direction": str(observation["direction"]).upper(),
            "timeframe_minutes": int(observation["timeframe_minutes"]),
            "level": str(observation["level"]),
            "reason": str(observation["reason"]),
            "entry_price": float(observation["entry_price"]),
            "opened_at": int(observation["opened_at"]),
            "expires_at": int(observation["expires_at"]),
            "threshold_segment": str(observation["threshold_segment"]),
            "profile_key": str(observation.get("profile_key", "")),
            "detail": observation.get("detail", {}),
        }

    def _save_observations(
        self,
        connection: sqlite3.Connection,
        arm_id: str,
        observations: Sequence[Mapping[str, Any]],
        updated_at_ms: int,
    ) -> None:
        for observation in observations:
            self._validate_observation_lifecycle(observation)
            observation_key = str(observation["observation_key"])
            immutable = self._observation_immutable(observation)
            immutable_hash = _payload_hash(immutable)
            detail_payload = _canonical_json(observation.get("detail", {}))
            existing = connection.execute(
                """
                select * from shadow_observations
                where arm_id = ? and observation_key = ?
                """,
                (arm_id, observation_key),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    insert into shadow_observations(
                        arm_id, observation_key, decision_event_id,
                        strategy_family, strategy_tag, direction,
                        timeframe_minutes, level, reason, entry_price,
                        opened_at, expires_at, threshold_segment,
                        status, result, exit_price, settled_at, pnl,
                        profile_key, detail_payload, immutable_hash,
                        compacted_at_ms, updated_at_ms
                    ) values (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, null, ?
                    )
                    """,
                    (
                        arm_id,
                        observation_key,
                        immutable["decision_event_id"],
                        immutable["strategy_family"],
                        immutable["strategy_tag"],
                        immutable["direction"],
                        immutable["timeframe_minutes"],
                        immutable["level"],
                        immutable["reason"],
                        immutable["entry_price"],
                        immutable["opened_at"],
                        immutable["expires_at"],
                        immutable["threshold_segment"],
                        str(observation["status"]).upper(),
                        observation.get("result"),
                        observation.get("exit_price"),
                        observation.get("settled_at"),
                        float(observation.get("pnl", 0.0)),
                        immutable["profile_key"],
                        detail_payload,
                        immutable_hash,
                        updated_at_ms,
                    ),
                )
                continue
            if existing["immutable_hash"] != immutable_hash:
                raise ShadowStorageConflictError(
                    f"observation {arm_id}/{observation_key} immutable fields changed"
                )
            requested_status = str(observation["status"]).upper()
            if existing["status"] == "SETTLED":
                if requested_status != "SETTLED":
                    raise ShadowStorageConflictError(
                        f"settled observation {arm_id}/{observation_key} cannot reopen"
                    )
                actual = (
                    existing["result"],
                    existing["settled_at"],
                    existing["exit_price"],
                    float(existing["pnl"]),
                )
                requested = (
                    observation.get("result"),
                    observation.get("settled_at"),
                    observation.get("exit_price"),
                    float(observation.get("pnl", 0.0)),
                )
                if actual != requested:
                    raise ShadowStorageConflictError(
                        f"settled observation {arm_id}/{observation_key} changed"
                    )
                continue
            if requested_status == "OPEN":
                continue
            connection.execute(
                """
                update shadow_observations set
                    status = ?, result = ?, exit_price = ?, settled_at = ?,
                    pnl = ?, detail_payload = ?, updated_at_ms = ?
                where arm_id = ? and observation_key = ?
                """,
                (
                    requested_status,
                    observation.get("result"),
                    observation.get("exit_price"),
                    observation.get("settled_at"),
                    float(observation.get("pnl", 0.0)),
                    detail_payload,
                    updated_at_ms,
                    arm_id,
                    observation_key,
                ),
            )

    @staticmethod
    def _validate_observation_lifecycle(
        observation: Mapping[str, Any],
    ) -> None:
        status = str(observation.get("status", "")).upper()
        result = observation.get("result")
        settled_at = observation.get("settled_at")
        exit_price = observation.get("exit_price")
        pnl = float(observation.get("pnl", 0.0))
        if status == "OPEN":
            if (
                result is not None
                or settled_at is not None
                or exit_price is not None
                or pnl != 0.0
            ):
                raise ValueError(
                    "OPEN shadow observation cannot contain settlement fields"
                )
            return
        if status != "SETTLED":
            raise ValueError("shadow observation status must be OPEN or SETTLED")
        if result not in {"WIN", "LOSS"}:
            raise ValueError(
                "SETTLED shadow observation requires WIN or LOSS result"
            )
        if settled_at is None or exit_price is None:
            raise ValueError(
                "SETTLED shadow observation requires settlement time and exit price"
            )
        if int(settled_at) < int(observation["opened_at"]):
            raise ValueError("shadow observation cannot settle before it opens")

    @staticmethod
    def _save_daily_rollup(
        connection: sqlite3.Connection,
        arm_id: str,
        rollup: Mapping[str, Any],
        updated_at_ms: int,
    ) -> None:
        if "day" not in rollup:
            raise ValueError("daily_rollup requires a day")
        payload = dict(rollup)
        day = str(payload.pop("day"))
        canonical = _canonical_json(payload)
        connection.execute(
            """
            insert into shadow_daily_rollups(
                arm_id, trading_day, payload, payload_hash, updated_at_ms
            ) values (?, ?, ?, ?, ?)
            on conflict(arm_id, trading_day) do update set
                payload = excluded.payload,
                payload_hash = excluded.payload_hash,
                updated_at_ms = excluded.updated_at_ms
            """,
            (arm_id, day, canonical, _payload_hash(payload), updated_at_ms),
        )

    @staticmethod
    def _save_runtime_state(
        connection: sqlite3.Connection,
        arm_id: str,
        state: Mapping[str, Any],
        *,
        state_version: int,
        updated_at_ms: int,
    ) -> None:
        canonical = _canonical_json(state)
        connection.execute(
            """
            insert into shadow_runtime_state(
                arm_id, state_version, state_payload, payload_hash, updated_at_ms
            ) values (?, ?, ?, ?, ?)
            on conflict(arm_id) do update set
                state_version = excluded.state_version,
                state_payload = excluded.state_payload,
                payload_hash = excluded.payload_hash,
                updated_at_ms = excluded.updated_at_ms
            """,
            (arm_id, state_version, canonical, _payload_hash(state), updated_at_ms),
        )

    def load_recovery_state(self, arm_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            cursor = connection.execute(
                "select * from shadow_event_cursors where arm_id = ?", (arm_id,)
            ).fetchone()
            runtime = connection.execute(
                "select * from shadow_runtime_state where arm_id = ?", (arm_id,)
            ).fetchone()
            orders = connection.execute(
                "select * from shadow_orders where arm_id = ? order by order_id",
                (arm_id,),
            ).fetchall()
            observations = connection.execute(
                """
                select * from shadow_observations
                where arm_id = ? order by opened_at, observation_key
                """,
                (arm_id,),
            ).fetchall()
            gaps = connection.execute(
                "select * from shadow_event_gaps where arm_id = ? order by closed_at_ms",
                (arm_id,),
            ).fetchall()
            rollups = connection.execute(
                "select * from shadow_daily_rollups where arm_id = ? order by trading_day",
                (arm_id,),
            ).fetchall()
        return {
            "cursor": _row_dict(cursor) if cursor is not None else None,
            "runtime_state": (
                _decode_json(runtime["state_payload"]) if runtime is not None else None
            ),
            "runtime_state_version": (
                int(runtime["state_version"]) if runtime is not None else None
            ),
            "orders": [self._decode_order(row) for row in orders],
            "observations": [self._decode_observation(row) for row in observations],
            "gaps": [_row_dict(row) for row in gaps],
            "daily_rollups": [self._decode_rollup(row) for row in rollups],
        }

    @staticmethod
    def _decode_order(row: sqlite3.Row) -> dict[str, Any]:
        item = _row_dict(row)
        item["detail"] = _decode_json(item.pop("detail_payload"))
        return item

    @staticmethod
    def _decode_observation(row: sqlite3.Row) -> dict[str, Any]:
        item = _row_dict(row)
        item["detail"] = _decode_json(item.pop("detail_payload"))
        return item

    @staticmethod
    def _decode_rollup(row: sqlite3.Row) -> dict[str, Any]:
        item = _decode_json(row["payload"])
        item["day"] = row["trading_day"]
        return item

    def list_orders(self, arm_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "select * from shadow_orders where arm_id = ? order by order_id",
                (arm_id,),
            ).fetchall()
        return [self._decode_order(row) for row in rows]

    def page_orders(
        self,
        arm_id: str,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        normalized_page = max(1, int(page))
        normalized_size = min(100, max(1, int(page_size)))
        with self._connect() as connection:
            total = int(
                connection.execute(
                    "select count(*) from shadow_orders where arm_id = ?",
                    (str(arm_id),),
                ).fetchone()[0]
            )
            total_pages = max(1, (total + normalized_size - 1) // normalized_size)
            normalized_page = min(normalized_page, total_pages)
            rows = connection.execute(
                """
                select * from shadow_orders
                where arm_id = ?
                order by order_id desc
                limit ? offset ?
                """,
                (
                    str(arm_id),
                    normalized_size,
                    (normalized_page - 1) * normalized_size,
                ),
            ).fetchall()
        return {
            "arm_id": str(arm_id),
            "page": normalized_page,
            "page_size": normalized_size,
            "total": total,
            "total_pages": total_pages,
            "orders": [self._decode_order(row) for row in rows],
        }

    def list_observations(self, arm_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select * from shadow_observations
                where arm_id = ? order by opened_at, observation_key
                """,
                (arm_id,),
            ).fetchall()
        return [self._decode_observation(row) for row in rows]

    def list_decisions(self, arm_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "select * from shadow_decisions where arm_id = ? "
                "order by closed_at_ms, decision_index",
                (arm_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = _row_dict(row)
            item["detail"] = _decode_json(item.pop("detail_payload"))
            result.append(item)
        return result

    def list_decision_rollups(self, arm_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "select * from shadow_decision_rollups where arm_id = ? "
                "order by trading_day, decision, direction, profile_key, "
                "first_decisive_block",
                (arm_id,),
            ).fetchall()
        return [_row_dict(row) for row in rows]

    def save_evaluation_bundle(
        self,
        *,
        evaluation_id: str,
        experiment_id: str,
        arm_id: str,
        evaluated_at_ms: int,
        decision: str,
        metrics: Mapping[str, Any],
        lifecycle_event: Mapping[str, Any] | None = None,
        arm_status: str | None = None,
    ) -> None:
        canonical = _canonical_json(metrics)
        evaluation_hash = _payload_hash(
            {
                "experiment_id": experiment_id,
                "arm_id": arm_id,
                "evaluated_at_ms": int(evaluated_at_ms),
                "decision": decision,
                "metrics": metrics,
                "lifecycle_event": lifecycle_event,
                "arm_status": arm_status,
            }
        )
        with self._transaction() as connection:
            arm = connection.execute(
                "select experiment_id from shadow_arms where arm_id = ?",
                (arm_id,),
            ).fetchone()
            if arm is None or arm["experiment_id"] != experiment_id:
                raise ShadowStorageConflictError(
                    f"arm {arm_id!r} does not belong to experiment {experiment_id!r}"
                )
            existing = connection.execute(
                "select payload_hash from shadow_evaluations where evaluation_id = ?",
                (evaluation_id,),
            ).fetchone()
            if existing is not None:
                if existing["payload_hash"] != evaluation_hash:
                    raise ShadowStorageConflictError(
                        f"evaluation {evaluation_id!r} changed"
                    )
                return
            connection.execute(
                """
                insert into shadow_evaluations(
                    evaluation_id, experiment_id, arm_id, evaluated_at_ms,
                    decision, metrics_payload, payload_hash
                ) values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation_id,
                    experiment_id,
                    arm_id,
                    int(evaluated_at_ms),
                    decision,
                    canonical,
                    evaluation_hash,
                ),
            )
            self._step("evaluation")
            if lifecycle_event is not None:
                evidence = lifecycle_event.get("evidence", {})
                lifecycle_hash = _payload_hash(
                    {**dict(lifecycle_event), "experiment_id": experiment_id}
                )
                connection.execute(
                    """
                    insert into shadow_lifecycle_history(
                        history_id, experiment_id, event_type, from_arm_id,
                        to_arm_id, effective_at_ms, reason, evidence_payload,
                        payload_hash
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(lifecycle_event["history_id"]),
                        experiment_id,
                        str(lifecycle_event["event_type"]),
                        lifecycle_event.get("from_arm_id"),
                        lifecycle_event.get("to_arm_id"),
                        int(lifecycle_event["effective_at_ms"]),
                        str(lifecycle_event["reason"]),
                        _canonical_json(evidence),
                        lifecycle_hash,
                    ),
                )
                to_arm_id = lifecycle_event.get("to_arm_id")
                if to_arm_id is not None:
                    target = connection.execute(
                        """
                        select 1 from shadow_arms
                        where arm_id = ? and experiment_id = ?
                        """,
                        (to_arm_id, experiment_id),
                    ).fetchone()
                    if target is None:
                        raise ShadowStorageConflictError(
                            f"lifecycle target arm {to_arm_id!r} is invalid"
                        )
                    connection.execute(
                        """
                        update shadow_experiments set champion_arm_id = ?
                        where experiment_id = ?
                        """,
                        (to_arm_id, experiment_id),
                    )
            self._step("lifecycle")
            if arm_status is not None:
                changed = connection.execute(
                    "update shadow_arms set status = ? where arm_id = ?",
                    (arm_status, arm_id),
                )
                if changed.rowcount != 1:
                    raise ShadowStorageConflictError(f"unknown arm {arm_id!r}")
            self._step("arm_status")

    def list_evaluations(self, arm_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "select * from shadow_evaluations where arm_id = ? order by evaluated_at_ms",
                (arm_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = _row_dict(row)
            item["metrics"] = _decode_json(item.pop("metrics_payload"))
            result.append(item)
        return result

    def list_lifecycle_history(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "select * from shadow_lifecycle_history order by effective_at_ms, history_id"
            ).fetchall()
        result = []
        for row in rows:
            item = _row_dict(row)
            item["evidence"] = _decode_json(item.pop("evidence_payload"))
            result.append(item)
        return result

    def compact_terminal_details(
        self,
        *,
        before_ms: int,
        limit: int = 1_000,
        compacted_at_ms: int | None = None,
    ) -> dict[str, int]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        timestamp = int(before_ms if compacted_at_ms is None else compacted_at_ms)
        with self._transaction(allow_hard_limit_compaction=True) as connection:
            decision_keys = connection.execute(
                """
                select arm_id, event_id, decision_index, closed_at_ms,
                       decision, direction, profile_key, detail_payload
                from shadow_decisions
                where upper(decision) in ('WAIT', 'BELOW_THRESHOLD')
                  and terminal_at_ms is not null and terminal_at_ms < ?
                  and detail_payload is not null
                order by terminal_at_ms
                limit ?
                """,
                (int(before_ms), int(limit)),
            ).fetchall()
            for row in decision_keys:
                detail = _decode_json(row["detail_payload"])
                if not isinstance(detail, Mapping):
                    detail = {}
                score = _finite_float(detail.get("score"))
                threshold = _finite_float(
                    detail.get("calculated_threshold", detail.get("threshold"))
                )
                first_decisive_block = str(
                    detail.get("first_decisive_block") or "UNSPECIFIED"
                )
                connection.execute(
                    """
                    insert into shadow_decision_rollups(
                        arm_id, trading_day, decision, direction, profile_key,
                        first_decisive_block, occurrences, minimum_score,
                        maximum_score, minimum_threshold, maximum_threshold,
                        updated_at_ms
                    ) values (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                    on conflict(
                        arm_id, trading_day, decision, direction, profile_key,
                        first_decisive_block
                    ) do update set
                        occurrences = shadow_decision_rollups.occurrences + 1,
                        minimum_score = min(
                            shadow_decision_rollups.minimum_score,
                            excluded.minimum_score
                        ),
                        maximum_score = max(
                            shadow_decision_rollups.maximum_score,
                            excluded.maximum_score
                        ),
                        minimum_threshold = min(
                            shadow_decision_rollups.minimum_threshold,
                            excluded.minimum_threshold
                        ),
                        maximum_threshold = max(
                            shadow_decision_rollups.maximum_threshold,
                            excluded.maximum_threshold
                        ),
                        updated_at_ms = max(
                            shadow_decision_rollups.updated_at_ms,
                            excluded.updated_at_ms
                        )
                    """,
                    (
                        row["arm_id"],
                        _trading_day(row["closed_at_ms"]),
                        str(row["decision"]),
                        str(row["direction"] or "NONE"),
                        str(row["profile_key"] or "__GLOBAL__"),
                        first_decisive_block,
                        score,
                        score,
                        threshold,
                        threshold,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    update shadow_decisions
                    set detail_payload = null, compacted_at_ms = ?
                    where arm_id = ? and event_id = ? and decision_index = ?
                      and terminal_at_ms is not null
                    """,
                    (timestamp, row["arm_id"], row["event_id"], row["decision_index"]),
                )

            remaining = max(0, int(limit) - len(decision_keys))
            observation_keys = connection.execute(
                """
                select arm_id, observation_key
                from shadow_observations
                where status = 'SETTLED' and settled_at < ?
                  and detail_payload is not null
                order by settled_at
                limit ?
                """,
                (int(before_ms), remaining),
            ).fetchall()
            for row in observation_keys:
                connection.execute(
                    """
                    update shadow_observations
                    set detail_payload = null, compacted_at_ms = ?
                    where arm_id = ? and observation_key = ? and status = 'SETTLED'
                    """,
                    (timestamp, row["arm_id"], row["observation_key"]),
                )
        try:
            with self._connect() as connection:
                if int(connection.execute("pragma auto_vacuum").fetchone()[0]) == 2:
                    connection.execute("pragma incremental_vacuum(256)")
                connection.execute("pragma wal_checkpoint(passive)")
        except sqlite3.OperationalError:
            pass
        return {
            "decisions": len(decision_keys),
            "observations": len(observation_keys),
            "orders": 0,
        }
