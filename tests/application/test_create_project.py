from __future__ import annotations

from typing import Any

import pytest
from arch_kernel.contracts import (
    EventEnvelope,
    ProjectState,
    ValidationIntent,
    ValidationPipelineRequest,
    ValidationPipelineResult,
)
from arch_kernel.kernel import compute_fingerprint, run_validation_pipeline

from arch_runtime.application import CreateProjectCommand, CreateProjectService, create_project
from arch_runtime.errors import IdempotencyConflictError, PersistenceError
from arch_runtime.ports import IdempotencyRecord, IdempotencyStatus
from tests.application.conftest import EventIds, ProjectIds
from tests.fakes.adapters import FakeUnitOfWork, FrozenClock, InMemoryEventStore

pytestmark = pytest.mark.unit


class RejectingValidationRunner:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, request: ValidationPipelineRequest, **_: Any) -> ValidationPipelineResult:
        self.calls += 1
        return run_validation_pipeline(
            ValidationPipelineRequest.model_validate(
                {
                    "intent": ValidationIntent.VALIDATE_STATE,
                    "evaluated_at": request.evaluated_at,
                    "raw_state": {"contract_name": "project_state"},
                }
            )
        )


class FailingEventStore(InMemoryEventStore):
    def append(self, *args: object, **kwargs: object) -> int:
        raise PersistenceError(
            "injected event failure",
            operation="event.append",
            remediation="test",
        )


def _service(
    uow: FakeUnitOfWork,
    clock: FrozenClock,
    project_ids: ProjectIds,
    event_ids: EventIds,
    *,
    validation_runner: Any = run_validation_pipeline,
) -> CreateProjectService:
    return CreateProjectService(
        unit_of_work_factory=lambda: uow,
        clock=clock,
        project_ids=project_ids,
        event_ids=event_ids,
        validation_runner=validation_runner,
    )


def test_command_normalizes_before_request_fingerprinting(
    create_command: CreateProjectCommand,
) -> None:
    equivalent = create_command.model_copy(update={"idempotency_key": "another-key"})
    assert create_command.name == "Runtime Pilot"
    assert create_command.slug == "runtime-pilot"
    assert create_command.objectives == ("Prove the vertical slice.",)
    assert compute_fingerprint(create_command.request_payload()) == compute_fingerprint(
        equivalent.request_payload()
    )


def test_create_project_commits_and_replays_exact_result_without_reevaluation(
    create_command: CreateProjectCommand,
    frozen_clock: FrozenClock,
    project_ids: ProjectIds,
    event_ids: EventIds,
) -> None:
    uow = FakeUnitOfWork(frozen_clock)
    service = _service(uow, frozen_clock, project_ids, event_ids)

    first = create_project(service, create_command)
    second = service.create_project(create_command)

    assert first == second
    assert first.success
    assert first.validation_result.contract_stage.executed
    assert first.validation_result.aggregate_stage.executed
    assert project_ids.calls == 1
    assert event_ids.calls == 1
    stored = uow.projects.get(first.project_id)
    assert stored is not None
    state = ProjectState.model_validate_json(stored.state_json, strict=True)
    assert state.metadata.name == create_command.name
    events = uow.events.read_stream(first.project_id)
    assert len(events) == 1
    assert events[0].event_type == "project.aggregate.created"
    event = EventEnvelope.model_validate_json(events[0].event_json, strict=True)
    payload = event.model_dump(mode="json")["payload"]
    assert payload["event_name"] == "project.created"
    assert payload["state"]["metadata"]["project_id"] == str(first.project_id)
    assert payload["record_fingerprint"] == first.record_fingerprint
    evidence = uow.idempotency.get("project.create", create_command.idempotency_key)
    assert evidence is not None
    assert evidence.status is IdempotencyStatus.COMPLETED


def test_same_key_with_different_request_is_rejected(
    create_command: CreateProjectCommand,
    frozen_clock: FrozenClock,
    project_ids: ProjectIds,
    event_ids: EventIds,
) -> None:
    service = _service(FakeUnitOfWork(frozen_clock), frozen_clock, project_ids, event_ids)
    service.create_project(create_command)
    changed = create_command.model_copy(update={"name": "Different"})

    with pytest.raises(IdempotencyConflictError):
        service.create_project(changed)


