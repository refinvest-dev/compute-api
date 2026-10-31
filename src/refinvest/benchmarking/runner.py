"""Warm benchmark workflow from verified inputs through an atomic report write."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from refinvest.application.backtest_executor import DailyBarBacktestExecutor
from refinvest.benchmarking.inputs import FixtureInput, load_fixture, load_scenario
from refinvest.benchmarking.measurement import MeasurementResult, run_warm_benchmark
from refinvest.benchmarking.report import build_report, load_expected, write_report_atomic


@dataclass(frozen=True)
class BenchmarkRunResult:
    scenario_id: str
    measurement: MeasurementResult
    passed: bool


def run_warm_benchmark_report(
    *,
    fixture_manifest: Path,
    snapshot_path: Path,
    scenario_path: Path,
    output: Path,
    warmups: int,
    iterations: int,
    expected_results: Path | None,
    repository_root: Path,
    resolve_engine_version: Callable[[], str],
    create_executor: Callable[[FixtureInput, str], DailyBarBacktestExecutor],
) -> BenchmarkRunResult:
    """Run the supported warm workflow; composition supplies version and executor factories."""
    fixture = load_fixture(fixture_manifest, snapshot_path)
    scenario = load_scenario(scenario_path, fixture.fixture_id)
    engine_version = resolve_engine_version()
    executor = create_executor(fixture, engine_version)
    expected = (
        load_expected(expected_results, scenario.scenario_id)
        if expected_results is not None
        else None
    )
    started_at = datetime.now(UTC)
    measurement = run_warm_benchmark(fixture, scenario, executor, warmups, iterations)
    completed_at = datetime.now(UTC)
    report, passed = build_report(
        fixture,
        scenario,
        measurement,
        cache_mode="warm",
        warmups=warmups,
        iterations=iterations,
        engine_version=engine_version,
        started_at=started_at,
        completed_at=completed_at,
        expected=expected,
        repository_root=repository_root,
    )
    write_report_atomic(output, report)
    return BenchmarkRunResult(scenario.scenario_id, measurement, passed)
