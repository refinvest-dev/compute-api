"""Warm-process measurement of the production Daily Bar pipeline."""

from __future__ import annotations

import hashlib
import tracemalloc
from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter_ns
from typing import TypeVar, cast

from refinvest.api.schemas.backtests import CreateBacktestRequestTransport
from refinvest.application.backtest_executor import (
    DailyBarBacktestExecutor,
    ExecutionStage,
    ExecutionStageObserver,
)
from refinvest.application.backtest_job_payload import backtest_request_to_job_payload
from refinvest.application.backtest_worker_host import (
    TerminalBacktestFailure,
)
from refinvest.application.backtest_workers import ClaimedBacktestJob, ExecutableBacktestJob
from refinvest.benchmarking.canonical import canonical_result_bytes
from refinvest.benchmarking.inputs import FixtureInput, ScenarioInput
from refinvest.engine.validation import validate_strategy_version

REQUIRED_STAGES: tuple[ExecutionStage, ...] = (
    "snapshot_load",
    "request_strategy_validation",
    "calendar_resolution",
    "signal_evaluation",
    "entry_exit_resolution",
    "trade_realization",
    "performance_benchmark_assembly",
    "result_serialization",
)
_T = TypeVar("_T")


@dataclass(frozen=True)
class IterationResult:
    iteration: int
    status: str
    total_nanoseconds: int
    stage_nanoseconds: dict[str, int]
    python_peak_allocated_bytes: int
    result_hash: str | None
    terminal_status: str
    trade_count: int | None
    sample_size_warning: str | None
    error_code: str | None = None


@dataclass(frozen=True)
class MeasurementResult:
    samples: tuple[IterationResult, ...]
    reference_hash: str | None
    terminal_status: str
    trade_count: int | None
    sample_size_warning: str | None
    determinism_passed: bool
    mismatches: tuple[dict[str, object], ...]


class StageRecorder(ExecutionStageObserver):
    def __init__(self) -> None:
        self._started: dict[ExecutionStage, int] = {}
        self._durations: dict[str, int] = {}

    def stage_started(self, stage: ExecutionStage) -> None:
        if stage in self._started:
            raise RuntimeError(f"Stage is already active: {stage}")
        self._started[stage] = perf_counter_ns()

    def stage_completed(self, stage: ExecutionStage) -> None:
        started = self._started.pop(stage)
        duration = perf_counter_ns() - started
        if duration < 0:
            raise RuntimeError("Monotonic clock returned a negative duration.")
        self._durations[stage] = self._durations.get(stage, 0) + duration

    def measure(self, stage: ExecutionStage, operation: Callable[[], _T]) -> _T:
        self.stage_started(stage)
        try:
            return operation()
        finally:
            self.stage_completed(stage)

    def durations(self) -> dict[str, int]:
        if self._started:
            raise RuntimeError("A measured stage did not complete.")
        return dict(self._durations)


def run_warm_benchmark(
    fixture: FixtureInput,
    scenario: ScenarioInput,
    executor: DailyBarBacktestExecutor,
    warmups: int,
    iterations: int,
) -> MeasurementResult:
    if warmups < 0:
        raise ValueError("warmups must be non-negative.")
    if iterations <= 0:
        raise ValueError("iterations must be positive.")

    observed: list[IterationResult] = []
    all_hashes: list[tuple[str, str]] = []
    mismatches: list[dict[str, object]] = []
    reference: IterationResult | None = None

    for warmup in range(1, warmups + 1):
        result = _run_iteration(fixture, scenario, executor, -warmup, measure_memory=False)
        if result.status != "SUCCEEDED":
            mismatches.append({"field": "warmup", "expected": "SUCCEEDED", "actual": "FAILED"})
        if result.result_hash is not None:
            all_hashes.append((f"warmup:{warmup}", result.result_hash))
            reference = reference or result

    for iteration in range(1, iterations + 1):
        result = _run_iteration(fixture, scenario, executor, iteration, measure_memory=True)
        observed.append(result)
        if result.result_hash is not None:
            all_hashes.append((f"iteration:{iteration}", result.result_hash))
            reference = reference or result

    reference_hash = reference.result_hash if reference is not None else None
    for label, result_hash in all_hashes:
        if reference_hash is not None and result_hash != reference_hash:
            mismatches.append(
                {
                    "field": "resultHash",
                    "iteration": label,
                    "expected": reference_hash,
                    "actual": result_hash,
                }
            )
    if any(sample.status != "SUCCEEDED" for sample in observed):
        mismatches.append(
            {
                "field": "successfulIterations",
                "expected": iterations,
                "actual": sum(sample.status == "SUCCEEDED" for sample in observed),
            }
        )
    return MeasurementResult(
        tuple(observed),
        reference_hash,
        reference.terminal_status if reference is not None else "FAILED",
        reference.trade_count if reference is not None else None,
        reference.sample_size_warning if reference is not None else None,
        not mismatches,
        tuple(mismatches),
    )


