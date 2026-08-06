from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from types import TracebackType

from arch_kernel.contracts import EventEnvelope, ProjectId, ProjectState
from arch_kernel.kernel import canonicalize_json, compute_content_fingerprint, compute_fingerprint

from arch_runtime.errors import (
    ConcurrentModificationError,
    IdempotencyConflictError,
    PersistenceError,
    ProjectAlreadyExistsError,
    ProjectNotFoundError,
)
from arch_runtime.ports.repositories import (
    EventStore,
    IdempotencyStore,
    ProjectRepository,
    SnapshotStore,
)
from arch_runtime.ports.storage import (
    Fingerprint,
    IdempotencyRecord,
    IdempotencyStatus,
    SnapshotId,
    StoredEvent,
    StoredProject,
    StoredSnapshot,
)

SCHEMA_FINGERPRINT: Fingerprint = "sha256:" + "a" * 64


class FrozenClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


def stored_project_from_state(state: ProjectState) -> StoredProject:
    from arch_kernel.kernel import build_builtin_contract_registry

    registration = build_builtin_contract_registry().get_by_model(ProjectState)
    assert registration is not None
    updated_at = state.updated_at or state.created_at
    return StoredProject(
        project_id=state.metadata.project_id,
        record_version=state.record_version,
        contract_name=state.contract_name,
        contract_version=state.contract_version,
        schema_fingerprint=registration.descriptor.schema_fingerprint,
        record_fingerprint=compute_fingerprint(state),
        content_fingerprint=compute_content_fingerprint(state),
        state_json=canonicalize_json(state),
        created_at=state.created_at,
        updated_at=updated_at,
    )


class InMemoryProjectRepository:
    def __init__(self) -> None:
        self.records: dict[ProjectId, StoredProject] = {}

    def get(self, project_id: ProjectId) -> StoredProject | None:
        return self.records.get(project_id)

    def add(self, state: ProjectState) -> None:
        project_id = state.metadata.project_id
        if project_id in self.records:
            raise ProjectAlreadyExistsError(
                "project already exists",
                operation="project.add",
                remediation="load the existing project",
                project_id=project_id,
            )
        self.records[project_id] = stored_project_from_state(state)

    def save(
        self,
        state: ProjectState,
        *,
        expected_record_version: int,
        expected_record_fingerprint: str,
    ) -> None:
        project_id = state.metadata.project_id
        current = self.records.get(project_id)
        if current is None:
            raise ProjectNotFoundError(
                "project not found",
                operation="project.save",
                remediation="create the project first",
                project_id=project_id,
            )
        if (
            current.record_version != expected_record_version
            or current.record_fingerprint != expected_record_fingerprint
        ):
            raise ConcurrentModificationError(
                "project changed concurrently",
                operation="project.save",
                remediation="reload and reevaluate the command",
                project_id=project_id,
            )
        self.records[project_id] = stored_project_from_state(state)


class InMemoryEventStore:
    def __init__(self) -> None:
        self.records: list[StoredEvent] = []

    def append(
        self,
        event: EventEnvelope,
        *,
        version_before: int,
        version_after: int,
        previous_event_fingerprint: str | None,
    ) -> int:
        project_stream = [
            record for record in self.records if record.project_id == event.project_id
        ]
        actual_tail = project_stream[-1].event_fingerprint if project_stream else None
        if previous_event_fingerprint != actual_tail:
            raise ConcurrentModificationError(
                "project event tail changed concurrently",
                operation="event.append",
                remediation="reload the project stream tail",
                project_id=event.project_id,
            )
        position = len(self.records) + 1
        self.records.append(
            StoredEvent(
                stream_position=position,
                event_id=event.event_id,
                project_id=event.project_id,
                aggregate_version_before=version_before,
                aggregate_version_after=version_after,
                is_state_change=version_after != version_before,
                event_type=event.event_type,
                schema_version=event.schema_version,
                idempotency_key=event.idempotency_key,
                event_fingerprint=compute_fingerprint(event),
                previous_event_fingerprint=previous_event_fingerprint,
                event_json=canonicalize_json(event),
                recorded_at=event.recorded_at,
            )
        )
        return position

    def read_stream(
        self,
        project_id: ProjectId,
        *,
        after_position: int = 0,
    ) -> tuple[StoredEvent, ...]:
        return tuple(
            record
            for record in self.records
            if record.project_id == project_id and record.stream_position > after_position
        )


