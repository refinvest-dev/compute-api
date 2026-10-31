"""CLI response and report lifecycle contracts across workflow extraction."""

from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def pipeline_module():
    return import_module("refinvest.benchmarking.runner")


@pytest.fixture
def pipeline(monkeypatch, pipeline_module, tmp_path):
    app = import_module("refinvest.app.benchmark")
    state = SimpleNamespace(calls=[], failure=None, passed=True)
    state.fixture = SimpleNamespace(fixture_id="fixture-test")
    state.scenario = SimpleNamespace(scenario_id="scenario-test")
    state.measurement = SimpleNamespace(reference_hash="sha256:fixed", samples=(object(), object()))
    state.report = {"reportVersion": 1, "fixture": "fixture-test"}
    state.expected = {"scenarioId": "scenario-test"}
    state.loader = object()
    state.verified = object()
    state.executor = object()
    state.paths = {
        name: tmp_path / (name + " with space")
        for name in ("manifest", "snapshot", "scenario", "output", "expected")
    }

    def operation(name, value):
        def invoke(*args, **kwargs):
            state.calls.append((name, args, kwargs))
            if state.failure == name:
                raise OSError("failure at " + name)
            return value() if callable(value) else value

        return invoke

    for name, value in (
        ("load_fixture", state.fixture),
        ("load_scenario", state.scenario),
        ("load_expected", state.expected),
        ("run_warm_benchmark", state.measurement),
        ("build_report", lambda: (state.report, state.passed)),
        ("write_report_atomic", None),
    ):
        monkeypatch.setattr(pipeline_module, name, operation(name, value))
    for name, value in (
        ("_engine_version", "engine-test"),
        ("FilesystemSnapshotExecutionDataLoader", state.loader),
        ("VerifiedFixtureLoader", state.verified),
        ("DailyBarBacktestExecutor", state.executor),
    ):
        monkeypatch.setattr(app, name, operation(name, value))
    ticks = iter([datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 1, 2, tzinfo=UTC)])
    monkeypatch.setattr(
        pipeline_module, "datetime", SimpleNamespace(now=operation("clock", lambda: next(ticks)))
    )
    state.arguments = [
        "refinvest-benchmark",
        "--fixture-manifest",
        str(state.paths["manifest"]),
        "--snapshot-path",
        str(state.paths["snapshot"]),
        "--scenario",
        str(state.paths["scenario"]),
        "--output",
        str(state.paths["output"]),
    ]
    monkeypatch.setattr("sys.argv", state.arguments)
    return state


@pytest.mark.parametrize("with_expected", [False, True])
def test_cli_preserves_call_order_report_arguments_and_success_output(
    pipeline, with_expected, capsys
):
    if with_expected:
        pipeline.arguments.extend(["--expected-results", str(pipeline.paths["expected"])])
        warmups, iterations = 3, 10
    else:
        pipeline.arguments.extend(["--warmups", "0", "--iterations", "2"])
        warmups, iterations = 0, 2
    import_module("refinvest.app.benchmark").benchmark_main()
    names = [name for name, _, _ in pipeline.calls]
    assert names == [
        "load_fixture",
        "load_scenario",
        "_engine_version",
        "FilesystemSnapshotExecutionDataLoader",
        "VerifiedFixtureLoader",
        "DailyBarBacktestExecutor",
        *(["load_expected"] if with_expected else []),
        "clock",
        "run_warm_benchmark",
        "clock",
        "build_report",
        "write_report_atomic",
    ]
    calls = {name: (args, kwargs) for name, args, kwargs in pipeline.calls if name != "clock"}
    assert calls["load_fixture"] == ((pipeline.paths["manifest"], pipeline.paths["snapshot"]), {})
    assert calls["load_scenario"] == ((pipeline.paths["scenario"], "fixture-test"), {})
    assert calls["VerifiedFixtureLoader"] == ((pipeline.fixture, pipeline.loader), {})
    assert calls["DailyBarBacktestExecutor"] == ((pipeline.verified, "engine-test"), {})
    assert calls["run_warm_benchmark"] == (
        (pipeline.fixture, pipeline.scenario, pipeline.executor, warmups, iterations),
        {},
    )
    assert calls["build_report"] == (
        (pipeline.fixture, pipeline.scenario, pipeline.measurement),
        {
            "cache_mode": "warm",
            "warmups": warmups,
            "iterations": iterations,
            "engine_version": "engine-test",
            "started_at": datetime(2025, 1, 1, tzinfo=UTC),
            "completed_at": datetime(2025, 1, 2, tzinfo=UTC),
            "expected": pipeline.expected if with_expected else None,
            "repository_root": Path(__file__).resolve().parents[1],
        },
    )
    assert calls["write_report_atomic"] == ((pipeline.paths["output"], pipeline.report), {})
    if with_expected:
        assert calls["load_expected"] == ((pipeline.paths["expected"], "scenario-test"), {})
    captured = capsys.readouterr()
    assert captured.out == "scenario-test: sha256:fixed (2 measured iterations)\n"
    assert captured.err == ""


def test_correctness_failure_writes_report_before_exit_one(pipeline, capsys):
    pipeline.passed = False
    with pytest.raises(SystemExit) as caught:
        import_module("refinvest.app.benchmark").benchmark_main()
    assert caught.value.code == 1
    assert pipeline.calls[-1][0] == "write_report_atomic"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "refinvest-benchmark: correctness gate failed; see report.\n"


@pytest.mark.parametrize(
    "stage",
    ["load_fixture", "load_expected", "run_warm_benchmark", "build_report", "write_report_atomic"],
)
def test_workflow_error_preserves_exit_two_and_never_prints_success(pipeline, capsys, stage):
    pipeline.failure = stage
    pipeline.arguments.extend(["--expected-results", str(pipeline.paths["expected"])])
    with pytest.raises(SystemExit) as caught:
        import_module("refinvest.app.benchmark").benchmark_main()
    assert caught.value.code == 2
    assert pipeline.calls[-1][0] == stage
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"refinvest-benchmark: OSError: failure at {stage}\n"


@pytest.mark.parametrize(
    ("option", "value", "message"),
    [
        ("--cache-mode", "process-cold", "never treated as warm"),
        ("--warmups", "-1", "--warmups must be non-negative"),
        ("--iterations", "0", "--iterations must be positive"),
    ],
)
def test_invalid_options_fail_before_workflow(pipeline, capsys, option, value, message):
    pipeline.arguments.extend([option, value])
    with pytest.raises(SystemExit) as caught:
        import_module("refinvest.app.benchmark").benchmark_main()
    assert caught.value.code == 2
    assert pipeline.calls == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err


def test_help_does_not_run_workflow(pipeline, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["refinvest-benchmark", "--help"])
    with pytest.raises(SystemExit) as caught:
        import_module("refinvest.app.benchmark").benchmark_main()
    assert caught.value.code == 0
    assert pipeline.calls == []
    captured = capsys.readouterr()
    assert "--fixture-manifest" in captured.out
    assert "--expected-results" in captured.out
    assert captured.err == ""


def test_missing_required_arguments_do_not_run_workflow(pipeline, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["refinvest-benchmark"])
    with pytest.raises(SystemExit) as caught:
        import_module("refinvest.app.benchmark").benchmark_main()
    assert caught.value.code == 2
    assert pipeline.calls == []
    assert "required" in capsys.readouterr().err
