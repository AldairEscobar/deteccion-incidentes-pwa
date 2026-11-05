"""Pruebas del modo vivo del caso de uso, sin cámara ni IA real."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Any

from incidentes.application.detect_incidents import DetectIncidentsUseCase
from incidentes.domain.entities import (
    AlertAck,
    AlertPayload,
    DetectionConfig,
    FrameAnalysis,
    Incident,
    SourceState,
    Suspicion,
    Verdict,
)
from incidentes.domain.ports import (
    AlertDispatcher,
    AlertStatusReader,
    IncidentDetector,
    ProgressListener,
    VideoReader,
    ViolenceVerifier,
)


class BlockingLiveReader(VideoReader):
    """Entrega frames y después espera hasta que se pida parar."""

    def __init__(self, frames: list[Any]) -> None:
        self._frames = list(frames)
        self._stopped = threading.Event()
        self.closed = False
        self.path = ""

    def open(self, path: str) -> None:
        self.path = path

    def read(self) -> tuple[bool, Any]:
        while not self._stopped.is_set():
            if self._frames:
                return True, self._frames.pop(0)
            time.sleep(0.005)
        return False, None

    def stop(self) -> None:
        self._stopped.set()

    def close(self) -> None:
        self.closed = True
        self._stopped.set()


class AlwaysSuspectDetector(IncidentDetector):
    def analyze(self, frame: Any) -> FrameAnalysis:
        return FrameAnalysis(frame, (Incident("PELEA", 0.9, "PELEA: 2 personas"),))


class FixedVerifier(ViolenceVerifier):
    def __init__(self, verdict: Verdict | None = None) -> None:
        self._verdict = verdict or Verdict(True, 0.9, "PELEA", "forcejeo")

    @property
    def name(self) -> str:
        return "fake-verifier"

    def ping(self) -> bool:
        return True

    def verify(self, jpeg_bytes: bytes, suspicion: Suspicion) -> Verdict:
        return self._verdict


class RecordingDispatcher(AlertDispatcher):
    def __init__(self) -> None:
        self.payloads: list[AlertPayload] = []

    def dispatch(self, alert: AlertPayload) -> AlertAck:
        self.payloads.append(alert)
        return AlertAck(delivered=True, ticket="ALT-VIVO-0001", status="registrada")


class FixedStatusReader(AlertStatusReader):
    def __init__(self, status: str = "atendida") -> None:
        self._status = status
        self.calls = 0

    def get_alert_status(self, ticket: str) -> str | None:
        self.calls += 1
        return self._status


class RecordingListener(ProgressListener):
    def __init__(self) -> None:
        self.suspicions: list[Suspicion] = []
        self.discarded: list[tuple[Suspicion, Verdict]] = []
        self.incidents: list[tuple[Any, float, str]] = []
        self.closed: list[tuple[Any, str]] = []
        self.completed: list[str] = []
        self.summaries: list[dict[str, Any]] = []
        self.errors: list[str] = []

    def on_progress(self, progress: Any, preview_frame: Any) -> None:
        pass

    def on_suspicion(self, suspicion: Suspicion) -> None:
        self.suspicions.append(suspicion)

    def on_suspicion_discarded(self, suspicion: Suspicion, verdict: Verdict) -> None:
        self.discarded.append((suspicion, verdict))

    def on_incident_alert(
        self, incident: Any, timestamp_sec: float, timestamp: str = ""
    ) -> None:
        self.incidents.append((incident, timestamp_sec, timestamp))

    def on_incident_closed(self, incident: Any, ticket: str) -> None:
        self.closed.append((incident, ticket))

    def on_completed(self, output_path: str) -> None:
        self.completed.append(output_path)

    def on_summary(self, summary: dict[str, Any]) -> None:
        self.summaries.append(summary)

    def on_error(self, message: str) -> None:
        self.errors.append(message)


class StepClock:
    """Reloj monótono falso: avanza un paso fijo en cada lectura."""

    def __init__(self, start: float = 100.0, step: float = 1.0) -> None:
        self._value = start
        self._step = step

    def __call__(self) -> float:
        self._value += self._step
        return self._value


def no_metadata(path: str) -> Any:
    raise AssertionError("el modo vivo no debe leer metadatos del archivo")


def build_live_use_case(
    reader: VideoReader,
    *,
    verifier: ViolenceVerifier | None = None,
    dispatcher: AlertDispatcher | None = None,
    alert_once: bool = True,
    cooldown_frames: int = 0,
    now: Any = None,
    monotonic: Any = None,
    status_reader: AlertStatusReader | None = None,
    ticket_poll_interval_sec: float = 10.0,
) -> tuple[DetectIncidentsUseCase, RecordingListener]:
    listener = RecordingListener()
    use_case = DetectIncidentsUseCase(
        suspicion_detector=AlwaysSuspectDetector(),
        verifier=verifier or FixedVerifier(),
        reader_factory=lambda _source: reader,
        writer_factory=None,
        info_provider=no_metadata,
        snapshot_encoder=lambda frame: b"jpeg",
        pending_marker=lambda frame, count: None,
        config=DetectionConfig(cooldown_frames=cooldown_frames),
        verify_cooldown_sec=0,
        drain_timeout_sec=5,
        alert_dispatcher=dispatcher or RecordingDispatcher(),
        alert_once=alert_once,
        status_reader=status_reader,
        ticket_poll_interval_sec=ticket_poll_interval_sec,
        now=now,
        monotonic=monotonic,
    )
    return use_case, listener


def run_live(
    use_case: DetectIncidentsUseCase,
    listener: RecordingListener,
    *,
    delay: float = 0.2,
) -> dict[str, Any]:
    timer = threading.Timer(delay, use_case.stop)
    timer.start()
    try:
        return use_case.execute("http://camara/video", None, listener, live=True)
    finally:
        timer.cancel()


def wait_for(predicate: Any, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_la_sesion_viva_no_exige_writer_ni_metadatos() -> None:
    reader = BlockingLiveReader([object(), object()])
    use_case, listener = build_live_use_case(reader)

    summary = run_live(use_case, listener)

    assert summary["mode"] == "vivo"
    assert summary["output"] == ""
    assert summary["report"] == ""
    assert summary["total_frames"] == 2
    assert summary["started_at"]
    assert summary["finished_at"]
    assert summary["discarded"] == 0
    assert summary["duration_sec"] >= 0
    assert reader.closed
    assert listener.completed == [""]
    assert listener.errors == []


def test_en_vivo_se_despacha_por_incidente_aunque_alert_once_sea_true() -> None:
    dispatcher = RecordingDispatcher()
    reader = BlockingLiveReader([object(), object()])
    use_case, listener = build_live_use_case(
        reader, dispatcher=dispatcher, alert_once=True
    )

    run_live(use_case, listener)

    assert wait_for(lambda: len(dispatcher.payloads) == 2)
    assert len(listener.suspicions) == 2
    assert len(listener.incidents) == 2
    # La alerta se identifica por client_id; no se filtra la URL de la cámara.
    assert all(payload.video == "" for payload in dispatcher.payloads)


def test_en_vivo_usa_hora_de_pared_y_tiempo_monotono() -> None:
    fixed = datetime(2026, 5, 1, 12, 30, 0, tzinfo=UTC)
    expected = fixed.isoformat(timespec="seconds")
    dispatcher = RecordingDispatcher()
    reader = BlockingLiveReader([object(), object()])
    use_case, listener = build_live_use_case(
        reader,
        dispatcher=dispatcher,
        now=lambda: fixed,
        monotonic=StepClock(),
    )

    summary = run_live(use_case, listener)

    assert [s.time_sec for s in listener.suspicions] == [1.0, 2.0]
    assert all(s.timestamp == expected for s in listener.suspicions)
    assert listener.incidents[0][2] == expected
    assert summary["events"][0]["timestamp"] == expected
    assert summary["started_at"] == expected
    assert summary["finished_at"] == expected

    assert wait_for(lambda: len(dispatcher.payloads) == 2)
    assert dispatcher.payloads[0].timestamp == expected
    assert dispatcher.payloads[0].time_sec == 1.0


def test_la_ia_descartada_notifica_el_descarte_y_no_alerta() -> None:
    reader = BlockingLiveReader([object()])
    dispatcher = RecordingDispatcher()
    verifier = FixedVerifier(Verdict(False, 0.2, "NADA", "no es violencia"))
    use_case, listener = build_live_use_case(
        reader, verifier=verifier, dispatcher=dispatcher
    )

    summary = run_live(use_case, listener)

    assert len(listener.suspicions) == 1
    assert len(listener.discarded) == 1
    assert listener.discarded[0][1].violent is False
    assert summary["discarded"] == 1
    assert listener.incidents == []
    assert dispatcher.payloads == []


def test_stop_termina_una_sesion_sin_frames() -> None:
    reader = BlockingLiveReader([])
    use_case, listener = build_live_use_case(reader)

    started = time.monotonic()
    summary = run_live(use_case, listener, delay=0.1)
    elapsed = time.monotonic() - started

    assert elapsed < 3
    assert summary["total_frames"] == 0
    assert summary["events"] == []
    assert reader.closed


def test_stop_antes_de_arrancar_el_worker_no_se_pierde() -> None:
    reader = BlockingLiveReader([object()])
    use_case, listener = build_live_use_case(reader)

    use_case.stop()
    summary = use_case.execute("http://camara/video", None, listener, live=True)

    assert summary["total_frames"] == 0
    assert reader.closed


def test_el_cierre_del_ticket_se_notifica_al_listener() -> None:
    reader = BlockingLiveReader([object()])
    dispatcher = RecordingDispatcher()
    status_reader = FixedStatusReader("atendida")
    use_case, listener = build_live_use_case(
        reader,
        dispatcher=dispatcher,
        status_reader=status_reader,
        ticket_poll_interval_sec=0.01,
    )

    run_live(use_case, listener)

    assert wait_for(lambda: listener.closed)
    assert status_reader.calls >= 1
    incident, ticket = listener.closed[0]
    assert ticket == "ALT-VIVO-0001"
    assert incident.type == "PELEA"
    assert len(dispatcher.payloads) == 1


def test_source_state_queda_en_stopped_al_terminar_la_sesion() -> None:
    reader = BlockingLiveReader([object()])
    use_case, listener = build_live_use_case(reader)

    assert use_case.source_state is SourceState.UNKNOWN
    run_live(use_case, listener)
    assert use_case.source_state is SourceState.STOPPED
