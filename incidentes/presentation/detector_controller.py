"""Controlador del cliente: une la vista Tkinter con el caso de uso.

Traduce los eventos del caso de uso a la bitácora, la luz de alerta y la
alarma sonora. También sondea el estado de la fuente durante el monitoreo para
mostrar "sin señal" y las reconexiones sin bloquear la interfaz.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from collections.abc import Callable
from tkinter import filedialog
from typing import Any

from incidentes.application.detect_incidents import DetectIncidentsUseCase
from incidentes.domain.entities import (
    AlertAck,
    ProcessingProgress,
    SourceState,
    Suspicion,
    Verdict,
)
from incidentes.domain.ports import ProgressListener
from incidentes.infrastructure.audio.alarm import sound_alarm
from incidentes.infrastructure.video.sources import is_live_source
from incidentes.presentation.detector_view import DetectorView

log = logging.getLogger(__name__)

POLL_INTERVAL_MS = 500
ALARM_COOLDOWN_SEC = 20.0


class TkProgressListener(ProgressListener):
    """Traduce eventos del caso de uso a la vista y a la alarma sonora."""

    def __init__(
        self,
        view: DetectorView,
        *,
        live: bool,
        muted: Callable[[], bool],
        central_enabled: bool,
        closing: Callable[[], bool],
    ) -> None:
        self._view = view
        self._live = live
        self._muted = muted
        self._central_enabled = central_enabled
        self._closing = closing
        self._alert_state = "idle"
        self._active = True
        self._open_tickets: set[str] = set()
        # Alertas confirmadas cuya respuesta de la central aún no llegó, y
        # alertas que se quedaron sin ticket (central caída o modo local).
        self._pending_alerts = 0
        self._local_alerts = 0
        self._last_alarm = float("-inf")
        self._fps_at: float | None = None
        self._fps_frames = 0

    # ------------------------------------------------------------------ video
    def on_progress(self, progress: ProcessingProgress, preview_frame: Any) -> None:
        try:
            if self._live:
                self._update_fps(progress.current_frame)
            else:
                self._view.show_progress(
                    progress.current_frame, progress.total_frames
                )
            self._view.show_frame(preview_frame)
        except Exception:
            log.exception(
                "fallo al actualizar el progreso del frame %d",
                progress.current_frame,
            )

    def _update_fps(self, current: int) -> None:
        now = time.monotonic()
        if self._fps_at is None:
            self._fps_at = now
            self._fps_frames = current
            return
        elapsed = now - self._fps_at
        if elapsed < 1.0:
            return
        fps = max(0.0, (current - self._fps_frames) / elapsed)
        self._view.set_live_stats(fps)
        self._fps_at = now
        self._fps_frames = current

    # --------------------------------------------------------------- bitácora
    def on_suspicion(self, suspicion: Suspicion) -> None:
        log.info(
            "sospecha YOLO: %s (%.2f) @ %s",
            suspicion.type,
            suspicion.heuristic_confidence,
            suspicion.timestamp,
        )
        self._view.add_log(
            "suspicion",
            f"SOSPECHA {suspicion.type} "
            f"({suspicion.heuristic_confidence:.0%}) · {suspicion.label}",
            suspicion.timestamp,
        )
        if self._alert_state != "alert":
            self._alert_state = "verifying"
            self._view.set_alert_state(
                "verifying", f"{suspicion.type} · verificando con IA"
            )

    def on_suspicion_discarded(self, suspicion: Suspicion, verdict: Verdict) -> None:
        log.info(
            "sospecha descartada por IA: %s :: %s",
            suspicion.type,
            verdict.reason[:100],
        )
        self._view.add_log(
            "discarded",
            f"DESCARTADA {suspicion.type} · {verdict.reason[:90]}",
            suspicion.timestamp,
        )
        if self._alert_state == "verifying":
            self._alert_state = "idle"
            self._view.set_alert_state("idle")

    def on_incident_alert(
        self, incident: Any, timestamp_sec: float, timestamp: str = ""
    ) -> None:
        self._alert_state = "alert"
        log.warning(
            "VIOLENCIA DETECTADA @ %.1fs (%s) :: %s",
            timestamp_sec,
            timestamp or "-",
            incident.label,
        )
        self._view.add_log(
            "confirmed",
            f"CONFIRMADA {incident.type} ({incident.confidence:.0%}) · "
            f"{incident.label}",
            timestamp,
        )
        self._view.set_alert_state(
            "alert", f"{incident.type} · {timestamp[11:19] or timestamp}"
        )
        self._pending_alerts += 1

        now = time.monotonic()
        if not self._muted() and now - self._last_alarm >= ALARM_COOLDOWN_SEC:
            self._last_alarm = now
            sound_alarm(times=3)

    def on_alert_dispatched(self, ack: AlertAck, incident: Any) -> None:
        if self._pending_alerts > 0:
            self._pending_alerts -= 1
        if ack.delivered:
            if ack.ticket:
                self._open_tickets.add(ack.ticket)
            else:
                # Entregada sin ticket: no hay cierre automático posible.
                self._local_alerts += 1
            log.warning(
                "la central respondió: ticket=%s eta=%s min (%s)",
                ack.ticket,
                ack.eta_min,
                ack.status,
            )
            self._view.add_log(
                "central",
                f"CENTRAL · ticket {ack.ticket} · {ack.status}"
                + (f" · ETA {ack.eta_min} min" if ack.eta_min is not None else "")
                + (f" · {ack.message}" if ack.message else ""),
            )
            self._view.show_central_response(
                ack.message or f"Ayuda en camino (ticket {ack.ticket})"
            )
        else:
            self._local_alerts += 1
            log.error("la central no confirmó la alerta: %s", ack.detail)
            self._view.add_log(
                "error", f"CENTRAL sin respuesta · {ack.detail}"
            )
            self._view.show_central_response(
                f"no se pudo entregar ({ack.detail})", error=True
            )
        self._view.set_central_badge(self._central_enabled, ack.delivered)

    def on_incident_closed(self, incident: Any, ticket: str) -> None:
        """La central cerró el ticket: la luz vuelve a verde si no queda otro."""
        if not self._active:
            return
        self._open_tickets.discard(ticket)
        self._view.add_log(
            "central",
            f"CENTRAL · ticket {ticket} atendido; alerta cerrada",
        )
        if (
            not self._open_tickets
            and self._pending_alerts == 0
            and self._local_alerts == 0
        ):
            self._alert_state = "idle"
            self._view.set_alert_state("idle")

    def acknowledge(self) -> None:
        """Reconocimiento local (modo sin central): apaga la luz roja."""
        self._open_tickets.clear()
        self._pending_alerts = 0
        self._local_alerts = 0
        if self._active:
            self._alert_state = "idle"
            self._view.set_alert_state("idle")

    def deactivate(self) -> None:
        """Ignora cierres tardíos de una sesión anterior."""
        self._active = False

    # --------------------------------------------------------------- cierre
    def on_completed(self, output_path: str) -> None:
        self._view.show_completed(output_path)

    def on_summary(self, summary: dict[str, Any]) -> None:
        log.warning(
            "resultado de la sesión: eventos=%d, descartes=%s, reporte=%s",
            len(summary.get("events", [])),
            summary.get("discarded"),
            summary.get("report") or "(sin reporte)",
        )
        self._view.show_summary(summary)

    def on_error(self, message: str) -> None:
        log.error("error del caso de uso: %s", message)
        if self._closing():
            return
        self._view.show_error(message)


class DetectorController:
    """Coordina la vista con el caso de uso en un hilo de fondo."""

    def __init__(
        self,
        view: DetectorView,
        use_case: DetectIncidentsUseCase,
        *,
        output_filename: str = "resultado_incidentes.mp4",
        ai_available: bool = True,
        central_enabled: bool = True,
        central_available: bool = False,
    ) -> None:
        self._view = view
        self._use_case = use_case
        self._output_filename = output_filename
        self._central_enabled = central_enabled
        self._file_path = ""
        self._source = ""
        self._live = False
        self._running = False
        self._closing = False
        self._muted = False
        self._poll_job: str | None = None
        self._last_state: SourceState | None = None
        self._worker: threading.Thread | None = None
        self._listener: TkProgressListener | None = None

        self._view.bind(
            on_select_file=self.select_file,
            on_start_file=self.start_file,
            on_connect_camera=self.connect_camera,
            on_stop=self.stop_session,
            on_mute=self.toggle_mute,
            on_recognize=self.recognize_alert,
        )
        self._view.set_ai_badge(ai_available)
        self._view.set_central_badge(central_enabled, central_available)
        self._view.set_connection_state(SourceState.UNKNOWN)
        self._view.set_alert_state("idle")
        self._view.set_muted(False)

    # ------------------------------------------------------------------ fuentes
    def select_file(self) -> None:
        """Abre el diálogo de selección del MP4 de demostración."""
        if self._running:
            return
        try:
            file_path = filedialog.askopenfilename(
                title="Selecciona un archivo de video",
                filetypes=[("Archivos MP4", "*.mp4"), ("Todos los archivos", "*.*")],
            )
        except Exception:
            log.exception("fallo al abrir el diálogo de archivo")
            return

        if file_path:
            self._file_path = file_path
            log.info("video seleccionado: %s", file_path)
            self._view.show_selected_file(os.path.basename(file_path))
            self._view.set_source_label(f"Archivo: {os.path.basename(file_path)}")

    def start_file(self) -> None:
        """Inicia la demo con el MP4 seleccionado."""
        if self._running:
            return
        if not self._file_path:
            self._notify("Selecciona primero un MP4 de demostración.")
            return
        self._start(self._file_path, live=False)

    def connect_camera(self) -> None:
        """Inicia el monitoreo de la URL de cámara IP escrita por el operador."""
        if self._running:
            return
        url = self._view.get_camera_url()
        if not url:
            self._notify("Escribe la URL de la cámara (http://, https:// o rtsp://).")
            return
        if not is_live_source(url):
            self._notify("La URL debe empezar por http://, https://, rtsp:// o udp://.")
            return
        self._start(url, live=True)

    def _start(self, source: str, *, live: bool) -> None:
        self._running = True
        self._live = live
        self._source = source
        self._last_state = SourceState.CONNECTING
        log.info("iniciando sesión %s: %s", "en vivo" if live else "demo", source)

        if self._listener is not None:
            self._listener.deactivate()
            self._listener = None

        self._view.reset_alert()
        label = f"Cámara IP: {source}" if live else f"Archivo: {os.path.basename(source)}"
        self._view.set_source_label(label)
        self._view.set_connection_state(
            SourceState.CONNECTING if live else SourceState.LIVE
        )
        self._view.set_monitoring(True, live=live)
        self._view.add_log(
            "system",
            "conectando con la cámara…" if live else "iniciando demo…",
        )

        output_path = (
            None
            if live
            else DetectIncidentsUseCase.resolve_output_path(
                source, self._output_filename
            )
        )
        listener = TkProgressListener(
            self._view,
            live=live,
            muted=lambda: self._muted,
            central_enabled=self._central_enabled,
            closing=lambda: self._closing,
        )
        self._listener = listener
        self._worker = threading.Thread(
            target=self._run,
            args=(source, output_path, listener, live),
            daemon=True,
            name="detector-worker",
        )
        self._worker.start()
        if live:
            self._schedule_poll()

    def _run(
        self,
        source: str,
        output_path: str | None,
        listener: TkProgressListener,
        live: bool,
    ) -> None:
        try:
            self._use_case.execute(source, output_path, listener, live=live)
        except Exception:
            log.exception("fallo la sesión %s", source)
        finally:
            self._finish()

    # ----------------------------------------------------------------- control
    def stop_session(self) -> None:
        """Detiene el monitoreo en vivo (equivale a apagar la cámara)."""
        if not self._running or not self._live:
            return
        self._view.set_status("Deteniendo el monitoreo…")
        self._use_case.stop()

    def toggle_mute(self) -> None:
        self._muted = not self._muted
        self._view.set_muted(self._muted)
        self._view.add_log(
            "system",
            "sonido silenciado" if self._muted else "sonido activado",
        )

    def recognize_alert(self) -> None:
        if self._listener is not None:
            self._listener.acknowledge()
        else:
            self._view.reset_alert()
        self._view.add_log("system", "alerta reconocida por el operador")

    def shutdown(self) -> None:
        """Cierra la sesión en curso antes de destruir la ventana."""
        self._closing = True
        self._cancel_poll()
        if not self._running:
            return
        self._use_case.stop()
        if self._worker is not None:
            self._worker.join(timeout=2)

    # ------------------------------------------------------------- seguimiento
    def _schedule_poll(self) -> None:
        self._cancel_poll()
        self._poll_source()

    def _poll_source(self) -> None:
        if not self._running or not self._live:
            return
        try:
            state = self._use_case.source_state
        except Exception:
            state = SourceState.UNKNOWN

        if state != self._last_state:
            self._view.set_connection_state(state)
            message = {
                SourceState.CONNECTING: "conectando con la cámara…",
                SourceState.LIVE: "cámara en vivo",
                SourceState.NO_SIGNAL: "cámara sin señal; se reintentará",
                SourceState.RECONNECTING: "reconectando cámara…",
                SourceState.STOPPED: "fuente detenida",
            }.get(state)
            if message:
                self._view.add_log("system", message)
            self._last_state = state

        try:
            self._poll_job = self._view.root.after(
                POLL_INTERVAL_MS, self._poll_source
            )
        except Exception:
            self._poll_job = None

    def _cancel_poll(self) -> None:
        if self._poll_job is None:
            return
        with contextlib.suppress(Exception):
            self._view.root.after_cancel(self._poll_job)
        self._poll_job = None

    def _finish(self) -> None:
        if not self._running:
            return
        self._running = False
        self._cancel_poll()
        self._view.set_monitoring(False, live=self._live)
        self._view.set_connection_state(SourceState.STOPPED)

    def _notify(self, message: str) -> None:
        self._view.set_status(message)
        self._view.add_log("system", message)
