"""Internal HTTP authentication boundary for Core-to-Compute calls."""

from hmac import compare_digest
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from refinvest.app.config import Settings, get_settings


def require_internal_api_key(
    settings: Annotated[Settings, Depends(get_settings)],
    api_key: Annotated[str | None, Header(alias="X-Internal-Api-Key")] = None,
) -> None:
    """Require the configured key on Core-facing API routes."""
    if settings.internal_api_key is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Internal API key is not configured.",
        )
    if api_key is None or not compare_digest(api_key, settings.internal_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid internal API key.",
        )
