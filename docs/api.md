# API de la central

Servicio REST que recibe las alertas confirmadas del detector, calcula la
ayuda a despachar y mantiene el estado de los clientes conectados.

## Ejecución

Central con PWA y análisis propio (modo recomendado para demo):

```bash
python -m incidentes.cli.serve
```

Dashboard de escritorio (modo original, sin PWA):

```bash
python -m incidentes.cli.central
```

Solo API (sin interfaz gráfica):

```bash
python -m uvicorn incidentes.server.app:app --host 0.0.0.0 --port 8000
```

La PWA queda en <http://localhost:8000> y la documentación interactiva en
<http://localhost:8000/docs>.

## Autenticación

Si la variable `CENTRAL_API_KEY` está definida, **todas** las peticiones deben
incluir la cabecera:

```http
X-API-Key: <tu-clave>
```

Las rutas que consume un `<img>` o un WebSocket (vista previa MJPEG, capturas y
`/ws/eventos`) aceptan además `?api_key=<tu-clave>`, porque el navegador no
permite enviar cabeceras en esos contextos. La PWA guarda la clave en
`localStorage` desde **Ajustes**.

Si no está definida, la API queda abierta (pensada para desarrollo local).

## Variables de entorno

| Variable | Defecto | Efecto |
|---|---|---|
| `CENTRAL_API_KEY` | *(vacío)* | Exige `X-API-Key` (o `?api_key=`) en cada petición. |
| `CENTRAL_DB_PATH` | `data/incidentes.db` | Base SQLite con alertas, cámaras y capturas. |
| `CENTRAL_AUTOSTART` | `1` | Arranca al iniciar los pipelines de las cámaras habilitadas. |
| `DISPATCH_WEBHOOK_URL` | *(vacío)* | POST a ese webhook al despachar una alerta. |
| `CLIENT_ALIVE_SEC` | `30` | Segundos sin latido para marcar un cliente caído. |

## Endpoints

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/` | PWA instalable (HTML, CSS, JS, manifest). |
| `GET` | `/api/v1/info` | Descubrimiento del servicio. |
| `GET` | `/health` | Estado, alertas, cámaras y clientes en línea. |
| `POST` | `/api/v1/alertas` | Recibe una alerta y calcula el despacho. |
| `GET` | `/api/v1/alertas` | Historial (más recientes primero). |
| `GET` | `/api/v1/alertas/{ticket}` | Detalle de una alerta. |
| `GET` | `/api/v1/alertas/{ticket}/json` | JSON persistido del incidente (descarga). |
| `GET` | `/api/v1/alertas/{ticket}/captura` | Captura JPEG de evidencia. |
| `PATCH` | `/api/v1/alertas/{ticket}` | Actualiza el estado. |
| `GET` | `/api/v1/camaras` | Cámaras con su estado en vivo. |
| `POST` | `/api/v1/camaras` | Alta de cámara + arranque del análisis. |
| `PATCH` | `/api/v1/camaras/{id}` | Cambios parciales (nombre, fuente, enabled...). |
| `DELETE` | `/api/v1/camaras/{id}` | Baja de la cámara (detiene el pipeline). |
| `POST` | `/api/v1/camaras/{id}/iniciar` | Arranca el análisis. |
| `POST` | `/api/v1/camaras/{id}/detener` | Detiene el análisis. |
| `GET` | `/api/v1/camaras/{id}/stream` | Vista previa MJPEG del frame anotado. |
| `GET` | `/api/v1/camaras/{id}/snapshot` | Último frame anotado en JPEG. |
| `WS` | `/ws/eventos` | Alertas, bitácora y estados en vivo. |
| `POST` | `/api/v1/clientes/registro` | Alta de un cliente externo. |
| `POST` | `/api/v1/clientes/heartbeat` | Latido de vida. |
| `GET` | `/api/v1/clientes` | Clientes conectados. |
| `DELETE` | `/api/v1/clientes/{client_id}` | Baja de un cliente. |

## Enviar una alerta

```bash
curl -X POST http://localhost:8000/api/v1/alertas \
  -H "Content-Type: application/json" \
  -d '{
    "client_id": "camara-01",
    "type": "PELEA",
    "confidence": 0.87,
    "label": "CONFIRMADO IA: PELEA",
    "frame": 320,
    "time_sec": 10.6,
    "video": "C:/videos/x.mp4",
    "verified_by": "ollama/qwen2.5vl:7b",
    "reason": "forcejeo entre dos personas",
    "timestamp": "2026-10-21T10:00:00+00:00",
    "location": {"lat": -12.05, "lon": -77.04, "address": "Piso 3 - pasillo"}
  }'
