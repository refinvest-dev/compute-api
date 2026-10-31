"""Static import boundaries and exact, stage-owned migration exceptions."""

import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
LEGACY_IMPORTS_PATH = Path(__file__).with_name("architecture_legacy_imports.json")
NETWORK_PACKAGES = ("socket", "urllib", "http", "ftplib", "smtplib")
MARKET_DATA_INTERFACES = (
    "refinvest.marketdata.domain",
    "refinvest.marketdata.ports",
    "refinvest.marketdata.errors",
)
CONTRACT_PACKAGES = ("refinvest.contracts.normalization", "refinvest.contracts.analytics")


@dataclass(frozen=True)
class DependencyRule:
    source_package: str
    forbidden_packages: tuple[str, ...]
    allowed_packages: tuple[str, ...] | None = None


@dataclass(frozen=True)
class DependencyViolation:
    source_file: str
    imported: str
    line: int
    reason: str
    statement: str

    @property
    def key(self) -> tuple[str, str]:
        return self.source_file, self.imported

    def __str__(self) -> str:
        return f"{self.source_file}:{self.line}: {self.reason} via '{self.statement}'"


@dataclass(frozen=True)
class LegacyImport:
    source_file: str
    imported: str
    remove_in: str
    reason: str

    @property
    def key(self) -> tuple[str, str]:
        return self.source_file, self.imported


