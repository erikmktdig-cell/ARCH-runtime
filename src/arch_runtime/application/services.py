"""Synchronous R05 application service for project creation."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from arch_kernel.contracts import (
    EventActor,
    EventEnvelope,
    EventId,
    InvariantEvaluationTarget,
    LifecycleState,
    PhaseStatus,
    ProjectId,
    ProjectMetadata,
    ProjectProfile,
    ProjectState,
    SemanticVersion,
    TypedReference,
    ValidationIntent,
    ValidationPipelinePolicy,
    ValidationPipelineRequest,
    ValidationPipelineResult,
)
from arch_kernel.kernel import (
    InvariantRegistry,
    build_builtin_contract_registry,
    canonicalize_json,
    compute_content_fingerprint,
    compute_fingerprint,
    run_validation_pipeline,
)

from arch_runtime.application.commands import CreateProjectCommand
from arch_runtime.application.results import CreateProjectResult
from arch_runtime.errors import CorruptStoredRecordError, IdempotencyConflictError, PersistenceError
from arch_runtime.ports import (
    Clock,
    EventIdGenerator,
    IdempotencyRecord,
    IdempotencyStatus,
    ProjectIdGenerator,
    UnitOfWorkFactory,
)

_OPERATION = "project.create"


def _version() -> SemanticVersion:
    return SemanticVersion.parse("1.0.0")


class ValidationRunner(Protocol):
    def __call__(
        self,
        request: ValidationPipelineRequest,
        *,
        invariant_registry: InvariantRegistry | None = None,
    ) -> ValidationPipelineResult: ...


class CreateProjectService:
    def __init__(
        self,
        *,
        unit_of_work_factory: UnitOfWorkFactory,
        clock: Clock,
        project_ids: ProjectIdGenerator,
        event_ids: EventIdGenerator,
        invariant_registry: InvariantRegistry | None = None,
        validation_runner: ValidationRunner = run_validation_pipeline,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._clock = clock
        self._project_ids = project_ids
        self._event_ids = event_ids
        self._invariant_registry = invariant_registry
        self._validation_runner = validation_runner

    def create_project(self, command: CreateProjectCommand) -> CreateProjectResult:
        request_fingerprint = compute_fingerprint(command.request_payload())
        replay = self._find_completed(command, request_fingerprint)
        if replay is not None:
            return replay

        now = self._clock.now()
        project_id = self._project_ids.new()
        state = _build_initial_state(command, project_id, now)
        policy = (
            ValidationPipelinePolicy(invariant_target=InvariantEvaluationTarget.ORIGINAL)
            if self._invariant_registry is not None
            else ValidationPipelinePolicy()
        )
        validation = self._validation_runner(
            ValidationPipelineRequest(
                intent=ValidationIntent.VALIDATE_STATE,
                evaluated_at=now,
                state=state,
                policy=policy,
            ),
            invariant_registry=self._invariant_registry,
        )
        event_id = self._event_ids.new() if validation.success else None
        result = self._persist(
            command,
            request_fingerprint,
            project_id,
            state,
            validation,
            event_id,
            now,
        )
        if result.success:
            self._verify_committed(result, state)
        return result

    def _find_completed(
        self,
        command: CreateProjectCommand,
        request_fingerprint: str,
    ) -> CreateProjectResult | None:
        with self._unit_of_work_factory() as unit_of_work:
            existing = unit_of_work.idempotency.get(_OPERATION, command.idempotency_key)
            return _resolve_existing(existing, request_fingerprint)

    def _persist(
        self,
        command: CreateProjectCommand,
        request_fingerprint: str,
        project_id: ProjectId,
        state: ProjectState,
        validation: ValidationPipelineResult,
        event_id: EventId | None,
        now: datetime,
    ) -> CreateProjectResult:
        with self._unit_of_work_factory() as unit_of_work:
            existing = unit_of_work.idempotency.get(_OPERATION, command.idempotency_key)
            replay = _resolve_existing(existing, request_fingerprint)
            if replay is not None:
                return replay
            unit_of_work.idempotency.reserve(
                IdempotencyRecord(
                    operation_name=_OPERATION,
                    idempotency_key=command.idempotency_key,
                    project_id=project_id,
                    request_fingerprint=request_fingerprint,
                    status=IdempotencyStatus.IN_PROGRESS,
                    created_at=now,
                )
            )
            if not validation.success:
                result = CreateProjectResult(
                    success=False,
                    project_id=project_id,
                    validation_result=validation,
                )
            else:
                assert event_id is not None
                if unit_of_work.projects.get(project_id) is not None:
                    raise IdempotencyConflictError(
                        "generated project identity already exists",
                        operation=_OPERATION,
                        remediation="use a collision-resistant ProjectId generator",
                        project_id=project_id,
                    )
                unit_of_work.projects.add(state)
                event = _build_created_event(command, state, event_id, now)
                stream_position = unit_of_work.events.append(
                    event,
                    version_before=0,
                    version_after=state.record_version,
                    previous_event_fingerprint=None,
                )
                result = CreateProjectResult(
                    success=True,
                    project_id=project_id,
                    validation_result=validation,
                    event_id=event_id,
                    stream_position=stream_position,
                    record_version=state.record_version,
                    record_fingerprint=compute_fingerprint(state),
                    content_fingerprint=compute_content_fingerprint(state),
                )
            unit_of_work.idempotency.complete(
                _OPERATION,
                command.idempotency_key,
                result,
            )
            unit_of_work.commit()
            return result

    def _verify_committed(self, result: CreateProjectResult, state: ProjectState) -> None:
        with self._unit_of_work_factory() as unit_of_work:
            stored = unit_of_work.projects.get(result.project_id)
        if stored is None:
            raise CorruptStoredRecordError(
                "committed project could not be reloaded",
                operation="project.create.verify",
                remediation="preserve storage and inspect the committed transaction",
                project_id=result.project_id,
            )
        if (
            stored.state_json != canonicalize_json(state)
            or stored.record_version != result.record_version
            or stored.record_fingerprint != result.record_fingerprint
            or stored.content_fingerprint != result.content_fingerprint
        ):
            raise CorruptStoredRecordError(
                "committed project evidence does not match the creation result",
                operation="project.create.verify",
                remediation="preserve storage and inspect the committed transaction",
                project_id=result.project_id,
            )


def _resolve_existing(
    record: IdempotencyRecord | None,
    request_fingerprint: str,
) -> CreateProjectResult | None:
    if record is None:
        return None
    if record.request_fingerprint != request_fingerprint:
        raise IdempotencyConflictError(
            "idempotency key was used for a different create-project request",
            operation=_OPERATION,
            remediation="use the original request or a new idempotency key",
            project_id=record.project_id,
        )
    if record.status is not IdempotencyStatus.COMPLETED:
        raise PersistenceError(
            "idempotency reservation is incomplete",
            operation=_OPERATION,
            remediation="retry after the active transaction has completed",
            project_id=record.project_id,
        )
    assert record.result_json is not None
    assert record.result_fingerprint is not None
    try:
        result = CreateProjectResult.model_validate_json(record.result_json, strict=True)
    except ValueError as error:
        raise CorruptStoredRecordError(
            "stored create-project result is invalid",
            operation=_OPERATION,
            remediation="preserve storage and inspect idempotency evidence",
            project_id=record.project_id,
        ) from error
    if (
        canonicalize_json(result) != record.result_json
        or compute_fingerprint(result) != record.result_fingerprint
    ):
        raise CorruptStoredRecordError(
            "stored create-project result evidence is corrupt",
            operation=_OPERATION,
            remediation="preserve storage and inspect idempotency evidence",
            project_id=record.project_id,
        )
    return result


def _build_initial_state(
    command: CreateProjectCommand,
    project_id: ProjectId,
    now: datetime,
) -> ProjectState:
    metadata = ProjectMetadata(
        contract_name="project_metadata",
        contract_version=_version(),
        schema_uri="urn:arch:contracts:project_metadata:1.0.0",
        created_at=now,
        updated_at=now,
        project_id=project_id,
        name=command.name,
        slug=command.slug,
        summary=command.summary,
        owner=command.owner,
    )
    profile = ProjectProfile(
        contract_name="project_profile",
        contract_version=_version(),
        schema_uri="urn:arch:contracts:project_profile:1.0.0",
        created_at=now,
        updated_at=now,
        project_type=command.project_type,
        criticality=command.criticality,
        default_route=command.default_route,
        objectives=command.objectives,
        constraints=command.constraints,
        success_criteria=command.success_criteria,
    )
    lifecycle = LifecycleState(
        contract_name="lifecycle_state",
        contract_version=_version(),
        schema_uri="urn:arch:contracts:lifecycle_state:1.0.0",
        created_at=now,
        updated_at=now,
        current_phase_id=None,
        current_phase_code=None,
        status=PhaseStatus.NOT_STARTED,
    )
    return ProjectState(
        contract_name="project_state",
        contract_version=_version(),
        schema_uri="urn:arch:contracts:project_state:1.0.0",
        created_at=now,
        updated_at=now,
        record_version=1,
        state_version_created=0,
        state_version_updated=1,
        metadata=metadata,
        profile=profile,
        lifecycle=lifecycle,
    )


def _build_created_event(
    command: CreateProjectCommand,
    state: ProjectState,
    event_id: EventId,
    now: datetime,
) -> EventEnvelope:
    actor = EventActor(
        contract_name="event_actor",
        contract_version=_version(),
        schema_uri="urn:arch:contracts:event_actor:1.0.0",
        created_at=now,
        updated_at=None,
        actor_type=command.actor_type,
        actor_id=command.actor_id,
        display_name=command.actor_display_name,
    )
    aggregate = TypedReference(
        target_type="project",
        target_id=state.metadata.project_id,
        target_version=state.contract_version,
        relation="describes",
    )
    project_contract = build_builtin_contract_registry().get_by_model(ProjectState)
    if project_contract is None:
        raise PersistenceError(
            "ProjectState contract registration is unavailable",
            operation="project.create.event",
            remediation="install a compatible arch-kernel release",
            project_id=state.metadata.project_id,
        )
    return EventEnvelope.model_validate(
        {
            "contract_name": "event_envelope",
            "contract_version": _version(),
            "schema_uri": "urn:arch:contracts:event_envelope:1.0.0",
            "created_at": now,
            "updated_at": None,
            "event_id": event_id,
            "event_type": "project.aggregate.created",
            "project_id": state.metadata.project_id,
            "aggregate": aggregate,
            "occurred_at": now,
            "recorded_at": now,
            "actor": actor,
            "correlation_id": None,
            "causation_id": None,
            "idempotency_key": command.idempotency_key,
            "schema_version": _version(),
            "payload": {
                "event_name": "project.created",
                "state": state.model_dump(mode="json"),
                "record_fingerprint": compute_fingerprint(state),
                "content_fingerprint": compute_content_fingerprint(state),
                "contract_name": state.contract_name,
                "contract_version": str(state.contract_version),
                "schema_fingerprint": project_contract.descriptor.schema_fingerprint,
            },
        }
    )


def create_project(
    service: CreateProjectService, command: CreateProjectCommand
) -> CreateProjectResult:
    """Functional synchronous entry point for callers preferring a function."""

    return service.create_project(command)
