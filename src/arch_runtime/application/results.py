"""Strict immutable application results."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from arch_kernel.contracts import (
    EventId,
    FrozenJsonObject,
    MigrationResult,
    ProjectId,
    ProjectState,
    SemanticVersion,
    ValidationPipelineResult,
    WorkflowId,
    WorkflowStateRecord,
)
from arch_kernel.kernel import compute_content_fingerprint, compute_fingerprint
from pydantic import BaseModel, ConfigDict, Field, model_validator

from arch_runtime.ports.storage import Fingerprint, SnapshotId
from arch_runtime.replay.contracts import ReplayResult


class CreateProjectResult(BaseModel):
    """Canonical logical result stored for idempotent project creation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_name: Literal["arch.runtime.result.create_project"] = (
        "arch.runtime.result.create_project"
    )
    contract_version: Literal["1.0.0"] = "1.0.0"
    success: bool
    project_id: ProjectId
    validation_result: ValidationPipelineResult
    event_id: EventId | None = None
    stream_position: Annotated[int, Field(ge=1)] | None = None
    record_version: Annotated[int, Field(ge=1)] | None = None
    record_fingerprint: Fingerprint | None = None
    content_fingerprint: Fingerprint | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        commit_evidence = (
            self.event_id,
            self.stream_position,
            self.record_version,
            self.record_fingerprint,
            self.content_fingerprint,
        )
        if self.success and any(value is None for value in commit_evidence):
            raise ValueError("successful creation requires complete commit evidence")
        if not self.success and any(value is not None for value in commit_evidence):
            raise ValueError("rejected creation cannot contain commit evidence")
        if self.success != self.validation_result.success:
            raise ValueError("application and K08 success values must agree")
        return self


class GetProjectResult(BaseModel):
    """Verified public projection of one operational aggregate."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_name: Literal["arch.runtime.result.get_project"] = "arch.runtime.result.get_project"
    contract_version: Literal["1.0.0"] = "1.0.0"
    project_id: ProjectId
    state: ProjectState
    record_version: Annotated[int, Field(ge=1)]
    record_fingerprint: Fingerprint
    content_fingerprint: Fingerprint
    stream_position: Annotated[int, Field(ge=1)]
    stream_fingerprint: Fingerprint

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if self.state.metadata.project_id != self.project_id:
            raise ValueError("project state identity must match the requested project")
        if self.state.record_version != self.record_version:
            raise ValueError("project state version must match stored evidence")
        if compute_fingerprint(self.state) != self.record_fingerprint:
            raise ValueError("project state fingerprint must match stored evidence")
        if compute_content_fingerprint(self.state) != self.content_fingerprint:
            raise ValueError("project content fingerprint must match stored evidence")
        return self


class ApplyTransitionResult(BaseModel):
    """Canonical idempotent result of one transition application."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_name: Literal["arch.runtime.result.apply_transition"] = (
        "arch.runtime.result.apply_transition"
    )
    contract_version: Literal["1.0.0"] = "1.0.0"
    success: bool
    changed: bool
    project_id: ProjectId
    validation_result: ValidationPipelineResult
    before_record_version: Annotated[int, Field(ge=1)]
    before_record_fingerprint: Fingerprint
    before_content_fingerprint: Fingerprint
    after_record_version: Annotated[int, Field(ge=1)] | None = None
    after_record_fingerprint: Fingerprint | None = None
    after_content_fingerprint: Fingerprint | None = None
    event_id: EventId | None = None
    stream_position: Annotated[int, Field(ge=1)] | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        after = (
            self.after_record_version,
            self.after_record_fingerprint,
            self.after_content_fingerprint,
        )
        event = (self.event_id, self.stream_position)
        if self.success != self.validation_result.success:
            raise ValueError("application and K08 success values must agree")
        if not self.success:
            if self.changed or any(value is not None for value in (*after, *event)):
                raise ValueError("rejected transition cannot contain commit evidence")
            return self
        if any(value is None for value in after):
            raise ValueError("successful transition requires after-state evidence")
        if self.changed:
            if any(value is None for value in event):
                raise ValueError("changed transition requires event evidence")
            if self.after_record_version != self.before_record_version + 1:
                raise ValueError("changed transition must increment the aggregate once")
        else:
            if any(value is not None for value in event):
                raise ValueError("no-op transition cannot contain event evidence")
            if after != (
                self.before_record_version,
                self.before_record_fingerprint,
                self.before_content_fingerprint,
            ):
                raise ValueError("no-op transition must preserve aggregate evidence")
        return self


class WorkflowInitializationDisposition(StrEnum):
    """Closed outcomes for authoritative workflow initialization."""

    INITIALIZED = "initialized"
    ALREADY_INITIALIZED = "already_initialized"
    UNKNOWN_DEFINITION = "unknown_definition"
    DEFINITION_MISMATCH = "definition_mismatch"
    CONFLICTING_WORKFLOW_IDENTITY = "conflicting_workflow_identity"
    NAMESPACE_ALREADY_INITIALIZED = "namespace_already_initialized"
    VALIDATION_REJECTED = "validation_rejected"


