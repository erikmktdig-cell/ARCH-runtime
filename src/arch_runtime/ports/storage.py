"""Immutable records exchanged across persistence ports."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from arch_kernel.contracts import EventId, ProjectId, SemanticVersion
from arch_kernel.kernel import ensure_utc
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Fingerprint = Annotated[
    str,
    StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$"),
]
CanonicalJson = Annotated[bytes, Field(min_length=2)]
SnapshotId = Annotated[
    str,
    StringConstraints(pattern=r"^SNP-[0-9A-HJKMNP-TV-Z]{26}$"),
]


class StoredRecord(BaseModel):
    """Strict frozen base for runtime-owned storage evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class IdempotencyStatus(StrEnum):
    """Lifecycle of an idempotency reservation."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class StoredProject(StoredRecord):
    """Canonical aggregate bytes plus independently indexed evidence."""

    project_id: ProjectId
    record_version: Annotated[int, Field(ge=1)]
    contract_name: str
    contract_version: SemanticVersion
    schema_fingerprint: Fingerprint
    record_fingerprint: Fingerprint
    content_fingerprint: Fingerprint
    state_json: CanonicalJson
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_times(self) -> Self:
        ensure_utc(self.created_at)
        ensure_utc(self.updated_at)
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        return self


class StoredEvent(StoredRecord):
    """Append-only event bytes and per-project chain metadata."""

    stream_position: Annotated[int, Field(ge=1)]
    event_id: EventId
    project_id: ProjectId
    aggregate_version_before: Annotated[int, Field(ge=0)]
    aggregate_version_after: Annotated[int, Field(ge=0)]
    is_state_change: bool
    event_type: str
    schema_version: SemanticVersion
    idempotency_key: str
    event_fingerprint: Fingerprint
    previous_event_fingerprint: Fingerprint | None
    event_json: CanonicalJson
    recorded_at: datetime

    @model_validator(mode="after")
    def validate_version_change(self) -> Self:
        ensure_utc(self.recorded_at)
        expected_after = (
            self.aggregate_version_before + 1
            if self.is_state_change
            else self.aggregate_version_before
        )
        if self.aggregate_version_after != expected_after:
            raise ValueError("aggregate versions disagree with is_state_change")
        return self


class StoredSnapshot(StoredRecord):
    """Derived aggregate checkpoint tied to an exact stream position."""

    snapshot_id: SnapshotId
    project_id: ProjectId
    aggregate_version: Annotated[int, Field(ge=1)]
    last_stream_position: Annotated[int, Field(ge=1)]
    contract_name: str
    contract_version: SemanticVersion
    schema_fingerprint: Fingerprint
    record_fingerprint: Fingerprint
    content_fingerprint: Fingerprint
    state_json: CanonicalJson
    created_at: datetime

    @model_validator(mode="after")
    def validate_created_at(self) -> Self:
        ensure_utc(self.created_at)
        return self


class IdempotencyRecord(StoredRecord):
    """Canonical request reservation or completed logical result."""

    operation_name: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")]
    idempotency_key: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    project_id: ProjectId | None = None
    request_fingerprint: Fingerprint
    status: IdempotencyStatus
    result_fingerprint: Fingerprint | None = None
    result_json: CanonicalJson | None = None
    created_at: datetime
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_status_evidence(self) -> Self:
        ensure_utc(self.created_at)
        completion = (self.result_fingerprint, self.result_json, self.completed_at)
        if self.status is IdempotencyStatus.IN_PROGRESS and any(
            value is not None for value in completion
        ):
            raise ValueError("in-progress idempotency records cannot contain result evidence")
        if self.status is IdempotencyStatus.COMPLETED and any(
            value is None for value in completion
        ):
            raise ValueError("completed idempotency records require complete result evidence")
        if self.completed_at is not None:
            ensure_utc(self.completed_at)
            if self.completed_at < self.created_at:
                raise ValueError("completed_at must not precede created_at")
        return self
