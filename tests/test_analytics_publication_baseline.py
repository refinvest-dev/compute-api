"""Publication measurements reject unsafe DB targets and preserve synthetic result identity."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from refinvest.application.analytics_result import bounded_analytics_json, build_analytics_payload
from refinvest.contracts.analytics.identity import strict_json
from tools import analytics_publication_baseline as probe
from tools.analytics_resource_baseline import measure
from tools.analytics_synthetic_inputs import synthetic_inputs


@pytest.fixture(autouse=True)
def clean_pg_environment(monkeypatch):
    for name in ("PGHOST", "PGHOSTADDR", "PGDATABASE", "PGOPTIONS", "PGSERVICE"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    "override",
    [
        "host=production.internal",
        "host=localhost,127.0.0.1",
        "hostaddr=192.0.2.1",
        "dbname=refinvest",
        "options='-c search_path=public'",
        "options='-c search_path=ar08_pub_a,public'",
        "service=unsafe",
    ],
)
def test_unsafe_targets_rejected_before_connect(override: str):
    base = "host=localhost dbname=refinvest_integration options='-c search_path=ar08_pub_test'"
    with pytest.raises(ValueError):
        probe.local_connection(base + " " + override, "test")


def test_environment_service_and_nonlocal_hostaddr_rejected(monkeypatch):
    url = "host=localhost dbname=refinvest_integration options='-c search_path=ar08_pub_test'"
    for name, value in (("PGSERVICE", "unsafe"), ("PGHOSTADDR", "192.0.2.2")):
        with monkeypatch.context() as patch:
            patch.setenv(name, value)
            with pytest.raises(ValueError):
                probe.local_connection(url, "test")
    monkeypatch.setenv("PGHOSTADDR", "::1")
    frozen, schema = probe.local_connection(url, "test")
    assert schema == "ar08_pub_test"
    assert probe.conninfo_to_dict(frozen)["hostaddr"] == "::1"


def test_prepared_sections_match_existing_full_pipeline(tmp_path: Path):
    prepared = tmp_path / "prepared"
    probe.prepare(prepared, 4)
    document = strict_json((prepared / "prepared.json").read_bytes())
    request = document["request"]
    fixed = datetime(2025, 3, 1, tzinfo=UTC)
    payload = build_analytics_payload(
        {
            "jobId": "synthetic-job",
            "attemptToken": request["attemptToken"],
            "deletionGeneration": 0,
            "createdAt": fixed,
        },
        request,
        document["sections"],
        document["digests"],
        terminal_at=fixed,
    )
    original = tmp_path / "original"
    original.mkdir()
    request, bodies = synthetic_inputs(4)
    (original / "request.json").write_text(json.dumps(request))
    for role, body in bodies.items():
        (original / f"{role}.json").write_bytes(body)
    expected = measure(original, probe.MAXIMUM)
    import hashlib

    assert (
        hashlib.sha256(bounded_analytics_json(payload, probe.MAXIMUM)).hexdigest()
        == expected["body_sha256"]
    )
    assert document["digests"]["resultHash"] == expected["result_hash"]
    with pytest.raises(FileExistsError):
        probe.prepare(prepared, 4)


def test_corrupt_preparation_fails_before_database_access(tmp_path: Path):
    probe.prepare(tmp_path / "prepared", 4)
    (tmp_path / "prepared/prepared.json").write_bytes(b"{}")
    with pytest.raises(ValueError, match="digest mismatch"):
        probe.measure(tmp_path / "prepared")


def test_cli_does_not_disclose_database_exception(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("sys.argv", ["probe", "measure", str(tmp_path)])

    def fail(*args, **kwargs):
        raise RuntimeError("postgresql://private:secret@host/database")

    monkeypatch.setattr(probe, "measure", fail)
    with pytest.raises(SystemExit) as error:
        probe.main()
    assert error.value.code == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "BENCHMARK_FAILED",
        "error_type": "RuntimeError",
    }
