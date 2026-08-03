import pytest
from arch_kernel.contracts import EventEnvelope, ProjectState
from arch_kernel.kernel import (
    build_builtin_contract_registry,
    canonicalize_json,
    compute_content_fingerprint,
    compute_fingerprint,
)

from arch_runtime.errors import (
    PersistenceError,
    RuntimeConfigurationError,
    RuntimeSchemaMigrationRequiredError,
)
from arch_runtime.persistence.sqlite import (
    SQLiteConfig,
    SQLiteUnitOfWork,
    migrate_schema,
    open_sqlite_connection,
)
from arch_runtime.ports import IdempotencyRecord, IdempotencyStatus, StoredSnapshot
from tests.fakes.adapters import FrozenClock

pytestmark = pytest.mark.sqlite


def _initialize(sqlite_config: SQLiteConfig, clock: FrozenClock) -> None:
    connection = open_sqlite_connection(sqlite_config)
    try:
        migrate_schema(connection, clock=clock)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "table", ["project_aggregates", "project_events", "project_snapshots", "idempotency_records"]
)
def test_fault_at_each_write_boundary_rolls_back_the_transaction(
    sqlite_config: SQLiteConfig,
    frozen_clock: FrozenClock,
    project_state: ProjectState,
    event_envelope: EventEnvelope,
    table: str,
) -> None:
    _initialize(sqlite_config, frozen_clock)
    connection = open_sqlite_connection(sqlite_config)
    try:
        connection.execute(
            f"CREATE TRIGGER fail_write BEFORE INSERT ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'injected fault'); END"
        )
    finally:
        connection.close()

    def execute_fault() -> None:
        with SQLiteUnitOfWork(sqlite_config, clock=frozen_clock) as uow:
            if table == "project_aggregates":
                uow.projects.add(project_state)
                return
            uow.projects.add(project_state)
            if table == "project_events":
                uow.events.append(
                    event_envelope,
                    version_before=1,
                    version_after=2,
                    previous_event_fingerprint=None,
                )
            if table == "project_snapshots":
                registration = build_builtin_contract_registry().get_by_model(ProjectState)
                assert registration is not None
                uow.snapshots.save(
                    StoredSnapshot(
                        snapshot_id="SNP-01HZX7M3FQ1T2Q9V8Y6K4C2B1A",
                        project_id=project_state.metadata.project_id,
                        aggregate_version=project_state.record_version,
                        last_stream_position=1,
                        contract_name=project_state.contract_name,
                        contract_version=project_state.contract_version,
                        schema_fingerprint=registration.descriptor.schema_fingerprint,
                        record_fingerprint=compute_fingerprint(project_state),
                        content_fingerprint=compute_content_fingerprint(project_state),
                        state_json=canonicalize_json(project_state),
                        created_at=project_state.created_at,
                    )
                )
            if table == "idempotency_records":
                uow.idempotency.reserve(
                    IdempotencyRecord(
                        operation_name="project.create",
                        idempotency_key="create:001",
                        project_id=project_state.metadata.project_id,
                        request_fingerprint="sha256:" + "b" * 64,
                        status=IdempotencyStatus.IN_PROGRESS,
                        created_at=project_state.created_at,
                    )
                )

    with pytest.raises(PersistenceError):
        execute_fault()
    verifier = open_sqlite_connection(sqlite_config)
    try:
        count = verifier.execute("SELECT count(*) FROM project_aggregates").fetchone()[0]
        assert count == 0
    finally:
        verifier.close()


def test_uow_requires_prepared_schema(
    sqlite_config: SQLiteConfig, frozen_clock: FrozenClock
) -> None:
    def enter_unprepared() -> None:
        with SQLiteUnitOfWork(sqlite_config, clock=frozen_clock):
            pass

    with pytest.raises(RuntimeSchemaMigrationRequiredError):
        enter_unprepared()


def test_uow_rejects_use_outside_context(
    sqlite_config: SQLiteConfig, frozen_clock: FrozenClock
) -> None:
    uow = SQLiteUnitOfWork(sqlite_config, clock=frozen_clock)
    with pytest.raises(RuntimeConfigurationError):
        uow.commit()


def test_aggregate_event_and_idempotency_commit_atomically(
    sqlite_config: SQLiteConfig,
    frozen_clock: FrozenClock,
    project_state: ProjectState,
    event_envelope: EventEnvelope,
) -> None:
    _initialize(sqlite_config, frozen_clock)
    reservation = IdempotencyRecord(
        operation_name="project.create",
        idempotency_key="create:atomic",
        project_id=project_state.metadata.project_id,
        request_fingerprint="sha256:" + "e" * 64,
        status=IdempotencyStatus.IN_PROGRESS,
        created_at=project_state.created_at,
    )
    with SQLiteUnitOfWork(sqlite_config, clock=frozen_clock) as uow:
        uow.projects.add(project_state)
        uow.events.append(
            event_envelope,
            version_before=0,
            version_after=1,
            previous_event_fingerprint=None,
        )
        uow.idempotency.reserve(reservation)
        uow.idempotency.complete(
            reservation.operation_name,
            reservation.idempotency_key,
            {"accepted": True},
        )
        uow.commit()

    verifier = open_sqlite_connection(sqlite_config)
    try:
        counts = tuple(
            verifier.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("project_aggregates", "project_events", "idempotency_records")
        )
        status = verifier.execute("SELECT status FROM idempotency_records").fetchone()[0]
    finally:
        verifier.close()
    assert counts == (1, 1, 1)
    assert status == "completed"
