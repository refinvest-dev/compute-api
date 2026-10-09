"""Artifact access and process-isolated production reconstruction execution."""

from __future__ import annotations

import hashlib
import logging
import multiprocessing
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from time import monotonic
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from refinvest.app.config import Settings
from refinvest.app.trading_review_payload import MEDIA_TYPE, build_normalization_payload
from refinvest.application.trading_review_jobs import (
    ClaimedTradingReviewJob,
    SafeTradingReviewFailure,
)
from refinvest.trading_review.decimal import ScaledDecimal
from refinvest.trading_review.errors import TradingReviewError
from refinvest.trading_review.hashing import canonical_json_bytes, content_hash
from refinvest.trading_review.model import (
    InstrumentDescriptor,
    NormalizationRequest,
    NormalizationVersionSet,
    SourceArtifact,
    SourceArtifactRole,
)
from refinvest.trading_review.service import normalize_and_reconstruct

_LOGGER = logging.getLogger(__name__)
_MAX_ARTIFACT_BYTES = 52_428_800
_MAX_ATTEMPTS = 3
_RETRY_DELAYS = (timedelta(seconds=30), timedelta(minutes=2))
_PAYLOAD_LIMIT_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CompletedNormalization:
    payload: bytes
    media_type: str
    payload_sha256: str
    input_hash: str
    result_hash: str


@dataclass(frozen=True, slots=True)
class FailedNormalization:
    failure: SafeTradingReviewFailure
    retryable: bool


NormalizationOutcome = CompletedNormalization | FailedNormalization


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str
    ):  # type: ignore[no-untyped-def]
        return None


