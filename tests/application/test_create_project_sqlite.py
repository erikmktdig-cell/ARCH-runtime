from __future__ import annotations

from collections.abc import Callable

import pytest
from arch_kernel.contracts import ValidationPipelineResult

from arch_runtime.application import CreateProjectCommand, CreateProjectService
from arch_runtime.errors import PersistenceError
from arch_runtime.persistence.sqlite import (
    SQLiteConfig,
    SQLiteUnitOfWork,
    migrate_schema,
    open_sqlite_connection,
)
from tests.application.conftest import EventIds, ProjectIds
from tests.application.test_create_project import RejectingValidationRunner
from tests.fakes.adapters import FrozenClock

pytestmark = pytest.mark.sqlite


def _initialize(config: SQLiteConfig, clock: FrozenClock) -> None:
    connection = open_sqlite_connection(config)
    try:
        migrate_schema(connection, clock=clock)
    finally:
        connection.close()


def _service(
    config: SQLiteConfig,
    clock: FrozenClock,
    project_ids: ProjectIds,
    event_ids: EventIds,
    validation_runner: Callable[..., ValidationPipelineResult] | None = None,
) -> CreateProjectService:
    def factory() -> SQLiteUnitOfWork:
        return SQLiteUnitOfWork(config, clock=clock)

    if validation_runner is None:
        return CreateProjectService(
            unit_of_work_factory=factory,
            clock=clock,
            project_ids=project_ids,
            event_ids=event_ids,
        )
    return CreateProjectService(
        unit_of_work_factory=factory,
        clock=clock,
        project_ids=project_ids,
        event_ids=event_ids,
        validation_runner=validation_runner,
    )


def _counts(config: SQLiteConfig) -> tuple[int, int, int]:
    connection = open_sqlite_connection(config)
    try:
        return tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("project_aggregates", "project_events", "idempotency_records")
        )
    finally:
        connection.close()


def test_sqlite_create_and_retry_commit_one_logical_result(
    sqlite_config: SQLiteConfig,
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
    event_ids: EventIds,
) -> None:
    _initialize(sqlite_config, frozen_clock)
    service = _service(sqlite_config, frozen_clock, project_ids, event_ids)

    first = service.create_project(create_command)
    second = service.create_project(create_command)

    assert first == second
    assert _counts(sqlite_config) == (1, 1, 1)


def test_sqlite_k08_rejection_commits_only_idempotency(
    sqlite_config: SQLiteConfig,
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
    event_ids: EventIds,
) -> None:
    _initialize(sqlite_config, frozen_clock)
    runner = RejectingValidationRunner()
    service = _service(sqlite_config, frozen_clock, project_ids, event_ids, runner)

    result = service.create_project(create_command)

    assert not result.success
    assert _counts(sqlite_config) == (0, 0, 1)


def test_sqlite_event_failure_rolls_back_full_create_flow(
    sqlite_config: SQLiteConfig,
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
    event_ids: EventIds,
) -> None:
    _initialize(sqlite_config, frozen_clock)
    connection = open_sqlite_connection(sqlite_config)
    try:
        connection.execute(
            "CREATE TRIGGER fail_create_event BEFORE INSERT ON project_events "
            "BEGIN SELECT RAISE(ABORT, 'injected create fault'); END"
        )
    finally:
        connection.close()
    service = _service(sqlite_config, frozen_clock, project_ids, event_ids)

    with pytest.raises(PersistenceError):
        service.create_project(create_command)

    assert _counts(sqlite_config) == (0, 0, 0)


def test_sqlite_idempotency_completion_failure_rolls_back_full_create_flow(
    sqlite_config: SQLiteConfig,
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
    event_ids: EventIds,
) -> None:
    _initialize(sqlite_config, frozen_clock)
    connection = open_sqlite_connection(sqlite_config)
    try:
        connection.execute(
            "CREATE TRIGGER fail_create_completion BEFORE UPDATE ON idempotency_records "
            "BEGIN SELECT RAISE(ABORT, 'injected completion fault'); END"
        )
    finally:
        connection.close()
    service = _service(sqlite_config, frozen_clock, project_ids, event_ids)

    with pytest.raises(PersistenceError):
        service.create_project(create_command)

    assert _counts(sqlite_config) == (0, 0, 0)
