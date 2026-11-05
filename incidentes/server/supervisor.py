"""Supervisor del análisis de cámaras dentro de la central (modo B).

En este modo la central no solo despacha: también **ejecuta** el pipeline de
dos etapas por cada cámara registrada desde la PWA. El diseño hexagonal se
mantiene intacto: el supervisor es un *composition root* que conecta el caso de
uso existente (``DetectIncidentsUseCase``) con los adaptadores de YOLO, Ollama
y OpenCV, y sustituye el adaptador HTTP por uno en proceso.

Cada cámara tiene su propio ``CameraRuntime``:

- un hilo que llama a ``use_case.execute(source, None, listener, live=True)``;
- un ``ProgressListener`` que publica la bitácora en el bus de eventos;
- un buffer del último frame anotado en JPEG para la vista previa (MJPEG).

No se escribe vídeo ni reporte: el modo vivo solo mantiene la sesión y emite
alertas, igual que hacía el cliente de escritorio.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from functools import partial
from typing import Any

from incidentes.application.detect_incidents import DetectIncidentsUseCase
from incidentes.config import AppConfig
from incidentes.domain.entities import (
    AlertAck,
    AlertPayload,
    DetectionConfig,
    Incident,
    SourceState,
    Suspicion,
    Verdict,
)
from incidentes.domain.ports import AlertDispatcher, ProgressListener
from incidentes.server.cameras import Camera, CameraStore, mask_source
from incidentes.server.events import EventBus
from incidentes.server.models import AlertIn, AlertRecord, Location

log = logging.getLogger(__name__)

# La vista previa se recodifica como mucho a esta frecuencia. Es independiente
# de los FPS del análisis: YOLO puede ir a 15 fps y la PWA a 6, que es de sobra
# para vigilar y ahorra CPU.
PREVIEW_FPS = 6.0
PREVIEW_MAX_WIDTH = 720
PREVIEW_QUALITY = 70

AlertRegistrar = Callable[[AlertIn], AlertRecord]
UseCaseFactory = Callable[[Camera, AlertRegistrar, EventBus], DetectIncidentsUseCase]


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Despacho en proceso
# ---------------------------------------------------------------------------
class InProcessDispatcher(AlertDispatcher):
    """Adaptador de alertas que escribe directamente en la central.

    Sustituye a ``HttpAlertDispatcher`` cuando el detector vive dentro del
    mismo proceso que el servidor: evita un POST a uno mismo y comparte la
    misma base de datos y el mismo bus de eventos que la API.
    """

    def __init__(
        self,
        camera: Camera,
        register: AlertRegistrar,
        bus: EventBus,
    ) -> None:
        self._camera = camera
        self._register = register
        self._bus = bus

    def _to_alert(self, payload: AlertPayload) -> AlertIn:
        snapshot_b64 = None
        if payload.snapshot_jpeg:
            snapshot_b64 = base64.b64encode(payload.snapshot_jpeg).decode("ascii")
        return AlertIn(
            client_id=self._camera.id,
            type=payload.type,
            confidence=payload.confidence,
            label=payload.label,
            frame=payload.frame,
            time_sec=payload.time_sec,
            video="",
            verified_by=payload.verified_by,
            reason=payload.reason,
            timestamp=payload.timestamp or _iso_now(),
            location=Location(
                lat=self._camera.lat,
                lon=self._camera.lon,
                address=self._camera.address,
            ),
            snapshot_b64=snapshot_b64,
        )

    def dispatch(self, alert: AlertPayload) -> AlertAck:
        """Registra la alerta y la difunde. Contrato fail-open: nunca lanza."""
        try:
            record = self._register(self._to_alert(alert))
        except Exception as exc:  # red de seguridad: el pipeline debe seguir
            log.exception("fallo al registrar la alerta en proceso")
            return AlertAck(False, detail=f"dispatch-error: {exc}"[:200])

        self._bus.publish_event("alerta", record.model_dump())
        return AlertAck(
            delivered=True,
            ticket=record.ticket,
            status=record.status,
            priority=record.priority,
            eta_min=record.eta_min,
            message=f"Ticket {record.ticket}.",
        )


# ---------------------------------------------------------------------------
# Listener del caso de uso
# ---------------------------------------------------------------------------
class _CameraListener(ProgressListener):
    """Traduce los eventos del caso de uso a mensajes para la PWA."""

    def __init__(self, runtime: CameraRuntime) -> None:
        self._runtime = runtime

    def on_progress(self, progress: Any, preview_frame: Any) -> None:
        self._runtime.handle_frame(preview_frame)

    def on_completed(self, output_path: str) -> None:
        self._runtime.handle_completed()

    def on_error(self, message: str) -> None:
        self._runtime.handle_error(message)

    def on_suspicion(self, suspicion: Suspicion) -> None:
        self._runtime.log_event(
            "info",
            f"YOLO propone {suspicion.type} "
            f"(frame {suspicion.frame}, conf {suspicion.heuristic_confidence:.2f})",
        )

    def on_suspicion_discarded(self, suspicion: Suspicion, verdict: Verdict) -> None:
        reason = verdict.reason or "sin detalles"
        self._runtime.log_event(
            "descarte", f"IA descarta {suspicion.type}: {reason}"
        )

    def on_incident_alert(
        self, incident: Incident, timestamp_sec: float, timestamp: str = ""
    ) -> None:
        self._runtime.log_event(
            "alerta",
            f"Incidente confirmado: {incident.type} ({incident.confidence:.2f})",
        )

    def on_alert_dispatched(self, ack: AlertAck, incident: Incident) -> None:
        if ack.delivered:
            eta = f", ETA {ack.eta_min} min" if ack.eta_min is not None else ""
            self._runtime.log_event(
                "alerta",
                f"Alerta {incident.type} enviada a la central: "
                f"ticket {ack.ticket}{eta}",
            )
        else:
            self._runtime.log_event(
                "error", f"Alerta no entregada: {ack.detail or 'sin detalle'}"
            )


# ---------------------------------------------------------------------------
# Ejecución de una cámara
# ---------------------------------------------------------------------------
class CameraRuntime:
    """Hilo de análisis, estado y vista previa de una cámara."""

    def __init__(
        self,
        camera: Camera,
        use_case_factory: Callable[[], DetectIncidentsUseCase],
        bus: EventBus,
    ) -> None:
        self.camera = camera
        self._factory = use_case_factory
        self._bus = bus

        self._thread: threading.Thread | None = None
        self._use_case: DetectIncidentsUseCase | None = None
        self._stop_event = threading.Event()
        self._finished = False
        self._error = ""
        self._started_at = ""
        self._last_state = ""
        self._last_event = ""

        self._cond = threading.Condition()
        self._jpeg: bytes | None = None
        self._seq = 0
        self._viewers = 0
        self._last_encode = 0.0

    # ------------------------------------------------------------------ estado
    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def status(self) -> dict[str, Any]:
        """Ficha de estado lista para la API."""
        if self._finished:
            state = "error" if self._error else "detenida"
            error = self._error
        elif self.running:
            use_case = self._use_case
            state = (
                use_case.source_state.value
                if use_case is not None
                else SourceState.CONNECTING.value
            )
            if state == SourceState.UNKNOWN.value:
                state = SourceState.CONNECTING.value
            error = ""
        elif self._error:
            state = "error"
            error = self._error
        else:
            state = "detenida"
            error = ""
        return {
            "id": self.camera.id,
            "name": self.camera.name,
            "source": mask_source(self.camera.source),
            "address": self.camera.address,
            "lat": self.camera.lat,
            "lon": self.camera.lon,
            "enabled": self.camera.enabled,
            "created_at": self.camera.created_at,
            "state": state,
            "running": self.running and not self._finished,
            "last_error": error,
            "last_event": self._last_event,
            "started_at": self._started_at,
        }

    def _publish_state(self) -> None:
        status = self.status()
        state = str(status["state"])
        if state == self._last_state:
            return
        self._last_state = state
        self._bus.publish_event("estado", status)

    def log_event(self, level: str, text: str) -> None:
        """Registra una línea de bitácora y la difunde a la PWA."""
        self._last_event = text
        self._bus.publish_event(
            "bitacora",
            {
                "camera_id": self.camera.id,
                "camera_name": self.camera.name,
                "level": level,
                "text": text,
                "at": _iso_now(),
            },
        )

    # ------------------------------------------------------------------ ciclo
    def start(self) -> bool:
        """Arranca el hilo de análisis. ``False`` si ya estaba corriendo."""
        if self.running:
            return False
        self._stop_event.clear()
        self._error = ""
        self._finished = False
        self._last_state = ""
        self._started_at = _iso_now()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"camera-{self.camera.id}",
        )
        self._thread.start()
        self._publish_state()
        return True

    def stop(self, timeout: float = 15.0) -> None:
        """Pide parar y espera al hilo (acotado)."""
        self._stop_event.set()
        use_case = self._use_case
        if use_case is not None:
            try:
                use_case.stop()
            except Exception:
                log.exception("fallo al detener la cámara %s", self.camera.id)
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)
        self._thread = None
        with self._cond:
            self._cond.notify_all()
        self._publish_state()

    def _run(self) -> None:
        try:
            use_case = self._factory()
            if self._stop_event.is_set():
                return
            self._use_case = use_case
            listener = _CameraListener(self)
            use_case.execute(self.camera.source, None, listener, live=True)
        except Exception as exc:
            self._error = str(exc) or exc.__class__.__name__
            if self._use_case is not None:
                # El caso de uso ya registró el traceback; aquí solo se marca
                # el estado para no duplicar el ruido en el log.
                log.error(
                    "cámara %s detenida por error: %s",
                    self.camera.id,
                    self._error,
                )
            else:
                log.exception(
                    "no se pudo iniciar el pipeline de la cámara %s",
                    self.camera.id,
                )
        finally:
            self._finished = True
            self._use_case = None
            self._publish_state()
            with self._cond:
                self._cond.notify_all()

    # -------------------------------------------------------------- callbacks
    def handle_frame(self, frame: Any) -> None:
        """Guarda una vista previa si hay espectadores y cambia el estado."""
        self._publish_state()
        if self._viewers <= 0 or frame is None:
            return
        now = time.monotonic()
        if now - self._last_encode < 1.0 / PREVIEW_FPS:
            return
        self._last_encode = now
        jpeg = _encode_preview(frame)
        if jpeg is None:
            return
        with self._cond:
            self._jpeg = jpeg
            self._seq += 1
            self._cond.notify_all()

    def handle_completed(self) -> None:
        self._finished = True
        with self._cond:
            self._cond.notify_all()
        self._publish_state()

    def handle_error(self, message: str) -> None:
        self._error = message
        self._finished = True
        self.log_event("error", f"Error del pipeline: {message}")
        with self._cond:
            self._cond.notify_all()
        self._publish_state()

    # ------------------------------------------------------------- vista previa
    def snapshot(self) -> bytes | None:
        """Último frame anotado en JPEG, o ``None`` si aún no hay."""
        with self._cond:
            return self._jpeg

    def mjpeg(self) -> Iterator[bytes]:
        """Generador MJPEG para ``<img src=...>`` en la PWA."""
        with self._cond:
            self._viewers += 1
            seq = self._seq
            jpeg = self._jpeg
        try:
            # Si ya había un frame, se manda de inmediato para no esperar.
            if jpeg is not None:
                yield _mjpeg_part(jpeg)
            while not self._stop_event.is_set() and not self._finished:
                with self._cond:
                    updated = self._cond.wait_for(
                        lambda expected=seq: self._seq != expected, timeout=2.0
                    )
                    if not updated:
                        continue
                    seq = self._seq
                    jpeg = self._jpeg
                if jpeg is not None:
                    yield _mjpeg_part(jpeg)
        finally:
            with self._cond:
                self._viewers = max(0, self._viewers - 1)


def _mjpeg_part(jpeg: bytes) -> bytes:
    return (
        b"--frame\r\n"
        b"Content-Type: image/jpeg\r\n"
        + f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii")
        + jpeg
        + b"\r\n"
    )


def _encode_preview(frame: Any) -> bytes | None:
    """Redimensiona el frame anotado y lo codifica como JPEG (nunca lanza)."""
    try:
        import cv2
    except Exception:
        return None
    try:
        height, width = frame.shape[:2]
        if width > PREVIEW_MAX_WIDTH:
            scale = PREVIEW_MAX_WIDTH / float(width)
            frame = cv2.resize(
                frame,
                (PREVIEW_MAX_WIDTH, max(1, int(height * scale))),
                interpolation=cv2.INTER_AREA,
            )
        ok, buffer = cv2.imencode(
            ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, PREVIEW_QUALITY]
        )
        return buffer.tobytes() if ok else None
    except Exception:
        log.debug("no se pudo codificar la vista previa", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------
class CameraSupervisor:
    """Gestiona el ciclo de vida de todas las cámaras registradas."""

    def __init__(
        self,
        store: CameraStore,
        bus: EventBus,
        register_alert: AlertRegistrar,
        use_case_factory: UseCaseFactory,
    ) -> None:
        self._store = store
        self._bus = bus
        self._register = register_alert
        self._factory = use_case_factory
        self._lock = threading.RLock()
        self._runtimes: dict[str, CameraRuntime] = {}

    # ------------------------------------------------------------------ estado
    @staticmethod
    def _camera_fields(camera: Camera) -> dict[str, Any]:
        return {
            "id": camera.id,
            "name": camera.name,
            "source": mask_source(camera.source),
            "address": camera.address,
            "lat": camera.lat,
            "lon": camera.lon,
            "enabled": camera.enabled,
            "created_at": camera.created_at,
        }

    def status(self, camera_id: str) -> dict[str, Any] | None:
        camera = self._store.get(camera_id)
        if camera is None:
            return None
        with self._lock:
            runtime = self._runtimes.get(camera_id)
        if runtime is not None:
            return runtime.status()
        return {
            **self._camera_fields(camera),
            "state": "detenida",
            "running": False,
            "last_error": "",
            "last_event": "",
            "started_at": "",
        }

    def statuses(self) -> list[dict[str, Any]]:
        cameras = self._store.list()
        return [self.status(camera.id) or {} for camera in cameras]

    def runtime(self, camera_id: str) -> CameraRuntime | None:
        with self._lock:
            return self._runtimes.get(camera_id)

    # ------------------------------------------------------------------- ciclo
    def start(self, camera_id: str) -> dict[str, Any] | None:
        """Arranca (o reutiliza) el pipeline de una cámara."""
        camera = self._store.get(camera_id)
        if camera is None:
            return None
        with self._lock:
            runtime = self._runtimes.get(camera_id)
            if runtime is None or not runtime.running:
                runtime = CameraRuntime(
                    camera,
                    partial(self._factory, camera, self._register, self._bus),
                    self._bus,
                )
                self._runtimes[camera_id] = runtime
        runtime.start()
        return runtime.status()

    def stop(self, camera_id: str) -> bool:
        """Detiene el pipeline y olvida el runtime (la cámara sigue guardada)."""
        with self._lock:
            runtime = self._runtimes.pop(camera_id, None)
        if runtime is None:
            return False
        runtime.stop()
        status = self.status(camera_id)
        if status is not None:
            self._bus.publish_event("estado", status)
        return True

    def delete(self, camera_id: str) -> bool:
        """Detiene y elimina la cámara del registro."""
        self.stop(camera_id)
        return self._store.delete(camera_id)

    def start_all(self) -> None:
        """Arranca todas las cámaras habilitadas (al iniciar la central)."""
        for camera in self._store.list():
            if camera.enabled:
                try:
                    self.start(camera.id)
                except Exception:
                    log.exception("no se pudo arrancar la cámara %s", camera.id)

    def stop_all(self) -> None:
        with self._lock:
            ids = list(self._runtimes)
        for camera_id in ids:
            try:
                self.stop(camera_id)
            except Exception:
                log.exception("no se pudo detener la cámara %s", camera_id)


# ---------------------------------------------------------------------------
# Composition root perezoso
# ---------------------------------------------------------------------------
def build_use_case_factory(config: AppConfig | None = None) -> UseCaseFactory:
    """Construye el pipeline real (YOLO + Ollama) bajo demanda.

    Las importaciones pesadas se hacen dentro de la fábrica para que arrancar
    la API (y los tests que la importan) no cargue Ultralytics ni OpenCV.
    """
    cfg = config or AppConfig.from_env()

    def factory(
        camera: Camera,
        register: AlertRegistrar,
        bus: EventBus,
    ) -> DetectIncidentsUseCase:
        from incidentes.infrastructure.detection.incident_detector import (
            YoloIncidentDetector,
        )
        from incidentes.infrastructure.verification.ollama_verifier import (
            OllamaViolenceVerifier,
        )
        from incidentes.infrastructure.video.frames import encode_snapshot, mark_pending
        from incidentes.infrastructure.video.opencv_reader import get_video_info
        from incidentes.infrastructure.video.sources import create_reader

        detection_config = DetectionConfig(
            model_path=cfg.model_path,
            person_class_id=cfg.person_class_id,
            confidence=cfg.confidence,
            motion_threshold=cfg.motion_threshold,
            proximity_factor=cfg.proximity_factor,
            min_persistence=cfg.suspicion_min_persistence,
            cooldown_frames=cfg.cooldown_frames,
        )
        detector = YoloIncidentDetector(
            detection_config,
            violence_model_path=cfg.violence_model_path,
            mark_confirmed=False,
        )
        verifier = OllamaViolenceVerifier(
            model=cfg.ollama_model,
            base_url=cfg.ollama_url,
            timeout_sec=cfg.ollama_timeout_sec,
        )
        return DetectIncidentsUseCase(
            suspicion_detector=detector,
            verifier=verifier,
            reader_factory=create_reader,
            writer_factory=None,
            info_provider=get_video_info,
            snapshot_encoder=partial(encode_snapshot, max_width=cfg.snapshot_width),
            pending_marker=mark_pending,
            config=detection_config,
            verify_cooldown_sec=cfg.verify_cooldown_sec,
            drain_timeout_sec=cfg.verify_drain_timeout_sec,
            alert_dispatcher=InProcessDispatcher(camera, register, bus),
            alert_once=False,
            status_reader=None,
        )

    return factory
