import json
import os
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest
from scripts.verify_installed import verify

pytestmark = pytest.mark.release
ROOT = Path(__file__).parents[2]


def _build(output: Path) -> Path:
    env = {**os.environ, "SOURCE_DATE_EPOCH": "1785456000"}
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(output)],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return next(output.glob("*.whl"))


def _build_all(output: Path) -> tuple[Path, Path]:
    env = {**os.environ, "SOURCE_DATE_EPOCH": "1785456000"}
    subprocess.run(
        ["uv", "build", "--out-dir", str(output)],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return next(output.glob("*.whl")), next(output.glob("*.tar.gz"))


def test_wheel_contains_only_the_typed_runtime_package(tmp_path: Path) -> None:
    wheel = _build(tmp_path / "dist")
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = archive.read(metadata_name).decode("utf-8")
    assert "arch_runtime/py.typed" in names
    assert "arch_runtime/ports/storage.py" in names
    assert "arch_runtime/ports/repositories.py" in names
    assert "arch_runtime/ports/unit_of_work.py" in names
    assert "arch_runtime/persistence/sqlite/sql/0001_initial_tables.sql" in names
    assert "arch_runtime/persistence/sqlite/sql/0002_initial_indexes.sql" in names
    assert "Requires-Dist: arch-kernel<0.3.0,>=0.2.0" in metadata
    assert not any(name.startswith(("tests/", "src/")) for name in names)


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_distribution_installs_released_kernel_and_public_vertical_slice(
    tmp_path: Path, kind: str
) -> None:
    wheel, sdist = _build_all(tmp_path / "dist")
    output = verify(wheel if kind == "wheel" else sdist)
    evidence = json.loads(output)
    assert evidence["versions"] == {"arch-kernel": "0.2.0", "arch-runtime": "0.2.0"}
    assert evidence["workflow"] == "a -> b -> c"
    assert evidence["migration"]["post_migration_replay"] == "PASS"
    print(output)


def test_sdist_contains_reviewable_sources_and_no_generated_state(tmp_path: Path) -> None:
    _, sdist = _build_all(tmp_path / "dist")
    with tarfile.open(sdist, mode="r:gz") as archive:
        names = {Path(name).as_posix() for name in archive.getnames()}
    assert any(name.endswith("/src/arch_runtime/py.typed") for name in names)
    assert any(name.endswith("/tests/architecture/test_import_boundaries.py") for name in names)
    assert any(name.endswith("/SECURITY.md") for name in names)
    forbidden = {
        ".coverage",
        ".env",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "dist",
        "htmlcov",
    }
    assert not any(forbidden.intersection(Path(name).parts) for name in names)
    assert not any(name.endswith((".db", ".sqlite", ".sqlite3")) for name in names)
