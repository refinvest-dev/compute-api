"""Local HTTP input-size/trickle probes; no worker or production policy is changed."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from time import perf_counter
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

from refinvest.adapters.analytics.artifacts import download_inputs
from refinvest.application.analytics_input import adapt_inputs
from refinvest.contracts.analytics.identity import (
    AnalyticsContractError,
    analysis_input_hash,
    canonical_bytes,
    validate_create_identity,
)
from tools.analytics_resource_baseline import process_memory, run_child
from tools.analytics_synthetic_inputs import ROLES, ROOT, synthetic_inputs

EXPERIMENT_MAX_BYTES = 512 * 1024 * 1024


def prepare_inputs(directory: Path, artifact_bytes: int) -> None:
    """Pad valid JSON with legal whitespace, without building large generator objects."""
    request, bodies = synthetic_inputs(16)
    if not max(map(len, bodies.values())) <= artifact_bytes <= EXPERIMENT_MAX_BYTES:
        raise ValueError("Artifact size must fit both documents and be at most 512MiB.")
    directory.mkdir(parents=True, exist_ok=False)
    for role, body in bodies.items():
        digest = hashlib.sha256(body)
        remaining = artifact_bytes - len(body)
        with (directory / f"{role}.json").open("xb") as stream:
            stream.write(body)
            while remaining:
                padding = b" " * min(65536, remaining)
                stream.write(padding)
                digest.update(padding)
                remaining -= len(padding)
        for reference in (request[role], request["inputIdentity"][role]):
            reference.update(byteSize=artifact_bytes, sha256=digest.hexdigest())
    request["expectedInputHash"] = analysis_input_hash(request["inputIdentity"])
    validate_create_identity(request)
    (directory / "request.json").write_text(json.dumps(request), encoding="utf-8")


@contextmanager
def artifact_server(directory: Path, chunk_bytes: int, delay_seconds: float):
    """Serve only two synthetic files, on an ephemeral IPv4 loopback listener."""
    request = json.loads((directory / "request.json").read_text(encoding="utf-8"))
    stop = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            role = self.path.removeprefix("/")
            if role not in ROLES:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", request[role]["mediaType"])
            self.send_header("Content-Length", str(request[role]["byteSize"]))
            self.send_header("Content-Encoding", "identity")
            self.end_headers()
            try:
                with (directory / f"{role}.json").open("rb") as stream:
                    while not stop.is_set() and (chunk := stream.read(chunk_bytes)):
                        self.wfile.write(chunk)
                        if delay_seconds and stop.wait(delay_seconds):
                            break
            except (BrokenPipeError, ConnectionResetError):
                pass  # Expected when the real client rejects expiry/size/hash or is stopped.

        def log_message(self, *_: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, name="synthetic-artifact-http", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError("Synthetic HTTP server did not stop.")


def measure(directory: Path, base_url: str, grant_seconds: float, maximum: int) -> dict[str, Any]:
    """Measure the unchanged downloader, strict parser and adapter, with real HTTP."""
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or not parsed.port
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Only the probe's IPv4 loopback HTTP server is allowed.")
    request = json.loads((directory / "request.json").read_text(encoding="utf-8"))
    validate_create_identity(request)
    expiry = (datetime.now(UTC) + timedelta(seconds=grant_seconds)).isoformat()
    grants = {role: {"accessUri": f"{base_url}/{role}", "expiresAt": expiry} for role in ROLES}
    result: dict[str, Any] = {
        "input_hash": request["expectedInputHash"],
        "input_bytes": {role: request[role]["byteSize"] for role in ROLES},
        "baseline_memory": process_memory(),
    }
    started = perf_counter()
    try:
        # Environment-only loopback proxy bypass; the HTTP opener/reader is not replaced.
        with patch.dict(os.environ, {"no_proxy": "127.0.0.1"}):
            ledger, manifest = download_inputs(
                request, grants, (parsed.netloc,), lambda: False, maximum
            )
        result["download_parse_seconds"] = perf_counter() - started
        result["download_memory"] = process_memory()
        adapted = perf_counter()
        episodes, records, _, _ = adapt_inputs(request, ledger, manifest)
        result.update(
            status="INPUTS_VERIFIED",
            adapt_seconds=perf_counter() - adapted,
            episode_count=len(episodes),
            record_count=len(records),
            document_sha256={
                role: hashlib.sha256(canonical_bytes(document)).hexdigest()
                for role, document in zip(ROLES, (ledger, manifest), strict=True)
            },
        )
    except AnalyticsContractError as error:
        result.update(status=error.code, retryable=getattr(error, "retryable", False))
    except MemoryError:
        result["status"] = "BENCHMARK_MEMORY_ERROR"
    result.update(total_seconds=perf_counter() - started, memory=process_memory())
    return result


def run(args: argparse.Namespace) -> dict[str, Any]:
    directory = args.directory.resolve()
    command = [sys.executable, "-m", "tools.analytics_http_baseline"]
    run_child(
        command + ["prepare", str(directory), "--artifact-bytes", str(args.artifact_bytes)],
        args.watchdog_seconds,
    )
    samples = []
    # Server/generator memory is outside fresh measurement children.
    with artifact_server(directory, args.send_chunk_bytes, args.chunk_delay_seconds) as base_url:
        for _ in range(args.repetitions):
            started = perf_counter()
            try:
                child = run_child(
                    command
                    + [
                        "measure",
                        str(directory),
                        "--base-url",
                        base_url,
                        "--grant-seconds",
                        str(args.grant_seconds),
                        "--max-input-bytes",
                        str(args.max_input_bytes),
                    ],
                    args.watchdog_seconds,
                )
            except subprocess.TimeoutExpired:
                samples.append(
                    {"status": "BENCHMARK_WATCHDOG", "process_seconds": perf_counter() - started}
                )
                break
            except subprocess.CalledProcessError as error:
                (directory / f"sample-{len(samples)}-stderr.log").write_text(
                    error.stderr or "", encoding="utf-8"
                )
                samples.append(
                    {
                        "status": "BENCHMARK_PROCESS_FAILURE",
                        "returncode": error.returncode,
                        "process_seconds": perf_counter() - started,
                    }
                )
                break
            sample = json.loads(child.stdout)
            sample["process_seconds"] = perf_counter() - started
            samples.append(sample)
    source = hashlib.sha256()
    for path in sorted((ROOT / "src").rglob("*.py")):
        source.update(path.relative_to(ROOT).as_posix().encode() + b"\0" + path.read_bytes())
    signatures = {
        json.dumps([sample.get("input_hash"), sample.get("document_sha256")], sort_keys=True)
        for sample in samples
    }
    report = {
        "platform": platform.platform(),
        "python": sys.version,
        "source_sha256": source.hexdigest(),
        "tool_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "synthetic_version_set": True,
        "transport": "loopback HTTP, no TLS",
        "workload": vars(args) | {"directory": str(directory)},
        "samples": samples,
        "determinism_passed": len(samples) >= 2
        and len(signatures) == 1
        and all(sample["status"] == "INPUTS_VERIFIED" for sample in samples),
    }
    (directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "measure", "run"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--artifact-bytes", type=int, default=1024 * 1024)
    parser.add_argument("--send-chunk-bytes", type=int, default=65536)
    parser.add_argument("--chunk-delay-seconds", type=float, default=0)
    parser.add_argument("--grant-seconds", type=float, default=300)
    parser.add_argument("--max-input-bytes", type=int, default=EXPERIMENT_MAX_BYTES)
    parser.add_argument("--watchdog-seconds", type=float, default=60)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--base-url", default="")
    args = parser.parse_args()
    if not (
        1 <= args.artifact_bytes <= EXPERIMENT_MAX_BYTES
        and 1 <= args.max_input_bytes <= EXPERIMENT_MAX_BYTES
        and 1 <= args.send_chunk_bytes <= 65536
        and math.isfinite(args.chunk_delay_seconds)
        and 0 <= args.chunk_delay_seconds <= 1
        and math.isfinite(args.grant_seconds)
        and 0 <= args.grant_seconds <= 300
        and math.isfinite(args.watchdog_seconds)
        and 0 < args.watchdog_seconds <= 600
        and 1 <= args.repetitions <= 10
    ):
        parser.error(
            "Invalid experiment bounds; these are benchmark safeguards, not runtime policy."
        )
    if args.mode == "prepare":
        prepare_inputs(args.directory.resolve(), args.artifact_bytes)
    elif args.mode == "measure":
        print(
            json.dumps(
                measure(args.directory, args.base_url, args.grant_seconds, args.max_input_bytes)
            )
        )
    else:
        report = run(args)
        print(
            json.dumps(
                {
                    "report": str(args.directory / "report.json"),
                    "statuses": [sample["status"] for sample in report["samples"]],
                    "determinism_passed": report["determinism_passed"],
                }
            )
        )


if __name__ == "__main__":
    main()
