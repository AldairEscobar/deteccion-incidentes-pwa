"""Lectura y metadatos de video con OpenCV."""

from __future__ import annotations

from typing import Any

import cv2

from incidentes.domain.entities import SourceState, VideoInfo
from incidentes.domain.ports import VideoReader


def get_video_info(path: str) -> VideoInfo:
    """Lee los metadatos del video sin dejar el archivo abierto."""
    capture = cv2.VideoCapture(path)
    try:
        if not capture.isOpened():
            raise OSError(f"No se pudo abrir el video: {path}")
        return VideoInfo(
            path=path,
            total_frames=int(capture.get(cv2.CAP_PROP_FRAME_COUNT)),
            fps=int(capture.get(cv2.CAP_PROP_FPS)) or 30,
            width=int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
            height=int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        )
    finally:
        capture.release()


class OpenCVVideoReader(VideoReader):
    """Implementación de ``VideoReader`` sobre ``cv2.VideoCapture``."""

    def __init__(self) -> None:
        self._capture: Any = None
        self._state = SourceState.UNKNOWN

    @property
    def state(self) -> SourceState:
        return self._state

    def open(self, path: str) -> None:
        self._capture = cv2.VideoCapture(path)
        if not self._capture.isOpened():
            self._capture.release()
            self._capture = None
            self._state = SourceState.NO_SIGNAL
            raise OSError(f"No se pudo abrir el video: {path}")
        self._state = SourceState.LIVE

    def read(self) -> tuple[bool, Any]:
        if self._capture is None:
            raise RuntimeError("El lector no está abierto; llama a open() primero.")
        ok, frame = self._capture.read()
        if not ok:
            self._state = SourceState.STOPPED
        return ok, frame

    def close(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None
        self._state = SourceState.STOPPED
