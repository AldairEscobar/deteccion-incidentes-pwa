"""API REST de la central y servidor de la PWA (modo B).

La central puede funcionar de dos maneras:

1. **Solo despacho** (compatibilidad): recibe alertas de detectores externos
   por REST. Es el modo original.
2. **Central con análisis propio**: además, ejecuta el pipeline de dos etapas
   por cada cámara registrada desde la PWA. El operador abre el navegador, da
   de alta la URL de su cámara y la central se encarga de YOLO + Ollama.

Ejecutar::

    python -m incidentes.cli.serve --host 0.0.0.0 --port 8000

o directamente::

    python -m uvicorn incidentes.server.app:app --host 0.0.0.0 --port 8000

Endpoints principales::

    GET    /                             PWA instalable
    GET    /api/v1/info                  descubrimiento del servicio
    GET    /health                       estado del servicio
    POST   /api/v1/alertas               recibe una alerta y despacha la ayuda
    GET    /api/v1/alertas               historial (más recientes primero)
    GET    /api/v1/alertas/{ticket}      detalle de una alerta
    GET    /api/v1/alertas/{ticket}/json     JSON del incidente (descarga)
    GET    /api/v1/alertas/{ticket}/captura  captura JPEG de evidencia
    PATCH  /api/v1/alertas/{ticket}      actualiza el estado (p. ej. "atendida")
    GET    /api/v1/camaras               cámaras con su estado en vivo
    POST   /api/v1/camaras               alta + arranque del análisis
    PATCH  /api/v1/camaras/{id}          cambios parciales
    DELETE /api/v1/camaras/{id}          baja (detiene el pipeline)
    POST   /api/v1/camaras/{id}/iniciar  arranca el análisis
    POST   /api/v1/camaras/{id}/detener  detiene el análisis
    GET    /api/v1/camaras/{id}/stream   vista previa MJPEG
    GET    /api/v1/camaras/{id}/snapshot último frame anotado (JPEG)
    WS     /ws/eventos                   alertas, bitácora y estados en vivo
    POST   /api/v1/clientes/registro     alta de un cliente externo
    POST   /api/v1/clientes/heartbeat    latido de vida
    GET    /api/v1/clientes              clientes conectados
    DELETE /api/v1/clientes/{client_id}  baja de un cliente

Autenticación: si ``CENTRAL_API_KEY`` está definida, exige la cabecera
``X-API-Key`` (o ``?api_key=`` en las rutas que consume un ``<img>`` o el
WebSocket). Aviso a unidades: si ``DISPATCH_WEBHOOK_URL`` está definida, hace
POST a ese webhook.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from incidentes import __version__
from incidentes.server.cameras import CameraStore
from incidentes.server.dispatch import DispatchPlan, notify_units, plan_dispatch
from incidentes.server.events import EventBus
from incidentes.server.models import (
    AlertAck,
    AlertIn,
    AlertRecord,
    CameraIn,
    CameraOut,
    CameraUpdate,
    ClientInfo,
    ClientRegister,
    HeartbeatIn,
    StatusUpdate,
)
from incidentes.server.registry import ClientRegistry
from incidentes.server.store import DEFAULT_DB_PATH, AlertStore
from incidentes.server.supervisor import CameraSupervisor, build_use_case_factory

log = logging.getLogger(__name__)

API_KEY = os.getenv("CENTRAL_API_KEY", "")
CLIENT_ALIVE_SEC = float(os.getenv("CLIENT_ALIVE_SEC", "30"))
DB_PATH = os.getenv("CENTRAL_DB_PATH", DEFAULT_DB_PATH)
# Permite arrancar la API sin lanzar los pipelines guardados (tests, demo de
# solo despacho). En uso normal las cámaras habilitadas arrancan solas.
AUTOSTART = os.getenv("CENTRAL_AUTOSTART", "1").strip().lower() not in {"0", "false", "no"}

WEB_DIR = Path(__file__).with_name("web")

# Persistencia compartida entre la API, el supervisor y la PWA.
store = AlertStore(DB_PATH)
camera_store = CameraStore(DB_PATH)
registry = ClientRegistry(alive_seconds=CLIENT_ALIVE_SEC)
bus = EventBus()


def _register_alert(alert: AlertIn) -> AlertRecord:
    """Calcula el despacho y persiste la alerta (común a REST y al supervisor).

    Se mantiene como función de módulo para que los tests puedan sustituir
    ``store`` y ``notify_units`` sin tocar la lógica de despacho.
    """
    plan: DispatchPlan = plan_dispatch(alert)
    record = AlertRecord(
        ticket="",
        status="registrada",
        priority=plan["priority"],
        eta_min=plan["eta_min"],
        units=plan["units"],
        received_at=datetime.now(UTC).isoformat(timespec="seconds"),
        alert=alert,
    )
    saved = store.add(record)  # asigna el ticket
    if notify_units(saved.ticket, alert, plan):
        # update_status devuelve el JSON persistido (dict), no el modelo; se
        # conserva el AlertRecord para responder con el mismo contrato.
        store.update_status(saved.ticket, "despachada")
        saved = saved.model_copy(update={"status": "despachada"})
    log.warning("ALERTA %s (%s): %s", saved.ticket, saved.status, plan["message"])
    return saved


supervisor = CameraSupervisor(
    camera_store,
    bus,
    _register_alert,
    build_use_case_factory(),
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Conecta el bus al bucle de uvicorn y arranca las cámaras guardadas."""
    bus.bind_loop(asyncio.get_running_loop())
    if AUTOSTART:
        supervisor.start_all()
    log.info("central lista en http://localhost:8000 (PWA en /)")
    try:
        yield
    finally:
        supervisor.stop_all()
        bus.unbind()
        camera_store.close()
        store.close()


