"""Pruebas del registro persistente de cámaras de la PWA."""

from __future__ import annotations

from incidentes.server.cameras import (
    CameraStore,
    mask_source,
    new_camera_id,
)


def test_alta_lista_y_busqueda() -> None:
    store = CameraStore()
    camera = store.add(
        "Pasillo",
        "rtsp://192.168.1.50:554/stream1",
        address="Piso 3",
        lat=-12.05,
        lon=-77.04,
    )

    assert camera.id.startswith("cam-")
    assert store.count() == 1
    assert store.get(camera.id) == camera
    listed = store.list()
    assert [item.id for item in listed] == [camera.id]


def test_update_parcial_y_delete() -> None:
    store = CameraStore()
    camera = store.add("Entrada", "rtsp://10.0.0.2/1")

    updated = store.update(camera.id, name="Entrada principal", enabled=False)
    assert updated is not None
    assert updated.name == "Entrada principal"
    assert updated.enabled is False
    assert updated.source == camera.source  # no se toca lo no enviado

    assert store.update("cam-inexistente", name="x") is None
    assert store.delete(camera.id) is True
    assert store.delete(camera.id) is False
    assert store.get(camera.id) is None


def test_persistencia_entre_aperturas(tmp_path) -> None:
    db = tmp_path / "camaras.db"
    first = CameraStore(db)
    camera = first.add("Patio", "http://192.168.1.60/mjpg")
    first.close()

    second = CameraStore(db)
    assert second.get(camera.id) == camera
    second.close()


def test_mascara_de_credenciales() -> None:
    assert mask_source("rtsp://admin:secreto@10.0.0.5:554/x") == (
        "rtsp://admin:***@10.0.0.5:554/x"
    )
    assert mask_source("rtsp://10.0.0.5:554/x") == "rtsp://10.0.0.5:554/x"
    assert mask_source("C:/videos/demo.mp4") == "C:/videos/demo.mp4"
    assert mask_source("") == ""


def test_identificadores_unicos() -> None:
    ids = {new_camera_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(identifier.startswith("cam-") for identifier in ids)
