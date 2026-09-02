"""Authoritative initialization of configured generic workflows."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from arch_kernel.contracts import (
    EventEnvelope,
    EventId,
    InvariantEvaluationTarget,
    ProjectState,
    ValidationIntent,
    ValidationPipelinePolicy,
    ValidationPipelineRequest,
    ValidationPipelineResult,
    WorkflowDefinition,
    WorkflowStateRecord,
)
from arch_kernel.kernel import (
    InvariantRegistry,
    TransitionRegistry,
    WorkflowDefinitionRegistry,
    compute_content_fingerprint,
    compute_fingerprint,
    initialize_workflow_state,
    run_validation_pipeline,
)

from arch_runtime.application.commands import InitializeWorkflowCommand
from arch_runtime.application.events import build_project_event
from arch_runtime.application.evidence import verify_project_evidence
from arch_runtime.application.idempotency import resolve_completed_result
from arch_runtime.application.results import (
    InitializeWorkflowResult,
    WorkflowInitializationDisposition,
)
from arch_runtime.errors import (
    ConcurrentModificationError,
    CorruptStoredRecordError,
    ProjectNotFoundError,
)
from arch_runtime.ports import (
    Clock,
    EventIdGenerator,
    IdempotencyRecord,
    IdempotencyStatus,
    StoredProject,
    UnitOfWorkFactory,
)

_OPERATION = "project.workflow.initialize"


class WorkflowValidationRunner(Protocol):
    def __call__(
        self,
        request: ValidationPipelineRequest,
        *,
        transition_registry: TransitionRegistry | None = None,
        invariant_registry: InvariantRegistry | None = None,
    ) -> ValidationPipelineResult: ...


class InitializeWorkflowService:
    """Resolve configured workflow authority and commit one initialization atomically."""

    def __init__(
        self,
        *,
        unit_of_work_factory: UnitOfWorkFactory,
        clock: Clock,
        event_ids: EventIdGenerator,
        workflow_definitions: WorkflowDefinitionRegistry,
        invariant_registry: InvariantRegistry | None = None,
        validation_runner: WorkflowValidationRunner = run_validation_pipeline,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._clock = clock
        self._event_ids = event_ids
        self._definitions = workflow_definitions
        self._invariants = invariant_registry
        self._validation_runner = validation_runner

    def initialize_workflow(self, command: InitializeWorkflowCommand) -> InitializeWorkflowResult:
        request_fingerprint = compute_fingerprint(command.request_payload())
        replay, stored = self._load(command, request_fingerprint)
        if replay is not None:
            return replay
        assert stored is not None
        _require_expected_evidence(command, stored, operation="project.workflow.initialize.load")
        state = _decode_state(stored, command)
        _require_configured_bindings(state, self._definitions, command)
        definition, disposition = _resolve_definition(command, self._definitions)
        validation: ValidationPipelineResult | None = None
        projected: ProjectState | None = None
        workflow_state: WorkflowStateRecord | None = None

        if definition is not None:
            existing = state.workflow_registry.get(command.workflow_id)
            if existing is not None:
                if _record_matches_definition(existing, definition):
                    disposition = WorkflowInitializationDisposition.ALREADY_INITIALIZED
                    projected = state
                    workflow_state = existing
                else:
                    disposition = WorkflowInitializationDisposition.CONFLICTING_WORKFLOW_IDENTITY
            elif any(
                record.namespace == command.workflow_namespace
                for record in state.workflow_registry.values()
            ):
                disposition = WorkflowInitializationDisposition.NAMESPACE_ALREADY_INITIALIZED
            else:
                workflow_state = initialize_workflow_state(
                    definition,
                    created_at=self._clock.now(),
                    state_version_created=state.record_version + 1,
                )
                projected = _project_initialized_state(state, workflow_state)
                validation = self._validate(projected, workflow_state.created_at)
                if validation.success:
                    disposition = WorkflowInitializationDisposition.INITIALIZED
                else:
                    disposition = WorkflowInitializationDisposition.VALIDATION_REJECTED
                    projected = None
                    workflow_state = None

        changed = disposition is WorkflowInitializationDisposition.INITIALIZED
        success = disposition in {
            WorkflowInitializationDisposition.INITIALIZED,
            WorkflowInitializationDisposition.ALREADY_INITIALIZED,
        }
        if success and validation is None:
            validation = self._validate(state, self._clock.now())
        now = self._clock.now()
        event_id = self._event_ids.new() if changed else None
        result = self._persist(
            command,
            request_fingerprint,
            stored,
            definition,
            workflow_state,
            validation,
            projected,
            disposition,
            success,
            changed,
            event_id,
            now,
        )
        if result.success:
            assert projected is not None
            self._verify_committed(result, projected)
        return result

    def _validate(self, state: ProjectState, evaluated_at: datetime) -> ValidationPipelineResult:
        policy = (
            ValidationPipelinePolicy(invariant_target=InvariantEvaluationTarget.ORIGINAL)
            if self._invariants is not None
            else ValidationPipelinePolicy()
        )
        return self._validation_runner(
            ValidationPipelineRequest(
                intent=ValidationIntent.VALIDATE_STATE,
                evaluated_at=evaluated_at,
                state=state,
                policy=policy,
            ),
            invariant_registry=self._invariants,
        )

    def _load(
        self,
        command: InitializeWorkflowCommand,
        request_fingerprint: str,
    ) -> tuple[InitializeWorkflowResult | None, StoredProject | None]:
        with self._unit_of_work_factory() as unit_of_work:
            completed = resolve_completed_result(
                unit_of_work.idempotency.get(_OPERATION, command.idempotency_key),
                request_fingerprint,
                InitializeWorkflowResult,
                operation=_OPERATION,
            )
            if completed is not None:
                return completed, None
            stored = unit_of_work.projects.get(command.project_id)
        if stored is None:
            raise ProjectNotFoundError(
                "project not found",
                operation="project.workflow.initialize.load",
                remediation="create or explicitly migrate the project first",
                project_id=command.project_id,
            )
        return None, stored

    def _persist(
        self,
        command: InitializeWorkflowCommand,
        request_fingerprint: str,
        before: StoredProject,
        definition: WorkflowDefinition | None,
        workflow_state: WorkflowStateRecord | None,
        validation: ValidationPipelineResult | None,
        projected: ProjectState | None,
        disposition: WorkflowInitializationDisposition,
        success: bool,
        changed: bool,
        event_id: EventId | None,
        now: datetime,
    ) -> InitializeWorkflowResult:
        with self._unit_of_work_factory() as unit_of_work:
            completed = resolve_completed_result(
                unit_of_work.idempotency.get(_OPERATION, command.idempotency_key),
                request_fingerprint,
                InitializeWorkflowResult,
                operation=_OPERATION,
            )
            if completed is not None:
                return completed
            unit_of_work.idempotency.reserve(
                IdempotencyRecord(
                    operation_name=_OPERATION,
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
                    "project disappeared before workflow initialization commit",
                    operation="project.workflow.initialize.commit",
                    remediation="reload the project and retry",
                    project_id=command.project_id,
                )
            _require_same_evidence(before, current, command)

            position: int | None = None
            if changed:
                assert definition is not None
                assert workflow_state is not None
                assert validation is not None
                assert projected is not None
                assert event_id is not None
                unit_of_work.projects.save(
                    projected,
                    expected_record_version=before.record_version,
                    expected_record_fingerprint=before.record_fingerprint,
                )
                stream = unit_of_work.events.read_stream(command.project_id)
                if not stream:
                    raise CorruptStoredRecordError(
                        "project event stream is empty",
                        operation="project.workflow.initialize.commit",
                        remediation="preserve storage and inspect project history",
                        project_id=command.project_id,
                    )
                event = _build_initialized_event(
                    command,
                    definition,
                    workflow_state,
                    validation,
                    before,
                    projected,
                    event_id,
                    now,
                )
                position = unit_of_work.events.append(
                    event,
                    version_before=before.record_version,
                    version_after=projected.record_version,
                    previous_event_fingerprint=stream[-1].event_fingerprint,
                )
            result = _result(
                command,
                before,
                workflow_state,
                validation,
                projected,
                disposition,
                success,
                changed,
                event_id=event_id,
                stream_position=position,
            )
            unit_of_work.idempotency.complete(_OPERATION, command.idempotency_key, result)
            unit_of_work.commit()
            return result

    def _verify_committed(self, result: InitializeWorkflowResult, projected: ProjectState) -> None:
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
            operation="project.workflow.initialize.verify",
        )


def _resolve_definition(
    command: InitializeWorkflowCommand,
    definitions: WorkflowDefinitionRegistry,
) -> tuple[WorkflowDefinition | None, WorkflowInitializationDisposition]:
    definition = definitions.get(command.workflow_namespace, command.definition_version)
    if definition is None:
        return None, WorkflowInitializationDisposition.UNKNOWN_DEFINITION
    if (
        definition.workflow_id != command.workflow_id
        or definition.definition_fingerprint() != command.definition_fingerprint
    ):
        return None, WorkflowInitializationDisposition.DEFINITION_MISMATCH
    return definition, WorkflowInitializationDisposition.INITIALIZED


def _record_matches_definition(record: WorkflowStateRecord, definition: WorkflowDefinition) -> bool:
    return (
        record.workflow_id == definition.workflow_id
        and record.namespace == definition.namespace
        and record.definition_version == definition.definition_version
        and record.definition_fingerprint == definition.definition_fingerprint()
        and record.current_state in definition.allowed_states
    )


def _require_configured_bindings(
    state: ProjectState,
    definitions: WorkflowDefinitionRegistry,
    command: InitializeWorkflowCommand,
) -> None:
    for record in state.workflow_registry.values():
        definition = definitions.get(record.namespace, record.definition_version)
        if definition is None or not _record_matches_definition(record, definition):
            raise CorruptStoredRecordError(
                "persisted workflow state disagrees with configured authority",
                operation="project.workflow.initialize.load",
                remediation="preserve storage and restore the approved workflow registry",
                project_id=command.project_id,
            )


def _decode_state(stored: StoredProject, command: InitializeWorkflowCommand) -> ProjectState:
    try:
        return ProjectState.model_validate_json(stored.state_json, strict=True)
    except ValueError as error:
        raise CorruptStoredRecordError(
            "stored project state cannot be reconstructed",
            operation="project.workflow.initialize.load",
            remediation="explicitly migrate compatible historical state before initialization",
            project_id=command.project_id,
        ) from error


def _project_initialized_state(
    state: ProjectState, workflow_state: WorkflowStateRecord
) -> ProjectState:
    workflows = dict(state.workflow_registry)
    workflows[workflow_state.workflow_id] = workflow_state
    return state.model_copy(
        update={
            "updated_at": workflow_state.created_at,
            "record_version": state.record_version + 1,
            "state_version_updated": state.state_version_updated + 1,
            "workflow_registry": workflows,
        }
    )


def _require_expected_evidence(
    command: InitializeWorkflowCommand, stored: StoredProject, *, operation: str
) -> None:
    if (
        stored.record_version != command.expected_record_version
        or stored.record_fingerprint != command.expected_record_fingerprint
        or stored.content_fingerprint != command.expected_content_fingerprint
    ):
        raise ConcurrentModificationError(
            "project evidence differs from workflow initialization preconditions",
            operation=operation,
            remediation="reload the project and retry with current evidence",
            project_id=command.project_id,
        )


def _require_same_evidence(
    before: StoredProject,
    current: StoredProject,
    command: InitializeWorkflowCommand,
) -> None:
    _require_expected_evidence(command, current, operation="project.workflow.initialize.commit")
    if (
        current.record_version != before.record_version
        or current.record_fingerprint != before.record_fingerprint
        or current.content_fingerprint != before.content_fingerprint
    ):
        raise ConcurrentModificationError(
            "project changed during workflow initialization evaluation",
            operation="project.workflow.initialize.commit",
            remediation="reload the project and reevaluate initialization",
            project_id=command.project_id,
        )


def _result(
    command: InitializeWorkflowCommand,
    before: StoredProject,
    workflow_state: WorkflowStateRecord | None,
    validation: ValidationPipelineResult | None,
    projected: ProjectState | None,
    disposition: WorkflowInitializationDisposition,
    success: bool,
    changed: bool,
    *,
    event_id: EventId | None = None,
    stream_position: int | None = None,
) -> InitializeWorkflowResult:
    return InitializeWorkflowResult(
        success=success,
        changed=changed,
        disposition=disposition,
        project_id=command.project_id,
        workflow_id=command.workflow_id,
        workflow_state=workflow_state if success else None,
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


def _build_initialized_event(
    command: InitializeWorkflowCommand,
    definition: WorkflowDefinition,
    workflow_state: WorkflowStateRecord,
    validation: ValidationPipelineResult,
    before: StoredProject,
    projected: ProjectState,
    event_id: EventId,
    now: datetime,
) -> EventEnvelope:
    return build_project_event(
        event_id=event_id,
        event_type="project.aggregate.workflow_initialized",
        semantic_name="project.workflow_initialized",
        project_id=command.project_id,
        aggregate_version=projected.contract_version,
        idempotency_key=command.idempotency_key,
        actor_type=command.actor_type,
        actor_id=command.actor_id,
        actor_display_name=command.actor_display_name,
        occurred_at=now,
        payload={
            "workflow_definition": definition.model_dump(mode="json"),
            "workflow_state": workflow_state.model_dump(mode="json"),
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
        },
    )


def initialize_workflow(
    service: InitializeWorkflowService, command: InitializeWorkflowCommand
) -> InitializeWorkflowResult:
    """Functional synchronous entry point for workflow initialization."""

    return service.initialize_workflow(command)


__all__ = ("InitializeWorkflowService", "initialize_workflow")
