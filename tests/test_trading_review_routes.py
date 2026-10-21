"""Contract-facing normalization route checks using synthetic descriptors."""

import copy
import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from starlette.testclient import TestClient

from refinvest.api.routes.trading_review_jobs import get_trading_review_repository
from refinvest.app.config import Settings, get_settings
from refinvest.app.main import app
from refinvest.application.trading_review_jobs import (
    GrantUpdateReceipt,
    SafeTradingReviewFailure,
    TerminalPayloadDescriptor,
    TradingReviewCapacityExceeded,
    TradingReviewIdempotencyConflict,
    TradingReviewJob,
    TradingReviewJobKind,
    TradingReviewJobStatus,
    TradingReviewJobStatusView,
    TradingReviewJobSubmission,
)
from tests.trading_review_support import (
    input_set_hash_for,
    normalization_identity_for,
    simple_long_request,
)

OPENAPI = yaml.safe_load(
    (Path(__file__).parents[1] / "openapi/compute-api.yaml").read_text(encoding="utf-8")
)


def _validate(name: str, document: dict[str, object]) -> None:
    def openapi_nullable(node: object) -> object:
        if isinstance(node, list):
            return [openapi_nullable(item) for item in node]
        if isinstance(node, dict):
            result = {key: openapi_nullable(value) for key, value in node.items()}
            if result.get("nullable") is True and isinstance(result.get("type"), str):
                result["type"] = [result["type"], "null"]
            return result
        return node

    Draft202012Validator(
        openapi_nullable(
            {"$ref": f"#/components/schemas/{name}", "components": OPENAPI["components"]}
        ),
        format_checker=FormatChecker(),
    ).validate(document)


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
            core_normalization_run_id=job.core_normalization_run_id,
            kind=TradingReviewJobKind.NORMALIZATION_RECONCILIATION,
            status=TradingReviewJobStatus.PENDING,
            progress_stage="QUEUED",
            attempt=0,
            attempt_token=job.attempt_token,
            deletion_generation=job.deletion_generation,
            created_at=now,
            updated_at=now,
            expected_input_set_hash=job.expected_input_set_hash,
            computed_input_set_hash=job.computed_input_set_hash,
            hash_specification_version=job.hash_specification_version,
            target_version_set=job.request_payload["inputIdentity"]["targetVersionSet"],
            retry_policy_version=job.retry_policy_version,
            version_set_hash=job.version_set_hash,
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

    def update_grants(
        self, job_id: str, _key: str, payload: dict[str, object]
    ) -> GrantUpdateReceipt | None:
        view = self.get(job_id)
        if view is None:
            return None
        now = datetime.now(UTC)
        self.view = replace(
            view,
            grant_revision=view.grant_revision + 1,
            attempt_token=str(payload["nextAttemptToken"]),
            grant_action="NONE",
            grant_refresh_deadline_at=None,
        )
        return GrantUpdateReceipt(
            job_id,
            str(payload["mode"]),
            self.view.grant_revision,
            self.view.attempt_token,
            self.view.status.value,
            now,
        )


