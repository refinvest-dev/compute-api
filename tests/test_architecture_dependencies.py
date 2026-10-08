"""Dependency-direction tests for the Engine and application boundaries."""

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"


@dataclass(frozen=True)
class DependencyRule:
    source_package: str
    forbidden_packages: tuple[str, ...]
    allowed_packages: tuple[str, ...] | None = None


RULES = (
    DependencyRule(
        "refinvest.trading_review",
        (
            "refinvest.api",
            "refinvest.app",
            "refinvest.application",
            "refinvest.engine",
            "refinvest.ingestion",
            "refinvest.marketdata",
            "refinvest.persistence",
            "tests",
        ),
        ("refinvest.trading_review",),
    ),
    DependencyRule(
        "refinvest.engine",
        (
            "refinvest.api",
            "refinvest.app",
            "refinvest.application",
            "refinvest.persistence",
            "refinvest.ingestion",
            "refinvest.marketdata.adapters",
        ),
    ),
    DependencyRule(
        "refinvest.application",
        (
            "refinvest.api",
            "refinvest.app",
            "refinvest.persistence",
            "refinvest.ingestion",
            "refinvest.marketdata.adapters",
        ),
        (
            "refinvest.application",
            "refinvest.engine",
            "refinvest.marketdata.domain",
            "refinvest.marketdata.ports",
            "refinvest.marketdata.errors",
        ),
    ),
)


def test_production_dependencies_follow_architecture() -> None:
    _assert_dependency_rules(SOURCE_ROOT)


def test_architecture_check_reports_absolute_relative_and_external_violations(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "src"
    _write_source(
        source_root / "refinvest/engine/absolute.py",
        "from refinvest.application import backtests\n",
    )
    _write_source(
        source_root / "refinvest/application/relative.py",
        "from ..marketdata import adapters\n",
    )
    _write_source(
        source_root / "refinvest/application/external.py",
        "import fastapi\n",
    )

    with pytest.raises(AssertionError) as raised:
        _assert_dependency_rules(source_root)

    message = str(raised.value)
    assert (
        "refinvest/engine/absolute.py:1: refinvest.engine must not import "
        "refinvest.application via 'from refinvest.application import backtests'" in message
    )
    assert (
        "refinvest/application/relative.py:1: refinvest.application must not import "
        "refinvest.marketdata.adapters via 'from ..marketdata import adapters'" in message
    )
    assert (
        "refinvest/application/external.py:1: refinvest.application may import only the Python "
        "standard library or approved refinvest packages via 'import fastapi'" in message
    )


def _assert_dependency_rules(source_root: Path) -> None:
    violations = _dependency_violations(source_root)
    assert not violations, "Architecture dependency violations:\n" + "\n".join(violations)


def _dependency_violations(source_root: Path) -> tuple[str, ...]:
    violations: list[str] = []
    for path in sorted((source_root / "refinvest").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        module = _module_name(source_root, path)
        rule = next((rule for rule in RULES if _matches(module, rule.source_package)), None)
        if rule is None:
            continue
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            statement = " ".join((ast.get_source_segment(source, node) or "").split())
            for imported in _imported_modules(node, module):
                violation = _violation(rule, imported)
                if violation is not None:
                    relative_path = path.relative_to(source_root).as_posix()
                    violations.append(
                        f"{relative_path}:{node.lineno}: {violation} via '{statement}'"
                    )
    return tuple(violations)


def _module_name(source_root: Path, path: Path) -> str:
    return ".".join(path.relative_to(source_root).with_suffix("").parts)


def _imported_modules(node: ast.Import | ast.ImportFrom, module: str) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)

    imported_from = _resolve_import_from(node, module)
    return tuple(
        imported_from if alias.name == "*" else f"{imported_from}.{alias.name}"
        for alias in node.names
    )


def _resolve_import_from(node: ast.ImportFrom, module: str) -> str:
    if node.level == 0:
        return node.module or ""
    current_package = module.split(".")[:-1]
    parent_count = node.level - 1
    anchor = current_package[: len(current_package) - parent_count]
    imported = node.module.split(".") if node.module else []
    return ".".join((*anchor, *imported))


def _violation(rule: DependencyRule, imported: str) -> str | None:
    forbidden = next(
        (package for package in rule.forbidden_packages if _matches(imported, package)), None
    )
    if forbidden is not None:
        return f"{rule.source_package} must not import {forbidden}"

    if rule.allowed_packages is None or _is_standard_library(imported):
        return None
    if any(_matches(imported, package) for package in rule.allowed_packages):
        return None
    return (
        f"{rule.source_package} may import only the Python standard library or approved "
        "refinvest packages"
    )


def _matches(module: str, package: str) -> bool:
    return module == package or module.startswith(f"{package}.")


def _is_standard_library(module: str) -> bool:
    return module.partition(".")[0] in sys.stdlib_module_names


def _write_source(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
