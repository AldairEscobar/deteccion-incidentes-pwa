/* Service worker de la PWA: carcasa offline y caché de estáticos.
 *
 * Nunca cachea la API ni el WebSocket: las alertas y el vídeo deben venir
 * siempre de la red. Solo guarda el "shell" para que la app abra sin conexión
 * y muestre el último estado conocido.
 */
const CACHE = "central-incidentes-v2";
const SHELL = [
  "/",
  "/styles.css",
  "/app.js",
  "/manifest.webmanifest",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

function isPrivate(url) {
  return url.pathname.startsWith("/api/")
    || url.pathname.startsWith("/ws/")
    || url.pathname.startsWith("/docs")
    || url.pathname === "/openapi.json"
    || url.pathname === "/health";
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin || isPrivate(url)) return;

  // Navegación: red primero, y si falla se sirve la carcasa cacheada.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put("/", copy)).catch(() => {});
          return response;
        })
        .catch(() => caches.match("/").then((cached) => cached || Response.error()))
    );
    return;
  }

  // Estáticos: sirve de caché al instante y actualiza en segundo plano.
  event.respondWith(
    caches.match(request).then((cached) => {
      const network = fetch(request)
        .then((response) => {
          if (response && response.status === 200) {
            const copy = response.clone();
            caches.open(CACHE).then((cache) => cache.put(request, copy)).catch(() => {});
          }
          return response;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
});
