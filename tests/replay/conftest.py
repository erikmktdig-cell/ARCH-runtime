from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from arch_kernel.contracts import ProjectId, ProjectState, Route
from arch_kernel.kernel import build_builtin_contract_registry

from arch_runtime.application import CreateProjectCommand, CreateProjectService
from arch_runtime.persistence.sqlite import SQLiteConfig
from arch_runtime.ports import StoredProject, StoredSnapshot
from tests.application.conftest import EventIds, ProjectIds
from tests.application.test_apply_transition import (
    TransitionEventIds,
    _command,
    _definition,
    _service,
)
from tests.fakes.adapters import FakeUnitOfWork, FrozenClock


@dataclass(frozen=True, slots=True)
class ReplaySeed:
    uow: FakeUnitOfWork
    project_id: ProjectId
    created: StoredProject
    current: StoredProject


@pytest.fixture
def project_ids() -> ProjectIds:
    return ProjectIds()


@pytest.fixture
def create_command() -> CreateProjectCommand:
    return CreateProjectCommand.model_validate(
        {
            "idempotency_key": "project:create:replay",
            "name": "Replay Pilot",
            "slug": "replay-pilot",
            "summary": "Replay integrity fixture.",
            "owner": "platform-team",
            "project_type": "platform",
            "criticality": "high",
            "default_route": Route.STANDARD,
            "objectives": ("Verify deterministic replay.",),
            "constraints": ("Never repair evidence.",),
            "success_criteria": ("Reconstruct exact fingerprints.",),
            "actor_id": "user:replay",
        },
        strict=True,
    )


@pytest.fixture
def sqlite_config(tmp_path: Path) -> SQLiteConfig:
    return SQLiteConfig(tmp_path / "replay.db", busy_timeout_ms=2_500)


@pytest.fixture
def replay_seed(
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
) -> ReplaySeed:
    uow = FakeUnitOfWork(frozen_clock)
    create_result = CreateProjectService(
        unit_of_work_factory=lambda: uow,
        clock=frozen_clock,
        project_ids=project_ids,
        event_ids=EventIds(),
    ).create_project(create_command)
    created = uow.projects.get(create_result.project_id)
    assert created is not None
    registration = build_builtin_contract_registry().get_by_model(ProjectState)
    assert registration is not None
    created = created.model_copy(
        update={"schema_fingerprint": registration.descriptor.schema_fingerprint}
    )
    uow._projects.records[create_result.project_id] = created
    state = ProjectState.model_validate_json(created.state_json, strict=True)
    transition = _service(uow, frozen_clock, _definition(), TransitionEventIds())
    transition.apply_transition(_command(state, created))
    current = uow.projects.get(create_result.project_id)
    assert current is not None
    current = current.model_copy(
        update={"schema_fingerprint": registration.descriptor.schema_fingerprint}
    )
    uow._projects.records[create_result.project_id] = current
    return ReplaySeed(uow, create_result.project_id, created, current)


def save_snapshot(seed: ReplaySeed, *, valid: bool = True) -> StoredSnapshot:
    registration = build_builtin_contract_registry().get_by_model(ProjectState)
    assert registration is not None
    tail = seed.uow.events.read_stream(seed.project_id)[-1]
    snapshot = StoredSnapshot(
        snapshot_id="SNP-01HZX7M3FQ1T2Q9V8Y6K4C2R07",
        project_id=seed.project_id,
        aggregate_version=seed.current.record_version,
        last_stream_position=tail.stream_position if valid else tail.stream_position + 100,
        contract_name=seed.current.contract_name,
        contract_version=seed.current.contract_version,
        schema_fingerprint=registration.descriptor.schema_fingerprint,
        record_fingerprint=seed.current.record_fingerprint,
        content_fingerprint=seed.current.content_fingerprint,
        state_json=seed.current.state_json,
        created_at=seed.current.updated_at,
    )
    seed.uow.snapshots.save(snapshot)
    return snapshot


__all__ = ("ReplaySeed", "save_snapshot")
