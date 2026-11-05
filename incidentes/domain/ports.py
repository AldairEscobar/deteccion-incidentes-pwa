"""Puertos del dominio (interfaces que implementa la infraestructura).

La regla de dependencia es unidireccional: ``application`` depende de estos
puertos y ``infrastructure`` los implementa. Así el caso de uso se puede
probar sin YOLO, sin cámara y sin red.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Protocol

from incidentes.domain.entities import (
    AlertAck,
    AlertPayload,
    ProcessingProgress,
    SourceState,
    Suspicion,
    Verdict,
    VideoInfo,
)


class IncidentDetector(ABC):
    """Analiza un frame y devuelve posibles incidentes (etapa 1)."""

    @abstractmethod
    def analyze(self, frame: Any) -> Any:
        """Devuelve un ``FrameAnalysis`` con el frame anotado y las sospechas."""
        raise NotImplementedError


class ViolenceVerifier(ABC):
    """Confirma o descarta una sospecha con un modelo de visión (etapa 2).

    El contrato exige **fail-closed**: ante error, timeout o respuesta
    inválida debe devolver ``Verdict(violent=False)``. La alarma solo suena
    con una confirmación positiva.
    """

    @property
    def name(self) -> str:
        """Identificador legible del verificador (ej. ``ollama/qwen2.5vl``)."""
        return "verifier"

    @abstractmethod
    def ping(self) -> bool:
        """Indica si el verificador está disponible. No debe lanzar."""
        raise NotImplementedError

    @abstractmethod
    def verify(self, jpeg_bytes: bytes, suspicion: Suspicion) -> Verdict:
        """Verifica una sospecha. No debe lanzar excepciones."""
        raise NotImplementedError


class VideoReader(ABC):
    """Puerto de lectura de video.

    Contrato según la fuente:

    - **Archivo:** ``read`` devuelve ``ok=False`` cuando se agota el video.
    - **Fuente en vivo:** ``ok=False`` solo debe ocurrir después de ``stop()``;
      las caídas se reintentan dentro del adaptador y no terminan la sesión.

    ``state`` es informativo para la presentación; el caso de uso no debe
    depender de él para decidir si continúa.
    """

    @abstractmethod
    def open(self, path: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def read(self) -> tuple[bool, Any]:
        """Devuelve ``(ok, frame)``; ``ok=False`` marca fin o parada."""
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError

    @property
    def state(self) -> SourceState:
        """Estado actual de la fuente (informativo)."""
        return SourceState.UNKNOWN

    def stop(self) -> None:
        """Pide a la fuente que deje de entregar frames.

        La implementación por defecto cierra la captura. Los adaptadores en
        vivo deben garantizar que un ``read`` pendiente termine en un tiempo
        acotado (timeout de lectura o watchdog).
        """
        self.close()


class VideoWriter(ABC):
    """Puerto de escritura del video anotado."""

    @abstractmethod
    def open(self, path: str, fps: int, width: int, height: int) -> None:
        raise NotImplementedError

    @abstractmethod
    def write(self, frame: Any) -> None:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError


class ProgressListener(ABC):
    """Puerto de notificación hacia la interfaz.

    La implementación de presentación es responsable de saltar al hilo de la
    GUI. El caso de uso puede invocar estos métodos desde un hilo worker.
    """

    @abstractmethod
    def on_progress(self, progress: ProcessingProgress, preview_frame: Any) -> None:
        raise NotImplementedError

    @abstractmethod
    def on_completed(self, output_path: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def on_error(self, message: str) -> None:
        raise NotImplementedError

    # Hooks opcionales: se dejan con implementación vacía para que un listener
    # que solo necesite progreso no tenga que implementarlos todos.
    def on_suspicion(self, suspicion: Suspicion) -> None:  # noqa: B027
        """YOLO propuso una sospecha y se envió a verificar."""

    def on_suspicion_discarded(self, suspicion: Suspicion, verdict: Verdict) -> None:  # noqa: B027
        """La IA descartó la sospecha; la bitácora puede mostrarlo."""

    def on_incident_alert(  # noqa: B027
        self, incident: Any, timestamp_sec: float, timestamp: str = ""
    ) -> None:
        """Se invoca por cada incidente confirmado (respetando el cooldown)."""

    def on_summary(self, summary: dict[str, Any]) -> None:  # noqa: B027
        """Resumen final con conteos y ruta del reporte JSON."""

    def on_alert_dispatched(self, ack: AlertAck, incident: Any) -> None:  # noqa: B027
        """La central respondió a una alerta (ticket, ETA, mensaje)."""

    def on_incident_closed(self, incident: Any, ticket: str) -> None:  # noqa: B027
        """La central marcó el ticket como atendido; la luz puede volver a verde."""


class AlertStatusReader(Protocol):
    """Consulta el estado actual de un ticket en la central."""

    def get_alert_status(self, ticket: str) -> str | None: ...


class AlertDispatcher(ABC):
    """Envía una alerta confirmada a la central.

    Contrato **fail-open**: nunca lanza. Si la red falla, devuelve
    ``AlertAck(delivered=False)`` y la detección local continúa.
    """

    @abstractmethod
    def dispatch(self, alert: AlertPayload) -> AlertAck:
        raise NotImplementedError


__all__ = [
    "AlertDispatcher",
    "AlertStatusReader",
    "IncidentDetector",
    "ProgressListener",
    "SourceState",
    "VideoInfo",
    "VideoReader",
    "VideoWriter",
    "ViolenceVerifier",
]
