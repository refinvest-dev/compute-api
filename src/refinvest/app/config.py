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
