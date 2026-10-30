import multiprocessing
import pickle
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from multiprocessing.connection import Connection
from pathlib import Path
from time import sleep

import pytest

from refinvest.adapters.backtest.process_executor import (
    ProcessIsolatedBacktestExecutor,
    execute_backtest_child,
)
from refinvest.app.config import Settings
from refinvest.app.worker import create_backtest_worker
from refinvest.application.backtest_worker_host import (
    CompletedBacktestExecution,
    RetryableBacktestFailure,
    TerminalBacktestFailure,
)
from refinvest.application.backtest_workers import ClaimedBacktestJob, ExecutableBacktestJob
from refinvest.application.dataset_snapshots import DatasetSnapshot


def _terminal_child(_: ExecutableBacktestJob, sender: Connection) -> None:
    sender.send(TerminalBacktestFailure("data corrupt", "DATASET_CORRUPTION"))
    sender.close()


def _blocking_child(_: ExecutableBacktestJob, sender: Connection) -> None:
    sleep(5)
    sender.close()


def _result_then_blocking_child(job: ExecutableBacktestJob, sender: Connection) -> None:
    sender.send(TerminalBacktestFailure("data corrupt", "DATASET_CORRUPTION"))
    Path(job.claim.request_payload["sentMarker"]).write_text("sent")
    sleep(5)
    sender.close()


def _empty_child(_: ExecutableBacktestJob, sender: Connection) -> None:
    sender.close()


def _job() -> ExecutableBacktestJob:
    claim = ClaimedBacktestJob("run-1", "worker-1", "snapshot-1", 1, {})
    snapshot = DatasetSnapshot(
        "snapshot-1", datetime(2025, 1, 1, tzinfo=UTC), "test", {}, "adjusted", "path"
    )
    return ExecutableBacktestJob(claim, snapshot)


def test_process_executor_returns_child_outcome() -> None:
    outcome = ProcessIsolatedBacktestExecutor(timedelta(seconds=5), _terminal_child).execute(_job())

    assert outcome == TerminalBacktestFailure("data corrupt", "DATASET_CORRUPTION")


def test_process_executor_terminates_a_timed_out_child() -> None:
    before = {child.pid for child in multiprocessing.active_children()}
    outcome = ProcessIsolatedBacktestExecutor(timedelta(milliseconds=100), _blocking_child).execute(
        _job()
    )

    assert outcome == TerminalBacktestFailure(
        "Backtest execution exceeded the configured time limit.", "EXECUTION_TIMEOUT"
    )
    assert not ({child.pid for child in multiprocessing.active_children()} - before)


def test_process_executor_times_out_when_child_does_not_exit_after_sending_result(tmp_path) -> None:
    marker = tmp_path / "sent.txt"
    original = _job()
    job = replace(
        original, claim=replace(original.claim, request_payload={"sentMarker": str(marker)})
    )
    outcome = ProcessIsolatedBacktestExecutor(
        timedelta(seconds=3), _result_then_blocking_child
    ).execute(job)

    assert marker.read_text() == "sent"
    assert outcome == TerminalBacktestFailure(
        "Backtest execution exceeded the configured time limit.", "EXECUTION_TIMEOUT"
    )


def test_spawn_eof_retains_retryable_classification_and_joins_child():
    before = {child.pid for child in multiprocessing.active_children()}
    outcome = ProcessIsolatedBacktestExecutor(timedelta(seconds=20), _empty_child).execute(_job())
    assert outcome == RetryableBacktestFailure(
        "Backtest execution process exited without returning an outcome."
    )
    assert not ({child.pid for child in multiprocessing.active_children()} - before)


def test_composed_spawn_factory_is_picklable_and_runs_real_snapshot(tmp_path):
    from tests.integration.test_postgres_backtest_jobs import (
        _write_zero_trade_artifacts,
        _zero_trade_request_payload,
    )

    _write_zero_trade_artifacts(tmp_path, "snapshot-1")
    original = _job()
    job = replace(
        original,
        claim=replace(original.claim, request_payload=_zero_trade_request_payload("snapshot-1")),
        dataset_snapshot=replace(
            original.dataset_snapshot,
            storage_path=str(tmp_path),
            calendar_source="test-calendar",
            calendar_version="v1",
            calendar_hash="calendar-provenance-hash",
        ),
    )
    worker = create_backtest_worker(
        Settings(None, database_url="postgresql://unused", engine_version="spawn-test")
    )
    executor = worker._executor
    assert isinstance(executor, ProcessIsolatedBacktestExecutor)
    target = pickle.loads(pickle.dumps(executor._child_target))
    assert target.func is execute_backtest_child
    assert target.func.__module__ == "refinvest.adapters.backtest.process_executor"
    outcome = executor.execute(job)
    assert isinstance(outcome, CompletedBacktestExecution)
    assert outcome.engine_version == "spawn-test"
    assert outcome.result_payload["sampleSizeWarning"] == "ZERO"


@pytest.mark.parametrize("failure_at", ["factory", "send"])
def test_child_failure_returns_safe_retryable_outcome_and_closes_pipe(failure_at):
    class Executor:
        def execute(self, job):
            assert job == _job()
            return TerminalBacktestFailure("data corrupt", "DATASET_CORRUPTION")

    def factory():
        if failure_at == "factory":
            raise OSError("private storage configuration")
        return Executor()

    class Sender:
        closed = False
        calls = 0
        outcome = None

        def send(self, outcome):
            self.calls += 1
            if failure_at == "send" and self.calls == 1:
                raise OSError("private pipe detail")
            self.outcome = outcome

        def close(self):
            self.closed = True

    sender = Sender()
    execute_backtest_child(factory, _job(), sender)
    assert sender.closed
    assert sender.outcome == RetryableBacktestFailure(
        "Backtest execution process failed unexpectedly."
    )
    assert sender.calls == (2 if failure_at == "send" else 1)


def test_termination_escalates_after_existing_grace_period():
    calls = []

    class Process:
        def terminate(self):
            calls.append("terminate")

        def join(self, timeout=None):
            calls.append(("join", timeout))

        def is_alive(self):
            return True

        def kill(self):
            calls.append("kill")

    ProcessIsolatedBacktestExecutor._terminate(Process())
    assert calls == ["terminate", ("join", 30), "kill", ("join", None)]
