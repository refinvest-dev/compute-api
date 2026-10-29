"""Analytics job ports and orchestration, independent of HTTP and PostgreSQL."""

from __future__ import annotations

from typing import Protocol, cast

from refinvest.application.trading_review_jobs import hash_idempotency_key
from refinvest.contracts.analytics.identity import (
    create_fingerprint,
    require_supported_version_set,
    validate_create_identity,
)


class AnalyticsJobConflict(ValueError):
    pass


class AnalyticsJobCapacity(ValueError):
    pass


class AnalyticsJobUnavailable(ValueError):
    pass


class AnalyticsAdmissionUnavailable(ValueError):
    """The input size or deployed implementation configuration prevents admission."""


class AnalyticsJobNotFound(ValueError):
    """The requested job does not exist."""


class AnalyticsResultUnavailable(ValueError):
    """The job is not completed or has no available descriptor."""


class AnalyticsResultExpired(ValueError):
    """Descriptor retrieval reports that the terminal resource is unavailable."""


class AnalyticsCancellationConflict(ValueError):
    """Cancellation conflicts with the persisted job state."""


class AnalyticsJobRepository(Protocol):
    def create(
        self, request: dict[str, object], key_hash: str, fingerprint: str
    ) -> tuple[dict[str, object], bool]: ...

    def get(self, job_id: str) -> dict[str, object] | None: ...

    def update_grants(
        self, job_id: str, update: dict[str, object], key_hash: str
    ) -> dict[str, object]: ...

    def cancel(self, job_id: str, key_hash: str) -> bool: ...

    def result_descriptor(self, job_id: str) -> dict[str, object] | None: ...


class AnalyticsResultDeliveryRepository(Protocol):
    """Existing shared payload/ACK capabilities; orchestration moves in AR-04."""

    def payload_bytes(self, payload_id: str) -> bytes | None: ...

    def acknowledge(self, job_id: str, body: dict[str, object]) -> None: ...


class AnalyticsRepository(AnalyticsJobRepository, AnalyticsResultDeliveryRepository, Protocol):
    """Composition port implemented by the current adapter, without worker capabilities."""


def submit_analytics_job(
    request: dict[str, object],
    idempotency_key: str,
    repository: AnalyticsJobRepository,
    *,
    max_input_bytes: int,
    implementation_digest: str | None,
) -> tuple[dict[str, object], bool]:
    """Accept the one validated wire projection; retain size → identity → version order."""
    if any(
        cast(int, cast(dict[str, object], request[role])["byteSize"]) > max_input_bytes
        for role in ("ledgerRevisionContent", "reconciliationManifest")
    ):
        raise AnalyticsAdmissionUnavailable("TEMPORARY_DEPENDENCY_FAILURE")
    validate_create_identity(request)
    if implementation_digest is None:
        raise AnalyticsAdmissionUnavailable("TEMPORARY_DEPENDENCY_FAILURE")
    require_supported_version_set(request["targetVersionSet"], implementation_digest)
    return repository.create(
        request, hash_idempotency_key(idempotency_key), create_fingerprint(request)
    )


def get_analytics_job_status(
    job_id: str, repository: AnalyticsJobRepository
) -> dict[str, object] | None:
    return repository.get(job_id)


def get_analytics_result(job_id: str, repository: AnalyticsJobRepository) -> dict[str, object]:
    job = repository.get(job_id)
    if job is None:
        raise AnalyticsJobNotFound("JOB_NOT_FOUND")
    if job["status"] != "COMPLETED":
        raise AnalyticsResultUnavailable("RESULT_NOT_AVAILABLE")
    try:
        result = repository.result_descriptor(job_id)
    except AnalyticsJobUnavailable as error:
        raise AnalyticsResultExpired("RESULT_PAYLOAD_EXPIRED") from error
    if result is None:
        raise AnalyticsResultUnavailable("RESULT_NOT_AVAILABLE")
    return result


def cancel_analytics_job(
    job_id: str, idempotency_key: str, repository: AnalyticsJobRepository
) -> dict[str, object] | None:
    try:
        repository.cancel(job_id, hash_idempotency_key(idempotency_key))
    except AnalyticsJobUnavailable as error:
        raise AnalyticsJobNotFound("JOB_NOT_FOUND") from error
    except AnalyticsJobConflict as error:
        raise AnalyticsCancellationConflict("JOB_STATE_CONFLICT") from error
    # Keep this query outside the command error mapping, matching the original handler.
    return repository.get(job_id)


def update_analytics_grants(
    job_id: str,
    payload: dict[str, object],
    idempotency_key: str,
    repository: AnalyticsJobRepository,
) -> dict[str, object]:
    return repository.update_grants(job_id, payload, hash_idempotency_key(idempotency_key))
