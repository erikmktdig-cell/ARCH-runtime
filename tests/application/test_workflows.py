from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import cast

import pytest
from arch_kernel.contracts import (
    ChangeId,
    EffectType,
    EffectValueSource,
    EventEnvelope,
    EventId,
    FrozenJsonObject,
    ProjectState,
    SemanticVersion,
    TransitionDomain,
    TransitionEffect,
    TransitionId,
    TransitionTarget,
    WorkflowDefinition,
    WorkflowId,
    WorkflowNamespace,
    WorkflowStateKey,
    WorkflowTransitionDefinition,
)
from arch_kernel.kernel import (
    ArchKernelError,
    InvariantRegistry,
    TransitionRegistry,
    WorkflowDefinitionRegistry,
    compute_content_fingerprint,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from arch_runtime.application import (
    ApplyTransitionCommand,
    ApplyTransitionService,
    CreateProjectCommand,
    CreateProjectResult,
    CreateProjectService,
    CreateSnapshotCommand,
    InitializeWorkflowCommand,
    InitializeWorkflowService,
    RecoverAggregateCommand,
    RecoveryService,
    SnapshotPolicy,
    SnapshotService,
    WorkflowInitializationDisposition,
)
from arch_runtime.errors import (
    ConcurrentModificationError,
    CorruptStoredRecordError,
    IdempotencyConflictError,
)
from arch_runtime.ports import StoredProject
from arch_runtime.replay import ReplayService
from tests.application.conftest import EventIds, ProjectIds
from tests.fakes.adapters import FakeUnitOfWork, FrozenClock, stored_project_from_state

pytestmark = pytest.mark.unit
VERSION = SemanticVersion.parse("1.0.0")
WORKFLOW_ID = WorkflowId.from_str("WFL-01HZX7M3FQ1T2Q9V8Y6K4C2B1A")
NAMESPACE = WorkflowNamespace.parse("example.delivery")


class WorkflowEventIds:
    def __init__(self) -> None:
        self.calls = 0

    def new(self) -> EventId:
        self.calls += 1
        return EventId(f"EVT-01HZX7M3FQ1T2Q9V8Y6K4C2R{10 + self.calls:02d}")


def workflow_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        contract_name="workflow_definition",
        contract_version=VERSION,
        schema_uri="urn:arch:contracts:workflow_definition:1.0.0",
        created_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
        workflow_id=WORKFLOW_ID,
        namespace=NAMESPACE,
        definition_version=VERSION,
        allowed_states=(
            WorkflowStateKey.parse("queued"),
            WorkflowStateKey.parse("running"),
            WorkflowStateKey.parse("done"),
        ),
        initial_state=WorkflowStateKey.parse("queued"),
    )


def workflow_transition(definition: WorkflowDefinition) -> WorkflowTransitionDefinition:
    return WorkflowTransitionDefinition(
        transition_id=TransitionId("TRN-01HZX7M3FQ1T2Q9V8Y6K4C2R20"),
        name="Start delivery",
        transition_key="workflow.start_delivery",
        contract_version=VERSION,
        domain=TransitionDomain.WORKFLOW_STATE,
        from_states=("queued",),
        to_state="running",
        effects=(
            TransitionEffect(
                effect_type=EffectType.SET_FIELD,
                path_template="/workflows/{entity_id}/current_state",
                value_source=EffectValueSource.TO_STATE,
            ),
        ),
        workflow_namespace=definition.namespace,
        workflow_definition_version=definition.definition_version,
        workflow_definition_fingerprint=definition.definition_fingerprint(),
    )


def workflow_completion_transition(
    definition: WorkflowDefinition,
) -> WorkflowTransitionDefinition:
    return WorkflowTransitionDefinition(
        transition_id=TransitionId("TRN-01HZX7M3FQ1T2Q9V8Y6K4C2R21"),
        name="Complete delivery",
        transition_key="workflow.complete_delivery",
        contract_version=VERSION,
        domain=TransitionDomain.WORKFLOW_STATE,
        from_states=("running",),
        to_state="done",
        effects=(
            TransitionEffect(
                effect_type=EffectType.SET_FIELD,
                path_template="/workflows/{entity_id}/current_state",
                value_source=EffectValueSource.TO_STATE,
            ),
        ),
        workflow_namespace=definition.namespace,
        workflow_definition_version=definition.definition_version,
        workflow_definition_fingerprint=definition.definition_fingerprint(),
    )


