"""Parent-only lease checks and metrics/publication boundaries through ports."""

import importlib
from datetime import timedelta

from refinvest.application.normalization_execution import (
    CompletedNormalization,
    FailedNormalization,
)
from refinvest.application.normalization_worker import TradingReviewWorkerHost
from refinvest.application.trading_review_jobs import SafeTradingReviewFailure
from tests.test_trading_review_worker_runtime import Executor, Jobs, _claim


def _host(repo, executor, metrics=None):
    return TradingReviewWorkerHost(
        repo,
        executor,
        worker_id="worker",
        lease_duration=timedelta(seconds=60),
        heartbeat_interval=timedelta(seconds=15),
        retention=timedelta(hours=24),
        metrics=metrics,
    )


def test_lost_heartbeat_stops_execution_and_does_not_publish(monkeypatch):
    worker_module = importlib.import_module("refinvest.application.normalization_worker")
    times = iter([0, 16])
    monkeypatch.setattr(worker_module, "monotonic", lambda: next(times))
    claim = _claim()
    calls = []

    class Repo(Jobs):
        def cancellation_requested(self, *args):
            calls.append(("cancellation", args))
            return False

        def heartbeat(self, *args):
            calls.append(("heartbeat", args))
            return False

    class StoppingExecutor:
        def execute(self, job, should_stop):
            assert should_stop()
            return FailedNormalization(
                SafeTradingReviewFailure("JOB_CANCELLED", "stopped", False, "CANCELLATION"), False
            )

    repo = Repo(claim)
    assert _host(repo, StoppingExecutor()).run_once()
    assert repo.transition == "CANCELLED"
    assert calls == [
        (
            "cancellation",
            (claim.job_id, claim.lease_owner, claim.attempt_token, claim.deletion_generation),
        ),
        (
            "heartbeat",
            (
                claim.job_id,
                claim.lease_owner,
                claim.attempt_token,
                claim.deletion_generation,
                timedelta(seconds=60),
            ),
        ),
    ]


def test_requested_cancellation_is_checked_before_heartbeat(monkeypatch):
    worker_module = importlib.import_module("refinvest.application.normalization_worker")
    monkeypatch.setattr(worker_module, "monotonic", lambda: 0)
    calls = []

    class Repo(Jobs):
        def cancellation_requested(self, *args):
            calls.append("cancel")
            return True

        def heartbeat(self, *args):
            raise AssertionError("heartbeat must not follow cancellation")

    class StoppingExecutor:
        def execute(self, job, should_stop):
            assert should_stop()
            return FailedNormalization(
                SafeTradingReviewFailure("JOB_CANCELLED", "stopped", False, "CANCELLATION"), False
            )

    repo = Repo(_claim())
    assert _host(repo, StoppingExecutor()).run_once()
    assert repo.transition == "CANCELLED" and calls == ["cancel"]


def test_broken_metrics_cannot_turn_successful_publication_into_retry():
    class BrokenMetrics:
        def __getattr__(self, name):
            def fail(*args):
                raise RuntimeError("metrics unavailable")

            return fail

    repo = Jobs(_claim())
    outcome = CompletedNormalization(
        b"{}", "application/json", "a" * 64, "b" * 64, "c" * 64, "d" * 64
    )
    assert _host(repo, Executor(outcome), BrokenMetrics()).run_once()
    assert repo.transition == "COMPLETED"


def test_rejected_stale_publication_does_not_record_committed_terminal_metrics():
    terminal = []

    class Metrics:
        def claimed(self):
            pass

        def queue_wait(self, seconds):
            pass

        def terminal(self, status, seconds):
            terminal.append(status)

    class Repo(Jobs):
        def publish_completed(self, claim, **fields):
            assert fields["retention"] == timedelta(hours=24)
            return False

    repo = Repo(_claim())
    outcome = CompletedNormalization(
        b"{}", "application/json", "a" * 64, "b" * 64, "c" * 64, "d" * 64
    )
    assert _host(repo, Executor(outcome), Metrics()).run_once()
    assert repo.transition == "" and terminal == []
