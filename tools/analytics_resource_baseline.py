"""Fresh-process Analytics measurements, with a benchmark-only wall-time watchdog.

No service entrypoint, DB, network, production executor or resource policy is changed.
Run from the repository with its virtualenv: python -m tools.analytics_resource_baseline --help.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from unittest.mock import patch

from tools.analytics_synthetic_inputs import ROLES, ROOT, synthetic_inputs


def process_memory() -> dict[str, int]:
    """OS process high-water marks; not Python allocations or a memory hard limit."""
    if sys.platform == "win32":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t)
                for name in (
                    "PeakWorkingSetSize",
                    "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage",
                    "PagefileUsage",
                    "PeakPagefileUsage",
                    "PrivateUsage",
                )
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(
            kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return {
            "peak_rss_bytes": counters.PeakWorkingSetSize,
            "rss_bytes": counters.WorkingSetSize,
            "peak_commit_bytes": counters.PeakPagefileUsage,
        }
    import resource

    # Linux reports KiB; Darwin reports bytes. Commit is intentionally not fabricated.
    factor = 1 if sys.platform == "darwin" else 1024
    return {"peak_rss_bytes": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * factor}


def measure(directory: Path, maximum: int) -> dict[str, Any]:
    from refinvest.adapters.analytics.artifacts import download_inputs
    from refinvest.application.analytics_input import adapt_inputs
    from refinvest.application.analytics_result import (
        bounded_analytics_json,
        build_analytics_payload,
        build_sections,
    )
    from refinvest.contracts.analytics.identity import AnalyticsContractError
    from refinvest.contracts.analytics.result_integrity import finish_sections
    from refinvest.persistence.postgres.analytics_payloads import (
        create_payload_cipher,
        encrypt_payload,
    )
    from refinvest.trading_review.analytics import calculate_behavior_metrics

    request = json.loads((directory / "request.json").read_text())
    phases: dict[str, Any] = {}

    def stage(name: str, operation: Any) -> Any:
        started = perf_counter()
        try:
            return operation()
        finally:
            phases[name] = {"seconds": perf_counter() - started, **process_memory()}

    class Response:
        status = 200

        def __init__(self, role: str) -> None:
            self.file = (directory / f"{role}.json").open("rb")
            self.headers = {
                "Content-Type": request[role]["mediaType"],
                "Content-Encoding": "identity",
            }

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_: object) -> None:
            self.file.close()

        def read(self, size: int) -> bytes:
            return self.file.read(size)

    class FileOpener:
        def open(self, requested: Any, timeout: int) -> Response:
            role = requested.full_url.rsplit("/", 1)[-1]
            if role not in ROLES:
                raise ValueError("unexpected synthetic artifact role")
            return Response(role)

    started = perf_counter()
    baseline = process_memory()
    # Only the transport is replaced. Production chunk/size/hash verification and
    # strict parsing remain in use, without retaining extra full input byte copies.
    with patch("refinvest.adapters.analytics.artifacts.build_opener", return_value=FileOpener()):
        ledger, manifest = stage(
            "read_verify_parse",
            lambda: download_inputs(
                request, request, ("synthetic.invalid",), lambda: False, 536870912
            ),
        )
    episodes, records, instruments, config = stage(
        "verify_adapt", lambda: adapt_inputs(request, ledger, manifest)
    )
    metrics = stage(
        "engine",
        lambda: calculate_behavior_metrics(
            episodes=episodes,
            canonical_records=records,
            instruments=instruments,
            analysis_config=config,
        ),
    )
    candidate_count = sum(len(relation.candidates) for relation in metrics[0].relations)
    sections = stage("projection", lambda: build_sections(metrics, ledger, manifest))
    digests = stage(
        "section_hashes",
        lambda: finish_sections(
            sections,
            input_hash=request["expectedInputHash"],
            config_hash=request["analysisConfigHash"],
            version_set=request["targetVersionSet"],
        ),
    )
    fixed_time = datetime(2025, 3, 1, tzinfo=UTC)
    claim = {
        "jobId": "synthetic-job",
        "attemptToken": request["attemptToken"],
        "deletionGeneration": 0,
        "createdAt": fixed_time,
    }
    payload = build_analytics_payload(claim, request, sections, digests, terminal_at=fixed_time)
    result: dict[str, Any] = {
        "input_hash": request["expectedInputHash"],
        "result_hash": digests["resultHash"],
        "input_bytes": {role: request[role]["byteSize"] for role in ROLES},
        "episode_count": len(episodes),
        "candidate_count": candidate_count,
        "metric_statuses": {item.definition_id: item.status.value for item in metrics},
        "section_counts": {name: len(rows) for name, rows in sections.items()},
        "baseline_memory": baseline,
        "phases": phases,
    }
    try:
        body = stage("serialization", lambda: bounded_analytics_json(payload, maximum))
    except AnalyticsContractError as error:
        if error.code != "RESULT_PAYLOAD_TOO_LARGE":
            raise
        result["status"] = error.code
    else:
        result.update(status="COMPLETED", payload_bytes=len(body))
        result["body_sha256"] = stage("body_hash", lambda: hashlib.sha256(body).hexdigest())
        cipher = create_payload_cipher(os.urandom(32))  # ephemeral synthetic key, never persisted
        encrypted = stage("encryption", lambda: encrypt_payload(cipher, "synthetic-job", body))
        result["ciphertext_bytes"] = len(encrypted)
    result.update(pipeline_seconds=perf_counter() - started, memory=process_memory())
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "measure", "run"))
    parser.add_argument(
        "directory", type=Path, help="local synthetic inputs/evidence; never commit"
    )
    parser.add_argument("--episodes", type=int, default=32)
    parser.add_argument("--spacing-seconds", type=int, default=2)
    parser.add_argument("--evidence-per-record", type=int, default=1)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--watchdog-seconds", type=float, default=60)
    parser.add_argument("--max-payload-bytes", type=int, default=64 * 1024 * 1024)
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 10 or not 0 < args.watchdog_seconds <= 600:
        parser.error("benchmark safety: repetitions 1..10, watchdog >0..600 seconds")
    if not 1 <= args.max_payload_bytes <= 512 * 1024 * 1024:
        parser.error("max payload must be 1..512MiB")
    directory = args.directory.resolve()
    if args.mode == "prepare":
        # Do not overwrite an existing run, accidentally mixing workload identities.
        directory.mkdir(parents=True, exist_ok=False)
        request, bodies = synthetic_inputs(
            args.episodes,
            spacing_seconds=args.spacing_seconds,
            evidence_per_record=args.evidence_per_record,
        )
        (directory / "request.json").write_text(json.dumps(request), encoding="utf-8")
        for role, body in bodies.items():
            (directory / f"{role}.json").write_bytes(body)
    elif args.mode == "measure":
        print(json.dumps(measure(directory, args.max_payload_bytes)))
    else:
        command = [sys.executable, "-m", "tools.analytics_resource_baseline"]
        options = [
            "--episodes",
            str(args.episodes),
            "--spacing-seconds",
            str(args.spacing_seconds),
            "--evidence-per-record",
            str(args.evidence_per_record),
        ]
        subprocess.run(
            command + ["prepare", str(directory), *options],
            check=True,
            timeout=args.watchdog_seconds,
        )
        samples = []
        for _ in range(args.repetitions):
            started = perf_counter()
            try:
                child = subprocess.run(
                    command
                    + [
                        "measure",
                        str(directory),
                        "--max-payload-bytes",
                        str(args.max_payload_bytes),
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=args.watchdog_seconds,
                )
            except subprocess.TimeoutExpired:
                samples.append(
                    {"status": "BENCHMARK_WATCHDOG", "process_seconds": perf_counter() - started}
                )
                break  # no production timeout or failure-code semantics are implied
            sample = json.loads(child.stdout)
            sample["process_seconds"] = perf_counter() - started
            samples.append(sample)
        source = hashlib.sha256()
        for path in sorted((ROOT / "src").rglob("*.py")):
            source.update(path.relative_to(ROOT).as_posix().encode() + b"\0" + path.read_bytes())
        hashes = {row.get("body_sha256") for row in samples}
        report = {
            "platform": platform.platform(),
            "python": sys.version,
            "processor": platform.processor(),
            "logical_cpus": os.cpu_count(),
            "pointer_bits": ctypes.sizeof(ctypes.c_void_p) * 8,
            "source_sha256": source.hexdigest(),
            "synthetic_version_set": True,
            "workload": vars(args) | {"directory": str(directory)},
            "samples": samples,
            "determinism_passed": len(samples) >= 2
            and hashes != {None}
            and len(hashes) == 1
            and all(row["status"] == "COMPLETED" for row in samples),
        }
        (directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "report": str(directory / "report.json"),
                    "determinism_passed": report["determinism_passed"],
                    "statuses": [row["status"] for row in samples],
                }
            )
        )


if __name__ == "__main__":
    main()
