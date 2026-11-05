"""Pruebas del lector en vivo con capturas falsas (sin cámara ni red)."""

from __future__ import annotations

import threading
import time
from typing import Any

import cv2
import pytest

from incidentes.domain.entities import SourceState
from incidentes.infrastructure.video.live_reader import LiveVideoReader
from incidentes.infrastructure.video.opencv_reader import OpenCVVideoReader
from incidentes.infrastructure.video.sources import create_reader, is_live_source


class FakeCapture:
    """Imita la parte de ``cv2.VideoCapture`` que usa el lector."""

    def __init__(
        self,
        frames: list[Any] | None = None,
        *,
        opened: bool = True,
        read_failures: int = 0,
    ) -> None:
        self._frames = list(frames or [])
        self._opened = opened
        self._read_failures = read_failures
        self.released = False
        self.settings: list[tuple[int, float]] = []

    def isOpened(self) -> bool:
        return self._opened

    def read(self) -> tuple[bool, Any]:
        if self._read_failures > 0:
            self._read_failures -= 1
            return False, None
        if self._frames:
            return True, self._frames.pop(0)
        return False, None

    def set(self, prop: int, value: float) -> bool:
        self.settings.append((prop, value))
        return True

    def release(self) -> None:
        self.released = True
        self._opened = False


def test_lee_frames_y_expone_el_estado() -> None:
    capture = FakeCapture(["frame-1", "frame-2"])
    reader = LiveVideoReader(
        capture_factory=lambda _: capture, watchdog_enabled=False
    )

    reader.open("http://camara/video")

    assert reader.state is SourceState.CONNECTING
    assert reader.read() == (True, "frame-1")
    assert reader.state is SourceState.LIVE
    assert reader.read() == (True, "frame-2")

    reader.close()
    assert reader.state is SourceState.STOPPED
    assert capture.released


def test_reconecta_tras_perder_la_senal() -> None:
    first = FakeCapture(["frame-1"])
    second = FakeCapture(["frame-2"])
    captures = iter([first, second])

    reader = LiveVideoReader(
        capture_factory=lambda _: next(captures),
        reconnect_delay_sec=0.01,
        max_reconnect_delay_sec=0.02,
        watchdog_enabled=False,
    )
    reader.open("rtsp://camara/video")

    assert reader.read() == (True, "frame-1")
    # El segundo read agota la captura, la libera y reconecta.
    assert reader.read() == (True, "frame-2")

    assert first.released
    assert reader.open_attempts == 2
    assert reader.state is SourceState.LIVE
    reader.close()


def test_reintenta_una_fuente_inalcanzable_sin_lanzar() -> None:
    attempts: list[int] = []

    def factory(_: str) -> FakeCapture:
        attempts.append(len(attempts) + 1)
        return FakeCapture(opened=False)

    reader = LiveVideoReader(
        capture_factory=factory,
        reconnect_delay_sec=0.01,
        max_reconnect_delay_sec=0.02,
        watchdog_enabled=False,
    )
    reader.open("http://no-existe/video")
    assert reader.state is SourceState.RECONNECTING

    result: list[tuple[bool, Any]] = []
    worker = threading.Thread(target=lambda: result.append(reader.read()), daemon=True)
    worker.start()

    deadline = time.monotonic() + 2
    while len(attempts) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)

    reader.stop()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert result == [(False, None)]
    assert len(attempts) >= 2
    assert reader.state is SourceState.STOPPED


def test_el_backoff_crece_y_se_acota() -> None:
    reader = LiveVideoReader(
        reconnect_delay_sec=2.0,
        max_reconnect_delay_sec=10.0,
        watchdog_enabled=False,
    )

    delays = [reader._next_delay() for _ in range(5)]

    assert delays == [2.0, 4.0, 8.0, 10.0, 10.0]


def test_el_watchdog_marca_sin_senal_y_libera_la_captura() -> None:
    now = [0.0]
    capture = FakeCapture(["frame-1"])
    reader = LiveVideoReader(
        capture_factory=lambda _: capture,
        clock=lambda: now[0],
        read_timeout_sec=1.0,
        watchdog_enabled=False,
    )
    reader.open("rtsp://camara/video")
    assert reader.read() == (True, "frame-1")

    now[0] = 3.0
    reader._check_watchdog()

    assert reader.state is SourceState.NO_SIGNAL
    assert reader.seconds_without_frame == 3.0
    assert capture.released
    reader.close()


def test_configura_buffer_y_timeout_de_lectura() -> None:
    capture = FakeCapture(["frame-1"])
    reader = LiveVideoReader(
        capture_factory=lambda _: capture,
        read_timeout_sec=2.0,
        watchdog_enabled=False,
    )

    reader.open("http://camara/video")

    props = {prop for prop, _ in capture.settings}
    assert cv2.CAP_PROP_BUFFERSIZE in props
    read_timeout = getattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC", None)
    if read_timeout is not None:
        assert read_timeout in props
    reader.close()


@pytest.mark.parametrize(
    "source",
    [
        "rtsp://192.168.1.10:554/video",
        "rtsps://camara/video",
        "http://192.168.1.10:8080/video",
        "https://camara/video",
        "udp://239.0.0.1:1234",
        "tcp://camara:5000",
    ],
)
def test_detecta_fuentes_en_vivo(source: str) -> None:
    assert is_live_source(source)


@pytest.mark.parametrize(
    "source",
    ["video.mp4", r"C:\videos\prueba.mp4", "/tmp/prueba.mp4", "0"],
)
def test_los_archivos_no_son_fuentes_en_vivo(source: str) -> None:
    assert not is_live_source(source)


def test_create_reader_elige_el_adaptador() -> None:
    assert isinstance(create_reader("rtsp://camara/video"), LiveVideoReader)
    assert isinstance(create_reader("video.mp4"), OpenCVVideoReader)
