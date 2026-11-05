# Monitoreo en tiempo real con cámara IP

> **Estado:** fases 1 a 5 implementadas (adaptador en vivo, caso de uso en
> tiempo real, interfaz de monitoreo, coordinación con la central y
> documentación). El cliente permite elegir cámara IP o archivo de demo; el
> modo archivo se conserva como demostración.

## Contexto

El cliente procesa hoy un archivo MP4 de principio a fin: lee frames, detecta
sospechas, las verifica con IA local, escribe un video anotado y genera un
reporte al terminar.

El caso de uso real es distinto: una cámara IP entrega video de forma continua,
no hay un final natural y el operador no elige cuándo empieza ni cuándo acaba.
Este documento describe cómo pasar a **monitoreo 24/7** conservando el modo
archivo como herramienta de demostración y diagnóstico.

## Decisiones

| Decisión | Motivo |
|---|---|
| La cámara IP (URL HTTP/RTSP) es la fuente principal. | Es el escenario real de videovigilancia y no requiere drivers. |
| El modo archivo se conserva como modo demo/CLI, no como test. | Los tests ya usan dobles; el archivo sirve para demos y diagnóstico sin cámara. |
| El cliente analiza; la central solo coordina y despacha. | La detección ocurre en el borde; solo viaja el snapshot de una alerta, no el video. |
| No se graba video. | El objetivo es monitoreo y alerta, no evidencia en video; se evita disco, tamaño y corrupción. |
| Los snapshots JPEG siguen siendo la evidencia. | Ya existen y viajan con la alerta. |
| El monitoreo no tiene botón "Detener". | Un monitoreo real dura hasta que el cliente se apaga; el cierre es del proceso, no del operador. |
| El heartbeat reporta el estado de la cámara. | "Cliente vivo" no significa "cámara viva"; sin esto la central muestra una cobertura falsa. |
| La alerta se identifica con `client_id`. | Basta para asociar la cámara; el campo `video` deja de tener sentido. |
| Las alertas son por incidente + cooldown, no por corrida. | `alert_once` enviaría una sola alerta en toda una sesión de días. |
| La incidencia se cierra desde la central. | El ciclo del ticket pertenece a la central; el cliente solo refleja el estado. |

## Ciclo de vida

Hay dos ciclos independientes. Mezclarlos es el error más fácil de cometer en
la UI y en el caso de uso.

```text
MONITOREO (cliente)                     INCIDENCIA (central)
CONECTANDO ──▶ EN_VIVO                  registrada
                 │                         │
                 ├──▶ SIN_SEÑAL            ▼
                 │        │             despachada
                 │        ▼                 │
                 │   RECONECTANDO           ▼
                 │        │              atendida
                 └────────┘
                 │
                 ▼
             DETENIDO (apagado del proceso)
```

- El monitoreo **no termina** cuando termina la incidencia. La luz vuelve a
  verde y el ticket queda en la bitácora.
- `SIN_SEÑAL` no es el fin de la sesión: es una caída que se reintenta con
  backoff indefinidamente.
- El único final del monitoreo es el apagado del cliente (ventana, proceso o
  equipo), que debe cerrar la captura, los hilos y la baja en la central.

### Estados de la interfaz

| Estado de conexión | Cuándo |
|---|---|
| `CONECTANDO` | Se abrió la URL y aún no llega el primer frame. |
| `EN_VIVO` | Llegan frames dentro del watchdog. |
| `SIN_SEÑAL` | Pasó el watchdog sin frames (o `read()` falló). |
| `RECONECTANDO` | Reintento en curso, con número de intento y tiempo sin señal. |
| `DETENIDO` | Cierre ordenado del cliente. |

| Estado de alerta | Cuándo | Luz |
|---|---|---|
| `SIN_ALERTA` | Sin sospechas activas. | Verde |
| `VERIFICANDO` | YOLO propuso una sospecha y la IA la está evaluando. | Ámbar |
| `ALERTA` | La IA confirmó y la alerta salió hacia la central. | Rojo |

El estado de alerta vuelve a `SIN_ALERTA` cuando la central cierra la
incidencia. Nunca detiene el monitoreo; el ticket permanece en la bitácora.

