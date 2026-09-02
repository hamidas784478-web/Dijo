const CACHE = "dijo-v1";
const STATIC = [
  "/static/css/style.css",
  "/static/js/app.js",
  "/static/images/logo.jpg",
  "/static/images/icon-192.png",
  "/static/images/icon-512.png",
  "/static/manifest.json"
];

// Install: cache static assets
self.addEventListener("install", e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(STATIC)).then(() => self.skipWaiting())
  );
});

// Activate: delete old caches
self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// Fetch: network-first for pages, cache-first for static files
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);

  // Static assets → cache first
  if (url.pathname.startsWith("/static/")) {
    e.respondWith(
      caches.match(e.request).then(cached => cached || fetch(e.request).then(res => {
        const clone = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
        return res;
      }))
    );
    return;
  }

  // HTML pages → network first, fallback to cache
  if (e.request.mode === "navigate") {
    e.respondWith(
      fetch(e.request).catch(() => caches.match("/"))
    );
    return;
  }

  e.respondWith(fetch(e.request));
});
