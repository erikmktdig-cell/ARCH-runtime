from __future__ import annotations

from typing import Any

import pytest
from arch_kernel.contracts import (
    ChangeId,
    EffectType,
    EffectValueSource,
    EventEnvelope,
    EventId,
    PhaseStatus,
    ProjectState,
    SemanticVersion,
    TransitionDefinition,
    TransitionDomain,
    TransitionEffect,
    TransitionId,
    TransitionTarget,
    ValidationPipelineRequest,
    ValidationPipelineResult,
)
from arch_kernel.kernel import InvariantRegistry, TransitionRegistry, run_validation_pipeline

from arch_runtime.application import (
    ApplyTransitionCommand,
    ApplyTransitionService,
    CreateProjectCommand,
    CreateProjectService,
    apply_transition,
)
from arch_runtime.errors import ConcurrentModificationError, IdempotencyConflictError
from arch_runtime.ports import IdempotencyStatus, StoredProject
from tests.application.conftest import EventIds, ProjectIds
from tests.fakes.adapters import FakeUnitOfWork, FrozenClock, stored_project_from_state

pytestmark = pytest.mark.unit
VERSION = SemanticVersion.parse("1.0.0")


class TransitionEventIds:
    def __init__(self) -> None:
        self.calls = 0

    def new(self) -> EventId:
        self.calls += 1
        return EventId("EVT-01HZX7M3FQ1T2Q9V8Y6K4C2R06")


class CountingRunner:
    def __init__(self, uow: FakeUnitOfWork | None = None) -> None:
        self.calls = 0
        self.uow = uow

    def __call__(
        self, request: ValidationPipelineRequest, **kwargs: Any
    ) -> ValidationPipelineResult:
        self.calls += 1
        result = run_validation_pipeline(request, **kwargs)
        if self.uow is not None:
            assert request.state is not None
            concurrent = request.state.model_copy(
                update={"record_version": request.state.record_version + 1}
            )
            self.uow._projects.records[request.state.metadata.project_id] = (
                stored_project_from_state(concurrent)
            )
        return result


def _definition(*, no_op: bool = False) -> TransitionDefinition:
    return TransitionDefinition(
        transition_id=TransitionId("TRN-01HZX7M3FQ1T2Q9V8Y6K4C2R06"),
        name="Cancel project" if not no_op else "Confirm not started",
        transition_key="project.cancel" if not no_op else "project.confirm_not_started",
        contract_version=VERSION,
        domain=TransitionDomain.PROJECT_LIFECYCLE,
        from_states=(PhaseStatus.NOT_STARTED.value,),
        to_state=(PhaseStatus.CANCELLED.value if not no_op else PhaseStatus.NOT_STARTED.value),
        effects=(
            TransitionEffect(
                effect_type=(EffectType.ASSERT_FIELD if no_op else EffectType.SET_FIELD),
                path_template="/lifecycle/status",
                value_source=EffectValueSource.TO_STATE,
            ),
        ),
        allow_self_transition=no_op,
    )


def _created(
    uow: FakeUnitOfWork,
    clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
) -> tuple[ProjectState, StoredProject]:
    result = CreateProjectService(
        unit_of_work_factory=lambda: uow,
        clock=clock,
        project_ids=project_ids,
        event_ids=EventIds(),
    ).create_project(create_command)
    stored = uow.projects.get(result.project_id)
    assert stored is not None
    return ProjectState.model_validate_json(stored.state_json, strict=True), stored


def _command(
    state: ProjectState,
    stored: StoredProject,
    *,
    key: str = "transition:001",
) -> ApplyTransitionCommand:
    return ApplyTransitionCommand.model_validate(
        {
            "idempotency_key": f" {key} ",
            "project_id": state.metadata.project_id,
            "request_id": ChangeId("CHG-01HZX7M3FQ1T2Q9V8Y6K4C2R06"),
            "target": TransitionTarget(
                domain=TransitionDomain.PROJECT_LIFECYCLE,
                entity_id=state.metadata.project_id,
            ),
            "transition_key": " project.cancel ",
            "expected_from_state": PhaseStatus.NOT_STARTED.value,
            "expected_record_version": stored.record_version,
            "expected_record_fingerprint": stored.record_fingerprint,
            "expected_content_fingerprint": stored.content_fingerprint,
            "metadata": {" source ": " test "},
            "actor_id": " user:erik ",
        },
        strict=True,
    )


def _service(
    uow: FakeUnitOfWork,
    clock: FrozenClock,
    definition: TransitionDefinition,
    event_ids: TransitionEventIds,
    *,
    runner: Any = run_validation_pipeline,
) -> ApplyTransitionService:
    return ApplyTransitionService(
        unit_of_work_factory=lambda: uow,
        clock=clock,
        event_ids=event_ids,
        transition_registry=TransitionRegistry((definition,)),
        invariant_registry=InvariantRegistry(()),
        validation_runner=runner,
    )