def _seed(
    uow: FakeUnitOfWork,
    clock: FrozenClock,
    command: CreateProjectCommand,
    project_ids: ProjectIds,
) -> tuple[CreateProjectResult, StoredProject]:
    created = CreateProjectService(
        unit_of_work_factory=lambda: uow,
        clock=clock,
        project_ids=project_ids,
        event_ids=EventIds(),
    ).create_project(command)
    stored = uow.projects.get(created.project_id)
    assert stored is not None
    return created, stored


def _command(
    created: CreateProjectResult,
    stored: StoredProject,
    definition: WorkflowDefinition,
    *,
    key: str,
) -> InitializeWorkflowCommand:
    return InitializeWorkflowCommand(
        idempotency_key=key,
        project_id=created.project_id,
        workflow_id=definition.workflow_id,
        workflow_namespace=definition.namespace,
        definition_version=definition.definition_version,
        definition_fingerprint=definition.definition_fingerprint(),
        expected_record_version=stored.record_version,
        expected_record_fingerprint=stored.record_fingerprint,
        expected_content_fingerprint=stored.content_fingerprint,
        actor_id="user:workflow",
    )


def _service(
    uow: FakeUnitOfWork,
    clock: FrozenClock,
    definition: WorkflowDefinition,
    event_ids: WorkflowEventIds,
) -> InitializeWorkflowService:
    return InitializeWorkflowService(
        unit_of_work_factory=lambda: uow,
        clock=clock,
        event_ids=event_ids,
        workflow_definitions=WorkflowDefinitionRegistry((definition,)),
        invariant_registry=InvariantRegistry(()),
    )


def test_initialization_is_atomic_replayable_and_exactly_idempotent(
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
) -> None:
    uow = FakeUnitOfWork(frozen_clock)
    created, stored = _seed(uow, frozen_clock, create_command, project_ids)
    definition = workflow_definition()
    ids = WorkflowEventIds()
    command = _command(created, stored, definition, key="workflow:init:001")
    service = _service(uow, frozen_clock, definition, ids)

    first = service.initialize_workflow(command)
    repeated = service.initialize_workflow(command)

    assert repeated == first
    assert first.success
    assert first.changed
    assert first.disposition is WorkflowInitializationDisposition.INITIALIZED
    assert first.workflow_state is not None
    assert first.workflow_state.current_state == definition.initial_state
    assert first.after_record_version == first.before_record_version + 1
    assert ids.calls == 1
    stream = uow.events.read_stream(first.project_id)
    assert len(stream) == 2
    assert stream[-1].event_type == "project.aggregate.workflow_initialized"
    envelope = EventEnvelope.model_validate_json(stream[-1].event_json, strict=True)
    assert envelope.model_dump(mode="json")["payload"]["event_name"] == (
        "project.workflow_initialized"
    )
    replay = ReplayService(
        unit_of_work_factory=lambda: uow,
        workflow_definitions=WorkflowDefinitionRegistry((definition,)),
    ).replay_project(first.project_id)
    assert replay.record_fingerprint == first.after_record_fingerprint
    assert replay.reconstructed_state.workflow_registry[WORKFLOW_ID] == first.workflow_state


def test_initial_state_cannot_be_injected_and_invalid_destination_is_rejected() -> None:
    definition = workflow_definition()
    with pytest.raises(ValidationError, match="initial_state"):
        InitializeWorkflowCommand.model_validate(
            {
                "idempotency_key": "workflow:init:injected",
                "project_id": "PRJ-01HZX7M3FQ1T2Q9V8Y6K4C2B1A",
                "workflow_id": str(definition.workflow_id),
                "workflow_namespace": str(definition.namespace),
                "definition_version": str(definition.definition_version),
                "definition_fingerprint": definition.definition_fingerprint(),
                "expected_record_version": 1,
                "expected_record_fingerprint": "sha256:" + "a" * 64,
                "expected_content_fingerprint": "sha256:" + "b" * 64,
                "actor_id": "user:workflow",
                "initial_state": "running",
            }
        )

    invalid = workflow_transition(definition).model_copy(update={"to_state": "invented"})
    with pytest.raises(ArchKernelError, match="invalid destination state"):
        TransitionRegistry((invalid,), workflow_definitions=(definition,))


