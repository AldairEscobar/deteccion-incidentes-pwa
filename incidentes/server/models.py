"""Contrato REST de la central (modelos Pydantic).

Los modelos son la frontera de validación de la API: cualquier cliente que
envíe datos fuera de rango recibe un 422 antes de tocar la lógica de despacho.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Location(BaseModel):
    """Ubicación reportada por el cliente."""

    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    address: str = ""


class AlertIn(BaseModel):
    """Alerta que envía el cliente (espejo de ``AlertPayload``)."""

    client_id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    label: str = ""
    frame: int = Field(default=0, ge=0)
    time_sec: float = Field(default=0.0, ge=0.0)
    video: str = ""
    verified_by: str = "yolo"
    reason: str = ""
    timestamp: str = ""
    location: Location = Field(default_factory=Location)
    snapshot_b64: str | None = None  # evidencia JPEG en base64 (opcional)


class AlertAck(BaseModel):
    """Respuesta al cliente: acuse + plan de ayuda despachado."""

    ticket: str
    status: str
    priority: str
    eta_min: int | None = None
    units: list[str] = Field(default_factory=list)
    message: str = ""
    received_at: str


class AlertRecord(BaseModel):
    """Alerta almacenada en la central, con su plan y su evidencia."""

    ticket: str
    status: str
    priority: str
    eta_min: int | None = None
    units: list[str] = Field(default_factory=list)
    received_at: str
    alert: AlertIn
    # Evidencia: la captura no viaja dentro del JSON para no duplicar el
    # binario; se sirve aparte desde estos metadatos.
    has_snapshot: bool = False
    snapshot_url: str = ""
    snapshot_sha256: str = ""


class StatusUpdate(BaseModel):
    """Cambio de estado de una alerta (p. ej. ``atendida``)."""

    status: str = Field(min_length=1)


class ClientRegister(BaseModel):
    """Datos de alta de un cliente (detector) en la central."""

    client_id: str = Field(min_length=1)
    name: str = ""
    address: str = ""
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    source_state: str = ""


class HeartbeatIn(BaseModel):
    """Latido de vida de un cliente, con el estado de su fuente de video."""

    client_id: str = Field(min_length=1)
    source_state: str = ""


class ClientInfo(BaseModel):
    """Ficha de un cliente conectado que expone la central."""

    client_id: str
    name: str = ""
    address: str = ""
    lat: float | None = None
    lon: float | None = None
    ip: str = ""
    connected_at: str = ""
    last_seen: str = ""
    online: bool = True
    source_state: str = "UNKNOWN"


# ---------------------------------------------------------------------------
# Cámaras registradas desde la PWA (modo central con análisis propio)
# ---------------------------------------------------------------------------
class CameraIn(BaseModel):
    """Datos para dar de alta una cámara en la central."""

    name: str = Field(min_length=1, max_length=80)
    source: str = Field(min_length=1, max_length=500)
    address: str = Field(default="", max_length=200)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    enabled: bool = True


class CameraUpdate(BaseModel):
    """Cambios parciales de una cámara; solo se aplican los campos enviados."""

    name: str | None = Field(default=None, min_length=1, max_length=80)
    source: str | None = Field(default=None, min_length=1, max_length=500)
    address: str | None = Field(default=None, max_length=200)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    enabled: bool | None = None


class CameraOut(BaseModel):
    """Cámara con su estado en vivo; la URL sale con la contraseña oculta."""

    id: str
    name: str
    source: str
    address: str = ""
    lat: float | None = None
    lon: float | None = None
    enabled: bool = True
    created_at: str = ""
    state: str = "detenida"
    running: bool = False
    last_error: str = ""
    last_event: str = ""
    started_at: str = ""
