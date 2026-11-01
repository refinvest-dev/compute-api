"""Development-only Analytics publication measurements on an owned integration schema.

Prepare synthetic sections separately; measure the unchanged repository in a fresh process.
No Engine graph, HTTP/Core delivery, production policy or whole-worker RSS is measured.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from time import perf_counter, sleep, time
from typing import Any
from uuid import uuid4

from psycopg import connect
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from refinvest.application.analytics_input import adapt_inputs
from refinvest.application.analytics_result import (
    bounded_analytics_json,
    build_analytics_payload,
    build_sections,
)
from refinvest.application.trading_review_jobs import hash_idempotency_key
from refinvest.contracts.analytics.identity import canonical_bytes, create_fingerprint, strict_json
from refinvest.contracts.analytics.result_integrity import finish_sections
from refinvest.persistence.postgres.trading_analytics_jobs import PostgresAnalyticsJobRepository
from refinvest.trading_review.analytics import calculate_behavior_metrics
from tools.analytics_resource_baseline import process_memory
from tools.analytics_synthetic_inputs import ROLES, synthetic_inputs

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
MAXIMUM = 64 * 1024 * 1024  # benchmark protection, not a production recommendation


def prepare(directory: Path, episodes: int) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    request, bodies = synthetic_inputs(episodes)
    ledger, manifest = (strict_json(bodies[role]) for role in ROLES)
    units, records, instruments, config = adapt_inputs(request, ledger, manifest)
    metrics = calculate_behavior_metrics(
        episodes=units, canonical_records=records, instruments=instruments, analysis_config=config
    )
    sections = build_sections(metrics, ledger, manifest)
    digests = finish_sections(
        sections,
        input_hash=request["expectedInputHash"],
        config_hash=request["analysisConfigHash"],
        version_set=request["targetVersionSet"],
    )
    document = canonical_bytes({"request": request, "sections": sections, "digests": digests})
    (directory / "prepared.json").write_bytes(document)
    (directory / "identity.json").write_text(
        json.dumps({"episodes": episodes, "sha256": hashlib.sha256(document).hexdigest()}),
        encoding="utf-8",
    )


def local_connection(database_url: str, label: str) -> tuple[str, str]:
    """Reject production/public/service/multihost targets before opening a connection."""
    params = conninfo_to_dict(database_url)
    host = params.get("host", os.getenv("PGHOST", ""))
    hostaddr = params.get("hostaddr", os.getenv("PGHOSTADDR", ""))
    database = params.get("dbname", os.getenv("PGDATABASE", ""))
    options = params.get("options", os.getenv("PGOPTIONS", ""))
    match = re.fullmatch(r"-c search_path=(ar08_pub_[a-z0-9_]{1,45})", options)
    if (
        host not in LOCAL_HOSTS
        or (hostaddr and hostaddr not in {"127.0.0.1", "::1"})
        or database != "refinvest_integration"
        or match is None
        or params.get("service")
        or os.getenv("PGSERVICE")
    ):
        raise ValueError("Use a local refinvest_integration DB and an owned ar08_pub_ schema.")
    # Freeze the validated target. Bound DB waits independently of any external watchdog.
    return make_conninfo(
        database_url,
        host=host,
        hostaddr=hostaddr,
        dbname=database,
        options=options + " -c statement_timeout=15000 -c lock_timeout=10000",
        connect_timeout="5",
        application_name=label,
    ), match[1]


def validate_database(database_url: str, schema: str) -> dict[str, Any]:
    with connect(database_url) as connection:
        row = connection.execute(
            "SELECT current_database(),current_schema(),host(inet_server_addr()),version()"
        ).fetchone()
        if (
            row is None
            or row[:2] != ("refinvest_integration", schema)
            or row[2] not in {"127.0.0.1", "::1"}
        ):
            raise ValueError("Connected database is outside the owned local target.")
        # One fresh schema per sample/client; do not claim or mutate another job.
        if connection.execute("SELECT count(*) FROM trading_analytics_jobs").fetchone()[0]:
            raise ValueError("Publication measurements require a fresh empty schema.")
        return {"database": row[0], "schema": schema, "server_version": row[3]}


def hold_publication_lock(
    database_url: str, job_id: str, label: str, seconds: float, ready: Event
) -> dict[str, Any]:
    """Release only after observing this client's actual DB lock wait, then hold N seconds."""
    with connect(database_url) as locker, connect(database_url, autocommit=True) as observer:
        locker.execute(
            "SELECT job_id FROM trading_analytics_jobs WHERE job_id=%s FOR UPDATE", (job_id,)
        )
        locker_pid = locker.info.backend_pid
        ready.set()
        deadline = perf_counter() + 8
        while perf_counter() < deadline:
            waiter = observer.execute(
                "SELECT pid FROM pg_stat_activity WHERE application_name=%s "
                "AND wait_event_type='Lock' AND %s=ANY(pg_blocking_pids(pid))",
                (label, locker_pid),
            ).fetchone()
            if waiter is not None:
                break
            sleep(0.01)
        else:
            raise TimeoutError("Publication lock wait was not observed.")
        started = perf_counter()
        sleep(seconds)
        state = observer.execute(
            "SELECT status,(SELECT count(*) FROM trading_analytics_payloads WHERE job_id=%s) "
            "FROM trading_analytics_jobs WHERE job_id=%s",
            (job_id, job_id),
        ).fetchone()
        if state != ("RUNNING", 0):
            raise AssertionError("Terminal state became visible before publication lock release.")
        held = perf_counter() - started
    return {"wait_observed": True, "hold_after_observation_seconds": held, "precommit": "RUNNING"}