def execute_normalization_child(
    settings: Settings,
    claim: ClaimedTradingReviewJob,
    sender: Connection,
) -> None:
    """Child entrypoint: download grants, verify bytes, call production engine, serialize."""
    try:
        if not settings.trading_review_timezone_database_version:
            raise _configuration_error("REFINVEST_TRADING_REVIEW_TZDB_VERSION")
        identity_key = _secret_key(
            settings.trading_review_identity_key, "REFINVEST_TRADING_IDENTITY_KEY"
        )
        masking_key = _secret_key(
            settings.trading_review_masking_key, "REFINVEST_TRADING_MASKING_KEY"
        )
        request = claim.request_payload
        artifacts_node = request.get("artifacts")
        if not isinstance(artifacts_node, list) or len(cast(list[object], artifacts_node)) != 2:
            sender.send(
                FailedNormalization(
                    SafeTradingReviewFailure(
                        "FILE_FORMAT_INVALID",
                        "Exactly one artifact per role is required.",
                        False,
                        "VALIDATION",
                    ),
                    False,
                )
            )
            return
        grants = cast(list[object], artifacts_node)
        artifacts: list[SourceArtifact] = []
        downloaded_by_role: dict[str, SourceArtifact] = {}
        for raw_grant in grants:
            grant = cast(dict[str, object], raw_grant) if isinstance(raw_grant, dict) else None
            if grant is None:
                raise _safe_failure(
                    "FILE_FORMAT_INVALID", "VALIDATION", "ARTIFACT_DESCRIPTOR_INVALID"
                )
            role_value = str(grant.get("role", ""))
            if role_value not in {role.value for role in SourceArtifactRole}:
                raise _safe_failure("FILE_FORMAT_INVALID", "VALIDATION", "ARTIFACT_ROLE_INVALID")
            role = SourceArtifactRole(role_value)
            if role_value in downloaded_by_role:
                raise _safe_failure("FILE_FORMAT_INVALID", "VALIDATION", "DUPLICATE_ARTIFACT_ROLE")
            content = _download_artifact(grant, settings.trading_review_artifact_hosts)
            artifact = SourceArtifact(
                artifact_id=str(grant["coreArtifactId"]),
                role=role,
                dialect=_dialect_for_role(request, role),
                content=content,
            )
            artifacts.append(artifact)
            downloaded_by_role[role_value] = artifact
        if set(downloaded_by_role) != {role.value for role in SourceArtifactRole}:
            raise _safe_failure(
                "FILE_FORMAT_INVALID", "VALIDATION", "REQUIRED_ARTIFACT_ROLE_MISSING"
            )

        terms_node = request.get("pinnedInstrumentTerms")
        if not isinstance(terms_node, list) or not terms_node:
            raise _safe_failure(
                "INSTRUMENT_TERMS_UNAVAILABLE", "VALIDATION", "PINNED_TERMS_REQUIRED"
            )
        term_nodes = cast(list[object], terms_node)
        instruments = tuple(
            _instrument(cast(dict[str, object], item))
            for item in term_nodes
            if isinstance(item, dict)
        )
        if len(instruments) != len(term_nodes):
            raise _safe_failure(
                "INSTRUMENT_TERMS_UNAVAILABLE", "VALIDATION", "PINNED_TERMS_INVALID"
            )
        version_set = _version_set(request)
        engine_request = NormalizationRequest(
            artifacts=tuple(artifacts),
            source_timezone=str(request["sourceTimezone"]),
            timezone_database_version=settings.trading_review_timezone_database_version,
            declared_position_mode="ONE_WAY",
            instruments=instruments,
            version_set=version_set,
            stable_identity_key=identity_key,
        )
        result = normalize_and_reconstruct(engine_request)
        if content_hash(result) != result.result_hash:
            raise _safe_failure(
                "RESULT_HASH_MISMATCH", "PUBLICATION", "ENGINE_RESULT_HASH_MISMATCH"
            )
        if result.input_hash != claim.expected_input_hash:
            raise _safe_failure(
                "RESULT_HASH_MISMATCH", "VALIDATION", "EXPECTED_INPUT_HASH_MISMATCH"
            )
        terminal_at = datetime.now(UTC)
        payload_object = build_normalization_payload(
            result,
            job_id=claim.job_id,
            core_logical_job_id=claim.core_logical_job_id,
            core_import_session_id=claim.core_import_session_id,
            artifacts=tuple(artifacts),
            masking_key=masking_key,
            created_at=terminal_at,
            terminal_at=terminal_at,
            instrument_terms=instruments,
        )
        payload = canonical_json_bytes(payload_object)
        if not payload or len(payload) > _PAYLOAD_LIMIT_BYTES:
            raise _safe_failure("INCOMPLETE_ANALYSIS_RESULT", "PUBLICATION", "PAYLOAD_SIZE_INVALID")
        sender.send(
            CompletedNormalization(
                payload,
                MEDIA_TYPE,
                hashlib.sha256(payload).hexdigest(),
                result.input_hash,
                result.result_hash,
            )
        )
    except TradingReviewError as error:
        detail = error.detail
        safe_details: tuple[dict[str, object], ...] = ()
        if detail is not None:
            item: dict[str, object] = {}
            if detail.field is not None:
                item["field"] = detail.field
            if detail.reason is not None:
                item["reason"] = detail.reason
            if detail.row_number is not None:
                item["rowNumber"] = detail.row_number
            if detail.artifact_role is not None:
                item["artifactRole"] = detail.artifact_role
            safe_details = (item,)
        failure = SafeTradingReviewFailure(
            code=error.code.value,
            message="Normalization input failed a supported validation or reconstruction rule.",
            retryable=False,
            stage=error.stage.value,
            details=safe_details,
        )
        sender.send(FailedNormalization(failure, False))
    except _SafeExecutionError as error:
        sender.send(FailedNormalization(error.failure, error.retryable))
    except Exception:
        _LOGGER.warning("trading_review_normalization_failed")
        failure = SafeTradingReviewFailure(
            "INTERNAL_ERROR",
            "Normalization could not be completed.",
            True,
            "EXECUTION",
        )
        sender.send(FailedNormalization(failure, True))
    finally:
        sender.close()