def test_fresh_key_reports_existing_exact_workflow_without_mutation(
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
) -> None:
    uow = FakeUnitOfWork(frozen_clock)
    created, stored = _seed(uow, frozen_clock, create_command, project_ids)
    definition = workflow_definition()
    ids = WorkflowEventIds()
    service = _service(uow, frozen_clock, definition, ids)
    service.initialize_workflow(_command(created, stored, definition, key="workflow:init:one"))
    current = uow.projects.get(created.project_id)
    assert current is not None

    result = service.initialize_workflow(
        _command(created, current, definition, key="workflow:init:two")
    )

    assert result.success
    assert not result.changed
    assert result.disposition is WorkflowInitializationDisposition.ALREADY_INITIALIZED
    assert result.after_record_fingerprint == current.record_fingerprint
    assert len(uow.events.read_stream(result.project_id)) == 2
    assert ids.calls == 1


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            {"workflow_namespace": WorkflowNamespace.parse("unknown.delivery")},
            WorkflowInitializationDisposition.UNKNOWN_DEFINITION,
        ),
        (
            {"definition_fingerprint": "sha256:" + "f" * 64},
            WorkflowInitializationDisposition.DEFINITION_MISMATCH,
        ),
        (
            {"workflow_id": WorkflowId.from_str("WFL-01HZX7M3FQ1T2Q9V8Y6K4C2B1B")},
            WorkflowInitializationDisposition.DEFINITION_MISMATCH,
        ),
    ],
)
def test_unknown_or_mismatching_definition_is_completed_rejection(
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
    mutation: dict[str, object],
    expected: WorkflowInitializationDisposition,
) -> None:
    uow = FakeUnitOfWork(frozen_clock)
    created, stored = _seed(uow, frozen_clock, create_command, project_ids)
    definition = workflow_definition()
    service = _service(uow, frozen_clock, definition, WorkflowEventIds())
    command = _command(created, stored, definition, key="workflow:init:reject").model_copy(
        update=mutation
    )

    result = service.initialize_workflow(command)

    assert not result.success
    assert not result.changed
    assert result.disposition is expected
    assert uow.projects.get(result.project_id) == stored
    assert len(uow.events.read_stream(result.project_id)) == 1
    assert service.initialize_workflow(command) == result


def test_stale_aggregate_and_conflicting_retry_fail_without_reservation(
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
) -> None:
    uow = FakeUnitOfWork(frozen_clock)
    created, stored = _seed(uow, frozen_clock, create_command, project_ids)
    definition = workflow_definition()
    service = _service(uow, frozen_clock, definition, WorkflowEventIds())
    command = _command(created, stored, definition, key="workflow:init:cas")

    with pytest.raises(ConcurrentModificationError):
        service.initialize_workflow(command.model_copy(update={"expected_record_version": 99}))
    assert uow.idempotency.get("project.workflow.initialize", command.idempotency_key) is None
    service.initialize_workflow(command)
    with pytest.raises(IdempotencyConflictError):
        service.initialize_workflow(command.model_copy(update={"actor_id": "user:other"}))


