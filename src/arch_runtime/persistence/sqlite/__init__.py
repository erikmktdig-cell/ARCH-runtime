"""SQLite connection, schema, and migration foundation."""

from arch_runtime.persistence.sqlite.config import SQLiteConfig
from arch_runtime.persistence.sqlite.connection import (
    TransactionMode,
    begin_transaction,
    commit_transaction,
    open_sqlite_connection,
    rollback_transaction,
)
from arch_runtime.persistence.sqlite.migrations import SqlMigration, load_sql_migrations
from arch_runtime.persistence.sqlite.schema import (
    SchemaInspection,
    SchemaStatus,
    inspect_schema,
    migrate_schema,
    require_current_schema,
)

__all__ = (
    "SQLiteConfig",
    "SchemaInspection",
    "SchemaStatus",
    "SqlMigration",
    "TransactionMode",
    "begin_transaction",
    "commit_transaction",
    "inspect_schema",
    "load_sql_migrations",
    "migrate_schema",
    "open_sqlite_connection",
    "require_current_schema",
    "rollback_transaction",
)