def measure(
    directory: Path, *, lock_seconds: float = 0, start_at: float | None = None
) -> dict[str, Any]:
    if not 0 <= lock_seconds <= 3:
        raise ValueError("Benchmark lock hold must be 0..3 seconds.")
    raw = (directory / "prepared.json").read_bytes()
    identity = json.loads((directory / "identity.json").read_text(encoding="utf-8"))
    if hashlib.sha256(raw).hexdigest() != identity["sha256"]:
        raise ValueError("Prepared synthetic document digest mismatch.")
    prepared = strict_json(raw)
    del raw
    request: Any = prepared["request"]
    request.update(
        coreLogicalJobId=str(uuid4()), coreAnalysisRunId=str(uuid4()), attemptToken=str(uuid4())
    )
    for role in ROLES:
        request[role]["expiresAt"] = (datetime.now(UTC) + timedelta(minutes=4)).isoformat()
    label = "ar08-pub-" + uuid4().hex
    database_url, schema = local_connection(os.environ["REFINVEST_TEST_DATABASE_URL"], label)
    target = validate_database(database_url, schema)
    repo = PostgresAnalyticsJobRepository(
        database_url, os.urandom(32), active_job_limit=10, max_payload_bytes=MAXIMUM
    )
    status, created = repo.create(
        request, hash_idempotency_key(str(uuid4())), create_fingerprint(request)
    )
    claim = repo.claim_next(label)
    if not created or claim is None or claim["jobId"] != status["jobId"]:
        raise AssertionError("Fresh synthetic claim was not acquired.")
    job_id = str(claim["jobId"])
    payload = build_analytics_payload(
        claim, request, prepared["sections"], prepared["digests"], terminal_at=datetime.now(UTC)
    )
    body = bounded_analytics_json(payload, MAXIMUM)
    digest = hashlib.sha256(body).hexdigest()
    result: dict[str, Any] = {
        "target": target,
        "episodes": identity["episodes"],
        "prepared_sha256": identity["sha256"],
        "input_hash": payload["inputHash"],
        "result_hash": payload["resultHash"],
        "payload_bytes": len(body),
        "body_sha256": digest,
        "phases": {},
        "baseline_memory": process_memory(),
    }

    def stage(name: str, operation: Any) -> Any:
        started = perf_counter()
        value = operation()
        result["phases"][name] = {"seconds": perf_counter() - started, **process_memory()}
        return value

    if start_at is not None:
        delay = start_at - time()
        if not 0 < delay <= 30:
            raise ValueError("Publication rendezvous missed or more than 30 seconds away.")
        sleep(delay)
    result["publication_started_at_epoch"] = time()
    if lock_seconds:
        ready = Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            holder = pool.submit(
                hold_publication_lock, database_url, job_id, label, lock_seconds, ready
            )
            if not ready.wait(6):
                raise TimeoutError("Lock holder was not ready.")
            published = stage(
                "publish_commit", lambda: repo.publish_completed(claim, payload, body)
            )
            result["lock"] = holder.result(timeout=12)
    else:
        published = stage("publish_commit", lambda: repo.publish_completed(claim, payload, body))
    result["publication_finished_at_epoch"] = time()
    if not published:
        raise AssertionError("Publication was fenced out.")
    descriptor = stage("descriptor", lambda: repo.result_descriptor(job_id))
    if (
        descriptor is None
        or descriptor["byteSize"] != len(body)
        or descriptor["payloadSha256"] != digest
    ):
        raise AssertionError("Descriptor does not match committed body.")
    readback = stage(
        "payload_read_decrypt", lambda: repo.payload_bytes(str(descriptor["payloadId"]))
    )
    if readback != body or hashlib.sha256(readback).hexdigest() != digest:
        raise AssertionError("Stored payload roundtrip failed.")
    del readback
    with connect(database_url) as connection:
        state = connection.execute(
            "SELECT j.status,a.outcome,octet_length(p.encrypted_bytes),p.payload_sha256 "
            "FROM trading_analytics_jobs j JOIN trading_analytics_attempts a USING(job_id) "
            "JOIN trading_analytics_payloads p USING(job_id) WHERE j.job_id=%s",
            (job_id,),
        ).fetchone()
        if state != ("COMPLETED", "COMPLETED", len(body) + 28, digest):
            raise AssertionError("Committed publication rows are inconsistent.")
    ack = {
        "coreAnalysisRunId": request["coreAnalysisRunId"],
        "payloadId": descriptor["payloadId"],
        "payloadSha256": digest,
        "terminalResultHash": descriptor["resultHash"],
        "attemptToken": claim["attemptToken"],
        "deletionGeneration": claim["deletionGeneration"],
        "disposition": "PERSISTED",
        "persistedAt": datetime.now(UTC).isoformat(),
    }
    stage("ack_commit", lambda: repo.acknowledge(job_id, ack))
    if (
        repo.result_descriptor(job_id) is not None
        or repo.payload_bytes(str(descriptor["payloadId"])) is not None
    ):
        raise AssertionError("ACK did not clear payload delivery.")
    with connect(database_url) as connection:
        receipt = connection.execute(
            "SELECT disposition FROM trading_analytics_ack_receipts WHERE job_id=%s", (job_id,)
        ).fetchone()
        if receipt != ("PERSISTED",):
            raise AssertionError("ACK receipt was not committed.")
    result.update(
        status="PUBLICATION_VERIFIED", ciphertext_bytes=len(body) + 28, memory=process_memory()
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "measure"))
    parser.add_argument("directory", type=Path, help="synthetic evidence outside the repository")
    parser.add_argument("--episodes", type=int, default=32)
    parser.add_argument("--lock-seconds", type=float, default=0)
    parser.add_argument("--start-at", type=float, help="epoch rendezvous for two fresh clients")
    args = parser.parse_args()
    if not 4 <= args.episodes <= 512 or not 0 <= args.lock_seconds <= 3:
        parser.error("benchmark protections: episodes 4..512, lock hold 0..3 seconds")
    try:
        if args.mode == "prepare":
            prepare(args.directory.resolve(), args.episodes)
        else:
            print(
                json.dumps(
                    measure(args.directory, lock_seconds=args.lock_seconds, start_at=args.start_at)
                )
            )
    except Exception as error:
        # Connection exceptions can contain credentials. Emit only a safe error class.
        print(json.dumps({"status": "BENCHMARK_FAILED", "error_type": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
