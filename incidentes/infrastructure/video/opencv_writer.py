"""Escritura del video anotado con OpenCV."""

from __future__ import annotations

from typing import Any

import cv2

from incidentes.domain.ports import VideoWriter


class OpenCVVideoWriter(VideoWriter):
    """Implementación de ``VideoWriter`` sobre ``cv2.VideoWriter``."""

    def __init__(self, fourcc: str = "mp4v") -> None:
        self._fourcc = fourcc
        self._writer: Any = None

    def open(self, path: str, fps: int, width: int, height: int) -> None:
        codec = cv2.VideoWriter_fourcc(*self._fourcc)
        self._writer = cv2.VideoWriter(path, codec, fps, (width, height))
        if not self._writer.isOpened():
            self._writer = None
            raise OSError(f"No se pudo crear el video de salida: {path}")

    def write(self, frame: Any) -> None:
        if self._writer is None:
            raise RuntimeError("El writer no está abierto; llama a open() primero.")
        self._writer.write(frame)

    def close(self) -> None:
        if self._writer is not None:
            self._writer.release()
            self._writer = None