def test_public_transition_advances_authoritative_workflow_and_replays(
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
) -> None:
    uow = FakeUnitOfWork(frozen_clock)
    created, stored = _seed(uow, frozen_clock, create_command, project_ids)
    definition = workflow_definition()
    ids = WorkflowEventIds()
    initialized = _service(uow, frozen_clock, definition, ids).initialize_workflow(
        _command(created, stored, definition, key="workflow:init:transition")
    )
    current = uow.projects.get(initialized.project_id)
    assert current is not None
    workflow = initialized.workflow_state
    assert workflow is not None
    registry = TransitionRegistry(
        (workflow_transition(definition), workflow_completion_transition(definition)),
        workflow_definitions=(definition,),
    )
    command = ApplyTransitionCommand(
        idempotency_key="workflow:transition:001",
        project_id=initialized.project_id,
        request_id=ChangeId("CHG-01HZX7M3FQ1T2Q9V8Y6K4C2R21"),
        target=TransitionTarget(
            domain=TransitionDomain.WORKFLOW_STATE,
            entity_id=WORKFLOW_ID,
        ),
        transition_key="workflow.start_delivery",
        expected_from_state="queued",
        expected_record_version=current.record_version,
        expected_record_fingerprint=current.record_fingerprint,
        expected_content_fingerprint=current.content_fingerprint,
        expected_target_record_version=workflow.record_version,
        expected_target_content_fingerprint=compute_content_fingerprint(workflow),
        metadata=cast(FrozenJsonObject, {"source": "workflow-test"}),
        actor_id="user:workflow",
    )
    transitions = ApplyTransitionService(
        unit_of_work_factory=lambda: uow,
        clock=frozen_clock,
        event_ids=ids,
        transition_registry=registry,
        invariant_registry=InvariantRegistry(()),
    )

    result = transitions.apply_transition(command)
    repeated = transitions.apply_transition(command)

    assert result == repeated
    assert result.success
    assert result.changed
    assert result.validation_result.projected_state is not None
    changed = result.validation_result.projected_state.workflow_registry[WORKFLOW_ID]
    assert str(changed.current_state) == "running"
    assert changed.record_version == workflow.record_version + 1
    after_first = uow.projects.get(initialized.project_id)
    assert after_first is not None
    second = transitions.apply_transition(
        ApplyTransitionCommand(
            idempotency_key="workflow:transition:002",
            project_id=initialized.project_id,
            request_id=ChangeId("CHG-01HZX7M3FQ1T2Q9V8Y6K4C2R23"),
            target=TransitionTarget(
                domain=TransitionDomain.WORKFLOW_STATE,
                entity_id=WORKFLOW_ID,
            ),
            transition_key="workflow.complete_delivery",
            expected_from_state="running",
            expected_record_version=after_first.record_version,
            expected_record_fingerprint=after_first.record_fingerprint,
            expected_content_fingerprint=after_first.content_fingerprint,
            expected_target_record_version=changed.record_version,
            expected_target_content_fingerprint=compute_content_fingerprint(changed),
            metadata=cast(FrozenJsonObject, {"source": "workflow-test"}),
            actor_id="user:workflow",
        )
    )
    assert second.success
    assert second.changed
    assert second.validation_result.projected_state is not None
    completed = second.validation_result.projected_state.workflow_registry[WORKFLOW_ID]
    assert str(completed.current_state) == "done"
    replay = ReplayService(
        unit_of_work_factory=lambda: uow,
        workflow_definitions=WorkflowDefinitionRegistry((definition,)),
    ).replay_project(initialized.project_id)
    assert replay.reconstructed_state == second.validation_result.projected_state


@pytest.mark.parametrize(
    "mutation",
    [
        {"expected_from_state": "running"},
        {"expected_target_record_version": 99},
        {"expected_target_content_fingerprint": "sha256:" + "e" * 64},
        {
            "target": TransitionTarget(
                domain=TransitionDomain.WORKFLOW_STATE,
                entity_id=WorkflowId.from_str("WFL-01HZX7M3FQ1T2Q9V8Y6K4C2B1C"),
            )
        },
    ],
)
def test_workflow_transition_rejections_write_no_event_or_state(
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
    mutation: dict[str, object],
) -> None:
    uow = FakeUnitOfWork(frozen_clock)
    created, stored = _seed(uow, frozen_clock, create_command, project_ids)
    definition = workflow_definition()
    initialized = _service(uow, frozen_clock, definition, WorkflowEventIds()).initialize_workflow(
        _command(created, stored, definition, key="workflow:init:reject-transition")
    )
    current = uow.projects.get(initialized.project_id)
    assert current is not None
    assert initialized.workflow_state is not None
    command = ApplyTransitionCommand(
        idempotency_key="workflow:transition:reject",
        project_id=initialized.project_id,
        request_id=ChangeId("CHG-01HZX7M3FQ1T2Q9V8Y6K4C2R22"),
        target=TransitionTarget(
            domain=TransitionDomain.WORKFLOW_STATE,
            entity_id=WORKFLOW_ID,
        ),
        transition_key="workflow.start_delivery",
        expected_from_state="queued",
        expected_record_version=current.record_version,
        expected_record_fingerprint=current.record_fingerprint,
        expected_content_fingerprint=current.content_fingerprint,
        expected_target_record_version=initialized.workflow_state.record_version,
        expected_target_content_fingerprint=compute_content_fingerprint(initialized.workflow_state),
        metadata=cast(FrozenJsonObject, {}),
        actor_id="user:workflow",
    ).model_copy(update=mutation)
    before_events = uow.events.read_stream(initialized.project_id)
    service = ApplyTransitionService(
        unit_of_work_factory=lambda: uow,
        clock=frozen_clock,
        event_ids=WorkflowEventIds(),
        transition_registry=TransitionRegistry(
            (workflow_transition(definition),), workflow_definitions=(definition,)
        ),
        invariant_registry=InvariantRegistry(()),
    )

    result = service.apply_transition(command)

    assert not result.success
    assert not result.changed
    assert uow.projects.get(initialized.project_id) == current
    assert uow.events.read_stream(initialized.project_id) == before_events


