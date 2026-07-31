from datetime import datetime

import pytest

from arch_runtime.ports import (
    Clock,
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

pytestmark = pytest.mark.contract


def test_fakes_satisfy_runtime_checkable_ports(frozen_clock: FrozenClock) -> None:
    assert isinstance(InMemoryProjectRepository(), ProjectRepository)
    assert isinstance(InMemoryEventStore(), EventStore)
    assert isinstance(InMemorySnapshotStore(), SnapshotStore)
    assert isinstance(InMemoryIdempotencyStore(frozen_clock), IdempotencyStore)
    assert isinstance(FakeUnitOfWork(frozen_clock), UnitOfWork)
    assert isinstance(frozen_clock, Clock)
    assert isinstance(frozen_clock.now(), datetime)
