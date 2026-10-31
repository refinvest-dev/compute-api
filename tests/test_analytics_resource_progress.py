"""Failed development measurements retain observations without claiming successful work."""

import json
import sys
from pathlib import Path

import pytest

from tools import analytics_resource_baseline as probe
from tools.analytics_synthetic_inputs import ROLES, synthetic_inputs


def inputs(directory: Path) -> None:
    request, bodies = synthetic_inputs(8, evidence_per_record=2)
    (directory / "request.json").write_text(json.dumps(request), encoding="utf-8")
    for role, body in bodies.items():
        (directory / f"{role}.json").write_bytes(body)


def test_phase_observation_preserves_complete_body_and_records_shape(tmp_path: Path):
    inputs(tmp_path)
    original = probe.measure(tmp_path, 10_000_000)
    observed = probe.measure_sample(tmp_path, 10_000_000, 0)
    for name in ("status", "input_hash", "result_hash", "body_sha256", "section_counts"):
        assert original[name] == observed[name]
    assert observed["record_count"] == 16
    assert observed["source_evidence_count"] == 32
    progress = observed["progress"]
    assert progress["phase"] == "encryption"
    assert progress["state"] == "SUCCEEDED"
    assert set(progress["phases"]) == set(observed["phases"])
    assert "canonicalRecords" not in (tmp_path / "sample-0-progress.json").read_text()


def test_result_cap_keeps_the_failed_serialization_phase(tmp_path: Path):
    inputs(tmp_path)
    result = probe.measure_sample(tmp_path, 100, 0)
    assert result["status"] == "RESULT_PAYLOAD_TOO_LARGE"
    assert result["progress"]["phase"] == "serialization"
    assert result["progress"]["state"] == "FAILED"
    assert result["progress"]["error_type"] == "AnalyticsContractError"
    assert "encryption" not in result["phases"]
    assert "body_sha256" not in result


def test_memory_error_is_diagnostic_failure_not_a_completed_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    inputs(tmp_path)

    def fail(*_, **__):
        raise MemoryError("synthetic private detail")

    monkeypatch.setattr("refinvest.adapters.analytics.artifacts.download_inputs", fail)
    result = probe.measure_sample(tmp_path, 10_000_000, 0)
    assert result["status"] == "BENCHMARK_MEMORY_ERROR"
    assert result["progress"]["phase"] == "read_verify_parse"
    assert result["progress"]["state"] == "FAILED"
    assert result["progress"]["error_type"] == "MemoryError"
    assert "body_sha256" not in result
    assert "synthetic private detail" not in json.dumps(result)


@pytest.mark.parametrize("content", [None, "{broken", "[]"])
def test_missing_or_corrupt_progress_does_not_fabricate_a_phase(
    tmp_path: Path, content: str | None
):
    target = tmp_path / "progress.json"
    if content is not None:
        target.write_text(content)
    assert probe.read_progress(target) == {"available": False}


def test_existing_progress_is_preserved_and_cannot_be_reused(tmp_path: Path):
    inputs(tmp_path)
    target = tmp_path / "sample-0-progress.json"
    target.write_text('{"phase":"previous_observation"}')
    with pytest.raises(FileExistsError, match="fresh sample index"):
        probe.measure_sample(tmp_path, 10_000_000, 0)
    assert target.read_text() == '{"phase":"previous_observation"}'


def test_real_measurement_child_failure_retains_stderr_and_last_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    original = probe.run_child
    target = tmp_path / "run"

    def tamper_after_prepare(command: list[str], timeout: float):
        if "measure" in command:
            artifact = target / f"{ROLES[0]}.json"
            with artifact.open("r+b") as stream:
                stream.seek(-1, 2)
                stream.write(b"x")
        return original(command, timeout)

    monkeypatch.setattr(probe, "run_child", tamper_after_prepare)
    monkeypatch.setattr(
        sys, "argv", ["probe", "run", str(target), "--episodes", "8", "--repetitions", "2"]
    )
    probe.main()
    summary = json.loads(capsys.readouterr().out)
    report = json.loads((target / "report.json").read_text())
    assert summary["statuses"] == ["BENCHMARK_PROCESS_FAILURE"]
    assert not report["determinism_passed"]
    sample = report["samples"][0]
    assert sample["returncode"] != 0
    assert sample["progress"]["phase"] == "read_verify_parse"
    assert sample["progress"]["state"] == "FAILED"
    assert (target / "sample-0-stderr.log").stat().st_size > 0
