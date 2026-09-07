"""Deterministic release artifact inspection and evidence generation."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement

from arch_runtime import __version__
from scripts.verify_installed import KERNEL_SHA256, KERNEL_URL

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "dist",
    "htmlcov",
    ".hypothesis",
    ".uv-cache",
    ".pytest-tmp",
    ".coverage",
}
FORBIDDEN_SUFFIXES = (".db", ".db-journal", ".db-shm", ".db-wal", ".sqlite", ".sqlite3")
SECRET_PATTERNS = (
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"[A-Za-z]+://[^\s/:]+:[^\s/@]+@"),
)


class ReleaseCheckError(RuntimeError):
    """Raised when release evidence fails closed."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_names(path: Path) -> tuple[str, ...]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return tuple(archive.namelist())
    with tarfile.open(path, mode="r:gz") as archive:
        return tuple(archive.getnames())


def inspect_artifacts(directory: Path) -> list[dict[str, Any]]:
    artifacts = sorted(
        path
        for path in directory.iterdir()
        if path.suffix == ".whl" or path.name.endswith(".tar.gz")
    )
    expected = {
        f"arch_runtime-{__version__}-py3-none-any.whl",
        f"arch_runtime-{__version__}.tar.gz",
    }
    if {path.name for path in artifacts} != expected:
        raise ReleaseCheckError("release requires exactly one wheel and one sdist")
    inventory: list[dict[str, Any]] = []
    for artifact in artifacts:
        names = _archive_names(artifact)
        for name in names:
            parts = set(Path(name).parts)
            if parts.intersection(FORBIDDEN_PARTS) or name.endswith(FORBIDDEN_SUFFIXES):
                raise ReleaseCheckError(f"forbidden archive member: {name}")
            if Path(name).name.startswith(".env"):
                raise ReleaseCheckError(f"forbidden environment file: {name}")
        _inspect_contents(artifact)
        inventory.append(
            {
                "filename": artifact.name,
                "sha256": sha256(artifact),
                "size": artifact.stat().st_size,
                "members": len(names),
            }
        )
    return inventory


def _inspect_contents(artifact: Path) -> None:
    if artifact.suffix == ".whl":
        with zipfile.ZipFile(artifact) as archive:
            members = {
                name: archive.read(name) for name in archive.namelist() if not name.endswith("/")
            }
    else:
        with tarfile.open(artifact, "r:gz") as tar:
            members = {}
            for member in tar.getmembers():
                if member.issym() or member.islnk():
                    raise ReleaseCheckError("release archives must not contain links")
                if member.isfile():
                    stream = tar.extractfile(member)
                    assert stream is not None
                    members[member.name] = stream.read()
    metadata = [
        value
        for name, value in members.items()
        if name.endswith((".dist-info/METADATA", "/PKG-INFO"))
    ]
    if not metadata:
        raise ReleaseCheckError("missing distribution metadata")
    for raw in metadata:
        parsed = BytesParser().parsebytes(raw)
        if parsed["Name"] != "arch-runtime" or parsed["Version"] != __version__:
            raise ReleaseCheckError("incorrect release identity")
        requirements = [Requirement(value) for value in parsed.get_all("Requires-Dist", [])]
        if len(requirements) != 1:
            raise ReleaseCheckError("unexpected runtime dependency set")
        dependency = requirements[0]
        if (
            dependency.name != "arch-kernel"
            or dependency.url is not None
            or str(dependency.specifier) != "<0.3.0,>=0.2.0"
            or dependency.marker is not None
        ):
            raise ReleaseCheckError("incorrect or nonportable Kernel dependency")
    for name, raw in members.items():
        content = raw.decode("utf-8", errors="replace")
        if any(pattern.search(content) for pattern in SECRET_PATTERNS):
            raise ReleaseCheckError(f"possible secret in artifact: {name}")
        if "C:" + "\\Users\\" in content or "/" + "home/" in content:
            raise ReleaseCheckError(f"personal path in artifact: {name}")
        if name.endswith(("METADATA", "PKG-INFO", "pyproject.toml")) and any(
            value in content for value in ("file:" + "//", "git+", "editable =", "../ARCH-")
        ):
            raise ReleaseCheckError(f"source dependency leakage: {name}")


def tracked_files(root: Path = ROOT) -> tuple[Path, ...]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return tuple(root / item.decode() for item in result.stdout.split(b"\0") if item)


def audit_tracked_source(root: Path = ROOT) -> None:
    for path in tracked_files(root):
        relative = path.relative_to(root)
        if relative.name.startswith(".env") or path.name.endswith(FORBIDDEN_SUFFIXES):
            raise ReleaseCheckError(f"forbidden tracked file: {relative.as_posix()}")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(content):
                raise ReleaseCheckError(f"possible secret in {relative.as_posix()}")
        if "C:" + "\\Users\\" in content or "/" + "home/" in content:
            raise ReleaseCheckError(f"personal filesystem path in {relative.as_posix()}")


def release_manifest(
    *,
    commit: str,
    tests: int,
    branch_coverage: float,
    artifacts: list[dict[str, Any]],
    ci: dict[str, str],
) -> dict[str, Any]:
    return {
        "package": "arch-runtime",
        "version": __version__,
        "implementation_baseline": "af19867518cae87fc3c1a8b229f49c22a2c9b87a",
        "commit": commit,
        "tag": f"v{__version__}",
        "tag_peeled_commit": commit,
        "repository": "https://github.com/erikmktdig-cell/ARCH-runtime",
        "release_url": f"https://github.com/erikmktdig-cell/ARCH-runtime/releases/tag/v{__version__}",
        "python": ["3.12", "3.13"],
        "tests": tests,
        "branch_coverage": branch_coverage,
        "artifacts": [item["filename"] for item in artifacts],
        "sha256": {item["filename"]: item["sha256"] for item in artifacts},
        "kernel_dependency": "arch-kernel>=0.2.0,<0.3.0",
        "released_kernel": {
            "version": "0.2.0",
            "url": KERNEL_URL,
            "sha256": KERNEL_SHA256,
            "commit": "4aa6a3acddd1b0ef761fb4dd0cfff720706ff5df",
        },
        "ci": ci,
    }


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
