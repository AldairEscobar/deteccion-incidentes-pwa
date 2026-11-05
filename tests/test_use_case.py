"""Pruebas del caso de uso con dobles de prueba (sin video ni IA real).

Verifican la orquestación: sospechas, verificación, cooldown, alert_once,
reporte JSON y cierre de recursos.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from incidentes.application.detect_incidents import DetectIncidentsUseCase
from incidentes.domain.entities import (
    AlertAck,
    AlertPayload,
    DetectionConfig,
    FrameAnalysis,
    Incident,
    ProcessingProgress,
    Verdict,
    VideoInfo,
)
from incidentes.domain.ports import (
    AlertDispatcher,
    IncidentDetector,
    ProgressListener,
    VideoReader,
    VideoWriter,
    ViolenceVerifier,
)


class FakeReader(VideoReader):
    def __init__(self, frames: list[Any]) -> None:
        self._frames = iter(frames)
        self.path = ""
        self.closed = False

    def open(self, path: str) -> None:
        self.path = path

    def read(self) -> tuple[bool, Any]:
        try:
            return True, next(self._frames)
        except StopIteration:
            return False, None

    def close(self) -> None:
        self.closed = True


class FakeWriter(VideoWriter):
    def __init__(self) -> None:
        self.frames: list[Any] = []
        self.path = ""
        self.closed = False

    def open(self, path: str, fps: int, width: int, height: int) -> None:
        self.path = path

    def write(self, frame: Any) -> None:
        self.frames.append(frame)

    def close(self) -> None:
        self.closed = True


class FakeDetector(IncidentDetector):
    """Devuelve una sospecha de PELEA durante los primeros frames."""

    def __init__(self, suspicious_frames: int = 2) -> None:
        self._remaining = suspicious_frames
        self.calls = 0

    def analyze(self, frame: Any) -> FrameAnalysis:
        self.calls += 1
        incidents: tuple[Incident, ...] = ()
        if self._remaining > 0:
            self._remaining -= 1
            incidents = (Incident("PELEA", 0.9, "PELEA: 2 personas"),)
        return FrameAnalysis(frame, incidents, alert=False)


class FakeVerifier(ViolenceVerifier):
    def __init__(
        self,
        verdict: Verdict | None = None,
        available: bool = True,
    ) -> None:
        self._verdict = verdict or Verdict(True, 0.9, "PELEA", "forcejeo")
        self._available = available
        self.calls = 0

    @property
    def name(self) -> str:
        return "fake-verifier"

    def ping(self) -> bool:
        return self._available

    def verify(self, jpeg_bytes: bytes, suspicion: Any) -> Verdict:
        self.calls += 1
        return self._verdict


class FakeDispatcher(AlertDispatcher):
    def __init__(self) -> None:
        self.payloads: list[AlertPayload] = []

    def dispatch(self, alert: AlertPayload) -> AlertAck:
        self.payloads.append(alert)
        return AlertAck(
            delivered=True,
            ticket="ALT-TEST-0001",
            status="registrada",
            eta_min=5,
            message="Ayuda en camino",
        )


class RecorderListener(ProgressListener):
    def __init__(self) -> None:
        self.progress: list[ProcessingProgress] = []
        self.incidents: list[tuple[Any, float, str]] = []
        self.suspicions: list[Any] = []
        self.discarded: list[tuple[Any, Any]] = []
        self.completed: list[str] = []
        self.summaries: list[dict] = []
        self.errors: list[str] = []

    def on_progress(self, progress: ProcessingProgress, preview_frame: Any) -> None:
        self.progress.append(progress)

    def on_suspicion(self, suspicion: Any) -> None:
        self.suspicions.append(suspicion)

    def on_suspicion_discarded(self, suspicion: Any, verdict: Any) -> None:
        self.discarded.append((suspicion, verdict))

    def on_incident_alert(
        self, incident: Any, timestamp_sec: float, timestamp: str = ""
    ) -> None:
        self.incidents.append((incident, timestamp_sec, timestamp))

    def on_completed(self, output_path: str) -> None:
        self.completed.append(output_path)

    def on_summary(self, summary: dict) -> None:
        self.summaries.append(summary)

    def on_error(self, message: str) -> None:
        self.errors.append(message)


def wait_for(predicate, timeout: float = 2.0) -> bool:
    """Espera a que se cumpla una condición (para hilos de despacho)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def run_use_case(
    tmp_path: Path,
    *,
    verifier: FakeVerifier | None = None,
    dispatcher: FakeDispatcher | None = None,
    alert_once: bool = True,
    cooldown_frames: int = 0,
    frame_count: int = 3,
    suspicious_frames: int = 2,
):
    frames = [object() for _ in range(frame_count)]
    reader = FakeReader(frames)
    writer = FakeWriter()
    detector = FakeDetector(suspicious_frames=suspicious_frames)
    verifier = verifier or FakeVerifier()
    dispatcher = dispatcher or FakeDispatcher()
    listener = RecorderListener()

    use_case = DetectIncidentsUseCase(
        suspicion_detector=detector,
        verifier=verifier,
        reader_factory=lambda _source: reader,
        writer_factory=lambda: writer,
        info_provider=lambda path: VideoInfo(path, len(frames), 10, 640, 480),
        snapshot_encoder=lambda frame: b"jpeg",
        pending_marker=lambda frame, count: None,
        config=DetectionConfig(cooldown_frames=cooldown_frames),
        verify_cooldown_sec=0,
        drain_timeout_sec=5,
        alert_dispatcher=dispatcher,
        alert_once=alert_once,
    )
    output_path = tmp_path / "salida.mp4"
    summary = use_case.execute("entrada.mp4", str(output_path), listener)
    return summary, listener, dispatcher, reader, writer


