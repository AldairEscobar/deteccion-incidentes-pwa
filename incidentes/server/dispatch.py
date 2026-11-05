"""Lógica de despacho: decide la ayuda a enviar según tipo y ubicación.

Es intencionalmente simple y basada en reglas para poder probarla sin
infraestructura. El aviso a unidades reales se hace por webhook opcional
(``DISPATCH_WEBHOOK_URL``); si no está configurado, el despacho queda
registrado en el log y en el historial de la central.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import TypedDict

from incidentes.server.models import AlertIn

log = logging.getLogger(__name__)

PRIORITY_BY_TYPE = {
    "ARMA": "CRITICA",
    "PELEA": "ALTA",
    "ROBO": "ALTA",
    "VIOLENCIA": "ALTA",
    "CAIDA": "MEDIA",
}
DEFAULT_PRIORITY = "MEDIA"

ETA_BY_PRIORITY = {"CRITICA": 3, "ALTA": 5, "MEDIA": 8, "BAJA": 12}
UNITS_BY_PRIORITY = {
    "CRITICA": ["patrulla", "unidad-medica", "supervisor"],
    "ALTA": ["patrulla", "supervisor"],
    "MEDIA": ["patrulla"],
    "BAJA": ["patrulla"],
}


class DispatchPlan(TypedDict):
    """Plan calculado para una alerta."""

    priority: str
    eta_min: int
    units: list[str]
    notify: str
    message: str


def plan_dispatch(alert: AlertIn) -> DispatchPlan:
    """Calcula prioridad, ETA, unidades y mensajes para una alerta."""
    priority = PRIORITY_BY_TYPE.get(alert.type.upper(), DEFAULT_PRIORITY)
    location = alert.location.address or "ubicación sin texto"
    if alert.location.lat is not None and alert.location.lon is not None:
        location += f" ({alert.location.lat:.5f}, {alert.location.lon:.5f})"

    eta_min = ETA_BY_PRIORITY[priority]
    units = list(UNITS_BY_PRIORITY[priority])
    message = (
        f"[{priority}] {alert.type} en {alert.client_id} :: {location}. "
        f"{alert.label} (conf {alert.confidence:.2f}, {alert.verified_by})"
    )
    return {
        "priority": priority,
        "eta_min": eta_min,
        "units": units,
        "notify": (
            f"Ayuda en camino: {', '.join(units)} "
            f"(ETA ~{eta_min} min). Ticket en camino."
        ),
        "message": message,
    }


def notify_units(ticket: str, alert: AlertIn, plan: DispatchPlan) -> bool:
    """Avisa a las unidades por webhook si está configurado.

    Best-effort: nunca lanza. Devuelve ``True`` si el webhook respondió 2xx.
    """
    url = os.getenv("DISPATCH_WEBHOOK_URL", "").strip()
    if not url:
        log.warning("DESPACHO %s: %s", ticket, plan["message"])
        return False

    body = {
        "ticket": ticket,
        "priority": plan["priority"],
        "eta_min": plan["eta_min"],
        "units": plan["units"],
        "message": plan["message"],
        "type": alert.type,
        "client_id": alert.client_id,
        "location": alert.location.model_dump(),
        "timestamp": alert.timestamp,
    }
    try:
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            ok = 200 <= response.status < 300
        log.warning("DESPACHO %s enviado por webhook: %s", ticket, plan["message"])
        return ok
    except Exception as exc:
        log.error(
            "fallo el webhook de despacho %s: %s (queda registrado en el log)",
            ticket,
            exc,
        )
        return False