def test_k08_rejection_persists_only_canonical_idempotency_result(
    create_command: CreateProjectCommand,
    frozen_clock: FrozenClock,
    project_ids: ProjectIds,
    event_ids: EventIds,
) -> None:
    runner = RejectingValidationRunner()
    uow = FakeUnitOfWork(frozen_clock)
    service = _service(
        uow,
        frozen_clock,
        project_ids,
        event_ids,
        validation_runner=runner,
    )

    first = service.create_project(create_command)
    second = service.create_project(create_command)

    assert first == second
    assert not first.success
    assert first.validation_result.blocking_failure
    assert runner.calls == 1
    assert project_ids.calls == 1
    assert event_ids.calls == 0
    assert uow.projects.get(first.project_id) is None
    assert uow.events.read_stream(first.project_id) == ()
    evidence = uow.idempotency.get("project.create", create_command.idempotency_key)
    assert evidence is not None
    assert evidence.status is IdempotencyStatus.COMPLETED


def test_infrastructure_failure_rolls_back_aggregate_event_and_reservation(
    create_command: CreateProjectCommand,
    frozen_clock: FrozenClock,
    project_ids: ProjectIds,
    event_ids: EventIds,
) -> None:
    uow = FakeUnitOfWork(frozen_clock)
    failing = FailingEventStore()
    uow._events = failing
    uow.events = failing
    service = _service(uow, frozen_clock, project_ids, event_ids)

    with pytest.raises(PersistenceError, match="injected"):
        service.create_project(create_command)

    project_id = project_ids.new()
    assert uow.projects.get(project_id) is None
    assert uow.idempotency.get("project.create", create_command.idempotency_key) is None


def test_incomplete_idempotency_reservation_is_not_reexecuted(
    create_command: CreateProjectCommand,
    frozen_clock: FrozenClock,
    project_ids: ProjectIds,
    event_ids: EventIds,
) -> None:
    uow = FakeUnitOfWork(frozen_clock)
    fingerprint = compute_fingerprint(create_command.request_payload())
    uow.idempotency.reserve(
        IdempotencyRecord(
            operation_name="project.create",
            idempotency_key=create_command.idempotency_key,
            project_id=None,
            request_fingerprint=fingerprint,
            status=IdempotencyStatus.IN_PROGRESS,
            created_at=frozen_clock.now(),
        )
    )
    service = _service(uow, frozen_clock, project_ids, event_ids)

    with pytest.raises(PersistenceError, match="incomplete"):
        service.create_project(create_command)
    assert project_ids.calls == 0
    assert event_ids.calls == 0


def test_generated_project_id_collision_rolls_back_second_reservation(
    create_command: CreateProjectCommand,
    frozen_clock: FrozenClock,
    project_ids: ProjectIds,
    event_ids: EventIds,
) -> None:
    uow = FakeUnitOfWork(frozen_clock)
    service = _service(uow, frozen_clock, project_ids, event_ids)
    first = service.create_project(create_command)
    second_command = create_command.model_copy(update={"idempotency_key": "project:create:002"})

    with pytest.raises(IdempotencyConflictError, match="identity"):
        service.create_project(second_command)

    assert len(uow.events.read_stream(first.project_id)) == 1
    assert uow.idempotency.get("project.create", second_command.idempotency_key) is None


def test_corrupt_completed_result_is_not_replayed(
    create_command: CreateProjectCommand,
    frozen_clock: FrozenClock,
    project_ids: ProjectIds,
    event_ids: EventIds,
) -> None:
    uow = FakeUnitOfWork(frozen_clock)
    uow._idempotency.records[("project.create", create_command.idempotency_key)] = (
        IdempotencyRecord(
            operation_name="project.create",
            idempotency_key=create_command.idempotency_key,
            project_id=None,
            request_fingerprint=compute_fingerprint(create_command.request_payload()),
            status=IdempotencyStatus.COMPLETED,
            result_fingerprint="sha256:" + "f" * 64,
            result_json=b"{}",
            created_at=frozen_clock.now(),
            completed_at=frozen_clock.now(),
        )
    )
    service = _service(uow, frozen_clock, project_ids, event_ids)

    from arch_runtime.errors import CorruptStoredRecordError

    with pytest.raises(CorruptStoredRecordError):
        service.create_project(create_command)
