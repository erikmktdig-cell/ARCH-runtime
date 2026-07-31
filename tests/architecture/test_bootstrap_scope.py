from pathlib import Path

import pytest

pytestmark = pytest.mark.architecture
PACKAGE_ROOT = Path(__file__).parents[2] / "src" / "arch_runtime"
RESERVED_NAMESPACES = (
    "application",
    "ports",
    "persistence",
    "persistence/sqlite",
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
    forbidden_names = {"api.py", "cli.py", "commands.py", "repositories.py", "unit_of_work.py"}
    files = [path for path in PACKAGE_ROOT.rglob("*") if path.is_file()]
    assert not {path.suffix for path in files} & forbidden_suffixes
    assert not {path.name for path in files} & forbidden_names
