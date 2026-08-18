import ast
import importlib.util
from collections.abc import Iterable
from pathlib import Path

import pytest

pytestmark = pytest.mark.architecture
PACKAGE_ROOT = Path(__file__).parents[2] / "src" / "arch_runtime"
EXCLUDED_FRAMEWORKS = {"django", "fastapi", "flask", "sqlalchemy", "typer"}


def _modules(tree: ast.AST) -> Iterable[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def _source_trees(root: Path) -> Iterable[tuple[Path, ast.Module]]:
    for path in sorted(root.rglob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_runtime_does_not_import_excluded_frameworks() -> None:
    imported = {
        module.split(".", maxsplit=1)[0]
        for _, tree in _source_trees(PACKAGE_ROOT)
        for module in _modules(tree)
    }
    assert imported.isdisjoint(EXCLUDED_FRAMEWORKS)


def test_sqlite_imports_are_confined_to_sqlite_foundation() -> None:
    violations = [
        (path, module)
        for path, tree in _source_trees(PACKAGE_ROOT)
        for module in _modules(tree)
        if module == "sqlite3" or module.startswith("sqlite3.")
        if "sqlite" not in path.relative_to(PACKAGE_ROOT).parts
    ]
    assert violations == []


def test_runtime_uses_only_public_kernel_modules() -> None:
    private_imports = [
        (path, module)
        for path, tree in _source_trees(PACKAGE_ROOT)
        for module in _modules(tree)
        if module.startswith("arch_kernel.")
        and any(part.startswith("_") for part in module.split(".")[1:])
    ]
    assert private_imports == []


@pytest.mark.parametrize(
    ("layer", "forbidden_prefixes"),
    [
        ("ports", ("arch_runtime.persistence", "arch_runtime.application")),
        ("application", ("arch_runtime.persistence",)),
    ],
)
def test_layers_do_not_cross_forbidden_boundaries(
    layer: str,
    forbidden_prefixes: tuple[str, ...],
) -> None:
    violations = [
        (path, module)
        for path, tree in _source_trees(PACKAGE_ROOT / layer)
        for module in _modules(tree)
        if module.startswith(forbidden_prefixes)
    ]
    assert violations == []


def test_package_root_exposes_only_stable_public_runtime_modules() -> None:
    tree = ast.parse((PACKAGE_ROOT / "__init__.py").read_text(encoding="utf-8"))
    assert set(_modules(tree)) == {
        "arch_runtime.__about__",
        "arch_runtime.application",
        "arch_runtime.errors",
        "arch_runtime.replay",
        "arch_runtime.runtime",
    }


def test_production_modules_have_no_import_time_calls_or_mutable_singletons() -> None:
    violations: list[tuple[Path, int]] = []
    for path, tree in _source_trees(PACKAGE_ROOT):
        for node in tree.body:
            value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign, ast.Expr)) else None
            if isinstance(value, (ast.Call, ast.Dict, ast.List, ast.Set)):
                violations.append((path, node.lineno))
    assert violations == []


def test_kernel_has_no_reverse_runtime_dependency() -> None:
    spec = importlib.util.find_spec("arch_kernel")
    assert spec is not None
    assert spec.origin is not None
    kernel_root = Path(spec.origin).parent
    imports = [
        (path, module)
        for path, tree in _source_trees(kernel_root)
        for module in _modules(tree)
        if module == "arch_runtime" or module.startswith("arch_runtime.")
    ]
    assert imports == []
