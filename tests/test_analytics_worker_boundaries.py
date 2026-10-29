"""Publication, lease and failure policies of the port-driven Analytics worker."""

import importlib
from dataclasses import fields
from datetime import timedelta
from threading import Event

import pytest

from refinvest.application.analytics_input import AnalyticsInputFailure
from refinvest.application.analytics_jobs import AnalyticsJobConflict
from refinvest.application.analytics_result import bounded_analytics_json
from refinvest.application.analytics_worker import (
    AnalyticsWorkerSettings,
    TradingAnalyticsWorkerHost,
)
from refinvest.contracts.analytics.identity import AnalyticsContractError
from tests.test_trading_analytics_runtime import MemoryAnalyticsRepository, _request, _vector

worker = importlib.import_module("refinvest.application.analytics_worker")


def _download(*_):
    samples = _vector()["artifactSamples"]
    return samples["ledgerRevisionContent"], samples["reconciliationManifest"]


def _host(repo, download=_download):
    return TradingAnalyticsWorkerHost(
        repo,
        AnalyticsWorkerSettings(
            ("core-artifacts.internal",),
            repo.request["targetVersionSet"]["implementationDigest"],
            1_000_000,
        ),
        worker_id="worker",
        download_inputs=download,
    )


def test_shutdown_before_claim_does_not_touch_repository():
    repo = MemoryAnalyticsRepository(_request())
    shutdown = Event()
    shutdown.set()
    assert not _host(repo).run_once(shutdown)
    assert not repo.claimed


@pytest.mark.parametrize("stage", ["download", "calculate", "serialize"])
def test_shutdown_before_publication_suppresses_all_terminal_writes(monkeypatch, stage):
    repo = MemoryAnalyticsRepository(_request())
    shutdown = Event()

    def download(*args):
        value = _download(*args)
        if stage == "download":
            shutdown.set()
        return value

    if stage != "download":
        name = "calculate_behavior_metrics" if stage == "calculate" else "bounded_analytics_json"
        original = getattr(worker, name)

        def stop_after(*args, **kwargs):
            value = original(*args, **kwargs)
            shutdown.set()
            return value

        monkeypatch.setattr(worker, name, stop_after)
    assert _host(repo, download).run_once(shutdown)
    assert repo.claimed and repo.body is None and repo.failure is None


@pytest.mark.parametrize("outcome", ["renewed", "lost", "exception"])
def test_heartbeat_renews_same_lease_and_loss_suppresses_publication(monkeypatch, outcome):
    calls = []
    targets = []

    class Finished:
        done = False
        waited = False

        def wait(self, seconds):
            calls.append(("wait", seconds))
            if self.waited:
                return True
            self.waited = True
            return False

        def set(self):
            self.done = True

    finished = Finished()
    events = iter([finished, Event()])
    monkeypatch.setattr(worker, "Event", lambda: next(events))

    class Thread:
        def __init__(self, *, target, name, daemon):
            targets.append(target)
            assert name == "analytics-lease-heartbeat" and daemon

        def start(self):
            pass

        def join(self, *, timeout):
            assert finished.done
            calls.append(("join", timeout))

    monkeypatch.setattr(worker, "Thread", Thread)

    class Repository(MemoryAnalyticsRepository):
        def claim_next(self, worker_id, lease):
            calls.append(("claim", lease))
            return super().claim_next(worker_id, lease)

        def heartbeat(self, claim, lease):
            assert claim["leaseOwner"] == "worker" and claim["leaseGeneration"] == 1
            calls.append(("heartbeat", lease))
            if outcome == "exception":
                raise OSError("synthetic heartbeat outage")
            return outcome == "renewed"

    def download(*args):
        targets[0]()
        assert args[3]() is (outcome != "renewed")
        return _download(*args)

    repo = Repository(_request())
    assert _host(repo, download).run_once()
    assert (repo.body is not None) is (outcome == "renewed")
    assert repo.failure is None
    assert calls[:3] == [
        ("claim", timedelta(seconds=60)),
        ("wait", 20),
        ("heartbeat", timedelta(seconds=60)),
    ]
    assert calls[-1] == ("join", 1)


