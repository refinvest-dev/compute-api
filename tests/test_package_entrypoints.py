"""Published CLI and ASGI entrypoints remain explicit and callable."""

import os
import subprocess
import sys
import textwrap
import tomllib
from importlib import import_module
from importlib.metadata import distribution
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
    "scripts"
]


def run_fresh(arguments: list[str]) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("REFINVEST_", "AWS_"))
        and key not in ("PYTHONPATH", "DATABASE_URL", "PGPASSWORD", "PGSERVICE")
    }
    env.update(
        PYTHONPATH=os.pathsep.join((str(PROJECT_ROOT / "src"), str(PROJECT_ROOT))),
        AWS_EC2_METADATA_DISABLED="true",
    )
    result = subprocess.run(
        [sys.executable, *arguments],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (result.stdout + result.stderr)[-4000:]


IMPORT_GUARD = """
import importlib.abc
import sys

class NoHttpComposition(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == item or fullname.startswith(item + '.') for item in blocked):
            raise AssertionError('Unexpected HTTP import: ' + fullname)

sys.meta_path.insert(0, NoHttpComposition())
"""


@pytest.mark.parametrize("target", ["refinvest:main", "refinvest.app.main:main"])
def test_cli_entrypoints_keep_uvicorn_arguments(monkeypatch, target) -> None:
    from uvicorn.importer import import_from_string

    calls = []
    monkeypatch.setattr(
        import_module("uvicorn"), "run", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    import_from_string(target)()
    assert calls == [(("refinvest.app.main:app",), {"host": "0.0.0.0", "port": 8000})]


def test_installed_console_entrypoint_keeps_target_and_launch_behavior(monkeypatch) -> None:
    entrypoint = next(ep for ep in distribution("refinvest").entry_points if ep.name == "refinvest")
    assert entrypoint.group == "console_scripts"
    assert entrypoint.value == "refinvest:main"
    calls = []
    monkeypatch.setattr(
        import_module("uvicorn"), "run", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    entrypoint.load()()
    assert calls == [(("refinvest.app.main:app",), {"host": "0.0.0.0", "port": 8000})]


def test_canonical_asgi_target_returns_existing_singleton() -> None:
    from uvicorn.importer import import_from_string

    application = import_module("refinvest.app.main").app
    assert import_from_string("refinvest.app.main:app") is application
    assert import_from_string("refinvest.app.main:app") is application


def test_console_entrypoint_does_not_hide_startup_failure(monkeypatch) -> None:
    error = OSError("synthetic-startup-failure")

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(import_module("uvicorn"), "run", fail)
    with pytest.raises(OSError) as caught:
        import_module("refinvest").main()
    assert caught.value is error


@pytest.mark.parametrize(
    "target",
    [
        "refinvest",
        "refinvest.engine.metrics",
        "refinvest.marketdata.ports",
        "refinvest.contracts.analytics.identity",
        "refinvest.trading_review.analytics",
        "refinvest.app.worker",
        "refinvest.app.trading_review_worker",
        "refinvest.app.trading_analytics_worker",
    ],
)
def test_cold_compute_and_worker_imports_do_not_load_http(target) -> None:
    run_fresh(
        [
            "-c",
            "blocked = ('fastapi', 'starlette', 'uvicorn', 'refinvest.api', 'refinvest.app.main')\n"
            + IMPORT_GUARD
            + "\nfrom importlib import import_module\nimport_module(sys.argv[1])\n",
            target,
        ]
    )


@pytest.mark.parametrize("name", list(SCRIPTS))
def test_every_declared_console_entrypoint_loads_without_http_composition(name) -> None:
    run_fresh(
        [
            "-c",
            "blocked = ('refinvest.app.main', 'refinvest.api.routes')\n"
            + IMPORT_GUARD
            + textwrap.dedent(
                """
                from importlib.metadata import distribution
                ep = next(ep for ep in distribution('refinvest').entry_points
                          if ep.group == 'console_scripts' and ep.name == sys.argv[1])
                assert ep.value == sys.argv[2]
                assert callable(ep.load())
                """
            ),
            name,
            SCRIPTS[name],
        ]
    )


@pytest.mark.parametrize(
    "order",
    [
        ["refinvest", "refinvest.app.worker", "refinvest.app.main"],
        ["refinvest.app.worker", "refinvest", "refinvest.app.main"],
        ["refinvest.app.main", "refinvest.app.worker", "refinvest"],
    ],
)
def test_import_order_keeps_app_namespace_and_explicit_http_target(order) -> None:
    run_fresh(
        [
            "-c",
            textwrap.dedent(
                """
                import sys
                from importlib import import_module
                from types import ModuleType
                for name in sys.argv[1:]:
                    import_module(name)
                import refinvest
                from refinvest import main
                from uvicorn.importer import import_from_string
                assert isinstance(refinvest.app, ModuleType)
                assert refinvest.app is import_module('refinvest.app')
                application = import_module('refinvest.app.main').app
                assert import_from_string('refinvest.app.main:app') is application
                calls = []
                import uvicorn
                uvicorn.run = lambda *args, **kwargs: calls.append((args, kwargs))
                main()
                assert calls == [(('refinvest.app.main:app',), {'host': '0.0.0.0', 'port': 8000})]
                """
            ),
            *order,
        ]
    )


def test_root_exports_console_main_and_leaves_app_as_package_namespace() -> None:
    run_fresh(
        [
            "-c",
            textwrap.dedent(
                """
                import sys
                import refinvest
                assert refinvest.__all__ == ['main']
                assert callable(refinvest.main)
                assert 'refinvest.app.main' not in sys.modules
                from refinvest import app
                from types import ModuleType
                assert isinstance(app, ModuleType)
                assert 'refinvest.app.main' not in sys.modules
                """
            ),
        ]
    )


def test_spawn_child_imports_compute_without_http_composition(tmp_path) -> None:
    script = tmp_path / "spawn_import_probe.py"
    script.write_text(
        textwrap.dedent(
            """
            import multiprocessing
            import sys

            def child(sender):
                from refinvest.trading_review.analytics import calculate_behavior_metrics
                from refinvest.engine.metrics import evaluate_simple_metric
                sender.send(
                    callable(calculate_behavior_metrics) and callable(evaluate_simple_metric)
                    and 'refinvest.app.main' not in sys.modules and 'fastapi' not in sys.modules
                )
                sender.close()

            if __name__ == '__main__':
                context = multiprocessing.get_context('spawn')
                receiver, sender = context.Pipe(duplex=False)
                process = context.Process(target=child, args=(sender,))
                process.start()
                sender.close()
                try:
                    assert receiver.poll(20), 'child did not report import state'
                    assert receiver.recv() is True
                    process.join(20)
                    assert not process.is_alive() and process.exitcode == 0
                finally:
                    if process.is_alive():
                        process.terminate()
                        process.join(5)
                        if process.is_alive():
                            process.kill()
                            process.join()
                    receiver.close()
            """
        ),
        encoding="utf-8",
    )
    run_fresh([str(script)])
