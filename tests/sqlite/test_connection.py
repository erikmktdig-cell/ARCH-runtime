import sqlite3
from pathlib import Path

import pytest

from arch_runtime.errors import PersistenceError, RuntimeConfigurationError
from arch_runtime.persistence.sqlite import (
    SQLiteConfig,
    TransactionMode,
    begin_transaction,
    commit_transaction,
    open_sqlite_connection,
    rollback_transaction,
)

pytestmark = pytest.mark.sqlite


def test_file_connection_enforces_mandatory_pragmas(
    sqlite_connection: sqlite3.Connection,
) -> None:
    assert sqlite_connection.isolation_level is None
    assert sqlite_connection.row_factory is sqlite3.Row
    assert sqlite_connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert sqlite_connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert sqlite_connection.execute("PRAGMA synchronous").fetchone()[0] == 1
    assert sqlite_connection.execute("PRAGMA busy_timeout").fetchone()[0] == 2_500
    assert sqlite_connection.execute("PRAGMA trusted_schema").fetchone()[0] == 0
    assert sqlite_connection.execute("PRAGMA recursive_triggers").fetchone()[0] == 0


def test_transactions_require_explicit_begin_commit_and_rollback(
    sqlite_connection: sqlite3.Connection,
) -> None:
    sqlite_connection.execute("CREATE TABLE evidence (value TEXT NOT NULL) STRICT")

    begin_transaction(sqlite_connection, mode=TransactionMode.IMMEDIATE)
    sqlite_connection.execute("INSERT INTO evidence (value) VALUES ('rolled-back')")
    rollback_transaction(sqlite_connection)
    assert sqlite_connection.execute("SELECT count(*) FROM evidence").fetchone()[0] == 0

    begin_transaction(sqlite_connection, mode=TransactionMode.DEFERRED)
    sqlite_connection.execute("INSERT INTO evidence (value) VALUES ('committed')")
    commit_transaction(sqlite_connection)
    assert sqlite_connection.execute("SELECT value FROM evidence").fetchone()[0] == "committed"


def test_transaction_state_misuse_fails_closed(sqlite_connection: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeConfigurationError, match="No SQLite transaction"):
        commit_transaction(sqlite_connection)
    with pytest.raises(RuntimeConfigurationError, match="No SQLite transaction"):
        rollback_transaction(sqlite_connection)

    begin_transaction(sqlite_connection, mode=TransactionMode.EXCLUSIVE)
    with pytest.raises(RuntimeConfigurationError, match="already active"):
        begin_transaction(sqlite_connection)
    rollback_transaction(sqlite_connection)


def test_shared_memory_connections_observe_the_same_database() -> None:
    config = SQLiteConfig.shared_memory("runtime_contract_test", busy_timeout_ms=500)
    first = open_sqlite_connection(config)
    second = open_sqlite_connection(config)
    try:
        first.execute("CREATE TABLE shared_evidence (value INTEGER NOT NULL) STRICT")
        begin_transaction(first)
        first.execute("INSERT INTO shared_evidence (value) VALUES (7)")
        commit_transaction(first)
        assert second.execute("SELECT value FROM shared_evidence").fetchone()[0] == 7
        assert first.execute("PRAGMA journal_mode").fetchone()[0] == "memory"
    finally:
        second.close()
        first.close()


@pytest.mark.parametrize(
    "config",
    [
        pytest.param(SQLiteConfig, id="empty-target"),
    ],
)
def test_invalid_configuration_is_rejected(config: type[SQLiteConfig]) -> None:
    with pytest.raises(RuntimeConfigurationError):
        config("")
    with pytest.raises(RuntimeConfigurationError):
        config("relative.db", uri=True)
    with pytest.raises(RuntimeConfigurationError):
        config("runtime.db", busy_timeout_ms=0)
    with pytest.raises(RuntimeConfigurationError):
        config.shared_memory("invalid name")


def test_connection_open_failure_is_structured(tmp_path: Path) -> None:
    missing_parent = tmp_path / "missing" / "runtime.db"
    with pytest.raises(PersistenceError, match="connection setup failed"):
        open_sqlite_connection(SQLiteConfig(missing_parent))
