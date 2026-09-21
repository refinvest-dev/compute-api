"""FastAPI application entrypoint."""

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from time import monotonic

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.responses import Response

from refinvest.api.routes.assets import router as assets_router
from refinvest.api.routes.backtests import router as backtests_router
from refinvest.api.routes.health import router as health_router
from refinvest.api.routes.series import router as series_router
from refinvest.api.schemas.errors import map_transport_validation_error
from refinvest.app.config import get_settings
from refinvest.observability.metrics import ApiMetrics, safe_record, serve_metrics


def request_validation_error_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    """Keep request-shape validation aligned with the OpenAPI 400 contract."""
    del request
    if not isinstance(error, RequestValidationError):
        raise error
    response = map_transport_validation_error(error)
    return JSONResponse(status_code=400, content=response.model_dump(by_alias=True))


def create_app() -> FastAPI:
    """Compose the HTTP service without importing future domain layers."""
    metrics = ApiMetrics()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
        with serve_metrics(get_settings().api_metrics, metrics.registry):
            yield

    application = FastAPI(title="refinvest Compute API", version="0.1.0", lifespan=lifespan)
    application.state.metrics = metrics

    async def observe_request(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = monotonic()
        status_code = 500
        try:
            response = await call_next(request)  # type: ignore[operator]
            status_code = response.status_code
            return response
        finally:
            path = request.scope.get("path", "")
            method = request.method
            operation = _operation(method, str(path))
            if operation is not None and get_settings().api_metrics.enabled:
                safe_record(_record_http, metrics, operation, status_code, monotonic() - started)

    application.middleware("http")(observe_request)

    # Deployment probes are deliberately unauthenticated. Future Core-facing
    # contract routes must apply app.security.require_internal_api_key.
    application.include_router(health_router)
    application.include_router(backtests_router)
    application.include_router(assets_router)
    application.include_router(series_router)
    application.add_exception_handler(RequestValidationError, request_validation_error_handler)
    return application


def _operation(method: str, path: str) -> str | None:
    if path == "/health":
        return None
    if method == "POST" and path == "/backtests":
        return "submit"
    if method == "GET" and path.startswith("/backtests/") and path.count("/") == 2:
        return "status"
    if method == "GET" and path.startswith("/assets"):
        return "assets"
    if method == "GET" and path.startswith("/series"):
        return "series"
    return "other"


def _record_http(metrics: ApiMetrics, operation: str, status: int, seconds: float) -> None:
    status_class = f"{status // 100}xx" if 200 <= status < 600 else "5xx"
    metrics.requests.labels(operation, status_class).inc()
    metrics.duration.labels(operation).observe(seconds)


app = create_app()


def main() -> None:
    """Run the API using the package's console entrypoint."""
    uvicorn.run("refinvest.app.main:app", host="0.0.0.0", port=8000)
