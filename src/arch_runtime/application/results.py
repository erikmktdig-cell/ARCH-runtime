"""Strict immutable application results."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from arch_kernel.contracts import EventId, ProjectId, ValidationPipelineResult
from pydantic import BaseModel, ConfigDict, Field, model_validator

from arch_runtime.ports.storage import Fingerprint


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