RULES = (
    DependencyRule("refinvest", ("tests", "integration", "refinvest.core", "refinvest.web")),
    DependencyRule("refinvest.__init__", (), ("refinvest.app.main.main",)),
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
            *NETWORK_PACKAGES,
            "os",
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
            *NETWORK_PACKAGES,
            "os",
        ),
        ("refinvest.engine", *MARKET_DATA_INTERFACES),
    ),
    DependencyRule(
        "refinvest.application",
        (
            "refinvest.api",
            "refinvest.app",
            "refinvest.persistence",
            "refinvest.ingestion",
            "refinvest.marketdata.adapters",
            *NETWORK_PACKAGES,
            "os",
            "multiprocessing",
            "subprocess",
        ),
        (
            "refinvest.application",
            "refinvest.engine",
            *MARKET_DATA_INTERFACES,
            *CONTRACT_PACKAGES,
            "refinvest.trading_review",
        ),
    ),
    DependencyRule(
        "refinvest.adapters.backtest",
        (
            "refinvest.api",
            "refinvest.app",
            "refinvest.persistence",
            "refinvest.ingestion",
            "refinvest.engine",
            "refinvest.marketdata",
            *NETWORK_PACKAGES,
            "os",
        ),
        ("refinvest.adapters.backtest", "refinvest.application.backtest_worker_host"),
    ),
    DependencyRule(
        "refinvest.adapters.normalization",
        (
            "refinvest.api",
            "refinvest.app",
            "refinvest.persistence",
            "refinvest.ingestion",
            "refinvest.engine",
            "refinvest.marketdata",
        ),
        (
            "refinvest.adapters.normalization",
            "refinvest.application.normalization_execution",
            "refinvest.application.trading_review_jobs",
        ),
    ),
    DependencyRule(
        "refinvest.adapters.analytics",
        (
            "refinvest.api",
            "refinvest.app",
            "refinvest.persistence",
            "refinvest.ingestion",
            "refinvest.engine",
            "refinvest.marketdata",
        ),
        (
            "refinvest.adapters.analytics",
            "refinvest.application.analytics_input",
            "refinvest.contracts.analytics",
        ),
    ),
    DependencyRule(
        "refinvest.application.analytics_input",
        ("multiprocessing", "subprocess", "pathlib"),
    ),
    DependencyRule(
        "refinvest.application.analytics_result",
        ("multiprocessing", "subprocess", "pathlib"),
    ),
    DependencyRule(
        "refinvest.application.normalization_execution",
        ("multiprocessing", "subprocess", "pathlib"),
    ),
    DependencyRule(
        "refinvest.contracts",
        (
            "refinvest.api",
            "refinvest.app",
            "refinvest.application",
            "refinvest.persistence",
            "refinvest.ingestion",
            "refinvest.engine",
            "refinvest.marketdata.adapters",
            *NETWORK_PACKAGES,
            "os",
            "pathlib",
            "subprocess",
            "multiprocessing",
        ),
        (*CONTRACT_PACKAGES, "pydantic", "refinvest.trading_review.versions"),
    ),
    DependencyRule("refinvest.marketdata.domain", (), ("refinvest.marketdata.domain",)),
    DependencyRule("refinvest.marketdata.errors", (), ("refinvest.marketdata.errors",)),
    DependencyRule("refinvest.marketdata.ports", (), MARKET_DATA_INTERFACES),
    DependencyRule(
        "refinvest.api.routes",
        ("refinvest.persistence", "refinvest.ingestion", "refinvest.api.routes"),
        (
            "fastapi",
            "starlette",
            "refinvest.api.schemas",
            "refinvest.api.dependencies",
            "refinvest.app.config",
            "refinvest.api.security",
            "refinvest.application",
            "refinvest.observability",
            "refinvest.engine.validation.StrategyValidationError",
            "refinvest.trading_review.decimal.ScaledDecimal",
            "refinvest.trading_review.errors.TradingReviewError",
            *MARKET_DATA_INTERFACES,
            *CONTRACT_PACKAGES,
        ),
    ),
    DependencyRule("refinvest.api.dependencies", ("refinvest.api.routes",)),
    DependencyRule(
        "refinvest.api.exception_handlers",
        (
            "refinvest.app",
            "refinvest.application",
            "refinvest.persistence",
            "refinvest.api.routes",
            "refinvest.api.dependencies",
            *NETWORK_PACKAGES,
        ),
        ("fastapi", "starlette", "refinvest.api.schemas.errors"),
    ),
    DependencyRule(
        "refinvest.api.http_metrics",
        (
            "refinvest.app",
            "refinvest.application",
            "refinvest.persistence",
            "refinvest.api.routes",
            "refinvest.api.dependencies",
            *NETWORK_PACKAGES,
        ),
        (
            "fastapi",
            "starlette",
            "refinvest.observability.metrics.ApiMetrics",
            "refinvest.observability.metrics.safe_record",
        ),
    ),
    DependencyRule(
        "refinvest.app.main",
        (
            "refinvest.api.schemas",
            "fastapi.responses.JSONResponse",
            "time.monotonic",
            "refinvest.observability.metrics.safe_record",
        ),
    ),
    DependencyRule(
        "refinvest.api.security",
        (
            "refinvest.application",
            "refinvest.persistence",
            "refinvest.api.routes",
            "refinvest.api.dependencies",
            *NETWORK_PACKAGES,
        ),
        ("fastapi", "refinvest.app.config"),
    ),
    DependencyRule(
        "refinvest.api.schemas",
        (
            "refinvest.app",
            "refinvest.persistence",
            "refinvest.ingestion",
            "refinvest.api.routes",
            "refinvest.api.dependencies",
        ),
        (
            "fastapi",
            "pydantic",
            "refinvest.api.schemas",
            "refinvest.engine.validation",
            *CONTRACT_PACKAGES,
        ),
    ),
    DependencyRule(
        "refinvest.persistence.postgres.normalization_rows",
        ("psycopg", "cryptography", "refinvest.persistence", *NETWORK_PACKAGES, "os"),
        ("refinvest.application.trading_review_jobs",),
    ),
    DependencyRule(
        "refinvest.persistence.postgres.normalization_payloads",
        ("psycopg", "refinvest.persistence", *NETWORK_PACKAGES),
        ("cryptography", "refinvest.contracts.normalization"),
    ),
    DependencyRule(
        "refinvest.persistence.postgres.normalization_grants",
        ("psycopg", "refinvest.persistence", *NETWORK_PACKAGES, "os"),
        (),
    ),
    DependencyRule(
        "refinvest.persistence.postgres.analytics_documents",
        ("psycopg", "cryptography", "refinvest.persistence", *NETWORK_PACKAGES, "os"),
        ("refinvest.application.analytics_jobs",),
    ),
    DependencyRule(
        "refinvest.persistence.postgres.analytics_payloads",
        ("psycopg", "refinvest.persistence", *NETWORK_PACKAGES),
        ("cryptography",),
    ),
    DependencyRule("refinvest.persistence", ("refinvest.app", "refinvest.api")),
)


