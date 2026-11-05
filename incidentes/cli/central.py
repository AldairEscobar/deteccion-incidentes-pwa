"""Punto de entrada de la central: dashboard PyQt5 + API REST embebida.

Uso::

    python -m incidentes.cli.central
    python -m incidentes.cli.central --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import argparse
import sys

from incidentes.central.dashboard import run
from incidentes.logging_setup import setup_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Central de alertas (dashboard + API REST)"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--no-auto",
        action="store_true",
        help="no arrancar el servidor automáticamente",
    )
    args = parser.parse_args(argv)

    setup_logging()
    return run(host=args.host, port=args.port, auto_start=not args.no_auto)


if __name__ == "__main__":
    sys.exit(main())