def _run_iteration(
    fixture: FixtureInput,
    scenario: ScenarioInput,
    executor: DailyBarBacktestExecutor,
    iteration: int,
    *,
    measure_memory: bool,
) -> IterationResult:
    recorder = StageRecorder()
    peak = 0
    started = perf_counter_ns()
    if measure_memory:
        tracemalloc.start()
        tracemalloc.reset_peak()
    try:
        request = recorder.measure(
            "request_strategy_validation",
            lambda: _validated_transport_request(scenario.request_payload),
        )
        run_id = _run_id(scenario.scenario_id, iteration)
        job = ExecutableBacktestJob(
            ClaimedBacktestJob(
                run_id,
                "benchmark-runner",
                fixture.fixture_id,
                1,
                backtest_request_to_job_payload(request),
            ),
            fixture.snapshot,
        )
        outcome = executor.execute_request(job, request, recorder)
        if isinstance(outcome, TerminalBacktestFailure):
            return _failed_iteration(iteration, started, recorder, peak, outcome.error_code)
        canonical = recorder.measure(
            "result_serialization",
            lambda: canonical_result_bytes(
                outcome.result_payload, scenario.canonicalization_version
            ),
        )
        result_hash = "sha256:" + hashlib.sha256(canonical).hexdigest()
        metrics = cast(dict[str, object], outcome.result_payload["metrics"])
        terminal_status = "COMPLETED"
        trade_count = cast(int, metrics["tradeCount"])
        sample_warning = cast(str, outcome.result_payload["sampleSizeWarning"])
        if measure_memory:
            _, peak = tracemalloc.get_traced_memory()
        total = perf_counter_ns() - started
        return IterationResult(
            iteration,
            "SUCCEEDED",
            total,
            recorder.durations(),
            peak,
            result_hash,
            terminal_status,
            trade_count,
            sample_warning,
        )
    except Exception:
        return _failed_iteration(iteration, started, recorder, peak, None)
    finally:
        if measure_memory and tracemalloc.is_tracing():
            tracemalloc.stop()


def _failed_iteration(
    iteration: int,
    started: int,
    recorder: StageRecorder,
    peak: int,
    error_code: str | None,
) -> IterationResult:
    if tracemalloc.is_tracing():
        _, peak = tracemalloc.get_traced_memory()
    return IterationResult(
        iteration,
        "FAILED",
        perf_counter_ns() - started,
        recorder.durations(),
        peak,
        None,
        "FAILED",
        None,
        None,
        error_code,
    )


def _validated_transport_request(payload: dict[str, object]):
    transport = CreateBacktestRequestTransport.model_validate(payload)
    request = transport.to_domain()
    validate_strategy_version(request.strategy_version)
    return request


def _run_id(scenario_id: str, iteration: int) -> str:
    phase = "warmup" if iteration < 0 else "iteration"
    return f"benchmark:{scenario_id}:{phase}:{abs(iteration)}"
