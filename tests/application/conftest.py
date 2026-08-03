from pathlib import Path

import pytest
from arch_kernel.contracts import EventId, ProjectId, Route

from arch_runtime.application import CreateProjectCommand
from arch_runtime.persistence.sqlite import SQLiteConfig


class ProjectIds:
    def __init__(self) -> None:
        self.calls = 0

    def new(self) -> ProjectId:
        self.calls += 1
        return ProjectId("PRJ-01HZX7M3FQ1T2Q9V8Y6K4C2R05")


class EventIds:
    def __init__(self) -> None:
        self.calls = 0

    def new(self) -> EventId:
        self.calls += 1
        return EventId("EVT-01HZX7M3FQ1T2Q9V8Y6K4C2R05")


@pytest.fixture
def project_ids() -> ProjectIds:
    return ProjectIds()


@pytest.fixture
def event_ids() -> EventIds:
    return EventIds()


@pytest.fixture
def create_command() -> CreateProjectCommand:
    return CreateProjectCommand.model_validate(
        {
            "idempotency_key": " project:create:001 ",
            "name": " Runtime Pilot ",
            "slug": " Runtime-Pilot ",
            "summary": " First persisted project. ",
            "owner": " platform-team ",
            "project_type": " platform ",
            "criticality": "high",
            "default_route": Route.STANDARD,
            "objectives": (" Prove the vertical slice. ",),
            "constraints": (" Keep infrastructure separate. ",),
            "success_criteria": (" Replay the exact result. ",),
            "actor_id": " user:erik ",
            "actor_display_name": " Erik ",
        },
        strict=True,
    )


@pytest.fixture
def sqlite_config(tmp_path: Path) -> SQLiteConfig:
    return SQLiteConfig(tmp_path / "runtime.db", busy_timeout_ms=2_500)


__all__ = ("EventIds", "ProjectIds")
