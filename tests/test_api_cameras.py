"""Pruebas de la API de cámaras de la PWA (sin arrancar YOLO)."""

from __future__ import annotations

from fastapi.testclient import TestClient

import incidentes.server.app as server_app
from incidentes.server.cameras import CameraStore
from incidentes.server.supervisor import CameraSupervisor


class FakeSupervisor:
    """Supervisor de mentira: registra llamadas y devuelve estados estáticos."""

    def __init__(self, store: CameraStore) -> None:
        self.store = store
        self.started: list[str] = []
        self.stopped: list[str] = []

    def _status(self, camera_id: str) -> dict | None:
        camera = self.store.get(camera_id)
        if camera is None:
            return None
        return {
            **CameraSupervisor._camera_fields(camera),
            "state": "detenida",
            "running": False,
            "last_error": "",
            "last_event": "",
            "started_at": "",
        }

    def status(self, camera_id: str) -> dict | None:
        return self._status(camera_id)

    def statuses(self) -> list[dict]:
        return [self._status(camera.id) or {} for camera in self.store.list()]

    def start(self, camera_id: str) -> dict | None:
        if self.store.get(camera_id) is None:
            return None
        self.started.append(camera_id)
        return self._status(camera_id)

    def stop(self, camera_id: str) -> bool:
        self.stopped.append(camera_id)
        return True

    def delete(self, camera_id: str) -> bool:
        self.stop(camera_id)
        return self.store.delete(camera_id)

    def runtime(self, camera_id: str):
        return None


def _setup(tmp_path, monkeypatch):
    store = CameraStore(tmp_path / "central.db")
    supervisor = FakeSupervisor(store)
    monkeypatch.setattr(server_app, "camera_store", store)
    monkeypatch.setattr(server_app, "supervisor", supervisor)
    monkeypatch.setattr(server_app, "API_KEY", "")
    return TestClient(server_app.app), store, supervisor


def test_alta_arranca_y_lista(tmp_path, monkeypatch) -> None:
    client, store, supervisor = _setup(tmp_path, monkeypatch)

    response = client.post(
        "/api/v1/camaras",
        json={
            "name": "Pasillo",
            "source": "rtsp://admin:secreto@10.0.0.5:554/stream1",
            "address": "Piso 3",
            "lat": -12.05,
            "lon": -77.04,
        },
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["name"] == "Pasillo"
    assert created["state"] == "detenida"
    assert "***" in created["source"]
    assert "secreto" not in created["source"]
    assert supervisor.started == [created["id"]]
    assert store.count() == 1

    listed = client.get("/api/v1/camaras")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [created["id"]]


def test_alta_sin_arrancar(tmp_path, monkeypatch) -> None:
    client, store, supervisor = _setup(tmp_path, monkeypatch)
    response = client.post(
        "/api/v1/camaras",
        json={"name": "Patio", "source": "rtsp://10.0.0.9/1", "enabled": False},
    )
    assert response.status_code == 201
    assert supervisor.started == []


def test_patch_detiene_y_cambia_fuente(tmp_path, monkeypatch) -> None:
    client, store, supervisor = _setup(tmp_path, monkeypatch)
    created = client.post(
        "/api/v1/camaras",
        json={"name": "Entrada", "source": "rtsp://10.0.0.5/1"},
    ).json()
    supervisor.started.clear()

    disabled = client.patch(f"/api/v1/camaras/{created['id']}", json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert created["id"] in supervisor.stopped
    assert supervisor.started == []

    client.patch(f"/api/v1/camaras/{created['id']}", json={"enabled": True})
    assert created["id"] in supervisor.started

    supervisor.stopped.clear()
    supervisor.started.clear()
    changed = client.patch(
        f"/api/v1/camaras/{created['id']}", json={"source": "rtsp://10.0.0.6/2"}
    )
    assert changed.status_code == 200
    assert created["id"] in supervisor.stopped
    assert created["id"] in supervisor.started


def test_iniciar_detener_y_eliminar(tmp_path, monkeypatch) -> None:
    client, store, supervisor = _setup(tmp_path, monkeypatch)
    created = client.post(
        "/api/v1/camaras",
        json={"name": "Patio", "source": "rtsp://10.0.0.9/1"},
    ).json()

    assert client.post(f"/api/v1/camaras/{created['id']}/detener").status_code == 200
    assert client.post(f"/api/v1/camaras/{created['id']}/iniciar").status_code == 200

    removed = client.delete(f"/api/v1/camaras/{created['id']}")
    assert removed.status_code == 200
    assert removed.json() == {"removed": created["id"]}
    assert store.count() == 0


def test_camara_inexistente_devuelve_404(tmp_path, monkeypatch) -> None:
    client, _, _ = _setup(tmp_path, monkeypatch)
    assert client.post("/api/v1/camaras/cam-nope/iniciar").status_code == 404
    assert client.post("/api/v1/camaras/cam-nope/detener").status_code == 404
    assert client.delete("/api/v1/camaras/cam-nope").status_code == 404
    assert client.patch("/api/v1/camaras/cam-nope", json={"name": "x"}).status_code == 404
    assert client.get("/api/v1/camaras/cam-nope/snapshot").status_code == 404


def test_validacion_de_payload(tmp_path, monkeypatch) -> None:
    client, _, _ = _setup(tmp_path, monkeypatch)
    assert client.post("/api/v1/camaras", json={"name": "", "source": "x"}).status_code == 422
    assert client.post("/api/v1/camaras", json={"name": "x"}).status_code == 422
