from datetime import UTC, datetime, timedelta
from multiprocessing.connection import Connection
from time import sleep

from refinvest.application.backtest_process_executor import ProcessIsolatedBacktestExecutor
from refinvest.application.backtest_worker_host import TerminalBacktestFailure
from refinvest.application.backtest_workers import ClaimedBacktestJob, ExecutableBacktestJob
from refinvest.application.dataset_snapshots import DatasetSnapshot


def _terminal_child(_: ExecutableBacktestJob, sender: Connection) -> None:
    sender.send(TerminalBacktestFailure("data corrupt", "DATASET_CORRUPTION"))
    sender.close()


def _blocking_child(_: ExecutableBacktestJob, sender: Connection) -> None:
    sleep(5)
    sender.close()


def _result_then_blocking_child(_: ExecutableBacktestJob, sender: Connection) -> None:
    sender.send(TerminalBacktestFailure("data corrupt", "DATASET_CORRUPTION"))
    sleep(5)
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
    outcome = ProcessIsolatedBacktestExecutor(timedelta(milliseconds=100), _blocking_child).execute(
        _job()
    )

    assert outcome == TerminalBacktestFailure(
        "Backtest execution exceeded the configured time limit.", "EXECUTION_TIMEOUT"
    )


def test_process_executor_times_out_when_child_does_not_exit_after_sending_result() -> None:
    outcome = ProcessIsolatedBacktestExecutor(
        timedelta(milliseconds=100), _result_then_blocking_child
    ).execute(_job())

    assert outcome == TerminalBacktestFailure(
        "Backtest execution exceeded the configured time limit.", "EXECUTION_TIMEOUT"
    )
