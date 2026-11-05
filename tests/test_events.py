"""Pruebas del bus de eventos hilos → asyncio."""

from __future__ import annotations

import asyncio
import threading

from incidentes.server.events import EventBus


def test_entrega_desde_otro_hilo() -> None:
    async def scenario() -> None:
        bus = EventBus()
        bus.bind_loop(asyncio.get_running_loop())
        queue = bus.subscribe()

        threading.Thread(target=lambda: bus.publish({"kind": "x", "data": 1})).start()
        message = await asyncio.wait_for(queue.get(), timeout=2)
        assert message == {"kind": "x", "data": 1}

        bus.unsubscribe(queue)
        bus.publish({"kind": "y"})
        assert queue.empty()

    asyncio.run(scenario())


def test_publicar_sin_bucle_no_falla() -> None:
    bus = EventBus()
    bus.publish_event("bitacora", {"text": "sin suscriptores"})


def test_suscriptor_lento_pierde_eventos_sin_crecer() -> None:
    async def scenario() -> None:
        bus = EventBus(queue_size=1)
        queue = bus.subscribe()
        bus._dispatch({"n": 1})
        bus._dispatch({"n": 2})
        assert queue.qsize() == 1
        assert await queue.get() == {"n": 1}

    asyncio.run(scenario())
