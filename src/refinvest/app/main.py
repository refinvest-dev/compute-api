"""FastAPI application entrypoint."""

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from refinvest.api.routes.assets import router as assets_router
from refinvest.api.routes.backtests import router as backtests_router
from refinvest.api.routes.health import router as health_router
from refinvest.api.schemas.errors import map_transport_validation_error


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
    application = FastAPI(title="refinvest Compute API", version="0.1.0")

    # Deployment probes are deliberately unauthenticated. Future Core-facing
    # contract routes must apply app.security.require_internal_api_key.
    application.include_router(health_router)
    application.include_router(backtests_router)
    application.include_router(assets_router)
    application.add_exception_handler(RequestValidationError, request_validation_error_handler)
    return application


app = create_app()


def main() -> None:
    """Run the API using the package's console entrypoint."""
    uvicorn.run("refinvest.app.main:app", host="0.0.0.0", port=8000)
