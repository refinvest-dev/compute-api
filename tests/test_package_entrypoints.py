"""Published CLI and ASGI entrypoints remain explicit and callable."""

from importlib import import_module
from importlib.metadata import distribution

import pytest


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
