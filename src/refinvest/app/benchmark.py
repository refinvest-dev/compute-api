"""Console entrypoint for deterministic Compute Backtest benchmarks."""

from __future__ import annotations

import argparse
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from refinvest.application.backtest_executor import DailyBarBacktestExecutor
from refinvest.benchmarking.inputs import (
    FixtureInput,
    VerifiedFixtureLoader,
)
from refinvest.benchmarking.runner import run_warm_benchmark_report
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
        result = run_warm_benchmark_report(
            fixture_manifest=args.fixture_manifest,
            snapshot_path=args.snapshot_path,
            scenario_path=args.scenario,
            output=args.output,
            warmups=args.warmups,
            iterations=args.iterations,
            expected_results=args.expected_results,
            repository_root=Path(__file__).resolve().parents[3],
            resolve_engine_version=_engine_version,
            create_executor=_create_executor,
        )
    except Exception as error:
        parser.exit(2, f"refinvest-benchmark: {type(error).__name__}: {error}\n")
    if not result.passed:
        parser.exit(1, "refinvest-benchmark: correctness gate failed; see report.\n")
    print(
        f"{result.scenario_id}: {result.measurement.reference_hash} "
        f"({len(result.measurement.samples)} measured iterations)"
    )


def _create_executor(fixture: FixtureInput, engine_version: str) -> DailyBarBacktestExecutor:
    loader = VerifiedFixtureLoader(fixture, FilesystemSnapshotExecutionDataLoader())
    return DailyBarBacktestExecutor(loader, engine_version)


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
