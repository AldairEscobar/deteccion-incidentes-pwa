"""Registro persistente de cámaras dadas de alta desde la PWA.

Cada cámara es una fila de SQLite: identidad, nombre visible, URL de la fuente
(RTSP/HTTP/archivo) y ubicación. El estado en vivo (en_vivo, sin_señal...) no se
persiste: lo calcula el supervisor mientras el pipeline corre.

La URL puede llevar credenciales (``rtsp://user:pass@host``); por eso todas las
salidas pasan por :func:`mask_source` antes de llegar al navegador.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Credenciales embebidas en una URL: user:password@host
_URL_CREDENTIALS = re.compile(
    r"(?P<scheme>[a-zA-Z][\w+.-]*://)(?P<user>[^:/@\s]+):(?P<password>[^@/\s]*)@"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS camaras (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    lat REAL,
    lon REAL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Camera:
    """Cámara registrada por el operador."""

    id: str
    name: str
    source: str
    address: str = ""
    lat: float | None = None
    lon: float | None = None
    enabled: bool = True
    created_at: str = ""


def mask_source(source: str) -> str:
    """Oculta la contraseña de una URL sin perder el resto de la información."""

    if not source:
        return ""
    return _URL_CREDENTIALS.sub(r"\g<scheme>\g<user>:***@", source)


def new_camera_id() -> str:
    """Identificador corto, legible y único para una cámara."""
    return f"cam-{uuid.uuid4().hex[:8]}"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class CameraStore:
    """Repositorio SQLite de cámaras, seguro para acceso concurrente."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._db_path = str(db_path) if str(db_path) else ":memory:"
        if self._db_path != ":memory:":
            Path(self._db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._conn = sqlite3.connect(
            self._db_path, check_same_thread=False, timeout=10.0
        )
        self._conn.row_factory = sqlite3.Row
        if self._db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------- conversión
    @staticmethod
    def _row_to_camera(row: sqlite3.Row) -> Camera:
        return Camera(
            id=str(row["id"]),
            name=str(row["name"]),
            source=str(row["source"]),
            address=str(row["address"] or ""),
            lat=row["lat"],
            lon=row["lon"],
            enabled=bool(row["enabled"]),
            created_at=str(row["created_at"]),
        )

    # -------------------------------------------------------------- escritura
    def add(
        self,
        name: str,
        source: str,
        *,
        address: str = "",
        lat: float | None = None,
        lon: float | None = None,
        enabled: bool = True,
        camera_id: str | None = None,
    ) -> Camera:
        """Registra una cámara nueva y la devuelve."""
        camera = Camera(
            id=camera_id or new_camera_id(),
            name=name.strip(),
            source=source.strip(),
            address=address.strip(),
            lat=lat,
            lon=lon,
            enabled=enabled,
            created_at=_now(),
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO camaras
                    (id, name, source, address, lat, lon, enabled,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    camera.id,
                    camera.name,
                    camera.source,
                    camera.address,
                    camera.lat,
                    camera.lon,
                    int(camera.enabled),
                    camera.created_at,
                    camera.created_at,
                ),
            )
            self._conn.commit()
        return camera

    def update(self, camera_id: str, **fields: object) -> Camera | None:
        """Actualiza los campos indicados; ``None`` si la cámara no existe."""
        allowed = {"name", "source", "address", "lat", "lon", "enabled"}
        changes = {key: value for key, value in fields.items() if key in allowed}
        if not changes:
            return self.get(camera_id)
        if "enabled" in changes:
            changes["enabled"] = int(bool(changes["enabled"]))
        assignments = ", ".join(f"{key} = ?" for key in changes)
        values = [*changes.values(), _now(), camera_id]
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE camaras SET {assignments}, updated_at = ? WHERE id = ?",
                values,
            )
            self._conn.commit()
            if cursor.rowcount == 0:
                return None
        return self.get(camera_id)

    def delete(self, camera_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM camaras WHERE id = ?", (camera_id,))
            self._conn.commit()
            return cursor.rowcount > 0

    # ---------------------------------------------------------------- lectura
    def get(self, camera_id: str) -> Camera | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM camaras WHERE id = ?", (camera_id,)
            ).fetchone()
        return self._row_to_camera(row) if row is not None else None

    def list(self) -> list[Camera]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM camaras ORDER BY created_at ASC, id ASC"
            ).fetchall()
        return [self._row_to_camera(row) for row in rows]

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS total FROM camaras").fetchone()
        return int(row["total"])

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True
