"""Shared terminal resource resolution and feature-specific acknowledgement routing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from refinvest.application.analytics_jobs import (
    AnalyticsJobConflict,
    AnalyticsJobUnavailable,
    AnalyticsResultDeliveryRepository,
)
from refinvest.application.trading_review_jobs import (
    NormalizationResultDeliveryRepository,
    TradingReviewIdempotencyConflict,
    TradingReviewJobStateConflict,
    hash_idempotency_key,
)
from refinvest.contracts.normalization.result_delivery import MEDIA_TYPE
from refinvest.contracts.normalization.result_integrity import ResultIntegrityError


class DeliveryFailureKind(StrEnum):
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    INVALID = "INVALID"
    UNAVAILABLE = "UNAVAILABLE"


class TerminalDeliveryFailure(ValueError):
    def __init__(
        self, kind: DeliveryFailureKind, code: str, message: str, *, retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class TerminalPayload:
    content: bytes
    media_type: str


@dataclass(frozen=True, slots=True)
class NormalizationAcknowledgement:
    core_aggregate_id: str
    payload_id: str
    payload_sha256: str
    terminal_result_hash: str
    attempt_token: str
    deletion_generation: int
    disposition: Literal["PERSISTED", "DISCARDED_STALE"]
    persisted_at: datetime | None
    discarded_at: datetime | None
    current_deletion_generation: int | None


@dataclass(frozen=True, slots=True)
class AnalyticsAcknowledgement:
    # One validated wire projection; no duplicate Analytics DTO or timestamp serialization.
    payload: dict[str, object]


def get_terminal_payload(
    payload_id: str,
    normalization: NormalizationResultDeliveryRepository,
    analytics: AnalyticsResultDeliveryRepository | None,
) -> TerminalPayload:
    try:
        payload = normalization.get_terminal_payload(payload_id)
    except ResultIntegrityError as error:
        raise TerminalDeliveryFailure(
            DeliveryFailureKind.CONFLICT, error.code, "Terminal result integrity failed."
        ) from error
    if payload is not None:
        return TerminalPayload(payload, MEDIA_TYPE)
    if analytics is not None:
        try:
            payload = analytics.payload_bytes(payload_id)
        except AnalyticsJobUnavailable as error:
            raise TerminalDeliveryFailure(
                DeliveryFailureKind.NOT_FOUND, "RESULT_PAYLOAD_EXPIRED", "Payload has expired."
            ) from error
        if payload is not None:
            return TerminalPayload(payload, "application/vnd.refinvest.trading-analytics+json")
    raise TerminalDeliveryFailure(
        DeliveryFailureKind.NOT_FOUND, "PAYLOAD_NOT_FOUND", "Payload was not found."
    )


def acknowledge_terminal_result(
    job_id: str,
    command: NormalizationAcknowledgement | AnalyticsAcknowledgement,
    idempotency_key: str,
    normalization: NormalizationResultDeliveryRepository,
    analytics: AnalyticsResultDeliveryRepository | None,
) -> None:
    if isinstance(command, AnalyticsAcknowledgement):
        if analytics is None:
            raise TerminalDeliveryFailure(
                DeliveryFailureKind.UNAVAILABLE,
                "TEMPORARY_DEPENDENCY_FAILURE",
                "Job store is unavailable.",
                retryable=True,
            )
        try:
            analytics.acknowledge(job_id, command.payload)
        except AnalyticsJobUnavailable as error:
            code = (
                "RESULT_PAYLOAD_EXPIRED"
                if str(error) == "RESULT_PAYLOAD_EXPIRED"
                else "JOB_NOT_FOUND"
            )
            raise TerminalDeliveryFailure(
                DeliveryFailureKind.NOT_FOUND, code, "Job or payload was not found."
            ) from error
        except AnalyticsJobConflict as error:
            raise TerminalDeliveryFailure(
                DeliveryFailureKind.CONFLICT, "RESULT_HASH_MISMATCH", "Acknowledgement conflicts."
            ) from error
        return
    recorded_at = (
        command.persisted_at if command.disposition == "PERSISTED" else command.discarded_at
    )
    if (
        recorded_at is None
        or (
            command.disposition == "PERSISTED"
            and (
                command.discarded_at is not None or command.current_deletion_generation is not None
            )
        )
        or (
            command.disposition == "DISCARDED_STALE"
            and (command.persisted_at is not None or command.current_deletion_generation is None)
        )
    ):
        raise TerminalDeliveryFailure(
            DeliveryFailureKind.INVALID, "REQUEST_INVALID", "Acknowledgement shape is invalid."
        )
    try:
        accepted = normalization.acknowledge(
            job_id,
            idempotency_key_hash=hash_idempotency_key(idempotency_key),
            core_aggregate_id=command.core_aggregate_id,
            payload_id=command.payload_id,
            payload_sha256=command.payload_sha256,
            terminal_result_hash=command.terminal_result_hash,
            attempt_token=command.attempt_token,
            deletion_generation=command.deletion_generation,
            disposition=command.disposition,
            recorded_at=recorded_at,
            current_deletion_generation=command.current_deletion_generation,
        )
    except (TradingReviewIdempotencyConflict, TradingReviewJobStateConflict) as error:
        raise TerminalDeliveryFailure(
            DeliveryFailureKind.CONFLICT,
            "RESULT_HASH_MISMATCH",
            "Acknowledgement conflicts with the result.",
        ) from error
    if not accepted:
        raise TerminalDeliveryFailure(
            DeliveryFailureKind.NOT_FOUND, "JOB_NOT_FOUND", "Job or payload was not found."
        )
