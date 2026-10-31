"""HTTP request observation with bounded labels and opt-in recording."""

from collections.abc import Awaitable, Callable
from time import monotonic

from fastapi import FastAPI, Request
from starlette.responses import Response

from refinvest.observability.metrics import ApiMetrics, safe_record


def install_http_metrics(
    application: FastAPI, metrics: ApiMetrics, enabled: Callable[[], bool]
) -> None:
    """Register HTTP observation; composition supplies the current enabled setting."""

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
            if operation is not None and enabled():
                safe_record(_record_http, metrics, operation, status_code, monotonic() - started)

    application.middleware("http")(observe_request)


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
    if path.startswith("/trading-review/"):
        return "trading_review"
    return "other"


def _record_http(metrics: ApiMetrics, operation: str, status: int, seconds: float) -> None:
    status_class = f"{status // 100}xx" if 200 <= status < 600 else "5xx"
    metrics.requests.labels(operation, status_class).inc()
    metrics.duration.labels(operation).observe(seconds)
