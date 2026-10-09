"""Contract-facing normalization route checks using synthetic descriptors."""

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from starlette.testclient import TestClient

from refinvest.api.routes.trading_review_jobs import get_trading_review_repository
from refinvest.app.config import Settings, get_settings
from refinvest.app.main import app
from refinvest.application.trading_review_jobs import (
    SafeTradingReviewFailure,
    TerminalPayloadDescriptor,
    TradingReviewIdempotencyConflict,
    TradingReviewJob,
    TradingReviewJobKind,
    TradingReviewJobStatus,
    TradingReviewJobStatusView,
    TradingReviewJobSubmission,
)
from refinvest.trading_review.versions import SUPPORTED_VERSION_SET
from tests.trading_review_support import simple_long_request


class MemoryJobs:
    def __init__(self) -> None:
        self.job: TradingReviewJob | None = None
        self.key_hash: str | None = None
        self.view: TradingReviewJobStatusView | None = None
        self.descriptor: TerminalPayloadDescriptor | None = None
        self.payload: bytes | None = None

    def create_idempotently(
        self, job: TradingReviewJob, key_hash: str
    ) -> TradingReviewJobSubmission:
        if self.job is not None:
            if self.key_hash != key_hash or self.job.request_fingerprint != job.request_fingerprint:
                raise TradingReviewIdempotencyConflict("conflict")
            return TradingReviewJobSubmission(
                self.job.job_id,
                self.job.core_logical_job_id,
                self.job.kind,
                self.view.created_at,
                False,  # type: ignore[union-attr]
            )
        self.job = job
        self.key_hash = key_hash
        now = datetime.now(UTC)
        self.view = TradingReviewJobStatusView(
            job_id=job.job_id,
            core_logical_job_id=job.core_logical_job_id,
            core_import_session_id=job.core_import_session_id,
            kind=TradingReviewJobKind.NORMALIZATION_RECONCILIATION,
            status=TradingReviewJobStatus.PENDING,
            progress_stage="QUEUED",
            attempt=0,
            attempt_token=job.attempt_token,
            deletion_generation=job.deletion_generation,
            created_at=now,
            updated_at=now,
        )
        return TradingReviewJobSubmission(job.job_id, job.core_logical_job_id, job.kind, now)

    def get(self, job_id: str) -> TradingReviewJobStatusView | None:
        return self.view if self.view is not None and self.view.job_id == job_id else None

    def request_cancellation(self, job_id: str, _key: str) -> TradingReviewJobStatusView | None:
        view = self.get(job_id)
        if view is None:
            return None
        self.view = replace(
            view, status=TradingReviewJobStatus.CANCELLED, terminal_at=datetime.now(UTC)
        )
        return self.view

    def get_result_descriptor(self, job_id: str) -> TerminalPayloadDescriptor | None:
        return (
            self.descriptor
            if self.descriptor is not None and self.descriptor.job_id == job_id
            else None
        )

    def get_terminal_payload(self, payload_id: str) -> bytes | None:
        return (
            self.payload
            if self.descriptor is not None and self.descriptor.payload_id == payload_id
            else None
        )

    def acknowledge(self, job_id: str, **_: object) -> bool:
        return self.descriptor is not None and self.descriptor.job_id == job_id


def _body() -> dict[str, object]:
    request = simple_long_request()
    version = SUPPORTED_VERSION_SET
    return {
        "coreLogicalJobId": "logical-1",
        "coreImportSessionId": "session-1",
        "bookProduct": {
            "venue": "BINANCE",
            "productFamily": "LINEAR_PERPETUAL",
            "positionMode": "ONE_WAY",
            "settlementAsset": "USDT",
        },
        "artifacts": [
            {
                "coreArtifactId": artifact.artifact_id,
                "role": artifact.role.value,
                "rawSha256": hashlib.sha256(artifact.content).hexdigest(),
                "byteSize": len(artifact.content),
                "contentType": "text/csv",
                "accessUri": f"https://internal.example/{artifact.role.value}",
                "expiresAt": (datetime.now(UTC) + timedelta(minutes=4)).isoformat(),
            }
            for artifact in request.artifacts
        ],
        "sourceTimezone": request.source_timezone,
        "reviewTimezone": "UTC",
        "targetVersionSet": {
            "sourceDialectVersionsByArtifactRole": {
                role.value: value
                for role, value in version.source_dialect_versions_by_artifact_role
            },
            "adapterVersion": version.adapter_version,
            "normalizerVersion": version.normalizer_version,
            "canonicalSchemaVersion": version.canonical_schema_version,
            "reconstructionPolicyVersion": version.reconstruction_policy_version,
            "reconciliationVersion": version.reconciliation_version,
            "reviewUnitSchemaVersion": version.review_unit_schema_version,
            "evidenceSchemaVersion": version.evidence_schema_version,
            "implementationDigest": version.implementation_digest,
            "versionSetHash": version.version_set_hash,
        },
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
            for term in request.instruments
        ],
        "expectedInputHash": "a" * 64,
        "deletionGeneration": 0,
        "attemptToken": "attempt-1",
    }


