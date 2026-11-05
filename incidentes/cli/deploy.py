"""Despliegue del ecosistema completo: central + N clientes.

Orden de arranque:

1. Central (dashboard PyQt5 con la API REST embebida).
2. Espera activa a que ``/health`` responda.
3. Semillero de N clientes (una ventana por cámara/localidad).

Uso::

    python -m incidentes.cli.deploy
    python -m incidentes.cli.deploy --clients 4 --consoles
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable
IS_WINDOWS = os.name == "nt"


def _spawn(module: str, args: list[str] | None = None) -> subprocess.Popen:
    """Lanza un módulo del proyecto en un proceso independiente."""
    command = [PYTHON, "-m", module, *(args or [])]
    options: dict = {"cwd": PROJECT_ROOT}
    if IS_WINDOWS:
        options["creationflags"] = subprocess.CREATE_NEW_CONSOLE
    print(f"  -> {' '.join(command)}")
    return subprocess.Popen(command, **options)


def _wait_for_central(url: str, timeout_sec: float = 20.0) -> bool:
    """Espera a que ``/health`` responda, sin dormir un tiempo fijo."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=1) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.4)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Despliegue del ecosistema de alertas"
    )
    parser.add_argument("--clients", type=int, default=4, help="nº de clientes")
    parser.add_argument(
        "--consoles",
        action="store_true",
        help="abrir cada cliente con su propia consola (Windows)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    if args.clients < 1:
        parser.error("--clients debe ser >= 1")

    print(f"Iniciando ecosistema de alertas (SO: {'Windows' if IS_WINDOWS else os.name})")

    central = _spawn(
        "incidentes.cli.central",
        ["--host", args.host, "--port", str(args.port)],
    )
    base_url = f"http://{args.host}:{args.port}"
    print(f"Esperando a que la central responda en {base_url}/health ...")
    if not _wait_for_central(base_url):
        print("[ERROR] La central no arrancó. Revisa la consola de la central.")
        return 1
    print("[OK] Central en línea.")

    client_args = ["--clients", str(args.clients)]
    if args.consoles:
        client_args.append("--consoles")
    _spawn("incidentes.cli.launch_clients", client_args)

    print(
        f"\nEcosistema desplegado: central + {args.clients} cliente(s).\n"
        "En cada ventana: carga el video y pulsa 'Iniciar Detección'."
    )
    # El proceso de la central se mantiene vivo; este lanzador puede terminar.
    del central
    return 0


if __name__ == "__main__":
    sys.exit(main())
