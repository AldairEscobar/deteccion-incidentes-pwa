"""Pruebas de la lógica de priorización y despacho de la central."""

from __future__ import annotations

from incidentes.server.dispatch import plan_dispatch
from incidentes.server.models import AlertIn, Location


def make_alert(incident_type: str, address: str = "") -> AlertIn:
    return AlertIn(
        client_id="camara-01",
        type=incident_type,
        confidence=0.9,
        label="CONFIRMADO IA",
        location=Location(address=address),
    )


def test_weapon_is_critical() -> None:
    plan = plan_dispatch(make_alert("ARMA"))
    assert plan["priority"] == "CRITICA"
    assert plan["eta_min"] == 3
    assert "unidad-medica" in plan["units"]


def test_fight_is_high_priority() -> None:
    plan = plan_dispatch(make_alert("PELEA"))
    assert plan["priority"] == "ALTA"
    assert "patrulla" in plan["units"]


def test_unknown_type_defaults_to_medium() -> None:
    plan = plan_dispatch(make_alert("DESCONOCIDO"))
    assert plan["priority"] == "MEDIA"


def test_message_includes_client_location_and_confidence() -> None:
    plan = plan_dispatch(make_alert("PELEA", address="Av. Abancay 1234"))
    assert "camara-01" in plan["message"]
    assert "Av. Abancay 1234" in plan["message"]
    assert "0.90" in plan["message"]


def test_coordinates_are_appended_when_present() -> None:
    alert = AlertIn(
        client_id="camara-02",
        type="PELEA",
        location=Location(lat=-12.04637, lon=-77.04279, address="Centro"),
    )
    plan = plan_dispatch(alert)
    assert "(-12.04637, -77.04279)" in plan["message"]


def test_notify_message_mentions_eta_and_ticket() -> None:
    plan = plan_dispatch(make_alert("CAIDA"))
    assert "Ayuda en camino" in plan["notify"]
    assert "8 min" in plan["notify"]
