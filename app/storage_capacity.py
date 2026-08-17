import sqlite3
from dataclasses import dataclass
from enum import Enum
from typing import NoReturn, Protocol


MAX_DATABASE_BYTES = 3 * 1024**3
WARNING_BYTES = int(2.5 * 1024**3)
COMPACT_ONLY_BYTES = int(2.75 * 1024**3)
CORE_RESERVE_BYTES = 256 * 1024**2


class StorageWriteClass(str, Enum):
    ORDINARY_AUDIT = "ORDINARY_AUDIT"
    CORE = "CORE"


@dataclass(frozen=True)
class StorageCapacity:
    status: str
    database_bytes: int
    max_database_bytes: int
    core_reserve_bytes: int
    ordinary_audit_allowed: bool
    core_write_allowed: bool


class StorageCapacityError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        write_class: StorageWriteClass,
        capacity: StorageCapacity | None,
    ) -> None:
        super().__init__(message)
        self.write_class = write_class
        self.capacity = capacity


class OrdinaryAuditCapacityError(StorageCapacityError):
    pass


class CoreStorageCapacityError(StorageCapacityError):
    pass


class StorageCapacityConfigurationError(RuntimeError):
    pass


class _PragmaConnection(Protocol):
    def execute(self, statement: str): ...


def classify_capacity(database_bytes: int) -> str:
    if type(database_bytes) is not int:
        raise TypeError("database_bytes must be an integer")
    if database_bytes < 0:
        raise ValueError("database_bytes must not be negative")
    if database_bytes >= MAX_DATABASE_BYTES:
        return "HARD_LIMIT"
    if database_bytes >= COMPACT_ONLY_BYTES:
        return "COMPACT_ONLY"
    if database_bytes >= WARNING_BYTES:
        return "WARNING"
    return "NORMAL"


def capacity_for_bytes(database_bytes: int) -> StorageCapacity:
    status = classify_capacity(database_bytes)
    remaining_bytes = max(0, MAX_DATABASE_BYTES - database_bytes)
    return StorageCapacity(
        status=status,
        database_bytes=database_bytes,
        max_database_bytes=MAX_DATABASE_BYTES,
        core_reserve_bytes=CORE_RESERVE_BYTES,
        ordinary_audit_allowed=(
            database_bytes < COMPACT_ONLY_BYTES
            and remaining_bytes >= CORE_RESERVE_BYTES
        ),
        core_write_allowed=database_bytes < MAX_DATABASE_BYTES,
    )


def configure_max_page_count(connection: _PragmaConnection) -> int:
    page_size_row = connection.execute("pragma page_size").fetchone()
    if not page_size_row or type(page_size_row[0]) is not int or page_size_row[0] <= 0:
        raise StorageCapacityConfigurationError(
            "SQLite returned an invalid page_size"
        )
    expected = MAX_DATABASE_BYTES // page_size_row[0]
    effective_row = connection.execute(
        f"pragma max_page_count = {expected}"
    ).fetchone()
    if not effective_row or effective_row[0] != expected:
        effective = effective_row[0] if effective_row else None
        raise StorageCapacityConfigurationError(
            f"SQLite max_page_count is {effective}, expected {expected}"
        )
    verified_row = connection.execute("pragma max_page_count").fetchone()
    if not verified_row or verified_row[0] != expected:
        verified = verified_row[0] if verified_row else None
        raise StorageCapacityConfigurationError(
            f"SQLite max_page_count verification is {verified}, expected {expected}"
        )
    return expected


def capacity_from_connection(connection: _PragmaConnection) -> StorageCapacity:
    page_count_row = connection.execute("pragma page_count").fetchone()
    page_size_row = connection.execute("pragma page_size").fetchone()
    if (
        not page_count_row
        or type(page_count_row[0]) is not int
        or page_count_row[0] < 0
        or not page_size_row
        or type(page_size_row[0]) is not int
        or page_size_row[0] <= 0
    ):
        raise StorageCapacityConfigurationError(
            "SQLite returned invalid page_count or page_size"
        )
    return capacity_for_bytes(page_count_row[0] * page_size_row[0])


def ensure_write_allowed(
    capacity: StorageCapacity,
    write_class: StorageWriteClass,
) -> None:
    normalized_class = StorageWriteClass(write_class)
    if normalized_class is StorageWriteClass.ORDINARY_AUDIT:
        if capacity.ordinary_audit_allowed:
            return
        raise OrdinaryAuditCapacityError(
            f"ordinary audit write is disabled at {capacity.status}",
            write_class=normalized_class,
            capacity=capacity,
        )
    if capacity.core_write_allowed:
        return
    raise CoreStorageCapacityError(
        f"core write is disabled at {capacity.status}",
        write_class=normalized_class,
        capacity=capacity,
    )


def _is_sqlite_full(error: BaseException) -> bool:
    if not isinstance(error, sqlite3.Error):
        return False
    error_code = getattr(error, "sqlite_errorcode", None)
    if error_code is not None:
        return (
            type(error_code) is int
            and error_code & 0xFF == sqlite3.SQLITE_FULL
        )
    message = str(error).strip().lower()
    return any(
        phrase in message
        for phrase in (
            "database or disk is full",
            "database is full",
            "disk is full",
        )
    )


def raise_for_sqlite_write_error(
    error: BaseException,
    write_class: StorageWriteClass,
) -> NoReturn:
    normalized_class = StorageWriteClass(write_class)
    if not _is_sqlite_full(error):
        raise error
    exception_type = (
        OrdinaryAuditCapacityError
        if normalized_class is StorageWriteClass.ORDINARY_AUDIT
        else CoreStorageCapacityError
    )
    raise exception_type(
        "SQLite cannot allocate another database page",
        write_class=normalized_class,
        capacity=None,
    ) from error
