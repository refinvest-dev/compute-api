"""Console entrypoint for deterministic Compute Backtest benchmarks."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from refinvest.application.backtest_executor import DailyBarBacktestExecutor
from refinvest.benchmarking.inputs import (
    VerifiedFixtureLoader,
    load_fixture,
    load_scenario,
)
from refinvest.benchmarking.measurement import run_warm_benchmark
from refinvest.benchmarking.report import (
    build_report,
    load_expected,
    write_report_atomic,
)
from refinvest.persistence.object_storage.snapshot_artifacts import (
    FilesystemSnapshotExecutionDataLoader,
)


def benchmark_main() -> None:
    parser = _parser()
    args = parser.parse_args()
    if args.cache_mode == "process-cold":
        parser.error("process-cold is not implemented; it is never treated as warm.")
    if args.warmups < 0:
        parser.error("--warmups must be non-negative.")
    if args.iterations <= 0:
        parser.error("--iterations must be positive.")

    try:
        fixture = load_fixture(args.fixture_manifest, args.snapshot_path)
        scenario = load_scenario(args.scenario, fixture.fixture_id)
        engine_version = _engine_version()
        loader = VerifiedFixtureLoader(fixture, FilesystemSnapshotExecutionDataLoader())
        executor = DailyBarBacktestExecutor(loader, engine_version)
        expected = (
            load_expected(args.expected_results, scenario.scenario_id)
            if args.expected_results is not None
            else None
        )
        started_at = datetime.now(UTC)
        measurement = run_warm_benchmark(fixture, scenario, executor, args.warmups, args.iterations)
        completed_at = datetime.now(UTC)
        report, passed = build_report(
            fixture,
            scenario,
            measurement,
            cache_mode=args.cache_mode,
            warmups=args.warmups,
            iterations=args.iterations,
            engine_version=engine_version,
            started_at=started_at,
            completed_at=completed_at,
            expected=expected,
            repository_root=Path(__file__).resolve().parents[3],
        )
        write_report_atomic(args.output, report)
    except Exception as error:
        parser.exit(2, f"refinvest-benchmark: {type(error).__name__}: {error}\n")
    if not passed:
        parser.exit(1, "refinvest-benchmark: correctness gate failed; see report.\n")
    print(
        f"{scenario.scenario_id}: {measurement.reference_hash} "
        f"({len(measurement.samples)} measured iterations)"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="refinvest-benchmark",
        description="Run the production Backtest pipeline with an integration performance fixture.",
    )
    parser.add_argument("--fixture-manifest", type=Path, required=True)
    parser.add_argument("--snapshot-path", type=Path, required=True)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--cache-mode", choices=("warm", "process-cold"), default="warm")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-results", type=Path)
    return parser


def _engine_version() -> str:
    try:
        return version("refinvest")
    except PackageNotFoundError:
        return "0.1.0"


if __name__ == "__main__":
    benchmark_main()
