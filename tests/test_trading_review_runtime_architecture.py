"""Keep the normalization runtime independent of Core, Web, and test harnesses."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src/refinvest"
RUNTIME_FILES = (
    ROOT / "application/trading_review_jobs.py",
    ROOT / "persistence/postgres/trading_review_jobs.py",
    ROOT / "api/routes/trading_review_jobs.py",
    ROOT / "app/trading_review_normalization.py",
    ROOT / "app/trading_review_payload.py",
    ROOT / "app/trading_review_worker.py",
)


def test_runtime_does_not_import_cross_service_or_fixture_code() -> None:
    forbidden = (
        "tests.",
        "integration.",
        "refinvest.core",
        "refinvest.web",
        "refinvest.application.backtest",
    )
    for path in RUNTIME_FILES:
        module = ast.parse(path.read_text(encoding="utf-8"))
        imports = [
            name
            for node in ast.walk(module)
            for name in (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else []
            )
        ]
        assert all(not name.startswith(forbidden) for name in imports), path.name


def test_route_and_repository_do_not_run_reconstruction_inside_transactions() -> None:
    for path in (
        ROOT / "api/routes/trading_review_jobs.py",
        ROOT / "persistence/postgres/trading_review_jobs.py",
    ):
        source = path.read_text(encoding="utf-8")
        assert "normalize_and_reconstruct" not in source
        assert "tests.trading_review_harness" not in source
