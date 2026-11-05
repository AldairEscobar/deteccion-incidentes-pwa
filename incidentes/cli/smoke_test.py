"""Prueba de humo del canal cliente -> central, sin video ni Ollama.

Levanta la API REST en un puerto libre, registra un cliente, envía una alerta
de ejemplo con el **mismo** adaptador que usa el detector y consulta el
historial. En segundos permite validar que el contrato REST funciona.

Uso::

    python -m incidentes.cli.smoke_test
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.request

import uvicorn

from incidentes.domain.entities import AlertPayload, GeoLocation
from incidentes.infrastructure.alerting.http_alert_client import HttpAlertDispatcher


def _free_port() -> int:
    """Pide al SO un puerto TCP libre para no chocar con la central real."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _health(base_url: str) -> str:
    try:
        with urllib.request.urlopen(f"{base_url}/health", timeout=3) as response:
            return response.read().decode()
    except Exception as exc:
        return f"error: {exc}"


def _get_json(url: str) -> list | dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode())


def main() -> int:
    # La prueba de humo no debe arrancar los pipelines de cámaras guardadas:
    # solo valida el contrato REST.
    os.environ.setdefault("CENTRAL_AUTOSTART", "0")
    from incidentes.server.app import app

    host, port = "127.0.0.1", _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True, name="smoke-central")
    thread.start()

    try:
        for _ in range(50):
            if getattr(server, "started", False):
                break
            time.sleep(0.1)
        if not getattr(server, "started", False):
            print("ERROR: la central no arrancó")
            return 1

        base_url = f"http://{host}:{port}"
        print(f"1) Central levantada en {base_url} (health: {_health(base_url)})")

        dispatcher = HttpAlertDispatcher(
            base_url=base_url,
            client_id="camara-prueba",
            location=GeoLocation(
                -12.04637, -77.04279, "Av. Abancay 1234, piso 2"
            ),
            timeout_sec=5,
        )
        print(f"2) ping desde el cliente -> {dispatcher.ping()}")
        print(f"   registro del cliente  -> {dispatcher.register()}")
        print(f"   heartbeat             -> {dispatcher.heartbeat()}")

        ack = dispatcher.dispatch(
            AlertPayload(
                type="PELEA",
                confidence=0.88,
                label="CONFIRMADO IA: PELEA",
                frame=320,
                time_sec=10.6,
                video="C:/videos/prueba.mp4",
                verified_by="ollama/qwen2.5vl:7b",
                reason="forcejeo entre dos personas",
            )
        )
        print(f"3) alerta enviada -> ticket={ack.ticket} estado={ack.status}")
        if not ack.delivered:
            print(f"ERROR: la central no aceptó la alerta ({ack.detail})")
            return 1
        print(f"   respuesta al cliente: {ack.message}")

        history = _get_json(f"{base_url}/api/v1/alertas")
        print(f"4) historial en la central -> {len(history)} alerta(s)")
        for record in history:
            print(
                f"     {record['ticket']} | {record['priority']} | "
                f"{record['alert']['type']} | {record['status']} | "
                f"{record['alert']['location'].get('address')}"
            )

        clients = _get_json(f"{base_url}/api/v1/clientes")
        print(f"5) clientes conectados -> {len(clients)}")
        for client in clients:
            print(
                f"     {client['client_id']} | {client['ip']} | "
                f"{client['address']} | online={client['online']}"
            )

        print("OK: canal cliente -> central verificado (alerta + registro).")
        return 0
    finally:
        server.should_exit = True
        thread.join(timeout=5)


if __name__ == "__main__":
    sys.exit(main())
