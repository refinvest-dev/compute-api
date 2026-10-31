"""Deployment smoke check for one immutable snapshot's reference-data read path."""

import argparse
import json
from collections.abc import Callable
from typing import Protocol

from refinvest.app.config import Settings, get_settings
from refinvest.app.snapshot_storage import create_snapshot_execution_data_loader
from refinvest.application import reference_data_diagnostics
from refinvest.application.dataset_snapshots import DatasetSnapshotRepository
from refinvest.application.snapshot_execution_data import SnapshotExecutionDataLoader
from refinvest.persistence.postgres.dataset_snapshots import PostgresDatasetSnapshotRepository


class DatasetSnapshotRepositoryFactory(Protocol):
    def __call__(self, database_url: str) -> DatasetSnapshotRepository: ...


def verify_reference_data(
    settings: Settings,
    dataset_snapshot_id: str | None = None,
    *,
    snapshot_repository_factory: DatasetSnapshotRepositoryFactory = (
        PostgresDatasetSnapshotRepository
    ),
    loader_factory: Callable[[Settings], SnapshotExecutionDataLoader] = (
        create_snapshot_execution_data_loader
    ),
) -> dict[str, object]:
    """Compose snapshot adapters for the application-owned reference-data diagnostic."""
    if not settings.database_url:
        raise ValueError("REFINVEST_DATABASE_URL is required for reference-data smoke checks.")
    snapshots = snapshot_repository_factory(settings.database_url)
    return reference_data_diagnostics.verify_reference_data(
        snapshots,
        dataset_snapshot_id,
        loader_factory=lambda: loader_factory(settings),
    )


def reference_data_smoke_main() -> None:
    """Console-script entrypoint without traceback or credential leakage."""
    parsed = _parser().parse_args()
    try:
        result = verify_reference_data(get_settings(), parsed.dataset_snapshot_id)
    except Exception:
        raise SystemExit(1) from None
    print(json.dumps(result, separators=(",", ":"), sort_keys=True, default=str))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify one immutable reference-data snapshot.")
    parser.add_argument(
        "--dataset-snapshot-id",
        help="Verify this immutable snapshot instead of the latest published snapshot.",
    )
    return parser
