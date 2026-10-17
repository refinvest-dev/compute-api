"""Lease-bound worker decisions and artifact integrity using synthetic input."""

import hashlib
import importlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from refinvest.app.config import Settings
from refinvest.app.trading_review_normalization import (
    CompletedNormalization,
    FailedNormalization,
    _download_artifact,
    _SafeExecutionError,
    execute_normalization_child,
)
from refinvest.app.trading_review_worker import TradingReviewWorkerHost
from refinvest.application.trading_review_jobs import (
    ClaimedTradingReviewJob,
    SafeTradingReviewFailure,
)
from refinvest.trading_review import normalize_and_reconstruct
from tests.trading_review_support import (
    input_set_hash_for,
    normalization_identity_for,
    simple_long_request,
)


def _claim(attempt: int = 1) -> ClaimedTradingReviewJob:
    return ClaimedTradingReviewJob(
        job_id="job-1",
        core_logical_job_id="logical-1",
        core_import_session_id="session-1",
        core_normalization_run_id="run-1",
        request_payload={},
        expected_input_set_hash="a" * 64,
        computed_input_set_hash="a" * 64,
        hash_specification_version="TRADING_INPUT_SET_HASH_V1",
        retry_policy_version="POLICY_V1",
        version_set_hash="b" * 64,
        deletion_generation=0,
        attempt_token="attempt-token",
        attempt_number=attempt,
        lease_owner="lease-owner",
        created_at=datetime.now(UTC),
        reclaimed_stale_lease=False,
    )


class Jobs:
    def __init__(self, claim: ClaimedTradingReviewJob) -> None:
        self.claim = claim
        self.transition = ""

    def claim_next(self, _worker: str, _lease: timedelta) -> ClaimedTradingReviewJob | None:
        claim, self.claim = self.claim, None  # type: ignore[assignment]
        return claim

    def cancellation_requested(self, *_: object) -> bool:
        return False

    def heartbeat(self, *_: object) -> bool:
        return True

    def publish_completed(self, *_: object, **__: object) -> bool:
        self.transition = "COMPLETED"
        return True

    def mark_failed(self, *_: object) -> bool:
        self.transition = "FAILED"
        return True

    def mark_cancelled(self, *_: object) -> bool:
        self.transition = "CANCELLED"
        return True

    def schedule_retry(self, *_: object) -> bool:
        self.transition = "RETRY"
        return True


class Executor:
    def __init__(self, outcome: CompletedNormalization | FailedNormalization) -> None:
        self.outcome = outcome

    def execute(self, _claim: ClaimedTradingReviewJob, cancellation_check: object):  # type: ignore[no-untyped-def]
        return self.outcome


@pytest.mark.parametrize(
    ("outcome", "attempt", "transition"),
    [
        (
            CompletedNormalization(
                b"{}",
                "application/vnd.refinvest.trading-normalization+json",
                hashlib.sha256(b"{}").hexdigest(),
                "a" * 64,
                "d" * 64,
                "c" * 64,
            ),
            1,
            "COMPLETED",
        ),
        (
            FailedNormalization(
                SafeTradingReviewFailure(
                    "TEMPORARY_DEPENDENCY_FAILURE", "Retry.", True, "EXECUTION"
                ),
                True,
            ),
            1,
            "RETRY",
        ),
        (
            FailedNormalization(
                SafeTradingReviewFailure(
                    "TEMPORARY_DEPENDENCY_FAILURE", "Done.", True, "EXECUTION"
                ),
                True,
            ),
            3,
            "FAILED",
        ),
        (
            FailedNormalization(
                SafeTradingReviewFailure("JOB_CANCELLED", "Cancelled.", False, "CANCELLATION"),
                False,
            ),
            1,
            "CANCELLED",
        ),
    ],
)
def test_worker_commits_exactly_one_transition(
    outcome: CompletedNormalization | FailedNormalization, attempt: int, transition: str
) -> None:
    jobs = Jobs(_claim(attempt))
    worker = TradingReviewWorkerHost(
        jobs,  # type: ignore[arg-type]
        Executor(outcome),  # type: ignore[arg-type]
        worker_id="worker",
        lease_duration=timedelta(seconds=60),
        heartbeat_interval=timedelta(seconds=15),
        retention=timedelta(hours=24),
    )
    assert worker.run_once()
    assert jobs.transition == transition
    assert not worker.run_once()


