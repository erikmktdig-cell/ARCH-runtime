from __future__ import annotations

from collections.abc import Callable

import pytest

from arch_runtime.application import CreateProjectCommand, CreateProjectService
from arch_runtime.errors import ReplayIntegrityError
from arch_runtime.persistence.sqlite import (
    SQLiteConfig,
    SQLiteUnitOfWork,
    migrate_schema,
    open_sqlite_connection,
)
from arch_runtime.replay import ReplayService
from tests.application.conftest import EventIds, ProjectIds
from tests.fakes.adapters import FrozenClock

pytestmark = pytest.mark.sqlite
_TABLES = (
    "project_aggregates",
    "project_events",
    "project_snapshots",
    "idempotency_records",
)


def _initialize(config: SQLiteConfig, clock: FrozenClock) -> None:
    connection = open_sqlite_connection(config)
    try:
        migrate_schema(connection, clock=clock)
    finally:
        connection.close()


def _factory(config: SQLiteConfig, clock: FrozenClock) -> Callable[[], SQLiteUnitOfWork]:
    return lambda: SQLiteUnitOfWork(config, clock=clock)


def _counts(config: SQLiteConfig) -> tuple[int, ...]:
    connection = open_sqlite_connection(config)
    try:
        return tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in _TABLES
        )
    finally:
        connection.close()


def test_sqlite_replay_reads_verified_evidence_without_writes(
    sqlite_config: SQLiteConfig,
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
) -> None:
    _initialize(sqlite_config, frozen_clock)
    factory = _factory(sqlite_config, frozen_clock)
    created = CreateProjectService(
        unit_of_work_factory=factory,
        clock=frozen_clock,
        project_ids=project_ids,
        event_ids=EventIds(),
    ).create_project(create_command)
    before = _counts(sqlite_config)

    result = ReplayService(unit_of_work_factory=factory).replay_project(created.project_id)

    assert result.record_fingerprint == created.record_fingerprint
    assert _counts(sqlite_config) == before


def test_sqlite_broken_event_chain_is_translated_to_replay_integrity_error(
    sqlite_config: SQLiteConfig,
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
) -> None:
    _initialize(sqlite_config, frozen_clock)
    factory = _factory(sqlite_config, frozen_clock)
    created = CreateProjectService(
        unit_of_work_factory=factory,
        clock=frozen_clock,
        project_ids=project_ids,
        event_ids=EventIds(),
    ).create_project(create_command)
    connection = open_sqlite_connection(sqlite_config)
    try:
        connection.execute(
            "UPDATE project_events SET previous_event_fingerprint=? WHERE project_id=?",
            ("sha256:" + "f" * 64, str(created.project_id)),
        )
    finally:
        connection.close()

    with pytest.raises(ReplayIntegrityError):
        ReplayService(unit_of_work_factory=factory).replay_project(created.project_id)
