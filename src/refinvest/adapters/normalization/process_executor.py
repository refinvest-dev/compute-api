"""Spawn/IPC and bounded termination; durable state remains in the parent worker."""

from __future__ import annotations

import multiprocessing
from collections.abc import Callable
from datetime import timedelta
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from time import monotonic
from typing import cast

from refinvest.adapters.normalization.artifacts import download_normalization_artifact
from refinvest.application.normalization_execution import (
    FailedNormalization,
    NormalizationExecutionSettings,
    NormalizationOutcome,
    execute_normalization,
    normalization_failure,
)
from refinvest.application.trading_review_jobs import (
    ClaimedTradingReviewJob,
    SafeTradingReviewFailure,
)


def execute_normalization_child(
    settings: NormalizationExecutionSettings, claim: ClaimedTradingReviewJob, sender: Connection
) -> None:
    try:
        sender.send(execute_normalization(settings, claim, download_normalization_artifact))
    except Exception as error:
        sender.send(normalization_failure(error))
    finally:
        sender.close()


class ProcessIsolatedNormalizationExecutor:
    """Keep lease ownership in the worker process and enforce a hard wall clock limit."""

    def __init__(self, settings: NormalizationExecutionSettings, timeout: timedelta) -> None:
        if timeout <= timedelta(0):
            raise ValueError("timeout must be positive.")
        self._settings = settings
        self._timeout = timeout

    def execute(
        self,
        claim: ClaimedTradingReviewJob,
        cancellation_check: Callable[[], bool],
    ) -> NormalizationOutcome:
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=execute_normalization_child,
            args=(self._settings, claim, sender),
        )
        process.start()
        sender.close()
        deadline = monotonic() + self._timeout.total_seconds()
        try:
            while True:
                if cancellation_check():
                    _terminate(process)
                    return FailedNormalization(
                        SafeTradingReviewFailure(
                            "JOB_CANCELLED", "Normalization was cancelled.", False, "CANCELLATION"
                        ),
                        False,
                    )
                remaining = deadline - monotonic()
                if remaining <= 0:
                    _terminate(process)
                    return FailedNormalization(
                        SafeTradingReviewFailure(
                            "INTERNAL_ERROR",
                            "Normalization exceeded its time limit.",
                            False,
                            "TIMEOUT",
                        ),
                        False,
                    )
                if receiver.poll(min(remaining, 0.2)):
                    try:
                        outcome = cast(NormalizationOutcome, receiver.recv())
                    except EOFError:
                        process.join()
                        return FailedNormalization(
                            SafeTradingReviewFailure(
                                "TEMPORARY_DEPENDENCY_FAILURE",
                                "Normalization worker exited without a result.",
                                True,
                                "EXECUTION",
                            ),
                            True,
                        )
                    process.join(max(0, deadline - monotonic()))
                    if process.is_alive():
                        _terminate(process)
                        return FailedNormalization(
                            SafeTradingReviewFailure(
                                "INTERNAL_ERROR",
                                "Normalization exceeded its time limit.",
                                False,
                                "TIMEOUT",
                            ),
                            False,
                        )
                    return outcome
                if not process.is_alive():
                    process.join()
                    return FailedNormalization(
                        SafeTradingReviewFailure(
                            "TEMPORARY_DEPENDENCY_FAILURE",
                            "Normalization worker exited without a result.",
                            True,
                            "EXECUTION",
                        ),
                        True,
                    )
        finally:
            receiver.close()


def _terminate(process: BaseProcess) -> None:
    process.terminate()
    process.join(5)
    if process.is_alive():
        process.kill()
        process.join()