class InitializeWorkflowResult(BaseModel):
    """Canonical idempotent result of one workflow initialization request."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_name: Literal["arch.runtime.result.initialize_workflow"] = (
        "arch.runtime.result.initialize_workflow"
    )
    contract_version: Literal["1.0.0"] = "1.0.0"
    success: bool
    changed: bool
    disposition: WorkflowInitializationDisposition
    project_id: ProjectId
    workflow_id: WorkflowId
    workflow_state: WorkflowStateRecord | None = None
    validation_result: ValidationPipelineResult | None = None
    before_record_version: Annotated[int, Field(ge=1)]
    before_record_fingerprint: Fingerprint
    before_content_fingerprint: Fingerprint
    after_record_version: Annotated[int, Field(ge=1)] | None = None
    after_record_fingerprint: Fingerprint | None = None
    after_content_fingerprint: Fingerprint | None = None
    event_id: EventId | None = None
    stream_position: Annotated[int, Field(ge=1)] | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        after = (
            self.after_record_version,
            self.after_record_fingerprint,
            self.after_content_fingerprint,
        )
        event = (self.event_id, self.stream_position)
        if not self.success:
            if self.changed or self.workflow_state is not None:
                raise ValueError("rejected initialization cannot expose workflow state")
            if any(value is not None for value in (*after, *event)):
                raise ValueError("rejected initialization cannot contain commit evidence")
            if self.disposition is WorkflowInitializationDisposition.VALIDATION_REJECTED and (
                self.validation_result is None or self.validation_result.success
            ):
                raise ValueError("validation rejection requires failed pipeline evidence")
            return self
        if self.validation_result is None or not self.validation_result.success:
            raise ValueError("successful initialization requires successful pipeline evidence")
        if self.workflow_state is None or self.workflow_state.workflow_id != self.workflow_id:
            raise ValueError("successful initialization requires matching workflow state")
        if any(value is None for value in after):
            raise ValueError("successful initialization requires after-state evidence")
        if self.changed:
            if self.disposition is not WorkflowInitializationDisposition.INITIALIZED:
                raise ValueError("changed initialization must use initialized disposition")
            if any(value is None for value in event):
                raise ValueError("changed initialization requires event evidence")
            if self.after_record_version != self.before_record_version + 1:
                raise ValueError("changed initialization must increment the aggregate once")
        else:
            if self.disposition is not WorkflowInitializationDisposition.ALREADY_INITIALIZED:
                raise ValueError("successful no-op must report already initialized")
            if any(value is not None for value in event):
                raise ValueError("no-op initialization cannot append an event")
            if after != (
                self.before_record_version,
                self.before_record_fingerprint,
                self.before_content_fingerprint,
            ):
                raise ValueError("no-op initialization must preserve aggregate evidence")
        return self


class CreateSnapshotResult(BaseModel):
    """Deterministic result of explicit snapshot evaluation and retention."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_name: Literal["arch.runtime.result.create_snapshot"] = (
        "arch.runtime.result.create_snapshot"
    )
    contract_version: Literal["1.0.0"] = "1.0.0"
    project_id: ProjectId
    policy_matched: bool
    created: bool
    snapshot_id: SnapshotId | None = None
    record_version: Annotated[int, Field(ge=1)]
    record_fingerprint: Fingerprint
    content_fingerprint: Fingerprint
    stream_position: Annotated[int, Field(ge=1)]
    stream_fingerprint: Fingerprint
    retained_snapshot_ids: tuple[SnapshotId, ...] = ()
    deleted_snapshot_ids: tuple[SnapshotId, ...] = ()

    @model_validator(mode="after")
    def validate_snapshot_outcome(self) -> Self:
        if self.created and self.snapshot_id is None:
            raise ValueError("created snapshot requires its identity")
        if self.snapshot_id is not None and self.snapshot_id not in self.retained_snapshot_ids:
            raise ValueError("selected snapshot must be retained")
        if set(self.retained_snapshot_ids) & set(self.deleted_snapshot_ids):
            raise ValueError("retained and deleted snapshots must be disjoint")
        return self