## Cambios por capa

| Capa | Cambio | Motivo |
|---|---|---|
| `domain/ports` | `VideoReader` gana señal de parada, timeout de lectura y estado de la fuente. | El caso de uso necesita distinguir "fin de archivo" de "caída de red" y poder cerrar. |
| `application` | Bucle sin fin por `ok=False`; reloj de pared; alertas por incidente; escritura opcional; notificación de sospechas y descartes. | El fin de archivo no existe en vivo y el tiempo no puede derivarse de frames. |
| `infrastructure/video` | Reconexión con backoff, watchdog y buffer acotado. | OpenCV puede quedar "abierto" sin entregar frames; el buffer acumula latencia. |
| `presentation` | Estados de conexión, luz de tres estados, bitácora acotada, badges, silenciar, cambiar cámara. | La UI actual asume un video finito con barra de progreso. |
| `server` | Estado de la cámara en el heartbeat; cierre de ticket ya soportado. | Evitar falsa cobertura en la central. |
| `config` / `cli` | URL de cámara, watchdog, backoff y modo archivo explícito. | Mantener el modo demo separado del modo real. |
| `tests` | Dobles de lector con caída/reconexión y reloj falso. | Probar el comportamiento en tiempo real sin cámara. |

## Plan por fases

### Fase 1 — Contrato de fuente y sesión

- `VideoReader` acepta URL, expone un estado de fuente y una forma de parar.
- Adaptador OpenCV con `CAP_PROP_BUFFERSIZE`, watchdog y reconexión.
- El modo archivo sigue funcionando sin cambios visibles.

**Estado:** implementada. `SourceState` en el dominio, `stop()` y `state` en el
puerto, `LiveVideoReader` con backoff y watchdog, y `create_reader` para elegir
adaptador según la URL. Probada con capturas falsas, sin cámara ni red.

**Criterio de aceptación:** una URL inalcanzable se reintenta sin tumbar el
cliente; el cierre del proceso libera la captura; los tests actuales siguen en
verde.

### Fase 2 — Caso de uso en tiempo real

- Tiempo de reloj de pared (fecha y hora, no segundos desde el inicio).
- `ok=False` ya no termina la sesión: dispara reconexión.
- Alertas por incidente + cooldown; `alert_once` se retira o queda como opción
  del modo archivo.
- Escritura de video opcional (desactivada en vivo).
- El listener recibe sospechas, descartes y confirmaciones.

**Criterio de aceptación:** con un lector falso que alterna caídas y frames, la
sesión continúa, no se duplican alertas fuera del cooldown y el tiempo de los
eventos coincide con el reloj.

**Estado:** implementada. `execute(..., live=True)` no exige metadatos ni
writer, usa reloj monotónico para `time_sec` y hora ISO para cada evento,
despacha una alerta por incidente (ignorando `alert_once`) y notifica
sospechas y descartes al listener. El reporte JSON solo se escribe en modo
archivo.

### Fase 3 — Interfaz de monitoreo

- Selector de fuente: cámara IP / archivo (demo).
- Estados de conexión visibles y luz de tres estados.
- Bitácora acotada con sospechas, descartes, confirmaciones y respuesta de la
  central.
- Badges de Cámara / IA / Central; silenciar alarma; cambiar cámara.
- Resumen de sesión al cerrar (sin rutas de video).

**Criterio de aceptación:** se puede monitorear, ver una sospecha convertirse
en confirmación o descarte, y cerrar el cliente sin dejar hilos ni capturas
colgadas.

**Estado:** implementada. La ventana arranca con selector cámara/archivo, badges
de Fuente/IA/Central, luz verde-ámbar-rojo y bitácora acotada a 200 entradas.
El controlador sondea `source_state` cada 500 ms para mostrar conexión, sin
señal y reconexión; el video se refresca como máximo a 20 fps para no acumular
eventos de Tk. La URL de la cámara se edita tras «Detener» (equivale a apagar la
cámara) y se vuelve a conectar con el mismo botón. «Silenciar» controla el audio
y «Reconocer» queda como respaldo local para apagar la luz; el cierre automático
al atender el ticket se implementa en la fase 4. Al cerrar la ventana se pide
`stop()` al caso de uso y se espera brevemente al worker antes de destruir la
interfaz.

