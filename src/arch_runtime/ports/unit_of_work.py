"""Atomic persistence boundary protocol."""

from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from arch_runtime.ports.repositories import (
    EventStore,
    IdempotencyStore,
    ProjectRepository,
    SnapshotStore,
)


@runtime_checkable
class UnitOfWork(Protocol):
    projects: ProjectRepository
    events: EventStore
    snapshots: SnapshotStore
    idempotency: IdempotencyStore

    def __enter__(self) -> Self: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


@runtime_checkable
class UnitOfWorkFactory(Protocol):
    def __call__(self) -> UnitOfWork: ...
