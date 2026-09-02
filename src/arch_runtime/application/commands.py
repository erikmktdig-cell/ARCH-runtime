"""Strict immutable application commands."""

from __future__ import annotations

from typing import Annotated, Literal

from arch_kernel.contracts import (
    ActorType,
    ChangeId,
    FrozenJsonObject,
    PatchId,
    ProjectId,
    Route,
    SemanticVersion,
    TransitionId,
    TransitionTarget,
    WorkflowId,
    WorkflowNamespace,
)
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from arch_runtime.ports.storage import Fingerprint


class CreateProjectCommand(BaseModel):
    """Normalized intent to create one minimal project aggregate."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_name: Literal["arch.runtime.command.create_project"] = (
        "arch.runtime.command.create_project"
    )
    contract_version: Literal["1.0.0"] = "1.0.0"
    idempotency_key: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    name: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    slug: Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
    summary: Annotated[str, StringConstraints(max_length=2_000)] | None = None
    owner: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    project_type: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    criticality: Literal["low", "medium", "high", "critical"]
    default_route: Route
    objectives: tuple[Annotated[str, StringConstraints(min_length=1, max_length=500)], ...] = ()
    constraints: tuple[Annotated[str, StringConstraints(min_length=1, max_length=500)], ...] = ()
    success_criteria: tuple[
        Annotated[str, StringConstraints(min_length=1, max_length=500)], ...
    ] = ()
    actor_type: ActorType = ActorType.HUMAN
    actor_id: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    actor_display_name: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = (
        None
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        for field in ("idempotency_key", "name", "owner", "project_type", "actor_id"):
            item = normalized.get(field)
            if isinstance(item, str):
                normalized[field] = item.strip()
        for field in ("summary", "actor_display_name"):
            item = normalized.get(field)
            if isinstance(item, str):
                normalized[field] = item.strip()
        slug = normalized.get("slug")
        if isinstance(slug, str):
            normalized["slug"] = slug.strip().lower()
        for field in ("objectives", "constraints", "success_criteria"):
            items = normalized.get(field)
            if isinstance(items, (list, tuple)):
                normalized[field] = tuple(
                    item.strip() if isinstance(item, str) else item for item in items
                )
        return normalized

    def request_payload(self) -> dict[str, object]:
        """Return normalized logical intent without its idempotency scope key."""

        return self.model_dump(
            mode="json",
            exclude={"idempotency_key"},
        )


class ApplyTransitionCommand(BaseModel):
    """Normalized intent to evaluate and atomically apply one transition."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_name: Literal["arch.runtime.command.apply_transition"] = (
        "arch.runtime.command.apply_transition"
    )
    contract_version: Literal["1.0.0"] = "1.0.0"
    idempotency_key: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    project_id: ProjectId
    request_id: ChangeId | PatchId
    target: TransitionTarget
    transition_id: TransitionId | None = None
    transition_key: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    expected_from_state: Annotated[str, StringConstraints(min_length=1, max_length=100)] | None = (
        None
    )
    expected_record_version: Annotated[int, Field(ge=1)]
    expected_record_fingerprint: Fingerprint
    expected_content_fingerprint: Fingerprint
    expected_target_record_version: Annotated[int, Field(ge=1)] | None = None
    expected_target_content_fingerprint: Fingerprint | None = None
    metadata: FrozenJsonObject
    actor_type: ActorType = ActorType.HUMAN
    actor_id: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    actor_display_name: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = (
        None
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        for field in ("idempotency_key", "transition_key", "expected_from_state", "actor_id"):
            item = normalized.get(field)
            if isinstance(item, str):
                normalized[field] = item.strip()
        display_name = normalized.get("actor_display_name")
        if isinstance(display_name, str):
            normalized["actor_display_name"] = display_name.strip()
        metadata = normalized.get("metadata")
        if isinstance(metadata, dict):
            normalized["metadata"] = {
                str(key).strip(): item.strip() if isinstance(item, str) else item
                for key, item in metadata.items()
            }
        return normalized

    @model_validator(mode="after")
    def validate_selector(self) -> ApplyTransitionCommand:
        if (self.transition_id is None) == (self.transition_key is None):
            raise ValueError("exactly one transition selector is required")
        return self

    def request_payload(self) -> dict[str, object]:
        """Return normalized logical intent without its idempotency scope key."""

        return self.model_dump(mode="json", exclude={"idempotency_key"})


class InitializeWorkflowCommand(BaseModel):
    """Initialize one configured generic workflow in an existing project."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_name: Literal["arch.runtime.command.initialize_workflow"] = (
        "arch.runtime.command.initialize_workflow"
    )
    contract_version: Literal["1.0.0"] = "1.0.0"
    idempotency_key: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    project_id: ProjectId
    workflow_id: WorkflowId
    workflow_namespace: WorkflowNamespace
    definition_version: SemanticVersion
    definition_fingerprint: Fingerprint
    expected_record_version: Annotated[int, Field(ge=1)]
    expected_record_fingerprint: Fingerprint
    expected_content_fingerprint: Fingerprint
    actor_type: ActorType = ActorType.HUMAN
    actor_id: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    actor_display_name: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = (
        None
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        for field in ("idempotency_key", "actor_id", "actor_display_name"):
            item = normalized.get(field)
            if isinstance(item, str):
                normalized[field] = item.strip()
        return normalized

    def request_payload(self) -> dict[str, object]:
        """Return normalized logical intent without its idempotency scope key."""

        return self.model_dump(mode="json", exclude={"idempotency_key"})


class CreateSnapshotCommand(BaseModel):
    """Explicit request to checkpoint one verified committed project state."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_name: Literal["arch.runtime.command.create_snapshot"] = (
        "arch.runtime.command.create_snapshot"
    )
    contract_version: Literal["1.0.0"] = "1.0.0"
    project_id: ProjectId
    expected_record_version: Annotated[int, Field(ge=1)]
    expected_record_fingerprint: Fingerprint
    expected_content_fingerprint: Fingerprint
    expected_stream_position: Annotated[int, Field(ge=1)]
    expected_stream_fingerprint: Fingerprint
    force: bool = False