def _body() -> dict[str, object]:
    request = simple_long_request()
    identity = normalization_identity_for(request)
    return {
        "coreLogicalJobId": "logical-1",
        "coreNormalizationRunId": "run-1",
        "coreImportSessionId": "session-1",
        "inputIdentity": identity,
        "expectedInputSetHash": input_set_hash_for(identity),
        "resultDeliveryPolicy": {
            "policyVersion": "RESULT_DELIVERY_V1",
            "mode": "SINGLE_RESOURCE",
            "maxAcceptedPayloadBytes": 52_428_800,
            "acceptedContentEncodings": ["identity"],
        },
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
            for artifact in request.artifacts
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
            for term in request.instruments
        ],
        "retryPolicyVersion": "POLICY_V1",
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
        _validate("CreateTradingNormalizationJobRequest", body)
        job_id = accepted.json()["jobId"]
        assert client.post(base, json=body, headers=headers).json()["jobId"] == job_id
        invalid_policy = copy.deepcopy(body)
        invalid_policy["resultDeliveryPolicy"]["policyVersion"] = "RESULT_DELIVERY_V2"
        rejected = client.post(
            base, json=invalid_policy, headers={**headers, "Idempotency-Key": "invalid-policy"}
        )
        assert rejected.status_code == 400
        assert rejected.json()["code"] == "REQUEST_INVALID"
        assert rejected.json()["retryable"] is False
        duplicate_json = json.dumps(body).replace(
            '"tradingBookId":', '"tradingBookId": "forged", "tradingBookId":', 1
        )
        duplicate_headers = {**headers, "Content-Type": "application/json"}
        assert (
            client.post(base, content=duplicate_json, headers=duplicate_headers).json()["code"]
            == "REQUEST_INVALID"
        )
        assert client.post(base, content=duplicate_json).status_code == 401
        changed = dict(body, expectedInputSetHash="b" * 64)
        assert client.post(base, json=changed, headers=headers).status_code == 409
        version_changed = copy.deepcopy(body)
        version_changed["inputIdentity"]["targetVersionSet"]["normalizer"]["normalizerVersion"] = (
            "latest"
        )
        assert client.post(base, json=version_changed, headers=headers).status_code == 422
        invalid_terms = dict(body)
        invalid_terms["pinnedInstrumentTerms"] = [
            dict(body["pinnedInstrumentTerms"][0], quantityStep="NaN")  # type: ignore[index]
        ]
        assert client.post(base, json=invalid_terms, headers=headers).status_code == 422
        expired = dict(body)
        expired["artifactAccessGrants"] = [
            dict(artifact, expiresAt="2020-01-01T00:00:00Z")
            for artifact in body["artifactAccessGrants"]  # type: ignore[union-attr]
        ]
        assert client.post(base, json=expired, headers=headers).json()["jobId"] == job_id
        pending = client.get(f"{base}/{job_id}", headers=headers).json()
        assert pending["status"] == "PENDING"
        _validate("TradingNormalizationJobStatusResponse", pending)
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
            "attemptToken": "attempt-1",
            "deletionGeneration": 0,
            "disposition": "PERSISTED",
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
        vector = json.loads(
            (
                Path(__file__).parent
                / "fixtures/trading_review/normalization_identity/v1/result-integrity-v1.json"
            ).read_text(encoding="utf-8")
        )["terminalPayload"]
        repository.descriptor = TerminalPayloadDescriptor(
            job_id=job_id,
            core_logical_job_id="logical-1",
            payload_id=payload_id,
            media_type="application/vnd.refinvest.trading-normalization+json",
            byte_size=len(payload),
            payload_sha256=payload_hash,
            core_normalization_run_id="run-1",
            input_set_hash=repository.job.computed_input_set_hash,
            hash_specification_version="TRADING_INPUT_SET_HASH_V1",
            target_version_set=repository.job.request_payload["inputIdentity"]["targetVersionSet"],
            reconciliation_manifest_hash="d" * 64,
            result_hash="b" * 64,
            created_at=now,
            expires_at=now + timedelta(hours=24),
            attempt_token="attempt-1",
            deletion_generation=0,
            result_schema_version="TRADING_NORMALIZATION_RESULT_V1",
            result_hash_specification_version="TRADING_NORMALIZATION_RESULT_HASH_V1",
            counts=vector["counts"],
            section_digests=vector["sectionDigests"],
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
        _validate("NormalizationTerminalResultDescriptor", result.json())
        assert result.json()["payloadSha256"] == payload_hash
        download = client.get(f"/trading-review/terminal-payloads/{payload_id}", headers=headers)
        assert download.status_code == 200 and download.content == payload
        assert hashlib.sha256(download.content).hexdigest() == result.json()["payloadSha256"]
        assert download.headers["content-type"] == (
            "application/vnd.refinvest.trading-normalization+json; charset=utf-8"
        )
        assert download.headers["content-encoding"] == "identity"
        assert not download.content.startswith(b"\xef\xbb\xbf")
        assert download.headers["content-length"] == str(len(payload))
        assert result.json()["byteSize"] == len(download.content)
        assert result.json()["resultDeliveryPolicyVersion"] == "RESULT_DELIVERY_V1"
        assert result.json()["contentEncoding"] == download.headers["content-encoding"]
        repeated = client.get(f"/trading-review/terminal-payloads/{payload_id}", headers=headers)
        assert repeated.content == download.content
        ack_body = {
            "coreAggregateId": "session-1",
            "payloadId": payload_id,
            "payloadSha256": payload_hash,
            "terminalResultHash": "b" * 64,
            "attemptToken": "attempt-1",
            "deletionGeneration": 0,
            "disposition": "PERSISTED",
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
        _validate("TradingNormalizationJobStatusResponse", failed.json())
        assert failed.json()["failure"]["code"] == "FILE_FORMAT_INVALID"
        assert failed.json()["failure"]["retryable"] is False
        assert failed.json().get("terminalPayloadId") is None
        assert failed.json().get("qualityReport") is None
        assert client.get(f"{base}/{job_id}/result", headers=headers).status_code == 409


def test_grant_update_route_auth_receipt_and_capacity_header() -> None:
    repository = MemoryJobs()
    with _client(repository) as client:
        headers = {"X-Internal-Api-Key": "test-internal-key", "Idempotency-Key": "grant-1"}
        body = _body()
        created = client.post("/trading-review/normalization-jobs", json=body, headers=headers)
        job_id = created.json()["jobId"]
        assert repository.view is not None
        repository.view = replace(
            repository.view,
            grant_action="REPLACE_QUEUED_GRANTS",
            grant_refresh_deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        update = {
            "mode": "REPLACE_QUEUED_GRANTS",
            "expectedGrantRevision": 0,
            "expectedAttemptToken": "attempt-1",
            "nextAttemptToken": "attempt-1",
            "expectedInputSetHash": body["expectedInputSetHash"],
            "versionSetHash": body["inputIdentity"]["targetVersionSet"]["versionSetHash"],  # type: ignore[index]
            "deletionGeneration": 0,
            "artifactAccessGrants": body["artifactAccessGrants"],
        }
        path = f"/trading-review/normalization-jobs/{job_id}/grant-updates"
        assert client.post(path, json=update).status_code == 401
        receipt = client.post(path, json=update, headers=headers)
        assert receipt.status_code == 200
        assert receipt.json()["grantRevision"] == 1
        assert (
            client.get(f"/trading-review/normalization-jobs/{job_id}", headers=headers).json()[
                "grantAction"
            ]
            == "NONE"
        )

    class CapacityJobs(MemoryJobs):
        def create_idempotently(
            self, job: TradingReviewJob, key_hash: str
        ) -> TradingReviewJobSubmission:
            raise TradingReviewCapacityExceeded("full")

    with _client(CapacityJobs()) as client:
        capacity = client.post(
            "/trading-review/normalization-jobs",
            json=_body(),
            headers={"X-Internal-Api-Key": "test-internal-key", "Idempotency-Key": "new"},
        )
        assert capacity.status_code == 503
        assert capacity.json()["code"] == "TRADING_REVIEW_CAPACITY_EXCEEDED"
        assert capacity.headers["Retry-After"] == "30"
