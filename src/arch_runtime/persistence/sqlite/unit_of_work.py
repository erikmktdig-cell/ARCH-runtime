"""Explicit SQLite transaction boundary for all R04 adapters."""

from __future__ import annotations

import sqlite3
from types import TracebackType
from typing import Self

from arch_runtime.errors import RuntimeConfigurationError
from arch_runtime.persistence.sqlite.codec import KernelContractCodec
from arch_runtime.persistence.sqlite.config import SQLiteConfig
from arch_runtime.persistence.sqlite.connection import (
    begin_transaction,
    commit_transaction,
    open_sqlite_connection,
    rollback_transaction,
)
from arch_runtime.persistence.sqlite.repositories import (
    SQLiteEventStore,
    SQLiteIdempotencyStore,
    SQLiteProjectRepository,
    SQLiteSnapshotStore,
)
from arch_runtime.persistence.sqlite.schema import require_current_schema
from arch_runtime.ports import Clock, EventStore, IdempotencyStore, ProjectRepository, SnapshotStore


class SQLiteUnitOfWork:
    def __init__(self, config: SQLiteConfig, *, clock: Clock) -> None:
        self._config = config
        self._clock = clock
        self._connection: sqlite3.Connection | None = None
        self._committed = False
        self.projects: ProjectRepository
        self.events: EventStore
        self.snapshots: SnapshotStore
        self.idempotency: IdempotencyStore

    def __enter__(self) -> Self:
        if self._connection is not None:
            raise RuntimeConfigurationError(
                "SQLite Unit of Work is already active",
                operation="sqlite.uow.enter",
                remediation="finish the active Unit of Work before reusing it",
            )
        connection = open_sqlite_connection(self._config)
        try:
            require_current_schema(connection)
            begin_transaction(connection)
        except BaseException:
            connection.close()
            raise
        codec = KernelContractCodec()
        self._connection = connection
        self._committed = False
        self.projects = SQLiteProjectRepository(connection, codec)
        self.events = SQLiteEventStore(connection, codec)
        self.snapshots = SQLiteSnapshotStore(connection, codec)
        self.idempotency = SQLiteIdempotencyStore(connection, codec, self._clock)
        return self

    def commit(self) -> None:
        connection = self._require_active()
        commit_transaction(connection)
        self._committed = True

    def rollback(self) -> None:
        connection = self._require_active()
        if connection.in_transaction:
            rollback_transaction(connection)
        self._committed = False

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        connection = self._require_active()
        try:
            if connection.in_transaction and (exc_type is not None or not self._committed):
                rollback_transaction(connection)
        finally:
            connection.close()
            self._connection = None

    def _require_active(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeConfigurationError(
                "SQLite Unit of Work is not active",
                operation="sqlite.uow",
                remediation="use the Unit of Work as a context manager",
            )
        return self._connection
