import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from arch_runtime.persistence.sqlite import (
    SQLiteConfig,
    SQLiteEventStore,
    SQLiteIdempotencyStore,
    SQLiteProjectRepository,
    SQLiteSnapshotStore,
    SQLiteUnitOfWork,
    migrate_schema,
    open_sqlite_connection,
)
from arch_runtime.persistence.sqlite.codec import KernelContractCodec
from arch_runtime.ports import (
    EventStore,
    IdempotencyStore,
    ProjectRepository,
    SnapshotStore,
    UnitOfWork,
)
from tests.fakes.adapters import FrozenClock


@pytest.fixture
def sqlite_config(tmp_path: Path) -> SQLiteConfig:
    return SQLiteConfig(tmp_path / "runtime.db", busy_timeout_ms=2_500)


@pytest.fixture
def sqlite_adapter_connection(
    sqlite_config: SQLiteConfig, frozen_clock: FrozenClock
) -> Iterator[sqlite3.Connection]:
    connection = open_sqlite_connection(sqlite_config)
    migrate_schema(connection, clock=frozen_clock)
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


@pytest.fixture
def project_repository(sqlite_adapter_connection: sqlite3.Connection) -> ProjectRepository:
    return SQLiteProjectRepository(sqlite_adapter_connection, KernelContractCodec())


@pytest.fixture
def event_store(sqlite_adapter_connection: sqlite3.Connection) -> EventStore:
    return SQLiteEventStore(sqlite_adapter_connection, KernelContractCodec())


@pytest.fixture
def snapshot_store(sqlite_adapter_connection: sqlite3.Connection) -> SnapshotStore:
    return SQLiteSnapshotStore(sqlite_adapter_connection, KernelContractCodec())


@pytest.fixture
def idempotency_store(
    sqlite_adapter_connection: sqlite3.Connection,
    frozen_clock: FrozenClock,
) -> IdempotencyStore:
    return SQLiteIdempotencyStore(sqlite_adapter_connection, KernelContractCodec(), frozen_clock)


@pytest.fixture
def unit_of_work(sqlite_config: SQLiteConfig, frozen_clock: FrozenClock) -> UnitOfWork:
    connection = open_sqlite_connection(sqlite_config)
    try:
        migrate_schema(connection, clock=frozen_clock)
    finally:
        connection.close()
    return SQLiteUnitOfWork(sqlite_config, clock=frozen_clock)