def test_command_is_normalized_immutable_and_fingerprints_without_key(
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
) -> None:
    uow = FakeUnitOfWork(frozen_clock)
    state, stored = _created(uow, frozen_clock, create_command, project_ids)
    command = _command(state, stored)
    equivalent = command.model_copy(update={"idempotency_key": "different"})

    assert command.idempotency_key == "transition:001"
    assert command.transition_key == "project.cancel"
    assert command.metadata == {"source": "test"}
    with pytest.raises(TypeError):
        command.metadata["source"] = "changed"  # type: ignore[index]
    assert command.request_payload() == equivalent.request_payload()


def test_transition_commits_event_and_replays_exact_result_without_reevaluation(
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
) -> None:
    uow = FakeUnitOfWork(frozen_clock)
    state, stored = _created(uow, frozen_clock, create_command, project_ids)
    command = _command(state, stored)
    event_ids = TransitionEventIds()
    runner = CountingRunner()
    service = _service(uow, frozen_clock, _definition(), event_ids, runner=runner)

    first = apply_transition(service, command)
    second = service.apply_transition(command)

    assert first == second
    assert first.success
    assert first.changed
    assert first.after_record_version == first.before_record_version + 1
    assert runner.calls == 1
    assert event_ids.calls == 1
    stream = uow.events.read_stream(command.project_id)
    assert len(stream) == 2
    assert stream[-1].previous_event_fingerprint == stream[0].event_fingerprint
    assert stream[-1].event_type == "project.aggregate.transition_applied"
    event = EventEnvelope.model_validate_json(stream[-1].event_json, strict=True)
    payload = event.model_dump(mode="json")["payload"]
    assert payload["event_name"] == "project.transition_applied"
    assert first.validation_result.generated_patch is not None
    assert payload["patch"] == first.validation_result.generated_patch.model_dump(mode="json")
    assert payload["before"]["record_fingerprint"] == first.before_record_fingerprint
    assert payload["after"]["record_fingerprint"] == first.after_record_fingerprint
    evidence = uow.idempotency.get("project.transition", command.idempotency_key)
    assert evidence is not None
    assert evidence.status is IdempotencyStatus.COMPLETED


def test_rejection_is_idempotent_and_writes_no_state_or_event(
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
) -> None:
    uow = FakeUnitOfWork(frozen_clock)
    state, stored = _created(uow, frozen_clock, create_command, project_ids)
    command = _command(state, stored).model_copy(update={"transition_key": "project.missing"})
    runner = CountingRunner()
    service = _service(uow, frozen_clock, _definition(), TransitionEventIds(), runner=runner)

    before_events = uow.events.read_stream(command.project_id)
    first = service.apply_transition(command)
    second = service.apply_transition(command)

    assert first == second
    assert not first.success
    assert not first.changed
    assert runner.calls == 1
    assert uow.projects.get(command.project_id) == stored
    assert uow.events.read_stream(command.project_id) == before_events


def test_no_op_completes_idempotency_without_mutation_event_or_new_version(
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
) -> None:
    uow = FakeUnitOfWork(frozen_clock)
    state, stored = _created(uow, frozen_clock, create_command, project_ids)
    command = _command(state, stored).model_copy(
        update={"transition_key": "project.confirm_not_started"}
    )
    service = _service(uow, frozen_clock, _definition(no_op=True), TransitionEventIds())

    result = service.apply_transition(command)

    assert result.success
    assert not result.changed
    assert result.after_record_version == result.before_record_version
    assert result.after_record_fingerprint == result.before_record_fingerprint
    assert result.event_id is None
    assert result.stream_position is None
    assert uow.projects.get(command.project_id) == stored
    assert len(uow.events.read_stream(command.project_id)) == 1


def test_stale_precondition_fails_before_evaluation_and_key_can_be_reused(
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
) -> None:
    uow = FakeUnitOfWork(frozen_clock)
    state, stored = _created(uow, frozen_clock, create_command, project_ids)
    command = _command(state, stored).model_copy(update={"expected_record_version": 99})
    runner = CountingRunner()
    service = _service(uow, frozen_clock, _definition(), TransitionEventIds(), runner=runner)

    with pytest.raises(ConcurrentModificationError):
        service.apply_transition(command)
    assert runner.calls == 0
    assert uow.idempotency.get("project.transition", command.idempotency_key) is None


def test_concurrent_change_after_evaluation_rolls_back_reservation_and_event(
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
) -> None:
    uow = FakeUnitOfWork(frozen_clock)
    state, stored = _created(uow, frozen_clock, create_command, project_ids)
    command = _command(state, stored)
    runner = CountingRunner(uow)
    service = _service(uow, frozen_clock, _definition(), TransitionEventIds(), runner=runner)

    with pytest.raises(ConcurrentModificationError, match="during"):
        service.apply_transition(command)
    assert len(uow.events.read_stream(command.project_id)) == 1
    assert uow.idempotency.get("project.transition", command.idempotency_key) is None


def test_same_key_with_different_transition_request_conflicts(
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
) -> None:
    uow = FakeUnitOfWork(frozen_clock)
    state, stored = _created(uow, frozen_clock, create_command, project_ids)
    command = _command(state, stored)
    service = _service(uow, frozen_clock, _definition(), TransitionEventIds())
    service.apply_transition(command)

    with pytest.raises(IdempotencyConflictError):
        service.apply_transition(command.model_copy(update={"actor_id": "user:other"}))
