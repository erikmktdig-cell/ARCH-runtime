from __future__ import annotations

import pytest
from arch_kernel.contracts import ProjectState

from arch_runtime.application import (
    ApplyTransitionService,
    CreateProjectCommand,
    CreateProjectService,
)
from arch_runtime.errors import PersistenceError
from arch_runtime.persistence.sqlite import (
    SQLiteConfig,
    SQLiteUnitOfWork,
    migrate_schema,
    open_sqlite_connection,
)
from arch_runtime.ports import StoredProject
from tests.application.conftest import EventIds, ProjectIds
from tests.application.test_apply_transition import (
    TransitionEventIds,
    _command,
    _definition,
)
from tests.fakes.adapters import FrozenClock

pytestmark = pytest.mark.sqlite


def _initialize(config: SQLiteConfig, clock: FrozenClock) -> None:
    connection = open_sqlite_connection(config)
    try:
        migrate_schema(connection, clock=clock)
    finally:
        connection.close()


def _seed(
    config: SQLiteConfig,
    clock: FrozenClock,
    command: CreateProjectCommand,
    project_ids: ProjectIds,
) -> tuple[ProjectState, StoredProject]:
    def factory() -> SQLiteUnitOfWork:
        return SQLiteUnitOfWork(config, clock=clock)

    created = CreateProjectService(
        unit_of_work_factory=factory,
        clock=clock,
        project_ids=project_ids,
        event_ids=EventIds(),
    ).create_project(command)
    with factory() as uow:
        stored = uow.projects.get(created.project_id)
    assert stored is not None
    return ProjectState.model_validate_json(stored.state_json, strict=True), stored


def _service(config: SQLiteConfig, clock: FrozenClock) -> ApplyTransitionService:
    from arch_kernel.kernel import InvariantRegistry, TransitionRegistry

    return ApplyTransitionService(
        unit_of_work_factory=lambda: SQLiteUnitOfWork(config, clock=clock),
        clock=clock,
        event_ids=TransitionEventIds(),
        transition_registry=TransitionRegistry((_definition(),)),
        invariant_registry=InvariantRegistry(()),
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


def test_sqlite_transition_commits_and_replays_once(
    sqlite_config: SQLiteConfig,
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
) -> None:
    _initialize(sqlite_config, frozen_clock)
    state, stored = _seed(sqlite_config, frozen_clock, create_command, project_ids)
    service = _service(sqlite_config, frozen_clock)

    first = service.apply_transition(_command(state, stored))
    second = service.apply_transition(_command(state, stored))

    assert first == second
    assert _counts(sqlite_config) == (1, 2, 2)


@pytest.mark.parametrize(
    "trigger_sql",
    [
        "CREATE TRIGGER fail_transition_save BEFORE UPDATE ON project_aggregates "
        "BEGIN SELECT RAISE(ABORT, 'injected save fault'); END",
        "CREATE TRIGGER fail_transition_event BEFORE INSERT ON project_events "
        "BEGIN SELECT RAISE(ABORT, 'injected event fault'); END",
        "CREATE TRIGGER fail_transition_completion BEFORE UPDATE ON idempotency_records "
        "WHEN OLD.operation_name='project.transition' "
        "BEGIN SELECT RAISE(ABORT, 'injected completion fault'); END",
    ],
    ids=("aggregate-save", "event-append", "idempotency-complete"),
)
def test_sqlite_faults_rollback_aggregate_event_and_idempotency(
    trigger_sql: str,
    sqlite_config: SQLiteConfig,
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
) -> None:
    _initialize(sqlite_config, frozen_clock)
    state, stored = _seed(sqlite_config, frozen_clock, create_command, project_ids)
    before = _counts(sqlite_config)
    connection = open_sqlite_connection(sqlite_config)
    try:
        connection.execute(trigger_sql)
    finally:
        connection.close()

    with pytest.raises(PersistenceError):
        _service(sqlite_config, frozen_clock).apply_transition(_command(state, stored))

    assert _counts(sqlite_config) == before