def test_artifact_hash_mismatch_is_rejected_before_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status = 200
        headers = {"Content-Type": "text/csv"}

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self, _: int) -> bytes:
            return b"synthetic-artifact"

    class Opener:
        def open(self, *_: object, **__: object) -> Response:
            return Response()

    normalization = importlib.import_module("refinvest.app.trading_review_normalization")
    monkeypatch.setattr(normalization, "build_opener", lambda *_: Opener())
    grant: dict[str, object] = {
        "accessUri": "https://internal.example/artifact",
        "expiresAt": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        "byteSize": len(b"synthetic-artifact"),
        "contentType": "text/csv",
        "rawSha256": "0" * 64,
    }
    with pytest.raises(_SafeExecutionError) as raised:
        _download_artifact(grant, ("internal.example",))
    assert raised.value.failure.code == "SOURCE_CONTENT_HASH_MISMATCH"
    with pytest.raises(_SafeExecutionError) as wrong_type:
        _download_artifact(
            dict(grant, contentType="application/vnd.ms-excel"), ("internal.example",)
        )
    assert wrong_type.value.failure.code == "ARTIFACT_ACCESS_DENIED"
    with pytest.raises(_SafeExecutionError) as untrusted:
        _download_artifact(dict(grant, accessUri="file:///private/raw.csv"), ("internal.example",))
    assert untrusted.value.failure.code == "ARTIFACT_ACCESS_DENIED"


