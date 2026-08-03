"""Versioned SQLite schema migration, inspection, and compatibility policy."""

import re
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn

from arch_kernel.kernel import ensure_utc

from arch_runtime.errors import (
    PersistenceError,
    RuntimeConfigurationError,
    RuntimeSchemaMigrationRequiredError,
    UnsupportedRuntimeSchemaError,
)
from arch_runtime.persistence.sqlite.connection import (
    begin_transaction,
    commit_transaction,
    rollback_transaction,
)
from arch_runtime.persistence.sqlite.migrations import (
    SqlMigration,
    execute_sql_migration,
    load_sql_migrations,
    validate_migration_sequence,
)
from arch_runtime.ports import Clock


class SchemaStatus(StrEnum):
    EMPTY = "empty"
    OUTDATED = "outdated"
    CURRENT = "current"
    NEWER = "newer"
    ALTERED = "altered"


@dataclass(frozen=True, slots=True)
class SchemaInspection:
    status: SchemaStatus
    current_version: int
    latest_version: int
    issues: tuple[str, ...] = ()


def inspect_schema(
    connection: sqlite3.Connection,
    *,
    migrations: tuple[SqlMigration, ...] | None = None,
) -> SchemaInspection:
    available = migrations or load_sql_migrations()
    validate_migration_sequence(available)
    latest = available[-1].version
    actual_signature = _schema_signature(connection)
    if not _migration_table_exists(connection):
        status = SchemaStatus.EMPTY if not actual_signature else SchemaStatus.ALTERED
        issues = () if status is SchemaStatus.EMPTY else ("migration table is missing",)
        return SchemaInspection(status, 0, latest, issues)

    applied = _read_applied_migrations(connection)
    if not applied:
        return SchemaInspection(
            SchemaStatus.ALTERED,
            0,
            latest,
            ("migration history is empty",),
        )
    current = max(applied)
    if current > latest:
        return SchemaInspection(SchemaStatus.NEWER, current, latest)

    expected_versions = set(range(1, current + 1))
    if set(applied) != expected_versions:
        return SchemaInspection(
            SchemaStatus.ALTERED,
            current,
            latest,
            ("migration history is not contiguous",),
        )
    for migration in available[:current]:
        name, checksum = applied[migration.version]
        if name != migration.name or checksum != migration.checksum:
            return SchemaInspection(
                SchemaStatus.ALTERED,
                current,
                latest,
                (f"migration {migration.version} evidence differs",),
            )

    expected_signature = _expected_schema_signature(available, current)
    if actual_signature != expected_signature:
        return SchemaInspection(
            SchemaStatus.ALTERED,
            current,
            latest,
            ("schema objects differ from migration evidence",),
        )
    status = SchemaStatus.CURRENT if current == latest else SchemaStatus.OUTDATED
    return SchemaInspection(status, current, latest)


def migrate_schema(
    connection: sqlite3.Connection,
    *,
    clock: Clock,
    migrations: tuple[SqlMigration, ...] | None = None,
    target_version: int | None = None,
) -> SchemaInspection:
    available = migrations or load_sql_migrations()
    validate_migration_sequence(available)
    inspection = inspect_schema(connection, migrations=available)
    if inspection.status is SchemaStatus.NEWER:
        _raise_newer(inspection)
    if inspection.status is SchemaStatus.ALTERED:
        _raise_altered(inspection)
    target = available[-1].version if target_version is None else target_version
    if not inspection.current_version <= target <= available[-1].version:
        raise RuntimeConfigurationError(
            "SQL migration target is invalid",
            operation="sqlite.schema.migrate",
            remediation="choose a target from the current through latest schema version",
        )
    if target == inspection.current_version:
        return inspection

    applied_at = ensure_utc(clock.now()).isoformat().replace("+00:00", "Z")
    try:
        begin_transaction(connection)
        for migration in available[inspection.current_version : target]:
            execute_sql_migration(connection, migration)
            connection.execute(
                "INSERT INTO runtime_schema_migrations "
                "(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.checksum, applied_at),
            )
        commit_transaction(connection)
    except (sqlite3.Error, RuntimeConfigurationError) as error:
        if connection.in_transaction:
            rollback_transaction(connection)
        if isinstance(error, RuntimeConfigurationError):
            raise
        raise PersistenceError(
            "SQL schema migration failed",
            operation="sqlite.schema.migrate",
            remediation="restore the database backup and inspect migration integrity",
        ) from error
    return inspect_schema(connection, migrations=available)


def require_current_schema(
    connection: sqlite3.Connection,
    *,
    migrations: tuple[SqlMigration, ...] | None = None,
) -> SchemaInspection:
    inspection = inspect_schema(connection, migrations=migrations)
    if inspection.status is SchemaStatus.CURRENT:
        return inspection
    if inspection.status in {SchemaStatus.EMPTY, SchemaStatus.OUTDATED}:
        raise RuntimeSchemaMigrationRequiredError(
            "SQLite runtime schema migration is required",
            operation="sqlite.schema.validate",
            remediation="run the explicit SQL schema migration workflow",
        )
    if inspection.status is SchemaStatus.NEWER:
        _raise_newer(inspection)
    _raise_altered(inspection)


def _raise_newer(inspection: SchemaInspection) -> NoReturn:
    raise UnsupportedRuntimeSchemaError(
        "SQLite runtime schema is newer than this runtime supports",
        operation="sqlite.schema.validate",
        remediation="upgrade arch-runtime before opening the database for writes",
    )


def _raise_altered(inspection: SchemaInspection) -> NoReturn:
    raise RuntimeConfigurationError(
        "SQLite runtime schema integrity validation failed",
        operation="sqlite.schema.validate",
        remediation="restore an unmodified schema and preserve the database for inspection",
    )


def _migration_table_exists(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runtime_schema_migrations'"
    ).fetchone()
    return row is not None


def _read_applied_migrations(
    connection: sqlite3.Connection,
) -> dict[int, tuple[str, str]]:
    rows = connection.execute(
        "SELECT version, name, checksum FROM runtime_schema_migrations ORDER BY version"
    ).fetchall()
    return {int(row[0]): (str(row[1]), str(row[2])) for row in rows}


def _schema_signature(connection: sqlite3.Connection) -> tuple[tuple[str, str, str], ...]:
    rows = connection.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE type IN ('table', 'index') AND name NOT LIKE 'sqlite_%' AND sql IS NOT NULL "
        "ORDER BY type, name"
    ).fetchall()
    return tuple((str(row[0]), str(row[1]), _normalize_sql(str(row[2]))) for row in rows)


def _expected_schema_signature(
    migrations: tuple[SqlMigration, ...],
    version: int,
) -> tuple[tuple[str, str, str], ...]:
    reference = sqlite3.connect(":memory:", isolation_level=None)
    try:
        for migration in migrations[:version]:
            execute_sql_migration(reference, migration)
        return _schema_signature(reference)
    finally:
        reference.close()


def _normalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().lower()
