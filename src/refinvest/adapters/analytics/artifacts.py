"""Bounded Analytics artifact HTTP reads with exact content verification."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from refinvest.application.analytics_input import (
    AnalyticsInputFailure,
    input_integer,
    input_mapping,
    input_time,
)
from refinvest.contracts.analytics.identity import strict_json

LEDGER_MEDIA = "application/vnd.refinvest.trading-analysis-input.v1+json"
MANIFEST_MEDIA = "application/vnd.refinvest.trading-reconciliation-manifest.v1+json"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def _reference_body(
    reference: dict[str, object],
    grant: dict[str, object],
    allowed_hosts: tuple[str, ...],
    should_stop: Callable[[], bool],
    max_input_bytes: int,
) -> bytes:
    uri = str(grant["accessUri"])
    parsed = urlsplit(uri)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.netloc.lower() not in allowed_hosts
    ):
        raise AnalyticsInputFailure("INPUT_REFERENCE_EXPIRED", retryable=True)
    expiry = input_time(grant["expiresAt"])
    if expiry <= datetime.now(UTC):
        raise AnalyticsInputFailure("INPUT_REFERENCE_EXPIRED", retryable=True)
    expected_size = input_integer(reference["byteSize"])
    expected_hash = str(reference["sha256"])
    if expected_size < 1:
        raise AnalyticsInputFailure("INPUT_SCHEMA_UNSUPPORTED")
    if expected_size > max_input_bytes:
        raise AnalyticsInputFailure("TEMPORARY_DEPENDENCY_FAILURE", retryable=True)
    output = bytearray()
    digest = hashlib.sha256()
    request = Request(
        uri, headers={"Accept": str(reference["mediaType"]), "Accept-Encoding": "identity"}
    )
    try:
        with build_opener(_NoRedirect()).open(request, timeout=20) as response:
            if response.status != 200:
                raise AnalyticsInputFailure("INPUT_STREAM_ABORTED", retryable=True)
            media = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if (
                media != reference["mediaType"]
                or response.headers.get("Content-Encoding", "identity").lower() != "identity"
            ):
                raise AnalyticsInputFailure("INPUT_SCHEMA_UNSUPPORTED")
            while True:
                if should_stop():
                    raise AnalyticsInputFailure("LEASE_LOST")
                if datetime.now(UTC) >= expiry:
                    raise AnalyticsInputFailure("INPUT_REFERENCE_EXPIRED", retryable=True)
                chunk = response.read(min(65536, expected_size + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                digest.update(chunk)
                if len(output) > expected_size:
                    raise AnalyticsInputFailure("INPUT_CONTENT_LENGTH_MISMATCH")
    except HTTPError as error:
        if error.code in {401, 403, 404, 410}:
            raise AnalyticsInputFailure("INPUT_REFERENCE_EXPIRED", retryable=True) from None
        raise AnalyticsInputFailure("INPUT_STREAM_ABORTED", retryable=True) from None
    except (URLError, TimeoutError, OSError):
        raise AnalyticsInputFailure("INPUT_STREAM_ABORTED", retryable=True) from None
    if len(output) != expected_size:
        raise AnalyticsInputFailure("INPUT_CONTENT_LENGTH_MISMATCH")
    if digest.hexdigest() != expected_hash:
        raise AnalyticsInputFailure("INPUT_CONTENT_HASH_MISMATCH")
    return bytes(output)


def download_inputs(
    request: dict[str, object],
    grants: dict[str, object],
    allowed_hosts: tuple[str, ...],
    should_stop: Callable[[], bool],
    max_input_bytes: int,
) -> tuple[dict[str, object], dict[str, object]]:
    ledger_reference = input_mapping(request["ledgerRevisionContent"])
    manifest_reference = input_mapping(request["reconciliationManifest"])
    if (
        ledger_reference["mediaType"] != LEDGER_MEDIA
        or manifest_reference["mediaType"] != MANIFEST_MEDIA
    ):
        raise AnalyticsInputFailure("INPUT_SCHEMA_UNSUPPORTED")
    ledger = strict_json(
        _reference_body(
            ledger_reference,
            input_mapping(grants["ledgerRevisionContent"]),
            allowed_hosts,
            should_stop,
            max_input_bytes,
        )
    )
    manifest = strict_json(
        _reference_body(
            manifest_reference,
            input_mapping(grants["reconciliationManifest"]),
            allowed_hosts,
            should_stop,
            max_input_bytes,
        )
    )
    return ledger, manifest
