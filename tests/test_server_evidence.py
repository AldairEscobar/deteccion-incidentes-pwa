"""Pruebas de la API de evidencia: JSON y captura persistidos en SQLite."""

from __future__ import annotations

import base64

from fastapi.testclient import TestClient

import incidentes.server.app as server_app
from incidentes.server.store import AlertStore


def test_api_guarda_y_sirve_json_y_captura(tmp_path, monkeypatch) -> None:
    store = AlertStore(tmp_path / "central.db")
    monkeypatch.setattr(server_app, "store", store)
    monkeypatch.setattr(server_app, "API_KEY", "")
    monkeypatch.setattr(server_app, "notify_units", lambda *args, **kwargs: False)
    client = TestClient(server_app.app)

    jpeg = b"\xff\xd8\xff\xe0captura-de-prueba"
    response = client.post(
        "/api/v1/alertas",
        json={
            "client_id": "camara-01",
            "type": "PELEA",
            "confidence": 0.93,
            "label": "pelea",
            "reason": "forcejeo entre dos personas",
            "timestamp": "2026-10-22T12:00:00+00:00",
            "location": {"address": "Centro"},
            "snapshot_b64": base64.b64encode(jpeg).decode(),
        },
    )
    assert response.status_code == 201, response.text
    ticket = response.json()["ticket"]

    detail = client.get(f"/api/v1/alertas/{ticket}")
    assert detail.status_code == 200
    record = detail.json()
    assert record["has_snapshot"] is True
    assert record["alert"]["snapshot_b64"] is None
    assert record["snapshot_url"] == f"/api/v1/alertas/{ticket}/captura"
    assert record["snapshot_sha256"]

    evidence = client.get(record["snapshot_url"])
    assert evidence.status_code == 200
    assert evidence.content == jpeg
    assert evidence.headers["content-type"] == "image/jpeg"

    as_json = client.get(f"/api/v1/alertas/{ticket}/json")
    assert as_json.status_code == 200
    assert as_json.json()["ticket"] == ticket
    assert "attachment" in as_json.headers["content-disposition"]

    store.close()