class ProcessIsolatedNormalizationExecutor:
    """Keep lease ownership in the worker process and enforce a hard wall clock limit."""

    def __init__(self, settings: Settings, timeout: timedelta) -> None:
        if timeout <= timedelta(0):
            raise ValueError("timeout must be positive.")
        self._settings = settings
        self._timeout = timeout

    def execute(
        self,
        claim: ClaimedTradingReviewJob,
        cancellation_check: Callable[[], bool],
    ) -> NormalizationOutcome:
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=execute_normalization_child,
            args=(self._settings, claim, sender),
        )
        process.start()
        sender.close()
        deadline = monotonic() + self._timeout.total_seconds()
        try:
            while True:
                if cancellation_check():
                    _terminate(process)
                    return FailedNormalization(
                        SafeTradingReviewFailure(
                            "JOB_CANCELLED", "Normalization was cancelled.", False, "CANCELLATION"
                        ),
                        False,
                    )
                remaining = deadline - monotonic()
                if remaining <= 0:
                    _terminate(process)
                    return FailedNormalization(
                        SafeTradingReviewFailure(
                            "INTERNAL_ERROR",
                            "Normalization exceeded its time limit.",
                            False,
                            "TIMEOUT",
                        ),
                        False,
                    )
                if receiver.poll(min(remaining, 0.2)):
                    try:
                        outcome = cast(NormalizationOutcome, receiver.recv())
                    except EOFError:
                        process.join()
                        return FailedNormalization(
                            SafeTradingReviewFailure(
                                "TEMPORARY_DEPENDENCY_FAILURE",
                                "Normalization worker exited without a result.",
                                True,
                                "EXECUTION",
                            ),
                            True,
                        )
                    process.join(max(0, deadline - monotonic()))
                    if process.is_alive():
                        _terminate(process)
                        return FailedNormalization(
                            SafeTradingReviewFailure(
                                "INTERNAL_ERROR",
                                "Normalization exceeded its time limit.",
                                False,
                                "TIMEOUT",
                            ),
                            False,
                        )
                    return outcome
                if not process.is_alive():
                    process.join()
                    return FailedNormalization(
                        SafeTradingReviewFailure(
                            "TEMPORARY_DEPENDENCY_FAILURE",
                            "Normalization worker exited without a result.",
                            True,
                            "EXECUTION",
                        ),
                        True,
                    )
        finally:
            receiver.close()


class _SafeExecutionError(Exception):
    def __init__(self, failure: SafeTradingReviewFailure, retryable: bool) -> None:
        self.failure = failure
        self.retryable = retryable


def retry_delay(attempt_number: int) -> timedelta | None:
    if attempt_number < 1 or attempt_number >= _MAX_ATTEMPTS:
        return None
    return _RETRY_DELAYS[attempt_number - 1]


def _download_artifact(grant: dict[str, object], allowed_hosts: tuple[str, ...]) -> bytes:
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
        raise _safe_failure("ARTIFACT_ACCESS_DENIED", "ARTIFACT_FETCH", "INVALID_SIGNED_GRANT")
    expiry = grant.get("expiresAt")
    if not isinstance(expiry, str):
        raise _safe_failure("ARTIFACT_ACCESS_DENIED", "ARTIFACT_FETCH", "GRANT_EXPIRY_MISSING")
    try:
        expires_at = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    except ValueError:
        raise _safe_failure(
            "ARTIFACT_ACCESS_DENIED", "ARTIFACT_FETCH", "GRANT_EXPIRY_INVALID"
        ) from None
    if expires_at <= datetime.now(UTC):
        raise _safe_failure("ARTIFACT_REFERENCE_EXPIRED", "ARTIFACT_FETCH", "GRANT_EXPIRED", True)
    expected_size = grant.get("byteSize")
    expected_hash = grant.get("rawSha256")
    if not isinstance(expected_size, int) or not 1 <= expected_size <= _MAX_ARTIFACT_BYTES:
        raise _safe_failure("FILE_FORMAT_INVALID", "ARTIFACT_FETCH", "ARTIFACT_SIZE_INVALID")
    opener = build_opener(_NoRedirect())
    request = Request(uri, headers={"Accept": "text/csv, application/vnd.ms-excel"})
    try:
        with opener.open(request, timeout=20) as response:
            if response.status != 200:
                raise _safe_failure(
                    "ARTIFACT_ACCESS_DENIED", "ARTIFACT_FETCH", "ARTIFACT_READ_DENIED"
                )
            content = response.read(_MAX_ARTIFACT_BYTES + 1)
    except HTTPError as error:
        if error.code in {301, 302, 303, 307, 308}:
            raise _safe_failure(
                "ARTIFACT_ACCESS_DENIED", "ARTIFACT_FETCH", "GRANT_REDIRECT_REJECTED"
            ) from None
        if error.code in {401, 403, 404, 410}:
            code = "ARTIFACT_REFERENCE_EXPIRED" if error.code == 410 else "ARTIFACT_ACCESS_DENIED"
            raise _safe_failure(
                code, "ARTIFACT_FETCH", "ARTIFACT_READ_DENIED", error.code == 410
            ) from None
        raise _safe_failure(
            "TEMPORARY_DEPENDENCY_FAILURE", "ARTIFACT_FETCH", "ARTIFACT_READ_FAILED", True
        ) from None
    except (URLError, TimeoutError, OSError):
        raise _safe_failure(
            "TEMPORARY_DEPENDENCY_FAILURE", "ARTIFACT_FETCH", "ARTIFACT_READ_FAILED", True
        ) from None
    if len(content) != expected_size or len(content) > _MAX_ARTIFACT_BYTES:
        raise _safe_failure("FILE_FORMAT_INVALID", "ARTIFACT_FETCH", "ARTIFACT_SIZE_MISMATCH")
    if not isinstance(expected_hash, str) or hashlib.sha256(content).hexdigest() != expected_hash:
        raise _safe_failure("FILE_FORMAT_INVALID", "ARTIFACT_FETCH", "ARTIFACT_HASH_MISMATCH")
    return content


