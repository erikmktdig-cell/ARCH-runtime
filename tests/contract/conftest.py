import pytest

from arch_runtime.ports import (
    EventStore,
    IdempotencyStore,
    ProjectRepository,
    SnapshotStore,
    UnitOfWork,
)
from tests.fakes.adapters import (
    FakeUnitOfWork,
    FrozenClock,
    InMemoryEventStore,
    InMemoryIdempotencyStore,
    InMemoryProjectRepository,
    InMemorySnapshotStore,
)


@pytest.fixture
def project_repository() -> ProjectRepository:
    return InMemoryProjectRepository()


@pytest.fixture
def event_store() -> EventStore:
    return InMemoryEventStore()


@pytest.fixture
def snapshot_store() -> SnapshotStore:
    return InMemorySnapshotStore()


@pytest.fixture
def idempotency_store(frozen_clock: FrozenClock) -> IdempotencyStore:
    return InMemoryIdempotencyStore(frozen_clock)


@pytest.fixture
def unit_of_work(frozen_clock: FrozenClock) -> UnitOfWork:
    return FakeUnitOfWork(frozen_clock)
