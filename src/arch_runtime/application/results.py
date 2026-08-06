"""Strict immutable application results."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from arch_kernel.contracts import EventId, ProjectId, ValidationPipelineResult
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
