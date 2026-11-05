"""Caso de uso principal: detección de incidentes en dos etapas.

Etapa 1 (rápida, síncrona)
    El detector analiza cada frame y propone **sospechas**. Se anota el video
    para que el operador vea qué está pasando, pero todavía no hay alarma.

Etapa 2 (lenta, asíncrona)
    Un hilo worker envía la sospecha a un verificador de visión local. Solo un
    veredicto ``violent=True`` produce un evento, dispara la alarma y envía la
    alerta a la central. El video ya escrito no se reescribe: la confirmación
    llega por el listener y por el reporte JSON.

El caso de uso no conoce YOLO, OpenCV, Ollama ni Tkinter. Las operaciones de
imagen (snapshot y overlay) se inyectan desde el *composition root*.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from incidentes.domain.entities import (
    AlertPayload,
    DetectionConfig,
    Incident,
    IncidentEvent,
    ProcessingProgress,
    SourceState,
    Suspicion,
    VideoInfo,
)
from incidentes.domain.ports import (
    AlertDispatcher,
    AlertStatusReader,
    IncidentDetector,
    ProgressListener,
    VideoReader,
    VideoWriter,
    ViolenceVerifier,
)

log = logging.getLogger(__name__)

# Cola acotada: si la IA no da abasto, es mejor descartar una sospecha que
# acumular frames y consumir memoria sin límite.
JOB_QUEUE_SIZE = 2

# Estados con los que la central da una alerta por cerrada. La comparación no
# distingue mayúsculas para tolerar cambios en el contrato.
CLOSED_TICKET_STATES = frozenset({"atendida", "cerrada", "resuelta"})
# Una alerta abierta no se vigila para siempre: 6 h es de sobra para el turno.
TICKET_WATCH_MAX_SEC = 6 * 3600.0


@dataclass(frozen=True)
class _VerificationJob:
    """Sospecha empaquetada para el hilo de verificación."""

    jpeg: bytes
    suspicion: Suspicion


@dataclass
class _RunState:
    """Estado compartido entre el hilo principal y el worker (bajo ``lock``)."""

    pending: int = 0
    alert_sent: bool = False
    discarded: int = 0


class DetectIncidentsUseCase:
    """Orquesta detección, verificación, reporte y alerta a la central."""

    def __init__(
        self,
        suspicion_detector: IncidentDetector,
        verifier: ViolenceVerifier,
        reader_factory: Callable[[str], VideoReader],
        writer_factory: Callable[[], VideoWriter] | None,
        info_provider: Callable[[str], VideoInfo],
        snapshot_encoder: Callable[[Any], bytes],
        pending_marker: Callable[[Any, int], None],
        *,
        config: DetectionConfig | None = None,
        verify_cooldown_sec: float = 8.0,
        drain_timeout_sec: float = 120.0,
        alert_dispatcher: AlertDispatcher | None = None,
        alert_once: bool = True,
        status_reader: AlertStatusReader | None = None,
        ticket_poll_interval_sec: float = 10.0,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._detector = suspicion_detector
        self._verifier = verifier
        self._reader_factory = reader_factory
        self._writer_factory = writer_factory
        self._info_provider = info_provider
        self._snapshot_encoder = snapshot_encoder
        self._pending_marker = pending_marker
        self._config = config or DetectionConfig()
        self._verify_cooldown = verify_cooldown_sec
        self._drain_timeout = drain_timeout_sec
        self._dispatcher = alert_dispatcher  # None => solo alerta local
        self._alert_once = alert_once
        self._status_reader = status_reader
        self._ticket_poll = max(0.01, ticket_poll_interval_sec)
        self._ticket_lock = threading.Lock()
        self._watched_tickets: set[str] = set()
        self._now = now or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic
        self._stop_event = threading.Event()
        self._stop_requested = False
        self._reader: VideoReader | None = None
        self._reader_lock = threading.Lock()
        self._live_session = False
        self._last_source_state = SourceState.UNKNOWN

    # --------------------------------------------------------------- control
    def stop(self) -> None:
        """Pide detener la sesión en curso.

        Solo tiene efecto en modo vivo: el lector devuelve ``ok=False`` y el
        bucle termina tras drenar las verificaciones pendientes. Es seguro
        llamarlo desde el hilo de la interfaz.
        """
        self._stop_requested = True
        self._stop_event.set()
        if not self._live_session:
            return
        with self._reader_lock:
            reader = self._reader
        if reader is None:
            return
        try:
            reader.stop()
        except Exception:
            log.exception("fallo al detener la fuente de video")

    @property
    def source_state(self) -> SourceState:
        """Estado de la fuente activa (informativo para la interfaz).

        Cuando la sesión terminó conserva el último estado: ``STOPPED`` tras
        una cámara y ``UNKNOWN`` tras un archivo de demo.
        """
        with self._reader_lock:
            reader = self._reader
        if reader is None:
            return self._last_source_state
        return reader.state

    # ------------------------------------------------------------------ rutas
    @staticmethod
    def resolve_output_path(video_path: str, filename: str) -> str:
        """Coloca el video procesado junto al video de entrada."""
        return str(Path(video_path).with_name(filename))

    @staticmethod
    def resolve_report_path(output_path: str) -> str:
        """Deriva el nombre del reporte JSON a partir del video de salida."""
        path = Path(output_path)
        return str(path.with_name(f"{path.stem}_reporte.json"))

    # ------------------------------------------------------------------ alerta
    def _dispatch_alert(
        self,
        event: IncidentEvent,
        snapshot: bytes,
        video_path: str,
        listener: ProgressListener,
    ) -> None:
        """Envía la alerta confirmada a la central desde un hilo aparte.

        El puerto ``AlertDispatcher`` no lanza excepciones; el ``try`` es una
        red de seguridad para que un fallo inesperado nunca tumbe el worker.
        """
        if self._dispatcher is None:
            return

        payload = AlertPayload(
            type=event.type,
            confidence=event.confidence,
            label=event.label,
            frame=event.frame,
            time_sec=event.time_sec,
            video="" if self._live_session else video_path,
            verified_by=event.verified_by,
            reason=event.reason,
            timestamp=event.timestamp
            or self._now().isoformat(timespec="seconds"),
            snapshot_jpeg=snapshot,
        )
        try:
            ack = self._dispatcher.dispatch(payload)
        except Exception:
            log.exception("fallo inesperado al despachar la alerta a la central")
            return

        if ack.delivered:
            log.warning(
                "alerta enviada a la central: ticket=%s estado=%s eta=%s min",
                ack.ticket,
                ack.status,
                ack.eta_min,
            )
        else:
            log.error("alerta NO entregada a la central: %s", ack.detail)

        incident = Incident(event.type, event.confidence, event.label)
        try:
            listener.on_alert_dispatched(ack, incident)
        except Exception:
            log.exception("fallo al notificar a la UI la respuesta de la central")

        if ack.delivered and ack.ticket and self._status_reader is not None:
            self._watch_ticket_async(ack.ticket, incident, listener)

    # ------------------------------------------------------ cierre de ticket
    def _watch_ticket_async(
        self, ticket: str, incident: Incident, listener: ProgressListener
    ) -> None:
        """Vigila en segundo plano el cierre del ticket en la central."""
        with self._ticket_lock:
            if ticket in self._watched_tickets:
                return
            self._watched_tickets.add(ticket)
        threading.Thread(
            target=self._watch_ticket,
            args=(ticket, incident, listener),
            daemon=True,
            name=f"ticket-watch-{ticket}",
        ).start()

    def _watch_ticket(
        self, ticket: str, incident: Incident, listener: ProgressListener
    ) -> None:
        """Sondea el estado del ticket hasta que la central lo da por atendido.

        Sobrevive al fin de la sesión: la alerta sigue abierta aunque el
        operador haya detenido la cámara. El hilo es daemon y termina como muy
        tarde a las ``TICKET_WATCH_MAX_SEC``.
        """
        if self._status_reader is None:
            return
        deadline = time.monotonic() + TICKET_WATCH_MAX_SEC
        try:
            while time.monotonic() < deadline:
                try:
                    status = self._status_reader.get_alert_status(ticket)
                except Exception:
                    log.exception("fallo al consultar el ticket %s", ticket)
                    status = None
                if status and status.strip().lower() in CLOSED_TICKET_STATES:
                    log.warning(
                        "ticket %s atendido por la central; se notifica a la UI",
                        ticket,
                    )
                    try:
                        listener.on_incident_closed(incident, ticket)
                    except Exception:
                        log.exception(
                            "fallo al notificar el cierre del ticket %s", ticket
                        )
                    return
                time.sleep(self._ticket_poll)
        finally:
            with self._ticket_lock:
                self._watched_tickets.discard(ticket)

    # ------------------------------------------------------------------ worker
    def _verification_worker(
        self,
        jobs: queue.Queue[_VerificationJob | None],
        lock: threading.Lock,
        state: _RunState,
        events: list[IncidentEvent],
        counts: dict[str, int],
        last_event_frame: dict[str, int],
        listener: ProgressListener,
        video_path: str,
        verifier_name: str,
        live: bool,
    ) -> None:
        """Consume sospechas y las verifica hasta recibir el centinela ``None``."""
        while True:
            job = jobs.get()
            if job is None:
                jobs.task_done()
                return
            try:
                self._verify_one(
                    job, lock, state, events, counts, last_event_frame,
                    listener, video_path, verifier_name, live,
                )
            except Exception:
                log.exception(
                    "fallo el worker de verificación (frame=%s)",
                    job.suspicion.frame,
                )
            finally:
                with lock:
                    state.pending -= 1
                jobs.task_done()

    def _verify_one(
        self,
        job: _VerificationJob,
        lock: threading.Lock,
        state: _RunState,
        events: list[IncidentEvent],
        counts: dict[str, int],
        last_event_frame: dict[str, int],
        listener: ProgressListener,
        video_path: str,
        verifier_name: str,
        live: bool,
    ) -> None:
        """Verifica una sospecha y, si se confirma, genera evento y alerta."""
        suspicion = job.suspicion
        log.info(
            "enviando sospecha a IA: frame=%d t=%.1fs %s",
            suspicion.frame,
            suspicion.time_sec,
            suspicion.label,
        )
        verdict = self._verifier.verify(job.jpeg, suspicion)
        log.info(
            "veredicto IA: violent=%s tipo=%s conf=%.2f :: %s",
            verdict.violent,
            verdict.type,
            verdict.confidence,
            verdict.reason[:100],
        )

        confirmed: IncidentEvent | None = None
        if not verdict.violent:
            with lock:
                state.discarded += 1
            try:
                listener.on_suspicion_discarded(suspicion, verdict)
            except Exception:
                log.exception("fallo al notificar la sospecha descartada")
            return

        with lock:
            previous = last_event_frame.get(verdict.type, -10**9)
            if suspicion.frame - previous >= self._config.cooldown_frames:
                last_event_frame[verdict.type] = suspicion.frame
                counts[verdict.type] = counts.get(verdict.type, 0) + 1
                confirmed = IncidentEvent(
                    frame=suspicion.frame,
                    time_sec=round(suspicion.time_sec, 2),
                    type=verdict.type,
                    confidence=round(verdict.confidence, 3),
                    label=f"CONFIRMADO IA: {verdict.type} ({verdict.reason})",
                    verified_by=verifier_name,
                    reason=verdict.reason,
                    timestamp=suspicion.timestamp,
                )
                events.append(confirmed)

        if confirmed is None:
            return

        # La UI se notifica fuera del lock: un callback lento no bloquea al worker.
        try:
            listener.on_incident_alert(
                Incident(confirmed.type, confirmed.confidence, confirmed.label),
                confirmed.time_sec,
                confirmed.timestamp,
            )
        except Exception:
            log.exception("fallo al notificar el incidente a la UI")

        # Petición de ayuda a la central, sin bloquear la verificación. En vivo
        # cada incidente se despacha (limitado por el cooldown); en archivo se
        # mantiene el comportamiento de "una alerta por corrida" si así se pide.
        should_dispatch = True
        if self._alert_once and not live:
            with lock:
                should_dispatch = not state.alert_sent
                if should_dispatch:
                    state.alert_sent = True
        if should_dispatch:
            threading.Thread(
                target=self._dispatch_alert,
                args=(confirmed, job.jpeg, video_path, listener),
                daemon=True,
                name="alert-dispatch",
            ).start()

    # ------------------------------------------------------------------ cierre
    @staticmethod
    def _stop_worker(
        jobs: queue.Queue[_VerificationJob | None],
        worker: threading.Thread,
    ) -> None:
        """Envía el centinela y espera al worker, descartando trabajos si hace falta."""
        if not worker.is_alive():
            return
        while True:
            try:
                jobs.put_nowait(None)
                break
            except queue.Full:
                # El worker está ocupado: libera un hueco descartando la cola.
                try:
                    jobs.get_nowait()
                    jobs.task_done()
                except queue.Empty:
                    break
        worker.join(timeout=5)

    def _build_summary(
        self,
        video_path: str,
        output_path: str | None,
        total_frames: int,
        suspicions: int,
        events: list[IncidentEvent],
        counts: dict[str, int],
        verifier_name: str,
        verifier_available: bool,
        discarded: int,
        *,
        live: bool,
        started_at: datetime,
        finished_at: datetime,
    ) -> dict[str, Any]:
        """Construye el resumen de la sesión y, si hay salida, el reporte JSON."""
        duration = max(0.0, (finished_at - started_at).total_seconds())
        summary: dict[str, Any] = {
            "mode": "vivo" if live else "archivo",
            "video": video_path,
            "output": output_path or "",
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": finished_at.isoformat(timespec="seconds"),
            "duration_sec": round(duration, 1),
            "total_frames": total_frames,
            "suspicions_yolo": suspicions,
            "discarded": discarded,
            "incident_frames": len(events),  # solo eventos confirmados por IA
            "counts": dict(counts),
            "verifier": verifier_name,
            "verifier_available": verifier_available,
            "events": [asdict(event) for event in events],
            "report": "",
        }
        if output_path:
            report_path = self.resolve_report_path(output_path)
            Path(report_path).write_text(
                json.dumps(summary, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            summary["report"] = report_path
        return summary

    # ------------------------------------------------------------------ público
    def execute(
        self,
        video_path: str,
        output_path: str | None,
        listener: ProgressListener,
        *,
        live: bool = False,
    ) -> dict[str, Any]:
        """Procesa un archivo o una sesión en vivo y devuelve su resumen.

        En modo vivo no se escriben video ni reporte: la sesión continúa hasta
        ``stop`` o hasta que la fuente deje de entregar frames. En modo archivo
        se mantiene el comportamiento anterior (video anotado + reporte JSON).
        """
        self._stop_event.clear()
        # Si el operador pulsó "detener" antes de que arrancara el worker, la
        # petición no debe perderse al limpiar el evento.
        if self._stop_requested:
            self._stop_event.set()
        self._stop_requested = False
        self._live_session = live
        self._last_source_state = (
            SourceState.CONNECTING if live else SourceState.UNKNOWN
        )
        reader = self._reader_factory(video_path)
        writer = None
        if not live and output_path and self._writer_factory is not None:
            writer = self._writer_factory()
        verifier_available = self._verifier.ping()
        verifier_name = self._verifier.name

        jobs: queue.Queue[_VerificationJob | None] = queue.Queue(maxsize=JOB_QUEUE_SIZE)
        lock = threading.Lock()
        state = _RunState()
        events: list[IncidentEvent] = []
        counts: dict[str, int] = {}
        last_event_frame: dict[str, int] = {}
        worker = threading.Thread(
            target=self._verification_worker,
            args=(
                jobs, lock, state, events, counts, last_event_frame,
                listener, video_path, verifier_name, live,
            ),
            daemon=True,
            name="ai-verifier",
        )

        total = 0
        fps = 30
        suspicions = 0
        frames_read = 0
        last_sent = float("-inf")
        started_at = self._now()
        started_mono = self._monotonic()

        try:
            info = None
            if not live:
                info = self._info_provider(video_path)
                total = info.total_frames
                fps = info.fps or 30

            reader.open(video_path)
            with self._reader_lock:
                self._reader = reader

            if writer is not None and info is not None:
                writer.open(
                    output_path or "", info.fps, info.width, info.height
                )
            worker.start()

            while True:
                if live and self._stop_event.is_set():
                    break
                ok, frame = reader.read()
                if not ok:
                    break
                frames_read += 1
                analysis = self._detector.analyze(frame)
                annotated = analysis.annotated_frame

                if live:
                    elapsed = self._monotonic() - started_mono
                    time_sec = round(max(0.0, elapsed), 2)
                else:
                    time_sec = round(frames_read / fps, 2)
                timestamp = self._now().isoformat(timespec="seconds")

                if analysis.incidents and verifier_available:
                    suspicions += 1
                    self._pending_marker(annotated, len(analysis.incidents))
                    now = time.monotonic()
                    if now - last_sent >= self._verify_cooldown:
                        best = max(analysis.incidents, key=lambda i: i.confidence)
                        snapshot = self._snapshot_encoder(frame)
                        if snapshot:
                            suspicion = Suspicion(
                                frame=frames_read,
                                time_sec=time_sec,
                                type=best.type,
                                label=best.label,
                                heuristic_confidence=round(best.confidence, 3),
                                timestamp=timestamp,
                            )
                            job = _VerificationJob(
                                jpeg=snapshot, suspicion=suspicion
                            )
                            # Incrementa antes de encolar para que el worker no
                            # decremente "pending" antes de que exista el trabajo.
                            with lock:
                                state.pending += 1
                            try:
                                jobs.put_nowait(job)
                                last_sent = now
                            except queue.Full:
                                with lock:
                                    state.pending -= 1
                                log.debug(
                                    "IA saturada: sospecha descartada (frame %d)",
                                    frames_read,
                                )
                            else:
                                try:
                                    listener.on_suspicion(suspicion)
                                except Exception:
                                    log.exception(
                                        "fallo al notificar la sospecha"
                                    )

                if writer is not None:
                    writer.write(annotated)
                try:
                    listener.on_progress(
                        ProcessingProgress(frames_read, 0 if live else total),
                        annotated,
                    )
                except Exception:
                    log.exception("fallo on_progress en el frame %d", frames_read)

            log.info(
                "sesión %s: %d frames, %d sospechas; esperando veredictos IA...",
                "en vivo" if live else "de archivo",
                frames_read,
                suspicions,
            )
            deadline = time.monotonic() + self._drain_timeout
            while time.monotonic() < deadline:
                with lock:
                    drained = state.pending <= 0
                if drained and jobs.empty():
                    break
                time.sleep(0.2)

            with lock:
                still_pending = state.pending
            if still_pending:
                log.warning(
                    "quedan %d verificaciones sin terminar; se cierra igual",
                    still_pending,
                )

            self._stop_worker(jobs, worker)

            summary = self._build_summary(
                video_path=video_path,
                output_path=None if live else output_path,
                total_frames=frames_read if live else total,
                suspicions=suspicions,
                events=events,
                counts=counts,
                verifier_name=verifier_name,
                verifier_available=verifier_available,
                discarded=state.discarded,
                live=live,
                started_at=started_at,
                finished_at=self._now(),
            )
            log.warning(
                "fin (%s): violencia=%s, eventos=%d, reporte=%s",
                "vivo" if live else "archivo",
                "SI" if events else "NO",
                len(events),
                summary["report"] or "(sin reporte)",
            )
            listener.on_completed("" if live else (output_path or ""))
            listener.on_summary(summary)
            return summary

        except Exception as exc:
            log.exception("fallo execute source=%s live=%s", video_path, live)
            try:
                listener.on_error(str(exc))
            except Exception:
                log.exception("fallo al notificar el error a la UI")
            raise
        finally:
            self._stop_worker(jobs, worker)
            with self._reader_lock:
                self._reader = None
            self._live_session = False
            self._stop_requested = False
            for resource in (reader, writer):
                if resource is None:
                    continue
                try:
                    resource.close()
                except Exception:
                    log.exception("fallo al cerrar un recurso del pipeline")
            self._last_source_state = (
                SourceState.STOPPED if live else SourceState.UNKNOWN
            )
