"""Immutable public results for deterministic project replay."""

from enum import StrEnum
from typing import Annotated, Literal, Self

from arch_kernel.contracts import ProjectId, ProjectState
from pydantic import BaseModel, ConfigDict, Field, model_validator

from arch_runtime.ports.storage import Fingerprint, SnapshotId


class SnapshotDisposition(StrEnum):
    """How replay treated the latest derived snapshot."""

    NOT_AVAILABLE = "not_available"
    USED = "used"
    BYPASSED_INVALID = "bypassed_invalid"


class ReplayFinding(BaseModel):
    """Safe non-blocking evidence surfaced by a successful replay."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    code: Literal["replay.snapshot_invalid"]
    message: Literal["Latest snapshot was invalid; full replay was used."]
    snapshot_id: SnapshotId | None = None


class ReplayResult(BaseModel):
    """Verified reconstructed state and its replay evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_name: Literal["arch.runtime.result.replay_project"] = (
        "arch.runtime.result.replay_project"
    )
    contract_version: Literal["1.0.0"] = "1.0.0"
    project_id: ProjectId
    reconstructed_state: ProjectState
    event_count: Annotated[int, Field(ge=1)]
    first_stream_position: Annotated[int, Field(ge=1)]
    last_stream_position: Annotated[int, Field(ge=1)]
    last_event_fingerprint: Fingerprint
    snapshot_disposition: SnapshotDisposition
    snapshot_id: SnapshotId | None = None
    snapshot_findings: tuple[ReplayFinding, ...] = ()
    record_version: Annotated[int, Field(ge=1)]
    record_fingerprint: Fingerprint
    content_fingerprint: Fingerprint

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if self.reconstructed_state.metadata.project_id != self.project_id:
            raise ValueError("reconstructed state must belong to the requested project")
        if self.reconstructed_state.record_version != self.record_version:
            raise ValueError("reconstructed state version must match replay evidence")
        if self.first_stream_position > self.last_stream_position:
            raise ValueError("stream positions must be ordered")
        if self.snapshot_disposition is SnapshotDisposition.NOT_AVAILABLE:
            if self.snapshot_id is not None or self.snapshot_findings:
                raise ValueError("absent snapshot cannot contain snapshot evidence")
        elif self.snapshot_disposition is SnapshotDisposition.USED:
            if self.snapshot_id is None:
                raise ValueError("used snapshot requires its identity")
        elif not self.snapshot_findings:
            raise ValueError("bypassed snapshot requires a visible integrity finding")
        return self