class RecoverAggregateCommand(BaseModel):
    """Explicit intent to replace divergent materialized state from verified history."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_name: Literal["arch.runtime.command.recover_aggregate"] = (
        "arch.runtime.command.recover_aggregate"
    )
    contract_version: Literal["1.0.0"] = "1.0.0"
    idempotency_key: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    project_id: ProjectId
    expected_record_version: Annotated[int, Field(ge=1)]
    expected_record_fingerprint: Fingerprint
    expected_content_fingerprint: Fingerprint
    reason: Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
    actor_type: ActorType = ActorType.HUMAN
    actor_id: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    actor_display_name: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = (
        None
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        for field in ("idempotency_key", "reason", "actor_id", "actor_display_name"):
            item = normalized.get(field)
            if isinstance(item, str):
                normalized[field] = item.strip()
        return normalized

    def request_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"idempotency_key"})


class PlanStoredContractMigrationCommand(BaseModel):
    """Explicit immutable request to dry-run stored contract migration."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_name: Literal["arch.runtime.command.plan_stored_contract_migration"] = (
        "arch.runtime.command.plan_stored_contract_migration"
    )
    contract_version: Literal["1.0.0"] = "1.0.0"
    project_id: ProjectId
    target_project_version: SemanticVersion
    target_event_version: SemanticVersion
    expected_record_version: Annotated[int, Field(ge=1)]
    expected_record_fingerprint: Fingerprint
    expected_content_fingerprint: Fingerprint
    expected_stream_position: Annotated[int, Field(ge=1)]
    expected_stream_fingerprint: Fingerprint


class ApplyStoredContractMigrationCommand(BaseModel):
    """Explicit write intent bound to one exact migration dry-run plan."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_name: Literal["arch.runtime.command.apply_stored_contract_migration"] = (
        "arch.runtime.command.apply_stored_contract_migration"
    )
    contract_version: Literal["1.0.0"] = "1.0.0"
    idempotency_key: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    plan: PlanStoredContractMigrationCommand
    expected_plan_fingerprint: Fingerprint
    reason: Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
    actor_type: ActorType = ActorType.HUMAN
    actor_id: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    actor_display_name: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = (
        None
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        for field in ("idempotency_key", "reason", "actor_id", "actor_display_name"):
            item = normalized.get(field)
            if isinstance(item, str):
                normalized[field] = item.strip()
        return normalized

    def request_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"idempotency_key"})