def test_child_reads_verified_artifacts_and_invokes_production_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine_request = replace(
        simple_long_request(), stable_identity_key=b"synthetic-stable-identity-secret-32-bytes"
    )
    expected = normalize_and_reconstruct(engine_request)
    content_by_role = {
        artifact.role.value: artifact.content for artifact in engine_request.artifacts
    }

    class Response:
        status = 200
        headers = {"Content-Type": "text/csv; charset=utf-8"}

        def __init__(self, content: bytes) -> None:
            self.content = content

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self, _: int) -> bytes:
            return self.content

    class Opener:
        def open(self, request: object, **_: object) -> Response:
            role = str(request.full_url).rsplit("/", 1)[-1]
            opened.append(role)
            return Response(content_by_role[role])

    normalization = importlib.import_module("refinvest.app.trading_review_normalization")
    opened: list[str] = []
    monkeypatch.setattr(normalization, "build_opener", lambda *_: Opener())
    identity = normalization_identity_for(engine_request)
    input_set_hash = input_set_hash_for(identity)
    request_payload: dict[str, object] = {
        "inputIdentity": identity,
        "artifactAccessGrants": [
            {
                "coreArtifactId": artifact.artifact_id,
                "role": artifact.role.value,
                "rawSha256": hashlib.sha256(artifact.content).hexdigest(),
                "byteSize": len(artifact.content),
                "contentType": "text/csv",
                "accessUri": f"https://internal.example/{artifact.role.value}",
                "expiresAt": (datetime.now(UTC) + timedelta(minutes=4)).isoformat(),
            }
            for artifact in engine_request.artifacts
        ],
        "pinnedInstrumentTerms": [
            {
                "instrumentId": term.instrument_id,
                "venueSymbol": term.venue_symbol,
                "settlementAsset": term.settlement_asset,
                "quantityStep": term.quantity_step.source_lexeme,
                "priceTick": term.price_tick.source_lexeme,
                "contractMultiplier": term.contract_multiplier.source_lexeme,
                "settlementPrecision": term.settlement_precision,
                "instrumentTermsVersion": term.instrument_terms_version,
            }
            for term in engine_request.instruments
        ],
    }
    claim = replace(
        _claim(),
        request_payload=request_payload,
        expected_input_set_hash=input_set_hash,
        computed_input_set_hash=input_set_hash,
        version_set_hash=identity["targetVersionSet"]["versionSetHash"],
    )
    settings = Settings(
        internal_api_key=None,
        trading_review_timezone_database_version=engine_request.timezone_database_version,
        trading_review_identity_key=engine_request.stable_identity_key.decode(),
        trading_review_masking_key="synthetic-masking-secret-at-least-32-bytes",
        trading_review_artifact_hosts=("internal.example",),
    )

    class Sender:
        outcome: CompletedNormalization | FailedNormalization | None = None

        def send(self, value: CompletedNormalization | FailedNormalization) -> None:
            self.outcome = value

        def close(self) -> None:
            return None

    sender = Sender()
    execute_normalization_child(settings, claim, sender)  # type: ignore[arg-type]
    outcome = sender.outcome
    assert isinstance(outcome, CompletedNormalization)
    assert outcome.result_hash == json.loads(outcome.payload)["resultHash"]
    payload = json.loads(outcome.payload)
    assert payload["reconciliationManifest"]["manifestHash"] == outcome.reconciliation_manifest_hash
    assert payload["coreNormalizationRunId"] == claim.core_normalization_run_id
    assert payload["inputSetHash"] == input_set_hash
    assert payload["reconciliationManifest"]["executionInputHash"] == expected.input_hash

    before = len(opened)
    mismatch = replace(claim, expected_input_set_hash="0" * 64)
    failed_sender = Sender()
    execute_normalization_child(settings, mismatch, failed_sender)  # type: ignore[arg-type]
    assert isinstance(failed_sender.outcome, FailedNormalization)
    assert failed_sender.outcome.failure.code == "INPUT_SET_HASH_MISMATCH"
    assert len(opened) == before

    unsupported_identity = {**identity, "hashSpecificationVersion": "FUTURE_SPEC"}
    unsupported = replace(
        claim,
        request_payload={**request_payload, "inputIdentity": unsupported_identity},
        computed_input_set_hash=None,
        hash_specification_version="FUTURE_SPEC",
    )
    failed_sender = Sender()
    execute_normalization_child(settings, unsupported, failed_sender)  # type: ignore[arg-type]
    assert isinstance(failed_sender.outcome, FailedNormalization)
    assert failed_sender.outcome.failure.code == "INPUT_HASH_SPECIFICATION_UNSUPPORTED"
    assert len(opened) == before

    from refinvest.app.trading_review_result_integrity import ResultIntegrityError

    def invalid_public_result(*_: object, **__: object) -> dict[str, object]:
        raise ResultIntegrityError("RESULT_HASH_MISMATCH")

    monkeypatch.setattr(normalization, "build_normalization_payload", invalid_public_result)
    failed_sender = Sender()
    execute_normalization_child(settings, claim, failed_sender)  # type: ignore[arg-type]
    assert isinstance(failed_sender.outcome, FailedNormalization)
    assert failed_sender.outcome.failure.code == "RESULT_HASH_MISMATCH"
    monkeypatch.setattr(normalization, "normalize_and_reconstruct", normalize_and_reconstruct)

    original = content_by_role["TRADE_HISTORY"]
    content_by_role["TRADE_HISTORY"] = original[::-1]
    failed_sender = Sender()
    execute_normalization_child(settings, claim, failed_sender)  # type: ignore[arg-type]
    assert isinstance(failed_sender.outcome, FailedNormalization)
    assert failed_sender.outcome.failure.code == "SOURCE_CONTENT_HASH_MISMATCH"
    content_by_role["TRADE_HISTORY"] = original
    invalid_bytes = b"Uid,Trade ID\r\nsensitive-user,sensitive-trade\r\n"
    content_by_role["TRADE_HISTORY"] = invalid_bytes
    failed_sender = Sender()
    execute_normalization_child(settings, claim, failed_sender)  # type: ignore[arg-type]
    failure = failed_sender.outcome
    assert isinstance(failure, FailedNormalization)
    assert failure.failure.code == "SOURCE_CONTENT_LENGTH_MISMATCH"
    assert "sensitive-user" not in str(failure)
    assert "sensitive-trade" not in str(failure)