@contextmanager
def _client(repository: MemoryJobs) -> Iterator[TestClient]:
    app.dependency_overrides[get_settings] = lambda: Settings("test-internal-key")
    app.dependency_overrides[get_trading_review_repository] = lambda: repository
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def test_normalization_routes_auth_admission_status_result_cancel_and_ack() -> None:
    repository = MemoryJobs()
    with _client(repository) as client:
        base = "/trading-review/normalization-jobs"
        body = _body()
        assert client.post(base, json=body).status_code == 401
        assert (
            client.post(base, json=body, headers={"X-Internal-Api-Key": "wrong"}).status_code == 401
        )
        assert client.get(f"{base}/nonexistent").status_code == 401
        assert client.get("/trading-review/terminal-payloads/nonexistent").status_code == 401
        headers = {"X-Internal-Api-Key": "test-internal-key", "Idempotency-Key": "dispatch-1"}
        accepted = client.post(base, json=body, headers=headers)
        assert accepted.status_code == 202
        job_id = accepted.json()["jobId"]
        assert client.post(base, json=body, headers=headers).json()["jobId"] == job_id
        changed = dict(body, expectedInputHash="b" * 64)
        assert client.post(base, json=changed, headers=headers).status_code == 409
        version_changed = dict(body)
        version_changed["targetVersionSet"] = dict(
            body["targetVersionSet"], normalizerVersion="latest"
        )  # type: ignore[arg-type]
        assert client.post(base, json=version_changed, headers=headers).status_code == 422
        invalid_terms = dict(body)
        invalid_terms["pinnedInstrumentTerms"] = [
            dict(body["pinnedInstrumentTerms"][0], quantityStep="NaN")  # type: ignore[index]
        ]
        assert client.post(base, json=invalid_terms, headers=headers).status_code == 422
        expired = dict(body)
        expired["artifacts"] = [
            dict(artifact, expiresAt="2020-01-01T00:00:00Z")
            for artifact in body["artifacts"]  # type: ignore[union-attr]
        ]
        assert client.post(base, json=expired, headers=headers).status_code == 422
        assert client.get(f"{base}/{job_id}", headers=headers).json()["status"] == "PENDING"
        assert client.get(f"{base}/{job_id}/result", headers=headers).status_code == 409
        assert (
            client.get("/trading-review/terminal-payloads/missing", headers=headers).status_code
            == 404
        )
        cancel = client.post(f"{base}/{job_id}/cancellation", headers=headers)
        assert cancel.status_code == 202
        assert cancel.json()["status"] == "CANCELLED"
        ack_body = {
            "coreAggregateId": "session-1",
            "payloadId": str(uuid4()),
            "payloadSha256": "a" * 64,
            "terminalResultHash": "b" * 64,
            "persistedAt": "2026-01-01T00:00:00Z",
        }
        assert (
            client.post(
                f"/trading-review/jobs/{job_id}/acknowledgement", json=ack_body, headers=headers
            ).status_code
            == 404
        )


def test_completed_descriptor_download_ack_and_failed_status() -> None:
    repository = MemoryJobs()
    with _client(repository) as client:
        base = "/trading-review/normalization-jobs"
        headers = {"X-Internal-Api-Key": "test-internal-key", "Idempotency-Key": "dispatch-2"}
        accepted = client.post(base, json=_body(), headers=headers)
        assert accepted.status_code == 202
        job_id = accepted.json()["jobId"]
        now = datetime.now(UTC)
        payload = b'{"status":"COMPLETED"}'
        payload_hash = hashlib.sha256(payload).hexdigest()
        payload_id = str(uuid4())
        repository.descriptor = TerminalPayloadDescriptor(
            job_id=job_id,
            core_logical_job_id="logical-1",
            payload_id=payload_id,
            media_type="application/vnd.refinvest.trading-normalization+json",
            byte_size=len(payload),
            payload_sha256=payload_hash,
            input_hash="a" * 64,
            version_set_hash=SUPPORTED_VERSION_SET.version_set_hash,
            result_hash="b" * 64,
            created_at=now,
            expires_at=now + timedelta(hours=24),
        )
        repository.payload = payload
        assert repository.view is not None
        repository.view = replace(
            repository.view,
            status=TradingReviewJobStatus.COMPLETED,
            terminal_payload_id=payload_id,
            result_hash="b" * 64,
            terminal_at=now,
        )
        result = client.get(f"{base}/{job_id}/result", headers=headers)
        assert result.status_code == 200
        assert result.json()["payloadSha256"] == payload_hash
        download = client.get(f"/trading-review/terminal-payloads/{payload_id}", headers=headers)
        assert download.status_code == 200 and download.content == payload
        ack_body = {
            "coreAggregateId": "session-1",
            "payloadId": payload_id,
            "payloadSha256": payload_hash,
            "terminalResultHash": "b" * 64,
            "persistedAt": now.isoformat(),
        }
        ack = client.post(
            f"/trading-review/jobs/{job_id}/acknowledgement", json=ack_body, headers=headers
        )
        assert ack.status_code == 204
        repository.view = replace(
            repository.view,
            status=TradingReviewJobStatus.FAILED,
            terminal_payload_id=None,
            result_hash=None,
            failure=SafeTradingReviewFailure(
                "FILE_FORMAT_INVALID", "Invalid input.", False, "PARSING"
            ),
        )
        failed = client.get(f"{base}/{job_id}", headers=headers)
        assert failed.status_code == 200
        assert failed.json()["failure"]["code"] == "FILE_FORMAT_INVALID"
        assert client.get(f"{base}/{job_id}/result", headers=headers).status_code == 409