def _version_set(request: dict[str, object]) -> NormalizationVersionSet:
    raw_node = request.get("targetVersionSet")
    if not isinstance(raw_node, dict):
        raise _safe_failure("VERSION_INCOMPATIBLE", "VALIDATION", "VERSION_SET_INVALID")
    node = cast(dict[str, object], raw_node)
    role_versions = node.get("sourceDialectVersionsByArtifactRole")
    if not isinstance(role_versions, dict):
        raise _safe_failure("VERSION_INCOMPATIBLE", "VALIDATION", "VERSION_SET_INVALID")
    versions = cast(dict[str, object], role_versions)
    try:
        entries = tuple(
            sorted(
                (
                    (SourceArtifactRole(str(role)), str(version))
                    for role, version in versions.items()
                ),
                key=lambda item: item[0].value,
            )
        )
        return NormalizationVersionSet(
            source_dialect_versions_by_artifact_role=entries,
            adapter_version=str(node["adapterVersion"]),
            normalizer_version=str(node["normalizerVersion"]),
            canonical_schema_version=str(node["canonicalSchemaVersion"]),
            reconstruction_policy_version=str(node["reconstructionPolicyVersion"]),
            reconciliation_version=str(node["reconciliationVersion"]),
            review_unit_schema_version=str(node["reviewUnitSchemaVersion"]),
            evidence_schema_version=str(node["evidenceSchemaVersion"]),
            implementation_digest=str(node["implementationDigest"]),
            version_set_hash=str(node["versionSetHash"]),
        )
    except (KeyError, ValueError, TypeError):
        raise _safe_failure("VERSION_INCOMPATIBLE", "VALIDATION", "VERSION_SET_INVALID") from None


def _instrument(node: dict[str, object]) -> InstrumentDescriptor:
    try:
        return InstrumentDescriptor(
            instrument_id=str(node["instrumentId"]),
            venue_symbol=str(node["venueSymbol"]),
            settlement_asset=str(node["settlementAsset"]),
            quantity_step=ScaledDecimal.parse(str(node["quantityStep"])),
            price_tick=ScaledDecimal.parse(str(node["priceTick"])),
            contract_multiplier=ScaledDecimal.parse(str(node["contractMultiplier"])),
            settlement_precision=int(str(node["settlementPrecision"])),
            instrument_terms_version=str(node["instrumentTermsVersion"]),
        )
    except (KeyError, ValueError, TypeError):
        raise _safe_failure(
            "INSTRUMENT_TERMS_UNAVAILABLE", "VALIDATION", "PINNED_TERMS_INVALID"
        ) from None


def _dialect_for_role(request: dict[str, object], role: SourceArtifactRole) -> str:
    versions = cast(
        dict[str, object],
        cast(dict[str, object], request["targetVersionSet"])["sourceDialectVersionsByArtifactRole"],
    )
    value = versions.get(role.value)
    if not isinstance(value, str) or not value:
        raise _safe_failure("VERSION_INCOMPATIBLE", "VALIDATION", "ROLE_DIALECT_VERSION_MISSING")
    return value


def _secret_key(value: str | None, name: str) -> bytes:
    if value is None or len(value.encode("utf-8")) < 32:
        raise _configuration_error(name)
    return value.encode("utf-8")


def _configuration_error(name: str) -> _SafeExecutionError:
    return _safe_failure("VERSION_UNAVAILABLE", "CONFIGURATION", f"{name}_NOT_CONFIGURED")


def _safe_failure(
    code: str, stage: str, reason: str, retryable: bool = False
) -> _SafeExecutionError:
    return _SafeExecutionError(
        SafeTradingReviewFailure(
            code,
            "Normalization input or runtime dependency could not be processed.",
            retryable,
            stage,
            ({"field": "artifact", "reason": reason},) if stage == "ARTIFACT_FETCH" else (),
        ),
        retryable,
    )


def _terminate(process: BaseProcess) -> None:
    process.terminate()
    process.join(5)
    if process.is_alive():
        process.kill()
        process.join()
