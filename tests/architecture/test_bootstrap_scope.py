from pathlib import Path

import pytest

pytestmark = pytest.mark.architecture
PACKAGE_ROOT = Path(__file__).parents[2] / "src" / "arch_runtime"
RESERVED_NAMESPACES = (
    "application",
    "persistence",
    "replay",
    "migrations",
)


@pytest.mark.parametrize("namespace", RESERVED_NAMESPACES)
def test_reserved_namespace_contains_only_its_marker(namespace: str) -> None:
    directory = PACKAGE_ROOT / namespace
    contents = {path.name for path in directory.iterdir() if path.name != "__pycache__"}
    expected = {"__init__.py", "sqlite"} if namespace == "persistence" else {"__init__.py"}
    assert contents == expected


def test_bootstrap_has_no_forbidden_runtime_artifacts() -> None:
    forbidden_suffixes = {".db", ".sqlite", ".sqlite3"}
    forbidden_names = {"api.py", "cli.py", "commands.py", "services.py"}
    files = [path for path in PACKAGE_ROOT.rglob("*") if path.is_file()]
    assert not {path.suffix for path in files} & forbidden_suffixes
    assert not {path.name for path in files} & forbidden_names


def test_r02_ports_are_contracts_without_adapters() -> None:
    ports = PACKAGE_ROOT / "ports"
    assert {path.name for path in ports.iterdir() if path.name != "__pycache__"} == {
        "__init__.py",
        "clock.py",
        "repositories.py",
        "storage.py",
        "unit_of_work.py",
    }


def test_r02_has_no_production_fake_or_in_memory_adapters() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in PACKAGE_ROOT.rglob("*.py"))
    assert "class Fake" not in source
    assert "class InMemory" not in source


def test_r03_sqlite_foundation_excludes_repositories_and_unit_of_work() -> None:
    sqlite_root = PACKAGE_ROOT / "persistence" / "sqlite"
    contents = {path.name for path in sqlite_root.iterdir() if path.name != "__pycache__"}
    assert contents == {
        "__init__.py",
        "config.py",
        "connection.py",
        "migrations.py",
        "schema.py",
        "sql",
    }
    source = "\n".join(path.read_text(encoding="utf-8") for path in sqlite_root.rglob("*.py"))
    assert "ProjectRepository" not in source
    assert "SQLiteUnitOfWork" not in source
