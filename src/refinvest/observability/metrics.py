"""Process-local Prometheus registries and loopback-only scrape listeners."""

import ipaddress
import logging
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

_LOG = logging.getLogger(__name__)
HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
QUEUE_BUCKETS = (0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 1800, 3600)
EXECUTION_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 900)
ERROR_CODES = frozenset(
    {
        "MISSING_REQUIRED_DATA",
        "DATASET_CORRUPTION",
        "CALENDAR_RESOLUTION_FAILED",
        "PRICE_DATA_MISSING",
        "EXECUTION_TIMEOUT",
        "DSL_INVALID",
    }
)


def safe_record(operation: object, *args: object) -> None:
    """A broken metrics adapter must not affect business state transitions."""
    try:
        operation(*args)  # type: ignore[operator]
    except Exception:
        _LOG.warning("metrics_record_failed")


@dataclass(frozen=True)
class MetricsConfig:
    enabled: bool = False
    bind: str = "127.0.0.1"
    port: int = 9101

    def __post_init__(self) -> None:
        try:
            address = ipaddress.ip_address(self.bind)
        except ValueError as error:
            raise ValueError("Metrics bind must be a loopback IP address.") from error
        if not address.is_loopback or address.version != 4:
            raise ValueError("Metrics bind must be an IPv4 loopback address.")
        if not 1 <= self.port <= 65535:
            raise ValueError("Metrics port must be in 1..65535.")


@contextmanager
def serve_metrics(
    config: MetricsConfig, registry: CollectorRegistry
) -> Generator[None, None, None]:
    if not config.enabled:
        yield
        return

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/metrics":
                self.send_error(404)
                return
            try:
                payload = generate_latest(registry)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except Exception:
                _LOG.warning("metrics_scrape_failed")

        def log_message(self, format: str, *args: object) -> None:
            return

    class MetricsServer(ThreadingHTTPServer):
        allow_reuse_address = False

    server = MetricsServer((config.bind, config.port), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class ApiMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "refinvest_compute_http_requests_total",
            "HTTP responses",
            ["operation", "status_class"],
            registry=self.registry,
        )
        self.duration = Histogram(
            "refinvest_compute_http_request_duration_seconds",
            "HTTP response duration",
            ["operation"],
            buckets=HTTP_BUCKETS,
            registry=self.registry,
        )
        self.accepted = Counter(
            "refinvest_compute_backtest_jobs_accepted_total",
            "New durable jobs",
            registry=self.registry,
        )
        self.replayed = Counter(
            "refinvest_compute_backtest_idempotency_replays_total",
            "Existing acceptance replays",
            registry=self.registry,
        )
        self.rejected = Counter(
            "refinvest_compute_backtest_capacity_rejections_total",
            "Capacity rejections",
            registry=self.registry,
        )


class WorkerMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.claims = Counter(
            "refinvest_compute_worker_claims_total", "Claimed attempts", registry=self.registry
        )
        self.queue = Histogram(
            "refinvest_compute_worker_queue_wait_seconds",
            "Creation to claim",
            buckets=QUEUE_BUCKETS,
            registry=self.registry,
        )
        self.duration = Histogram(
            "refinvest_compute_worker_attempt_duration_seconds",
            "Executor and heartbeat stop duration",
            buckets=EXECUTION_BUCKETS,
            registry=self.registry,
        )
        self.terminal = Counter(
            "refinvest_compute_worker_terminal_transitions_total",
            "Committed terminal transitions",
            ["status"],
            registry=self.registry,
        )
        self.retries = Counter(
            "refinvest_compute_worker_retries_scheduled_total",
            "Committed retry schedules",
            registry=self.registry,
        )
        self.timeouts = Counter(
            "refinvest_compute_worker_timeouts_total",
            "Process timeout outcomes",
            registry=self.registry,
        )
        self.failures = Counter(
            "refinvest_compute_worker_failures_total",
            "Committed failed transitions",
            ["error_code"],
            registry=self.registry,
        )

    def failed(self, error_code: str | None) -> None:
        self.terminal.labels("FAILED").inc()
        self.failures.labels(error_code if error_code in ERROR_CODES else "UNSPECIFIED").inc()

    def queue_wait(self, seconds: float) -> None:
        self.queue.observe(seconds)

    def attempt_duration(self, seconds: float) -> None:
        self.duration.observe(seconds)

    def timeout(self) -> None:
        self.timeouts.inc()
