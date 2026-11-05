"""Adaptador de salida hacia la central de alertas (REST).

Solo usa la librería estándar (``urllib``) para no añadir dependencias al
cliente. Contrato **fail-open**: nunca lanza; si la central está caída, la
detección local sigue funcionando y la alerta se reporta como no entregada.

La central decide la ayuda a enviar según el tipo de incidente y la ubicación
que este adaptador adjunta desde su configuración (``client_id`` + ubicación).
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime

from incidentes.domain.entities import AlertAck, AlertPayload, GeoLocation
from incidentes.domain.ports import AlertDispatcher

log = logging.getLogger(__name__)


class HttpAlertDispatcher(AlertDispatcher):
    """Cliente REST de la central (alertas, registro y heartbeat)."""

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        client_id: str = "camara-01",
        location: GeoLocation | None = None,
        timeout_sec: float = 5.0,
        api_key: str = "",
        include_snapshot: bool = True,
        alerts_path: str = "/api/v1/alertas",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client_id = client_id
        self._location = location or GeoLocation()
        self._timeout = timeout_sec
        self._api_key = api_key
        self._include_snapshot = include_snapshot
        self._alerts_path = alerts_path

    # ------------------------------------------------------------------ utils
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        return headers

    def _post_json(self, path: str, payload: dict) -> dict:
        """Hace un POST JSON y devuelve la respuesta. Puede lanzar."""
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=json.dumps(payload).encode(),
            headers=self._headers(),
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            return json.loads(response.read().decode())

    # ------------------------------------------------------------------ salud
    def ping(self) -> bool:
        """True si la central responde en ``/health`` (best-effort)."""
        try:
            request = urllib.request.Request(f"{self._base_url}/health")
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status == 200
        except Exception:
            return False

    # -------------------------------------------------------------- ciclo vida
    def register(self, source_state: str = "") -> bool:
        """Da de alta (o refresca) este cliente en la central. Nunca lanza."""
        try:
            self._post_json(
                "/api/v1/clientes/registro",
                {
                    "client_id": self._client_id,
                    "name": self._client_id,
                    "address": self._location.address,
                    "lat": self._location.lat,
                    "lon": self._location.lon,
                    "source_state": source_state,
                },
            )
            log.info(
                "cliente %s registrado en la central %s",
                self._client_id,
                self._base_url,
            )
            return True
        except Exception as exc:
            log.warning(
                "no se pudo registrar el cliente %s en la central: %s",
                self._client_id,
                exc,
            )
            return False

    def heartbeat(self, source_state: str = "") -> bool:
        """Envía un latido. Si la central olvidó al cliente (404), lo re-registra."""
        try:
            self._post_json(
                "/api/v1/clientes/heartbeat",
                {
                    "client_id": self._client_id,
                    "source_state": source_state,
                },
            )
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return self.register(source_state)
            log.debug("el heartbeat falló (%s)", exc)
            return False
        except Exception:
            return False

    def get_alert_status(self, ticket: str) -> str | None:
        """Consulta el estado de un ticket; ``None`` si no se pudo leer.

        Lo usa el cliente tras una alerta para volver a verde cuando la central
        marca la incidencia como atendida.
        """
        if not ticket:
            return None
        try:
            path = f"{self._alerts_path}/{urllib.parse.quote(ticket, safe='')}"
            request = urllib.request.Request(
                f"{self._base_url}{path}", headers=self._headers()
            )
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = json.loads(response.read().decode())
            return str(body.get("status", "")) or None
        except Exception:
            log.debug("no se pudo consultar el ticket %s", ticket, exc_info=True)
            return None

    def unregister(self) -> bool:
        """Avisa a la central de que este cliente se desconecta. Nunca lanza."""
        try:
            request = urllib.request.Request(
                f"{self._base_url}/api/v1/clientes/{self._client_id}",
                method="DELETE",
                headers=self._headers(),
            )
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return response.status == 200
        except Exception:
            return False

    # ----------------------------------------------------------------- alerta
    def _build_payload(self, alert: AlertPayload) -> dict:
        timestamp = alert.timestamp or datetime.now(UTC).isoformat(
            timespec="seconds"
        )
        body: dict = {
            "client_id": self._client_id,
            "type": alert.type,
            "confidence": alert.confidence,
            "label": alert.label,
            "frame": alert.frame,
            "time_sec": alert.time_sec,
            "video": alert.video,
            "verified_by": alert.verified_by,
            "reason": alert.reason,
            "timestamp": timestamp,
            "location": {
                "lat": self._location.lat,
                "lon": self._location.lon,
                "address": self._location.address,
            },
        }
        if self._include_snapshot and alert.snapshot_jpeg:
            body["snapshot_b64"] = base64.b64encode(alert.snapshot_jpeg).decode()
        return body

    def dispatch(self, alert: AlertPayload) -> AlertAck:
        """Envía la alerta. Nunca lanza: devuelve ``delivered=False`` si falla."""
        try:
            request = urllib.request.Request(
                f"{self._base_url}{self._alerts_path}",
                data=json.dumps(self._build_payload(alert)).encode(),
                headers=self._headers(),
            )
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = json.loads(response.read().decode())
            return AlertAck(
                delivered=True,
                ticket=str(body.get("ticket", "")),
                status=str(body.get("status", "recibida")),
                priority=str(body.get("priority", "")),
                eta_min=body.get("eta_min"),
                message=str(body.get("message", "")),
            )
        except urllib.error.HTTPError as exc:
            detail = f"HTTP {exc.code}"
            with contextlib.suppress(Exception):
                detail += f": {exc.read().decode()[:200]}"
            log.error("la central rechazó la alerta (%s): %s", exc.code, detail)
            return AlertAck(False, detail=detail[:200])
        except Exception as exc:
            # Fail-open: no se pudo entregar, pero la detección local continúa.
            log.error(
                "no se pudo enviar la alerta a la central (%s): %s",
                self._base_url,
                exc,
            )
            return AlertAck(False, detail=f"dispatch-error: {exc}"[:200])