class RecoverAggregateResult(BaseModel):
    """Canonical idempotent evidence of explicit materialized-state recovery."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_name: Literal["arch.runtime.result.recover_aggregate"] = (
        "arch.runtime.result.recover_aggregate"
    )
    contract_version: Literal["1.0.0"] = "1.0.0"
    project_id: ProjectId
    recovered: bool
    replay_result: ReplayResult
    before_record_version: Annotated[int, Field(ge=1)]
    before_record_fingerprint: Fingerprint
    before_content_fingerprint: Fingerprint
    after_record_version: Annotated[int, Field(ge=1)]
    after_record_fingerprint: Fingerprint
    after_content_fingerprint: Fingerprint
    event_id: EventId | None = None
    stream_position: Annotated[int, Field(ge=1)] | None = None

    @model_validator(mode="after")
    def validate_recovery_outcome(self) -> Self:
        if self.replay_result.project_id != self.project_id:
            raise ValueError("replay result must belong to the recovered project")
        after = (
            self.after_record_version,
            self.after_record_fingerprint,
            self.after_content_fingerprint,
        )
        replay = (
            self.replay_result.record_version,
            self.replay_result.record_fingerprint,
            self.replay_result.content_fingerprint,
        )
        if after != replay:
            raise ValueError("recovery after evidence must equal verified replay")
        if self.recovered != (self.before_record_fingerprint != self.after_record_fingerprint):
            raise ValueError("recovered must reflect materialized aggregate divergence")
        event = (self.event_id, self.stream_position)
        if self.recovered and any(value is None for value in event):
            raise ValueError("recovery requires complete audit event evidence")
        if not self.recovered and any(value is not None for value in event):
            raise ValueError("no-op recovery cannot append an audit event")
        return self


class StoredContractKind(StrEnum):
    AGGREGATE = "aggregate"
    EVENT = "event"
    SNAPSHOT = "snapshot"


class StoredContractMigrationItem(BaseModel):
    """Before/after evidence for one K10-evaluated stored record."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: StoredContractKind
    record_id: str
    source_version: SemanticVersion
    target_version: SemanticVersion
    before_schema_fingerprint: Fingerprint
    before_record_fingerprint: Fingerprint
    before_content_fingerprint: Fingerprint | None = None
    after_schema_fingerprint: Fingerprint | None = None
    after_record_fingerprint: Fingerprint | None = None
    after_content_fingerprint: Fingerprint | None = None
    after_payload: FrozenJsonObject | None = None
    migration_result: MigrationResult

    @model_validator(mode="after")
    def validate_migration_item(self) -> Self:
        after = (
            self.after_schema_fingerprint,
            self.after_record_fingerprint,
            self.after_payload,
        )
        if self.migration_result.success and any(value is None for value in after):
            raise ValueError("successful stored migration requires complete after evidence")
        if not self.migration_result.success and any(value is not None for value in after):
            raise ValueError("failed stored migration cannot expose an after projection")
        return self


class StoredContractMigrationDryRunResult(BaseModel):
    """Pure deterministic plan produced before any persistence mutation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_name: Literal["arch.runtime.result.stored_contract_migration_dry_run"] = (
        "arch.runtime.result.stored_contract_migration_dry_run"
    )
    contract_version: Literal["1.0.0"] = "1.0.0"
    success: bool
    changed: bool
    project_id: ProjectId
    evaluated_at: datetime
    before_record_version: Annotated[int, Field(ge=1)]
    before_record_fingerprint: Fingerprint
    before_content_fingerprint: Fingerprint
    stream_position: Annotated[int, Field(ge=1)]
    stream_fingerprint: Fingerprint
    migration_registry_fingerprint: Fingerprint
    items: tuple[StoredContractMigrationItem, ...]
    projected_state: ProjectState | None = None
    plan_fingerprint: Fingerprint

    @model_validator(mode="after")
    def validate_dry_run(self) -> Self:
        if self.changed != bool(self.items):
            raise ValueError("changed must reflect migration items")
        if self.success != all(item.migration_result.success for item in self.items):
            raise ValueError("dry-run success must reflect all K10 results")
        if self.success and self.projected_state is None:
            raise ValueError("successful dry run requires projected current state")
        if not self.success and self.projected_state is not None:
            raise ValueError("failed dry run cannot expose projected state")
        return self


class ApplyStoredContractMigrationResult(BaseModel):
    """Canonical idempotent result of one explicit stored-contract migration."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_name: Literal["arch.runtime.result.apply_stored_contract_migration"] = (
        "arch.runtime.result.apply_stored_contract_migration"
    )
    contract_version: Literal["1.0.0"] = "1.0.0"
    success: bool
    changed: bool
    project_id: ProjectId
    dry_run: StoredContractMigrationDryRunResult
    event_id: EventId | None = None
    stream_position: Annotated[int, Field(ge=1)] | None = None

    @model_validator(mode="after")
    def validate_apply_result(self) -> Self:
        if self.success != self.dry_run.success or self.changed != self.dry_run.changed:
            raise ValueError("apply outcome must agree with its dry run")
        event = (self.event_id, self.stream_position)
        if self.success and self.changed and any(value is None for value in event):
            raise ValueError("changed migration requires audit event evidence")
        if (not self.success or not self.changed) and any(value is not None for value in event):
            raise ValueError("non-committed migration cannot contain event evidence")
        return self