```

Respuesta `201 Created`:

```json
{
  "ticket": "ALT-20261021-0001",
  "status": "registrada",
  "priority": "ALTA",
  "eta_min": 5,
  "units": ["patrulla", "supervisor"],
  "message": "Ayuda en camino: patrulla, supervisor (ETA ~5 min). Ticket ALT-20261021-0001.",
  "received_at": "2026-10-21T10:00:01+00:00"
}
```

### Prioridad y unidades

| Tipo | Prioridad | ETA | Unidades |
|---|---|---|---|
| `ARMA` | `CRITICA` | 3 min | patrulla, unidad médica, supervisor |
| `PELEA`, `ROBO`, `VIOLENCIA` | `ALTA` | 5 min | patrulla, supervisor |
| `CAIDA` | `MEDIA` | 8 min | patrulla |
| Otro | `MEDIA` | 8 min | patrulla |

Si `DISPATCH_WEBHOOK_URL` está definida, el servidor hace `POST` con el ticket,
la prioridad, las unidades y la ubicación. Si el webhook responde 2xx, la
alerta pasa a estado `despachada`.

El campo opcional `snapshot_b64` lleva la captura JPEG en base64. El servidor la
decodifica, la guarda como BLOB y **no** la devuelve dentro del JSON del
incidente; en su lugar expone `snapshot_url`, `snapshot_sha256` y
`has_snapshot`.

## Consultar el historial

```bash
curl "http://localhost:8000/api/v1/alertas?limit=50"
curl http://localhost:8000/api/v1/alertas/ALT-20261021-0001
curl -X PATCH http://localhost:8000/api/v1/alertas/ALT-20261021-0001 \
  -H "Content-Type: application/json" \
  -d '{"status": "atendida"}'
```

## Evidencia por incidente

Cada alerta se persiste como una fila de SQLite con su JSON y, si el cliente la
adjuntó, la captura JPEG. El JSON es autocontenido y enlaza la imagen:

```bash
# JSON del incidente (se descarga como ALT-20261021-0001.json)
curl http://localhost:8000/api/v1/alertas/ALT-20261021-0001/json

# Captura de evidencia
curl http://localhost:8000/api/v1/alertas/ALT-20261021-0001/captura \
  -o ALT-20261021-0001.jpg
```

Ejemplo de JSON almacenado (resumido):

```json
{
  "ticket": "ALT-20261021-0001",
  "status": "atendida",
  "priority": "ALTA",
  "eta_min": 5,
  "units": ["patrulla", "supervisor"],
  "received_at": "2026-10-21T10:00:01+00:00",
  "alert": {
    "client_id": "camara-01",
    "type": "PELEA",
    "confidence": 0.87,
    "reason": "forcejeo entre dos personas",
    "timestamp": "2026-10-21T10:00:00+00:00",
    "location": {"lat": -12.05, "lon": -77.04, "address": "Piso 3 - pasillo"},
    "snapshot_b64": null
  },
  "has_snapshot": true,
  "snapshot_url": "/api/v1/alertas/ALT-20261021-0001/captura",
  "snapshot_sha256": "…"
}
```

El dashboard muestra los mismos datos con el botón **Ver evidencia**. La base
por defecto es `data/incidentes.db`; cámbiala con `CENTRAL_DB_PATH`. Para
inspeccionarla:

```bash
sqlite3 data/incidentes.db \
  "SELECT ticket, client_id, incident_type, status, has_snapshot FROM alertas;"
```

La retención aún es manual: la base crece con cada incidente y no se purga
automáticamente. Para producción conviene definir una política de retención y
respaldo del archivo `.db`.

## Clientes y heartbeat

Cada cliente se registra una vez y luego envía latidos. El latido incluye el
estado de la fuente de video para que la central distinga «cliente conectado» de
«cámara entregando frames»:

```bash
curl -X POST http://localhost:8000/api/v1/clientes/registro \
  -H "Content-Type: application/json" \
  -d '{"client_id":"camara-01","address":"Av. Abancay 1234","lat":-12.05,"lon":-77.04,"source_state":"CONNECTING"}'

curl -X POST http://localhost:8000/api/v1/clientes/heartbeat \
  -H "Content-Type: application/json" \
  -d '{"client_id":"camara-01","source_state":"LIVE"}'

