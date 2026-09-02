from __future__ import annotations

import json

import pytest
from arch_kernel.contracts import ProjectState
from arch_kernel.kernel import InvariantRegistry, WorkflowDefinitionRegistry

from arch_runtime.application import (
    CreateProjectCommand,
    InitializeWorkflowCommand,
    InitializeWorkflowService,
)
from arch_runtime.errors import PersistenceError, ReplayIntegrityError
from arch_runtime.persistence.sqlite import (
    SQLiteConfig,
    SQLiteUnitOfWork,
    open_sqlite_connection,
)
from arch_runtime.ports import StoredProject
from arch_runtime.replay import ReplayService
from tests.application.conftest import ProjectIds
from tests.application.test_apply_transition_sqlite import _counts, _initialize, _seed
from tests.application.test_workflows import (
    WorkflowEventIds,
    workflow_definition,
)
from tests.fakes.adapters import FrozenClock

pytestmark = pytest.mark.sqlite


def _service(config: SQLiteConfig, clock: FrozenClock) -> InitializeWorkflowService:
    definition = workflow_definition()
    return InitializeWorkflowService(
        unit_of_work_factory=lambda: SQLiteUnitOfWork(config, clock=clock),
        clock=clock,
        event_ids=WorkflowEventIds(),
        workflow_definitions=WorkflowDefinitionRegistry((definition,)),
        invariant_registry=InvariantRegistry(()),
    )


def _command_for(
    state: ProjectState, stored: StoredProject, *, key: str
) -> InitializeWorkflowCommand:
    definition = workflow_definition()
    return InitializeWorkflowCommand(
        idempotency_key=key,
        project_id=state.metadata.project_id,
        workflow_id=definition.workflow_id,
        workflow_namespace=definition.namespace,
        definition_version=definition.definition_version,
        definition_fingerprint=definition.definition_fingerprint(),
        expected_record_version=stored.record_version,
        expected_record_fingerprint=stored.record_fingerprint,
        expected_content_fingerprint=stored.content_fingerprint,
        actor_id="user:workflow",
    )


def test_sqlite_initialization_commits_once_and_replays(
    sqlite_config: SQLiteConfig,
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
) -> None:
    _initialize(sqlite_config, frozen_clock)
    state, stored = _seed(sqlite_config, frozen_clock, create_command, project_ids)
    definition = workflow_definition()
    command = _command_for(state, stored, key="workflow:sqlite:init")
    service = _service(sqlite_config, frozen_clock)

    first = service.initialize_workflow(command)
    repeated = service.initialize_workflow(command)

    assert repeated == first
    assert _counts(sqlite_config) == (1, 2, 2)
    replay = ReplayService(
        unit_of_work_factory=lambda: SQLiteUnitOfWork(sqlite_config, clock=frozen_clock),
        workflow_definitions=WorkflowDefinitionRegistry((definition,)),
    ).replay_project(state.metadata.project_id)
    assert replay.reconstructed_state.workflow_registry[definition.workflow_id].current_state == (
        definition.initial_state
    )


@pytest.mark.parametrize(
    "trigger_sql",
    [
        "CREATE TRIGGER fail_workflow_save BEFORE UPDATE ON project_aggregates "
        "BEGIN SELECT RAISE(ABORT, 'injected save fault'); END",
        "CREATE TRIGGER fail_workflow_event BEFORE INSERT ON project_events "
        "BEGIN SELECT RAISE(ABORT, 'injected event fault'); END",
        "CREATE TRIGGER fail_workflow_completion BEFORE UPDATE ON idempotency_records "
        "WHEN OLD.operation_name='project.workflow.initialize' "
        "BEGIN SELECT RAISE(ABORT, 'injected completion fault'); END",
    ],
    ids=("aggregate-save", "event-append", "idempotency-complete"),
)
def test_sqlite_initialization_faults_roll_back_atomically(
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
    command = _command_for(state, stored, key="workflow:sqlite:fault")

    with pytest.raises(PersistenceError):
        _service(sqlite_config, frozen_clock).initialize_workflow(command)

    assert _counts(sqlite_config) == before


def test_tampered_workflow_event_fails_closed(
    sqlite_config: SQLiteConfig,
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
) -> None:
    from arch_kernel.kernel import canonicalize_json, compute_fingerprint

    _initialize(sqlite_config, frozen_clock)
    state, stored = _seed(sqlite_config, frozen_clock, create_command, project_ids)
    definition = workflow_definition()
    command = _command_for(state, stored, key="workflow:sqlite:tamper")
    _service(sqlite_config, frozen_clock).initialize_workflow(command)
    connection = open_sqlite_connection(sqlite_config)
    try:
        row = connection.execute(
            "SELECT event_json FROM project_events WHERE event_type = ?",
            ("project.aggregate.workflow_initialized",),
        ).fetchone()
        assert row is not None
        envelope = json.loads(row[0])
        envelope["payload"]["workflow_state"]["current_state"] = "running"
        event_json = canonicalize_json(envelope)
        connection.execute(
            "UPDATE project_events SET event_json = ?, event_fingerprint = ? WHERE event_type = ?",
            (
                event_json,
                compute_fingerprint(envelope),
                "project.aggregate.workflow_initialized",
            ),
        )
    finally:
        connection.close()

    replay = ReplayService(
        unit_of_work_factory=lambda: SQLiteUnitOfWork(sqlite_config, clock=frozen_clock),
        workflow_definitions=WorkflowDefinitionRegistry((definition,)),
    )
    with pytest.raises(ReplayIntegrityError):
        replay.replay_project(state.metadata.project_id)
