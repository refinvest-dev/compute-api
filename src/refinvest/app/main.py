"""FastAPI application entrypoint."""

import uvicorn
from fastapi import FastAPI

from refinvest.api.routes.health import router as health_router


def create_app() -> FastAPI:
    """Compose the HTTP service without importing future domain layers."""
    application = FastAPI(title="refinvest Compute API", version="0.1.0")

    # Deployment probes are deliberately unauthenticated. Future Core-facing
    # contract routes must apply app.security.require_internal_api_key.
    application.include_router(health_router)
    return application


app = create_app()


def main() -> None:
    """Run the API using the package's console entrypoint."""
    uvicorn.run("refinvest.app.main:app", host="0.0.0.0", port=8000)
