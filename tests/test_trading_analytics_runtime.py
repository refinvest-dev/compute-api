"""Analytics V1 fixed-vector input, pure Engine invocation, and terminal delivery."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker
from starlette.testclient import TestClient

from refinvest.adapters.analytics.artifacts import download_inputs
from refinvest.api.dependencies.trading_analytics_jobs import (
    get_analytics_repository,
    get_optional_analytics_repository,
)
from refinvest.api.dependencies.trading_review_jobs import get_trading_review_repository
from refinvest.app.config import Settings, get_settings
from refinvest.app.main import app
from refinvest.application.analytics_input import AnalyticsInputFailure, adapt_inputs
from refinvest.application.analytics_jobs import AnalyticsJobConflict
from refinvest.application.analytics_result import bounded_analytics_json, build_sections
from refinvest.application.analytics_worker import (
    AnalyticsWorkerSettings,
    TradingAnalyticsWorkerHost,
)
from refinvest.contracts.analytics.identity import (
    AnalyticsContractError,
    analysis_input_hash,
    canonical_bytes,
    create_fingerprint,
)
from refinvest.contracts.analytics.result_integrity import finish_sections
from refinvest.trading_review.analytics import calculate_behavior_metrics

FIXTURE = (
    Path(__file__).parent / "fixtures/trading_review/analytics_protocol/v1/input-identity-v1.json"
)
OPENAPI = yaml.safe_load((Path(__file__).parents[1] / "openapi/compute-api.yaml").read_text())


def _vector() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _request() -> dict[str, Any]:
    vector = _vector()
    identity = copy.deepcopy(vector["inputIdentity"])
    now = datetime.now(UTC)

    def reference(role: str) -> dict[str, Any]:
        return {
            **{
                key: identity[role][key]
                for key in ("coreReferenceId", "mediaType", "byteSize", "sha256")
            },
            "accessUri": f"https://core-artifacts.internal/{role}",
            "expiresAt": (now + timedelta(minutes=4)).isoformat().replace("+00:00", "Z"),
        }

    return {
        "coreLogicalJobId": f"logical-{uuid4()}",
        "coreAnalysisRunId": f"run-{uuid4()}",
        "ledgerRevisionId": identity["ledgerRevisionId"],
        "ledgerRevisionContent": reference("ledgerRevisionContent"),
        "reconciliationManifest": reference("reconciliationManifest"),
        "inputIdentity": identity,
        "targetVersionSet": identity["targetVersionSet"],
        "analysisConfig": vector["analysisConfig"],
        "analysisConfigHash": vector["expectedAnalysisConfigHash"],
        "capabilitySnapshot": identity["capabilitySnapshot"],
        "expectedInputHash": vector["expectedInputHash"],
        "inputHashSpecificationVersion": "TRADING_ANALYTICS_INPUT_HASH_V1",
        "resultSchemaVersion": "TRADING_ANALYTICS_RESULT_SCHEMA_V1",
        "resultHashSpecificationVersion": "TRADING_ANALYTICS_RESULT_HASH_V1",
        "retryPolicyVersion": "ANALYTICS_RETRY_V1",
        "resultDeliveryPolicy": {
            "policyVersion": "ANALYTICS_RESULT_DELIVERY_V1",
            "transportMode": "SINGLE_RESOURCE",
            "maxPayloadBytes": 1_000_000,
        },
        "deletionGeneration": 0,
        "attemptToken": str(uuid4()),
    }


def _validate(name: str, document: dict[str, Any]) -> None:
    def nullable(node: Any) -> Any:
        if isinstance(node, list):
            return [nullable(item) for item in node]
        if isinstance(node, dict):
            result = {key: nullable(value) for key, value in node.items()}
            if result.get("nullable") is True:
                if "allOf" in result:
                    result = {
                        **{
                            key: value
                            for key, value in result.items()
                            if key not in {"allOf", "nullable", "type"}
                        },
                        "anyOf": [{"allOf": result["allOf"]}, {"type": "null"}],
                    }
                elif isinstance(result.get("type"), str):
                    result["type"] = [result["type"], "null"]
            return result
        return node

    validator = Draft202012Validator(
        nullable({"$ref": f"#/components/schemas/{name}", "components": OPENAPI["components"]}),
        format_checker=FormatChecker(),
    )
    errors = list(validator.iter_errors(document))
    if errors:
        nested = errors[0].context
        raise AssertionError(
            [(list(error.path), error.validator, error.message[:100]) for error in nested[:8]]
            or [(list(errors[0].path), errors[0].validator, errors[0].message[:100])]
        )


def test_fixed_vector_runs_all_metrics_and_projects_contract() -> None:
    vector = _vector()
    request = _request()
    assert analysis_input_hash(request["inputIdentity"]) == request["expectedInputHash"]
    ledger = vector["artifactSamples"]["ledgerRevisionContent"]
    manifest = vector["artifactSamples"]["reconciliationManifest"]
    episodes, records, instruments, config = adapt_inputs(request, ledger, manifest)
    first = calculate_behavior_metrics(
        episodes=episodes,
        canonical_records=records,
        instruments=instruments,
        analysis_config=config,
    )
    second = calculate_behavior_metrics(
        episodes=episodes,
        canonical_records=records,
        instruments=instruments,
        analysis_config=config,
    )
    assert first == second
    sections = build_sections(first, ledger, manifest)
    repeated = build_sections(second, ledger, manifest)
    digests = finish_sections(
        sections,
        input_hash=request["expectedInputHash"],
        config_hash=request["analysisConfigHash"],
        version_set=request["targetVersionSet"],
    )
    assert digests == finish_sections(
        repeated,
        input_hash=request["expectedInputHash"],
        config_hash=request["analysisConfigHash"],
        version_set=request["targetVersionSet"],
    )
    assert digests["counts"]["metricCount"] == 3
    assert digests["counts"]["findingCount"] == 0
    assert len({item["definitionId"] for item in sections["METRICS"]}) == 3
    assert all(item["status"] == "INSUFFICIENT_SAMPLE" for item in sections["METRICS"])


def test_input_transport_checks_exact_bytes_before_parsing(monkeypatch: Any) -> None:
    vector = _vector()
    request = _request()
    payloads = {
        role: canonical_bytes(vector["artifactSamples"][role])
        for role in ("ledgerRevisionContent", "reconciliationManifest")
    }
    for role, body in payloads.items():
        assert len(body) == request[role]["byteSize"]
        assert hashlib.sha256(body).hexdigest() == request[role]["sha256"]
    grants = {
        role: {key: request[role][key] for key in ("accessUri", "expiresAt")} for role in payloads
    }

    class Response(BytesIO):
        status = 200

        def __init__(self, body: bytes, media_type: str) -> None:
            super().__init__(body)
            self.headers = {"Content-Type": media_type, "Content-Encoding": "identity"}

    class Opener:
        def open(self, requested: Any, timeout: int) -> Response:
            role = requested.full_url.rsplit("/", 1)[1]
            return Response(payloads[role], request[role]["mediaType"])

    input_module = importlib.import_module("refinvest.adapters.analytics.artifacts")
    monkeypatch.setattr(input_module, "build_opener", lambda *_: Opener())
    ledger, manifest = download_inputs(
        request, grants, ("core-artifacts.internal",), lambda: False, 1_000_000
    )
    assert ledger == vector["artifactSamples"]["ledgerRevisionContent"]
    assert manifest == vector["artifactSamples"]["reconciliationManifest"]
    payloads["ledgerRevisionContent"] = payloads["ledgerRevisionContent"][:-1] + b" "
    with pytest.raises(AnalyticsInputFailure, match="INPUT_CONTENT_HASH_MISMATCH"):
        download_inputs(request, grants, ("core-artifacts.internal",), lambda: False, 1_000_000)


def test_result_digest_matches_context_fixed_vector_and_payload_cap() -> None:
    path = FIXTURE.with_name("result-integrity-v1.json")
    vector = json.loads(path.read_text(encoding="utf-8"))
    manifest = vector["resultDigestManifest"]
    computed = finish_sections(
        copy.deepcopy(vector["sections"]),
        input_hash=manifest["inputHash"],
        config_hash=manifest["analysisConfigHash"],
        version_set=manifest["targetVersionSet"],
    )
    assert computed["counts"] == vector["counts"]
    assert computed["sectionDigests"] == vector["sectionDigests"]
    assert computed["evidenceHash"] == vector["evidenceHash"]
    assert computed["resultHash"] == vector["expectedResultHash"]
    with pytest.raises(AnalyticsContractError, match="RESULT_PAYLOAD_TOO_LARGE"):
        bounded_analytics_json({"payload": "x" * 1000}, 100)


class MemoryAnalyticsRepository:
    max_payload_bytes = 1_000_000

    def __init__(self, request: dict[str, Any]) -> None:
        self.request = request
        self.job_id = str(uuid4())
        self.claimed = False
        self.created = False
        self.create_key: str | None = None
        self.create_fingerprint: str | None = None
        self.failure: tuple[str, bool] | None = None
        self.payload: dict[str, Any] | None = None
        self.body: bytes | None = None
        self.descriptor: dict[str, Any] | None = None
        self.acked = False

    def create(
        self, request: dict[str, Any], key: str, fingerprint: str
    ) -> tuple[dict[str, Any], bool]:
        assert fingerprint == create_fingerprint(request)
        if self.created:
            if self.create_key != key or self.create_fingerprint != fingerprint:
                raise AnalyticsJobConflict("IDEMPOTENCY_CONFLICT")
            return self.get(self.job_id) or {}, False
        self.created = True
        self.create_key = key
        self.create_fingerprint = fingerprint
        return self.get(self.job_id) or {}, True

    def get(self, job_id: str) -> dict[str, Any] | None:
        if job_id != self.job_id:
            return None
        return {
            "jobId": job_id,
            "coreLogicalJobId": self.request["coreLogicalJobId"],
            "coreAnalysisRunId": self.request["coreAnalysisRunId"],
            "kind": "ANALYTICS",
            "status": "COMPLETED" if self.body else "PENDING",
            "progressStage": "COMPLETED" if self.body else "QUEUED",
            "attempt": 1,
            "attemptToken": self.request["attemptToken"],
            "deletionGeneration": self.request["deletionGeneration"],
            "grantRevision": 0,
            "expectedInputHash": self.request["expectedInputHash"],
            "inputHashSpecificationVersion": self.request["inputHashSpecificationVersion"],
            "analysisConfigHash": self.request["analysisConfigHash"],
            "targetVersionSet": self.request["targetVersionSet"],
            "retryPolicyVersion": self.request["retryPolicyVersion"],
            "resultDeliveryPolicy": self.request["resultDeliveryPolicy"],
            "retryCutoffAt": (datetime.now(UTC) + timedelta(hours=24)).isoformat(),
            "retryAvailable": False,
            "createdAt": datetime.now(UTC).isoformat(),
            "updatedAt": datetime.now(UTC).isoformat(),
            "startedAt": None,
        }

    def claim_next(self, worker_id: str, lease: timedelta) -> dict[str, Any] | None:
        if self.claimed:
            return None
        self.claimed = True
        return {
            "jobId": self.job_id,
            "request": self.request,
            "grants": {},
            "attempt": 1,
            "attemptToken": self.request["attemptToken"],
            "deletionGeneration": self.request["deletionGeneration"],
            "leaseGeneration": 1,
            "leaseOwner": worker_id,
            "createdAt": datetime.now(UTC),
        }

    def heartbeat(self, claim: dict[str, Any], lease: timedelta) -> bool:
        return True

    def fail_attempt(self, claim: dict[str, Any], code: str, *, retryable: bool) -> bool:
        self.failure = code, retryable
        return True

    def publish_completed(
        self, claim: dict[str, Any], payload: dict[str, Any], body: bytes
    ) -> bool:
        self.payload = payload
        self.body = body
        self.descriptor = {
            "jobId": self.job_id,
            "coreLogicalJobId": self.request["coreLogicalJobId"],
            "coreAnalysisRunId": self.request["coreAnalysisRunId"],
            "payloadId": str(uuid4()),
            "transportMode": "SINGLE_RESOURCE",
            "resultDeliveryPolicyVersion": "ANALYTICS_RESULT_DELIVERY_V1",
            "mediaType": "application/vnd.refinvest.trading-analytics+json",
            "contentEncoding": "identity",
            "byteSize": len(body),
            "payloadSha256": hashlib.sha256(body).hexdigest(),
            "attemptToken": claim["attemptToken"],
            "deletionGeneration": claim["deletionGeneration"],
            "inputHash": payload["inputHash"],
            "inputHashSpecificationVersion": payload["inputHashSpecificationVersion"],
            "analysisConfigHash": payload["analysisConfigHash"],
            "targetVersionSet": payload["targetVersionSet"],
            "resultSchemaVersion": payload["resultSchemaVersion"],
            "resultHashSpecificationVersion": payload["resultHashSpecificationVersion"],
            "counts": payload["counts"],
            "sectionDigests": payload["sectionDigests"],
            "evidenceHash": payload["evidenceHash"],
            "resultHash": payload["resultHash"],
            "createdAt": payload["createdAt"],
            "expiresAt": (datetime.now(UTC) + timedelta(hours=24)).isoformat(),
        }
        return True

    def result_descriptor(self, job_id: str) -> dict[str, Any] | None:
        return self.descriptor if job_id == self.job_id and not self.acked else None

    def payload_bytes(self, payload_id: str) -> bytes | None:
        return (
            self.body
            if self.descriptor and payload_id == self.descriptor["payloadId"] and not self.acked
            else None
        )

    def acknowledge(self, job_id: str, body: dict[str, Any]) -> None:
        assert self.descriptor is not None
        assert job_id == self.job_id and body["payloadSha256"] == self.descriptor["payloadSha256"]
        self.acked = True


class EmptyNormalizationRepository:
    def get_terminal_payload(self, payload_id: str) -> None:
        return None


def test_worker_api_terminal_delivery_and_ack(monkeypatch: Any) -> None:
    request = _request()
    repo = MemoryAnalyticsRepository(request)
    vector = _vector()

    def fixed_download(*_: Any) -> Any:
        return (
            vector["artifactSamples"]["ledgerRevisionContent"],
            vector["artifactSamples"]["reconciliationManifest"],
        )

    settings = Settings(
        internal_api_key="test-internal-key",
        trading_review_artifact_hosts=("core-artifacts.internal",),
        trading_analytics_implementation_digest=request["targetVersionSet"]["implementationDigest"],
        trading_review_active_job_limit=20,
        database_url="postgresql://unused",
        trading_review_payload_encryption_key="S0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0s=",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_analytics_repository] = lambda: repo
    app.dependency_overrides[get_optional_analytics_repository] = lambda: repo
    app.dependency_overrides[get_trading_review_repository] = EmptyNormalizationRepository
    try:
        with TestClient(app) as client:
            base = "/trading-review/analytics-jobs"
            headers = {"X-Internal-Api-Key": "test-internal-key", "Idempotency-Key": "analytics-1"}
            assert client.post(base, json=request).status_code == 401
            accepted = client.post(base, json=request, headers=headers)
            assert accepted.status_code == 202
            _validate("TradingAnalyticsJobAccepted", accepted.json())
            replay = client.post(base, json=request, headers=headers)
            assert replay.status_code == 200 and replay.json()["jobId"] == repo.job_id
            changed = dict(request, coreAnalysisRunId=str(uuid4()))
            assert client.post(base, json=changed, headers=headers).status_code == 409
            assert (
                client.post(base, json=dict(request, unexpected=True), headers=headers).status_code
                == 400
            )
            unsupported = copy.deepcopy(request)
            unsupported["targetVersionSet"]["metricDefinitionVersionsByMetricId"][
                "ENTRY_WITHIN_WINDOW_AFTER_LOSS"
            ] = "2"
            unsupported["inputIdentity"]["targetVersionSet"] = unsupported["targetVersionSet"]
            assert client.post(base, json=unsupported, headers=headers).status_code == 422
            assert client.get(f"{base}/{repo.job_id}", headers=headers).status_code == 200
            duplicate = json.dumps(request).replace(
                '"coreLogicalJobId":', '"coreLogicalJobId":"other","coreLogicalJobId":', 1
            )
            assert client.post(base, content=duplicate, headers=headers).status_code == 422
            assert TradingAnalyticsWorkerHost(
                repo,
                AnalyticsWorkerSettings(
                    settings.trading_review_artifact_hosts,
                    settings.trading_analytics_implementation_digest,
                    settings.trading_analytics_max_input_bytes,
                ),
                worker_id="test-worker",
                download_inputs=fixed_download,
            ).run_once()
            assert (
                repo.payload is not None and repo.body is not None and repo.descriptor is not None
            )
            _validate("AnalyticsTerminalPayload", repo.payload)
            _validate("AnalyticsTerminalResultDescriptor", repo.descriptor)
            descriptor = client.get(f"{base}/{repo.job_id}/result", headers=headers)
            assert descriptor.status_code == 200
            resource = client.get(
                f"/trading-review/terminal-payloads/{repo.descriptor['payloadId']}", headers=headers
            )
            assert resource.content == repo.body
            ack = {
                "coreAnalysisRunId": request["coreAnalysisRunId"],
                "payloadId": repo.descriptor["payloadId"],
                "payloadSha256": repo.descriptor["payloadSha256"],
                "terminalResultHash": repo.descriptor["resultHash"],
                "attemptToken": request["attemptToken"],
                "deletionGeneration": request["deletionGeneration"],
                "disposition": "PERSISTED",
                "persistedAt": datetime.now(UTC).isoformat(),
            }
            assert (
                client.post(
                    f"/trading-review/jobs/{repo.job_id}/acknowledgement", json=ack, headers=headers
                ).status_code
                == 204
            )
            assert repo.acked
    finally:
        app.dependency_overrides.clear()


def test_worker_separates_input_and_engine_failures(monkeypatch: Any) -> None:
    worker_module = importlib.import_module("refinvest.application.analytics_worker")
    request = _request()
    settings = AnalyticsWorkerSettings(
        trading_analytics_max_input_bytes=1_000_000,
        trading_review_artifact_hosts=("core-artifacts.internal",),
        trading_analytics_implementation_digest=request["targetVersionSet"]["implementationDigest"],
    )
    input_repo = MemoryAnalyticsRepository(request)

    def bad_input(*_: Any) -> Any:
        raise AnalyticsInputFailure("INPUT_CONTENT_HASH_MISMATCH")

    assert TradingAnalyticsWorkerHost(
        input_repo, settings, worker_id="input-worker", download_inputs=bad_input
    ).run_once()
    assert input_repo.failure == ("INPUT_CONTENT_HASH_MISMATCH", False)
    assert input_repo.payload is None

    vector = _vector()
    engine_repo = MemoryAnalyticsRepository(request)

    def fixed_download(*_: Any) -> Any:
        return (
            vector["artifactSamples"]["ledgerRevisionContent"],
            vector["artifactSamples"]["reconciliationManifest"],
        )

    def bad_engine(**_: Any) -> Any:
        raise ValueError("private engine input")

    monkeypatch.setattr(worker_module, "calculate_behavior_metrics", bad_engine)
    assert TradingAnalyticsWorkerHost(
        engine_repo, settings, worker_id="engine-worker", download_inputs=fixed_download
    ).run_once()
    assert engine_repo.failure == ("ENGINE_INVARIANT_VIOLATION", False)
    assert engine_repo.payload is None
