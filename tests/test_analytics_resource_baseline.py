"""Workloads must exercise successful metrics and real integrity checks."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from refinvest.application.analytics_input import AnalyticsInputFailure, adapt_inputs
from refinvest.application.analytics_result import build_analytics_payload
from tools.analytics_resource_baseline import measure, run_child
from tools.analytics_synthetic_inputs import ROLES, synthetic_inputs


def write_inputs(directory: Path, request: dict[str, Any], bodies: dict[str, bytes]) -> None:
    (directory / "request.json").write_text(json.dumps(request), encoding="utf-8")
    for role, body in bodies.items():
        (directory / f"{role}.json").write_bytes(body)


@pytest.mark.parametrize(("spacing", "fanout"), [(2, 1), (3600, 1), (2, 8)])
def test_workloads_are_valid_deterministic_and_have_expected_relations(
    tmp_path: Path, spacing: int, fanout: int
) -> None:
    from tests.test_trading_analytics_runtime import _validate

    request, bodies = synthetic_inputs(16, spacing_seconds=spacing, evidence_per_record=fanout)
    assert (request, bodies) == synthetic_inputs(
        16, spacing_seconds=spacing, evidence_per_record=fanout
    )
    _validate("CreateTradingAnalyticsJobRequest", request)
    ledger, manifest = (json.loads(bodies[role]) for role in ROLES)
    _validate("AnalyticsLedgerInputV1", ledger)
    _validate("ReconciliationManifestPayload", manifest)
    episodes, records, _, _ = adapt_inputs(request, ledger, manifest)
    assert len(episodes) == 16 and len(records) == 32
    assert len({record.record_id for record in records}) == 32
    assert all(len(record.provenance) == fanout for record in records)
    write_inputs(tmp_path, request, bodies)

    def validated_payload(*args: Any, **kwargs: Any) -> dict[str, object]:
        payload = build_analytics_payload(*args, **kwargs)
        _validate("AnalyticsTerminalPayload", payload)
        return payload

    with patch("refinvest.application.analytics_result.build_analytics_payload", validated_payload):
        first, second = (measure(tmp_path, 10_000_000) for _ in range(2))
    assert first["body_sha256"] == second["body_sha256"]
    assert first["status"] == "COMPLETED"
    assert first["candidate_count"] == 64  # 15 + 13 + ... + 1, including AFTER_WINDOW
    statuses = first["metric_statuses"]
    assert statuses["ENTRY_WITHIN_WINDOW_AFTER_LOSS"] == "AVAILABLE"
    assert statuses["WIN_LOSS_HOLDING_DURATION_DIFFERENCE"] == "AVAILABLE"
    assert statuses["INITIAL_EXPOSURE_CHANGE_AFTER_LOSS"] == (
        "AVAILABLE" if spacing == 2 else "INSUFFICIENT_SAMPLE"
    )
    assert first["ciphertext_bytes"] == first["payload_bytes"] + 28
    assert first["memory"]["peak_rss_bytes"] > 0
    assert set(first["phases"]) == {
        "read_verify_parse",
        "verify_adapt",
        "engine",
        "projection",
        "section_hashes",
        "serialization",
        "body_hash",
        "encryption",
    }


def test_generated_input_tampering_fails_real_verifier(tmp_path: Path) -> None:
    request, bodies = synthetic_inputs(8)
    ledger, manifest = (json.loads(bodies[role]) for role in ROLES)
    changed = copy.deepcopy(ledger)
    changed["canonicalRecords"][0]["price"]["value"] = "999"
    with pytest.raises(AnalyticsInputFailure, match="INPUT_HASH_MISMATCH"):
        adapt_inputs(request, changed, manifest)
    changed = copy.deepcopy(ledger)
    changed["reviewUnitEngineInputs"][0]["evidenceRecordIds"] = ["missing-record"]
    with pytest.raises(AnalyticsInputFailure, match="EVIDENCE_REFERENCE_INVALID"):
        adapt_inputs(request, changed, manifest)
    write_inputs(tmp_path, request, bodies)
    result = measure(tmp_path, 100)
    assert result["status"] == "RESULT_PAYLOAD_TOO_LARGE"
    assert "encryption" not in result["phases"]
    assert "body_sha256" not in result
    assert hashlib.sha256(bodies[ROLES[0]]).hexdigest() == request[ROLES[0]]["sha256"]


def test_benchmark_watchdog_stops_descendants(tmp_path: Path) -> None:
    ready, survived = tmp_path / "ready", tmp_path / "survived"
    descendant = (
        "import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); "
        "time.sleep(3); pathlib.Path(sys.argv[2]).touch(); time.sleep(30)"
    )
    program = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2],sys.argv[3]]); "
        "time.sleep(30)"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        run_child([sys.executable, "-c", program, descendant, str(ready), str(survived)], 2)
    assert ready.exists(), "the descendant must have started for cleanup to be tested"
    time.sleep(2)
    assert not survived.exists(), "a benchmark descendant survived its watchdog"
