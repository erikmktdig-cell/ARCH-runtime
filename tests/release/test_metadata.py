import tomllib
from pathlib import Path
from typing import cast

import pytest

pytestmark = pytest.mark.release
ROOT = Path(__file__).parents[2]


def _project() -> dict[str, object]:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        config = tomllib.load(stream)
    return cast(dict[str, object], config["project"])


def test_package_metadata_is_frozen() -> None:
    project = _project()
    assert project["name"] == "arch-runtime"
    assert project["requires-python"] == ">=3.12,<3.14"
    assert project["dynamic"] == ["version"]
    assert project["dependencies"] == ["arch-kernel>=0.1.0,<0.2.0"]


def test_python_requirement_and_classifiers_agree() -> None:
    project = _project()
    classifiers = cast(list[str], project["classifiers"])
    assert project["requires-python"] == ">=3.12,<3.14"
    assert "Programming Language :: Python :: 3.12" in classifiers
    assert "Programming Language :: Python :: 3.13" in classifiers


def test_required_governance_files_exist() -> None:
    required = {"CHANGELOG.md", "CONTRIBUTING.md", "LICENSE", "README.md", "SECURITY.md"}
    assert required <= {path.name for path in ROOT.iterdir()}


def test_security_policy_uses_private_reporting() -> None:
    policy = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "security/advisories/new" in policy
    assert "public issues" in policy


def test_ci_has_least_privilege_and_supported_matrix() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "permissions:\n  contents: read" in workflow
    assert "os: [ubuntu-latest, windows-latest]" in workflow
    assert 'python: ["3.12", "3.13"]' in workflow
    assert "persist-credentials: false" in workflow
    assert "secrets." not in workflow
    assert "publish" not in workflow.lower()
