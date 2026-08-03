import os
import subprocess
import sys
import tarfile
import venv
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.release
ROOT = Path(__file__).parents[2]
KERNEL_GIT_SOURCE = "arch-kernel @ git+https://github.com/erikmktdig-cell/ARCH-kernel.git@v0.1.0"


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


def _kernel_install_target(output: Path) -> str:
    checkout = ROOT.parent / "arch-kernel"
    if checkout.is_dir():
        tags = subprocess.run(
            ["git", "tag", "--points-at", "HEAD"],
            cwd=checkout,
            check=False,
            capture_output=True,
            text=True,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )
        if "v0.1.0" in tags.stdout.splitlines() and not status.stdout.strip():
            kernel_dist = output / "kernel-dist"
            subprocess.run(
                ["uv", "build", "--wheel", "--out-dir", str(kernel_dist), str(checkout)],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            return str(next(kernel_dist.glob("arch_kernel-0.1.0-*.whl")))
    return KERNEL_GIT_SOURCE


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
    assert "Requires-Dist: arch-kernel<0.2.0,>=0.1.0" in metadata
    assert not any(name.startswith(("tests/", "src/")) for name in names)


def test_wheel_imports_from_an_isolated_environment(tmp_path: Path) -> None:
    wheel = _build(tmp_path / "dist")
    environment = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            _kernel_install_target(tmp_path),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", str(wheel)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    completed = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            "import arch_kernel, arch_runtime; "
            "from arch_runtime.persistence.sqlite import load_sql_migrations; "
            "print(arch_runtime.__version__, arch_kernel.__version__, len(load_sql_migrations()))",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "0.1.0 0.1.0 2"


def test_sdist_contains_reviewable_sources_and_no_generated_state(tmp_path: Path) -> None:
    _, sdist = _build_all(tmp_path / "dist")
    with tarfile.open(sdist, mode="r:gz") as archive:
        names = {Path(name).as_posix() for name in archive.getnames()}
    assert any(name.endswith("/src/arch_runtime/py.typed") for name in names)
    assert any(name.endswith("/tests/architecture/test_import_boundaries.py") for name in names)
    assert any(name.endswith("/SECURITY.md") for name in names)
    assert not any("/.venv/" in name or "/dist/" in name for name in names)