curl http://localhost:8000/api/v1/clientes
```

`source_state` admite `UNKNOWN`, `CONNECTING`, `LIVE`, `NO_SIGNAL`,
`RECONNECTING` y `STOPPED`. Un latido sin `source_state` conserva el último
estado conocido. El dashboard resume cuántos clientes tienen la cámara en vivo
y pinta en rojo los que están conectados pero sin señal.

Si un cliente no envía latidos durante `CLIENT_ALIVE_SEC`, deja de aparecer en
`GET /api/v1/clientes` (que por defecto filtra solo los que están en línea).
Un heartbeat de un cliente desconocido devuelve `404`; el cliente se
re-registra automáticamente.

Cuando la central marca un ticket como `atendida` (con `PATCH` o con el botón
**Marcar atendida** del dashboard), el cliente que envió esa alerta lo detecta
en su siguiente sondeo y apaga la luz roja. El ticket permanece en la bitácora
del cliente.

## Cámaras y vista previa (modo PWA)

Dar de alta una cámara arranca su pipeline en la central:

```bash
curl -X POST http://localhost:8000/api/v1/camaras \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Pasillo principal",
    "source": "rtsp://usuario:clave@192.168.1.50:554/stream1",
    "address": "Piso 3",
    "lat": -12.05,
    "lon": -77.04,
    "enabled": true
  }'
```

Respuesta `201 Created`:

```json
{
  "id": "cam-3eac2237",
  "name": "Pasillo principal",
  "source": "rtsp://usuario:***@192.168.1.50:554/stream1",
  "address": "Piso 3",
  "lat": -12.05,
  "lon": -77.04,
  "enabled": true,
  "created_at": "2026-11-04T22:07:27+00:00",
  "state": "conectando",
  "running": true,
  "last_error": "",
  "last_event": "",
  "started_at": "2026-11-04T22:07:27+00:00"
}
```

La contraseña de la URL **nunca** se devuelve al navegador: se enmascara con
`***`. Para cambiarla hay que reenviar la `source` completa en el `PATCH`.

Estados posibles de `state`: `conectando`, `en_vivo`, `sin_senal`,
`reconectando`, `detenida` y `error`. `last_error` explica el motivo cuando el
pipeline no puede abrir la fuente.

La vista previa es un MJPEG que se puede incrustar directamente:

```html
<img src="http://localhost:8000/api/v1/camaras/cam-3eac2237/stream">
```

Se recodifica a ~6 fps y 720 px de ancho solo cuando hay algún espectador, para
no competir con YOLO. `GET .../snapshot` devuelve el último frame suelto.

> La central debe poder alcanzar la URL de la cámara. Si está en otra red,
> necesitas un túnel o un agente edge; un navegador no puede abrir RTSP.

## Eventos en vivo (WebSocket)

`ws://localhost:8000/ws/eventos` (o `wss://` tras HTTPS) empuja un sobre
`{"kind": ..., "data": ...}` por cada novedad:

| `kind` | `data` | Cuándo |
|---|---|---|
| `hola` | versión, cámaras y alertas | Al conectar. |
| `alerta` | `AlertRecord` completo | Alerta nueva (del supervisor o de un cliente REST). |
| `alerta_actualizada` | `AlertRecord` | Cambio de estado de un ticket. |
| `bitacora` | `camera_id`, `level`, `text`, `at` | Sospecha, descarte o confirmación. |
| `estado` | `CameraOut` | Cambia el estado de una cámara. |
| `ping` | `null` | Keep-alive cada 20 s sin tráfico. |

Ejemplo mínimo en JavaScript:

```js
const socket = new WebSocket("ws://localhost:8000/ws/eventos");
socket.addEventListener("message", (event) => {
  const { kind, data } = JSON.parse(event.data);
  if (kind === "alerta") console.log("Nueva alerta", data.ticket, data.alert.type);
});
```

El bus es de un solo proceso: si despliegas varios workers de uvicorn, cada uno
tendría su propio bus. Para producción, usa un único worker o un broker externo
(Redis pub/sub).

## Códigos de error

| Código | Situación |
|---|---|
| `401` | `CENTRAL_API_KEY` configurada y clave ausente o incorrecta. |
| `404` | Ticket, cliente o cámara inexistente; cámara sin imagen todavía. |
| `422` | Cuerpo inválido (tipos, rangos de confianza o coordenadas). |
| `1008` | WebSocket rechazado por API key inválida. |

## Persistencia

La central guarda las alertas en **SQLite** mediante `AlertStore`. Cada fila
contiene los metadatos consultables, el JSON completo del incidente, la captura
JPEG como BLOB y su hash SHA-256. El historial sobrevive a reinicios y el
dashboard lo lee de la misma base.

- Ruta por defecto: `data/incidentes.db` (`CENTRAL_DB_PATH` para cambiarla).
- Se activa WAL para permitir lecturas del dashboard mientras la API escribe.
- Una conexión compartida protegida por un lock; válida para varios hilos de
  uvicorn y para el hilo de Qt.
- El JSON no incluye la captura en base64: se sirve aparte en
  `/api/v1/alertas/{ticket}/captura`.
- La retención y la purga automática quedan pendientes; ver la nota en
  «Evidencia por incidente».
