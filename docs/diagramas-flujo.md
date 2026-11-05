# Diagramas de flujo del proyecto

Diagramas del sistema completo y del pipeline de detección. Los diagramas
Mermaid se renderizan en GitHub, GitLab y en la vista previa de Markdown de
VS Code.

Índice:

1. [Vista de contexto](#1-vista-de-contexto)
2. [Flujo de extremo a extremo](#2-flujo-de-extremo-a-extremo)
3. [Etapa 1: de frame a sospecha](#3-etapa-1-de-frame-a-sospecha)
4. [Etapa 2: de sospecha a incidente](#4-etapa-2-de-sospecha-a-incidente)
5. [Secuencia completa](#5-secuencia-completa)
6. [Estados de la luz y del ticket](#6-estados-de-la-luz-y-del-ticket)
7. [Hilos y responsabilidades](#7-hilos-y-responsabilidades)
8. [Arranque y cierre](#8-arranque-y-cierre)
9. [Modo archivo vs modo vivo](#9-modo-archivo-vs-modo-vivo)
10. [Cadena de llamadas en el código](#10-cadena-de-llamadas-en-el-código)

---

## 1. Vista de contexto

Quién habla con quién. Cada cliente es un proceso independiente; la central y
Ollama son servicios aparte.

```mermaid
flowchart LR
    subgraph FUENTES["Fuentes de video"]
        MP4["Archivo MP4"]
        CAM["Cámara IP<br/>rtsp:// · http:// · udp://"]
    end

    subgraph CLIENTES["Clientes detectores (N procesos)"]
        C1["Cliente 1<br/>Tkinter"]
        C2["Cliente 2<br/>Tkinter"]
        CN["Cliente N<br/>Tkinter"]
    end

    subgraph LOCAL["Servicios locales"]
        OLL["Ollama<br/>localhost:11434<br/>qwen2.5vl:7b"]
    end

    subgraph CENTRAL["Central C5 (un proceso)"]
        API["FastAPI + uvicorn<br/>:8000"]
        DB[("SQLite<br/>data/incidentes.db")]
        DASH["Dashboard<br/>PyQt5 + matplotlib"]
    end

    subgraph EXT["Sistemas externos"]
        WH["Webhook de unidades"]
    end

    MP4 --> C1
    CAM --> C1
    MP4 --> C2
    CAM --> CN

    C1 <--> OLL
    C2 <--> OLL
    CN <--> OLL

    C1 -->|"REST · alertas, registro, heartbeat"| API
    C2 --> API
    CN --> API

    API --> DB
    API -->|"best-effort"| WH
    DASH -->|"sondeo cada 800 ms"| DB

    OPS["Operador del cliente"] --> C1
    OPSC["Operador de la central"] --> DASH
```

**Claves:**

- Ollama es local a cada cliente o compartido en la red; nunca se llama desde la
  central.
- La central no detecta nada: recibe, prioriza, persiste y muestra.
- El dashboard no habla con la API: lee la misma SQLite que escribe el servidor.
- Si la central cae, los clientes siguen detectando (**fail-open**).

---

## 2. Flujo de extremo a extremo

Resumen visual de las dos etapas y el despacho.

```mermaid
flowchart TD
    START(["Operador pulsa Iniciar"]) --> READ["Leer frame<br/>OpenCVVideoReader.read()"]
    READ --> E1["Etapa 1<br/>YOLO11n + heurísticas"]
    E1 --> Q1{"¿Hay sospecha?<br/>incidents no vacío"}
    Q1 -->|No| WRITE["Escribir frame anotado<br/>si es archivo"]
    WRITE --> READ

    Q1 -->|Sí| CD{"¿Pasaron 8 s<br/>desde el último envío?"}
    CD -->|No| WRITE

    CD -->|Sí| SNAP["Snapshot JPEG<br/>máx. 480 px, calidad 65"]
    SNAP --> QUEUE["Cola acotada<br/>maxsize = 2"]
    QUEUE --> E2["Etapa 2<br/>Ollama verifica la imagen"]
    E2 --> Q2{"¿violent = true?"}

    Q2 -->|No| DISC["on_suspicion_discarded<br/>bitacora, sin alarma"]
    DISC --> WRITE

    Q2 -->|Sí| EV["IncidentEvent<br/>cooldown 30 frames por tipo"]
    EV --> ALARM["Alarma local + luz roja<br/>on_incident_alert"]
    ALARM --> DISP["Hilo alert-dispatch<br/>POST /api/v1/alertas"]
    DISP --> PLAN["Central: plan_dispatch<br/>prioridad, ETA, unidades"]
    PLAN --> SAVE["Guardar alerta + JPEG<br/>en SQLite"]
    PLAN --> WH{"¿Webhook<br/>configurado?"}
    WH -->|Sí y responde 2xx| DESP["estado = despachada"]
    WH -->|No o falla| REG["estado = registrada"]
    SAVE --> ACK["AlertAck: ticket, ETA"]
    DESP --> ACK
    REG --> ACK
    ACK --> UI["on_alert_dispatched<br/>Ayuda en camino"]
    UI --> TICKET["Hilo ticket-watch<br/>GET cada 10 s"]
    TICKET --> CLOSED{"¿atendida,<br/>cerrada o resuelta?"}
    CLOSED -->|No| TICKET
    CLOSED -->|Sí| GREEN["on_incident_closed<br/>luz verde"]
    GREEN --> WRITE

    READ -->|"fin de video"| DRAIN["Drenar verificaciones<br/>hasta drain_timeout"]
    DRAIN --> SUMMARY["Reporte JSON + resumen"]
```

**Nota:** en modo vivo no hay `WRITE` ni `SUMMARY` con archivos; el bucle sigue
hasta `stop()` o hasta que la fuente deja de entregar frames.

---

## 3. Etapa 1: de frame a sospecha

Detalle de `YoloIncidentDetector.analyze()`.

```mermaid
flowchart TD
    F["Frame BGR<br/>ndarray (alto, ancho, 3) uint8"] --> CUSTOM{"¿Existe y carga<br/>violence.pt?"}

    CUSTOM -->|Sí| A1["YOLO modelo propio<br/>conf = 0.5"]
    A1 --> A2["Cada caja se etiqueta<br/>como VIOLENCIA"]
    A2 --> SMOOTH["_smooth<br/>min_persistence frames<br/>consecutivos por tipo"]

    CUSTOM -->|No| B1["YOLO11n COCO<br/>classes = [0,24,26,28,34,43,76]<br/>conf = 0.5, NMS interno"]
    B1 --> B2["Separar cajas<br/>personas · armas · bolsos"]
    B2 --> MOT["cv2.cvtColor BGR→GRAY<br/>motion_score = mean(absdiff)"]
    B2 --> CLU["find_clusters (Union-Find)<br/>factor = 0.7"]
    B2 --> FAL["find_fallen<br/>area ≥ 4000 y aspecto ≥ 1.15"]

    subgraph RULES["Reglas de sospecha"]
        R1["ARMA<br/>bate, cuchillo o tijeras"]
        R2["PELEA<br/>grupo + movimiento alto"]
        R3["PELEA<br/>grupo de 3 o más"]
        R4["CAIDA<br/>caída + grupo/movimiento/arma"]
        R5["ROBO<br/>arma + grupo"]
        R6["ROBO<br/>grupo + movimiento + bolso"]
    end

    B2 -->|"armas"| R1
    B2 -->|"armas + grupo"| R5
    B2 -->|"grupo + movimiento + bolso"| R6
    MOT -->|"movimiento"| R2
    CLU -->|"grupo cercano"| R2
    CLU -->|"3 o más"| R3
    FAL -->|"caída + contexto"| R4

    R1 --> SMOOTH
    R2 --> SMOOTH
    R3 --> SMOOTH
    R4 --> SMOOTH
    R5 --> SMOOTH
    R6 --> SMOOTH
    SMOOTH --> DEC{"¿Algún tipo<br/>confirmado?"}
    DEC -->|No| EMPTY["FrameAnalysis con<br/>incidents = ()"]
    DEC -->|Sí| MARK{"mark_confirmed"}

    MARK -->|False · cliente| SUSP["FrameAnalysis<br/>annotated + incidents<br/>alert = False"]
    MARK -->|True · diagnóstico| CONF["_overlay<br/>banda roja + etiquetas<br/>alert = True"]

    EMPTY --> RETURN(["Devolver a execute()"])
    SUSP --> RETURN
    CONF --> RETURN
```

### Valores por defecto relevantes

| Parámetro | Valor | Efecto |
|---|---|---|
| `confidence` | 0.5 | Descarta cajas con menos del 50 %. |
| `motion_threshold` | 12.0 | Media de `absdiff` en 0-255. |
| `proximity_factor` | 0.7 | Distancia entre centros / tamaño medio. |
| `min_persistence` | 6 (cliente: 2) | Frames consecutivos para confirmar. |
| `fall_aspect_ratio` | 1.15 | Caja más ancha que alta. |
| `min_person_area` | 4000 px² | Ignora detecciones diminutas. |

---

## 4. Etapa 2: de sospecha a incidente

Detalle de la cola y el worker de verificación.

```mermaid
flowchart TD
    LOOP["Bucle de frames<br/>hilo detector-worker"] --> HAS{"analysis.incidents<br/>no vacío y verificador<br/>disponible"}
    HAS -->|No| NEXT["Siguiente frame"]
    HAS -->|Sí| COUNT["suspicions += 1<br/>marcar frame: SOSPECHA N"]
    COUNT --> COOL{"now - last_sent<br/>≥ 8 s"}
    COOL -->|No| NEXT
    COOL -->|Sí| BEST["Elegir la sospecha<br/>de mayor confianza"]
    BEST --> JPEG["encode_snapshot<br/>480 px, quality 65"]
    JPEG --> PUT["pending += 1<br/>queue.put_nowait"]
    PUT --> FULL{"¿Cola llena?"}

    FULL -->|Sí · maxsize 2| DROP["pending -= 1<br/>descartar sospecha<br/>(backpressure)"]
    DROP --> NEXT

    FULL -->|No| NOTIFY["on_suspicion<br/>luz ámbar en la UI"]
    NOTIFY --> WORKER["Hilo ai-verifier<br/>jobs.get()"]

    WORKER --> VER["OllamaViolenceVerifier.verify<br/>/api/chat, temperature 0.1<br/>JPEG en base64"]
    VER --> PARSE["parse_verdict<br/>extrae JSON, valida tipo<br/>limita confianza a 0..1"]
    PARSE --> V{"¿violent?"}

    V -->|No o error| FAIL["Verdict(False)<br/>fail-closed"]
    FAIL --> DIS["discarded += 1<br/>on_suspicion_discarded"]
    DIS --> DONE["finally: pending -= 1<br/>jobs.task_done()"]

    V -->|Sí| COOLDOWN{"frame - último evento<br/>≥ 30 frames"}
    COOLDOWN -->|No| DONE
    COOLDOWN -->|Sí| EVENT["Crear IncidentEvent<br/>contar por tipo<br/>añadir a events"]
    EVENT --> CALLBACK["on_incident_alert<br/>alarma + luz roja"]
    CALLBACK --> ONCE{"alert_once<br/>y modo archivo"}
    ONCE -->|Ya se envió| DONE
    ONCE -->|Permitido| THREAD["Thread alert-dispatch<br/>_dispatch_alert"]
    THREAD --> DONE
```

**Invariante de concurrencia:** `pending` se incrementa **antes** de encolar y
se decrementa en `finally`. Así el drenaje nunca ve `pending = 0` mientras hay
un trabajo en vuelo.

---

## 5. Secuencia completa

Orden temporal de una detección confirmada.

```mermaid
sequenceDiagram
    autonumber
    participant OP as Operador
    participant UI as Tkinter
    participant CTRL as DetectorController
    participant UC as Caso de uso
    participant RD as VideoReader
    participant DET as YoloIncidentDetector
    participant Q as Cola max 2
    participant W as Hilo ai-verifier
    participant OLL as Ollama
    participant CENT as Central REST
    participant DB as SQLite

    OP->>UI: Iniciar
    UI->>CTRL: start_file() / connect_camera()
    CTRL->>UC: execute(...) en hilo detector-worker

    UC->>OLL: ping() /api/tags
    OLL-->>UC: disponible / no disponible

    loop Cada frame
        UC->>RD: read()
        RD-->>UC: (ok, frame BGR)
        UC->>DET: analyze(frame)
        DET-->>UC: FrameAnalysis(annotated, incidents)

        alt Hay sospecha y pasaron 8 s
            UC->>UC: encode_snapshot + Suspicion
            UC->>Q: put_nowait(job)
            UC-->>UI: on_suspicion (luz ámbar)
            W->>Q: get()
            W->>OLL: POST /api/chat
            OLL-->>W: JSON con violent/type/confidence/reason
            W->>W: parse_verdict()

            alt violent = false o error
                W-->>UI: on_suspicion_discarded
            else violent = true
                W->>W: comprobar cooldown 30 frames
                W-->>UI: on_incident_alert (alarma)
                W->>CENT: POST /api/v1/alertas (hilo aparte)
                CENT->>DB: guardar JSON + JPEG
                CENT-->>W: AlertAck(ticket, prioridad, ETA)
                W-->>UI: on_alert_dispatched
                loop Cada 10 s
                    W->>CENT: GET /api/v1/alertas/{ticket}
                    CENT-->>W: estado
                end
                CENT-->>W: atendida / cerrada / resuelta
                W-->>UI: on_incident_closed (luz verde)
            end
        end

        UC-->>UI: on_progress(frame anotado)
    end

    UC-->>UI: on_completed + on_summary
```

**Aclaración:** el `POST` a la central no lo hace el worker de IA, sino un hilo
`alert-dispatch` que se lanza después del evento. En el diagrama se muestra en
la misma línea temporal para no añadir un participante más, pero son hilos
distintos: el worker sigue verificando mientras la alerta viaja.

---

## 6. Estados de la luz y del ticket

### 6.1 Luz del cliente

```mermaid
stateDiagram-v2
    [*] --> idle
    idle --> verifying: on_suspicion
    verifying --> idle: on_suspicion_discarded
    verifying --> alert: on_incident_alert
    alert --> alert: nueva alerta confirmada
    alert --> idle: sin tickets ni alertas pendientes
    alert --> idle: acknowledge() en modo local
```

La luz solo vuelve a verde cuando se cumplen las tres condiciones:

- no quedan tickets abiertos (`_open_tickets`),
- no hay alertas esperando respuesta de la central (`_pending_alerts`),
- no hay alertas locales sin ticket (`_local_alerts`).

### 6.2 Ticket en la central

```mermaid
stateDiagram-v2
    [*] --> registrada: POST /api/v1/alertas
    registrada --> despachada: webhook responde 2xx
    registrada --> registrada: sin webhook o fallo
    registrada --> atendida: PATCH estado
    despachada --> atendida: PATCH estado
    atendida --> [*]
    registrada --> cerrada: PATCH estado
    despachada --> cerrada: PATCH estado
    registrada --> resuelta: PATCH estado
    despachada --> resuelta: PATCH estado
```

El cliente considera cerrado cualquiera de `atendida`, `cerrada` o `resuelta`
(comparación sin distinguir mayúsculas).

---

## 7. Hilos y responsabilidades

```mermaid
flowchart TB
    subgraph MAIN["Hilo principal Tkinter"]
        TK["Widgets y bucle de eventos"]
        AFTER["root.after(0, ...)<br/>todo cambio visual"]
    end

    subgraph CLIENT["Proceso cliente"]
        DW["detector-worker<br/>lee frames + etapa 1"]
        AV["ai-verifier<br/>consume cola + Ollama"]
        AD["alert-dispatch<br/>POST a la central"]
        TW["ticket-watch<br/>GET estado cada 10 s"]
        WD["video-watchdog<br/>solo cámara IP"]
        BC["client-beacon<br/>registro + heartbeat 10 s"]
    end

    subgraph SERVER["Proceso central"]
        REST["central-rest<br/>uvicorn"]
        QTM["Hilo principal PyQt5<br/>QTimer cada 800 ms"]
    end

    TK --> DW
    DW -->|"on_progress"| AFTER
    DW -->|"cola max 2"| AV
    AV -->|"on_incident_alert"| AFTER
    AV -->|"lanza"| AD
    AD -->|"on_alert_dispatched"| AFTER
    AV -->|"lanza"| TW
    TW -->|"on_incident_closed"| AFTER
    WD -.->|"estado de fuente"| DW
    BC -.->|"heartbeat"| REST
    AD -->|"HTTP"| REST
    TW -->|"HTTP"| REST
    REST -->|"escribe"| DB[("SQLite")]
    QTM -->|"lee"| DB
```

**Regla de oro:** ningún hilo toca widgets directamente. Todo pasa por
`root.after(0, callback)`. En la central, el dashboard nunca hace red desde Qt:
el servidor corre en otro hilo y el dashboard lee la base.

---

## 8. Arranque y cierre

### 8.1 Arranque del cliente

```mermaid
sequenceDiagram
    autonumber
    participant MAIN as main()
    participant BUILD as build()
    participant CFG as AppConfig
    participant BEACON as ClientBeacon
    participant UI as Tkinter

    MAIN->>CFG: from_env()
    MAIN->>BUILD: build(config)
    BUILD->>BUILD: DetectionConfig + YoloIncidentDetector
    BUILD->>BUILD: OllamaViolenceVerifier
    BUILD->>BUILD: HttpAlertDispatcher (si alertas activas)
    BUILD->>BUILD: DetectIncidentsUseCase
    BUILD->>UI: DetectorView + DetectorController
    BUILD->>BEACON: construir
    BUILD->>BEACON: start()
    Note over BEACON: se registra aunque<br/>la central esté caída
    BUILD-->>MAIN: root, controller
    MAIN->>UI: mainloop()
```

### 8.2 Cierre ordenado

```mermaid
flowchart TD
    S(["stop() o fin de video"]) --> E["stop_event.set()<br/>si es vivo: reader.stop()"]
    E --> LOOP["Sale el bucle de frames"]
    LOOP --> DRAIN{"pending = 0<br/>y cola vacía"}
    DRAIN -->|No| WAIT["esperar 0.2 s<br/>hasta drain_timeout"]
    WAIT --> DRAIN
    DRAIN -->|Sí| SENT["Enviar centinela None<br/>si la cola está llena, descartar un job"]
    SENT --> JOIN["worker.join(timeout=5)"]
    JOIN --> CLOSE["Cerrar reader y writer"]
    CLOSE --> SUMMARY["Construir resumen<br/>escribir JSON si hay salida"]
    SUMMARY --> EVENTS["on_completed + on_summary"]
    EVENTS --> STATE["Restaurar estado<br/>last_source_state"]
```

---

## 9. Modo archivo vs modo vivo

```mermaid
flowchart LR
    subgraph ARCH["Modo archivo (demo)"]
        A1["MP4 local"] --> A2["Bucle hasta agotar frames"]
        A2 --> A3["Video anotado MP4"]
        A2 --> A4["Reporte JSON"]
        A2 --> A5["alert_once puede limitar<br/>a una alerta por corrida"]
    end

    subgraph LIVE["Modo vivo (cámara IP)"]
        L1["rtsp / http / udp"] --> L2["Bucle hasta stop()"]
        L2 --> L3["Sin video de salida"]
        L2 --> L4["Sin reporte JSON"]
        L2 --> L5["Reconexión con backoff<br/>2 s → 30 s"]
        L5 --> L6["Watchdog: 8 s sin frames"]
        L2 --> L7["Una alerta por incidente<br/>limitada por cooldown"]
    end
```

| Aspecto | Archivo | Vivo |
|---|---|---|
| Fuente | MP4 | URL de red |
| Fin | Se agota el video | `stop()` o fuente detenida |
| Video anotado | Sí | No |
| Reporte JSON | Sí | No |
| Tiempo del evento | `frames_read / fps` | Reloj monótono |
| Fallo de fuente | Error y fin | `NO_SIGNAL` + reconexión |
| Alertas | `alert_once` configurable | Una por incidente con cooldown |

---

## 10. Cadena de llamadas en el código

Para seguir el hilo con el editor abierto.

```mermaid
flowchart TD
    M["cli/client.py :: main()"] --> B["build()"]
    B --> DET["YoloIncidentDetector.__init__<br/>YOLO(yolo11n.pt)"]
    B --> VER["OllamaViolenceVerifier.__init__"]
    B --> UC["DetectIncidentsUseCase.__init__"]
    B --> CTRL["DetectorController.__init__"]

    CTRL --> START["_start()"]
    START --> TH["Thread(name='detector-worker')"]
    TH --> EXEC["DetectIncidentsUseCase.execute()"]

    EXEC --> ROPEN["reader.open()"]
    EXEC --> WOPEN["writer.open() si archivo"]
    EXEC --> WORKER["Thread(name='ai-verifier')"]

    EXEC --> READ["reader.read()"]
    READ --> ANALYZE["detector.analyze()"]
    ANALYZE --> H1["_analyze_heuristic()"]
    H1 --> HB["heuristics.motion_score()"]
    H1 --> HC["heuristics.find_clusters()"]
    H1 --> HF["heuristics.find_fallen()"]
    H1 --> HS["_smooth()"]
    H1 --> HO["_overlay() si mark_confirmed"]

    ANALYZE --> ENC["encode_snapshot()"]
    ENC --> PUT["jobs.put_nowait()"]
    PUT --> VWORK["_verification_worker()"]
    VWORK --> VONE["_verify_one()"]
    VONE --> VV["verifier.verify()"]
    VV --> PV["parse_verdict()"]
    VONE --> EV["IncidentEvent"]
    EV --> LIST["listener.on_incident_alert()"]
    VONE --> DISP["Thread(name='alert-dispatch')"]
    DISP --> D1["_dispatch_alert()"]
    D1 --> HTTP["HttpAlertDispatcher.dispatch()"]
    HTTP --> POST["POST /api/v1/alertas"]

    POST --> APP["server/app.py :: receive_alert()"]
    APP --> PLAN["dispatch.plan_dispatch()"]
    APP --> STORE["store.add()"]
    APP --> WEB["dispatch.notify_units()"]

    D1 --> WATCH["_watch_ticket_async()"]
    WATCH --> TW["Thread(name='ticket-watch-...')"]
    TW --> GET["get_alert_status()<br/>GET /api/v1/alertas/{ticket}"]
```

---

## Leyenda rápida

| Símbolo / color | Significado |
|---|---|
| Rectángulo | Proceso, función o paso. |
| Rombo | Decisión. |
| Cilindro | Base de datos. |
| Flecha sólida | Flujo o llamada. |
| Flecha discontinua | Señal periódica o best-effort. |
| `fail-closed` | Ante duda, no alertar (Ollama). |
| `fail-open` | Ante fallo, seguir funcionando (central). |

## Los tres invariantes que resume el dibujo

1. **YOLO propone, la IA confirma, la central despacha.**
2. **La etapa 2 nunca bloquea a la etapa 1:** cola acotada + worker + descarte.
3. **La caída de la central no detiene la detección:** fail-open; la caída de
   Ollama no genera alarmas: fail-closed.
