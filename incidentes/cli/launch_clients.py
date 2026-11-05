"""Semillero de clientes: lanza N detectores, uno por cámara/localidad.

Cada proceso abre su propia ventana Tkinter, se registra en la central y
mantiene su latido. El operador carga un video en cada ventana; al confirmarse
violencia, el cliente envía una alerta y suena la alarma local.

Uso::

    python -m incidentes.cli.launch_clients
    python -m incidentes.cli.launch_clients --clients 6
    python -m incidentes.cli.launch_clients --consoles   # Windows
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable
IS_WINDOWS = os.name == "nt"

# Cámara + localidad de ejemplo. Si se piden más de cuatro, se generan
# localidades adicionales automáticamente.
CATALOG = [
    ("camara-01", "Gustavo A. Madero"),
    ("camara-02", "Iztapalapa"),
    ("camara-03", "Coyoacán"),
    ("camara-04", "Álvaro Obregón"),
]
COORDINATES = {
    "Gustavo A. Madero": (19.4845, -99.1110),
    "Iztapalapa": (19.3560, -99.0920),
    "Coyoacán": (19.3467, -99.1618),
    "Álvaro Obregón": (19.3590, -99.2000),
}


def identity(index: int) -> tuple[str, str]:
    """Devuelve ``(client_id, localidad)`` para la cámara número ``index``."""
    if index < len(CATALOG):
        return CATALOG[index]
    return f"camara-{index + 1:02d}", f"Localidad {index + 1}"


def launch_client(
    client_id: str,
    address: str,
    new_console: bool,
) -> subprocess.Popen:
    """Abre un proceso del cliente con su identidad y coordenadas."""
    command = [
        PYTHON,
        "-m",
        "incidentes.cli.client",
        "--client-id",
        client_id,
        "--address",
        address,
    ]
    lat, lon = COORDINATES.get(address, (None, None))
    if lat is not None:
        command += ["--lat", str(lat), "--lon", str(lon)]

    options: dict = {"cwd": PROJECT_ROOT}
    if new_console and IS_WINDOWS:
        options["creationflags"] = subprocess.CREATE_NEW_CONSOLE
    return subprocess.Popen(command, **options)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Lanza N clientes (una ventana = una cámara)"
    )
    parser.add_argument("--clients", type=int, default=4)
    parser.add_argument(
        "--consoles",
        action="store_true",
        help="abrir cada cliente con su propia consola de log (Windows)",
    )
    args = parser.parse_args(argv)

    if args.clients < 1:
        parser.error("--clients debe ser >= 1")

    print(f"Lanzando {args.clients} cliente(s), una ventana por cámara...\n")
    processes: list[subprocess.Popen] = []
    for index in range(args.clients):
        client_id, address = identity(index)
        process = launch_client(client_id, address, args.consoles)
        processes.append(process)
        print(f"  [+] {client_id}  ->  {address}")
        # Arranque escalonado para no saturar la carga de YOLO/Ollama.
        time.sleep(0.6)

    print(
        f"\n{len(processes)} ventanas abiertas. En cada una: carga el video y "
        "pulsa 'Iniciar Detección'."
    )
    print("Cierra las ventanas para terminar (o Ctrl+C aquí).")

    try:
        for process in processes:
            process.wait()
    except KeyboardInterrupt:
        print("\nDeteniendo clientes...")
        for process in processes:
            if process.poll() is None:
                process.terminate()
        print("Clientes detenidos.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
