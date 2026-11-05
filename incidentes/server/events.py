"""Bus de eventos en tiempo real entre los hilos de detección y la PWA.

Los pipelines de cada cámara corren en hilos nativos y publican desde ahí,
mientras que los clientes WebSocket viven en el bucle asyncio de uvicorn. El
bus es el único punto que cruza esa frontera: ``publish`` es seguro desde
cualquier hilo y encola el mensaje en el bucle correcto con
``call_soon_threadsafe``.

Los suscriptores usan colas acotadas: si un navegador se atasca, se descartan
eventos en vez de acumular memoria sin límite (mismo criterio que la cola de
verificación del caso de uso).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

log = logging.getLogger(__name__)

QUEUE_SIZE = 64


class EventBus:
    """Difunde mensajes JSON a todos los WebSocket conectados."""

    def __init__(self, queue_size: int = QUEUE_SIZE) -> None:
        self._queue_size = max(1, int(queue_size))
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    # ------------------------------------------------------------------ ciclo
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Registra el bucle de uvicorn donde se entregarán los mensajes."""
        with self._lock:
            self._loop = loop

    def unbind(self) -> None:
        with self._lock:
            self._loop = None

    # ----------------------------------------------------------- suscripción
    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=self._queue_size
        )
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(queue)

    # --------------------------------------------------------------- publicación
    def publish(self, message: dict[str, Any]) -> None:
        """Envía un mensaje a todos los suscriptores (seguro entre hilos)."""
        with self._lock:
            loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._dispatch, message)
        except RuntimeError:
            # El bucle se cerró entre la comprobación y la llamada.
            log.debug("bus de eventos sin bucle activo", exc_info=True)

    def _dispatch(self, message: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for queue in subscribers:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                log.debug("suscriptor lento: evento descartado")

    def publish_event(self, kind: str, data: Any) -> None:
        """Atajo con el sobre estándar ``{kind, data}``."""
        self.publish({"kind": kind, "data": data})