app = FastAPI(
    title="Central de Alertas",
    version=__version__,
    description="API REST y PWA de la central de detección de incidentes.",
    lifespan=lifespan,
)


def require_api_key(
    x_api_key: str | None = Header(default=None),
    api_key: str | None = Query(default=None),
) -> None:
    """Valida la clave solo si la central tiene una configurada.

    Se acepta ``?api_key=`` además de la cabecera porque ``<img>`` y los
    WebSocket del navegador no pueden enviar cabeceras personalizadas.
    """
    if API_KEY and x_api_key != API_KEY and api_key != API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key inválida o ausente",
        )


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


# ---------------------------------------------------------------------------
# Descubrimiento y salud
# ---------------------------------------------------------------------------
@app.get("/api/v1/info", include_in_schema=False)
def info() -> dict:
    """Descubrimiento del servicio (la raíz ``/`` sirve la PWA)."""
    return {
        "service": "central-alertas",
        "version": __version__,
        "docs": "/docs",
        "health": "/health",
        "pwa": "/",
        "camaras": camera_store.count(),
    }


@app.get("/health")
def health() -> dict:
    """Estado del servicio, alertas, cámaras y clientes en línea."""
    return {
        "status": "ok",
        "service": "central-alertas",
        "alertas": store.count(),
        "camaras": camera_store.count(),
        "clientes_en_linea": registry.count(),
    }


# ---------------------------------------------------------------------------
# Alertas
# ---------------------------------------------------------------------------
@app.post(
    "/api/v1/alertas",
    response_model=AlertAck,
    status_code=status.HTTP_201_CREATED,
)
def receive_alert(alert: AlertIn, _: None = Depends(require_api_key)) -> AlertAck:
    """Registra la alerta, calcula el despacho y avisa a las unidades."""
    saved = _register_alert(alert)
    # Difunde también las alertas de clientes externos (no solo las del
    # supervisor en proceso), para que la PWA las muestre en vivo.
    bus.publish_event("alerta", saved.model_dump())
    plan = plan_dispatch(alert)
    return AlertAck(
        ticket=saved.ticket,
        status=saved.status,
        priority=saved.priority,
        eta_min=saved.eta_min,
        units=saved.units,
        message=f"{plan['notify']} Ticket {saved.ticket}.",
        received_at=saved.received_at,
    )


@app.get("/api/v1/alertas", response_model=list[AlertRecord])
def list_alerts(
    limit: int = Query(default=100, ge=1, le=1000),
    _: None = Depends(require_api_key),
) -> list[dict]:
    """Historial de alertas, de la más reciente a la más antigua."""
    return store.list(limit)


@app.get("/api/v1/alertas/{ticket}", response_model=AlertRecord)
def get_alert(ticket: str, _: None = Depends(require_api_key)) -> dict:
    record = store.get(ticket)
    if record is None:
        raise HTTPException(status_code=404, detail=f"ticket {ticket} no existe")
    return record