def test_same_namespace_with_another_definition_is_rejected_without_overwrite(
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
) -> None:
    uow = FakeUnitOfWork(frozen_clock)
    created, stored = _seed(uow, frozen_clock, create_command, project_ids)
    first = workflow_definition()
    ids = WorkflowEventIds()
    _service(uow, frozen_clock, first, ids).initialize_workflow(
        _command(created, stored, first, key="workflow:init:namespace:first")
    )
    current = uow.projects.get(created.project_id)
    assert current is not None
    second = first.model_copy(
        update={
            "workflow_id": WorkflowId.from_str("WFL-01HZX7M3FQ1T2Q9V8Y6K4C2B1B"),
            "definition_version": SemanticVersion.parse("2.0.0"),
        }
    )
    service = InitializeWorkflowService(
        unit_of_work_factory=lambda: uow,
        clock=frozen_clock,
        event_ids=ids,
        workflow_definitions=WorkflowDefinitionRegistry((first, second)),
        invariant_registry=InvariantRegistry(()),
    )
    command = InitializeWorkflowCommand(
        idempotency_key="workflow:init:namespace:second",
        project_id=created.project_id,
        workflow_id=second.workflow_id,
        workflow_namespace=second.namespace,
        definition_version=second.definition_version,
        definition_fingerprint=second.definition_fingerprint(),
        expected_record_version=current.record_version,
        expected_record_fingerprint=current.record_fingerprint,
        expected_content_fingerprint=current.content_fingerprint,
        actor_id="user:workflow",
    )

    result = service.initialize_workflow(command)

    assert not result.success
    assert result.disposition is WorkflowInitializationDisposition.NAMESPACE_ALREADY_INITIALIZED
    assert uow.projects.get(created.project_id) == current
    assert ids.calls == 1


def test_tampered_persisted_workflow_binding_fails_closed(
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
) -> None:
    uow = FakeUnitOfWork(frozen_clock)
    created, stored = _seed(uow, frozen_clock, create_command, project_ids)
    definition = workflow_definition()
    service = _service(uow, frozen_clock, definition, WorkflowEventIds())
    service.initialize_workflow(_command(created, stored, definition, key="workflow:init:tamper"))
    current = uow.projects.get(created.project_id)
    assert current is not None
    state = ProjectState.model_validate_json(current.state_json, strict=True)
    record = state.workflow_registry[WORKFLOW_ID].model_copy(
        update={"definition_fingerprint": "sha256:" + "a" * 64}
    )
    tampered = state.model_copy(update={"workflow_registry": {WORKFLOW_ID: record}})
    uow._projects.records[created.project_id] = stored_project_from_state(tampered)
    tampered_stored = uow.projects.get(created.project_id)
    assert tampered_stored is not None

    with pytest.raises(CorruptStoredRecordError, match="configured authority"):
        service.initialize_workflow(
            _command(
                created,
                tampered_stored,
                definition,
                key="workflow:init:tamper:retry",
            )
        )


@settings(max_examples=10, deadline=None)
@given(key=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=12))
def test_initialization_idempotency_property(key: str) -> None:
    from datetime import UTC, datetime

    from arch_kernel.contracts import Route

    clock = FrozenClock(datetime(2026, 7, 31, 12, 0, tzinfo=UTC))
    uow = FakeUnitOfWork(clock)
    command = CreateProjectCommand(
        idempotency_key=f"create:{key}",
        name="Property Project",
        slug="property-project",
        owner="platform",
        project_type="test",
        criticality="low",
        default_route=Route.QUICK,
        actor_id="property",
    )
    created, stored = _seed(uow, clock, command, ProjectIds())
    definition = workflow_definition()
    service = _service(uow, clock, definition, WorkflowEventIds())
    initialization = _command(created, stored, definition, key=f"workflow:{key}")

    first = service.initialize_workflow(initialization)
    second = service.initialize_workflow(initialization)

    assert first == second
    assert len(uow.events.read_stream(created.project_id)) == 2


