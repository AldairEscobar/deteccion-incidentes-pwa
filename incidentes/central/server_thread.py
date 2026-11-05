"""Hilo que ejecuta uvicorn dentro del proceso de la GUI.

La GUI nunca llama a la red desde su hilo: el servidor REST corre aparte y el
dashboard solo lee el estado compartido (``store`` y ``registry``), que ya está
protegido por locks.
"""

from __future__ import annotations

import logging
import threading

import uvicorn

from incidentes.server.app import app as api_app

log = logging.getLogger(__name__)


class RestServerThread(threading.Thread):
    """Ejecuta la API REST en un hilo daemon y permite detenerla."""

    def __init__(self, host: str, port: int) -> None:
        super().__init__(daemon=True, name="central-rest")
        self._server = uvicorn.Server(
            uvicorn.Config(api_app, host=host, port=port, log_level="warning")
        )

    @property
    def running(self) -> bool:
        """True cuando uvicorn terminó su arranque y acepta peticiones."""
        return bool(getattr(self._server, "started", False))

    def run(self) -> None:
        self._server.run()

    def stop(self) -> None:
        """Pide a uvicorn un apagado ordenado."""
        self._server.should_exit = True
