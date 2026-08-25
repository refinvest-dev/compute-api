"""Deployment health probe route."""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    """Confirm that the application is serving HTTP requests."""
    return {"status": "ok"}
