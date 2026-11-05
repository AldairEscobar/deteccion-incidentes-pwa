"""Entidades y objetos de valor del dominio.

Este módulo no importa librerías externas a propósito: cualquier adaptador
(YOLO, OpenCV, Ollama, Tkinter, FastAPI) puede depender del dominio, pero el
dominio no depende de ninguno de ellos.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SourceState(StrEnum):
    """Estado de una fuente de video.

    Un archivo pasa a ``LIVE`` y termina al agotarse. Una fuente en vivo puede
    alternar entre ``LIVE``, ``NO_SIGNAL`` y ``RECONNECTING`` sin que la sesión
    termine.
    """

    UNKNOWN = "desconocido"
    CONNECTING = "conectando"
    LIVE = "en_vivo"
    NO_SIGNAL = "sin_senal"
    RECONNECTING = "reconectando"
    STOPPED = "detenido"


@dataclass(frozen=True)
class VideoInfo:
    """Metadatos de un video de entrada."""

    path: str
    total_frames: int
    fps: int
    width: int
    height: int


@dataclass(frozen=True)
class DetectionConfig:
    """Parámetros ajustables del detector de sospechas.

    Los valores por defecto están pensados para ``yolo11n.pt`` (COCO) y videos
    de vigilancia con personas a distancia media.
    """

    model_path: str = "yolo11n.pt"
    person_class_id: int = 0
    confidence: float = 0.5

    # Clases COCO usadas como señales indirectas de violencia.
    weapon_class_ids: tuple[int, ...] = (34, 43, 76)  # bate, cuchillo, tijeras
    bag_class_ids: tuple[int, ...] = (24, 26, 28)  # mochila, bolso, maleta

    motion_threshold: float = 12.0  # media de |diff| en 0-255
    proximity_factor: float = 0.7  # distancia entre centros / tamaño medio
    min_persistence: int = 6  # frames consecutivos para confirmar una señal
    cooldown_frames: int = 30  # separación mínima entre eventos del mismo tipo
    fall_aspect_ratio: float = 1.15  # caja más ancha que alta => posible caída
    min_person_area: int = 4000  # ignora detecciones diminutas (ruido)


class IncidentType:
    """Etiquetas de tipo de incidente compartidas por todas las capas.

    Se usan constantes de cadena (y no un ``Enum``) porque viajan tal cual en
    los mensajes JSON del contrato REST.
    """

    FIGHT = "PELEA"
    WEAPON = "ARMA"
    FALL = "CAIDA"
    ROBBERY = "ROBO"
    VIOLENCE = "VIOLENCIA"
    NOTHING = "NADA"

    ALL = frozenset({FIGHT, WEAPON, FALL, ROBBERY, VIOLENCE})


@dataclass(frozen=True)
class ProcessingProgress:
    """Avance del procesamiento de un video."""

    current_frame: int
    total_frames: int

    @property
    def percentage(self) -> int:
        """Porcentaje completado (0-100), seguro ante totales inválidos."""
        if self.total_frames <= 0:
            return 0
        return min(100, int(self.current_frame / self.total_frames * 100))


@dataclass(frozen=True)
class Incident:
    """Incidente detectado por la primera etapa (aún sin verificar por IA)."""

    type: str
    confidence: float  # 0.0-1.0; heurística, no probabilidad calibrada
    label: str


@dataclass(frozen=True)
class FrameAnalysis:
    """Resultado de analizar un frame.

    ``annotated_frame`` es una imagen BGR lista para el writer o la vista. Se
    tipa como ``Any`` para no acoplar el dominio a NumPy/OpenCV.
    """

    annotated_frame: Any
    incidents: tuple[Incident, ...] = ()
    alert: bool = False

    def __post_init__(self) -> None:
        # Normaliza cualquier iterable a tupla para mantener la inmutabilidad.
        object.__setattr__(self, "incidents", tuple(self.incidents))


@dataclass(frozen=True)
class Suspicion:
    """Sospecha enviada a la segunda etapa de verificación."""

    frame: int
    time_sec: float
    type: str
    label: str
    heuristic_confidence: float = 0.0
    timestamp: str = ""  # ISO-8601; hora real del frame, no del veredicto


@dataclass(frozen=True)
class Verdict:
    """Veredicto del verificador de IA local."""

    violent: bool
    confidence: float
    type: str
    reason: str = ""


@dataclass(frozen=True)
class IncidentEvent:
    """Incidente confirmado, listo para el reporte y para la central."""

    frame: int
    time_sec: float
    type: str
    confidence: float
    label: str
    verified_by: str = "yolo"
    reason: str = ""
    timestamp: str = ""  # ISO-8601; se hereda de la sospecha que lo originó


@dataclass(frozen=True)
class GeoLocation:
    """Ubicación del cliente que reporta; todos los campos son opcionales."""

    lat: float | None = None
    lon: float | None = None
    address: str = ""


@dataclass(frozen=True)
class AlertPayload:
    """Alerta confirmada lista para enviarse a la central por REST.

    No incluye ``client_id`` ni ubicación: eso es configuración del adaptador
    de salida (``HttpAlertDispatcher``), no del incidente detectado.
    """

    type: str
    confidence: float
    label: str
    frame: int
    time_sec: float
    video: str
    verified_by: str = "yolo"
    reason: str = ""
    timestamp: str = ""  # ISO-8601 UTC; el adaptador lo completa si falta
    snapshot_jpeg: bytes | None = field(default=None, repr=False)


@dataclass(frozen=True)
class AlertAck:
    """Respuesta de la central a una alerta.

    ``delivered=False`` significa que la alerta no se pudo entregar; el
    pipeline local debe continuar (fail-open).
    """

    delivered: bool
    ticket: str = ""
    status: str = ""
    priority: str = ""
    eta_min: int | None = None
    message: str = ""
    detail: str = ""
