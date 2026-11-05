"""Latido del cliente hacia la central.

Mantiene registrado al detector mientras la aplicación está abierta: se
registra una vez y luego envía heartbeats periódicos en un hilo daemon. Así la
central puede mostrar "clientes conectados" y detectar clientes caídos.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Protocol

log = logging.getLogger(__name__)


class ClientChannel(Protocol):
    """Operaciones del canal necesarias para mantener la presencia."""

    def register(self, source_state: str = "") -> bool: ...

    def heartbeat(self, source_state: str = "") -> bool: ...

    def unregister(self) -> bool: ...


class ClientBeacon:
    """Registra el cliente y mantiene su latido en segundo plano."""

    def __init__(
        self,
        channel: ClientChannel,
        interval_sec: float = 10.0,
        state_provider: Callable[[], str] | None = None,
    ) -> None:
        self._channel = channel
        self._interval = interval_sec
        self._state_provider = state_provider
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _source_state(self) -> str:
        """Estado de la cámara para el latido; nunca rompe el hilo."""
        if self._state_provider is None:
            return ""
        try:
            return str(self._state_provider() or "")
        except Exception:
            log.debug("no se pudo leer el estado de la fuente", exc_info=True)
            return ""

    def start(self) -> None:
        """Arranca el registro y los latidos en segundo plano (idempotente)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="client-beacon"
        )
        self._thread.start()

    def _run(self) -> None:
        # El registro puede tardar (o fallar) si la central está caída; no debe
        # bloquear el arranque de la interfaz. El primer heartbeat reintenta el
        # alta si el registro no llegó.
        try:
            self._channel.register(self._source_state())
        except Exception:
            log.exception("fallo el registro inicial en la central")
        self._loop()

    def _loop(self) -> None:
        # wait() devuelve True cuando se pide parar; False al cumplirse el intervalo.
        while not self._stop_event.wait(self._interval):
            try:
                self._channel.heartbeat(self._source_state())
            except Exception:
                log.exception("fallo el latido hacia la central")

    def stop(self) -> None:
        """Detiene los latidos y da de baja al cliente. Nunca lanza."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        try:
            self._channel.unregister()
        except Exception:
            log.debug("no se pudo dar de baja al cliente", exc_info=True)
