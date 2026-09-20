"""Versioned benchmark report assembly and atomic persistence."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from refinvest.benchmarking.inputs import FixtureInput, ScenarioInput
from refinvest.benchmarking.measurement import (
    REQUIRED_STAGES,
    IterationResult,
    MeasurementResult,
)
from refinvest.benchmarking.statistics import duration_statistics


def build_report(
    fixture: FixtureInput,
    scenario: ScenarioInput,
    measurement: MeasurementResult,
    *,
    cache_mode: str,
    warmups: int,
    iterations: int,
    engine_version: str,
    started_at: datetime,
    completed_at: datetime,
    expected: dict[str, object] | None,
    repository_root: Path,
) -> tuple[dict[str, object], bool]:
    samples = [_sample_payload(sample) for sample in measurement.samples]
    successful = [sample for sample in measurement.samples if sample.status == "SUCCEEDED"]
    summary: dict[str, object] = {
        "sampleCount": len(measurement.samples),
        "successfulIterations": len(successful),
        "failedIterations": len(measurement.samples) - len(successful),
        "total": duration_statistics([sample.total_nanoseconds for sample in successful])
        if successful
        else None,
        "stages": {
            stage: duration_statistics([sample.stage_nanoseconds[stage] for sample in successful])
            for stage in REQUIRED_STAGES
            if successful and all(stage in sample.stage_nanoseconds for sample in successful)
        },
        "pythonPeakAllocatedBytes": {
            "median": _integer_median(
                [sample.python_peak_allocated_bytes for sample in successful]
            ),
            "max": max(sample.python_peak_allocated_bytes for sample in successful),
        }
        if successful
        else None,
    }
    correctness_mismatches = list(measurement.mismatches)
    if expected is not None:
        correctness_mismatches.extend(
            _expected_mismatches(fixture, scenario, measurement, expected)
        )
    passed = measurement.determinism_passed and not correctness_mismatches
    report: dict[str, object] = {
        "reportVersion": 1,
        "canonicalizationVersion": scenario.canonicalization_version,
        "fixture": {
            "id": fixture.fixture_id,
            "version": fixture.version,
            "manifestSha256": fixture.manifest_sha256,
        },
        "scenario": {"id": scenario.scenario_id, "version": scenario.version},
        "execution": {
            "cacheMode": cache_mode,
            "warmups": warmups,
            "iterations": iterations,
            "startedAt": _utc_text(started_at),
            "completedAt": _utc_text(completed_at),
            "runIdRule": "benchmark:<scenarioId>:<warmup|iteration>:<one-based-index>",
            "stageSumMayDifferFromTotal": True,
        },
        "environment": {
            "pythonVersion": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine() or None,
            "engineVersion": engine_version,
            "computeRevision": _git_revision(repository_root),
        },
        "samples": samples,
        "summary": summary,
        "correctness": {
            "terminalStatus": measurement.terminal_status,
            "tradeCount": measurement.trade_count,
            "sampleSizeWarning": measurement.sample_size_warning,
            "resultHash": measurement.reference_hash,
            "expectedResultChecked": expected is not None,
            "passed": passed,
            "mismatches": correctness_mismatches,
        },
    }
    return report, passed


def load_expected(path: Path, scenario_id: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Expected-result file is unavailable or invalid.") from error
    if not isinstance(value, dict):
        raise ValueError("Expected-result file must be a JSON object.")
    payload = cast(dict[str, object], value)
    scenarios = payload.get("scenarios")
    if isinstance(scenarios, list):
        matches: list[dict[str, object]] = []
        for item in cast(list[object], scenarios):
            if isinstance(item, dict):
                candidate = cast(dict[str, object], item)
                if candidate.get("scenarioId") == scenario_id:
                    matches.append(candidate)
        if len(matches) != 1:
            raise ValueError("Expected-result file must contain exactly one matching scenario.")
        return matches[0]
    return payload


def write_report_atomic(path: Path, report: dict[str, object]) -> None:
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, destination)
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def _sample_payload(sample: IterationResult) -> dict[str, object]:
    payload: dict[str, object] = {
        "iteration": sample.iteration,
        "status": sample.status,
        "totalNanoseconds": sample.total_nanoseconds,
        "stageNanoseconds": sample.stage_nanoseconds,
        "pythonPeakAllocatedBytes": sample.python_peak_allocated_bytes,
        "resultHash": sample.result_hash,
        "terminalStatus": sample.terminal_status,
        "tradeCount": sample.trade_count,
        "sampleSizeWarning": sample.sample_size_warning,
    }
    if sample.error_code is not None:
        payload["errorCode"] = sample.error_code
    return payload


def _expected_mismatches(
    fixture: FixtureInput,
    scenario: ScenarioInput,
    measurement: MeasurementResult,
    expected: dict[str, object],
) -> list[dict[str, object]]:
    actual = {
        "fixtureId": fixture.fixture_id,
        "scenarioId": scenario.scenario_id,
        "canonicalizationVersion": scenario.canonicalization_version,
        "terminalStatus": measurement.terminal_status,
        "tradeCount": measurement.trade_count,
        "sampleSizeWarning": measurement.sample_size_warning,
        "resultHash": measurement.reference_hash,
    }
    mismatches: list[dict[str, object]] = []
    for field, actual_value in actual.items():
        expected_value = expected.get(field)
        if expected_value != actual_value:
            mismatches.append({"field": field, "expected": expected_value, "actual": actual_value})
    return mismatches


def _integer_median(values: list[int]) -> int:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) // 2


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _git_revision(repository_root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    revision = completed.stdout.strip()
    return revision or None
