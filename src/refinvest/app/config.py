"""Configuration read at the service boundary."""

from dataclasses import dataclass
from os import getenv


@dataclass(frozen=True)
class Settings:
    """Runtime settings supplied by the deployment environment."""

    internal_api_key: str | None
    database_url: str | None = None

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            internal_api_key=getenv("REFINVEST_INTERNAL_API_KEY") or None,
            database_url=getenv("REFINVEST_DATABASE_URL") or None,
        )


def get_settings() -> Settings:
    """Return settings without introducing configuration dependencies prematurely."""
    return Settings.from_environment()
