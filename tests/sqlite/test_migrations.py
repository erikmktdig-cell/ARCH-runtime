import sqlite3
from dataclasses import FrozenInstanceError
from typing import Any, cast

import pytest

from arch_runtime.errors import (
    PersistenceError,
    RuntimeConfigurationError,
    RuntimeSchemaMigrationRequiredError,
    UnsupportedRuntimeSchemaError,
)
from arch_runtime.persistence.sqlite import (
    SchemaStatus,
    SqlMigration,
    inspect_schema,
    load_sql_migrations,
    migrate_schema,
    require_current_schema,
)
from tests.fakes.adapters import FrozenClock

pytestmark = pytest.mark.sqlite


def test_packaged_migrations_are_contiguous_immutable_and_hashed() -> None:
    migrations = load_sql_migrations()
    assert [migration.version for migration in migrations] == [1, 2]
    assert [migration.name for migration in migrations] == ["initial_tables", "initial_indexes"]
    assert all(migration.checksum.startswith("sha256:") for migration in migrations)
    assert all(len(migration.checksum) == 71 for migration in migrations)
    with pytest.raises(FrozenInstanceError):
        cast(Any, migrations[0]).name = "changed"


def test_empty_schema_migrates_to_current_atomically(
    sqlite_connection: sqlite3.Connection,
    frozen_clock: FrozenClock,
) -> None:
    before = inspect_schema(sqlite_connection)
    after = migrate_schema(sqlite_connection, clock=frozen_clock)
    validated = require_current_schema(sqlite_connection)

    assert before.status is SchemaStatus.EMPTY
    assert after.status is SchemaStatus.CURRENT
    assert after.current_version == after.latest_version == 2
    assert validated == after
    applied = sqlite_connection.execute(
        "SELECT version, applied_at FROM runtime_schema_migrations ORDER BY version"
    ).fetchall()
    assert [(row[0], row[1]) for row in applied] == [
        (1, "2026-07-31T12:00:00Z"),
        (2, "2026-07-31T12:00:00Z"),
    ]


def test_schema_contains_all_r00_tables_and_indexes(
    sqlite_connection: sqlite3.Connection,
    frozen_clock: FrozenClock,
) -> None:
    migrate_schema(sqlite_connection, clock=frozen_clock)
    objects = {
        (row[0], row[1])
        for row in sqlite_connection.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' AND type IN ('table', 'index')"
        )
    }
    assert {
        ("table", "runtime_schema_migrations"),
        ("table", "project_aggregates"),
        ("table", "project_events"),
        ("table", "project_snapshots"),
        ("table", "idempotency_records"),
        ("index", "uq_project_events_state_version"),
        ("index", "uq_project_snapshots_project_version"),
    } <= objects


def test_schema_enforces_event_and_idempotency_invariants(
    sqlite_connection: sqlite3.Connection,
    frozen_clock: FrozenClock,
) -> None:
    migrate_schema(sqlite_connection, clock=frozen_clock)
    sqlite_connection.execute(
        "INSERT INTO project_aggregates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "PRJ-01HZX7M3FQ1T2Q9V8Y6K4C2B1A",
            1,
            "project_state",
            "1.0.0",
            "sha256:" + "a" * 64,
            "sha256:" + "b" * 64,
            "sha256:" + "c" * 64,
            b"{}",
            "2026-08-01T00:00:00Z",
            "2026-08-01T00:00:00Z",
        ),
    )
    invalid_event = (
        "EVT-01HZX7M3FQ1T2Q9V8Y6K4C2B1G",
        "PRJ-01HZX7M3FQ1T2Q9V8Y6K4C2B1A",
        1,
        1,
        1,
        "project.transition_applied",
        "1.0.0",
        "transition:001",
        "sha256:" + "d" * 64,
        None,
        b"{}",
        "2026-08-01T00:00:01Z",
    )
    with pytest.raises(sqlite3.IntegrityError):
        sqlite_connection.execute(
            "INSERT INTO project_events "
            "(event_id, project_id, aggregate_version_before, aggregate_version_after, "
            "is_state_change, event_type, schema_version, idempotency_key, "
            "event_fingerprint, previous_event_fingerprint, event_json, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            invalid_event,
        )
    with pytest.raises(sqlite3.IntegrityError):
        sqlite_connection.execute(
            "INSERT INTO idempotency_records "
            "(operation_name, idempotency_key, request_fingerprint, status, created_at) "
            "VALUES (?, ?, ?, 'completed', ?)",
            ("project.create", "create:001", "sha256:" + "e" * 64, "2026-08-01T00:00:00Z"),
        )


def test_state_change_version_index_is_unique_per_project(
    sqlite_connection: sqlite3.Connection,
    frozen_clock: FrozenClock,
) -> None:
    migrate_schema(sqlite_connection, clock=frozen_clock)
    sqlite_connection.execute(
        "INSERT INTO project_aggregates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "PRJ-01HZX7M3FQ1T2Q9V8Y6K4C2B1A",
            1,
            "project_state",
            "1.0.0",
            "sha256:" + "a" * 64,
            "sha256:" + "b" * 64,
            "sha256:" + "c" * 64,
            b"{}",
            "2026-08-01T00:00:00Z",
            "2026-08-01T00:00:00Z",
        ),
    )
    values = (
        "PRJ-01HZX7M3FQ1T2Q9V8Y6K4C2B1A",
        0,
        1,
        1,
        "project.created",
        "1.0.0",
        "create:001",
        None,
        b"{}",
        "2026-08-01T00:00:01Z",
    )
    statement = (
        "INSERT INTO project_events "
        "(event_id, project_id, aggregate_version_before, aggregate_version_after, "
        "is_state_change, event_type, schema_version, idempotency_key, "
        "event_fingerprint, previous_event_fingerprint, event_json, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    sqlite_connection.execute(
        statement,
        ("EVT-01HZX7M3FQ1T2Q9V8Y6K4C2B1G", *values[:7], "sha256:" + "d" * 64, *values[7:]),
    )
    with pytest.raises(sqlite3.IntegrityError):
        sqlite_connection.execute(
            statement,
            (
                "EVT-01HZX7M3FQ1T2Q9V8Y6K4C2B1H",
                *values[:7],
                "sha256:" + "e" * 64,
                *values[7:],
            ),
        )


