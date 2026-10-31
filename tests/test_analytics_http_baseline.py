"""Probe the actual HTTP reader/parser, not a transport substitute."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from refinvest.contracts.analytics.identity import canonical_bytes, validate_create_identity
from tools.analytics_http_baseline import artifact_server, measure, prepare_inputs
from tools.analytics_synthetic_inputs import ROLES, synthetic_inputs


def test_padded_artifacts_preserve_documents_and_have_new_exact_byte_identities(tmp_path: Path):
    target = tmp_path / "input"
    prepare_inputs(target, 100_000)
    request = json.loads((target / "request.json").read_text())
    original_request, bodies = synthetic_inputs(16)
    validate_create_identity(request)
    assert request["expectedInputHash"] != original_request["expectedInputHash"]
    for role in ROLES:
        body = (target / f"{role}.json").read_bytes()
        assert len(body) == 100_000
        assert body.endswith(b" ")
        assert canonical_bytes(json.loads(body)) == bodies[role]
        assert request[role]["sha256"] == hashlib.sha256(body).hexdigest()
        assert request["inputIdentity"][role]["sha256"] == request[role]["sha256"]


def test_prepare_never_truncates_or_overwrites_a_run(tmp_path: Path):
    target = tmp_path / "input"
    with pytest.raises(ValueError, match="fit both documents"):
        prepare_inputs(target, 10)
    assert not target.exists()
    prepare_inputs(target, 100_000)
    with pytest.raises(FileExistsError):
        prepare_inputs(target, 100_000)


def test_real_http_chunk_reads_validate_documents_and_reproduce_identity(tmp_path: Path):
    target = tmp_path / "input"
    prepare_inputs(target, 100_000)
    _, bodies = synthetic_inputs(16)
    with artifact_server(target, 4096, 0) as base_url:
        first, second = (measure(target, base_url, 30, 100_000) for _ in range(2))
    assert first["status"] == second["status"] == "INPUTS_VERIFIED"
    assert (
        first["document_sha256"]
        == second["document_sha256"]
        == {role: hashlib.sha256(body).hexdigest() for role, body in bodies.items()}
    )
    assert first["input_hash"] == second["input_hash"]
    assert first["episode_count"] == 16
    assert first["record_count"] == 32
    assert first["download_parse_seconds"] > 0
    assert first["memory"]["peak_rss_bytes"] > 0


def test_real_http_content_tampering_is_not_a_verified_sample(tmp_path: Path):
    target = tmp_path / "input"
    prepare_inputs(target, 100_000)
    with (target / f"{ROLES[0]}.json").open("r+b") as stream:
        stream.seek(-1, 2)
        stream.write(b"x")
    with artifact_server(target, 4096, 0) as base_url:
        result = measure(target, base_url, 30, 100_000)
    assert result["status"] == "INPUT_CONTENT_HASH_MISMATCH"
    assert not result["retryable"]
    assert "document_sha256" not in result
    assert "episode_count" not in result


@pytest.mark.parametrize(
    "base_url",
    [
        "https://127.0.0.1:1",
        "http://example.invalid:1",
        "http://127.0.0.1:1/path",
        "http://user@127.0.0.1:1",
    ],
)
def test_measure_cannot_target_an_external_or_non_probe_server(tmp_path: Path, base_url: str):
    with pytest.raises(ValueError, match="loopback"):
        measure(tmp_path, base_url, 30, 100_000)


@pytest.mark.parametrize(
    "options,status,verified",
    [
        ([], "INPUTS_VERIFIED", True),
        (["--max-input-bytes", "99999"], "TEMPORARY_DEPENDENCY_FAILURE", False),
        (["--grant-seconds", "0"], "INPUT_REFERENCE_EXPIRED", False),
    ],
)
def test_cli_uses_fresh_children_and_records_failures_without_false_determinism(
    tmp_path: Path, options: list[str], status: str, verified: bool
):
    root = Path(__file__).resolve().parents[1]
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("REFINVEST_", "AWS_"))
        and key not in ("DATABASE_URL", "PGPASSWORD", "PGSERVICE", "VIRTUAL_ENV")
    }
    env.update(
        PYTHONPATH=os.pathsep.join((str(root / "src"), str(root))), AWS_EC2_METADATA_DISABLED="true"
    )
    target = tmp_path / "run"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.analytics_http_baseline",
            "run",
            str(target),
            "--artifact-bytes",
            "100000",
            "--repetitions",
            "2",
            *options,
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    report = json.loads((target / "report.json").read_text())
    assert summary["statuses"] == [status, status]
    assert report["determinism_passed"] is verified
    assert len(report["samples"]) == 2
    assert all(sample["process_seconds"] > 0 for sample in report["samples"])
    assert all(("document_sha256" in sample) is verified for sample in report["samples"])


@pytest.mark.parametrize(
    "option,value",
    [
        ("--chunk-delay-seconds", "nan"),
        ("--grant-seconds", "inf"),
        ("--watchdog-seconds", "-1"),
        ("--artifact-bytes", "536870913"),
    ],
)
def test_cli_rejects_invalid_experiment_bounds_before_creating_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, option: str, value: str
):
    from tools.analytics_http_baseline import main

    target = tmp_path / "run"
    monkeypatch.setattr(sys, "argv", ["probe", "run", str(target), option, value])
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 2
    assert not target.exists()
