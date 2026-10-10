"""Configuration read at the service boundary."""

from base64 import b64decode
from binascii import Error as Base64Error
from dataclasses import dataclass
from os import getenv

from refinvest.observability.metrics import MetricsConfig


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
    backtest_execution_timeout_seconds: int = 600
    backtest_active_job_limit: int = 20
    backtest_job_retention_seconds: int = 86_400
    backtest_job_cleanup_batch_size: int = 1_000
    trading_review_execution_timeout_seconds: int = 600
    trading_review_cleanup_batch_size: int = 1_000
    trading_review_active_job_limit: int | None = None
    trading_review_capacity_retry_after_seconds: int = 30
    trading_review_grant_renewal_cutoff_seconds: int = 3_600
    trading_review_grant_refresh_wait_seconds: int = 300
    trading_review_max_attempts: int = 3
    trading_review_timezone_database_version: str | None = None
    trading_review_identity_key: str | None = None
    trading_review_masking_key: str | None = None
    trading_review_artifact_hosts: tuple[str, ...] = ()
    trading_review_payload_encryption_key: str | None = None
    massive_api_key: str | None = None
    snapshot_storage_root: str | None = None
    snapshot_storage_backend: str = "filesystem"
    snapshot_storage_s3_bucket: str | None = None
    snapshot_storage_s3_prefix: str | None = None
    snapshot_storage_s3_region: str | None = None
    snapshot_start_date: str | None = None
    us_equity_overrides_path: str | None = None
    api_metrics: MetricsConfig = MetricsConfig()
    worker_metrics: MetricsConfig = MetricsConfig(port=9102)

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
            backtest_execution_timeout_seconds=_positive_environment_integer(
                "REFINVEST_BACKTEST_EXECUTION_TIMEOUT_SECONDS", 600
            ),
            backtest_active_job_limit=_positive_environment_integer(
                "REFINVEST_BACKTEST_ACTIVE_JOB_LIMIT", 20
            ),
            backtest_job_retention_seconds=_positive_environment_integer(
                "REFINVEST_BACKTEST_JOB_RETENTION_SECONDS", 86_400
            ),
            backtest_job_cleanup_batch_size=_positive_environment_integer(
                "REFINVEST_BACKTEST_JOB_CLEANUP_BATCH_SIZE", 1_000
            ),
            trading_review_execution_timeout_seconds=_positive_environment_integer(
                "REFINVEST_TRADING_REVIEW_EXECUTION_TIMEOUT_SECONDS", 600
            ),
            trading_review_cleanup_batch_size=_positive_environment_integer(
                "REFINVEST_TRADING_REVIEW_CLEANUP_BATCH_SIZE", 1_000
            ),
            trading_review_active_job_limit=_optional_positive_environment_integer(
                "REFINVEST_TRADING_REVIEW_ACTIVE_JOB_LIMIT"
            ),
            trading_review_capacity_retry_after_seconds=_positive_environment_integer(
                "REFINVEST_TRADING_REVIEW_CAPACITY_RETRY_AFTER_SECONDS", 30
            ),
            trading_review_grant_renewal_cutoff_seconds=_positive_environment_integer(
                "REFINVEST_TRADING_REVIEW_GRANT_RENEWAL_CUTOFF_SECONDS", 3_600
            ),
            trading_review_grant_refresh_wait_seconds=_positive_environment_integer(
                "REFINVEST_TRADING_REVIEW_GRANT_REFRESH_WAIT_SECONDS", 300
            ),
            trading_review_max_attempts=_positive_environment_integer(
                "REFINVEST_TRADING_REVIEW_MAX_ATTEMPTS", 3
            ),
            trading_review_timezone_database_version=(
                getenv("REFINVEST_TRADING_REVIEW_TZDB_VERSION") or None
            ),
            trading_review_identity_key=(getenv("REFINVEST_TRADING_IDENTITY_KEY") or None),
            trading_review_masking_key=(getenv("REFINVEST_TRADING_MASKING_KEY") or None),
            trading_review_artifact_hosts=tuple(
                host.strip().lower()
                for host in getenv("REFINVEST_TRADING_REVIEW_ARTIFACT_HOSTS", "").split(",")
                if host.strip()
            ),
            trading_review_payload_encryption_key=(
                getenv("REFINVEST_TRADING_REVIEW_PAYLOAD_KEY") or None
            ),
            massive_api_key=getenv("REFINVEST_MASSIVE_API_KEY") or None,
            snapshot_storage_root=getenv("REFINVEST_SNAPSHOT_STORAGE_ROOT") or None,
            snapshot_storage_backend=getenv("REFINVEST_SNAPSHOT_STORAGE_BACKEND") or "filesystem",
            snapshot_storage_s3_bucket=getenv("REFINVEST_SNAPSHOT_STORAGE_S3_BUCKET") or None,
            snapshot_storage_s3_prefix=getenv("REFINVEST_SNAPSHOT_STORAGE_S3_PREFIX") or None,
            snapshot_storage_s3_region=getenv("REFINVEST_SNAPSHOT_STORAGE_S3_REGION") or None,
            snapshot_start_date=getenv("REFINVEST_SNAPSHOT_START_DATE") or None,
            us_equity_overrides_path=getenv("REFINVEST_US_EQUITY_OVERRIDES_PATH") or None,
            api_metrics=_metrics_config("API", 9101),
            worker_metrics=_metrics_config("WORKER", 9102),
        )


def get_settings() -> Settings:
    """Return settings without introducing configuration dependencies prematurely."""
    return Settings.from_environment()


def trading_review_payload_key(settings: Settings) -> bytes:
    """Decode the shared 256-bit API/worker payload key without logging its value."""
    value = settings.trading_review_payload_encryption_key
    if value is None:
        raise ValueError("REFINVEST_TRADING_REVIEW_PAYLOAD_KEY is required.")
    try:
        key = b64decode(value, altchars=b"-_", validate=True)
    except (Base64Error, ValueError) as error:
        raise ValueError("REFINVEST_TRADING_REVIEW_PAYLOAD_KEY must be URL-safe base64.") from error
    if len(key) != 32:
        raise ValueError("REFINVEST_TRADING_REVIEW_PAYLOAD_KEY must decode to 32 bytes.")
    return key


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


def _optional_positive_environment_integer(name: str) -> int | None:
    if getenv(name) is None:
        return None
    return _positive_environment_integer(name, 1)


def _metrics_config(role: str, default_port: int) -> MetricsConfig:
    prefix = f"REFINVEST_{role}_METRICS"
    enabled = getenv(f"{prefix}_ENABLED", "false").lower()
    if enabled not in {"true", "false"}:
        raise ValueError(f"{prefix}_ENABLED must be true or false.")
    raw_port = getenv(f"{prefix}_PORT", str(default_port))
    try:
        port = int(raw_port)
    except ValueError as error:
        raise ValueError(f"{prefix}_PORT must be an integer in 1..65535.") from error
    return MetricsConfig(enabled == "true", getenv(f"{prefix}_BIND", "127.0.0.1"), port)