def test_outdated_schema_requires_explicit_migration(
    sqlite_connection: sqlite3.Connection,
    frozen_clock: FrozenClock,
) -> None:
    old = migrate_schema(sqlite_connection, clock=frozen_clock, target_version=1)
    assert old.status is SchemaStatus.OUTDATED
    with pytest.raises(RuntimeSchemaMigrationRequiredError):
        require_current_schema(sqlite_connection)

    current = migrate_schema(sqlite_connection, clock=frozen_clock)
    assert current.status is SchemaStatus.CURRENT


def test_migration_is_idempotent_when_schema_is_current(
    sqlite_connection: sqlite3.Connection,
    frozen_clock: FrozenClock,
) -> None:
    first = migrate_schema(sqlite_connection, clock=frozen_clock)
    second = migrate_schema(sqlite_connection, clock=frozen_clock)
    assert second == first
    assert (
        sqlite_connection.execute("SELECT count(*) FROM runtime_schema_migrations").fetchone()[0]
        == 2
    )


def test_newer_schema_is_rejected(
    sqlite_connection: sqlite3.Connection,
    frozen_clock: FrozenClock,
) -> None:
    migrate_schema(sqlite_connection, clock=frozen_clock)
    sqlite_connection.execute(
        "INSERT INTO runtime_schema_migrations "
        "(version, name, checksum, applied_at) VALUES (3, 'future', ?, ?)",
        ("sha256:" + "f" * 64, "2026-08-01T00:00:00Z"),
    )
    assert inspect_schema(sqlite_connection).status is SchemaStatus.NEWER
    with pytest.raises(UnsupportedRuntimeSchemaError):
        require_current_schema(sqlite_connection)
    with pytest.raises(UnsupportedRuntimeSchemaError):
        migrate_schema(sqlite_connection, clock=frozen_clock)


@pytest.mark.parametrize("alteration", ["checksum", "schema"])
def test_altered_migration_evidence_or_schema_is_rejected(
    sqlite_connection: sqlite3.Connection,
    frozen_clock: FrozenClock,
    alteration: str,
) -> None:
    migrate_schema(sqlite_connection, clock=frozen_clock)
    if alteration == "checksum":
        sqlite_connection.execute(
            "UPDATE runtime_schema_migrations SET checksum = ? WHERE version = 1",
            ("sha256:" + "0" * 64,),
        )
    else:
        sqlite_connection.execute("DROP INDEX ix_project_aggregates_updated_at")

    assert inspect_schema(sqlite_connection).status is SchemaStatus.ALTERED
    with pytest.raises(RuntimeConfigurationError, match="integrity validation failed"):
        require_current_schema(sqlite_connection)
    with pytest.raises(RuntimeConfigurationError, match="integrity validation failed"):
        migrate_schema(sqlite_connection, clock=frozen_clock)


def test_non_migration_schema_without_history_is_altered(
    sqlite_connection: sqlite3.Connection,
) -> None:
    sqlite_connection.execute("CREATE TABLE rogue (value TEXT) STRICT")
    inspection = inspect_schema(sqlite_connection)
    assert inspection.status is SchemaStatus.ALTERED
    assert inspection.issues == ("migration table is missing",)


def test_failed_sql_migration_rolls_back_every_statement(
    sqlite_connection: sqlite3.Connection,
    frozen_clock: FrozenClock,
) -> None:
    base = load_sql_migrations()
    broken = SqlMigration.create(
        version=3,
        name="broken",
        sql=(
            "CREATE TABLE transient_evidence (value TEXT) STRICT;\n"
            "CREATE TABLE transient_evidence (value TEXT) STRICT;\n"
        ),
    )
    migrations = (*base, broken)
    migrate_schema(sqlite_connection, clock=frozen_clock, migrations=base)

    with pytest.raises(PersistenceError, match="schema migration failed"):
        migrate_schema(sqlite_connection, clock=frozen_clock, migrations=migrations)

    assert (
        sqlite_connection.execute(
            "SELECT count(*) FROM runtime_schema_migrations WHERE version = 3"
        ).fetchone()[0]
        == 0
    )
    assert (
        sqlite_connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name = 'transient_evidence'"
        ).fetchone()[0]
        == 0
    )


def test_invalid_target_and_migration_sequence_fail_closed(
    sqlite_connection: sqlite3.Connection,
    frozen_clock: FrozenClock,
) -> None:
    with pytest.raises(RuntimeConfigurationError, match="target is invalid"):
        migrate_schema(sqlite_connection, clock=frozen_clock, target_version=3)

    migration = SqlMigration.create(version=2, name="starts_late", sql="SELECT 1;")
    with pytest.raises(RuntimeConfigurationError, match="sequence is invalid"):
        inspect_schema(sqlite_connection, migrations=(migration,))
