"""Registro de clientes conectados (multi-cliente) con heartbeat.

Cada cliente se registra una vez y luego envía latidos periódicos. Si deja de
latir durante más de ``alive_seconds``, la central lo marca como desconectado.

El acceso es concurrente (varios hilos del pool de FastAPI y el hilo de la GUI
que consulta el estado), por lo que todas las operaciones pasan por un único
``Lock``.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat(timespec="seconds")


class ClientRegistry:
    """Registro en memoria de clientes con detección de desconexión."""

    def __init__(self, alive_seconds: float = 30.0) -> None:
        self._alive = alive_seconds
        self._lock = threading.Lock()
        self._clients: dict[str, dict] = {}

    def _format(self, client: dict, now: float) -> dict:
        """Convierte el registro interno en una ficha lista para la API."""
        return {
            "client_id": client["client_id"],
            "name": client["name"],
            "address": client["address"],
            "lat": client["lat"],
            "lon": client["lon"],
            "ip": client["ip"],
            "connected_at": _iso(client["connected_at"]),
            "last_seen": _iso(client["last_seen"]),
            "online": (now - client["last_seen"]) <= self._alive,
            "source_state": client.get("source_state", "UNKNOWN"),
        }

    def register(
        self,
        client_id: str,
        name: str = "",
        address: str = "",
        lat: float | None = None,
        lon: float | None = None,
        ip: str = "",
        source_state: str = "",
    ) -> dict:
        """Registra o refresca un cliente y añade la bandera ``nuevo``."""
        now = time.time()
        with self._lock:
            is_new = client_id not in self._clients
            previous = self._clients.get(client_id)
            self._clients[client_id] = {
                "client_id": client_id,
                "name": name or client_id,
                "address": address,
                "lat": lat,
                "lon": lon,
                "ip": ip,
                "connected_at": previous["connected_at"] if previous else now,
                "last_seen": now,
                "source_state": source_state
                or (previous or {}).get("source_state", "UNKNOWN"),
            }
            record = dict(self._clients[client_id])

        info = self._format(record, now)
        info["nuevo"] = is_new
        return info

    def heartbeat(
        self, client_id: str, ip: str = "", source_state: str = ""
    ) -> dict | None:
        """Refresca el latido y el estado de la cámara; ``None`` si no existe."""
        now = time.time()
        with self._lock:
            client = self._clients.get(client_id)
            if client is None:
                return None
            client["last_seen"] = now
            if ip:
                client["ip"] = ip
            if source_state:
                client["source_state"] = source_state
            record = dict(client)
        return self._format(record, now)

    def remove(self, client_id: str) -> bool:
        with self._lock:
            return self._clients.pop(client_id, None) is not None

    def list(self, only_alive: bool = True) -> list[dict]:
        now = time.time()
        with self._lock:
            records = [dict(client) for client in self._clients.values()]
        clients = [self._format(record, now) for record in records]
        if only_alive:
            clients = [client for client in clients if client["online"]]
        return sorted(clients, key=lambda item: item["client_id"])

    def count(self) -> int:
        """Número de clientes en línea."""
        return len(self.list())
