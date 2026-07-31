import ast
import importlib.metadata
from pathlib import Path

import pytest
from arch_kernel.contracts import ArtifactRecord, ProjectState
from arch_kernel.kernel import canonicalize_json, compute_content_fingerprint
from packaging.specifiers import SpecifierSet
from packaging.version import Version

pytestmark = pytest.mark.architecture
PACKAGE_ROOT = Path(__file__).parents[2] / "src" / "arch_runtime"


def test_released_kernel_version_and_public_symbols_are_available() -> None:
    installed = Version(importlib.metadata.version("arch-kernel"))
    assert installed in SpecifierSet(">=0.1.0,<0.2.0")
    assert ArtifactRecord.__module__.startswith("arch_kernel.contracts")
    assert ProjectState.__module__.startswith("arch_kernel.contracts")
    assert callable(canonicalize_json)
    assert callable(compute_content_fingerprint)


def test_runtime_does_not_duplicate_kernel_contracts() -> None:
    kernel_contract_names = {"ArtifactRecord", "EventEnvelope", "ProjectState", "StatePatch"}
    runtime_classes = {
        node.name
        for path in PACKAGE_ROOT.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ClassDef)
    }
    assert runtime_classes.isdisjoint(kernel_contract_names)
    assert not (PACKAGE_ROOT / "contracts").exists()
