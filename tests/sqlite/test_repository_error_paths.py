import sqlite3

import pytest
from arch_kernel.contracts import ProjectState

from arch_runtime.errors import (
    IdempotencyConflictError,
    PersistenceError,
    ProjectNotFoundError,
    RuntimeConfigurationError,
)
from arch_runtime.persistence.sqlite import (
    SQLiteConfig,
    SQLiteUnitOfWork,
    migrate_schema,
    open_sqlite_connection,
)
from arch_runtime.persistence.sqlite.codec import KernelContractCodec
from arch_runtime.persistence.sqlite.repositories import (
    SQLiteIdempotencyStore,
    SQLiteProjectRepository,
    SQLiteSnapshotStore,
)
from arch_runtime.ports import IdempotencyRecord, IdempotencyStatus
from tests.fakes.adapters import FrozenClock

pytestmark = pytest.mark.sqlite


def test_missing_records_and_duplicate_idempotency_are_structured(
    sqlite_connection: sqlite3.Connection,
    frozen_clock: FrozenClock,
    project_state: ProjectState,
) -> None:
    migrate_schema(sqlite_connection, clock=frozen_clock)
    sqlite_connection.execute("BEGIN IMMEDIATE")
    codec = KernelContractCodec()
    projects = SQLiteProjectRepository(sqlite_connection, codec)
    snapshots = SQLiteSnapshotStore(sqlite_connection, codec)
    idempotency = SQLiteIdempotencyStore(sqlite_connection, codec, frozen_clock)

    assert snapshots.get_latest(project_state.metadata.project_id) is None
    assert idempotency.get("project.create", "missing") is None
    with pytest.raises(ProjectNotFoundError):
        projects.save(
            project_state,
            expected_record_version=1,
            expected_record_fingerprint="sha256:" + "a" * 64,
        )

    reservation = IdempotencyRecord(
        operation_name="project.create",
        idempotency_key="duplicate",
        project_id=project_state.metadata.project_id,
        request_fingerprint="sha256:" + "b" * 64,
        status=IdempotencyStatus.IN_PROGRESS,
        created_at=project_state.created_at,
    )
    idempotency.reserve(reservation)
    with pytest.raises(IdempotencyConflictError):
        idempotency.reserve(reservation)
    with pytest.raises(PersistenceError):
        idempotency.complete("project.create", "missing", {"accepted": False})


def test_uow_explicit_rollback_and_nested_entry_are_guarded(
    sqlite_config: SQLiteConfig,
    frozen_clock: FrozenClock,
    project_state: ProjectState,
) -> None:
    connection = open_sqlite_connection(sqlite_config)
    try:
        migrate_schema(connection, clock=frozen_clock)
    finally:
        connection.close()

    uow = SQLiteUnitOfWork(sqlite_config, clock=frozen_clock)
    with uow:
        uow.projects.add(project_state)
        with pytest.raises(RuntimeConfigurationError):
            uow.__enter__()
        uow.rollback()

    with uow:
        assert uow.projects.get(project_state.metadata.project_id) is None