def test_confirmed_incidents_generate_report_and_one_alert(tmp_path: Path) -> None:
    summary, listener, dispatcher, reader, writer = run_use_case(tmp_path)

    assert summary["total_frames"] == 3
    assert summary["suspicions_yolo"] == 2
    assert summary["incident_frames"] == 2
    assert summary["counts"] == {"PELEA": 2}
    assert len(listener.incidents) == 2
    assert listener.errors == []
    assert reader.closed and writer.closed
    assert Path(summary["report"]).is_file()

    assert wait_for(lambda: len(dispatcher.payloads) == 1)
    payload = dispatcher.payloads[0]
    assert payload.type == "PELEA"
    assert payload.verified_by == "fake-verifier"
    assert payload.snapshot_jpeg == b"jpeg"


def test_alert_once_false_dispatches_every_confirmation(tmp_path: Path) -> None:
    _, _, dispatcher, _, _ = run_use_case(tmp_path, alert_once=False)
    assert wait_for(lambda: len(dispatcher.payloads) == 2)


def test_non_violent_verdict_produces_no_alert(tmp_path: Path) -> None:
    verifier = FakeVerifier(Verdict(False, 0.2, "NADA", "no es violencia"))
    summary, listener, dispatcher, _, _ = run_use_case(tmp_path, verifier=verifier)

    assert summary["incident_frames"] == 0
    assert summary["counts"] == {}
    assert listener.incidents == []
    assert dispatcher.payloads == []


def test_unavailable_verifier_skips_verification(tmp_path: Path) -> None:
    verifier = FakeVerifier(available=False)
    summary, _, _, _, _ = run_use_case(tmp_path, verifier=verifier)

    assert summary["verifier_available"] is False
    assert summary["suspicions_yolo"] == 0
    assert verifier.calls == 0


def test_output_and_report_paths(tmp_path: Path) -> None:
    output = DetectIncidentsUseCase.resolve_output_path(
        str(tmp_path / "entrada.mp4"), "resultado.mp4"
    )
    assert output == str(tmp_path / "resultado.mp4")
    report = DetectIncidentsUseCase.resolve_report_path(output)
    assert report == str(tmp_path / "resultado_reporte.json")


def test_cooldown_limits_events_for_same_type(tmp_path: Path) -> None:
    summary, _, _, _, _ = run_use_case(tmp_path, cooldown_frames=10)
    assert summary["incident_frames"] == 1
    assert summary["counts"] == {"PELEA": 1}
