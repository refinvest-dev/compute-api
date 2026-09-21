"""Scrape and durable-transition contracts for process-local metrics."""

from datetime import UTC, date, datetime, timedelta
from socket import socket
from urllib.request import urlopen

import pytest
from prometheus_client import generate_latest
from starlette.testclient import TestClient

from refinvest.app.main import create_app
from refinvest.application.backtest_workers import ClaimedBacktestJob
from refinvest.engine.validation.models import Period
from refinvest.observability.job_repository import ObservedWorkerRepository
from refinvest.observability.metrics import ApiMetrics, MetricsConfig, WorkerMetrics, serve_metrics


class FakeJobs:
    def claim_next(self, worker_id: str, lease_duration: timedelta) -> ClaimedBacktestJob:
        return ClaimedBacktestJob(
            "run-1", worker_id, "snapshot-1", 1, {}, datetime.now(UTC) - timedelta(seconds=3)
        )

    def heartbeat(self, run_id: str, worker_id: str, lease_duration: timedelta) -> bool:
        return True

    def mark_completed(
        self,
        run_id: str,
        worker_id: str,
        result_payload: dict[str, object],
        actual_period: Period,
        engine_version: str,
    ) -> bool:
        return True

    def mark_failed(
        self, run_id: str, worker_id: str, failure_reason: str, error_code: str | None
    ) -> bool:
        return True

    def schedule_retry(
        self, run_id: str, worker_id: str, retry_delay: timedelta, failure_reason: str
    ) -> bool:
        return True


def free_port() -> int:
    with socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def scrape(port: int) -> str:
    with urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2) as response:
        return response.read().decode()


def test_loopback_listener_is_opt_in_and_scrapeable() -> None:
    port = free_port()
    metrics = WorkerMetrics()
    with serve_metrics(MetricsConfig(port=port), metrics.registry):
        with socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0
    with serve_metrics(MetricsConfig(True, "127.0.0.1", port), metrics.registry):
        metrics.claims.inc()
        assert "refinvest_compute_worker_claims_total 1.0" in scrape(port)
        with pytest.raises(OSError):
            with serve_metrics(MetricsConfig(True, "127.0.0.1", port), ApiMetrics().registry):
                pass


@pytest.mark.parametrize(
    "bind,port", [("0.0.0.0", 9101), ("example.com", 9101), ("127.0.0.1", 0), ("127.0.0.1", 65536)]
)
def test_invalid_metrics_bind_or_port_fails(bind: str, port: int) -> None:
    with pytest.raises(ValueError):
        MetricsConfig(True, bind, port)


def test_api_scrape_uses_bounded_route_and_excludes_sensitive_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = free_port()
    monkeypatch.setenv("REFINVEST_API_METRICS_ENABLED", "true")
    monkeypatch.setenv("REFINVEST_API_METRICS_PORT", str(port))
    application = create_app()
    with TestClient(application) as client:
        assert client.get("/health").status_code == 200
        run_id = "secret-run-id"
        api_key = "secret-api-key"
        assert (
            client.get(f"/backtests/{run_id}", headers={"X-Internal-Api-Key": api_key}).status_code
            == 503
        )
        output = scrape(port)
        assert 'operation="status",status_class="5xx"' in output
        assert run_id not in output
        assert api_key not in output
        assert "/health" not in output
        assert "refinvest_compute_http_request_duration_seconds_bucket" in output


def test_durable_metrics_only_follow_successful_transitions() -> None:
    metrics = WorkerMetrics()
    jobs = FakeJobs()
    observed = ObservedWorkerRepository(jobs, metrics)
    assert observed.claim_next("worker-1", timedelta(seconds=60)) is not None
    period = Period(date(2024, 1, 1), date(2024, 1, 2))
    assert observed.mark_completed("run-1", "worker-1", {}, period, "v1")
    assert observed.schedule_retry("run-2", "worker-1", timedelta(seconds=30), "reason")
    assert observed.mark_failed("run-3", "worker-1", "reason", "unbounded-secret")
    output = generate_latest(metrics.registry).decode()
    assert "refinvest_compute_worker_queue_wait_seconds_count 1.0" in output
    assert 'status="COMPLETED"} 1.0' in output
    assert 'status="FAILED"} 1.0' in output
    assert 'error_code="UNSPECIFIED"} 1.0' in output
    assert "refinvest_compute_worker_retries_scheduled_total 1.0" in output
    assert "unbounded-secret" not in output


def test_lost_lease_or_failed_write_does_not_count_terminal_or_retry() -> None:
    class LostLeaseJobs(FakeJobs):
        def mark_completed(
            self,
            run_id: str,
            worker_id: str,
            result_payload: dict[str, object],
            actual_period: Period,
            engine_version: str,
        ) -> bool:
            return False

        def mark_failed(
            self, run_id: str, worker_id: str, failure_reason: str, error_code: str | None
        ) -> bool:
            return False

        def schedule_retry(
            self, run_id: str, worker_id: str, retry_delay: timedelta, failure_reason: str
        ) -> bool:
            return False

    metrics = WorkerMetrics()
    jobs = ObservedWorkerRepository(LostLeaseJobs(), metrics)
    period = Period(date(2024, 1, 1), date(2024, 1, 2))
    assert not jobs.mark_completed("run", "worker", {}, period, "v1")
    assert not jobs.mark_failed("run", "worker", "reason", "EXECUTION_TIMEOUT")
    assert not jobs.schedule_retry("run", "worker", timedelta(seconds=30), "reason")
    output = generate_latest(metrics.registry).decode()
    assert "refinvest_compute_worker_terminal_transitions_total{" not in output
    assert "refinvest_compute_worker_failures_total{" not in output
    assert "refinvest_compute_worker_retries_scheduled_total 0.0" in output
