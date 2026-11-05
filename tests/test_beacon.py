"""Pruebas del latido del cliente y del estado de fuente que reporta."""

from __future__ import annotations

import time

from incidentes.infrastructure.alerting.client_beacon import ClientBeacon
from incidentes.infrastructure.alerting.http_alert_client import HttpAlertDispatcher


class FakeChannel:
    def __init__(self) -> None:
        self.registered: list[str] = []
        self.heartbeats: list[str] = []
        self.unregistered = 0

    def register(self, source_state: str = "") -> bool:
        self.registered.append(source_state)
        return True

    def heartbeat(self, source_state: str = "") -> bool:
        self.heartbeats.append(source_state)
        return True

    def unregister(self) -> bool:
        self.unregistered += 1
        return True


def test_beacon_envia_el_estado_actual_de_la_fuente() -> None:
    channel = FakeChannel()
    state = {"value": "UNKNOWN"}
    beacon = ClientBeacon(
        channel,
        interval_sec=0.01,
        state_provider=lambda: state["value"],
    )

    beacon.start()
    deadline = time.monotonic() + 1.0
    while not channel.registered and time.monotonic() < deadline:
        time.sleep(0.01)
    assert channel.registered == ["UNKNOWN"]

    state["value"] = "LIVE"
    deadline = time.monotonic() + 1.0
    while "LIVE" not in channel.heartbeats and time.monotonic() < deadline:
        time.sleep(0.01)
    beacon.stop()

    assert "LIVE" in channel.heartbeats
    assert channel.unregistered == 1


def test_heartbeat_http_incluye_el_estado_de_la_fuente() -> None:
    dispatcher = HttpAlertDispatcher(
        base_url="http://central.local", client_id="camara-01", api_key="secreto"
    )
    calls: list[tuple[str, dict]] = []

    def fake_post(path: str, payload: dict) -> dict:
        calls.append((path, payload))
        return {}

    dispatcher._post_json = fake_post  # type: ignore[method-assign]

    assert dispatcher.heartbeat("RECONNECTING") is True
    path, payload = calls[0]
    assert path == "/api/v1/clientes/heartbeat"
    assert payload["client_id"] == "camara-01"
    assert payload["source_state"] == "RECONNECTING"


def test_register_http_incluye_el_estado_de_la_fuente() -> None:
    dispatcher = HttpAlertDispatcher(
        base_url="http://central.local", client_id="camara-01"
    )
    calls: list[tuple[str, dict]] = []

    def fake_post(path: str, payload: dict) -> dict:
        calls.append((path, payload))
        return {}

    dispatcher._post_json = fake_post  # type: ignore[method-assign]

    assert dispatcher.register("LIVE") is True
    path, payload = calls[0]
    assert path == "/api/v1/clientes/registro"
    assert payload["source_state"] == "LIVE"
