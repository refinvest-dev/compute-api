"""Configuration read at the service boundary."""

from dataclasses import dataclass
from os import getenv


@dataclass(frozen=True)
class Settings:
    """Runtime settings supplied by the deployment environment."""

    internal_api_key: str | None
    database_url: str | None = None
    engine_version: str = "0.1.0"
    worker_id: str | None = None
    worker_lease_seconds: int = 60
    worker_heartbeat_seconds: int = 15
    worker_poll_seconds: int = 1
    backtest_job_retention_seconds: int = 86_400
    backtest_job_cleanup_batch_size: int = 1_000
    massive_api_key: str | None = None
    snapshot_storage_root: str | None = None
    snapshot_storage_backend: str = "filesystem"
    snapshot_storage_s3_bucket: str | None = None
    snapshot_storage_s3_prefix: str | None = None
    snapshot_storage_s3_region: str | None = None
    snapshot_start_date: str | None = None
    us_equity_overrides_path: str | None = None

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            internal_api_key=getenv("REFINVEST_INTERNAL_API_KEY") or None,
            database_url=getenv("REFINVEST_DATABASE_URL") or None,
            engine_version=getenv("REFINVEST_ENGINE_VERSION") or "0.1.0",
            worker_id=getenv("REFINVEST_WORKER_ID") or None,
            worker_lease_seconds=_positive_environment_integer(
                "REFINVEST_WORKER_LEASE_SECONDS", 60
            ),
            worker_heartbeat_seconds=_positive_environment_integer(
                "REFINVEST_WORKER_HEARTBEAT_SECONDS", 15
            ),
            worker_poll_seconds=_positive_environment_integer("REFINVEST_WORKER_POLL_SECONDS", 1),
            backtest_job_retention_seconds=_positive_environment_integer(
                "REFINVEST_BACKTEST_JOB_RETENTION_SECONDS", 86_400
            ),
            backtest_job_cleanup_batch_size=_positive_environment_integer(
                "REFINVEST_BACKTEST_JOB_CLEANUP_BATCH_SIZE", 1_000
            ),
            massive_api_key=getenv("REFINVEST_MASSIVE_API_KEY") or None,
            snapshot_storage_root=getenv("REFINVEST_SNAPSHOT_STORAGE_ROOT") or None,
            snapshot_storage_backend=getenv("REFINVEST_SNAPSHOT_STORAGE_BACKEND") or "filesystem",
            snapshot_storage_s3_bucket=getenv("REFINVEST_SNAPSHOT_STORAGE_S3_BUCKET") or None,
            snapshot_storage_s3_prefix=getenv("REFINVEST_SNAPSHOT_STORAGE_S3_PREFIX") or None,
            snapshot_storage_s3_region=getenv("REFINVEST_SNAPSHOT_STORAGE_S3_REGION") or None,
            snapshot_start_date=getenv("REFINVEST_SNAPSHOT_START_DATE") or None,
            us_equity_overrides_path=getenv("REFINVEST_US_EQUITY_OVERRIDES_PATH") or None,
        )


def get_settings() -> Settings:
    """Return settings without introducing configuration dependencies prematurely."""
    return Settings.from_environment()


def _positive_environment_integer(name: str, default: int) -> int:
    value = getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive integer.") from error
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return parsed
