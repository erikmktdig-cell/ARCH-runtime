import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from arch_runtime.persistence.sqlite import SQLiteConfig, open_sqlite_connection


@pytest.fixture
def sqlite_config(tmp_path: Path) -> SQLiteConfig:
    return SQLiteConfig(tmp_path / "runtime.db", busy_timeout_ms=2_500)


@pytest.fixture
def sqlite_connection(sqlite_config: SQLiteConfig) -> Iterator[sqlite3.Connection]:
    connection = open_sqlite_connection(sqlite_config)
    try:
        yield connection
    finally:
        connection.close()
