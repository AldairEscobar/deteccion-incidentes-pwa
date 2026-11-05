"""Configuración del cliente detector.

Los valores se pueden sobrescribir con variables de entorno con el prefijo
``INCIDENTES_`` (ver ``.env.example``). La configuración es inmutable: los
argumentos de línea de comandos se aplican con ``dataclasses.replace`` en el
punto de entrada, evitando estado global mutable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"La variable {name} debe ser numérica, no {raw!r}") from exc


def _env_optional_float(name: str) -> float | None:
    raw = os.getenv(name)
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"La variable {name} debe ser numérica, no {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


@dataclass(frozen=True)
class AppConfig:
    """Configuración completa del cliente detector y de su enlace con la central."""

    # --- Detección (etapa 1) ---
    model_path: str = "yolo11n.pt"
    # Modelo propio opcional (p. ej. un .pt entrenado en peleas). Si el archivo
    # existe se usa en lugar de la heurística; si no, se usa YOLO-COCO.
    violence_model_path: str = "violence.pt"
    person_class_id: int = 0
    confidence: float = 0.5
    motion_threshold: float = 12.0
    proximity_factor: float = 0.7
    suspicion_min_persistence: int = 2  # bajo a propósito: recall alto
    cooldown_frames: int = 30
    output_filename: str = "resultado_incidentes.mp4"
    writer_fourcc: str = "mp4v"

    # --- Verificación (etapa 2, Ollama) ---
    ollama_model: str = "qwen2.5vl:7b"
    ollama_url: str = "http://localhost:11434"
    ollama_timeout_sec: int = 120
    verify_cooldown_sec: float = 8.0  # mínimo entre envíos a la IA
    verify_drain_timeout_sec: float = 180.0  # espera máxima al cerrar
    snapshot_width: int = 480  # ancho del JPEG que recibe el modelo

    # --- Enlace con la central (REST) ---
    alerts_enabled: bool = True
    central_url: str = "http://localhost:8000"
    central_api_key: str = ""  # debe coincidir con CENTRAL_API_KEY del servidor
    client_id: str = "camara-01"
    location_lat: float | None = None
    location_lon: float | None = None
    location_address: str = "Sin dirección configurada"
    alert_timeout_sec: float = 5.0  # no bloquear la detección si la central cae
    alert_include_snapshot: bool = True
    alert_once: bool = True  # una sola alerta a la central por video procesado
    beacon_interval_sec: float = 10.0

    @classmethod
    def from_env(cls) -> AppConfig:
        """Construye la configuración leyendo ``INCIDENTES_*`` del entorno."""
        return cls(
            model_path=_env_str("INCIDENTES_MODEL_PATH", cls.model_path),
            violence_model_path=_env_str(
                "INCIDENTES_VIOLENCE_MODEL", cls.violence_model_path
            ),
            confidence=_env_float("INCIDENTES_CONFIDENCE", cls.confidence),
            motion_threshold=_env_float(
                "INCIDENTES_MOTION_THRESHOLD", cls.motion_threshold
            ),
            proximity_factor=_env_float(
                "INCIDENTES_PROXIMITY_FACTOR", cls.proximity_factor
            ),
            ollama_model=_env_str("INCIDENTES_OLLAMA_MODEL", cls.ollama_model),
            ollama_url=_env_str("INCIDENTES_OLLAMA_URL", cls.ollama_url),
            alert_timeout_sec=_env_float(
                "INCIDENTES_ALERT_TIMEOUT", cls.alert_timeout_sec
            ),
            alerts_enabled=_env_bool("INCIDENTES_ALERTS_ENABLED", cls.alerts_enabled),
            central_url=_env_str("INCIDENTES_CENTRAL_URL", cls.central_url),
            central_api_key=_env_str(
                "INCIDENTES_CENTRAL_API_KEY", cls.central_api_key
            ),
            client_id=_env_str("INCIDENTES_CLIENT_ID", cls.client_id),
            location_lat=_env_optional_float("INCIDENTES_LOCATION_LAT"),
            location_lon=_env_optional_float("INCIDENTES_LOCATION_LON"),
            location_address=_env_str(
                "INCIDENTES_LOCATION_ADDRESS", cls.location_address
            ),
            alert_include_snapshot=_env_bool(
                "INCIDENTES_ALERT_INCLUDE_SNAPSHOT", cls.alert_include_snapshot
            ),
            alert_once=_env_bool("INCIDENTES_ALERT_ONCE", cls.alert_once),
            beacon_interval_sec=_env_float(
                "INCIDENTES_BEACON_INTERVAL", cls.beacon_interval_sec
            ),
        )
