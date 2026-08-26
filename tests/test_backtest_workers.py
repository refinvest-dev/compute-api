from datetime import UTC, datetime, timedelta

import pytest

from refinvest.application.backtest_workers import (
    MISSING_REQUIRED_DATA,
    ClaimedBacktestJob,
    claim_executable_backtest,
    record_transient_failure,
    retry_delay_for_attempt,
)
from refinvest.application.dataset_snapshots import DatasetSnapshot


class FakeSnapshots:
    def __init__(self, snapshots: list[DatasetSnapshot]) -> None:
        self.snapshots = snapshots

    def get(self, snapshot_id: str) -> DatasetSnapshot | None:
        return next(
            (snapshot for snapshot in self.snapshots if snapshot.snapshot_id == snapshot_id), None
        )

    def latest(self) -> DatasetSnapshot | None:
        return max(self.snapshots, key=lambda item: item.created_at, default=None)


class FakeWorkerJobs:
    def __init__(self, claim: ClaimedBacktestJob | None) -> None:
        self.claim = claim
        self.failed: list[tuple[str, str, str, str | None]] = []
        self.retries: list[tuple[str, str, timedelta, str]] = []

    def claim_next(self, worker_id: str, lease_duration: timedelta) -> ClaimedBacktestJob | None:
        assert lease_duration == timedelta(minutes=1)
        return self.claim

    def heartbeat(self, run_id: str, worker_id: str, lease_duration: timedelta) -> bool:
        return True

    def mark_failed(
        self, run_id: str, worker_id: str, failure_reason: str, error_code: str | None
    ) -> bool:
        self.failed.append((run_id, worker_id, failure_reason, error_code))
        return True

    def schedule_retry(
        self, run_id: str, worker_id: str, retry_delay: timedelta, failure_reason: str
    ) -> bool:
        self.retries.append((run_id, worker_id, retry_delay, failure_reason))
        return True


def snapshot(snapshot_id: str) -> DatasetSnapshot:
    return DatasetSnapshot(
        snapshot_id, datetime(2025, 1, 1, tzinfo=UTC), "vendor", {}, "adjusted", "path"
    )


def claim(dataset_snapshot_id: str = "pinned", attempt_count: int = 1) -> ClaimedBacktestJob:
    return ClaimedBacktestJob("run-1", "worker-1", dataset_snapshot_id, attempt_count, {})


def test_claim_loads_the_snapshot_pinned_in_the_atomic_claim() -> None:
    jobs = FakeWorkerJobs(claim("requested"))
    executable = claim_executable_backtest(
        "worker-1", timedelta(minutes=1), jobs, FakeSnapshots([snapshot("requested")])
    )
    assert executable is not None
    assert executable.dataset_snapshot.snapshot_id == "requested"


def test_claim_reuses_the_same_pinned_snapshot_on_retry() -> None:
    jobs = FakeWorkerJobs(claim(dataset_snapshot_id="pinned", attempt_count=2))
    executable = claim_executable_backtest(
        "worker-1", timedelta(minutes=1), jobs, FakeSnapshots([snapshot("pinned")])
    )
    assert executable is not None
    assert executable.dataset_snapshot.snapshot_id == "pinned"


def test_missing_pinned_snapshot_is_terminal_and_not_retried() -> None:
    jobs = FakeWorkerJobs(claim())
    assert (
        claim_executable_backtest("worker-1", timedelta(minutes=1), jobs, FakeSnapshots([]))
        is None
    )
    assert jobs.failed == [
        (
            "run-1",
            "worker-1",
            "The pinned DatasetSnapshot is unavailable.",
            MISSING_REQUIRED_DATA,
        )
    ]


@pytest.mark.parametrize(
    ("attempt_count", "expected"),
    [(1, timedelta(seconds=30)), (2, timedelta(minutes=2)), (3, timedelta(minutes=10)), (4, None)],
)
def test_retry_delays_are_bounded(attempt_count: int, expected: timedelta | None) -> None:
    assert retry_delay_for_attempt(attempt_count) == expected


def test_transient_failure_releases_claim_for_retry() -> None:
    jobs = FakeWorkerJobs(claim(attempt_count=2))
    assert record_transient_failure(claim(attempt_count=2), "database unavailable", jobs)
    assert jobs.retries == [("run-1", "worker-1", timedelta(minutes=2), "database unavailable")]
    assert jobs.failed == []


def test_transient_failure_after_retry_budget_is_terminal() -> None:
    jobs = FakeWorkerJobs(claim(attempt_count=4))
    assert record_transient_failure(claim(attempt_count=4), "database unavailable", jobs)
    assert jobs.retries == []
    assert jobs.failed == [("run-1", "worker-1", "database unavailable", None)]
