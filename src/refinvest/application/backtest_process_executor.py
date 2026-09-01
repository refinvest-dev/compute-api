"""Process-isolated execution boundary for one leased Backtest attempt."""

import logging
from collections.abc import Callable
from datetime import timedelta
from multiprocessing import get_context
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from time import monotonic
from typing import cast

from refinvest.application.backtest_worker_host import (
    BacktestExecutionOutcome,
    ExecutableBacktestJob,
    RetryableBacktestFailure,
    TerminalBacktestFailure,
)

_LOGGER = logging.getLogger(__name__)
_TERMINATION_GRACE_SECONDS = 30

ChildTarget = Callable[[ExecutableBacktestJob, Connection], None]


class ProcessIsolatedBacktestExecutor:
    """Run engine work outside the lease-owning worker process with a hard deadline."""

    def __init__(self, execution_timeout: timedelta, child_target: ChildTarget) -> None:
        if execution_timeout <= timedelta(0):
            raise ValueError("execution_timeout must be positive.")
        self._execution_timeout = execution_timeout
        self._child_target = child_target

    def execute(self, job: ExecutableBacktestJob) -> BacktestExecutionOutcome:
        context = get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=self._child_target, args=(job, sender))
        process.start()
        sender.close()
        deadline = monotonic() + self._execution_timeout.total_seconds()
        try:
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    self._terminate(process)
                    _LOGGER.warning(
                        "backtest_execution_timeout run_id=%s snapshot_id=%s attempt=%s "
                        "timeout_seconds=%s",
                        job.claim.run_id,
                        job.claim.dataset_snapshot_id,
                        job.claim.attempt_count,
                        self._execution_timeout.total_seconds(),
                    )
                    return TerminalBacktestFailure(
                        "Backtest execution exceeded the configured time limit.",
                        "EXECUTION_TIMEOUT",
                    )
                if receiver.poll(min(remaining, 0.1)):
                    try:
                        outcome = cast(BacktestExecutionOutcome, receiver.recv())
                    except EOFError:
                        process.join()
                        return RetryableBacktestFailure(
                            "Backtest execution process exited without returning an outcome."
                        )
                    if self._join_or_terminate(process, deadline):
                        _LOGGER.warning(
                            "backtest_execution_timeout run_id=%s snapshot_id=%s attempt=%s "
                            "timeout_seconds=%s",
                            job.claim.run_id,
                            job.claim.dataset_snapshot_id,
                            job.claim.attempt_count,
                            self._execution_timeout.total_seconds(),
                        )
                        return TerminalBacktestFailure(
                            "Backtest execution exceeded the configured time limit.",
                            "EXECUTION_TIMEOUT",
                        )
                    return outcome
                if not process.is_alive():
                    process.join()
                    return RetryableBacktestFailure(
                        "Backtest execution process exited without returning an outcome."
                    )
        finally:
            receiver.close()

    @staticmethod
    def _terminate(process: BaseProcess) -> None:
        process.terminate()
        process.join(_TERMINATION_GRACE_SECONDS)
        if process.is_alive():
            process.kill()
            process.join()

    @classmethod
    def _join_or_terminate(cls, process: BaseProcess, deadline: float) -> bool:
        process.join(max(0, deadline - monotonic()))
        if process.is_alive():
            cls._terminate(process)
            return True
        return False
