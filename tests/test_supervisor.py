"""Pruebas del supervisor de cámaras y del despacho en proceso."""

from __future__ import annotations

import base64
import threading

from incidentes.domain.entities import (
    AlertPayload,
    ProcessingProgress,
    SourceState,
)
from incidentes.server.cameras import Camera, CameraStore
from incidentes.server.models import AlertRecord, Location
from incidentes.server.supervisor import (
    CameraSupervisor,
    InProcessDispatcher,
)


class RecordingBus:
    """Sustituto del EventBus que guarda los mensajes publicados."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self._lock = threading.Lock()

    def publish_event(self, kind: str, data) -> None:
        with self._lock:
            self.events.append({"kind": kind, "data": data})


class FakeUseCase:
    """Caso de uso mínimo: arranca, espera y termina al pedir stop."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.stopped = False
        self._state = SourceState.CONNECTING

    @property
    def source_state(self) -> SourceState:
        return self._state

    def execute(self, video_path, output_path, listener, live=False) -> None:
        self._state = SourceState.LIVE
        self.started.set()
        listener.on_progress(ProcessingProgress(0, 0), None)
        self.release.wait(5)
        listener.on_completed("")

    def stop(self) -> None:
        self.stopped = True
        self._state = SourceState.STOPPED
        self.release.set()


def test_supervisor_arranca_analiza_y_detiene(tmp_path) -> None:
    store = CameraStore(tmp_path / "central.db")
    camera = store.add("Entrada", "rtsp://192.168.1.50/stream")
    bus = RecordingBus()
    use_case = FakeUseCase()
    supervisor = CameraSupervisor(
        store,
        bus,  # type: ignore[arg-type]
        lambda alert: None,  # type: ignore[arg-type,return-value]
        lambda camera, register, event_bus: use_case,
    )

    status_now = supervisor.start(camera.id)
    assert status_now is not None
    assert status_now["running"] is True
    assert use_case.started.wait(2)

    live = supervisor.status(camera.id)
    assert live is not None
    assert live["state"] == "en_vivo"
    assert any(event["kind"] == "estado" for event in bus.events)

    assert supervisor.stop(camera.id) is True
    assert use_case.stopped is True
    assert supervisor.runtime(camera.id) is None
    stopped = supervisor.status(camera.id)
    assert stopped is not None
    assert stopped["state"] == "detenida"
    store.close()


def test_camara_sin_arrancar_aparece_detenida(tmp_path) -> None:
    store = CameraStore(tmp_path / "central.db")
    camera = store.add("Patio", "rtsp://10.0.0.9/1", enabled=False)
    supervisor = CameraSupervisor(
        store,
        RecordingBus(),  # type: ignore[arg-type]
        lambda alert: None,  # type: ignore[arg-type,return-value]
        lambda camera, register, event_bus: FakeUseCase(),
    )

    statuses = supervisor.statuses()
    assert len(statuses) == 1
    assert statuses[0]["id"] == camera.id
    assert statuses[0]["state"] == "detenida"
    assert statuses[0]["running"] is False
    assert supervisor.start("cam-inexistente") is None
    store.close()


def test_despacho_en_proceso_registra_y_difunde() -> None:
    camera = Camera(
        id="cam-1",
        name="Pasillo",
        source="rtsp://10.0.0.5/1",
        address="Piso 3",
        lat=-12.05,
        lon=-77.04,
    )
    captured: dict = {}

    def register(alert):
        captured["alert"] = alert
        return AlertRecord(
            ticket="ALT-20260101-0001",
            status="registrada",
            priority="ALTA",
            eta_min=5,
            units=["patrulla"],
            received_at="2026-01-01T00:00:00+00:00",
            alert=alert,
        )

    bus = RecordingBus()
    dispatcher = InProcessDispatcher(camera, register, bus)  # type: ignore[arg-type]
    payload = AlertPayload(
        type="PELEA",
        confidence=0.91,
        label="CONFIRMADO IA: PELEA",
        frame=120,
        time_sec=4.5,
        video="",
        verified_by="ollama/qwen2.5vl",
        reason="forcejeo",
        timestamp="2026-01-01T00:00:00+00:00",
        snapshot_jpeg=b"\xff\xd8jpeg",
    )

    ack = dispatcher.dispatch(payload)
    assert ack.delivered is True
    assert ack.ticket == "ALT-20260101-0001"
    assert ack.priority == "ALTA"

    alert = captured["alert"]
    assert alert.client_id == "cam-1"
    assert alert.type == "PELEA"
    assert alert.location == Location(lat=-12.05, lon=-77.04, address="Piso 3")
    assert alert.snapshot_b64 == base64.b64encode(b"\xff\xd8jpeg").decode("ascii")
    assert any(event["kind"] == "alerta" for event in bus.events)


def test_despacho_en_proceso_es_fail_open() -> None:
    camera = Camera(id="cam-1", name="Pasillo", source="rtsp://10.0.0.5/1")

    def register(alert):
        raise RuntimeError("base caída")

    dispatcher = InProcessDispatcher(camera, register, RecordingBus())  # type: ignore[arg-type]
    ack = dispatcher.dispatch(
        AlertPayload(
            type="PELEA", confidence=0.9, label="pelea", frame=1, time_sec=0.1, video=""
        )
    )
    assert ack.delivered is False
    assert "dispatch-error" in ack.detail
