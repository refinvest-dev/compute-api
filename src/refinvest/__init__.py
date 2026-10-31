"""Compute package with an explicit, deferred HTTP console entrypoint."""

__all__ = ["main"]


def main() -> None:
    """Launch the HTTP API; ASGI servers use refinvest.app.main:app directly."""
    from refinvest.app.main import main as api_main

    api_main()
