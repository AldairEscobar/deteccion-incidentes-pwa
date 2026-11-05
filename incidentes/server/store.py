"""Almacén persistente de alertas y evidencia de la central (SQLite).

Cada incidente se guarda en una fila: metadatos consultables, el JSON completo
del incidente y la captura JPEG como BLOB. El JSON no incluye el base64 de la
imagen; incluye ``snapshot_url`` para recuperarla por la API.

La API (hilos de uvicorn) y el dashboard (hilo de Qt) comparten una conexión
protegida por un lock. Para pruebas, ``AlertStore()`` usa una base en memoria.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from incidentes.server.models import AlertRecord

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = "data/incidentes.db"
SNAPSHOT_MIME = "image/jpeg"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alertas (
    ticket TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    incident_type TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT '',
    eta_min INTEGER,
    units_json TEXT NOT NULL DEFAULT '[]',
    received_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    snapshot BLOB,
    snapshot_mime TEXT NOT NULL DEFAULT '',
    snapshot_sha256 TEXT NOT NULL DEFAULT '',
    has_snapshot INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alertas_client ON alertas(client_id);
CREATE INDEX IF NOT EXISTS idx_alertas_status ON alertas(status);
"""


class AlertStore:
    """Repositorio SQLite de alertas, seguro para acceso concurrente."""

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

    # ----------------------------------------------------------------- tickets
    def _next_ticket_locked(self) -> str:
        """Genera un ticket legible y único por día (``ALT-YYYYMMDD-0001``)."""
        day = datetime.now(UTC).strftime("%Y%m%d")
        prefix = f"ALT-{day}-"
        row = self._conn.execute(
            "SELECT ticket FROM alertas WHERE ticket LIKE ? "
            "ORDER BY ticket DESC LIMIT 1",
            (f"{prefix}%",),
        ).fetchone()
        number = 1
        if row is not None:
            try:
                number = int(str(row["ticket"]).rsplit("-", 1)[1]) + 1
            except (IndexError, ValueError):
                number = 1
        return f"{prefix}{number:04d}"

    # --------------------------------------------------------------- escritura
    def add(self, record: AlertRecord) -> AlertRecord:
        """Guarda la alerta y su captura; devuelve el registro con ticket."""
        snapshot, digest = self._decode_snapshot(record.alert.snapshot_b64)
        clean_alert = record.alert.model_copy(update={"snapshot_b64": None})
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            for _ in range(5):
                ticket = self._next_ticket_locked()
                saved = record.model_copy(
                    update={
                        "ticket": ticket,
                        "alert": clean_alert,
                        "has_snapshot": snapshot is not None,
                        "snapshot_url": (
                            f"/api/v1/alertas/{ticket}/captura"
                            if snapshot is not None
                            else ""
                        ),
                        "snapshot_sha256": digest,
                    }
                )
                try:
                    self._conn.execute(
                        """
                        INSERT INTO alertas (
                            ticket, client_id, incident_type, confidence, status,
                            priority, eta_min, units_json, received_at, updated_at,
                            payload_json, snapshot, snapshot_mime, snapshot_sha256,
                            has_snapshot
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            ticket,
                            saved.alert.client_id,
                            saved.alert.type,
                            saved.alert.confidence,
                            saved.status,
                            saved.priority,
                            saved.eta_min,
                            json.dumps(saved.units, ensure_ascii=False),
                            saved.received_at,
                            now,
                            saved.model_dump_json(),
                            snapshot,
                            SNAPSHOT_MIME if snapshot is not None else "",
                            digest,
                            int(snapshot is not None),
                        ),
                    )
                    self._conn.commit()
                    return saved
                except sqlite3.IntegrityError:
                    log.warning("ticket %s ya existe; reintentando", ticket)
            raise RuntimeError("no se pudo asignar un ticket único")

    def update_status(self, ticket: str, status: str) -> dict[str, Any] | None:
        """Actualiza el estado de una alerta; ``None`` si no existe."""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM alertas WHERE ticket = ?", (ticket,)
            ).fetchone()
            if row is None:
                return None
            record = json.loads(row["payload_json"])
            record["status"] = status
            self._conn.execute(
                "UPDATE alertas SET status = ?, updated_at = ?, payload_json = ? "
                "WHERE ticket = ?",
                (
                    status,
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    json.dumps(record, ensure_ascii=False),
                    ticket,
                ),
            )
            self._conn.commit()
            return record

    # ---------------------------------------------------------------- lectura
    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        """Devuelve las alertas más recientes primero."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload_json FROM alertas ORDER BY rowid DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def get(self, ticket: str) -> dict[str, Any] | None:
        """Devuelve el JSON del incidente, o ``None`` si no existe."""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM alertas WHERE ticket = ?", (ticket,)
            ).fetchone()
        return json.loads(row["payload_json"]) if row is not None else None

    def get_snapshot(self, ticket: str) -> tuple[bytes, str] | None:
        """Devuelve ``(jpeg, mime)`` de una alerta, o ``None`` si no tiene."""
        with self._lock:
            row = self._conn.execute(
                "SELECT snapshot, snapshot_mime FROM alertas "
                "WHERE ticket = ? AND snapshot IS NOT NULL",
                (ticket,),
            ).fetchone()
        if row is None:
            return None
        return bytes(row["snapshot"]), str(row["snapshot_mime"] or SNAPSHOT_MIME)

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS total FROM alertas"
            ).fetchone()
        return int(row["total"])

    # ------------------------------------------------------------------- util
    @staticmethod
    def _decode_snapshot(raw: str | None) -> tuple[bytes | None, str]:
        """Decodifica la captura base64 y calcula su huella; nunca lanza."""
        if not raw:
            return None, ""
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            log.warning("snapshot_b64 inválido: la alerta se guarda sin captura")
            return None, ""
        if not data:
            return None, ""
        return data, hashlib.sha256(data).hexdigest()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True
