"""Infrastructure-neutral runtime contracts and ports."""

from arch_runtime.ports.clock import Clock
from arch_runtime.ports.generation import EventIdGenerator, ProjectIdGenerator, SnapshotIdGenerator
from arch_runtime.ports.repositories import (
    EventStore,
    IdempotencyStore,
    ProjectRepository,
    SnapshotStore,
)
from arch_runtime.ports.storage import (
    IdempotencyRecord,
    IdempotencyStatus,
    StoredEvent,
    StoredProject,
    StoredSnapshot,
)
from arch_runtime.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory

__all__ = (
    "Clock",
    "EventIdGenerator",
    "EventStore",
    "IdempotencyRecord",
    "IdempotencyStatus",
    "IdempotencyStore",
    "ProjectIdGenerator",
    "ProjectRepository",
    "SnapshotIdGenerator",
    "SnapshotStore",
    "StoredEvent",
    "StoredProject",
    "StoredSnapshot",
    "UnitOfWork",
    "UnitOfWorkFactory",
)