@pytest.mark.parametrize("boundary", ["project", "event", "idempotency"])
def test_initialization_faults_roll_back_every_surface(
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    uow = FakeUnitOfWork(frozen_clock)
    created, stored = _seed(uow, frozen_clock, create_command, project_ids)
    definition = workflow_definition()
    before = (
        deepcopy(uow._projects.records),
        deepcopy(uow._events.records),
        deepcopy(uow._idempotency.records),
    )

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError(f"injected {boundary} failure")

    target = {"project": uow._projects, "event": uow._events, "idempotency": uow._idempotency}[
        boundary
    ]
    method = {"project": "save", "event": "append", "idempotency": "complete"}[boundary]
    monkeypatch.setattr(target, method, fail)

    with pytest.raises(RuntimeError, match="injected"):
        _service(uow, frozen_clock, definition, WorkflowEventIds()).initialize_workflow(
            _command(created, stored, definition, key=f"workflow:init:fault:{boundary}")
        )
    assert (uow._projects.records, uow._events.records, uow._idempotency.records) == before


def test_snapshot_replay_and_explicit_recovery_preserve_workflow_authority(
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
) -> None:
    class SnapshotIds:
        def new(self) -> str:
            return "SNP-01HZX7M3FQ1T2Q9V8Y6K4C2R30"

    class RecoveryIds:
        def new(self) -> EventId:
            return EventId("EVT-01HZX7M3FQ1T2Q9V8Y6K4C2R30")

    uow = FakeUnitOfWork(frozen_clock)
    created, stored = _seed(uow, frozen_clock, create_command, project_ids)
    definition = workflow_definition()
    initialized = _service(uow, frozen_clock, definition, WorkflowEventIds()).initialize_workflow(
        _command(created, stored, definition, key="workflow:init:recovery")
    )
    current = uow.projects.get(initialized.project_id)
    assert current is not None
    assert initialized.workflow_state is not None
    replay = ReplayService(
        unit_of_work_factory=lambda: uow,
        workflow_definitions=WorkflowDefinitionRegistry((definition,)),
    )
    full = replay.replay_project(initialized.project_id)
    tail = uow.events.read_stream(initialized.project_id)[-1]
    snapshot = SnapshotService(
        unit_of_work_factory=lambda: uow,
        replay_service=replay,
        clock=frozen_clock,
        snapshot_ids=SnapshotIds(),
        policy=SnapshotPolicy(every_n_versions=1),
    ).create_snapshot(
        CreateSnapshotCommand(
            project_id=initialized.project_id,
            expected_record_version=current.record_version,
            expected_record_fingerprint=current.record_fingerprint,
            expected_content_fingerprint=current.content_fingerprint,
            expected_stream_position=tail.stream_position,
            expected_stream_fingerprint=tail.event_fingerprint,
            force=True,
        )
    )
    accelerated = replay.replay_project(initialized.project_id)
    assert snapshot.created
    assert accelerated.reconstructed_state == full.reconstructed_state

    state = ProjectState.model_validate_json(current.state_json, strict=True)
    divergent = state.model_copy(
        update={"metadata": state.metadata.model_copy(update={"name": "Divergent name"})}
    )
    uow.projects.save(
        divergent,
        expected_record_version=current.record_version,
        expected_record_fingerprint=current.record_fingerprint,
    )
    divergent_stored = uow.projects.get(initialized.project_id)
    assert divergent_stored is not None
    recovered = RecoveryService(
        unit_of_work_factory=lambda: uow,
        replay_service=replay,
        clock=frozen_clock,
        event_ids=RecoveryIds(),
    ).recover_aggregate(
        RecoverAggregateCommand(
            idempotency_key="workflow:recover:001",
            project_id=initialized.project_id,
            expected_record_version=divergent_stored.record_version,
            expected_record_fingerprint=divergent_stored.record_fingerprint,
            expected_content_fingerprint=divergent_stored.content_fingerprint,
            reason="Restore workflow-bearing aggregate from verified history.",
            actor_id="operator:workflow",
        )
    )
    assert recovered.recovered
    assert recovered.replay_result.reconstructed_state.workflow_registry[WORKFLOW_ID] == (
        initialized.workflow_state
    )
