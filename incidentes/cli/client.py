"""Composition root del cliente detector.

Único lugar donde se conectan las capas: se construyen los adaptadores
concretos (YOLO, Ollama, OpenCV, REST, Tkinter) y se inyectan en el caso de
uso. Una ventana representa una cámara/localidad.

Uso::

    python -m incidentes.cli.client
    python -m incidentes.cli.client --client-id camara-02 --address Iztapalapa
"""

from __future__ import annotations

import argparse
import logging
import sys
import tkinter as tk
from dataclasses import replace
from functools import partial

from incidentes.application.detect_incidents import DetectIncidentsUseCase
from incidentes.config import AppConfig
from incidentes.domain.entities import DetectionConfig, GeoLocation
from incidentes.infrastructure.alerting.client_beacon import ClientBeacon
from incidentes.infrastructure.alerting.http_alert_client import HttpAlertDispatcher
from incidentes.infrastructure.detection.incident_detector import YoloIncidentDetector
from incidentes.infrastructure.verification.ollama_verifier import (
    OllamaViolenceVerifier,
)
from incidentes.infrastructure.video.frames import encode_snapshot, mark_pending
from incidentes.infrastructure.video.opencv_reader import get_video_info
from incidentes.infrastructure.video.opencv_writer import OpenCVVideoWriter
from incidentes.infrastructure.video.sources import create_reader
from incidentes.logging_setup import setup_logging
from incidentes.presentation.detector_controller import DetectorController
from incidentes.presentation.detector_view import DetectorView

log = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cliente detector de violencia (una ventana = una cámara)"
    )
    parser.add_argument(
        "--client-id",
        default=None,
        help="identificador ante la central (ej. camara-01)",
    )
    parser.add_argument(
        "--address",
        default=None,
        help="localidad/dirección que se reporta en la alerta",
    )
    parser.add_argument("--lat", type=float, default=None)
    parser.add_argument("--lon", type=float, default=None)
    parser.add_argument(
        "--no-alerts",
        action="store_true",
        help="no enviar alertas a la central (modo local)",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def _config_from_args(args: argparse.Namespace) -> AppConfig:
    """Combina entorno y argumentos; los argumentos tienen prioridad."""
    overrides: dict = {}
    if args.client_id:
        overrides["client_id"] = args.client_id
    if args.address:
        overrides["location_address"] = args.address
    if args.lat is not None:
        overrides["location_lat"] = args.lat
    if args.lon is not None:
        overrides["location_lon"] = args.lon
    if args.no_alerts:
        overrides["alerts_enabled"] = False
    return replace(AppConfig.from_env(), **overrides)


def _report_service_status(
    verifier: OllamaViolenceVerifier,
    dispatcher: HttpAlertDispatcher | None,
    beacon: ClientBeacon | None,
    config: AppConfig,
) -> tuple[bool, bool, bool]:
    """Comprueba los servicios externos y arranca los latidos si es posible.

    Devuelve ``(ia_disponible, central_habilitada, central_disponible)`` para
    que la interfaz pinte los badges desde el primer momento.
    """
    ai_available = verifier.ping()
    if ai_available:
        log.info("Ollama OK en %s (modelo %s)", config.ollama_url, config.ollama_model)
    else:
        log.warning(
            "Ollama no responde en %s (modelo %s). Sin IA no habrá alarmas; "
            "solo se marcarán sospechas. Inicia Ollama y reintenta.",
            config.ollama_url,
            config.ollama_model,
        )

    if dispatcher is None:
        log.info("alertas a la central deshabilitadas (modo local)")
        return ai_available, False, False

    central_available = dispatcher.ping()
    # El latido arranca aunque la central esté caída: al volver, el heartbeat
    # re-registra al cliente y la central recupera la presencia sin reiniciar.
    if beacon is not None:
        beacon.start()
    if central_available:
        log.info("central OK en %s (cliente %s)", config.central_url, config.client_id)
    else:
        log.warning(
            "la central no responde en %s. Las alertas locales funcionarán, "
            "pero no se enviarán hasta que esté disponible.",
            config.central_url,
        )
    return ai_available, True, central_available


def build(config: AppConfig) -> tuple[tk.Tk, DetectorController]:
    """Construye el grafo de dependencias y devuelve la ventana lista."""
    detection_config = DetectionConfig(
        model_path=config.model_path,
        person_class_id=config.person_class_id,
        confidence=config.confidence,
        motion_threshold=config.motion_threshold,
        proximity_factor=config.proximity_factor,
        min_persistence=config.suspicion_min_persistence,  # recall alto
        cooldown_frames=config.cooldown_frames,
    )

    detector = YoloIncidentDetector(
        detection_config,
        violence_model_path=config.violence_model_path,
        mark_confirmed=False,  # modo sospecha: la alarma la decide la IA
    )
    verifier = OllamaViolenceVerifier(
        model=config.ollama_model,
        base_url=config.ollama_url,
        timeout_sec=config.ollama_timeout_sec,
    )

    dispatcher: HttpAlertDispatcher | None = None
    if config.alerts_enabled:
        dispatcher = HttpAlertDispatcher(
            base_url=config.central_url,
            client_id=config.client_id,
            location=GeoLocation(
                config.location_lat,
                config.location_lon,
                config.location_address,
            ),
            timeout_sec=config.alert_timeout_sec,
            api_key=config.central_api_key,
            include_snapshot=config.alert_include_snapshot,
        )

    use_case = DetectIncidentsUseCase(
        suspicion_detector=detector,
        verifier=verifier,
        reader_factory=create_reader,
        writer_factory=lambda: OpenCVVideoWriter(fourcc=config.writer_fourcc),
        info_provider=get_video_info,
        snapshot_encoder=partial(
            encode_snapshot, max_width=config.snapshot_width
        ),
        pending_marker=mark_pending,
        config=detection_config,
        verify_cooldown_sec=config.verify_cooldown_sec,
        drain_timeout_sec=config.verify_drain_timeout_sec,
        alert_dispatcher=dispatcher,
        alert_once=config.alert_once,
        status_reader=dispatcher,
    )

    root = tk.Tk()
    view = DetectorView(
        root,
        client_id=config.client_id,
        address=config.location_address,
    )

    beacon = (
        ClientBeacon(
            dispatcher,
            interval_sec=config.beacon_interval_sec,
            state_provider=lambda: use_case.source_state.name,
        )
        if dispatcher is not None
        else None
    )
    ai_available, central_enabled, central_available = _report_service_status(
        verifier, dispatcher, beacon, config
    )

    controller = DetectorController(
        view,
        use_case,
        output_filename=config.output_filename,
        ai_available=ai_available,
        central_enabled=central_enabled,
        central_available=central_available,
    )

    def on_close() -> None:
        controller.shutdown()
        if beacon is not None:
            beacon.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    return root, controller


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    setup_logging("DEBUG" if args.verbose else None)
    config = _config_from_args(args)

    log.info(
        "iniciando detector de violencia (YOLO + Ollama) cliente=%s (%s)",
        config.client_id,
        config.location_address,
    )
    root, _ = build(config)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
