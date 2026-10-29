"""Bounded, allowlisted Normalization artifact HTTP access."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from refinvest.application.normalization_execution import execution_failure

_MAX_ARTIFACT_BYTES = 52_428_800


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str
    ):  # type: ignore[no-untyped-def]
        return None


def download_normalization_artifact(
    grant: dict[str, object], allowed_hosts: tuple[str, ...]
) -> bytes:
    uri = str(grant.get("accessUri", ""))
    parsed = urlsplit(uri)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.netloc.lower() not in allowed_hosts
    ):
        raise execution_failure("ARTIFACT_ACCESS_DENIED", "ARTIFACT_FETCH", "INVALID_SIGNED_GRANT")
    expiry = grant.get("expiresAt")
    if not isinstance(expiry, str):
        raise execution_failure("ARTIFACT_ACCESS_DENIED", "ARTIFACT_FETCH", "GRANT_EXPIRY_MISSING")
    try:
        expires_at = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    except ValueError:
        raise execution_failure(
            "ARTIFACT_ACCESS_DENIED", "ARTIFACT_FETCH", "GRANT_EXPIRY_INVALID"
        ) from None
    if expires_at <= datetime.now(UTC):
        raise execution_failure(
            "ARTIFACT_REFERENCE_EXPIRED", "ARTIFACT_FETCH", "GRANT_EXPIRED", True
        )
    expected_size = grant.get("byteSize")
    expected_hash = grant.get("rawSha256")
    expected_content_type = grant.get("contentType")
    if not isinstance(expected_size, int) or not 1 <= expected_size <= _MAX_ARTIFACT_BYTES:
        raise execution_failure("FILE_FORMAT_INVALID", "ARTIFACT_FETCH", "ARTIFACT_SIZE_INVALID")
    if expected_content_type not in {"text/csv", "application/vnd.ms-excel"}:
        raise execution_failure("ARTIFACT_ACCESS_DENIED", "ARTIFACT_FETCH", "ARTIFACT_TYPE_INVALID")
    opener = build_opener(_NoRedirect())
    request = Request(uri, headers={"Accept": "text/csv, application/vnd.ms-excel"})
    try:
        with opener.open(request, timeout=20) as response:
            if response.status != 200:
                raise execution_failure(
                    "ARTIFACT_ACCESS_DENIED", "ARTIFACT_FETCH", "ARTIFACT_READ_DENIED"
                )
            actual_content_type = (
                response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            )
            if actual_content_type != expected_content_type:
                raise execution_failure(
                    "ARTIFACT_ACCESS_DENIED", "ARTIFACT_FETCH", "ARTIFACT_TYPE_MISMATCH"
                )
            content = response.read(_MAX_ARTIFACT_BYTES + 1)
    except HTTPError as error:
        if error.code in {301, 302, 303, 307, 308}:
            raise execution_failure(
                "ARTIFACT_ACCESS_DENIED", "ARTIFACT_FETCH", "GRANT_REDIRECT_REJECTED"
            ) from None
        if error.code in {401, 403, 404, 410}:
            code = "SOURCE_ARTIFACT_EXPIRED" if error.code == 410 else "ARTIFACT_ACCESS_DENIED"
            raise execution_failure(code, "ARTIFACT_FETCH", "ARTIFACT_READ_DENIED") from None
        raise execution_failure(
            "TEMPORARY_DEPENDENCY_FAILURE", "ARTIFACT_FETCH", "ARTIFACT_READ_FAILED", True
        ) from None
    except (URLError, TimeoutError, OSError):
        raise execution_failure(
            "TEMPORARY_DEPENDENCY_FAILURE", "ARTIFACT_FETCH", "ARTIFACT_READ_FAILED", True
        ) from None
    if len(content) != expected_size or len(content) > _MAX_ARTIFACT_BYTES:
        raise execution_failure(
            "SOURCE_CONTENT_LENGTH_MISMATCH", "ARTIFACT_FETCH", "ARTIFACT_SIZE_MISMATCH"
        )
    if not isinstance(expected_hash, str) or hashlib.sha256(content).hexdigest() != expected_hash:
        raise execution_failure(
            "SOURCE_CONTENT_HASH_MISMATCH", "ARTIFACT_FETCH", "ARTIFACT_HASH_MISMATCH"
        )
    return content
