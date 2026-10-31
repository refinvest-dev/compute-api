"""HTTP observation behavior before and after moving middleware out of app."""

from contextlib import nullcontext
from importlib import import_module

import pytest
from prometheus_client import generate_latest
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.testclient import TestClient

from refinvest.app.config import Settings
from refinvest.app.main import create_app
from refinvest.observability.metrics import MetricsConfig


@pytest.fixture
def observer_module():
    return import_module("refinvest.app.main")


@pytest.fixture
def observing_app(monkeypatch: pytest.MonkeyPatch):
    module = import_module("refinvest.app.main")
    monkeypatch.setattr(
        module, "get_settings", lambda: Settings(None, api_metrics=MetricsConfig(enabled=True))
    )
    # Existing scrape tests exercise the real listener; these probes use its registry directly.
    monkeypatch.setattr(module, "serve_metrics", lambda *_: nullcontext())
    return create_app()


def install_probe(application, *, status: int = 201, error: Exception | None = None) -> None:
    async def probe(_: Request) -> Response:
        if error is not None:
            raise error
        return Response(b"probe-response", status_code=status, headers={"X-Probe": "kept"})

    application.router.routes.insert(
        0, Route("/{probe:path}", probe, methods=["GET", "POST", "DELETE"])
    )


@pytest.mark.parametrize(
    ("method", "path", "operation"),
    [
        ("GET", "/health", None),
        ("POST", "/backtests", "submit"),
        ("GET", "/backtests/private-run", "status"),
        ("GET", "/backtests/private-run/result", "other"),
        ("POST", "/backtests/private-run", "other"),
        ("GET", "/backtests/", "status"),
        ("GET", "/assets", "assets"),
        ("GET", "/assets/QQQ/availability", "assets"),
        ("GET", "/assets-shadow", "assets"),
        ("POST", "/assets", "other"),
        ("GET", "/series", "series"),
        ("GET", "/series-shadow", "series"),
        ("POST", "/series", "other"),
        ("POST", "/trading-review/analytics-jobs", "trading_review"),
        ("DELETE", "/trading-review/normalization-jobs/private-job", "trading_review"),
        ("GET", "/trading-review", "other"),
        ("GET", "/private-unknown", "other"),
    ],
)
def test_http_operation_labels_and_timing_are_preserved(
    observing_app, observer_module, monkeypatch: pytest.MonkeyPatch, method, path, operation
) -> None:
    install_probe(observing_app)
    ticks = iter([10.0, 10.125])
    monkeypatch.setattr(observer_module, "monotonic", lambda: next(ticks))
    with TestClient(observing_app) as client:
        response = client.request(
            method, path + "?secret=private-query", headers={"X-Internal-Api-Key": "private-key"}
        )
    assert response.status_code == 201
    assert response.content == b"probe-response"
    assert response.headers["X-Probe"] == "kept"
    registry = observing_app.state.metrics.registry
    if operation is None:
        assert registry.get_sample_value("refinvest_compute_http_requests_total", {}) is None
        assert 'operation="' not in generate_latest(registry).decode()
    else:
        assert (
            registry.get_sample_value(
                "refinvest_compute_http_requests_total",
                {"operation": operation, "status_class": "2xx"},
            )
            == 1
        )
        assert (
            registry.get_sample_value(
                "refinvest_compute_http_request_duration_seconds_count", {"operation": operation}
            )
            == 1
        )
        assert (
            registry.get_sample_value(
                "refinvest_compute_http_request_duration_seconds_sum", {"operation": operation}
            )
            == 0.125
        )
    exposition = generate_latest(registry).decode()
    assert path not in exposition
    for private in ("private-run", "private-job", "private-query", "private-key"):
        assert private not in exposition


@pytest.mark.parametrize(
    ("status", "status_class"), [(201, "2xx"), (302, "3xx"), (422, "4xx"), (503, "5xx")]
)
def test_http_status_classes_follow_the_response(observing_app, status, status_class) -> None:
    install_probe(observing_app, status=status)
    with TestClient(observing_app) as client:
        response = client.get("/probe", follow_redirects=False)
    assert response.status_code == status
    assert (
        observing_app.state.metrics.registry.get_sample_value(
            "refinvest_compute_http_requests_total",
            {"operation": "other", "status_class": status_class},
        )
        == 1
    )


@pytest.mark.parametrize("raise_server_exceptions", [False, True])
def test_unhandled_error_is_observed_as_500_and_keeps_exception_behavior(
    observing_app, raise_server_exceptions
) -> None:
    error = RuntimeError("private-exception")
    install_probe(observing_app, error=error)
    with TestClient(observing_app, raise_server_exceptions=raise_server_exceptions) as client:
        if raise_server_exceptions:
            with pytest.raises(RuntimeError) as caught:
                client.get("/probe")
            assert caught.value is error
        else:
            response = client.get("/probe")
            assert response.status_code == 500
            assert b"private-exception" not in response.content
    registry = observing_app.state.metrics.registry
    assert (
        registry.get_sample_value(
            "refinvest_compute_http_requests_total", {"operation": "other", "status_class": "5xx"}
        )
        == 1
    )
    assert (
        registry.get_sample_value(
            "refinvest_compute_http_request_duration_seconds_count", {"operation": "other"}
        )
        == 1
    )
    assert "private-exception" not in generate_latest(registry).decode()


@pytest.mark.parametrize("collector", ["requests", "duration"])
def test_metric_adapter_failure_preserves_response_and_logs_safely(
    observing_app, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, collector
) -> None:
    install_probe(observing_app)

    def fail(*_):
        raise RuntimeError("private-metrics-detail")

    monkeypatch.setattr(getattr(observing_app.state.metrics, collector), "labels", fail)
    with TestClient(observing_app) as client:
        response = client.get("/probe")
    assert response.status_code == 201
    assert response.content == b"probe-response"
    assert response.headers["X-Probe"] == "kept"
    assert "metrics_record_failed" in caplog.text
    assert "private-metrics-detail" not in caplog.text


def test_enabled_setting_is_read_for_each_request(observing_app, monkeypatch) -> None:
    install_probe(observing_app)
    enabled = False
    monkeypatch.setattr(
        import_module("refinvest.app.main"),
        "get_settings",
        lambda: Settings(None, api_metrics=MetricsConfig(enabled=enabled)),
    )
    with TestClient(observing_app) as client:
        assert client.get("/probe").status_code == 201
        enabled = True
        assert client.get("/probe").status_code == 201
        enabled = False
        assert client.get("/probe").status_code == 201
    assert (
        observing_app.state.metrics.registry.get_sample_value(
            "refinvest_compute_http_requests_total", {"operation": "other", "status_class": "2xx"}
        )
        == 1
    )


def test_health_exclusion_does_not_read_request_settings(observing_app, monkeypatch) -> None:
    def fail():
        raise AssertionError("health must skip request metrics settings")

    with TestClient(observing_app) as client:
        monkeypatch.setattr(import_module("refinvest.app.main"), "get_settings", fail)
        assert client.get("/health").status_code == 200


def test_apps_keep_separate_registries(observing_app) -> None:
    other = create_app()
    assert other.state.metrics.registry is not observing_app.state.metrics.registry
    install_probe(observing_app)
    with TestClient(observing_app) as client:
        assert client.get("/probe").status_code == 201
    assert 'operation="' not in generate_latest(other.state.metrics.registry).decode()
