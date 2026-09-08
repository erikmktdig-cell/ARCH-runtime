from __future__ import annotations

import io
import json
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest
from scripts.check_branch_coverage import check
from scripts.release_tools import ReleaseCheckError, inspect_artifacts
from scripts.verify_installed import KERNEL_SHA256, KERNEL_URL

pytestmark = pytest.mark.release


def test_lock_uses_exact_released_kernel() -> None:
    root = Path(__file__).parents[2]
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    kernel = next(package for package in lock["package"] if package["name"] == "arch-kernel")
    assert kernel["version"] == "0.2.0"
    assert kernel["source"] == {"url": KERNEL_URL}
    assert kernel["wheels"][0]["hash"] == "sha256:" + KERNEL_SHA256


def archives(
    root: Path,
    *,
    version: str = "0.2.0",
    dependency: str = "arch-kernel<0.3.0,>=0.2.0",
    member: str = "arch_runtime/py.typed",
    content: bytes = b"",
) -> None:
    metadata = (
        f"Metadata-Version: 2.4\nName: arch-runtime\nVersion: {version}\n"
        f"Requires-Dist: {dependency}\n\n"
    ).encode()
    with zipfile.ZipFile(root / "arch_runtime-0.2.0-py3-none-any.whl", "w") as wheel:
        wheel.writestr("arch_runtime-0.2.0.dist-info/METADATA", metadata)
        wheel.writestr(member, content)
    with tarfile.open(root / "arch_runtime-0.2.0.tar.gz", "w:gz") as sdist:
        info = tarfile.TarInfo("arch_runtime-0.2.0/PKG-INFO")
        info.size = len(metadata)
        sdist.addfile(info, io.BytesIO(metadata))


def test_inspector_accepts_portable_metadata(tmp_path: Path) -> None:
    archives(tmp_path)
    assert len(inspect_artifacts(tmp_path)) == 2


@pytest.mark.parametrize(
    "dependency",
    [
        "arch-kernel<0.2.0,>=0.1.0",
        "arch-kernel @ " + "file:" + "///tmp/kernel.whl",
        "arch-web>=0.2.0",
        'arch-kernel>=0.2.0,<0.3.0; sys_platform == "win32"',
    ],
)
def test_inspector_rejects_nonportable_or_wrong_dependency(tmp_path: Path, dependency: str) -> None:
    archives(tmp_path, dependency=dependency)
    with pytest.raises(ReleaseCheckError, match="Kernel dependency"):
        inspect_artifacts(tmp_path)


def test_inspector_rejects_wrong_version(tmp_path: Path) -> None:
    archives(tmp_path, version="0.1.0")
    with pytest.raises(ReleaseCheckError, match="identity"):
        inspect_artifacts(tmp_path)


@pytest.mark.parametrize("member", [".coverage", "x/.env", "x/a.db", "x/__pycache__/a.pyc"])
def test_inspector_rejects_generated_files(tmp_path: Path, member: str) -> None:
    archives(tmp_path, member=member)
    with pytest.raises(ReleaseCheckError, match="forbidden"):
        inspect_artifacts(tmp_path)


def test_inspector_rejects_embedded_secret(tmp_path: Path) -> None:
    archives(tmp_path, member="arch_runtime/leak.txt", content=("ghp_" + "a" * 30).encode())
    with pytest.raises(ReleaseCheckError, match="secret"):
        inspect_artifacts(tmp_path)


@pytest.mark.parametrize(("covered", "branches"), [(80, 100), (0, 0), (8999, 10000)])
def test_branch_gate_cannot_be_satisfied_by_combined_coverage(
    tmp_path: Path,
    covered: int,
    branches: int,
) -> None:
    path = tmp_path / "coverage.json"
    path.write_text(
        json.dumps(
            {
                "totals": {
                    "percent_covered": 99.9,
                    "covered_branches": covered,
                    "num_branches": branches,
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="branch"):
        check(path)


def test_branch_gate_accepts_exact_threshold(tmp_path: Path) -> None:
    path = tmp_path / "coverage.json"
    path.write_text(
        json.dumps({"totals": {"covered_branches": 90, "num_branches": 100}}), encoding="utf-8"
    )
    assert check(path) == 90.0
