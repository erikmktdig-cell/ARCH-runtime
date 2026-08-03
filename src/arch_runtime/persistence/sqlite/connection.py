"""Safe SQLite connection setup and explicit transaction primitives."""

import sqlite3
from enum import StrEnum

from arch_runtime.errors import PersistenceError, RuntimeConfigurationError
from arch_runtime.persistence.sqlite.config import SQLiteConfig


class TransactionMode(StrEnum):
    DEFERRED = "DEFERRED"
    IMMEDIATE = "IMMEDIATE"
    EXCLUSIVE = "EXCLUSIVE"


def open_sqlite_connection(config: SQLiteConfig) -> sqlite3.Connection:
    """Open a connection in autocommit mode and enforce mandatory pragmas."""

    try:
        connection = sqlite3.connect(
            str(config.database),
            timeout=config.busy_timeout_ms / 1_000,
            isolation_level=None,
            check_same_thread=True,
            uri=config.uri,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        journal_mode = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute(f"PRAGMA busy_timeout = {config.busy_timeout_ms}")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("PRAGMA recursive_triggers = OFF")
        _verify_pragmas(connection, config, journal_mode)
    except RuntimeConfigurationError:
        if "connection" in locals():
            connection.close()
        raise
    except sqlite3.Error as error:
        if "connection" in locals():
            connection.close()
        raise PersistenceError(
            "SQLite connection setup failed",
            operation="sqlite.open",
            remediation="verify the database path, permissions, and SQLite runtime",
        ) from error
    return connection


def _verify_pragmas(
    connection: sqlite3.Connection,
    config: SQLiteConfig,
    journal_mode: str,
) -> None:
    foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    busy_timeout = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
    if foreign_keys != 1 or busy_timeout != config.busy_timeout_ms:
        raise RuntimeConfigurationError(
            "SQLite mandatory pragmas could not be enabled",
            operation="sqlite.open",
            remediation="use a SQLite runtime that supports required connection pragmas",
        )
    if journal_mode not in {"wal", "memory"}:
        raise RuntimeConfigurationError(
            "SQLite journal mode is unsupported",
            operation="sqlite.open",
            remediation="use a writable file database or shared in-memory database",
        )


def begin_transaction(
    connection: sqlite3.Connection,
    *,
    mode: TransactionMode = TransactionMode.IMMEDIATE,
) -> None:
    if connection.in_transaction:
        raise RuntimeConfigurationError(
            "SQLite transaction is already active",
            operation="sqlite.begin",
            remediation="finish the active transaction before beginning another",
        )
    try:
        connection.execute(f"BEGIN {mode.value}")
    except sqlite3.Error as error:
        raise PersistenceError(
            "SQLite transaction could not begin",
            operation="sqlite.begin",
            remediation="retry only when the failure is known to be transient",
        ) from error


def commit_transaction(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        raise RuntimeConfigurationError(
            "No SQLite transaction is active",
            operation="sqlite.commit",
            remediation="begin a transaction before committing",
        )
    try:
        connection.commit()
    except sqlite3.Error as error:
        raise PersistenceError(
            "SQLite transaction commit failed",
            operation="sqlite.commit",
            remediation="treat the operation as uncommitted and inspect infrastructure health",
        ) from error


def rollback_transaction(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        raise RuntimeConfigurationError(
            "No SQLite transaction is active",
            operation="sqlite.rollback",
            remediation="begin a transaction before rolling back",
        )
    try:
        connection.rollback()
    except sqlite3.Error as error:
        raise PersistenceError(
            "SQLite transaction rollback failed",
            operation="sqlite.rollback",
            remediation="discard the connection and inspect infrastructure health",
        ) from error
