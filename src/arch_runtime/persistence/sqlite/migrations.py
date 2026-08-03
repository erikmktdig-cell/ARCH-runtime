"""Immutable numbered SQL migration loading and statement execution."""

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from importlib import resources

from arch_runtime.errors import RuntimeConfigurationError

_MIGRATION_NAME = r"^(?P<version>[0-9]{4})_(?P<name>[a-z0-9_]+)\.sql$"
_APPROVED_CHECKSUMS = (
    (1, "sha256:89f4e9766726d3db27c07d92c4c8c58dd99b800e6c3df7ff45cdc2f32b571058"),
    (2, "sha256:3a4fd1394107791cf38a2a6e4a3749f7a3642fed90a6e0999f066b18e57cfaff"),
)


@dataclass(frozen=True, slots=True)
class SqlMigration:
    version: int
    name: str
    sql: str
    checksum: str

    @classmethod
    def create(cls, *, version: int, name: str, sql: str) -> "SqlMigration":
        checksum = "sha256:" + hashlib.sha256(sql.encode("utf-8")).hexdigest()
        return cls(version=version, name=name, sql=sql, checksum=checksum)


def load_sql_migrations() -> tuple[SqlMigration, ...]:
    package = resources.files("arch_runtime.persistence.sqlite.sql")
    migrations: list[SqlMigration] = []
    for resource in sorted(package.iterdir(), key=lambda value: value.name):
        match = re.fullmatch(_MIGRATION_NAME, resource.name)
        if match is None:
            continue
        migrations.append(
            SqlMigration.create(
                version=int(match.group("version")),
                name=match.group("name"),
                sql=resource.read_text(encoding="utf-8"),
            )
        )
    result = tuple(migrations)
    validate_migration_sequence(result)
    checksums = tuple((migration.version, migration.checksum) for migration in result)
    if checksums != _APPROVED_CHECKSUMS:
        raise RuntimeConfigurationError(
            "Packaged SQL migration checksum differs from the approved manifest",
            operation="sqlite.migrations.load",
            remediation="restore immutable migration files or add a new numbered migration",
        )
    return result


def validate_migration_sequence(migrations: tuple[SqlMigration, ...]) -> None:
    versions = tuple(migration.version for migration in migrations)
    expected = tuple(range(1, len(migrations) + 1))
    names = tuple(migration.name for migration in migrations)
    if not migrations or versions != expected or len(names) != len(set(names)):
        raise RuntimeConfigurationError(
            "SQL migration sequence is invalid",
            operation="sqlite.migrations.load",
            remediation="provide contiguous immutable migrations beginning at version 1",
        )
    for migration in migrations:
        expected_checksum = SqlMigration.create(
            version=migration.version,
            name=migration.name,
            sql=migration.sql,
        ).checksum
        if migration.checksum != expected_checksum:
            raise RuntimeConfigurationError(
                "SQL migration checksum is invalid",
                operation="sqlite.migrations.load",
                remediation="restore the immutable migration content and checksum",
            )


def execute_sql_migration(connection: sqlite3.Connection, migration: SqlMigration) -> None:
    buffer = ""
    for line in migration.sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                connection.execute(statement)
            buffer = ""
    if buffer.strip():
        raise RuntimeConfigurationError(
            "SQL migration contains an incomplete statement",
            operation="sqlite.migrations.execute",
            remediation="terminate every migration statement explicitly",
        )
