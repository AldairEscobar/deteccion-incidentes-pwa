"""Punto de entrada de la central con PWA (modo B).

Levanta la API, el supervisor de cámaras y sirve la interfaz instalable en la
raíz. No requiere PyQt5: todo se controla desde el navegador.

Uso::

    python -m incidentes.cli.serve
    python -m incidentes.cli.serve --host 0.0.0.0 --port 8000

Abre después <http://localhost:8000> y da de alta la URL de tu cámara.
"""

from __future__ import annotations

import argparse
import sys

import uvicorn

from incidentes.logging_setup import setup_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Central de alertas + PWA (sin dashboard de escritorio)"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
    )
    args = parser.parse_args(argv)

    setup_logging()
    uvicorn.run(
        "incidentes.server.app:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
