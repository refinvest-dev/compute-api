"""HTTP framework exception responses for the internal Compute API."""

from uuid import uuid4

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from refinvest.api.schemas.errors import map_transport_validation_error


def request_validation_error_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    """Keep request-shape validation aligned with the OpenAPI 400 contract."""
    if not isinstance(error, RequestValidationError):
        raise error
    if request.url.path.startswith("/trading-review/"):
        return JSONResponse(
            status_code=400,
            content={
                "code": "REQUEST_INVALID",
                "message": "Trading Review request shape is invalid.",
                "traceId": str(uuid4()),
                "retryable": False,
            },
        )
    response = map_transport_validation_error(error)
    return JSONResponse(status_code=400, content=response.model_dump(by_alias=True))


def internal_http_error_handler(request: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, StarletteHTTPException):
        raise error
    if request.url.path.startswith("/trading-review/"):
        reason = {
            401: "UNAUTHORIZED",
            404: "JOB_NOT_FOUND",
            503: "TEMPORARY_DEPENDENCY_FAILURE",
        }.get(error.status_code, "REQUEST_INVALID")
        return JSONResponse(
            status_code=error.status_code,
            content={
                "code": reason,
                "message": "Internal request could not be completed.",
                "traceId": str(uuid4()),
                "retryable": error.status_code == 503,
            },
        )
    return JSONResponse(status_code=error.status_code, content={"detail": error.detail})