def test_production_dependencies_follow_architecture() -> None:
    _assert_dependency_rules(SOURCE_ROOT, _load_legacy_imports(LEGACY_IMPORTS_PATH))


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
        "standard library or approved packages via 'import fastapi'" in message
    )


@pytest.mark.parametrize(
    ("path", "source"),
    (
        ("__init__.py", "from refinvest.app.main import app\n"),
        ("__init__.py", "from refinvest.app.config import get_settings\n"),
        ("__init__.py", "from fastapi import FastAPI\n"),
        ("__init__.py", "import uvicorn\n"),
        ("engine/new.py", "import fastapi\n"),
        ("engine/new.py", "from urllib.request import urlopen\n"),
        ("engine/new.py", "from refinvest.marketdata import adapters\n"),
        ("engine/new.py", "from refinvest.marketdata.domain_extra import Session\n"),
        ("engine/__init__.py", "from ..api import routes\n"),
        ("engine/execution/__init__.py", "from ...persistence import postgres\n"),
        ("engine/new.py", "from refinvest import persistence as storage\n"),
        ("engine/new.py", "import refinvest.persistence.postgres as storage\n"),
        ("engine/new.py", "from ..api import *\n"),
        ("engine/new.py", "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n import boto3\n"),
        ("trading_review/new.py", "from refinvest.contracts.analytics import Identity\n"),
        ("contracts/normalization/new.py", "from ...app.config import Settings\n"),
        ("contracts/analytics/new.py", "import httpx\n"),
        ("contracts/analytics/new.py", "from os import getenv\n"),
        ("contracts/normalization/new.py", "from pathlib import Path\n"),
        ("application/new.py", "from refinvest.contracts import unapproved\n"),
        ("application/new.py", "import refinvest.persistence.postgres\n"),
        ("api/routes/new.py", "from refinvest.app.security import require_internal_api_key\n"),
        ("api/security.py", "from refinvest.persistence.postgres import jobs\n"),
        ("api/security.py", "from refinvest.api.routes import assets\n"),
        ("api/security.py", "from refinvest.app.main import app\n"),
        ("api/exception_handlers.py", "from refinvest.app.config import get_settings\n"),
        ("api/exception_handlers.py", "from refinvest.persistence.postgres import jobs\n"),
        ("api/exception_handlers.py", "from refinvest.api.routes import backtests\n"),
        ("api/exception_handlers.py", "from refinvest.app.main import create_app\n"),
        ("api/http_metrics.py", "from refinvest.app.config import get_settings\n"),
        ("api/http_metrics.py", "from refinvest.persistence.postgres import jobs\n"),
        ("api/http_metrics.py", "from refinvest.api.dependencies import backtests\n"),
        ("api/http_metrics.py", "from refinvest.observability.metrics import serve_metrics\n"),
        ("app/main.py", "from time import monotonic\n"),
        ("app/main.py", "from refinvest.observability.metrics import safe_record\n"),
        (
            "app/main.py",
            "from refinvest.api.schemas.errors import map_transport_validation_error\n",
        ),
        ("application/new.py", "from socket import socket\n"),
        ("application/new.py", "from refinvest.adapters.normalization import Executor\n"),
        ("application/normalization_execution.py", "import multiprocessing\n"),
        ("adapters/normalization/new.py", "from refinvest.app.config import Settings\n"),
        ("adapters/normalization/new.py", "import psycopg\n"),
        ("adapters/normalization/new.py", "from refinvest.persistence.postgres import Jobs\n"),
        ("application/analytics_input.py", "from urllib.request import urlopen\n"),
        ("application/analytics_result.py", "from pathlib import Path\n"),
        ("application/analytics_input.py", "import subprocess\n"),
        ("application/analytics_worker.py", "from refinvest.adapters.analytics import artifacts\n"),
        ("adapters/analytics/new.py", "from refinvest.app.config import Settings\n"),
        ("adapters/analytics/new.py", "import psycopg\n"),
        ("adapters/analytics/new.py", "from refinvest.application.analytics_worker import Host\n"),
        ("application/new.py", "from multiprocessing import get_context\n"),
        ("application/new.py", "import subprocess\n"),
        ("adapters/backtest/new.py", "from refinvest.app.worker import factory\n"),
        ("adapters/backtest/new.py", "import psycopg\n"),
        ("adapters/backtest/new.py", "from refinvest.persistence.postgres import Jobs\n"),
        ("adapters/backtest/new.py", "from urllib.request import urlopen\n"),
        ("persistence/postgres/normalization_rows.py", "from psycopg import connect\n"),
        ("persistence/postgres/normalization_rows.py", "from cryptography import Cipher\n"),
        ("persistence/postgres/normalization_payloads.py", "from psycopg import connect\n"),
        (
            "persistence/postgres/normalization_payloads.py",
            "from refinvest.persistence.postgres import trading_review_jobs\n",
        ),
        ("persistence/postgres/normalization_grants.py", "from psycopg import connect\n"),
        ("persistence/postgres/analytics_documents.py", "from psycopg import connect\n"),
        ("persistence/postgres/analytics_documents.py", "from cryptography import Cipher\n"),
        ("persistence/postgres/analytics_payloads.py", "from psycopg import connect\n"),
        (
            "persistence/postgres/analytics_payloads.py",
            "from refinvest.persistence.postgres import trading_analytics_jobs\n",
        ),
        ("marketdata/ports.py", "from .adapters import in_memory\n"),
        ("api/routes/new.py", "from refinvest.persistence.postgres import Repository\n"),
        ("api/routes/new.py", "from .trading_review_jobs import router\n"),
        ("api/routes/new.py", "from refinvest.app.worker import create_backtest_worker\n"),
        ("api/schemas/new.py", "from refinvest.persistence.postgres import Repository\n"),
        ("api/dependencies/new.py", "from refinvest.api.routes import backtests\n"),
        ("api/dependencies/new.py", "from tests import fixtures\n"),
        ("persistence/postgres/new.py", "from refinvest.app.config import Settings\n"),
        ("app/new.py", "import integration\n"),
    ),
)
def test_forbidden_dependencies_are_rejected(tmp_path: Path, path: str, source: str) -> None:
    source_root = tmp_path / "src"
    _write_source(source_root / "refinvest" / path, source)

    with pytest.raises(AssertionError, match="Architecture dependency violations"):
        _assert_dependency_rules(source_root)


