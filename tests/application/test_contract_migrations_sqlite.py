from __future__ import annotations

from arch_kernel.contracts import EventEnvelope, ProjectState
from arch_kernel.kernel import canonicalize_json, compute_content_fingerprint, compute_fingerprint

from arch_runtime.application import (
    ApplyStoredContractMigrationCommand,
    CreateProjectCommand,
    CreateProjectService,
    PlanStoredContractMigrationCommand,
    StoredContractMigrationService,
)
from arch_runtime.persistence.sqlite import (
    SQLiteConfig,
    SQLiteUnitOfWork,
    migrate_schema,
    open_sqlite_connection,
)
from arch_runtime.ports import StoredSnapshot, UnitOfWorkFactory
from arch_runtime.replay import ReplayService
from tests.application.conftest import EventIds, ProjectIds
from tests.application.test_contract_migrations import (
    EVENT_CURRENT,
    OLD,
    PROJECT_CURRENT,
    MigrationEventIds,
    _registries,
)
from tests.fakes.adapters import FrozenClock


def _factory(config: SQLiteConfig, clock: FrozenClock) -> UnitOfWorkFactory:
    return lambda: SQLiteUnitOfWork(config, clock=clock)


def test_sqlite_migrates_contracts_without_rewriting_historical_event_rows(
    sqlite_config: SQLiteConfig,
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
) -> None:
    connection = open_sqlite_connection(sqlite_config)
    try:
        migrate_schema(connection, clock=frozen_clock)
    finally:
        connection.close()
    factory = _factory(sqlite_config, frozen_clock)
    created = CreateProjectService(
        unit_of_work_factory=factory,
        clock=frozen_clock,
        project_ids=project_ids,
        event_ids=EventIds(),
    ).create_project(create_command)
    contracts, migrations, project_schema, _ = _registries()
    with factory() as unit_of_work:
        aggregate = unit_of_work.projects.get(created.project_id)
        event = unit_of_work.events.read_stream(created.project_id)[0]
        assert aggregate is not None
        unit_of_work.snapshots.save(
            StoredSnapshot(
                snapshot_id="SNP-01HZX7M3FQ1T2Q9V8Y6K4C2R09",
                project_id=created.project_id,
                aggregate_version=aggregate.record_version,
                last_stream_position=event.stream_position,
                contract_name=aggregate.contract_name,
                contract_version=aggregate.contract_version,
                schema_fingerprint=aggregate.schema_fingerprint,
                record_fingerprint=aggregate.record_fingerprint,
                content_fingerprint=aggregate.content_fingerprint,
                state_json=aggregate.state_json,
                created_at=aggregate.updated_at,
            )
        )
        unit_of_work.commit()

    state = ProjectState.model_validate_json(aggregate.state_json, strict=True)
    old_state = state.model_copy(update={"contract_version": OLD})
    envelope = EventEnvelope.model_validate_json(event.event_json, strict=True)
    old_envelope = envelope.model_copy(update={"schema_version": OLD})
    old_event_json = canonicalize_json(old_envelope)
    old_event_fingerprint = compute_fingerprint(old_envelope)
    connection = open_sqlite_connection(sqlite_config)
    try:
        connection.execute(
            "UPDATE project_aggregates SET contract_version=?, schema_fingerprint=?, "
            "record_fingerprint=?, content_fingerprint=?, state_json=? WHERE project_id=?",
            (
                str(OLD),
                project_schema,
                compute_fingerprint(old_state),
                compute_content_fingerprint(old_state),
                canonicalize_json(old_state),
                str(created.project_id),
            ),
        )
        connection.execute(
            "UPDATE project_snapshots SET contract_version=?, schema_fingerprint=?, "
            "record_fingerprint=?, content_fingerprint=?, state_json=? WHERE project_id=?",
            (
                str(OLD),
                project_schema,
                compute_fingerprint(old_state),
                compute_content_fingerprint(old_state),
                canonicalize_json(old_state),
                str(created.project_id),
            ),
        )
        connection.execute(
            "UPDATE project_events SET schema_version=?, event_fingerprint=?, event_json=? "
            "WHERE event_id=?",
            (str(OLD), old_event_fingerprint, old_event_json, str(event.event_id)),
        )
    finally:
        connection.close()

    with factory() as unit_of_work:
        historical = unit_of_work.projects.get_stored(created.project_id)
        tail = unit_of_work.events.read_stored_stream(created.project_id)[-1]
    assert historical is not None
    plan = PlanStoredContractMigrationCommand(
        project_id=created.project_id,
        target_project_version=PROJECT_CURRENT,
        target_event_version=EVENT_CURRENT,
        expected_record_version=historical.record_version,
        expected_record_fingerprint=historical.record_fingerprint,
        expected_content_fingerprint=historical.content_fingerprint,
        expected_stream_position=tail.stream_position,
        expected_stream_fingerprint=tail.event_fingerprint,
    )
    replay = ReplayService(unit_of_work_factory=factory)
    service = StoredContractMigrationService(
        unit_of_work_factory=factory,
        clock=frozen_clock,
        event_ids=MigrationEventIds(),
        contract_registry=contracts,
        migration_registry=migrations,
        replay_verifier=replay,
    )
    dry_run = service.dry_run(plan)
    result = service.apply(
        ApplyStoredContractMigrationCommand(
            idempotency_key="migration:sqlite",
            plan=plan,
            expected_plan_fingerprint=dry_run.plan_fingerprint,
            reason="Upgrade SQLite historical fixture.",
            actor_id="operator:sqlite",
        )
    )

    assert result.success
    with factory() as unit_of_work:
        current = unit_of_work.projects.get(created.project_id)
        events = unit_of_work.events.read_stored_stream(created.project_id)
        snapshots = unit_of_work.snapshots.list_for_project(created.project_id)
    assert current is not None
    assert current.contract_version == PROJECT_CURRENT
    assert snapshots[0].contract_version == PROJECT_CURRENT
    assert events[0].event_json == old_event_json
    assert events[0].event_fingerprint == old_event_fingerprint
    assert events[-1].event_type == "project.aggregate.contract_migrated"
    replay.replay_project(created.project_id)
