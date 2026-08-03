"""Validated SQLite connection configuration."""

from dataclasses import dataclass
from pathlib import Path
from typing import Self

from arch_runtime.errors import RuntimeConfigurationError


@dataclass(frozen=True, slots=True)
class SQLiteConfig:
    """Infrastructure-only SQLite settings with bounded operational values."""

    database: str | Path
    uri: bool = False
    busy_timeout_ms: int = 5_000

    def __post_init__(self) -> None:
        database = str(self.database)
        if not database or "\x00" in database:
            raise RuntimeConfigurationError(
                "SQLite database target is invalid",
                operation="sqlite.configure",
                remediation="provide a non-empty local path or SQLite file URI",
            )
        if self.uri and not database.startswith("file:"):
            raise RuntimeConfigurationError(
                "SQLite URI must use the file scheme",
                operation="sqlite.configure",
                remediation="use a file: URI or disable URI mode",
            )
        if not 1 <= self.busy_timeout_ms <= 60_000:
            raise RuntimeConfigurationError(
                "SQLite busy timeout is outside the supported range",
                operation="sqlite.configure",
                remediation="choose a timeout from 1 through 60000 milliseconds",
            )

    @classmethod
    def shared_memory(cls, name: str, *, busy_timeout_ms: int = 5_000) -> Self:
        if not name or not name.replace("_", "").replace("-", "").isalnum():
            raise RuntimeConfigurationError(
                "Shared-memory database name is invalid",
                operation="sqlite.configure",
                remediation="use only letters, digits, hyphens, and underscores",
            )
        return cls(
            database=f"file:{name}?mode=memory&cache=shared",
            uri=True,
            busy_timeout_ms=busy_timeout_ms,
        )