@pytest.mark.parametrize(
    ("error", "failure"),
    [
        (
            AnalyticsInputFailure("INPUT_CONTENT_HASH_MISMATCH"),
            ("INPUT_CONTENT_HASH_MISMATCH", False),
        ),
        (AnalyticsInputFailure("INPUT_FETCH_FAILED", retryable=True), ("INPUT_FETCH_FAILED", True)),
        (AnalyticsInputFailure("LEASE_LOST"), None),
        (AnalyticsContractError("RESULT_INTEGRITY_FAILURE"), ("RESULT_INTEGRITY_FAILURE", False)),
        (AnalyticsJobConflict("RESULT_PAYLOAD_TOO_LARGE"), ("RESULT_PAYLOAD_TOO_LARGE", False)),
        (AnalyticsJobConflict("LEASE_LOST"), None),
        (ValueError("private input"), ("ENGINE_INVARIANT_VIOLATION", False)),
        (OSError("private connection"), ("TEMPORARY_DEPENDENCY_FAILURE", True)),
    ],
)
def test_failure_classification_through_injected_port(error, failure):
    def download(*_):
        raise error

    repo = MemoryAnalyticsRepository(_request())
    assert _host(repo, download).run_once()
    assert repo.failure == failure and repo.body is None


@pytest.mark.parametrize("limiting_side", ["request", "repository"])
def test_payload_cap_uses_smaller_request_or_repository_limit(limiting_side):
    request = _request()
    repo = MemoryAnalyticsRepository(request)
    if limiting_side == "request":
        request["resultDeliveryPolicy"]["maxPayloadBytes"] = 100
    else:
        repo.max_payload_bytes = 100
    assert _host(repo).run_once()
    assert repo.failure == ("RESULT_PAYLOAD_TOO_LARGE", False)
    assert repo.body is None


def test_bounded_serialization_measures_utf8_bytes():
    payload = {"value": "한글😀"}
    expected = '{"value":"한글😀"}'.encode()
    assert bounded_analytics_json(payload, len(expected)) == expected
    with pytest.raises(AnalyticsContractError, match="RESULT_PAYLOAD_TOO_LARGE"):
        bounded_analytics_json(payload, len(expected) - 1)


def test_composition_injects_adapter_and_only_worker_settings(monkeypatch):
    from refinvest.app.config import Settings

    composition = importlib.import_module("refinvest.app.trading_analytics_worker")
    calls = []
    repo = MemoryAnalyticsRepository(_request())

    def repository(*args, **kwargs):
        calls.append((args, kwargs))
        return repo

    monkeypatch.setattr(composition, "PostgresAnalyticsJobRepository", repository)
    settings = Settings(
        "synthetic-internal-key",
        database_url="postgresql://unused",
        trading_review_active_job_limit=20,
        trading_review_payload_encryption_key="S0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0s=",
        trading_review_artifact_hosts=("core-artifacts.internal",),
        trading_analytics_implementation_digest=repo.request["targetVersionSet"][
            "implementationDigest"
        ],
        worker_id="configured-worker",
    )
    host = composition.create_trading_analytics_worker(settings)
    assert host.repository is repo and host.worker_id == "configured-worker"
    assert host.download_inputs is composition.download_inputs
    assert calls[0][0][0] == settings.database_url
    assert calls[0][1] == {
        "active_job_limit": 20,
        "max_payload_bytes": settings.trading_review_max_payload_bytes,
    }
    assert {field.name for field in fields(host.settings)} == {
        "trading_review_artifact_hosts",
        "trading_analytics_implementation_digest",
        "trading_analytics_max_input_bytes",
    }
