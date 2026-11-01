"""Metadata-only delivery queries retain expiry, ACK fencing and replay on PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pytest
from psycopg import connect

from refinvest.application.analytics_jobs import AnalyticsJobConflict, AnalyticsJobUnavailable
from tests.integration.test_postgres_trading_analytics_jobs import (
    DATABASE_URL,
    _create,
    _publication,
    _repo,
    _request,
)

pytestmark = pytest.mark.skipif(DATABASE_URL is None, reason="Isolated PostgreSQL test URL absent.")


def completed():
    repo = _repo()
    accepted, _ = _create(repo, _request(), str(uuid4()))
    claim = repo.claim_next("delivery-query-test")
    assert claim is not None and claim["jobId"] == accepted["jobId"]
    payload, body = _publication(claim)
    assert repo.publish_completed(claim, payload, body)
    descriptor = repo.result_descriptor(claim["jobId"])
    assert descriptor is not None
    ack = {
        "coreAnalysisRunId": claim["request"]["coreAnalysisRunId"],
        "payloadId": descriptor["payloadId"],
        "payloadSha256": descriptor["payloadSha256"],
        "terminalResultHash": descriptor["resultHash"],
        "attemptToken": claim["attemptToken"],
        "deletionGeneration": claim["deletionGeneration"],
        "disposition": "PERSISTED",
        "persistedAt": datetime.now(UTC).isoformat(),
    }
    return repo, str(claim["jobId"]), descriptor, body, ack


@pytest.mark.parametrize("operation", ["descriptor", "ack"])
def test_metadata_returns_no_bytea_but_payload_download_still_does(monkeypatch, operation):
    repo, job_id, descriptor, body, ack = completed()
    original = repo._connection
    returned_types = []

    @contextmanager
    def traced_connection():
        with original() as connection:

            class Connection:
                @contextmanager
                def cursor(self):
                    with connection.cursor() as cursor:

                        class Cursor:
                            def execute(self, query, parameters=None):
                                cursor.execute(query, parameters)
                                if cursor.description is not None:
                                    returned_types.extend(
                                        column.type_code for column in cursor.description
                                    )

                            def __getattr__(self, name):
                                return getattr(cursor, name)

                        yield Cursor()

            yield Connection()

    monkeypatch.setattr(repo, "_connection", traced_connection)
    assert repo.payload_bytes(str(descriptor["payloadId"])) == body
    assert 17 in returned_types  # PostgreSQL BYTEA type OID
    returned_types.clear()
    if operation == "descriptor":
        assert repo.result_descriptor(job_id) == descriptor
    else:
        repo.acknowledge(job_id, ack)
    assert returned_types and 17 not in returned_types
    repo.acknowledge(job_id, ack)
    assert repo.result_descriptor(job_id) is None


@pytest.mark.parametrize("storage", ["stored", "acked", "empty"])
@pytest.mark.parametrize("expired", [False, True])
def test_descriptor_presence_and_expiry_priority_are_preserved(storage, expired):
    repo, job_id, descriptor, _, ack = completed()
    if storage == "acked":
        repo.acknowledge(job_id, ack)
    elif storage == "empty":
        # Even a malformed zero-length BYTEA must retain the previous falsey presence result.
        with connect(DATABASE_URL) as connection:
            connection.execute(
                "UPDATE trading_analytics_payloads SET encrypted_bytes=%s WHERE job_id=%s",
                (b"", job_id),
            )
    if expired:
        with connect(DATABASE_URL) as connection:
            connection.execute(
                "UPDATE trading_analytics_payloads "
                "SET created_at=CURRENT_TIMESTAMP-INTERVAL '25 hours',"
                "expires_at=CURRENT_TIMESTAMP-INTERVAL '1 hour' WHERE job_id=%s",
                (job_id,),
            )
        with pytest.raises(AnalyticsJobUnavailable, match="RESULT_PAYLOAD_EXPIRED"):
            repo.result_descriptor(job_id)
        if storage == "acked":
            repo.acknowledge(job_id, ack)  # A durable matching receipt precedes expiry checks.
        else:
            with pytest.raises(AnalyticsJobUnavailable, match="RESULT_PAYLOAD_EXPIRED"):
                repo.acknowledge(job_id, ack)
    else:
        assert repo.result_descriptor(job_id) == (descriptor if storage == "stored" else None)


@pytest.mark.parametrize("conflicting", [False, True])
def test_concurrent_ack_replay_or_conflict_retains_one_receipt(conflicting):
    repo, job_id, descriptor, _, ack = completed()
    other = dict(ack)
    if conflicting:
        other.pop("persistedAt")
        other.update(
            disposition="DISCARDED_STALE",
            staleReason="DELETED_GENERATION",
            currentDeletionGeneration=ack["deletionGeneration"] + 1,
            discardedAt=datetime.now(UTC).isoformat(),
        )
    start = Barrier(2)

    def send(body):
        start.wait(timeout=5)
        try:
            repo.acknowledge(job_id, body)
        except AnalyticsJobConflict as error:
            return str(error)
        return "ACKED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(send, [ack, other]))
    assert sorted(outcomes) == (["ACKED", "ACK_CONFLICT"] if conflicting else ["ACKED", "ACKED"])
    with connect(DATABASE_URL) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM trading_analytics_ack_receipts WHERE job_id=%s", (job_id,)
            ).fetchone()[0]
            == 1
        )
    assert repo.payload_bytes(str(descriptor["payloadId"])) is None
    assert repo.result_descriptor(job_id) is None
