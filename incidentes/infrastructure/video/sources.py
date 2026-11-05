"""Selección del adaptador de video según la fuente.

Un archivo local y una cámara IP comparten el puerto ``VideoReader`` pero no
el contrato: el archivo termina, la cámara se reconecta. Esta función es el
único punto donde se elige el adaptador.
"""

from __future__ import annotations

from incidentes.domain.ports import VideoReader
from incidentes.infrastructure.video.live_reader import LiveVideoReader
from incidentes.infrastructure.video.opencv_reader import OpenCVVideoReader

LIVE_SCHEMES = (
    "rtsp://",
    "rtsps://",
    "rtmp://",
    "http://",
    "https://",
    "udp://",
    "tcp://",
)


def is_live_source(source: str) -> bool:
    """Indica si la fuente es una URL de red (cámara IP) y no un archivo."""
    return source.strip().lower().startswith(LIVE_SCHEMES)


def create_reader(source: str) -> VideoReader:
    """Devuelve el lector adecuado para ``source``."""
    if is_live_source(source):
        return LiveVideoReader()
    return OpenCVVideoReader()
