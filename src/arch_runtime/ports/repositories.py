"""Infrastructure-neutral persistence protocols."""

from typing import Protocol, runtime_checkable

from arch_kernel.contracts import EventEnvelope, ProjectId, ProjectState

from arch_runtime.ports.storage import IdempotencyRecord, StoredEvent, StoredProject, StoredSnapshot


@runtime_checkable
class ProjectRepository(Protocol):
    def get(self, project_id: ProjectId) -> StoredProject | None: ...

    def add(self, state: ProjectState) -> None: ...

    def save(
        self,
        state: ProjectState,
        *,
        expected_record_version: int,
        expected_record_fingerprint: str,
    ) -> None: ...


@runtime_checkable
class EventStore(Protocol):
    def append(
        self,
        event: EventEnvelope,
        *,
        version_before: int,
        version_after: int,
        previous_event_fingerprint: str | None,
    ) -> int: ...

    def read_stream(
        self,
        project_id: ProjectId,
        *,
        after_position: int = 0,
    ) -> tuple[StoredEvent, ...]: ...


@runtime_checkable
class SnapshotStore(Protocol):
    def get_latest(self, project_id: ProjectId) -> StoredSnapshot | None: ...

    def save(self, snapshot: StoredSnapshot) -> None: ...


@runtime_checkable
class IdempotencyStore(Protocol):
    def get(self, operation_name: str, key: str) -> IdempotencyRecord | None: ...

    def reserve(self, record: IdempotencyRecord) -> None: ...

    def complete(self, operation_name: str, key: str, result: object) -> None: ...
