/* PWA de la central: registro de cámaras, alertas y bitácora en vivo. */
(() => {
  "use strict";

  // ---------------------------------------------------------------- estado
  const state = {
    cameras: [],
    alerts: [],
    activity: [],
    apiKey: localStorage.getItem("central.apiKey") || "",
    sound: localStorage.getItem("central.sound") !== "0",
    ws: null,
    wsTimer: null,
    wsRetry: 0,
    installPrompt: null,
    loadErrorShown: false,
  };

  const $ = (selector, root = document) => root.querySelector(selector);

  const STATE = {
    en_vivo: { label: "En vivo", cls: "state-live" },
    conectando: { label: "Conectando", cls: "state-connecting" },
    reconectando: { label: "Reconectando", cls: "state-connecting" },
    sin_senal: { label: "Sin señal", cls: "state-no_signal" },
    desconocido: { label: "Sin estado", cls: "state-stopped" },
    detenida: { label: "Detenida", cls: "state-stopped" },
    detenido: { label: "Detenida", cls: "state-stopped" },
    error: { label: "Error", cls: "state-no_signal" },
  };

  const CLOSED = new Set(["atendida", "cerrada", "resuelta"]);

  // --------------------------------------------------------------- helpers
  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "dataset") Object.assign(node.dataset, value);
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function withKey(path) {
    if (!state.apiKey) return path;
    return `${path}${path.includes("?") ? "&" : "?"}api_key=${encodeURIComponent(state.apiKey)}`;
  }

  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.body !== undefined && !(options.body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
    }
    if (state.apiKey) headers["X-API-Key"] = state.apiKey;
    const response = await fetch(path, { ...options, headers });
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        const body = await response.json();
        if (typeof body.detail === "string") detail = body.detail;
        else if (Array.isArray(body.detail)) {
          detail = body.detail.map((item) => item.msg || "dato inválido").join("; ");
        }
      } catch (_) { /* respuesta sin JSON */ }
      throw new Error(detail);
    }
    if (response.status === 204) return null;
    return response.json();
  }

  function fmtTime(iso) {
    if (!iso) return "";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return iso;
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function toast(title, text = "", kind = "") {
    const node = el("div", { class: `toast ${kind}`.trim() },
      el("strong", {}, title),
      text ? el("span", {}, text) : null,
    );
    $("#toasts").append(node);
    setTimeout(() => node.remove(), 6000);
  }

  let audioContext = null;
  function beep() {
    if (!state.sound) return;
    try {
      audioContext = audioContext || new (window.AudioContext || window.webkitAudioContext)();
      if (audioContext.state === "suspended") audioContext.resume();
      const now = audioContext.currentTime;
      [880, 1180].forEach((freq, index) => {
        const osc = audioContext.createOscillator();
        const gain = audioContext.createGain();
        osc.type = "sine";
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(0.0001, now + index * 0.16);
        gain.gain.exponentialRampToValueAtTime(0.18, now + index * 0.16 + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + index * 0.16 + 0.14);
        osc.connect(gain).connect(audioContext.destination);
        osc.start(now + index * 0.16);
        osc.stop(now + index * 0.16 + 0.15);
      });
    } catch (_) { /* el navegador puede bloquear el audio sin interacción */ }
  }

  // --------------------------------------------------------------- cámaras
  function stateBadge(node, value) {
    if (!node) return;
    const info = STATE[value] || STATE.desconocido;
    // Conserva las clases de posición (p. ej. preview-badge) y solo cambia
    // la clase de estado, para poder actualizar el badge varias veces.
    node.classList.remove("state-live", "state-connecting", "state-no_signal", "state-stopped");
    node.classList.add(info.cls);
    node.textContent = info.label;
  }

  function streamUrl(camera) {
    return withKey(`/api/v1/camaras/${encodeURIComponent(camera.id)}/stream`);
  }

  function buildCameraCard(camera) {
    const card = el("article", { class: "camera", dataset: { id: camera.id } });
    card.innerHTML = `
      <div class="camera-preview">
        <div class="placeholder"><strong>Conectando…</strong><span>esperando el primer frame</span></div>
        <span class="badge preview-badge">—</span>
        <span class="preview-live">EN VIVO</span>
      </div>
      <div class="camera-body">
        <div class="camera-title"><h3></h3><span class="badge state"></span></div>
        <p class="addr"></p>
        <code class="source"></code>
        <p class="last-event"></p>
        <div class="actions">
          <button class="btn ghost" data-action="toggle"></button>
          <button class="btn danger" data-action="delete">Eliminar</button>
        </div>
      </div>`;

    card.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-action]");
      if (!button) return;
      const current = state.cameras.find((item) => item.id === camera.id);
      if (current && button.dataset.action === "toggle") toggleCamera(current);
      if (current && button.dataset.action === "delete") deleteCamera(current);
    });
    return card;
  }

  function updateCameraCard(card, camera) {
    $("h3", card).textContent = camera.name;
    $(".addr", card).textContent = camera.address || "Sin ubicación";
    $(".source", card).textContent = camera.source;
    $(".last-event", card).textContent = camera.last_error || camera.last_event || "";

    stateBadge($(".state", card), camera.state);
    stateBadge($(".preview-badge", card), camera.state);
    card.classList.toggle("is-live", camera.state === "en_vivo");

    const toggle = $('[data-action="toggle"]', card);
    toggle.textContent = camera.running ? "Detener" : "Iniciar";

    const preview = $(".camera-preview", card);
    const placeholder = $(".placeholder", preview);
    $("strong", placeholder).textContent = camera.running ? "Conectando…" : "Vista detenida";
    $("span", placeholder).textContent = camera.running
      ? "esperando el primer frame"
      : "pulsa Iniciar para ver la cámara";

    let image = $("img", preview);

    if (camera.running) {
      if (!image) {
        image = el("img", { alt: `Vista de ${camera.name}` });
        image.addEventListener("error", () => {
          // El stream se cerró (cámara detenida o red): reintenta una vez.
          const stillRunning = state.cameras.find((item) => item.id === camera.id)?.running;
          if (!stillRunning) return;
          setTimeout(() => {
            if (image.isConnected && state.cameras.find((item) => item.id === camera.id)?.running) {
              const url = streamUrl(camera);
              image.src = `${url}${url.includes("?") ? "&" : "?"}t=${Date.now()}`;
            }
          }, 2500);
        });
        preview.prepend(image);
      }
      if (!image.getAttribute("src")) image.src = streamUrl(camera);
    } else if (image) {
      image.remove();
    }
  }

  function renderCameras() {
    const list = $("#cameraList");
    const ids = new Set(state.cameras.map((camera) => camera.id));
    for (const node of [...list.children]) {
      if (!ids.has(node.dataset.id)) node.remove();
    }
    for (const camera of state.cameras) {
      let card = list.querySelector(`[data-id="${camera.id}"]`);
      if (!card) {
        card = buildCameraCard(camera);
        list.append(card);
      }
      updateCameraCard(card, camera);
    }
    $("#camCount").textContent = String(state.cameras.length);
    $("#cameraEmpty").classList.toggle("is-visible", state.cameras.length === 0);
  }

  async function loadCameras() {
    try {
      state.cameras = await api("/api/v1/camaras");
      renderCameras();
      state.loadErrorShown = false;
    } catch (error) {
      if (!state.loadErrorShown) {
        state.loadErrorShown = true;
        toast("No se pudo leer las cámaras", error.message, "danger");
      }
    }
  }

  async function toggleCamera(camera) {
    const action = camera.running ? "detener" : "iniciar";
    try {
      const updated = await api(`/api/v1/camaras/${camera.id}/${action}`, { method: "POST" });
      Object.assign(camera, updated);
      renderCameras();
    } catch (error) {
      toast("No se pudo cambiar la cámara", error.message, "danger");
    }
  }

  async function deleteCamera(camera) {
    if (!window.confirm(`¿Eliminar la cámara "${camera.name}"? Se detendrá su análisis.`)) return;
    try {
      await api(`/api/v1/camaras/${camera.id}`, { method: "DELETE" });
      state.cameras = state.cameras.filter((item) => item.id !== camera.id);
      renderCameras();
      toast("Cámara eliminada", camera.name, "ok");
    } catch (error) {
      toast("No se pudo eliminar", error.message, "danger");
    }
  }

  async function submitCamera(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const errorBox = $("#formError");
    const number = (value) => (value === "" ? null : Number(value));
    const payload = {
      name: String(data.get("name") || "").trim(),
      source: String(data.get("source") || "").trim(),
      address: String(data.get("address") || "").trim(),
      lat: number(data.get("lat")),
      lon: number(data.get("lon")),
      enabled: data.get("enabled") === "on",
    };
    errorBox.hidden = true;
    try {
      const created = await api("/api/v1/camaras", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      const index = state.cameras.findIndex((item) => item.id === created.id);
      if (index >= 0) state.cameras[index] = created;
      else state.cameras.push(created);
      renderCameras();
      form.reset();
      form.hidden = true;
      toast("Cámara registrada", payload.enabled ? "El análisis arranca ahora." : "Guardada sin arrancar.", "ok");
      // El estado real (conectando/en vivo) llega por WebSocket y por el sondeo.
      setTimeout(loadCameras, 800);
    } catch (error) {
      errorBox.textContent = error.message;
      errorBox.hidden = false;
    }
  }

  // ---------------------------------------------------------------- alertas
  function buildAlertCard(record) {
    const alert = record.alert || {};
    const attended = CLOSED.has(String(record.status || "").toLowerCase());
    const card = el("article", {
      class: `alert prio-${record.priority}-card${attended ? " alert-attended" : ""}`,
    });

    const thumb = el("div", { class: "alert-thumb" });
    if (record.has_snapshot && record.snapshot_url) {
      const img = el("img", { alt: `Captura de ${record.ticket}`, loading: "lazy" });
      img.src = withKey(record.snapshot_url);
      img.addEventListener("click", () => window.open(img.src, "_blank", "noopener"));
      img.style.cursor = "zoom-in";
      thumb.append(img);
    } else {
      thumb.append(el("span", {}, "sin captura"));
    }

    const head = el("div", { class: "alert-head" },
      el("span", { class: `badge type-${alert.type || "NADA"}` }, alert.type || "INCIDENTE"),
      el("span", { class: "badge" }, record.priority || "MEDIA"),
      el("span", { class: "ticket" }, record.ticket || ""),
      el("time", {}, fmtTime(record.received_at)),
    );

    const meta = el("div", { class: "alert-meta" });
    const camera = state.cameras.find((item) => item.id === alert.client_id);
    meta.append(el("span", {}, camera ? camera.name : (alert.client_id || "cámara")));
    if (alert.location?.address) meta.append(el("span", {}, `· ${alert.location.address}`));
    if (record.eta_min !== null && record.eta_min !== undefined) {
      meta.append(el("span", {}, `· ETA ${record.eta_min} min`));
    }
    if (record.units?.length) meta.append(el("span", {}, `· ${record.units.join(", ")}`));

    const actions = el("div", { class: "alert-actions" });
    if (!attended) {
      const button = el("button", { class: "btn ghost" }, "Marcar atendida");
      button.addEventListener("click", () => markAttended(record.ticket));
      actions.append(button);
    } else {
      actions.append(el("span", { class: "badge state-live" }, record.status || "atendida"));
    }

    card.append(
      thumb,
      el("div", { class: "alert-body" },
        head,
        el("p", { class: "alert-label" }, alert.label || alert.reason || "Incidente confirmado"),
        meta,
        actions,
      ),
    );
    return card;
  }

  function renderAlerts() {
    const list = $("#alertList");
    list.replaceChildren(...state.alerts.map(buildAlertCard));
    $("#alertCount").textContent = String(state.alerts.length);
    $("#alertEmpty").classList.toggle("is-visible", state.alerts.length === 0);
  }

  async function loadAlerts() {
    try {
      state.alerts = await api("/api/v1/alertas?limit=100");
      renderAlerts();
    } catch (error) {
      toast("No se pudo leer las alertas", error.message, "danger");
    }
  }

  async function markAttended(ticket) {
    try {
      const updated = await api(`/api/v1/alertas/${encodeURIComponent(ticket)}`, {
        method: "PATCH",
        body: JSON.stringify({ status: "atendida" }),
      });
      upsertAlert(updated);
      renderAlerts();
      toast("Alerta atendida", ticket, "ok");
    } catch (error) {
      toast("No se pudo actualizar la alerta", error.message, "danger");
    }
  }

  function upsertAlert(record) {
    const index = state.alerts.findIndex((item) => item.ticket === record.ticket);
    if (index >= 0) state.alerts[index] = record;
    else state.alerts.unshift(record);
    // La lista se mantiene acotada como la de la central.
    if (state.alerts.length > 200) state.alerts.length = 200;
  }

  // --------------------------------------------------------------- actividad
  function pushActivity(entry) {
    state.activity.unshift(entry);
    if (state.activity.length > 300) state.activity.length = 300;
    renderActivity();
  }

  function renderActivity() {
    const list = $("#activityList");
    list.replaceChildren(...state.activity.map((entry) => el("li", { class: `level-${entry.level || "info"}` },
      el("time", {}, fmtTime(entry.at)),
      el("span", { class: "tag" }, entry.level || "info"),
      el("span", {},
        el("span", { class: "who" }, `${entry.camera_name || ""} · `),
        entry.text || "",
      ),
    )));
    $("#activityCount").textContent = String(state.activity.length);
    $("#activityEmpty").classList.toggle("is-visible", state.activity.length === 0);
  }

  // --------------------------------------------------------------- WebSocket
  function setConnection(online) {
    const pill = $("#conn");
    pill.classList.toggle("pill-on", online);
    pill.classList.toggle("pill-off", !online);
    $("#connText").textContent = online ? "en vivo" : "sin conexión";
  }

  function scheduleReconnect() {
    if (state.wsTimer) return;
    const delay = Math.min(15000, 1000 * 2 ** state.wsRetry);
    state.wsRetry = Math.min(state.wsRetry + 1, 5);
    state.wsTimer = setTimeout(() => {
      state.wsTimer = null;
      connectWS();
    }, delay);
  }

  function connectWS() {
    if (state.ws && state.ws.readyState <= WebSocket.OPEN) return;
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    const query = state.apiKey ? `?api_key=${encodeURIComponent(state.apiKey)}` : "";
    const socket = new WebSocket(`${protocol}://${location.host}/ws/eventos${query}`);
    state.ws = socket;

    socket.addEventListener("open", () => {
      state.wsRetry = 0;
      setConnection(true);
      // Al recuperar el canal, sincroniza lo que pudo perderse.
      loadAlerts();
      loadCameras();
    });
    socket.addEventListener("message", (event) => {
      try { handleMessage(JSON.parse(event.data)); } catch (_) { /* ignora basura */ }
    });
    socket.addEventListener("close", () => {
      if (state.ws === socket) {
        state.ws = null;
        setConnection(false);
        scheduleReconnect();
      }
    });
    socket.addEventListener("error", () => socket.close());
  }

  function handleMessage(message) {
    const { kind, data } = message || {};
    if (kind === "alerta" && data) {
      upsertAlert(data);
      renderAlerts();
      const alert = data.alert || {};
      toast(`⚠ ${alert.type || "Incidente"}`, `${data.ticket || ""} · ${alert.label || ""}`.trim(), "danger");
      beep();
      if (!state.cameras.some((item) => item.id === alert.client_id)) loadCameras();
      return;
    }
    if (kind === "alerta_actualizada" && data) {
      upsertAlert(data);
      renderAlerts();
      return;
    }
    if (kind === "bitacora" && data) {
      pushActivity(data);
      return;
    }
    if (kind === "estado" && data) {
      const index = state.cameras.findIndex((item) => item.id === data.id);
      if (index >= 0) state.cameras[index] = { ...state.cameras[index], ...data };
      else state.cameras.push(data);
      renderCameras();
      return;
    }
    if (kind === "hola" && data) {
      $("#subtitle").textContent =
        `v${data.version} · ${data.camaras} cámara(s) · ${data.alertas} alerta(s)`;
    }
  }

  // ------------------------------------------------------------------- ajustes
  function openSettings() {
    const dialog = $("#settingsDialog");
    $("#settingsForm").elements.apiKey.value = state.apiKey;
    $("#settingsForm").elements.sound.checked = state.sound;
    dialog.showModal();
  }

  function onSettingsClosed() {
    const dialog = $("#settingsDialog");
    if (dialog.returnValue !== "save") return;
    const form = $("#settingsForm").elements;
    state.apiKey = form.apiKey.value.trim();
    state.sound = form.sound.checked;
    localStorage.setItem("central.apiKey", state.apiKey);
    localStorage.setItem("central.sound", state.sound ? "1" : "0");
    const previous = state.ws;
    state.ws = null;
    if (previous) previous.close();
    // Las URLs de vista previa llevan la clave: se recrean con la nueva.
    document.querySelectorAll(".camera-preview img").forEach((img) => img.remove());
    connectWS();
    loadCameras();
    loadAlerts();
  }

  // ---------------------------------------------------------------- arranque
  function wireEvents() {
    $("#toggleForm").addEventListener("click", () => {
      const form = $("#cameraForm");
      form.hidden = !form.hidden;
      if (!form.hidden) form.elements.name.focus();
    });
    $("#cancelForm").addEventListener("click", () => {
      $("#cameraForm").reset();
      $("#cameraForm").hidden = true;
      $("#formError").hidden = true;
    });
    $("#cameraForm").addEventListener("submit", submitCamera);

    $("#refreshAlerts").addEventListener("click", loadAlerts);
    $("#clearActivity").addEventListener("click", () => {
      state.activity = [];
      renderActivity();
    });

    $("#settingsBtn").addEventListener("click", openSettings);
    $("#settingsDialog").addEventListener("close", onSettingsClosed);

    $("#installBtn").addEventListener("click", async () => {
      if (!state.installPrompt) return;
      state.installPrompt.prompt();
      await state.installPrompt.userChoice;
      state.installPrompt = null;
      $("#installBtn").hidden = true;
    });

    window.addEventListener("beforeinstallprompt", (event) => {
      event.preventDefault();
      state.installPrompt = event;
      $("#installBtn").hidden = false;
    });

    window.addEventListener("online", () => {
      setConnection(!!state.ws && state.ws.readyState === WebSocket.OPEN);
      connectWS();
    });
    window.addEventListener("offline", () => setConnection(false));

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") {
        connectWS();
        loadCameras();
      }
    });
  }

  async function loadInfo() {
    try {
      const info = await api("/api/v1/info");
      $("#subtitle").textContent = `v${info.version} · ${info.camaras} cámara(s)`;
    } catch (_) {
      $("#subtitle").textContent = "central de alertas";
    }
  }

  async function boot() {
    wireEvents();
    setConnection(false);
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/sw.js").catch(() => { /* sin offline */ });
    }
    await loadInfo();
    await Promise.all([loadCameras(), loadAlerts()]);
    renderActivity();
    connectWS();
    // Sondeo de respaldo: el WebSocket empuja estados, esto cubre reconexiones.
    setInterval(loadCameras, 5000);
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
