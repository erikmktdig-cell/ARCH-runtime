import sqlite3
from dataclasses import replace
from importlib import resources
from pathlib import Path

import pytest

from arch_runtime.errors import RuntimeConfigurationError
from arch_runtime.persistence.sqlite import SchemaStatus, inspect_schema, migrate_schema
from arch_runtime.persistence.sqlite.migrations import (
    SqlMigration,
    execute_sql_migration,
    load_sql_migrations,
    validate_migration_sequence,
)
from tests.fakes.adapters import FrozenClock

pytestmark = pytest.mark.sqlite


@pytest.mark.parametrize("clause", ["", "WHERE version=1"])
def test_empty_or_gapped_migration_evidence_is_altered(
    sqlite_connection: sqlite3.Connection,
    frozen_clock: FrozenClock,
    clause: str,
) -> None:
    migrate_schema(sqlite_connection, clock=frozen_clock)
    sqlite_connection.execute("DELETE FROM runtime_schema_migrations " + clause)
    assert inspect_schema(sqlite_connection).status is SchemaStatus.ALTERED


def test_migration_checksum_tamper_and_incomplete_sql_are_rejected(
    sqlite_connection: sqlite3.Connection,
) -> None:
    migration = load_sql_migrations()[0]
    with pytest.raises(RuntimeConfigurationError, match="checksum is invalid"):
        validate_migration_sequence((replace(migration, checksum="sha256:" + "0" * 64),))
    with pytest.raises(RuntimeConfigurationError, match="incomplete statement"):
        execute_sql_migration(
            sqlite_connection,
            SqlMigration.create(version=1, name="incomplete", sql="CREATE TABLE bad (id INTEGER)"),
        )


def test_packaged_migration_manifest_rejects_modified_resource(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = load_sql_migrations()
    for item in original:
        (tmp_path / f"{item.version:04d}_{item.name}.sql").write_text(
            item.sql + "\n-- tampered\n", encoding="utf-8"
        )
    monkeypatch.setattr(resources, "files", lambda name: tmp_path)
    with pytest.raises(RuntimeConfigurationError, match="approved manifest"):
        load_sql_migrations()
