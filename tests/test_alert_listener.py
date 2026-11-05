"""Pruebas de la máquina de estados de la luz de alerta (sin Tk)."""

from __future__ import annotations

from typing import Any

from incidentes.domain.entities import AlertAck, Incident
from incidentes.presentation.detector_controller import TkProgressListener


class FakeView:
    def __init__(self) -> None:
        self.alert_states: list[str] = []
        self.logs: list[tuple[str, str]] = []
        self.responses: list[tuple[str, bool]] = []

    def add_log(self, tag: str, message: str, timestamp: str = "") -> None:
        self.logs.append((tag, message))

    def set_alert_state(self, state: str, detail: str = "") -> None:
        self.alert_states.append(state)

    def set_central_badge(self, enabled: bool, available: bool) -> None:
        pass

    def show_central_response(self, text: str, error: bool = False) -> None:
        self.responses.append((text, error))


def make_listener(view: FakeView) -> TkProgressListener:
    return TkProgressListener(
        view,  # type: ignore[arg-type]
        live=True,
        muted=lambda: True,
        central_enabled=True,
        closing=lambda: False,
    )


def _incident() -> Any:
    return Incident("PELEA", 0.9, "pelea")


def test_no_vuelve_a_verde_si_queda_otra_alerta_pendiente() -> None:
    view = FakeView()
    listener = make_listener(view)

    listener.on_incident_alert(_incident(), 1.0, "2026-10-22T12:00:00")
    listener.on_alert_dispatched(AlertAck(True, "ALT-1"), _incident())
    listener.on_incident_alert(_incident(), 2.0, "2026-10-22T12:00:02")
    listener.on_incident_closed(_incident(), "ALT-1")
    assert "idle" not in view.alert_states

    listener.on_alert_dispatched(AlertAck(True, "ALT-2"), _incident())
    listener.on_incident_closed(_incident(), "ALT-2")
    assert view.alert_states[-1] == "idle"


def test_una_alerta_sin_ticket_solo_se_apaga_manualmente() -> None:
    view = FakeView()
    listener = make_listener(view)

    listener.on_incident_alert(_incident(), 1.0, "2026-10-22T12:00:00")
    listener.on_alert_dispatched(AlertAck(False, detail="central caída"), _incident())
    assert "idle" not in view.alert_states

    listener.acknowledge()
    assert view.alert_states[-1] == "idle"
