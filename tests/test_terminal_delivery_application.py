"""Shared delivery ordering, exact bytes and feature-specific ACK boundaries."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from refinvest.application.analytics_jobs import AnalyticsJobConflict, AnalyticsJobUnavailable
from refinvest.application.terminal_delivery import (
    AnalyticsAcknowledgement,
    DeliveryFailureKind,
    NormalizationAcknowledgement,
    TerminalDeliveryFailure,
    acknowledge_terminal_result,
    get_terminal_payload,
)
from refinvest.application.trading_review_jobs import (
    TradingReviewIdempotencyConflict,
    TradingReviewJobStateConflict,
    hash_idempotency_key,
)
from refinvest.contracts.normalization.result_integrity import ResultIntegrityError


class NormalizationDelivery:
    def __init__(self, calls, payload=None, error=None, accepted=True):
        self.calls, self.payload, self.error, self.accepted = calls, payload, error, accepted

    def get_terminal_payload(self, payload_id):
        self.calls.append(("normalization-payload", payload_id))
        if self.error:
            raise self.error
        return self.payload

    def acknowledge(self, job_id, **fields):
        self.calls.append(("normalization-ack", job_id, fields))
        if self.error:
            raise self.error
        return self.accepted


class AnalyticsDelivery:
    def __init__(self, calls, payload=None, error=None):
        self.calls, self.payload, self.error = calls, payload, error

    def payload_bytes(self, payload_id):
        self.calls.append(("analytics-payload", payload_id))
        if self.error:
            raise self.error
        return self.payload

    def acknowledge(self, job_id, payload):
        self.calls.append(("analytics-ack", job_id, payload))
        if self.error:
            raise self.error


def _normalization_ack():
    return NormalizationAcknowledgement(
        "session",
        "payload",
        "a" * 64,
        "b" * 64,
        "attempt",
        0,
        "PERSISTED",
        datetime(2026, 10, 28, tzinfo=UTC),
        None,
        None,
    )


@pytest.mark.parametrize("content", [b"", "한글\n".encode()])
def test_normalization_wins_without_probing_analytics_or_copying_bytes(content):
    calls = []
    result = get_terminal_payload(
        "payload", NormalizationDelivery(calls, content), AnalyticsDelivery(calls, b"other")
    )
    assert result.content is content
    assert result.media_type == "application/vnd.refinvest.trading-normalization+json"
    assert calls == [("normalization-payload", "payload")]


@pytest.mark.parametrize("content", [b"", "한글\n".encode()])
def test_analytics_is_queried_only_after_normalization_returns_none(content):
    calls = []
    result = get_terminal_payload(
        "payload", NormalizationDelivery(calls), AnalyticsDelivery(calls, content)
    )
    assert result.content is content
    assert result.media_type == "application/vnd.refinvest.trading-analytics+json"
    assert calls == [("normalization-payload", "payload"), ("analytics-payload", "payload")]


@pytest.mark.parametrize(
    "code", ["RESULT_TRANSPORT_HASH_MISMATCH", "RESULT_COUNT_MISMATCH", "RESULT_HASH_MISMATCH"]
)
def test_integrity_failure_never_falls_back_to_another_payload(code):
    calls = []
    failure = ResultIntegrityError(code)
    with pytest.raises(TerminalDeliveryFailure) as caught:
        get_terminal_payload(
            "payload",
            NormalizationDelivery(calls, error=failure),
            AnalyticsDelivery(calls, b"valid-other"),
        )
    assert caught.value.kind is DeliveryFailureKind.CONFLICT
    assert caught.value.code == code and caught.value.__cause__ is failure
    assert calls == [("normalization-payload", "payload")]


@pytest.mark.parametrize("configured", [False, True])
def test_optional_or_empty_analytics_store_keeps_missing_resource_outcome(configured):
    calls = []
    analytics = AnalyticsDelivery(calls) if configured else None
    with pytest.raises(TerminalDeliveryFailure) as caught:
        get_terminal_payload("payload", NormalizationDelivery(calls), analytics)
    assert caught.value.kind is DeliveryFailureKind.NOT_FOUND
    assert caught.value.code == "PAYLOAD_NOT_FOUND" and not caught.value.retryable
    assert len(calls) == (2 if configured else 1)


def test_analytics_unavailability_during_payload_read_is_expired_not_dependency_retry():
    calls = []
    with pytest.raises(TerminalDeliveryFailure) as caught:
        get_terminal_payload(
            "payload",
            NormalizationDelivery(calls),
            AnalyticsDelivery(calls, error=AnalyticsJobUnavailable("any")),
        )
    assert caught.value.kind is DeliveryFailureKind.NOT_FOUND
    assert caught.value.code == "RESULT_PAYLOAD_EXPIRED" and not caught.value.retryable


@pytest.mark.parametrize("stage", ["normalization", "analytics"])
def test_unclassified_read_errors_are_not_swallowed_or_used_as_fallback(stage):
    calls = []
    failure = RuntimeError("dependency")
    norm = NormalizationDelivery(calls, error=failure if stage == "normalization" else None)
    ana = AnalyticsDelivery(calls, error=failure if stage == "analytics" else None)
    with pytest.raises(RuntimeError) as caught:
        get_terminal_payload("payload", norm, ana)
    assert caught.value is failure
    assert len(calls) == (1 if stage == "normalization" else 2)


@pytest.mark.parametrize("disposition", ["PERSISTED", "DISCARDED_STALE"])
def test_normalization_ack_preserves_token_generation_timestamp_and_key_hash(disposition):
    command = _normalization_ack()
    if disposition == "DISCARDED_STALE":
        command = replace(
            command,
            disposition=disposition,
            persisted_at=None,
            discarded_at=command.persisted_at,
            current_deletion_generation=1,
        )
    calls = []
    acknowledge_terminal_result(
        "job", command, "raw-key", NormalizationDelivery(calls), AnalyticsDelivery(calls)
    )
    assert len(calls) == 1 and calls[0][:2] == ("normalization-ack", "job")
    fields = calls[0][2]
    assert fields["idempotency_key_hash"] == hash_idempotency_key("raw-key")
    assert fields["recorded_at"] is (
        command.persisted_at if disposition == "PERSISTED" else command.discarded_at
    )
    assert fields["attempt_token"] == command.attempt_token
    assert fields["deletion_generation"] == command.deletion_generation
    assert fields["current_deletion_generation"] == command.current_deletion_generation
    assert fields["payload_sha256"] == command.payload_sha256
    assert fields["terminal_result_hash"] == command.terminal_result_hash


@pytest.mark.parametrize(
    "changes",
    [
        {"persisted_at": None},
        {"discarded_at": datetime(2026, 10, 28, tzinfo=UTC)},
        {"current_deletion_generation": 1},
        {
            "disposition": "DISCARDED_STALE",
            "persisted_at": None,
            "discarded_at": datetime(2026, 10, 28, tzinfo=UTC),
        },
        {
            "disposition": "DISCARDED_STALE",
            "discarded_at": datetime(2026, 10, 28, tzinfo=UTC),
            "current_deletion_generation": 1,
        },
    ],
)
def test_bad_normalization_ack_shape_never_contacts_either_store(changes):
    calls = []
    with pytest.raises(TerminalDeliveryFailure) as caught:
        acknowledge_terminal_result(
            "job",
            replace(_normalization_ack(), **changes),
            "key",
            NormalizationDelivery(calls),
            AnalyticsDelivery(calls),
        )
    assert caught.value.kind is DeliveryFailureKind.INVALID
    assert caught.value.code == "REQUEST_INVALID" and calls == []


@pytest.mark.parametrize(
    "failure", [TradingReviewIdempotencyConflict(), TradingReviewJobStateConflict()]
)
def test_normalization_ack_conflicts_keep_result_error_and_do_not_try_analytics(failure):
    calls = []
    with pytest.raises(TerminalDeliveryFailure) as caught:
        acknowledge_terminal_result(
            "job",
            _normalization_ack(),
            "key",
            NormalizationDelivery(calls, error=failure),
            AnalyticsDelivery(calls),
        )
    assert caught.value.kind is DeliveryFailureKind.CONFLICT
    assert caught.value.code == "RESULT_HASH_MISMATCH"
    assert len(calls) == 1 and calls[0][0] == "normalization-ack"


def test_normalization_ack_false_is_not_found_not_a_dispatch_hint():
    calls = []
    with pytest.raises(TerminalDeliveryFailure) as caught:
        acknowledge_terminal_result(
            "job",
            _normalization_ack(),
            "key",
            NormalizationDelivery(calls, accepted=False),
            AnalyticsDelivery(calls),
        )
    assert caught.value.code == "JOB_NOT_FOUND" and len(calls) == 1


def test_analytics_ack_forwards_exact_projection_and_does_not_add_header_idempotency():
    calls = []
    body = {"coreAnalysisRunId": "run", "disposition": "PERSISTED", "attemptToken": "attempt"}
    command = AnalyticsAcknowledgement(body)
    norm, ana = NormalizationDelivery(calls), AnalyticsDelivery(calls)
    acknowledge_terminal_result("job", command, "first-key", norm, ana)
    acknowledge_terminal_result("job", command, "different-key", norm, ana)
    assert calls == [("analytics-ack", "job", body)] * 2
    assert calls[0][2] is body


@pytest.mark.parametrize(
    "code,expected",
    [
        ("RESULT_PAYLOAD_EXPIRED", "RESULT_PAYLOAD_EXPIRED"),
        ("JOB_NOT_FOUND", "JOB_NOT_FOUND"),
        ("dependency", "JOB_NOT_FOUND"),
    ],
)
def test_analytics_ack_unavailable_retains_its_existing_reason_mapping(code, expected):
    calls = []
    with pytest.raises(TerminalDeliveryFailure) as caught:
        acknowledge_terminal_result(
            "job",
            AnalyticsAcknowledgement({}),
            "key",
            NormalizationDelivery(calls),
            AnalyticsDelivery(calls, error=AnalyticsJobUnavailable(code)),
        )
    assert caught.value.kind is DeliveryFailureKind.NOT_FOUND and caught.value.code == expected
    assert len(calls) == 1 and calls[0][0] == "analytics-ack"


def test_analytics_ack_conflict_and_unconfigured_store_stay_distinct():
    calls = []
    command = AnalyticsAcknowledgement({})
    with pytest.raises(TerminalDeliveryFailure) as caught:
        acknowledge_terminal_result("job", command, "key", NormalizationDelivery(calls), None)
    assert caught.value.kind is DeliveryFailureKind.UNAVAILABLE
    assert caught.value.code == "TEMPORARY_DEPENDENCY_FAILURE" and caught.value.retryable
    assert calls == []
    with pytest.raises(TerminalDeliveryFailure) as caught:
        acknowledge_terminal_result(
            "job",
            command,
            "key",
            NormalizationDelivery(calls),
            AnalyticsDelivery(calls, error=AnalyticsJobConflict("ACK_CONFLICT")),
        )
    assert caught.value.kind is DeliveryFailureKind.CONFLICT
    assert caught.value.code == "RESULT_HASH_MISMATCH" and not caught.value.retryable
