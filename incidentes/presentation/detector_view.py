"""Vista Tkinter del cliente detector.

Estructura pensada para monitoreo continuo: panel de video, estado de la
fuente, luz de alerta de tres estados y bitácora de eventos. La vista es
"tonta": no conoce el caso de uso, OpenCV ni la red; solo construye widgets y
expone métodos para que el controlador los actualice.
"""

from __future__ import annotations

import contextlib
import time
import tkinter as tk
from collections.abc import Callable
from datetime import datetime
from tkinter import messagebox, ttk
from typing import Any

import cv2
from PIL import Image, ImageTk

from incidentes.domain.entities import SourceState

GREEN = "#27AE60"
AMBER = "#F39C12"
RED = "#C0392B"
BLUE = "#0B5394"
GRAY = "#7F8C8D"
AMBER_BG = "#FDEBD0"
AMBER_FG = "#7E5109"


class DetectorView:
    """Ventana del detector: video, estado de conexión y bitácora."""

    PREVIEW_SIZE = (640, 360)
    MAX_LOG_ENTRIES = 200
    FRAME_INTERVAL_SEC = 0.05

    def __init__(self, root: tk.Tk, client_id: str = "", address: str = "") -> None:
        self.root = root
        self.client_id = client_id
        self.address = address
        self._bg = root.cget("bg")
        self._callbacks: dict[str, Callable[[], None]] = {}
        self._last_frame_at = 0.0

        suffix = f" - {client_id}" if client_id else ""
        self.root.title(f"Monitoreo de incidentes (YOLO){suffix}")
        self.root.geometry("1180x740")
        self.root.minsize(980, 640)

        self._build_header(client_id, address)
        self._build_body()
        self._build_status()

        self._update_mode()

    # ------------------------------------------------------------------ widgets
    def _build_header(self, client_id: str, address: str) -> None:
        header = tk.Frame(self.root)
        header.pack(fill=tk.X, padx=16, pady=(10, 2))

        tk.Label(
            header,
            text="Monitoreo de incidentes violentos",
            font=("Arial", 16, "bold"),
        ).pack(side=tk.LEFT)

        badges = tk.Frame(header)
        badges.pack(side=tk.RIGHT)
        self.badge_source = self._badge(badges, "Fuente", GRAY)
        self.badge_ai = self._badge(badges, "IA", GRAY)
        self.badge_central = self._badge(badges, "Central", GRAY)

        identity = (
            f"{client_id}  ·  {address}"
            if client_id
            else (address or "Cámara sin identificar")
        )
        self.lbl_identity = tk.Label(
            self.root, text=identity, font=("Arial", 10, "italic"), fg=BLUE
        )
        self.lbl_identity.pack(anchor=tk.W, padx=18)

    def _build_body(self) -> None:
        main = tk.Frame(self.root)
        main.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)

        # El panel derecho se empaqueta primero para reservar su ancho fijo;
        # si el video se empaqueta antes con expand=True, lo recorta.
        right = tk.Frame(main, width=370)
        right.pack(side=tk.RIGHT, fill=tk.Y, padx=(12, 0))
        right.pack_propagate(False)

        left = tk.Frame(main)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.video_frame = tk.Frame(left, bg="black")
        self.video_frame.pack(fill=tk.BOTH, expand=True)
        self.video_label = tk.Label(
            self.video_frame,
            bg="black",
            fg="#AAAAAA",
            font=("Arial", 12),
            text="Sin señal",
        )
        self.video_label.pack(fill=tk.BOTH, expand=True)

        meta = tk.Frame(left)
        meta.pack(fill=tk.X, pady=(6, 0))
        self.lbl_source = tk.Label(
            meta, text="Sin fuente", font=("Arial", 9), anchor=tk.W
        )
        self.lbl_source.pack(side=tk.LEFT)
        self.lbl_conn = tk.Label(
            meta, text="detenida", font=("Arial", 9, "bold"), fg=GRAY
        )
        self.lbl_conn.pack(side=tk.LEFT, padx=(16, 0))
        self.lbl_stats = tk.Label(meta, text="", font=("Arial", 9), fg=BLUE)
        self.lbl_stats.pack(side=tk.RIGHT)

        self.progress = ttk.Progressbar(
            left, orient="horizontal", mode="determinate"
        )
        self.progress.pack(fill=tk.X, pady=6)

        self._build_alert_panel(right)
        self._build_source_panel(right)
        self._build_actions(right)
        self._build_log(right)

    def _build_alert_panel(self, parent: tk.Frame) -> None:
        status = tk.Frame(parent)
        status.pack(fill=tk.X, pady=(0, 4))

        self.light = tk.Canvas(
            status, width=24, height=24, highlightthickness=0, bg=self._bg
        )
        self.light.pack(side=tk.LEFT)
        self._light_item = self.light.create_oval(
            3, 3, 21, 21, fill=GREEN, outline=""
        )
        self.lbl_alert_state = tk.Label(
            status, text="Sin alertas", font=("Arial", 11, "bold"), fg=GREEN
        )
        self.lbl_alert_state.pack(side=tk.LEFT, padx=8)

        self.lbl_alert = tk.Label(
            parent,
            text="",
            font=("Arial", 10, "bold"),
            bg=self._bg,
            fg=RED,
            wraplength=340,
            justify=tk.CENTER,
        )
        self.lbl_alert.pack(fill=tk.X, pady=(0, 6))

    def _build_source_panel(self, parent: tk.Frame) -> None:
        box = tk.LabelFrame(
            parent, text="Fuente", font=("Arial", 9, "bold"), padx=8, pady=6
        )
        box.pack(fill=tk.X, pady=(0, 6))

        modes = tk.Frame(box)
        modes.pack(fill=tk.X)
        self.mode_var = tk.StringVar(value="camera")
        self.radio_camera = tk.Radiobutton(
            modes,
            text="Cámara IP",
            variable=self.mode_var,
            value="camera",
            command=self._update_mode,
        )
        self.radio_camera.pack(side=tk.LEFT)
        self.radio_file = tk.Radiobutton(
            modes,
            text="Archivo (demo)",
            variable=self.mode_var,
            value="file",
            command=self._update_mode,
        )
        self.radio_file.pack(side=tk.LEFT, padx=(8, 0))

        self.camera_frame = tk.Frame(box)
        self.url_entry = tk.Entry(self.camera_frame)
        self.url_entry.pack(fill=tk.X)
        self.btn_connect = tk.Button(
            self.camera_frame,
            text="Conectar cámara",
            command=self._fire("on_connect_camera"),
            bg=BLUE,
            fg="white",
            pady=4,
        )
        self.btn_connect.pack(fill=tk.X, pady=(4, 0))

        self.file_frame = tk.Frame(box)
        file_row = tk.Frame(self.file_frame)
        file_row.pack(fill=tk.X)
        self.btn_select_file = tk.Button(
            file_row,
            text="Seleccionar MP4",
            command=self._fire("on_select_file"),
        )
        self.btn_select_file.pack(side=tk.LEFT)
        self.lbl_file = tk.Label(
            file_row,
            text="Ningún archivo",
            font=("Arial", 8, "italic"),
            anchor=tk.W,
        )
        self.lbl_file.pack(side=tk.LEFT, padx=6, fill=tk.X, expand=True)
        self.btn_start_file = tk.Button(
            self.file_frame,
            text="Iniciar demo (MP4)",
            command=self._fire("on_start_file"),
            bg=GREEN,
            fg="white",
            pady=4,
        )
        self.btn_start_file.pack(fill=tk.X, pady=(4, 0))

        self.camera_frame.pack(fill=tk.X, pady=(4, 0))

    def _build_actions(self, parent: tk.Frame) -> None:
        actions = tk.Frame(parent)
        actions.pack(fill=tk.X, pady=(0, 6))
        self.btn_stop = tk.Button(
            actions,
            text="Detener",
            command=self._fire("on_stop"),
            state=tk.DISABLED,
        )
        self.btn_stop.pack(side=tk.LEFT)
        self.btn_mute = tk.Button(
            actions,
            text="Silenciar",
            command=self._fire("on_mute"),
        )
        self.btn_mute.pack(side=tk.LEFT, padx=6)
        self.btn_ack = tk.Button(
            actions,
            text="Reconocer",
            command=self._fire("on_recognize"),
        )
        self.btn_ack.pack(side=tk.LEFT)

    def _build_log(self, parent: tk.Frame) -> None:
        box = tk.LabelFrame(
            parent,
            text="Seguimiento de alerta",
            font=("Arial", 9, "bold"),
            padx=6,
            pady=6,
        )
        box.pack(fill=tk.BOTH, expand=True)

        self.log = tk.Text(
            box,
            height=18,
            width=40,
            state=tk.DISABLED,
            wrap=tk.WORD,
            bg="#FAFAFA",
            relief=tk.FLAT,
            font=("Consolas", 8),
        )
        scroll = ttk.Scrollbar(box, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.log.tag_configure("suspicion", foreground="#B9770E")
        self.log.tag_configure("discarded", foreground=GRAY)
        self.log.tag_configure(
            "confirmed", foreground=RED, font=("Consolas", 8, "bold")
        )
        self.log.tag_configure("central", foreground="#1E8449")
        self.log.tag_configure("system", foreground=BLUE)
        self.log.tag_configure("error", foreground=RED)

    def _build_status(self) -> None:
        ttk.Separator(self.root, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=12)
        self.lbl_status = tk.Label(
            self.root,
            text="Sin fuente. Conecta una cámara IP o carga un MP4 de demo.",
            font=("Arial", 9),
            anchor=tk.W,
        )
        self.lbl_status.pack(fill=tk.X, padx=16, pady=(4, 8))

    @staticmethod
    def _badge(parent: tk.Frame, text: str, color: str) -> tk.Label:
        label = tk.Label(
            parent, text=f"● {text}", font=("Arial", 9, "bold"), fg=color
        )
        label.pack(side=tk.LEFT, padx=(0, 10))
        return label

    @staticmethod
    def _set_badge(label: tk.Label, text: str, color: str) -> None:
        label.config(text=f"● {text}", fg=color)

    # ------------------------------------------------------------- integración
    def bind(
        self,
        *,
        on_select_file: Callable[[], None] | None = None,
        on_start_file: Callable[[], None] | None = None,
        on_connect_camera: Callable[[], None] | None = None,
        on_stop: Callable[[], None] | None = None,
        on_mute: Callable[[], None] | None = None,
        on_recognize: Callable[[], None] | None = None,
    ) -> None:
        """Conecta botones con el controlador (la vista no lo importa)."""
        callbacks = {
            "on_select_file": on_select_file,
            "on_start_file": on_start_file,
            "on_connect_camera": on_connect_camera,
            "on_stop": on_stop,
            "on_mute": on_mute,
            "on_recognize": on_recognize,
        }
        self._callbacks.update(
            {name: cb for name, cb in callbacks.items() if cb is not None}
        )

    def _fire(self, name: str) -> Callable[[], None]:
        return lambda: self._callbacks.get(name, lambda: None)()

    def _update_mode(self) -> None:
        if self.mode_var.get() == "file":
            self.camera_frame.pack_forget()
            self.file_frame.pack(fill=tk.X, pady=(4, 0))
        else:
            self.file_frame.pack_forget()
            self.camera_frame.pack(fill=tk.X, pady=(4, 0))

    def _run_on_ui(self, callback: Callable[[], None]) -> None:
        with contextlib.suppress(tk.TclError):
            self.root.after(0, callback)  # la ventana pudo haberse cerrado

    # ----------------------------------------------------------------- consulta
    def get_mode(self) -> str:
        return self.mode_var.get()

    def get_camera_url(self) -> str:
        return self.url_entry.get().strip()

    # ------------------------------------------------------------------ estado
    def show_selected_file(self, file_name: str) -> None:
        def apply() -> None:
            self.lbl_file.config(text=f"Archivo: {file_name}")
            self.lbl_status.config(text="Demo lista. Pulsa «Iniciar demo (MP4)».")

        self._run_on_ui(apply)

    def set_source_label(self, text: str) -> None:
        self._run_on_ui(lambda: self.lbl_source.config(text=text))

    def set_connection_state(self, state: SourceState) -> None:
        mapping = {
            SourceState.UNKNOWN: ("sin fuente", GRAY),
            SourceState.CONNECTING: ("conectando", AMBER),
            SourceState.LIVE: ("activa", GREEN),
            SourceState.NO_SIGNAL: ("sin señal", RED),
            SourceState.RECONNECTING: ("reconectando", AMBER),
            SourceState.STOPPED: ("detenida", GRAY),
        }
        text, color = mapping.get(state, ("desconocida", GRAY))

        def apply() -> None:
            self.lbl_conn.config(text=text, fg=color)
            self._set_badge(self.badge_source, "Fuente", color)

        self._run_on_ui(apply)

    def set_ai_badge(self, available: bool) -> None:
        color = GREEN if available else RED
        text = "IA" if available else "IA off"
        self._run_on_ui(lambda: self._set_badge(self.badge_ai, text, color))

    def set_central_badge(self, enabled: bool, available: bool) -> None:
        if not enabled:
            self._run_on_ui(
                lambda: self._set_badge(self.badge_central, "Central off", GRAY)
            )
            return
        color = GREEN if available else RED
        text = "Central" if available else "Central sin enlace"
        self._run_on_ui(lambda: self._set_badge(self.badge_central, text, color))

    def set_alert_state(self, state: str, text: str = "") -> None:
        """``state``: ``idle``, ``verifying`` o ``alert``."""
        colors = {"idle": GREEN, "verifying": AMBER, "alert": RED}
        labels = {
            "idle": "Sin alertas",
            "verifying": "Verificando sospecha",
            "alert": "ALERTA CONFIRMADA",
        }
        color = colors.get(state, GREEN)
        label = labels.get(state, "Sin alertas")

        def apply() -> None:
            self.light.itemconfig(self._light_item, fill=color)
            self.lbl_alert_state.config(text=label, fg=color)
            if state == "alert":
                self.lbl_alert.config(
                    text=f"ALARMA: {text}", bg=RED, fg="white"
                )
            elif state == "verifying":
                self.lbl_alert.config(
                    text=f"SOSPECHA: {text}", bg=AMBER_BG, fg=AMBER_FG
                )
            else:
                self.lbl_alert.config(text="", bg=self._bg)

        self._run_on_ui(apply)

    def set_muted(self, muted: bool) -> None:
        text = "Activar sonido" if muted else "Silenciar"
        self._run_on_ui(lambda: self.btn_mute.config(text=text))

    def set_monitoring(self, monitoring: bool, live: bool) -> None:
        state = tk.DISABLED if monitoring else tk.NORMAL
        widgets = (
            self.radio_camera,
            self.radio_file,
            self.url_entry,
            self.btn_connect,
            self.btn_select_file,
            self.btn_start_file,
        )

        def apply() -> None:
            for widget in widgets:
                widget.config(state=state)
            self.btn_stop.config(
                state=tk.NORMAL if (monitoring and live) else tk.DISABLED
            )
            if live:
                self.progress.pack_forget()
            else:
                self.progress.pack(fill=tk.X, pady=6)
            if monitoring:
                self.lbl_status.config(
                    text="Monitoreando en vivo."
                    if live
                    else "Procesando demo; el video se guarda al terminar."
                )

        self._run_on_ui(apply)

    def set_live_stats(self, fps: float) -> None:
        self._run_on_ui(
            lambda: self.lbl_stats.config(text=f"en vivo · {fps:.1f} fps")
        )

    def set_status(self, text: str) -> None:
        self._run_on_ui(lambda: self.lbl_status.config(text=text))

    # ------------------------------------------------------------------ video
    def show_frame(self, bgr_frame: Any) -> None:
        """Convierte el frame a RGB en el hilo worker y publica el PhotoImage.

        En vivo llegan más frames de los que la interfaz puede pintar; se
        limitan a ~20 fps para que la cola de ``after`` no crezca sin control.
        """
        now = time.monotonic()
        if now - self._last_frame_at < self.FRAME_INTERVAL_SEC:
            return
        self._last_frame_at = now

        rgb = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)
        rgb = cv2.resize(rgb, self.PREVIEW_SIZE)
        image = ImageTk.PhotoImage(image=Image.fromarray(rgb))

        def apply() -> None:
            self.video_label.imgtk = image  # referencia para evitar el GC
            self.video_label.config(image=image, text="")

        self._run_on_ui(apply)

    def show_progress(self, current: int, total: int) -> None:
        def apply() -> None:
            if total > 0:
                self.progress.config(maximum=total, value=current)
                self.lbl_stats.config(text=f"frame {current} de {total}")
            else:
                self.lbl_stats.config(text=f"frame {current}")

        self._run_on_ui(apply)

    # ---------------------------------------------------------------- alertas
    def show_central_response(self, text: str, error: bool = False) -> None:
        """Muestra el acuse de la central o el fallo de entrega."""
        background = RED if error else "#1E8449"
        message = f"SIN RESPUESTA: {text}" if error else text

        def apply() -> None:
            self.lbl_alert.config(text=message, bg=background, fg="white")

        self._run_on_ui(apply)

    def add_log(self, kind: str, message: str, timestamp: str = "") -> None:
        """Añade una entrada a la bitácora (acotada a las últimas N)."""
        if timestamp and "T" in timestamp:
            hour = timestamp.split("T", 1)[1][:8]
        else:
            hour = timestamp or datetime.now().strftime("%H:%M:%S")

        def apply() -> None:
            self.log.config(state=tk.NORMAL)
            self.log.insert(tk.END, f"{hour}  {message}\n", kind)
            lines = int(self.log.index("end-1c").split(".")[0])
            if lines > self.MAX_LOG_ENTRIES:
                self.log.delete("1.0", f"{lines - self.MAX_LOG_ENTRIES}.0")
            self.log.see(tk.END)
            self.log.config(state=tk.DISABLED)

        self._run_on_ui(apply)

    def show_completed(self, output_path: str) -> None:
        if output_path:
            text = f"Demo finalizada. Video guardado en: {output_path}"
        else:
            text = "Sesión de monitoreo finalizada."
        self.set_status(text)

    def show_summary(self, summary: dict[str, Any]) -> None:
        """Registra el resumen de la sesión; en demo muestra el detalle."""
        duration = self._format_duration(summary.get("duration_sec", 0))
        line = (
            f"SESIÓN {summary.get('mode', '')}: "
            f"{summary.get('total_frames', 0)} frames · "
            f"{summary.get('suspicions_yolo', 0)} sospechas · "
            f"{summary.get('discarded', 0)} descartes · "
            f"{summary.get('incident_frames', 0)} confirmadas · {duration}"
        )
        self.add_log("system", line, summary.get("finished_at", ""))
        self.set_ai_badge(bool(summary.get("verifier_available", True)))

        if summary.get("mode") != "archivo":
            return

        events = summary.get("events", [])
        body = (
            f"Confirmadas: {len(events)}\n"
            f"Descartadas por IA: {summary.get('discarded', 0)}\n"
            f"Duración: {duration}\n\n"
            f"Video: {summary.get('output', '')}\n"
            f"Reporte: {summary.get('report', '')}"
        )
        self._run_on_ui(
            lambda: messagebox.showinfo("Demo finalizada", body)
        )

    def show_error(self, message: str) -> None:
        self.add_log("error", f"ERROR: {message}")
        self._run_on_ui(lambda: messagebox.showerror("Error", message))

    def reset_alert(self) -> None:
        self.set_alert_state("idle")

    @staticmethod
    def _format_duration(seconds: Any) -> str:
        try:
            total = int(float(seconds))
        except (TypeError, ValueError):
            total = 0
        hours, remainder = divmod(max(0, total), 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours:d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"