### Fase 4 — Central

- El heartbeat incluye el estado de la fuente.
- El dashboard distingue cliente conectado de cámara en vivo.
- El cierre de ticket devuelve el cliente a `SIN_ALERTA`.

**Criterio de aceptación:** apagar la cámara (sin apagar el cliente) se refleja
en la central en menos de dos intervalos de heartbeat.

**Estado:** implementada. El cliente envía `source_state` en el registro y en
cada latido (`LIVE`, `RECONNECTING`, `NO_SIGNAL`, `STOPPED`, `UNKNOWN`); la
central lo guarda en el registro y lo expone en `GET /api/v1/clientes`. El
dashboard muestra «Clientes conectados · cámaras en vivo» y el estado de cada
cámara, de modo que un cliente en línea sin señal no parece cobertura activa.
La tabla de alertas tiene una columna `Estado` y un botón **Marcar atendida**.
Tras una alerta entregada, el cliente sondea `GET /api/v1/alertas/{ticket}`
cada 10 s; cuando la central lo marca `atendida` (o `cerrada`/`resuelta`), la
luz vuelve a verde y el ticket queda en la bitácora. «Reconocer» sigue
disponible como respaldo cuando no hay central.

La central persiste cada incidente en SQLite: el JSON del alerta y la captura
JPEG que envió el cliente. El dashboard incorpora **Ver evidencia**, que abre
la imagen y el JSON del ticket seleccionado. El JSON no duplica la imagen en
base64; guarda `snapshot_url` y su hash SHA-256.

### Fase 5 — Documentación y calidad

- Actualizar `README.md`, `docs/arquitectura.md` y `docs/api.md`.
- Añadir pruebas de reconexión, watchdog y reloj.
- Verificar lint y suite completa.

**Estado:** implementada. README y documentación describen el modo vivo, el
heartbeat con estado de cámara, el cierre de ticket y la evidencia persistente
(JSON + captura JPEG en SQLite). La suite cubre reconexión, backoff, watchdog,
reloj de pared/monótono, latido con estado, cierre de ticket y persistencia;
`ruff check .` y `pytest` pasan.

## No objetivos

- Grabar video continuo o clips. La evidencia es una captura JPEG por incidente,
  más su JSON; no hay video almacenado.
- Detección o verificación en el servidor.
- Purga o retención automática de la evidencia (queda como trabajo futuro).
- Varias cámaras por ventana (una ventana = una cámara).
- Autenticación de la cámara IP más allá de la URL.

## Riesgos

| Riesgo | Mitigación |
|---|---|
| Latencia creciente por el buffer de OpenCV | `CAP_PROP_BUFFERSIZE` bajo y descarte de frames si el análisis va atrasado. |
| Una URL inalcanzable bloquea el hilo al abrir | `CAP_PROP_OPEN_TIMEOUT_MSEC` pasado al constructor de OpenCV: medido ~1.5 s frente a ~15 s sin él. |
| RTSP "abierto" sin frames | Watchdog por tiempo desde el último frame, no por `isOpened()`. |
| La bitácora crece sin límite en sesiones largas | Límite de N entradas y log rotativo si se necesita historia. |
| Ollama saturado | La cola acotada ya descarta sospechas en vez de acumular. |
| Falsa cobertura en la central | Estado de la fuente en el heartbeat y en el dashboard. |
| Caída de la central | Fail-open: la alarma local y la detección continúan. |
| Crecimiento indefinido de la base SQLite en 24/7 | Una captura por incidente, no video; pendiente una política de retención. |

## Preguntas abiertas

- **Resuelto:** la evidencia en vivo es **un JSON por incidente** con su captura
  JPEG, persistidos en SQLite en la central. El cliente no escribe JSON en vivo;
  el resumen de sesión sigue sin escribirse.
- Retención: ¿cuántos días conservar capturas y JSON? ¿Purga automática o
  exportación a un almacenamiento externo? Hoy la base crece sin límite.
- MJPEG y RTSP funcionan a través de OpenCV. Para el celular se recomienda
  MJPEG por HTTP: más simple y con menor latencia; RTSP depende del backend.
