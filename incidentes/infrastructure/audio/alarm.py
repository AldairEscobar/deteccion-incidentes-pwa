"""Alarma audible multiplataforma.

En Windows usa ``winsound``; en otros sistemas emite la campana del terminal.
No bloquea: el patrón de beeps corre en un hilo daemon.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

try:  # pragma: no cover - depende de la plataforma
    import winsound

    _HAS_WINSOUND = True
except ImportError:  # pragma: no cover
    _HAS_WINSOUND = False


def _beep_pattern(times: int = 3, freq: int = 880, duration_ms: int = 300) -> None:
    for index in range(times):
        try:
            if _HAS_WINSOUND:
                winsound.Beep(freq, duration_ms)
            else:
                print("\a", end="", flush=True)
                time.sleep(duration_ms / 1000)
        except Exception as exc:
            log.error("fallo al sonar la alarma (beep %d/%d): %s", index + 1, times, exc)
            return
        time.sleep(0.15)


def sound_alarm(times: int = 3) -> None:
    """Reproduce la alarma sin bloquear. Nunca lanza."""
    try:
        thread = threading.Thread(
            target=_beep_pattern, args=(times,), daemon=True, name="alarm"
        )
        thread.start()
        log.warning("ALARMA SONORA activada (%d beeps)", times)
    except Exception:
        log.exception("no se pudo lanzar el hilo de la alarma")
