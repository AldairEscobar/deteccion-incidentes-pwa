"""Lector de fuentes en vivo (cámara IP) con reconexión automática.

La diferencia con ``OpenCVVideoReader`` es el contrato: un archivo termina
cuando se agota, pero una cámara IP no. Este adaptador oculta las caídas al
caso de uso: ``read`` sigue esperando y solo devuelve ``ok=False`` cuando se
pide parar. Mientras tanto expone ``state`` para que la UI muestre "sin señal"
o "reconectando".

La reconexión usa backoff exponencial y un watchdog de frames: OpenCV puede
dejar la captura "abierta" sin entregar imágenes (típico en RTSP), así que
esperar a que ``read`` falle no es suficiente.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import cv2

from incidentes.domain.entities import SourceState
from incidentes.domain.ports import VideoReader

log = logging.getLogger(__name__)


class LiveVideoReader(VideoReader):
    """Mantiene una fuente IP abierta y se reconecta ante cualquier caída."""

    def __init__(
        self,
        *,
        open_timeout_sec: float = 5.0,
        read_timeout_sec: float = 8.0,
        reconnect_delay_sec: float = 2.0,
        max_reconnect_delay_sec: float = 30.0,
        buffer_size: int = 1,
        capture_factory: Callable[[str], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        watchdog_enabled: bool = True,
    ) -> None:
        self._open_timeout = max(open_timeout_sec, 0.1)
        self._read_timeout = max(read_timeout_sec, 0.1)
        self._base_delay = max(reconnect_delay_sec, 0.01)
        self._max_delay = max(max_reconnect_delay_sec, self._base_delay)
        self._buffer_size = buffer_size
        self._capture_factory = capture_factory or self._default_capture
        self._clock = clock
        self._watchdog_enabled = watchdog_enabled
        self._watchdog_interval = max(0.5, min(5.0, self._read_timeout / 2))

        self._source = ""
        self._capture: Any = None
        self._state = SourceState.UNKNOWN
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._watchdog_thread: threading.Thread | None = None
        self._last_frame_at: float | None = None
        self._attempt = 0
        self._open_attempts = 0

    # ------------------------------------------------------------------ estado
    @property
    def state(self) -> SourceState:
        return self._state

    @property
    def source(self) -> str:
        return self._source

    @property
    def open_attempts(self) -> int:
        """Conexiones intentadas desde ``open`` (incluye reconexiones)."""
        return self._open_attempts

    @property
    def seconds_without_frame(self) -> float | None:
        """Segundos desde el último frame, o ``None`` si aún no llegó ninguno."""
        with self._lock:
            last = self._last_frame_at
        if last is None:
            return None
        return max(0.0, self._clock() - last)

    # ------------------------------------------------------------------ lectura
    def open(self, path: str) -> None:
        """Prepara la fuente; no bloquea esperando a que responda."""
        if not path.strip():
            raise ValueError("La fuente en vivo está vacía.")

        # Un open repetido no debe dejar un watchdog huérfano.
        self._stop_event.set()
        self._stop_watchdog()
        self._stop_event.clear()

        self._source = path
        self._attempt = 0
        self._open_attempts = 0
        self._last_frame_at = None
        self._state = SourceState.CONNECTING

        self._capture = self._connect(path)
        if self._capture is None:
            self._state = SourceState.RECONNECTING

        self._start_watchdog()

    def read(self) -> tuple[bool, Any]:
        """Devuelve ``(True, frame)``; ``(False, None)`` solo si se pidió parar.

        Las caídas no se propagan: se registran, se cambia el estado y se
        reintenta con backoff hasta que vuelva la señal o se llame a ``stop``.
        """
        while not self._stop_event.is_set():
            with self._lock:
                capture = self._capture

            if capture is None:
                if not self._reconnect():
                    break
                continue

            ok, frame = capture.read()
            if self._stop_event.is_set():
                break

            if ok and frame is not None:
                with self._lock:
                    self._last_frame_at = self._clock()
                self._state = SourceState.LIVE
                self._attempt = 0
                return True, frame

            log.warning("se perdió la señal de %s", self._source)
            self._state = SourceState.NO_SIGNAL
            self._release_capture()

        return False, None

    def stop(self) -> None:
        """Desbloquea ``read`` y libera la captura."""
        self.close()

    def close(self) -> None:
        self._stop_event.set()
        self._release_capture()
        self._stop_watchdog()
        self._state = SourceState.STOPPED

    # ------------------------------------------------------------- conexión
    def _default_capture(self, source: str) -> Any:
        """Abre la fuente con timeouts de red cuando el backend lo permite.

        Sin ``CAP_PROP_OPEN_TIMEOUT_MSEC`` OpenCV puede tardar ~15 s en
        descartar una URL inalcanzable, y ese bloqueo se nota al reconectar.
        """
        timeout_ms = int(self._open_timeout * 1000)
        params: list[int] = []
        if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
            params += [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms]
        if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
            params += [
                cv2.CAP_PROP_READ_TIMEOUT_MSEC,
                int(self._read_timeout * 1000),
            ]

        if params:
            try:
                return cv2.VideoCapture(source, cv2.CAP_FFMPEG, params)
            except Exception:
                log.debug(
                    "el constructor con timeouts falló; se reintenta sin ellos",
                    exc_info=True,
                )
        return cv2.VideoCapture(source)

    def _connect(self, source: str) -> Any:
        """Intenta abrir la fuente una vez; ``None`` si no está disponible."""
        self._open_attempts += 1
        try:
            capture = self._capture_factory(source)
        except Exception:
            log.exception("no se pudo abrir la fuente %s", source)
            return None

        if capture is None:
            return None

        self._configure(capture)
        if not capture.isOpened():
            self._safe_release(capture)
            log.warning("la fuente %s no respondió (intento %d)", source, self._open_attempts)
            return None

        with self._lock:
            self._last_frame_at = self._clock()
        self._state = SourceState.CONNECTING
        log.info("fuente conectada: %s", source)
        return capture

    def _configure(self, capture: Any) -> None:
        """Reduce el buffer y, si el backend lo soporta, limita la lectura."""
        try:
            capture.set(cv2.CAP_PROP_BUFFERSIZE, self._buffer_size)
        except Exception:
            log.debug("el backend no acepta CAP_PROP_BUFFERSIZE", exc_info=True)

        read_timeout = getattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC", None)
        if read_timeout is not None:
            try:
                capture.set(read_timeout, int(self._read_timeout * 1000))
            except Exception:
                log.debug("el backend no acepta el timeout de lectura", exc_info=True)

    def _reconnect(self) -> bool:
        """Espera el backoff y reintenta. Devuelve ``False`` si se pidió parar."""
        attempt = self._attempt + 1
        delay = self._next_delay()
        self._state = SourceState.RECONNECTING
        log.warning(
            "reconectando %s en %.1fs (intento %d)",
            self._source,
            delay,
            attempt,
        )
        if self._stop_event.wait(delay):
            return False

        capture = self._connect(self._source)
        if capture is None:
            return True

        with self._lock:
            self._capture = capture
        return True

    def _next_delay(self) -> float:
        """Backoff exponencial acotado según el número de intentos fallidos."""
        exponent = min(self._attempt, 30)
        delay = min(self._base_delay * (2**exponent), self._max_delay)
        self._attempt += 1
        return delay

    def _release_capture(self) -> None:
        with self._lock:
            capture, self._capture = self._capture, None
        if capture is not None:
            self._safe_release(capture)

    @staticmethod
    def _safe_release(capture: Any) -> None:
        try:
            capture.release()
        except Exception:
            log.debug("fallo al liberar la captura", exc_info=True)

    # ------------------------------------------------------------- watchdog
    def _start_watchdog(self) -> None:
        if not self._watchdog_enabled:
            return
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            daemon=True,
            name="video-watchdog",
        )
        self._watchdog_thread.start()

    def _stop_watchdog(self) -> None:
        thread = self._watchdog_thread
        self._watchdog_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def _watchdog_loop(self) -> None:
        while not self._stop_event.wait(self._watchdog_interval):
            self._check_watchdog()

    def _check_watchdog(self) -> None:
        """Marca la fuente sin señal si pasó demasiado tiempo sin frames.

        Se libera la captura para desbloquear un ``read`` que quedó colgado en
        el backend; el bucle de ``read`` se encarga de reconectar.
        """
        if self._state is not SourceState.LIVE:
            return
        elapsed = self.seconds_without_frame
        if elapsed is None or elapsed <= self._read_timeout:
            return

        log.warning(
            "watchdog: %.1fs sin frames de %s; se fuerza reconexión",
            elapsed,
            self._source,
        )
        self._state = SourceState.NO_SIGNAL
        self._release_capture()
