"""Dashboard de mando de la central.

Muestra en tiempo real las alertas recibidas, los clientes conectados y dos
gráficas (por tipo de incidente y por cliente). El servidor REST se levanta en
un hilo aparte; la GUI solo **lee** el estado compartido con un ``QTimer``,
respetando la regla de Qt de no tocar widgets desde otros hilos.
"""

from __future__ import annotations

import json
import logging
import time

import matplotlib

matplotlib.use("Qt5Agg")  # debe fijarse antes de importar pyplot

import matplotlib.pyplot as plt
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from incidentes.central.server_thread import RestServerThread
from incidentes.server.app import registry, store

log = logging.getLogger(__name__)

MAX_VISIBLE_ALERTS = 500
POLL_INTERVAL_MS = 800
START_CHECK_MS = 200
START_MAX_ATTEMPTS = 25

# Estilos reutilizados por los botones de arranque/parada.
BUTTON_START = (
    "background-color:#28A745;color:white;font-weight:bold;"
    "padding:6px 14px;border-radius:4px;"
)
BUTTON_STOP = (
    "background-color:#C0392B;color:white;font-weight:bold;"
    "padding:6px 14px;border-radius:4px;"
)
BUTTON_INFO = (
    "background-color:#1F6FEB;color:white;font-weight:bold;"
    "padding:6px 14px;border-radius:4px;"
)

plt.style.use("dark_background")

# Estado de fuente que reporta el cliente en su heartbeat.
SOURCE_LABELS = {
    "LIVE": ("cámara en vivo", "#39ff14"),
    "CONNECTING": ("conectando", "#ffa64d"),
    "RECONNECTING": ("reconectando", "#ffa64d"),
    "NO_SIGNAL": ("SIN SEÑAL", "#ff4d4d"),
    "STOPPED": ("cámara detenida", "#8b949e"),
    "UNKNOWN": ("sin fuente", "#8b949e"),
}


def source_display(state: str) -> tuple[str, str]:
    """Etiqueta y color para el estado de fuente de un cliente."""
    return SOURCE_LABELS.get(state.upper(), (state.lower() or "desconocida", "#8b949e"))


