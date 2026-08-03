"""Canonical kernel contract encoding and fail-closed reconstruction."""

from __future__ import annotations

import json
from dataclasses import dataclass

from arch_kernel.contracts import EventEnvelope, ProjectState
from arch_kernel.kernel import (
    build_builtin_contract_registry,
    canonicalize_json,
    compute_content_fingerprint,
    compute_fingerprint,
)
from pydantic import ValidationError

from arch_runtime.errors import CorruptStoredRecordError, UnsupportedStoredContractError
from arch_runtime.ports.storage import StoredEvent, StoredProject, StoredSnapshot


@dataclass(frozen=True, slots=True)
class EncodedProject:
    state_json: bytes
    schema_fingerprint: str
    record_fingerprint: str
    content_fingerprint: str


class KernelContractCodec:
    """Encode and verify the kernel contracts persisted by R04."""

    def __init__(self) -> None:
        registry = build_builtin_contract_registry()
        project = registry.get_by_model(ProjectState)
        event = registry.get_by_model(EventEnvelope)
        if project is None or event is None:
            raise UnsupportedStoredContractError(
                "required kernel contracts are not registered",
                operation="sqlite.codec.initialize",
                remediation="install a compatible arch-kernel release",
            )
        self._project_descriptor = project.descriptor
        self._event_descriptor = event.descriptor

    def encode_project(self, state: ProjectState) -> EncodedProject:
        return EncodedProject(
            state_json=canonicalize_json(state),
            schema_fingerprint=self._project_descriptor.schema_fingerprint,
            record_fingerprint=compute_fingerprint(state),
            content_fingerprint=compute_content_fingerprint(state),
        )

    def verify_project(self, stored: StoredProject | StoredSnapshot) -> ProjectState:
        self._verify_contract_identity(
            stored.contract_name,
            str(stored.contract_version),
            stored.schema_fingerprint,
        )
        try:
            state = ProjectState.model_validate_json(stored.state_json, strict=True)
        except (ValidationError, ValueError) as error:
            self._corrupt("stored project JSON is invalid", stored.project_id, error)
        if canonicalize_json(state) != stored.state_json:
            self._corrupt("stored project JSON is not canonical", stored.project_id)
        if (
            state.metadata.project_id != stored.project_id
            or state.contract_name != stored.contract_name
            or state.contract_version != stored.contract_version
            or state.record_version
            != getattr(stored, "record_version", getattr(stored, "aggregate_version", -1))
            or compute_fingerprint(state) != stored.record_fingerprint
            or compute_content_fingerprint(state) != stored.content_fingerprint
        ):
            self._corrupt("stored project evidence does not match its payload", stored.project_id)
        return state

    def encode_event(self, event: EventEnvelope) -> tuple[bytes, str]:
        return canonicalize_json(event), compute_fingerprint(event)

    def verify_event(self, stored: StoredEvent) -> EventEnvelope:
        if str(stored.schema_version) != str(self._event_descriptor.version):
            raise UnsupportedStoredContractError(
                "stored event contract version is unsupported",
                operation="event.read",
                remediation="run an explicit stored-contract migration",
                project_id=stored.project_id,
            )
        try:
            event = EventEnvelope.model_validate_json(stored.event_json, strict=True)
        except (ValidationError, ValueError) as error:
            self._corrupt("stored event JSON is invalid", stored.project_id, error)
        if (
            canonicalize_json(event) != stored.event_json
            or event.event_id != stored.event_id
            or event.project_id != stored.project_id
            or event.event_type != stored.event_type
            or event.schema_version != stored.schema_version
            or event.idempotency_key != stored.idempotency_key
            or event.recorded_at != stored.recorded_at
            or compute_fingerprint(event) != stored.event_fingerprint
        ):
            self._corrupt("stored event evidence does not match its payload", stored.project_id)
        return event

    def verify_json_evidence(self, payload: bytes, fingerprint: str) -> None:
        try:
            value = json.loads(payload)
            canonical = canonicalize_json(value)
            actual_fingerprint = compute_fingerprint(value)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            self._corrupt("stored JSON evidence is invalid", cause=error)
        if canonical != payload or actual_fingerprint != fingerprint:
            self._corrupt("stored JSON evidence is not canonical or fingerprinted correctly")

    def _verify_contract_identity(self, name: str, version: str, schema_fingerprint: str) -> None:
        descriptor = self._project_descriptor
        if version != str(descriptor.version):
            raise UnsupportedStoredContractError(
                "stored project contract is unsupported",
                operation="project.read",
                remediation="run an explicit stored-contract migration",
            )
        if schema_fingerprint != descriptor.schema_fingerprint:
            self._corrupt("stored project schema fingerprint is invalid")

    @staticmethod
    def _corrupt(
        message: str,
        project_id: object | None = None,
        cause: BaseException | None = None,
    ) -> None:
        error = CorruptStoredRecordError(
            message,
            operation="sqlite.decode",
            remediation="preserve the database and restore verified evidence",
            project_id=project_id,  # type: ignore[arg-type]
        )
        if cause is None:
            raise error
        raise error from cause
