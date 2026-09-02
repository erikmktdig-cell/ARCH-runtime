"""Synchronous R05-R06 application services for project mutations."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from arch_kernel.contracts import (
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
    TransitionRequest,
    ValidationIntent,
    ValidationPipelinePolicy,
    ValidationPipelineRequest,
    ValidationPipelineResult,
)
from arch_kernel.kernel import (
    InvariantRegistry,
    TransitionRegistry,
    build_builtin_contract_registry,
    compute_content_fingerprint,
    compute_fingerprint,
    run_validation_pipeline,
)

from arch_runtime.application.commands import ApplyTransitionCommand, CreateProjectCommand
from arch_runtime.application.events import build_project_event, runtime_contract_version
from arch_runtime.application.evidence import verify_project_evidence
from arch_runtime.application.idempotency import resolve_completed_result
from arch_runtime.application.results import ApplyTransitionResult, CreateProjectResult
from arch_runtime.errors import (
    ConcurrentModificationError,
    CorruptStoredRecordError,
    IdempotencyConflictError,
    PersistenceError,
    ProjectNotFoundError,
)
from arch_runtime.ports import (
    Clock,
    EventIdGenerator,
    IdempotencyRecord,
    IdempotencyStatus,
    ProjectIdGenerator,
    StoredProject,
    UnitOfWorkFactory,
)

_OPERATION = "project.create"
_TRANSITION_OPERATION = "project.transition"


def _version() -> SemanticVersion:
    return runtime_contract_version()


class ValidationRunner(Protocol):
    def __call__(
        self,
        request: ValidationPipelineRequest,
        *,
        transition_registry: TransitionRegistry | None = None,
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
            return resolve_completed_result(
                existing,
                request_fingerprint,
                CreateProjectResult,
                operation=_OPERATION,
            )

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
            replay = resolve_completed_result(
                existing,
                request_fingerprint,
                CreateProjectResult,
                operation=_OPERATION,
            )
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
        assert result.record_version is not None
        assert result.record_fingerprint is not None
        assert result.content_fingerprint is not None
        verify_project_evidence(
            stored,
            state,
            expected_record_version=result.record_version,
            expected_record_fingerprint=result.record_fingerprint,
            expected_content_fingerprint=result.content_fingerprint,
            operation="project.create.verify",
        )


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
        contract_version=SemanticVersion.parse("2.0.0"),
        schema_uri="urn:arch:contracts:project_state:2.0.0",
        created_at=now,
        updated_at=now,
        record_version=1,
        state_version_created=0,
        state_version_updated=1,
        metadata=metadata,
        profile=profile,
        lifecycle=lifecycle,
        workflow_registry={},
    )


def _build_created_event(
    command: CreateProjectCommand,
    state: ProjectState,
    event_id: EventId,
    now: datetime,
) -> EventEnvelope:
    project_contract = build_builtin_contract_registry().get_by_model(ProjectState)
    if project_contract is None:
        raise PersistenceError(
            "ProjectState contract registration is unavailable",
            operation="project.create.event",
            remediation="install a compatible arch-kernel release",
            project_id=state.metadata.project_id,
        )
    return build_project_event(
        event_id=event_id,
        event_type="project.aggregate.created",
        semantic_name="project.created",
        project_id=state.metadata.project_id,
        aggregate_version=state.contract_version,
        idempotency_key=command.idempotency_key,
        actor_type=command.actor_type,
        actor_id=command.actor_id,
        actor_display_name=command.actor_display_name,
        occurred_at=now,
        payload={
            "state": state.model_dump(mode="json"),
            "record_fingerprint": compute_fingerprint(state),
            "content_fingerprint": compute_content_fingerprint(state),
            "contract_name": state.contract_name,
            "contract_version": str(state.contract_version),
            "schema_fingerprint": project_contract.descriptor.schema_fingerprint,
        },
    )


class ApplyTransitionService:
    """Evaluate K08 transitions outside writes and commit verified evidence atomically."""

    def __init__(
        self,
        *,
        unit_of_work_factory: UnitOfWorkFactory,
        clock: Clock,
        event_ids: EventIdGenerator,
        transition_registry: TransitionRegistry,
        invariant_registry: InvariantRegistry | None = None,
        validation_runner: ValidationRunner = run_validation_pipeline,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._clock = clock
        self._event_ids = event_ids
        self._transition_registry = transition_registry
        self._invariant_registry = invariant_registry
        self._validation_runner = validation_runner

    def apply_transition(self, command: ApplyTransitionCommand) -> ApplyTransitionResult:
        request_fingerprint = compute_fingerprint(command.request_payload())
        replay, stored = self._load(command, request_fingerprint)
        if replay is not None:
            return replay
        assert stored is not None
        self._require_expected_evidence(command, stored)
        try:
            state = ProjectState.model_validate_json(stored.state_json, strict=True)
        except ValueError as error:
            raise CorruptStoredRecordError(
                "stored project state cannot be reconstructed",
                operation="project.transition.load",
                remediation="preserve storage and inspect aggregate evidence",
                project_id=command.project_id,
            ) from error

        now = self._clock.now()
        transition_request = _build_transition_request(command, now)
        validation = self._validation_runner(
            ValidationPipelineRequest(
                intent=ValidationIntent.VALIDATE_TRANSITION,
                evaluated_at=now,
                state=state,
                transition_request=transition_request,
            ),
            transition_registry=self._transition_registry,
            invariant_registry=self._invariant_registry,
        )
        projected = validation.projected_state if validation.success else None
        changed = (
            projected is not None and compute_fingerprint(projected) != stored.record_fingerprint
        )
        event_id = self._event_ids.new() if changed else None
        result = self._persist(
            command,
            request_fingerprint,
            stored,
            transition_request,
            validation,
            projected,
            changed,
            event_id,
            now,
        )
        if result.success:
            assert projected is not None
            self._verify_committed(result, projected)
        return result

    def _load(
        self,
        command: ApplyTransitionCommand,
        request_fingerprint: str,
    ) -> tuple[ApplyTransitionResult | None, StoredProject | None]:
        with self._unit_of_work_factory() as unit_of_work:
            existing = unit_of_work.idempotency.get(_TRANSITION_OPERATION, command.idempotency_key)
            replay = resolve_completed_result(
                existing,
                request_fingerprint,
                ApplyTransitionResult,
                operation=_TRANSITION_OPERATION,
            )
            if replay is not None:
                return replay, None
            stored = unit_of_work.projects.get(command.project_id)
        if stored is None:
            raise ProjectNotFoundError(
                "project not found",
                operation="project.transition.load",
                remediation="create the project before applying transitions",
                project_id=command.project_id,
            )
        return None, stored

    @staticmethod
    def _require_expected_evidence(command: ApplyTransitionCommand, stored: StoredProject) -> None:
        if (
            stored.record_version != command.expected_record_version
            or stored.record_fingerprint != command.expected_record_fingerprint
            or stored.content_fingerprint != command.expected_content_fingerprint
        ):
            raise ConcurrentModificationError(
                "project evidence differs from transition preconditions",
                operation="project.transition.precondition",
                remediation="reload the project and reevaluate the transition",
                project_id=command.project_id,
            )

    def _persist(
        self,
        command: ApplyTransitionCommand,
        request_fingerprint: str,
        before: StoredProject,
        transition_request: TransitionRequest,
        validation: ValidationPipelineResult,
        projected: ProjectState | None,
        changed: bool,
        event_id: EventId | None,
        now: datetime,
    ) -> ApplyTransitionResult:
        with self._unit_of_work_factory() as unit_of_work:
            existing = unit_of_work.idempotency.get(_TRANSITION_OPERATION, command.idempotency_key)
            replay = resolve_completed_result(
                existing,
                request_fingerprint,
                ApplyTransitionResult,
                operation=_TRANSITION_OPERATION,
            )
            if replay is not None:
                return replay
            unit_of_work.idempotency.reserve(
                IdempotencyRecord(
                    operation_name=_TRANSITION_OPERATION,
                    idempotency_key=command.idempotency_key,
                    project_id=command.project_id,
                    request_fingerprint=request_fingerprint,
                    status=IdempotencyStatus.IN_PROGRESS,
                    created_at=now,
                )
            )
            current = unit_of_work.projects.get(command.project_id)
            if current is None:
                raise ProjectNotFoundError(
                    "project disappeared before transition commit",
                    operation="project.transition.commit",
                    remediation="reload the project and retry",
                    project_id=command.project_id,
                )
            if (
                current.record_version != before.record_version
                or current.record_fingerprint != before.record_fingerprint
            ):
                raise ConcurrentModificationError(
                    "project changed during transition evaluation",
                    operation="project.transition.commit",
                    remediation="reload the project and reevaluate the transition",
                    project_id=command.project_id,
                )

            if not validation.success:
                result = _transition_result(command, before, validation, None, False)
            else:
                assert projected is not None
                if changed:
                    assert event_id is not None
                    unit_of_work.projects.save(
                        projected,
                        expected_record_version=before.record_version,
                        expected_record_fingerprint=before.record_fingerprint,
                    )
                    stream = unit_of_work.events.read_stream(command.project_id)
                    previous = stream[-1].event_fingerprint if stream else None
                    event = _build_transition_event(
                        command,
                        transition_request,
                        validation,
                        before,
                        projected,
                        event_id,
                        now,
                        self._transition_registry,
                        self._invariant_registry,
                    )
                    position = unit_of_work.events.append(
                        event,
                        version_before=before.record_version,
                        version_after=projected.record_version,
                        previous_event_fingerprint=previous,
                    )
                    result = _transition_result(
                        command,
                        before,
                        validation,
                        projected,
                        True,
                        event_id=event_id,
                        stream_position=position,
                    )
                else:
                    result = _transition_result(command, before, validation, projected, False)
            unit_of_work.idempotency.complete(
                _TRANSITION_OPERATION, command.idempotency_key, result
            )
            unit_of_work.commit()
            return result

    def _verify_committed(self, result: ApplyTransitionResult, projected: ProjectState) -> None:
        with self._unit_of_work_factory() as unit_of_work:
            stored = unit_of_work.projects.get(result.project_id)
        assert result.after_record_version is not None
        assert result.after_record_fingerprint is not None
        assert result.after_content_fingerprint is not None
        verify_project_evidence(
            stored,
            projected,
            expected_record_version=result.after_record_version,
            expected_record_fingerprint=result.after_record_fingerprint,
            expected_content_fingerprint=result.after_content_fingerprint,
            operation="project.transition.verify",
        )


def _build_transition_request(command: ApplyTransitionCommand, now: datetime) -> TransitionRequest:
    return TransitionRequest(
        request_id=command.request_id,
        project_id=command.project_id,
        target=command.target,
        transition_id=command.transition_id,
        transition_key=command.transition_key,
        expected_from_state=command.expected_from_state,
        base_state_record_version=command.expected_record_version,
        expected_state_content_fingerprint=command.expected_content_fingerprint,
        expected_target_record_version=command.expected_target_record_version,
        expected_target_content_fingerprint=command.expected_target_content_fingerprint,
        requested_at=now,
        metadata=command.metadata,
    )


def _transition_result(
    command: ApplyTransitionCommand,
    before: StoredProject,
    validation: ValidationPipelineResult,
    projected: ProjectState | None,
    changed: bool,
    *,
    event_id: EventId | None = None,
    stream_position: int | None = None,
) -> ApplyTransitionResult:
    return ApplyTransitionResult(
        success=validation.success,
        changed=changed,
        project_id=command.project_id,
        validation_result=validation,
        before_record_version=before.record_version,
        before_record_fingerprint=before.record_fingerprint,
        before_content_fingerprint=before.content_fingerprint,
        after_record_version=None if projected is None else projected.record_version,
        after_record_fingerprint=None if projected is None else compute_fingerprint(projected),
        after_content_fingerprint=(
            None if projected is None else compute_content_fingerprint(projected)
        ),
        event_id=event_id,
        stream_position=stream_position,
    )


def _build_transition_event(
    command: ApplyTransitionCommand,
    transition_request: TransitionRequest,
    validation: ValidationPipelineResult,
    before: StoredProject,
    projected: ProjectState,
    event_id: EventId,
    now: datetime,
    transition_registry: TransitionRegistry,
    invariant_registry: InvariantRegistry | None,
) -> EventEnvelope:
    return build_project_event(
        event_id=event_id,
        event_type="project.aggregate.transition_applied",
        semantic_name="project.transition_applied",
        project_id=command.project_id,
        aggregate_version=projected.contract_version,
        idempotency_key=command.idempotency_key,
        actor_type=command.actor_type,
        actor_id=command.actor_id,
        actor_display_name=command.actor_display_name,
        occurred_at=now,
        payload={
            "transition_request": transition_request.model_dump(mode="json"),
            "patch": None
            if validation.generated_patch is None
            else validation.generated_patch.model_dump(mode="json"),
            "validation_result": validation.model_dump(mode="json"),
            "before": {
                "record_version": before.record_version,
                "record_fingerprint": before.record_fingerprint,
                "content_fingerprint": before.content_fingerprint,
            },
            "after": {
                "record_version": projected.record_version,
                "record_fingerprint": compute_fingerprint(projected),
                "content_fingerprint": compute_content_fingerprint(projected),
            },
            "transition_registry_fingerprint": compute_fingerprint(transition_registry.definitions),
            "invariant_registry_fingerprint": None
            if invariant_registry is None
            else compute_fingerprint(invariant_registry.definitions),
        },
    )


def create_project(
    service: CreateProjectService, command: CreateProjectCommand
) -> CreateProjectResult:
    """Functional synchronous entry point for callers preferring a function."""

    return service.create_project(command)


def apply_transition(
    service: ApplyTransitionService, command: ApplyTransitionCommand
) -> ApplyTransitionResult:
    """Functional synchronous entry point for transition callers."""

    return service.apply_transition(command)