class InMemorySnapshotStore:
    def __init__(self) -> None:
        self.records: dict[ProjectId, list[StoredSnapshot]] = {}

    def get_latest(self, project_id: ProjectId) -> StoredSnapshot | None:
        snapshots = self.list_for_project(project_id)
        return snapshots[0] if snapshots else None

    def list_for_project(self, project_id: ProjectId) -> tuple[StoredSnapshot, ...]:
        return tuple(
            sorted(
                self.records.get(project_id, []),
                key=lambda value: (
                    value.aggregate_version,
                    value.created_at,
                    value.snapshot_id,
                ),
                reverse=True,
            )
        )

    def save(self, snapshot: StoredSnapshot) -> None:
        snapshots = self.records.setdefault(snapshot.project_id, [])
        if any(item.aggregate_version == snapshot.aggregate_version for item in snapshots):
            raise PersistenceError(
                "snapshot version already exists",
                operation="snapshot.save",
                remediation="load the existing snapshot",
                project_id=snapshot.project_id,
            )
        snapshots.append(snapshot)

    def delete(self, project_id: ProjectId, snapshot_id: SnapshotId) -> None:
        snapshots = self.records.get(project_id, [])
        for index, snapshot in enumerate(snapshots):
            if snapshot.snapshot_id == snapshot_id:
                snapshots.pop(index)
                return
        raise PersistenceError(
            "snapshot not found",
            operation="snapshot.delete",
            remediation="reload snapshot retention evidence",
            project_id=project_id,
        )


class InMemoryIdempotencyStore:
    def __init__(self, clock: FrozenClock) -> None:
        self.clock = clock
        self.records: dict[tuple[str, str], IdempotencyRecord] = {}

    def get(self, operation_name: str, key: str) -> IdempotencyRecord | None:
        return self.records.get((operation_name, key))

    def reserve(self, record: IdempotencyRecord) -> None:
        identity = (record.operation_name, record.idempotency_key)
        if identity in self.records:
            raise IdempotencyConflictError(
                "idempotency key is already reserved",
                operation="idempotency.reserve",
                remediation="load and compare the existing reservation",
                project_id=record.project_id,
            )
        self.records[identity] = record

    def complete(self, operation_name: str, key: str, result: object) -> None:
        identity = (operation_name, key)
        current = self.records.get(identity)
        if current is None:
            raise PersistenceError(
                "idempotency reservation not found",
                operation="idempotency.complete",
                remediation="reserve the key in the active transaction",
            )
        result_json = canonicalize_json(result)
        self.records[identity] = IdempotencyRecord(
            operation_name=current.operation_name,
            idempotency_key=current.idempotency_key,
            project_id=current.project_id,
            request_fingerprint=current.request_fingerprint,
            status=IdempotencyStatus.COMPLETED,
            result_fingerprint=compute_fingerprint(result),
            result_json=result_json,
            created_at=current.created_at,
            completed_at=self.clock.now(),
        )


class FakeUnitOfWork:
    def __init__(self, clock: FrozenClock) -> None:
        self._projects = InMemoryProjectRepository()
        self._events = InMemoryEventStore()
        self._snapshots = InMemorySnapshotStore()
        self._idempotency = InMemoryIdempotencyStore(clock)
        self.projects: ProjectRepository = self._projects
        self.events: EventStore = self._events
        self.snapshots: SnapshotStore = self._snapshots
        self.idempotency: IdempotencyStore = self._idempotency
        self.committed = False
        self.rolled_back = False
        self._checkpoint: tuple[object, ...] | None = None

    def __enter__(self) -> FakeUnitOfWork:
        self.committed = False
        self.rolled_back = False
        self._checkpoint = (
            deepcopy(self._projects.records),
            deepcopy(self._events.records),
            deepcopy(self._snapshots.records),
            deepcopy(self._idempotency.records),
        )
        return self

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        if self._checkpoint is not None:
            projects, events, snapshots, idempotency = self._checkpoint
            self._projects.records = projects  # type: ignore[assignment]
            self._events.records = events  # type: ignore[assignment]
            self._snapshots.records = snapshots  # type: ignore[assignment]
            self._idempotency.records = idempotency  # type: ignore[assignment]
        self.rolled_back = True

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None or not self.committed:
            self.rollback()
