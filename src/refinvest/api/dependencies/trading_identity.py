"""Shared normalization HTTP identity validation and safe error envelope."""

import json
from collections.abc import Callable, Coroutine
from hmac import compare_digest
from typing import Any, cast
from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute

from refinvest.app.config import Settings, get_settings


def trading_http_error(
    code: int, reason: str, message: str, retryable: bool = False
) -> JSONResponse:
    return JSONResponse(
        status_code=code,
        content={
            "code": reason,
            "message": message,
            "traceId": str(uuid4()),
            "retryable": retryable,
        },
    )


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    values: dict[str, object] = {}
    for key, value in pairs:
        if key in values:
            raise ValueError("Duplicate JSON key.")
        values[key] = value
    return values


class TradingIdentityRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def reject_duplicate_keys(request: Request) -> Response:
            if request.method in {"POST", "PUT"} and request.url.path.startswith(
                "/trading-review/"
            ):
                settings_provider = cast(
                    Callable[[], Settings],
                    request.app.dependency_overrides.get(get_settings, get_settings),
                )
                configured_key = settings_provider().internal_api_key
                supplied_key = request.headers.get("X-Internal-Api-Key")
                if (
                    configured_key is not None
                    and supplied_key is not None
                    and compare_digest(configured_key, supplied_key)
                ):
                    try:
                        body = await request.body()
                        document: object = {}
                        if body:
                            document = json.loads(body, object_pairs_hook=_unique_json_object)
                    except (ValueError, UnicodeDecodeError):
                        return trading_http_error(422, "REQUEST_INVALID", "Malformed request body.")
                    if request.url.path == "/trading-review/normalization-jobs":
                        if not isinstance(document, dict):
                            return trading_http_error(
                                422, "REQUEST_INVALID", "Malformed request body."
                            )
                        version_request = cast(dict[str, object], document)
                        schema = version_request.get("resultSchemaVersion")
                        specification = version_request.get("resultHashSpecificationVersion")
                        if schema is None or specification is None:
                            return trading_http_error(
                                422, "REQUEST_INVALID", "Result version pin is required."
                            )
                        known_schema = {
                            "TRADING_NORMALIZATION_RESULT_V1",
                            "TRADING_NORMALIZATION_RESULT_V2",
                        }
                        known_specification = {
                            "TRADING_NORMALIZATION_RESULT_HASH_V1",
                            "TRADING_NORMALIZATION_RESULT_HASH_V2",
                        }
                        if (
                            not isinstance(schema, str)
                            or not isinstance(specification, str)
                            or schema not in known_schema
                            or specification not in known_specification
                        ):
                            return trading_http_error(
                                422, "RESULT_SCHEMA_UNSUPPORTED", "Result version is unsupported."
                            )
                        if schema[-2:] != specification[-2:]:
                            return trading_http_error(
                                422,
                                "RESULT_VERSION_PAIR_INVALID",
                                "Result version pair is invalid.",
                            )
            return await handler(request)

        return reject_duplicate_keys
