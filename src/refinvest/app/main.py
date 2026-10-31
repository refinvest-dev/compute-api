"""FastAPI application entrypoint."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from refinvest.api import exception_handlers, http_metrics
from refinvest.api.routes.assets import router as assets_router
from refinvest.api.routes.backtests import router as backtests_router
from refinvest.api.routes.health import router as health_router
from refinvest.api.routes.series import router as series_router
from refinvest.api.routes.trading_analytics_jobs import router as trading_analytics_jobs_router
from refinvest.api.routes.trading_review_jobs import router as trading_review_jobs_router
from refinvest.app.config import get_settings
from refinvest.observability.metrics import ApiMetrics, serve_metrics


def create_app() -> FastAPI:
    """Compose the HTTP service without importing future domain layers."""
    metrics = ApiMetrics()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
        with serve_metrics(get_settings().api_metrics, metrics.registry):
            yield

    application = FastAPI(title="refinvest Compute API", version="0.1.0", lifespan=lifespan)
    application.state.metrics = metrics

    http_metrics.install_http_metrics(
        application, metrics, lambda: get_settings().api_metrics.enabled
    )

    # Deployment probes are deliberately unauthenticated. Future Core-facing
    # contract routes must apply api.security.require_internal_api_key.
    application.include_router(health_router)
    application.include_router(backtests_router)
    application.include_router(assets_router)
    application.include_router(series_router)
    application.include_router(trading_review_jobs_router)
    application.include_router(trading_analytics_jobs_router)
    application.add_exception_handler(
        RequestValidationError, exception_handlers.request_validation_error_handler
    )
    application.add_exception_handler(
        StarletteHTTPException, exception_handlers.internal_http_error_handler
    )
    return application


app = create_app()


def main() -> None:
    """Run the API using the package's console entrypoint."""
    uvicorn.run("refinvest.app.main:app", host="0.0.0.0", port=8000)