@app.get("/api/v1/alertas/{ticket}/json")
def get_alert_json(ticket: str, _: None = Depends(require_api_key)) -> JSONResponse:
    """Descarga el JSON persistido del incidente, con metadatos de evidencia."""
    record = store.get(ticket)
    if record is None:
        raise HTTPException(status_code=404, detail=f"ticket {ticket} no existe")
    return JSONResponse(
        content=record,
        headers={"Content-Disposition": f'attachment; filename="{ticket}.json"'},
    )


@app.get("/api/v1/alertas/{ticket}/captura")
def get_alert_snapshot(ticket: str, _: None = Depends(require_api_key)) -> Response:
    """Devuelve la captura JPEG que el cliente adjuntó a la alerta."""
    evidence = store.get_snapshot(ticket)
    if evidence is None:
        raise HTTPException(
            status_code=404, detail=f"ticket {ticket} no tiene captura"
        )
    data, mime = evidence
    return Response(
        content=data,
        media_type=mime or "image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@app.patch("/api/v1/alertas/{ticket}", response_model=AlertRecord)
def update_alert(
    ticket: str,
    update: StatusUpdate,
    _: None = Depends(require_api_key),
) -> dict:
    record = store.update_status(ticket, update.status)
    if record is None:
        raise HTTPException(status_code=404, detail=f"ticket {ticket} no existe")
    log.info("estado de %s -> %s", ticket, update.status)
    bus.publish_event("alerta_actualizada", record)
    return record


# ---------------------------------------------------------------------------
# Cámaras (la PWA da de alta la fuente y la central analiza)
# ---------------------------------------------------------------------------
@app.get("/api/v1/camaras", response_model=list[CameraOut])
def list_cameras(_: None = Depends(require_api_key)) -> list[dict]:
    return supervisor.statuses()


@app.post(
    "/api/v1/camaras",
    response_model=CameraOut,
    status_code=status.HTTP_201_CREATED,
)
def create_camera(
    payload: CameraIn, _: None = Depends(require_api_key)
) -> dict:
    """Registra la cámara y, si está habilitada, arranca su análisis."""
    camera = camera_store.add(
        payload.name,
        payload.source,
        address=payload.address,
        lat=payload.lat,
        lon=payload.lon,
        enabled=payload.enabled,
    )
    log.warning(
        "CÁMARA registrada: %s (%s) fuente=%s",
        camera.name,
        camera.id,
        camera.source.split("://", 1)[0] if "://" in camera.source else "archivo",
    )
    if payload.enabled:
        supervisor.start(camera.id)
    return supervisor.status(camera.id) or {}


@app.patch("/api/v1/camaras/{camera_id}", response_model=CameraOut)
def update_camera(
    camera_id: str,
    payload: CameraUpdate,
    _: None = Depends(require_api_key),
) -> dict:
    previous = camera_store.get(camera_id)
    if previous is None:
        raise HTTPException(status_code=404, detail=f"cámara {camera_id} no existe")

    fields = payload.model_dump(exclude_unset=True)
    # ``None`` solo es válido para lat/lon; en el resto significa "no cambiar".
    for key in ("name", "source", "address", "enabled"):
        if key in fields and fields[key] is None:
            fields.pop(key)
    updated = camera_store.update(camera_id, **fields)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"cámara {camera_id} no existe")

    source_changed = "source" in fields and updated.source != previous.source
    if source_changed or updated.enabled is False:
        supervisor.stop(camera_id)
    if updated.enabled and (source_changed or not previous.enabled):
        supervisor.start(camera_id)

    return supervisor.status(camera_id) or {}


@app.delete("/api/v1/camaras/{camera_id}")
def delete_camera(camera_id: str, _: None = Depends(require_api_key)) -> dict:
    if not supervisor.delete(camera_id):
        raise HTTPException(status_code=404, detail=f"cámara {camera_id} no existe")
    log.info("cámara %s eliminada", camera_id)
    return {"removed": camera_id}


@app.post("/api/v1/camaras/{camera_id}/iniciar", response_model=CameraOut)
def start_camera(camera_id: str, _: None = Depends(require_api_key)) -> dict:
    if supervisor.start(camera_id) is None:
        raise HTTPException(status_code=404, detail=f"cámara {camera_id} no existe")
    return supervisor.status(camera_id) or {}


