import sqlite3
from collections.abc import Callable
from unittest.mock import Mock

import pytest

from arch_runtime.errors import PersistenceError, RuntimeConfigurationError
from arch_runtime.persistence.sqlite import SQLiteConfig
from arch_runtime.persistence.sqlite import connection as connection_module

pytestmark = pytest.mark.sqlite


@pytest.mark.parametrize(
    ("journal", "foreign_keys", "timeout"),
    [
        ("wal", 0, 500),
        ("wal", 1, 42),
        ("delete", 1, 500),
    ],
)
def test_connection_closes_when_mandatory_pragmas_cannot_be_enforced(
    monkeypatch: pytest.MonkeyPatch,
    journal: str,
    foreign_keys: int,
    timeout: int,
) -> None:
    connection = Mock(spec=sqlite3.Connection)

    def execute(sql: str) -> Mock:
        row = Mock()
        row.fetchone.return_value = (
            {
                "PRAGMA journal_mode = WAL": journal,
                "PRAGMA foreign_keys": foreign_keys,
                "PRAGMA busy_timeout": timeout,
            }.get(sql, 0),
        )
        return row

    connection.execute.side_effect = execute
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: connection)
    with pytest.raises(RuntimeConfigurationError):
        connection_module.open_sqlite_connection(
            SQLiteConfig("pragma-test.db", busy_timeout_ms=500)
        )
    connection.close.assert_called_once()


def test_connection_setup_sql_error_closes_allocated_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = Mock(spec=sqlite3.Connection)
    connection.execute.side_effect = sqlite3.OperationalError("injected")
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: connection)
    with pytest.raises(PersistenceError):
        connection_module.open_sqlite_connection(SQLiteConfig("setup-test.db"))
    connection.close.assert_called_once()


@pytest.mark.parametrize(
    ("operation", "method", "active"),
    [
        (connection_module.begin_transaction, "execute", False),
        (connection_module.commit_transaction, "commit", True),
        (connection_module.rollback_transaction, "rollback", True),
    ],
)
def test_sqlite_transaction_errors_remain_infrastructure_failures(
    operation: Callable[[sqlite3.Connection], None],
    method: str,
    active: bool,
) -> None:
    connection = Mock(spec=sqlite3.Connection)
    connection.in_transaction = active
    getattr(connection, method).side_effect = sqlite3.OperationalError("injected")
    with pytest.raises(PersistenceError):
        operation(connection)