def test_pure_interfaces_contracts_and_composition_providers_are_allowed(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    sources = {
        "__init__.py": (
            "def main():\n from refinvest.app.main import main as api_main\n api_main()\n"
        ),
        "engine/validation/models.py": "from dataclasses import dataclass\n",
        "engine/execution/__init__.py": "from .entry import Entry\n",
        "engine/new.py": "from refinvest.marketdata.ports import SessionCalendarPort\n",
        "trading_review/new.py": "from .model import MoneyAmount\n",
        "contracts/normalization/__init__.py": "from .identity import Identity\n",
        "contracts/normalization/identity.py": (
            "from pydantic import BaseModel\n"
            "from refinvest.trading_review.versions import SUPPORTED_VERSION_SET\n"
        ),
        "contracts/analytics/identity.py": "from hashlib import sha256\n",
        "application/new.py": (
            "from refinvest.contracts.normalization import Identity\n"
            "from refinvest.contracts.analytics import Result\n"
            "from refinvest.trading_review.service import normalize_and_reconstruct\n"
        ),
        "api/routes/new.py": (
            "from fastapi import Depends\n"
            "from refinvest.api.dependencies.assets import get_dataset_snapshot_repository\n"
            "from refinvest.application.assets import AssetReference\n"
            "from refinvest.api.security import require_internal_api_key\n"
            "from refinvest.trading_review.decimal import ScaledDecimal\n"
            "from refinvest.trading_review.errors import TradingReviewError\n"
        ),
        "api/dependencies/new.py": (
            "from refinvest.app.config import Settings\n"
            "from refinvest.persistence.postgres.dataset_snapshots import Repository\n"
        ),
        "api/security.py": (
            "from fastapi import Depends, Header, HTTPException\n"
            "from refinvest.app.config import Settings, get_settings\n"
        ),
        "api/exception_handlers.py": (
            "from fastapi import Request\n"
            "from starlette.responses import JSONResponse\n"
            "from refinvest.api.schemas.errors import map_transport_validation_error\n"
        ),
        "api/http_metrics.py": (
            "from fastapi import FastAPI, Request\n"
            "from starlette.responses import Response\n"
            "from refinvest.observability.metrics import ApiMetrics, safe_record\n"
        ),
        "app/main.py": "from refinvest.api import exception_handlers, http_metrics\n",
        "api/schemas/new.py": "from refinvest.contracts.normalization import Identity\n",
        "persistence/postgres/new.py": "from psycopg import connect\n",
        "adapters/backtest/new.py": (
            "from multiprocessing import get_context\n"
            "from refinvest.application.backtest_worker_host import BacktestExecutionOutcome\n"
        ),
        "adapters/analytics/new.py": (
            "from urllib.request import build_opener\n"
            "from refinvest.application.analytics_input import AnalyticsInputFailure\n"
            "from refinvest.contracts.analytics.identity import strict_json\n"
        ),
        "adapters/normalization/new.py": (
            "from multiprocessing import get_context\n"
            "from urllib.request import build_opener\n"
            "from refinvest.application.normalization_execution import NormalizationOutcome\n"
        ),
    }
    for relative_path, source in sources.items():
        _write_source(source_root / "refinvest" / relative_path, source)

    _assert_dependency_rules(source_root)


@pytest.mark.parametrize(
    ("new_path", "new_import"),
    (
        ("api/routes/other.py", "from refinvest.persistence.postgres import Repository\n"),
        (
            "api/routes/legacy.py",
            "from refinvest.persistence.postgres import Repository, NewStore\n",
        ),
        ("api/routes/legacy.py", "from refinvest.persistence.postgres.child import Repository\n"),
    ),
)
def test_legacy_exception_does_not_allow_new_files_symbols_or_submodules(
    tmp_path: Path, new_path: str, new_import: str
) -> None:
    source_root = tmp_path / "src"
    legacy = LegacyImport(
        "refinvest/api/routes/legacy.py",
        "refinvest.persistence.postgres.Repository",
        "AR-03N",
        "Move the original handler to its application port.",
    )
    _write_source(
        source_root / legacy.source_file,
        "from refinvest.persistence.postgres import Repository\n",
    )
    _assert_dependency_rules(source_root, (legacy,))

    _write_source(source_root / "refinvest" / new_path, new_import)

    with pytest.raises(AssertionError, match="must not import refinvest.persistence"):
        _assert_dependency_rules(source_root, (legacy,))


def test_removed_dependency_requires_removing_its_exception(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    legacy = LegacyImport(
        "refinvest/persistence/postgres/legacy.py",
        "refinvest.app.old_integrity.verify",
        "AR-02N",
        "Move integrity to contracts.",
    )
    _write_source(source_root / legacy.source_file, "from hashlib import sha256\n")

    with pytest.raises(AssertionError, match="Stale exception:.*remove with AR-02N"):
        _assert_dependency_rules(source_root, (legacy,))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("source_file", "refinvest/api/routes/*.py"),
        ("source_file", "../refinvest/api/routes/legacy.py"),
        ("imported_from", "refinvest.app.*"),
        ("names", ["*"]),
        ("names", ["verify.child"]),
        ("names", []),
        ("remove_in", "later"),
        ("reason", ""),
    ),
)
def test_legacy_registry_rejects_broad_or_unowned_exceptions(
    tmp_path: Path, field: str, value: object
) -> None:
    record = {
        "source_file": "refinvest/api/routes/legacy.py",
        "imported_from": "refinvest.app.old_integrity",
        "names": ["verify"],
        "remove_in": "AR-02N",
        "reason": "Extract integrity contract.",
    }
    record[field] = value
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps([record]), encoding="utf-8")

    with pytest.raises(ValueError, match="exact file, symbols, reason and removal step"):
        _load_legacy_imports(path)