@app.post("/api/v1/camaras/{camera_id}/detener", response_model=CameraOut)
def stop_camera(camera_id: str, _: None = Depends(require_api_key)) -> dict:
    status_now = supervisor.status(camera_id)
    if status_now is None:
        raise HTTPException(status_code=404, detail=f"cámara {camera_id} no existe")
    supervisor.stop(camera_id)
    return supervisor.status(camera_id) or status_now


@app.get("/api/v1/camaras/{camera_id}/snapshot")
def camera_snapshot(camera_id: str, _: None = Depends(require_api_key)) -> Response:
    """Último frame anotado disponible, útil como miniatura."""
    runtime = supervisor.runtime(camera_id)
    jpeg = runtime.snapshot() if runtime is not None else None
    if jpeg is None:
        raise HTTPException(
            status_code=404, detail=f"cámara {camera_id} sin imagen todavía"
        )
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/v1/camaras/{camera_id}/stream")
def camera_stream(camera_id: str, _: None = Depends(require_api_key)) -> Response:
    """Vista previa MJPEG del frame anotado (para un ``<img>`` en la PWA)."""
    runtime = supervisor.runtime(camera_id)
    if runtime is None:
        raise HTTPException(
            status_code=404, detail=f"cámara {camera_id} no está en marcha"
        )
    return StreamingResponse(
        runtime.mjpeg(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# WebSocket: alertas, bitácora y estados en tiempo real
# ---------------------------------------------------------------------------
@app.websocket("/ws/eventos")
async def events_socket(websocket: WebSocket) -> None:
    """Difunde a la PWA las alertas y la bitácora de todas las cámaras."""
    if API_KEY and websocket.query_params.get("api_key") != API_KEY:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    queue = bus.subscribe()
    try:
        await websocket.send_json(
            {
                "kind": "hola",
                "data": {
                    "version": __version__,
                    "camaras": camera_store.count(),
                    "alertas": store.count(),
                },
            }
        )
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=20.0)
            except TimeoutError:
                message = {"kind": "ping", "data": None}
            await websocket.send_json(message)
    except WebSocketDisconnect:
        log.debug("PWA desconectada del bus de eventos")
    except Exception:
        log.debug("WebSocket cerrado de forma inesperada", exc_info=True)
    finally:
        bus.unsubscribe(queue)


# ---------------------------------------------------------------------------
# Clientes externos (multi-cliente): alta + heartbeat + listado
# ---------------------------------------------------------------------------
@app.post("/api/v1/clientes/registro", response_model=ClientInfo)
def register_client(
    info: ClientRegister,
    request: Request,
    _: None = Depends(require_api_key),
) -> dict:
    record = registry.register(
        info.client_id,
        name=info.name,
        address=info.address,
        lat=info.lat,
        lon=info.lon,
        ip=_client_ip(request),
        source_state=info.source_state,
    )
    if record.pop("nuevo", False):
        log.warning(
            "CLIENTE conectado: %s (%s) desde %s",
            record["client_id"],
            record["address"] or "sin dirección",
            record["ip"],
        )
    else:
        log.info("cliente %s re-registrado", info.client_id)
    return record


@app.post("/api/v1/clientes/heartbeat", response_model=ClientInfo)
def client_heartbeat(
    info: HeartbeatIn,
    request: Request,
    _: None = Depends(require_api_key),
) -> dict:
    record = registry.heartbeat(
        info.client_id, ip=_client_ip(request), source_state=info.source_state
    )
    if record is None:
        # El cliente debe volver a registrarse (la central se reinició).
        raise HTTPException(
            status_code=404,
            detail=f"cliente {info.client_id} no registrado",
        )
    return record


@app.get("/api/v1/clientes", response_model=list[ClientInfo])
def list_clients(
    only_alive: bool = Query(default=True),
    _: None = Depends(require_api_key),
) -> list[dict]:
    return registry.list(only_alive=only_alive)


@app.delete("/api/v1/clientes/{client_id}")
def remove_client(client_id: str, _: None = Depends(require_api_key)) -> dict:
    if not registry.remove(client_id):
        raise HTTPException(status_code=404, detail=f"cliente {client_id} no existe")
    log.info("cliente %s se desconectó", client_id)
    return {"removed": client_id}


# ---------------------------------------------------------------------------
# PWA (se monta al final para no tapar las rutas de la API)
# ---------------------------------------------------------------------------
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
