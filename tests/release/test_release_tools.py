import subprocess
import sys
from pathlib import Path

import pytest
from scripts.release_tools import ReleaseCheckError, audit_tracked_source, release_manifest

pytestmark = pytest.mark.release


def test_release_manifest_records_exact_external_evidence() -> None:
    manifest = release_manifest(
        commit="a" * 40,
        tests=200,
        branch_coverage=91.5,
        artifacts=[{"filename": "package.whl", "sha256": "b" * 64}],
        ci={"ubuntu-3.12": "pass"},
    )
    assert manifest["commit"] == "a" * 40
    assert manifest["sha256"] == {"package.whl": "b" * 64}


def test_release_check_error_is_explicit() -> None:
    with pytest.raises(ReleaseCheckError, match="blocked"):
        raise ReleaseCheckError("blocked")


def test_release_scripts_do_not_embed_personal_paths() -> None:
    root = Path(__file__).parents[2]
    for path in (root / "scripts").glob("*.py"):
        assert "C:" + "\\Users\\" not in path.read_text(encoding="utf-8")


def test_tracked_source_audit_accepts_the_release_checkout() -> None:
    audit_tracked_source()


def test_release_scripts_load_when_executed_by_path() -> None:
    root = Path(__file__).parents[2]
    for script in ("scripts/check_reproducible_build.py", "scripts/build_release_manifest.py"):
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                f"import runpy; runpy.run_path({script!r})",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