def test_legacy_registry_rejects_duplicate_edges(tmp_path: Path) -> None:
    record = {
        "source_file": "refinvest/api/routes/legacy.py",
        "imported_from": "refinvest.app.old_integrity",
        "names": ["verify"],
        "remove_in": "AR-02N",
        "reason": "Extract integrity contract.",
    }
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps([record, record]), encoding="utf-8")

    with pytest.raises(ValueError, match="Duplicate legacy import exception"):
        _load_legacy_imports(path)


def _assert_dependency_rules(
    source_root: Path, legacy_imports: tuple[LegacyImport, ...] = ()
) -> None:
    violations = _dependency_violations(source_root)
    allowed = {item.key for item in legacy_imports}
    actual = {item.key for item in violations}
    unexpected = [str(item) for item in violations if item.key not in allowed]
    stale = [
        f"Stale exception: {item.source_file} -> {item.imported}; remove with {item.remove_in}"
        for item in legacy_imports
        if item.key not in actual
    ]
    errors = (*unexpected, *stale)
    assert not errors, "Architecture dependency violations:\n" + "\n".join(errors)


def _dependency_violations(source_root: Path) -> tuple[DependencyViolation, ...]:
    violations: list[DependencyViolation] = []
    for path in sorted((source_root / "refinvest").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        module = _module_name(source_root, path)
        rules = tuple(rule for rule in RULES if _matches(module, rule.source_package))
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            statement = " ".join((ast.get_source_segment(source, node) or "").split())
            for imported in _imported_modules(node, module):
                violation = next(
                    (
                        reason
                        for rule in rules
                        if (reason := _violation(rule, imported)) is not None
                    ),
                    None,
                )
                if violation is not None:
                    relative_path = path.relative_to(source_root).as_posix()
                    violations.append(
                        DependencyViolation(
                            relative_path, imported, node.lineno, violation, statement
                        )
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
    return f"{rule.source_package} may import only the Python standard library or approved packages"


def _matches(module: str, package: str) -> bool:
    return module == package or module.startswith(f"{package}.")


def _is_standard_library(module: str) -> bool:
    return module.partition(".")[0] in sys.stdlib_module_names


def _load_legacy_imports(path: Path) -> tuple[LegacyImport, ...]:
    records = json.loads(path.read_text(encoding="utf-8"))
    result: list[LegacyImport] = []
    valid_steps = {
        "AR-02N",
        "AR-02A",
        "AR-03N",
        "AR-03A",
        "AR-04",
        "AR-05",
        "AR-06",
        "AR-07",
        "AR-11",
    }
    for record in records:
        source = record["source_file"]
        imported_from = record["imported_from"]
        names = record["names"]
        remove_in = record["remove_in"]
        reason = record["reason"]
        source_path = PurePosixPath(source)
        if (
            source != source_path.as_posix()
            or source_path.is_absolute()
            or ".." in source_path.parts
            or not re.fullmatch(r"refinvest/[A-Za-z0-9_/]+\.py", source)
            or not re.fullmatch(r"refinvest(?:\.[A-Za-z_]\w*)+", imported_from)
            or not isinstance(names, list)
            or not names
            or any(not name.isidentifier() for name in names)
            or remove_in not in valid_steps
            or not reason.strip()
        ):
            raise ValueError(
                "Legacy imports require an exact file, symbols, reason and removal step."
            )
        result.extend(
            LegacyImport(source, f"{imported_from}.{name}", remove_in, reason) for name in names
        )
    if len({item.key for item in result}) != len(result):
        raise ValueError("Duplicate legacy import exception.")
    return tuple(result)


def _write_source(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
