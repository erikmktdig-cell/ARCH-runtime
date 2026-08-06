from __future__ import annotations

from arch_kernel.contracts import ProjectState

from arch_runtime.application import (
    CreateProjectCommand,
    CreateProjectService,
    CreateSnapshotCommand,
    RecoverAggregateCommand,
    RecoveryService,
    SnapshotPolicy,
    SnapshotService,
)
from arch_runtime.persistence.sqlite import SQLiteConfig, SQLiteUnitOfWork, migrate_schema
from arch_runtime.persistence.sqlite.connection import open_sqlite_connection
from arch_runtime.ports import StoredEvent, StoredProject, UnitOfWorkFactory
from arch_runtime.replay import ReplayService
from tests.application.conftest import EventIds, ProjectIds
from tests.application.test_recovery import RecoveryEventIds
from tests.application.test_snapshots import SnapshotIds
from tests.fakes.adapters import FrozenClock


def _initialize(config: SQLiteConfig, clock: FrozenClock) -> None:
    connection = open_sqlite_connection(config)
    try:
        migrate_schema(connection, clock=clock)
    finally:
        connection.close()


def _factory(config: SQLiteConfig, clock: FrozenClock) -> UnitOfWorkFactory:
    return lambda: SQLiteUnitOfWork(config, clock=clock)


def _seed(
    config: SQLiteConfig,
    clock: FrozenClock,
    command: CreateProjectCommand,
    project_ids: ProjectIds,
) -> tuple[UnitOfWorkFactory, StoredProject, StoredEvent]:
    factory = _factory(config, clock)
    created = CreateProjectService(
        unit_of_work_factory=factory,
        clock=clock,
        project_ids=project_ids,
        event_ids=EventIds(),
    ).create_project(command)
    with factory() as unit_of_work:
        stored = unit_of_work.projects.get(created.project_id)
        tail = unit_of_work.events.read_stream(created.project_id)[-1]
    assert stored is not None
    return factory, stored, tail


def test_snapshot_and_explicit_recovery_execute_against_sqlite(
    sqlite_config: SQLiteConfig,
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
) -> None:
    _initialize(sqlite_config, frozen_clock)
    factory, stored, tail = _seed(sqlite_config, frozen_clock, create_command, project_ids)
    replay = ReplayService(unit_of_work_factory=factory)
    snapshot = SnapshotService(
        unit_of_work_factory=factory,
        replay_service=replay,
        clock=frozen_clock,
        snapshot_ids=SnapshotIds(),
        policy=SnapshotPolicy(every_n_versions=1),
    ).create_snapshot(
        CreateSnapshotCommand(
            project_id=stored.project_id,
            expected_record_version=stored.record_version,
            expected_record_fingerprint=stored.record_fingerprint,
            expected_content_fingerprint=stored.content_fingerprint,
            expected_stream_position=tail.stream_position,
            expected_stream_fingerprint=tail.event_fingerprint,
        )
    )
    assert snapshot.created

    state = ProjectState.model_validate_json(stored.state_json, strict=True)
    divergent = state.model_copy(
        update={"metadata": state.metadata.model_copy(update={"name": "SQLite divergence"})}
    )
    with factory() as unit_of_work:
        unit_of_work.projects.save(
            divergent,
            expected_record_version=stored.record_version,
            expected_record_fingerprint=stored.record_fingerprint,
        )
        unit_of_work.commit()
    with factory() as unit_of_work:
        current = unit_of_work.projects.get(stored.project_id)
    assert current is not None

    recovered = RecoveryService(
        unit_of_work_factory=factory,
        replay_service=replay,
        clock=frozen_clock,
        event_ids=RecoveryEventIds(),
    ).recover_aggregate(
        RecoverAggregateCommand(
            idempotency_key="project:recover:sqlite",
            project_id=stored.project_id,
            expected_record_version=current.record_version,
            expected_record_fingerprint=current.record_fingerprint,
            expected_content_fingerprint=current.content_fingerprint,
            reason="Restore SQLite materialized evidence.",
            actor_id="operator:sqlite",
        )
    )

    assert recovered.recovered
    verified = replay.replay_project(stored.project_id)
    assert verified.record_fingerprint == stored.record_fingerprint
    with factory() as unit_of_work:
        events = unit_of_work.events.read_stream(stored.project_id)
        snapshots = unit_of_work.snapshots.list_for_project(stored.project_id)
    assert events[-1].event_type == "project.aggregate.recovered"
    assert snapshots[0].snapshot_id == snapshot.snapshot_id
