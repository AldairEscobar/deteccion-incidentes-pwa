"""Configuración común de logging para los ejecutables."""

from __future__ import annotations

import logging
import os

LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
DATE_FORMAT = "%H:%M:%S"


def setup_logging(level: str | int | None = None) -> None:
    """Configura el logger raíz una sola vez.

    Args:
        level: nivel explícito (``"DEBUG"``, 20...). Si se omite, se usa la
            variable de entorno ``LOG_LEVEL`` y, en su defecto, ``INFO``.
    """
    if isinstance(level, str):
        resolved: str | int = level.upper()
    else:
        resolved = level or os.getenv("LOG_LEVEL", "INFO").upper()

    logging.basicConfig(
        level=resolved,
        format=LOG_FORMAT,
        datefmt=DATE_FORMAT,
    )
