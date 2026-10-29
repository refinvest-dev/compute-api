from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_backtest_worker_service_preserves_durable_worker_runtime_contract() -> None:
    service = (ROOT / "deploy/systemd/refinvest-backtest-worker.service").read_text(
        encoding="utf-8"
    )

    assert "Type=exec" in service
    assert "EnvironmentFile=/etc/refinvest/backtest-worker.env" in service
    assert "ExecStart=/opt/refinvest/compute-api/.venv/bin/refinvest-backtest-worker" in service
    assert "Restart=on-failure" in service
    assert "RestartSec=30s" in service
    assert "TimeoutStopSec=10m" in service
    assert "KillSignal=SIGTERM" in service


def test_backtest_worker_environment_uses_instance_role_without_access_keys() -> None:
    environment = (ROOT / "deploy/systemd/backtest-worker.env.example").read_text(encoding="utf-8")

    assert "REFINVEST_DATABASE_URL=" in environment
    assert "REFINVEST_ENGINE_VERSION=" in environment
    assert "REFINVEST_SNAPSHOT_STORAGE_S3_REGION=" in environment
    assert "AWS_ACCESS_KEY_ID" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment


def test_compute_api_service_preserves_internal_api_runtime_contract() -> None:
    service = (ROOT / "deploy/systemd/refinvest-compute-api.service").read_text(encoding="utf-8")
    environment = (ROOT / "deploy/systemd/compute-api.env.example").read_text(encoding="utf-8")

    assert "Type=exec" in service
    assert "EnvironmentFile=/etc/refinvest/compute-api.env" in service
    assert "uvicorn refinvest.app.main:app --host 0.0.0.0 --port 8000" in service
    assert "--reload" not in service
    assert "Restart=on-failure" in service
    assert "RestartSec=15s" in service
    assert "TimeoutStopSec=30s" in service
    assert "REFINVEST_DATABASE_URL=" in environment
    assert "REFINVEST_INTERNAL_API_KEY=" in environment
