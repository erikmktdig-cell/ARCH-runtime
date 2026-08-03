"""Injectable identity generation ports."""

from typing import Protocol, runtime_checkable

from arch_kernel.contracts import EventId, ProjectId


@runtime_checkable
class ProjectIdGenerator(Protocol):
    def new(self) -> ProjectId: ...


@runtime_checkable
class EventIdGenerator(Protocol):
    def new(self) -> EventId: ...