class DashboardWindow(QMainWindow):
    """Ventana principal de la central."""

    def __init__(self, host: str, port: int) -> None:
        super().__init__()
        self._host = host
        self._port = port
        self._server: RestServerThread | None = None
        self._start_attempts = 0
        self._seen_tickets: set[str] = set()
        self._seen_clients: set[str] = set()
        self._client_states: dict[str, str] = {}
        self._client_count = 0
        self._camera_count = 0

        self.setWindowTitle("🚨 CENTRAL DE ALERTAS C5 - Dashboard de Mando")
        self.resize(1280, 720)
        self.setStyleSheet("background-color: #121212; color: #ffffff;")

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        self._build_toolbar(root)
        self._build_body(root)

        self._draw_charts({}, {})
        self.table.setRowCount(0)

        # Sondeo periódico del estado compartido desde el hilo de la GUI.
        self.timer = QTimer(self)
        self.timer.setInterval(POLL_INTERVAL_MS)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()

        self.append_log("Panel de la central listo. Pulsa 'Iniciar central'.")

    # ------------------------------------------------------------------ UI
    def _build_toolbar(self, root: QVBoxLayout) -> None:
        bar = QHBoxLayout()

        self.btn_server = QPushButton("Iniciar central")
        self.btn_server.clicked.connect(self.toggle_server)
        self.btn_server.setStyleSheet(BUTTON_START)
        bar.addWidget(self.btn_server)

        self.lbl_status = QLabel("Central detenida")
        self.lbl_status.setStyleSheet("color:#ffa64d;font-weight:bold;")
        bar.addWidget(self.lbl_status)
        bar.addStretch(1)

        self.lbl_summary = QLabel("Clientes: 0   |   Alertas: 0")
        self.lbl_summary.setStyleSheet(
            "color:#39ff14;font-family:Consolas,monospace;"
        )
        bar.addWidget(self.lbl_summary)
        root.addLayout(bar)

    def _build_body(self, root: QVBoxLayout) -> None:
        body = QHBoxLayout()
        root.addLayout(body, stretch=1)

        left = QVBoxLayout()
        body.addLayout(left, stretch=1)
        alerts_header = QHBoxLayout()
        alerts_header.addWidget(QLabel("Alertas recibidas"))
        alerts_header.addStretch(1)
        self.btn_evidence = QPushButton("Ver evidencia")
        self.btn_evidence.setStyleSheet(BUTTON_INFO)
        self.btn_evidence.setEnabled(False)
        self.btn_evidence.clicked.connect(self.show_evidence)
        alerts_header.addWidget(self.btn_evidence)
        self.btn_attend = QPushButton("Marcar atendida")
        self.btn_attend.setStyleSheet(BUTTON_START)
        self.btn_attend.setEnabled(False)
        self.btn_attend.clicked.connect(self.attend_selected)
        alerts_header.addWidget(self.btn_attend)
        left.addLayout(alerts_header)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Ticket", "Cliente", "Tipo", "Descripción", "Hora", "Estado"]
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)  # Descripción
        header.setStyleSheet(
            "QHeaderView::section{background:#1e1e1e;color:#39ff14;"
            "padding:5px;border:0;}"
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setStyleSheet(
            "QTableWidget{background:#0d1117;color:#e6e6e6;"
            "gridline-color:#30363d;border:1px solid #30363d;}"
            "QTableWidget::item{padding:4px;}"
        )
        self.table.itemSelectionChanged.connect(self._update_attend_button)
        left.addWidget(self.table, stretch=2)

        left.addWidget(QLabel("Consola de eventos"))
        self.console = QTextEdit()
        self.console.setReadOnly(True)
        self.console.setStyleSheet(
            "QTextEdit{background:#0d1117;color:#39ff14;"
            "font-family:Consolas,monospace;font-size:13px;"
            "border:1px solid #30363d;}"
        )
        left.addWidget(self.console, stretch=2)

        right = QVBoxLayout()
        body.addLayout(right, stretch=1)

        self.lbl_clients = QLabel("Clientes conectados: 0")
        right.addWidget(self.lbl_clients)

        self.client_list = QListWidget()
        self.client_list.setStyleSheet(
            "QListWidget{background:#11111b;color:#89b4fa;"
            "font-family:Consolas,monospace;border:1px solid #313244;}"
            "QListWidget::item{background:#11111b;color:#89b4fa;padding:3px;}"
            "QListWidget::item:selected{background:#313244;color:#cdd6f4;}"
        )
        self.client_list.viewport().setStyleSheet("background:#11111b;")
        self.client_list.setMaximumHeight(160)
        right.addWidget(self.client_list)

        self.figure, (self.ax_types, self.ax_clients) = plt.subplots(
            2, 1, figsize=(6, 5)
        )
        self.figure.patch.set_facecolor("#121212")
        self.canvas = FigureCanvas(self.figure)
        right.addWidget(self.canvas, stretch=1)

    # --------------------------------------------------------------- servidor
    def toggle_server(self) -> None:
        if self._server is None:
            self.start_server()
        else:
            self.stop_server()

    def start_server(self) -> None:
        """Arranca uvicorn sin bloquear el hilo de la GUI."""
        self._server = RestServerThread(self._host, self._port)
        self._server.start()
        self._start_attempts = 0
        self.lbl_status.setText("Central iniciando...")
        self.lbl_status.setStyleSheet("color:#ffa64d;font-weight:bold;")
        # Sondeo no bloqueante: la GUI sigue respondiendo mientras uvicorn arranca.
        QTimer.singleShot(START_CHECK_MS, self._confirm_start)

    def _confirm_start(self) -> None:
        if self._server is None:
            return
        if self._server.running:
            self.btn_server.setText("Detener central")
            self.btn_server.setStyleSheet(BUTTON_STOP)
            self.lbl_status.setText(
                f"Central EN LÍNEA en http://{self._host}:{self._port}"
            )
            self.lbl_status.setStyleSheet("color:#39ff14;font-weight:bold;")
            self.append_log(
                f"[OK] Servidor REST escuchando en http://{self._host}:{self._port}"
            )
            return

        self._start_attempts += 1
        if self._start_attempts < START_MAX_ATTEMPTS:
            QTimer.singleShot(START_CHECK_MS, self._confirm_start)
            return

        self.append_log("[ERROR] No se pudo levantar el servidor (¿puerto ocupado?)")
        self.stop_server()

    def stop_server(self) -> None:
        if self._server is not None:
            self._server.stop()
            self._server = None
        self.btn_server.setText("Iniciar central")
        self.btn_server.setStyleSheet(BUTTON_START)
        self.lbl_status.setText("Central detenida")
        self.lbl_status.setStyleSheet("color:#ffa64d;font-weight:bold;")
        self.append_log("[--] Servidor REST detenido.")

    # ---------------------------------------------------------------- sondeo
    def refresh(self) -> None:
        """Sincroniza la vista con el estado compartido (nunca lanza)."""
        try:
            self._refresh_clients()
            self._refresh_alerts()
        except Exception as exc:  # un fallo de la GUI no debe romper el bucle
            self.append_log(f"[ERROR GUI] {exc}")

    def _refresh_clients(self) -> None:
        clients = registry.list()
        self.client_list.clear()
        live_cameras = 0
        for client in clients:
            state = str(client.get("source_state") or "UNKNOWN").upper()
            label, color = source_display(state)
            if state == "LIVE":
                live_cameras += 1
            item = QListWidgetItem(
                f"● {client['client_id']}  ·  {label}  ·  {client['ip']}  ·  "
                f"{client['address'] or 'sin dirección'}"
            )
            item.setForeground(QColor(color))
            item.setToolTip(
                f"fuente: {state}\núltimo latido: {client['last_seen']}"
            )
            self.client_list.addItem(item)

            previous = self._client_states.get(client["client_id"])
            if client["client_id"] not in self._seen_clients:
                self._seen_clients.add(client["client_id"])
                self.append_log(
                    f"[REGISTRO] cliente conectado: {client['client_id']} "
                    f"desde {client['ip']} :: "
                    f"{client['address'] or 'sin dirección'} · cámara {label}"
                )
            elif previous is not None and previous != state:
                self.append_log(
                    f"[CÁMARA] {client['client_id']}: {previous} → {state}"
                )
            self._client_states[client["client_id"]] = state

        self._client_count = len(clients)
        self._camera_count = live_cameras
        self.lbl_clients.setText(
            f"Clientes conectados: {self._client_count} · "
            f"cámaras en vivo: {self._camera_count}"
        )

    def _refresh_alerts(self) -> None:
        alerts = store.list(limit=MAX_VISIBLE_ALERTS)
        new_alerts = [
            alert for alert in alerts if alert["ticket"] not in self._seen_tickets
        ]
        for alert in reversed(new_alerts):  # las más antiguas primero
            self._seen_tickets.add(alert["ticket"])
            self._append_alert(alert)

        # Refresca estados que hayan cambiado por la API o por este panel.
        for row in range(self.table.rowCount()):
            ticket_item = self.table.item(row, 0)
            status_item = self.table.item(row, 5)
            if ticket_item is None or status_item is None:
                continue
            record = store.get(ticket_item.text())
            if record is not None:
                status_item.setText(record["status"])
        self._update_attend_button()

        self.lbl_summary.setText(
            f"Clientes: {self._client_count} "
            f"(cámaras {self._camera_count})   |   Alertas: {store.count()}"
        )
        if new_alerts:
            self._update_charts(alerts)

    def _append_alert(self, alert: dict) -> None:
        body = alert["alert"]
        location = body.get("location", {})
        address = location.get("address", "") or "sin dirección"
        description = body.get("reason") or body.get("label", "")
        row = self.table.rowCount()
        self.table.insertRow(row)
        values = [
            alert["ticket"],
            body["client_id"],
            body["type"],
            description,
            alert["received_at"][11:19],
            alert["status"],
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            if column == 3:
                item.setToolTip(f"{description}\n{address}")
            self.table.setItem(row, column, item)
        self.table.scrollToBottom()

        self.append_log(
            f"⚠️ ALERTA {alert['ticket']} [{alert['priority']}] "
            f"{body['type']} - {body['client_id']}"
        )
        self.append_log(f"    descripción: {description}")
        self.append_log(f"    dirección:   {address}")
        self.append_log(
            f"    ► AYUDA EN CAMINO: {', '.join(alert['units'])} "
            f"(ETA ~{alert['eta_min']} min)"
        )

    def attend_selected(self) -> None:
        """Marca el ticket seleccionado como atendido (cierra la alerta)."""
        row = self.table.currentRow()
        if row < 0:
            return
        ticket_item = self.table.item(row, 0)
        if ticket_item is None:
            return
        ticket = ticket_item.text()
        record = store.get(ticket)
        if record is not None and record.get("status") == "atendida":
            self.append_log(f"[TICKET] {ticket} ya estaba atendida.")
            return
        updated = store.update_status(ticket, "atendida")
        if updated is None:
            self.append_log(f"[ERROR] no existe el ticket {ticket}.")
            return
        status_item = self.table.item(row, 5)
        if status_item is not None:
            status_item.setText(updated["status"])
        self.append_log(
            f"[TICKET] {ticket} marcada como atendida; "
            f"el cliente volverá a SIN_ALERTA en el próximo sondeo."
        )
        self._update_attend_button()

    def show_evidence(self) -> None:
        """Muestra la captura y el JSON persistidos del ticket seleccionado."""
        row = self.table.currentRow()
        if row < 0:
            return
        ticket_item = self.table.item(row, 0)
        if ticket_item is None:
            return
        ticket = ticket_item.text()
        record = store.get(ticket)
        if record is None:
            self.append_log(f"[ERROR] no existe el ticket {ticket}.")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(f"Evidencia {ticket}")
        dialog.resize(860, 720)
        layout = QVBoxLayout(dialog)

        evidence = store.get_snapshot(ticket)
        if evidence is not None:
            pixmap = QPixmap()
            if pixmap.loadFromData(evidence[0]):
                image = QLabel()
                image.setAlignment(Qt.AlignCenter)
                image.setPixmap(
                    pixmap.scaled(
                        760,
                        420,
                        Qt.KeepAspectRatio,
                        Qt.SmoothTransformation,
                    )
                )
                layout.addWidget(image)
            else:
                layout.addWidget(QLabel("No se pudo decodificar la captura."))
        else:
            layout.addWidget(QLabel("Esta alerta no tiene captura adjunta."))

        editor = QTextEdit()
        editor.setReadOnly(True)
        editor.setStyleSheet(
            "QTextEdit{background:#0d1117;color:#e6e6e6;"
            "font-family:Consolas,monospace;font-size:12px;}"
        )
        editor.setPlainText(json.dumps(record, indent=2, ensure_ascii=False))
        layout.addWidget(editor, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec_()

    def _update_attend_button(self) -> None:
        row = self.table.currentRow()
        self.btn_evidence.setEnabled(row >= 0)
        enabled = False
        if row >= 0:
            status_item = self.table.item(row, 5)
            enabled = status_item is None or status_item.text() != "atendida"
        self.btn_attend.setEnabled(enabled)

    # --------------------------------------------------------------- gráficas
    def _update_charts(self, alerts: list[dict]) -> None:
        by_type: dict[str, int] = {}
        by_client: dict[str, int] = {}
        for alert in alerts:
            incident_type = alert["alert"]["type"]
            client = alert["alert"]["client_id"]
            by_type[incident_type] = by_type.get(incident_type, 0) + 1
            by_client[client] = by_client.get(client, 0) + 1
        self._draw_charts(by_type, by_client)

    def _draw_charts(self, by_type: dict, by_client: dict) -> None:
        self.ax_types.clear()
        self.ax_types.set_facecolor("#1e1e1e")
        self.ax_types.bar(
            list(by_type.keys()) or ["-"],
            list(by_type.values()) or [0],
            color="#ff4d4d",
        )
        self.ax_types.set_title(
            "Alertas por Tipo de Incidente", color="white", pad=8
        )
        self.ax_types.tick_params(colors="white", labelsize=8)

        self.ax_clients.clear()
        self.ax_clients.set_facecolor("#1e1e1e")
        self.ax_clients.bar(
            list(by_client.keys()) or ["-"],
            list(by_client.values()) or [0],
            color="#9933ff",
        )
        self.ax_clients.set_title("Alertas por Cliente", color="white", pad=8)
        self.ax_clients.tick_params(colors="white", labelsize=8)

        self.figure.tight_layout(pad=2.5)
        self.canvas.draw_idle()

    # ------------------------------------------------------------------ util
    def append_log(self, message: str) -> None:
        self.console.append(f"[{time.strftime('%H:%M:%S')}] {message}")

    def closeEvent(self, event) -> None:  # noqa: N802 (API de Qt)
        self.timer.stop()
        self.stop_server()
        event.accept()


def run(host: str = "127.0.0.1", port: int = 8000, auto_start: bool = True) -> int:
    """Crea la aplicación Qt y ejecuta el bucle de eventos."""
    application = QApplication.instance() or QApplication([])
    window = DashboardWindow(host, port)
    window.show()
    if auto_start:
        window.start_server()
    return application.exec_()
