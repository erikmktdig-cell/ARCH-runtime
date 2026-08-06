"""Explicit fail-closed recovery of divergent materialized aggregates."""

from datetime import datetime

from arch_kernel.contracts import EventEnvelope, EventId, ProjectState
from arch_kernel.kernel import compute_fingerprint

from arch_runtime.application.commands import RecoverAggregateCommand
from arch_runtime.application.events import build_project_event
from arch_runtime.application.idempotency import resolve_completed_result
from arch_runtime.application.results import RecoverAggregateResult
from arch_runtime.errors import ConcurrentModificationError, ProjectNotFoundError
from arch_runtime.ports import (
    Clock,
    EventIdGenerator,
    IdempotencyRecord,
    IdempotencyStatus,
    StoredProject,
    UnitOfWorkFactory,
)
from arch_runtime.replay import ReplayService
from arch_runtime.replay.contracts import ReplayResult

_OPERATION = "project.recover"


class RecoveryService:
    """Replace operational state only after immutable history verifies successfully."""

    def __init__(
        self,
        *,
        unit_of_work_factory: UnitOfWorkFactory,
        replay_service: ReplayService,
        clock: Clock,
        event_ids: EventIdGenerator,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._replay = replay_service
        self._clock = clock
        self._event_ids = event_ids

    def recover_aggregate(self, command: RecoverAggregateCommand) -> RecoverAggregateResult:
        request_fingerprint = compute_fingerprint(command.request_payload())
        completed, current = self._load(command, request_fingerprint)
        if completed is not None:
            return completed
        assert current is not None
        self._require_expected(command, current)

        replay = self._replay.replay_verified_history(command.project_id)
        with self._unit_of_work_factory() as unit_of_work:
            stream = unit_of_work.events.read_stream(command.project_id)
        tail = stream[-1]
        if tail.stream_position != replay.last_stream_position:
            raise ConcurrentModificationError(
                "project stream changed during recovery evaluation",
                operation="project.recover.evaluate",
                remediation="repeat verified replay from the new stream tail",
                project_id=command.project_id,
            )

        recovered = current.record_fingerprint != replay.record_fingerprint
        event_id = self._event_ids.new() if recovered else None
        result = self._persist(
            command,
            request_fingerprint,
            current,
            replay,
            tail.event_fingerprint,
            recovered,
            event_id,
            self._clock.now(),
        )
        self._replay.replay_project(command.project_id)
        return result

    def _load(
        self, command: RecoverAggregateCommand, request_fingerprint: str
    ) -> tuple[RecoverAggregateResult | None, StoredProject | None]:
        with self._unit_of_work_factory() as unit_of_work:
            completed = resolve_completed_result(
                unit_of_work.idempotency.get(_OPERATION, command.idempotency_key),
                request_fingerprint,
                RecoverAggregateResult,
                operation=_OPERATION,
            )
            if completed is not None:
                return completed, None
            current = unit_of_work.projects.get(command.project_id)
        if current is None:
            raise ProjectNotFoundError(
                "project not found",
                operation="project.recover.load",
                remediation="create the project before recovery",
                project_id=command.project_id,
            )
        return None, current

    def _persist(
        self,
        command: RecoverAggregateCommand,
        request_fingerprint: str,
        before: StoredProject,
        replay: ReplayResult,
        expected_tail_fingerprint: str,
        recovered: bool,
        event_id: EventId | None,
        now: datetime,
    ) -> RecoverAggregateResult:
        with self._unit_of_work_factory() as unit_of_work:
            completed = resolve_completed_result(
                unit_of_work.idempotency.get(_OPERATION, command.idempotency_key),
                request_fingerprint,
                RecoverAggregateResult,
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
            stream = unit_of_work.events.read_stream(command.project_id)
            if current is None or not stream:
                raise ConcurrentModificationError(
                    "project evidence disappeared before recovery commit",
                    operation="project.recover.commit",
                    remediation="reload and replay before retrying recovery",
                    project_id=command.project_id,
                )
            tail = stream[-1]
            if (
                current.record_version != before.record_version
                or current.record_fingerprint != before.record_fingerprint
                or current.content_fingerprint != before.content_fingerprint
                or tail.stream_position != replay.last_stream_position
                or tail.event_fingerprint != expected_tail_fingerprint
            ):
                raise ConcurrentModificationError(
                    "aggregate or event tail changed before recovery commit",
                    operation="project.recover.commit",
                    remediation="reload and replay before retrying recovery",
                    project_id=command.project_id,
                )

            position: int | None = None
            if recovered:
                assert event_id is not None
                state = replay.reconstructed_state
                unit_of_work.projects.save(
                    state,
                    expected_record_version=current.record_version,
                    expected_record_fingerprint=current.record_fingerprint,
                )
                event = _build_recovery_event(command, current, replay, event_id, now)
                position = unit_of_work.events.append(
                    event,
                    version_before=state.record_version,
                    version_after=state.record_version,
                    previous_event_fingerprint=expected_tail_fingerprint,
                )
            result = RecoverAggregateResult(
                project_id=command.project_id,
                recovered=recovered,
                replay_result=replay,
                before_record_version=before.record_version,
                before_record_fingerprint=before.record_fingerprint,
                before_content_fingerprint=before.content_fingerprint,
                after_record_version=replay.record_version,
                after_record_fingerprint=replay.record_fingerprint,
                after_content_fingerprint=replay.content_fingerprint,
                event_id=event_id,
                stream_position=position,
            )
            unit_of_work.idempotency.complete(_OPERATION, command.idempotency_key, result)
            unit_of_work.commit()
            return result

    @staticmethod
    def _require_expected(command: RecoverAggregateCommand, current: StoredProject) -> None:
        if (
            current.record_version != command.expected_record_version
            or current.record_fingerprint != command.expected_record_fingerprint
            or current.content_fingerprint != command.expected_content_fingerprint
        ):
            raise ConcurrentModificationError(
                "materialized aggregate differs from recovery preconditions",
                operation="project.recover.precondition",
                remediation="reload the aggregate before explicit recovery",
                project_id=command.project_id,
            )


def _build_recovery_event(
    command: RecoverAggregateCommand,
    before: StoredProject,
    replay: ReplayResult,
    event_id: EventId,
    now: datetime,
) -> EventEnvelope:
    state: ProjectState = replay.reconstructed_state
    stream_evidence = {
        "record_version": replay.record_version,
        "record_fingerprint": replay.record_fingerprint,
        "content_fingerprint": replay.content_fingerprint,
    }
    return build_project_event(
        event_id=event_id,
        event_type="project.aggregate.recovered",
        semantic_name="project.recovered",
        project_id=command.project_id,
        aggregate_version=state.contract_version,
        idempotency_key=command.idempotency_key,
        actor_type=command.actor_type,
        actor_id=command.actor_id,
        actor_display_name=command.actor_display_name,
        occurred_at=now,
        payload={
            "reason": command.reason,
            "stream_before": stream_evidence,
            "materialized_before": {
                "record_version": before.record_version,
                "record_fingerprint": before.record_fingerprint,
                "content_fingerprint": before.content_fingerprint,
            },
            "after": stream_evidence,
            "replay_event_count": replay.event_count,
            "replay_last_stream_position": replay.last_stream_position,
            "snapshot_disposition": replay.snapshot_disposition.value,
        },
    )


def recover_aggregate(
    service: RecoveryService, command: RecoverAggregateCommand
) -> RecoverAggregateResult:
    return service.recover_aggregate(command)
