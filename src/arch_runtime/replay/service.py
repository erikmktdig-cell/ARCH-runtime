"""Read-only deterministic replay and integrity verification."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NoReturn, cast

from arch_kernel.contracts import (
    DryRunResult,
    EventEnvelope,
    ProjectId,
    ProjectState,
    StatePatch,
    TransitionRequest,
    ValidationPipelineResult,
)
from arch_kernel.kernel import (
    apply_state_patch_dry_run,
    build_builtin_contract_registry,
    canonicalize_json,
    compute_content_fingerprint,
    compute_fingerprint,
)
from pydantic import ValidationError

from arch_runtime.errors import (
    CorruptStoredRecordError,
    ProjectNotFoundError,
    ReplayIntegrityError,
    StoredMigrationRequiredError,
    UnsupportedStoredContractError,
)
from arch_runtime.ports import StoredEvent, StoredProject, StoredSnapshot, UnitOfWorkFactory
from arch_runtime.replay.contracts import (
    ReplayFinding,
    ReplayResult,
    SnapshotDisposition,
)

_CREATED = "project.aggregate.created"
_TRANSITION_APPLIED = "project.aggregate.transition_applied"


@dataclass(frozen=True, slots=True)
class _DecodedEvent:
    stored: StoredEvent
    envelope: EventEnvelope
    payload: dict[str, Any]
    after_version: int
    after_record_fingerprint: str
    after_content_fingerprint: str


type PatchRunner = Callable[..., DryRunResult]


class ReplayService:
    """Reconstruct one project from immutable evidence without performing writes."""

    def __init__(
        self,
        *,
        unit_of_work_factory: UnitOfWorkFactory,
        patch_runner: PatchRunner = apply_state_patch_dry_run,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._patch_runner = patch_runner

    def replay_project(self, project_id: ProjectId) -> ReplayResult:
        aggregate, events, snapshot, snapshot_unreadable = self._load(project_id)
        aggregate_state = _decode_project(aggregate, operation="project.replay.aggregate")
        decoded = _decode_stream(project_id, events)
        snapshot_state, disposition, finding = _select_snapshot(
            project_id,
            snapshot,
            snapshot_unreadable,
            decoded,
        )

        if snapshot_state is None:
            state = _created_state(decoded[0])
            remaining = decoded[1:]
        else:
            state = snapshot_state
            assert snapshot is not None
            remaining = tuple(
                item
                for item in decoded
                if item.stored.stream_position > snapshot.last_stream_position
            )
        for item in remaining:
            if item.stored.event_type != _TRANSITION_APPLIED:
                _fail(
                    "event following replay checkpoint is not a supported transition",
                    project_id,
                )
            state = self._apply_transition(state, item)

        _require_final_aggregate(state, aggregate_state, aggregate, decoded[-1])
        return ReplayResult(
            project_id=project_id,
            reconstructed_state=state,
            event_count=len(decoded),
            first_stream_position=decoded[0].stored.stream_position,
            last_stream_position=decoded[-1].stored.stream_position,
            snapshot_disposition=disposition,
            snapshot_id=None if snapshot is None else snapshot.snapshot_id,
            snapshot_findings=() if finding is None else (finding,),
            record_version=state.record_version,
            record_fingerprint=compute_fingerprint(state),
            content_fingerprint=compute_content_fingerprint(state),
        )

    def _load(
        self, project_id: ProjectId
    ) -> tuple[
        StoredProject,
        tuple[StoredEvent, ...],
        StoredSnapshot | None,
        bool,
    ]:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                aggregate = unit_of_work.projects.get(project_id)
                events = unit_of_work.events.read_stream(project_id)
                try:
                    snapshot = unit_of_work.snapshots.get_latest(project_id)
                    snapshot_unreadable = False
                except (
                    CorruptStoredRecordError,
                    StoredMigrationRequiredError,
                    UnsupportedStoredContractError,
                ):
                    snapshot = None
                    snapshot_unreadable = True
        except CorruptStoredRecordError as error:
            _fail("stored replay evidence is corrupt", project_id, cause=error)
        if aggregate is None:
            raise ProjectNotFoundError(
                "project not found",
                operation="project.replay.load",
                remediation="create the project before replay",
                project_id=project_id,
            )
        if not events:
            _fail("project event stream is empty", project_id)
        return aggregate, events, snapshot, snapshot_unreadable

    def _apply_transition(self, state: ProjectState, item: _DecodedEvent) -> ProjectState:
        payload = item.payload
        before = _object(payload, "before", item.stored.project_id)
        _require_state_evidence(state, before, "transition before evidence")
        try:
            patch = StatePatch.model_validate_json(
                canonicalize_json(payload.get("patch")), strict=True
            )
            transition_request = TransitionRequest.model_validate_json(
                canonicalize_json(payload.get("transition_request")), strict=True
            )
            validation = ValidationPipelineResult.model_validate_json(
                canonicalize_json(payload.get("validation_result")), strict=True
            )
        except (ValidationError, ValueError, TypeError) as error:
            _fail("transition replay payload is invalid", item.stored.project_id, cause=error)
        if (
            transition_request.project_id != item.stored.project_id
            or patch.project_id != item.stored.project_id
            or not validation.success
            or validation.generated_patch != patch
            or validation.projected_state is None
        ):
            _fail("transition replay contracts disagree", item.stored.project_id)
        dry_run = self._patch_runner(state, patch, evaluated_at=validation.evaluated_at)
        if dry_run.projected_state is None or dry_run.projected_state != validation.projected_state:
            _fail(
                "persisted transition patch cannot reproduce its K08 projection",
                item.stored.project_id,
            )
        projected = dry_run.projected_state
        after = _object(payload, "after", item.stored.project_id)
        _require_state_evidence(projected, after, "transition after evidence")
        if (
            item.stored.aggregate_version_before != state.record_version
            or item.stored.aggregate_version_after != projected.record_version
        ):
            _fail("transition event versions disagree with replayed state", item.stored.project_id)
        return projected


def _decode_stream(
    project_id: ProjectId, events: tuple[StoredEvent, ...]
) -> tuple[_DecodedEvent, ...]:
    decoded: list[_DecodedEvent] = []
    previous_fingerprint: str | None = None
    previous_position = 0
    previous_version = 0
    previous_after: tuple[int, str, str] | None = None
    for index, stored in enumerate(events):
        if stored.project_id != project_id:
            _fail("event belongs to another project", project_id)
        if stored.stream_position <= previous_position:
            _fail("project events are not ordered by stream position", project_id)
        if stored.previous_event_fingerprint != previous_fingerprint:
            _fail("project event fingerprint chain is broken", project_id)
        envelope = _decode_envelope(stored)
        payload = envelope.model_dump(mode="json")["payload"]
        if not isinstance(payload, dict):
            _fail("event payload is not a JSON object", project_id)
        if index == 0 and stored.event_type != _CREATED:
            _fail("project stream must begin with the creation event", project_id)
        if index > 0 and stored.event_type != _TRANSITION_APPLIED:
            _fail("project stream contains an unsupported event type", project_id)
        if stored.aggregate_version_before != previous_version:
            _fail("project event versions are not continuous", project_id)
        after = _event_after_evidence(stored, payload)
        if previous_after is not None:
            before = _object(payload, "before", project_id)
            if _evidence_tuple(before, project_id) != previous_after:
                _fail("adjacent event state evidence is discontinuous", project_id)
        decoded.append(_DecodedEvent(stored, envelope, payload, *after))
        previous_fingerprint = stored.event_fingerprint
        previous_position = stored.stream_position
        previous_version = stored.aggregate_version_after
        previous_after = after
    return tuple(decoded)


def _decode_envelope(stored: StoredEvent) -> EventEnvelope:
    try:
        envelope = EventEnvelope.model_validate_json(stored.event_json, strict=True)
    except (ValidationError, ValueError) as error:
        _fail("stored event JSON is invalid", stored.project_id, cause=error)
    if (
        canonicalize_json(envelope) != stored.event_json
        or compute_fingerprint(envelope) != stored.event_fingerprint
        or envelope.event_id != stored.event_id
        or envelope.project_id != stored.project_id
        or envelope.aggregate.target_id != stored.project_id
        or envelope.event_type != stored.event_type
        or envelope.schema_version != stored.schema_version
        or envelope.idempotency_key != stored.idempotency_key
        or envelope.recorded_at != stored.recorded_at
    ):
        _fail("stored event evidence does not match its envelope", stored.project_id)
    return envelope


def _event_after_evidence(stored: StoredEvent, payload: dict[str, Any]) -> tuple[int, str, str]:
    if stored.event_type == _CREATED:
        state = _created_state_payload(payload, stored.project_id)
        after = (
            state.record_version,
            compute_fingerprint(state),
            compute_content_fingerprint(state),
        )
        if (
            payload.get("record_fingerprint") != after[1]
            or payload.get("content_fingerprint") != after[2]
            or stored.aggregate_version_before != 0
            or stored.aggregate_version_after != after[0]
        ):
            _fail("creation event evidence is inconsistent", stored.project_id)
        return after
    after_object = _object(payload, "after", stored.project_id)
    after = _evidence_tuple(after_object, stored.project_id)
    if (
        stored.aggregate_version_after != after[0]
        or stored.aggregate_version_after != stored.aggregate_version_before + 1
        or not stored.is_state_change
    ):
        _fail("transition event version evidence is inconsistent", stored.project_id)
    return after


def _created_state(item: _DecodedEvent) -> ProjectState:
    return _created_state_payload(item.payload, item.stored.project_id)


def _created_state_payload(payload: dict[str, Any], project_id: ProjectId) -> ProjectState:
    try:
        state = ProjectState.model_validate_json(
            canonicalize_json(payload.get("state")), strict=True
        )
    except (ValidationError, ValueError, TypeError) as error:
        _fail("creation event state is invalid", project_id, cause=error)
    if state.metadata.project_id != project_id:
        _fail("creation event state belongs to another project", project_id)
    return state


def _select_snapshot(
    project_id: ProjectId,
    snapshot: StoredSnapshot | None,
    unreadable: bool,
    events: tuple[_DecodedEvent, ...],
) -> tuple[ProjectState | None, SnapshotDisposition, ReplayFinding | None]:
    if unreadable:
        return None, SnapshotDisposition.BYPASSED_INVALID, _snapshot_finding(None)
    if snapshot is None:
        return None, SnapshotDisposition.NOT_AVAILABLE, None
    try:
        state = _decode_project(snapshot, operation="project.replay.snapshot")
        checkpoint = next(
            item for item in events if item.stored.stream_position == snapshot.last_stream_position
        )
        expected = (
            checkpoint.after_version,
            checkpoint.after_record_fingerprint,
            checkpoint.after_content_fingerprint,
        )
        actual = (
            snapshot.aggregate_version,
            snapshot.record_fingerprint,
            snapshot.content_fingerprint,
        )
        if snapshot.project_id != project_id or actual != expected:
            raise ValueError("snapshot evidence mismatch")
    except (ReplayIntegrityError, StopIteration, ValueError):
        return (
            None,
            SnapshotDisposition.BYPASSED_INVALID,
            _snapshot_finding(snapshot.snapshot_id),
        )
    return state, SnapshotDisposition.USED, None


def _decode_project(stored: StoredProject | StoredSnapshot, *, operation: str) -> ProjectState:
    registration = build_builtin_contract_registry().get_by_model(ProjectState)
    if registration is None:
        _fail("ProjectState contract registration is unavailable", stored.project_id)
    try:
        state = ProjectState.model_validate_json(stored.state_json, strict=True)
    except (ValidationError, ValueError) as error:
        _fail("stored project JSON is invalid", stored.project_id, cause=error)
    version = getattr(stored, "record_version", getattr(stored, "aggregate_version", -1))
    if (
        canonicalize_json(state) != stored.state_json
        or state.metadata.project_id != stored.project_id
        or state.contract_name != stored.contract_name
        or state.contract_version != stored.contract_version
        or stored.schema_fingerprint != registration.descriptor.schema_fingerprint
        or state.record_version != version
        or compute_fingerprint(state) != stored.record_fingerprint
        or compute_content_fingerprint(state) != stored.content_fingerprint
    ):
        _fail("stored project evidence does not match its payload", stored.project_id)
    return state


def _require_final_aggregate(
    reconstructed: ProjectState,
    aggregate_state: ProjectState,
    aggregate: StoredProject,
    tail: _DecodedEvent,
) -> None:
    evidence = (
        reconstructed.record_version,
        compute_fingerprint(reconstructed),
        compute_content_fingerprint(reconstructed),
    )
    if (
        reconstructed != aggregate_state
        or canonicalize_json(reconstructed) != aggregate.state_json
        or evidence
        != (
            aggregate.record_version,
            aggregate.record_fingerprint,
            aggregate.content_fingerprint,
        )
        or evidence
        != (
            tail.after_version,
            tail.after_record_fingerprint,
            tail.after_content_fingerprint,
        )
    ):
        _fail("replayed state disagrees with the operational aggregate", aggregate.project_id)


def _require_state_evidence(state: ProjectState, evidence: dict[str, Any], label: str) -> None:
    expected = (
        state.record_version,
        compute_fingerprint(state),
        compute_content_fingerprint(state),
    )
    if _evidence_tuple(evidence, state.metadata.project_id) != expected:
        _fail(f"{label} is inconsistent", state.metadata.project_id)


def _object(payload: dict[str, Any], key: str, project_id: ProjectId) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        _fail(f"event {key} evidence is missing", project_id)
    return cast(dict[str, Any], value)


def _evidence_tuple(value: dict[str, Any], project_id: ProjectId) -> tuple[int, str, str]:
    version = value.get("record_version")
    record = value.get("record_fingerprint")
    content = value.get("content_fingerprint")
    if not isinstance(version, int) or not isinstance(record, str) or not isinstance(content, str):
        _fail("event state evidence is malformed", project_id)
    return version, record, content


def _snapshot_finding(snapshot_id: str | None) -> ReplayFinding:
    return ReplayFinding(
        code="replay.snapshot_invalid",
        message="Latest snapshot was invalid; full replay was used.",
        snapshot_id=snapshot_id,
    )


def _fail(
    message: str,
    project_id: ProjectId,
    *,
    cause: BaseException | None = None,
) -> NoReturn:
    error = ReplayIntegrityError(
        message,
        operation="project.replay",
        remediation="preserve stored evidence and investigate before allowing writes",
        project_id=project_id,
    )
    if cause is None:
        raise error
    raise error from cause


def replay_project(service: ReplayService, project_id: ProjectId) -> ReplayResult:
    """Functional synchronous entry point for replay callers."""

    return service.replay_project(project_id)
