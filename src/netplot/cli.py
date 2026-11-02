"""Command-line entry point for netplot."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import uvicorn


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the netplot web dashboard.")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface to bind (default: %(default)s)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="TCP port to bind (default: %(default)s)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload while developing",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Sequence[str] | None = None) -> int:
    """Launch the netplot ASGI application and return process status."""
    parser = _parse_args(argv)

    try:
        uvicorn.run(
            "netplot.web:app",
            host=parser.host,
            port=parser.port,
            reload=parser.reload,
        )
        return 0
    except (KeyboardInterrupt, SystemExit):
        raise
    except (
        Exception  # noqa: BLE001 - top-level CLI must not crash, just report
    ) as exc:  # pragma: no cover - exercised indirectly by behavior tests
        _report_startup_error(exc)
        return 1


def _report_startup_error(exc: BaseException) -> None:
    """Report a startup failure in a user-friendly format."""
    print(f"Failed to start netplot server: {exc.__class__.__name__}: {exc}")


__all__: list[str] = ["main"]
