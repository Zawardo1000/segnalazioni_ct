"use strict";
// Service worker della pagina di prova: rende l'app installabile e la apre anche senza rete.
// Prima la rete, poi la copia salvata: durante le prove si vede sempre l'ultima versione. L'API non passa mai dalla
// cache (ogni abbinamento è nuovo), le tessere della mappa le gestisce il browser.

const CACHE = "abbina-v2";
const GUSCIO = [
  "/",
  "/static/comune.js",
  "/static/app.js",
  "/static/style.css",
  "/static/vendor/leaflet/leaflet.js",
  "/static/vendor/leaflet/leaflet.css",
  "/manifest.webmanifest",
  "/static/icon.svg",
  "/static/icon-192.png",
];

self.addEventListener("install", (ev) => {
  ev.waitUntil(caches.open(CACHE).then((c) => c.addAll(GUSCIO)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (ev) => {
  ev.waitUntil(
    caches.keys()
      .then((nomi) => Promise.all(nomi.filter((n) => n !== CACHE).map((n) => caches.delete(n))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (ev) => {
  const url = new URL(ev.request.url);
  if (ev.request.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;
  ev.respondWith(
    fetch(ev.request)
      .then((risposta) => {
        if (risposta.ok) {
          const copia = risposta.clone();
          caches.open(CACHE).then((c) => c.put(ev.request, copia));
        }
        return risposta;
      })
      .catch(() => caches.match(ev.request).then((r) => r || caches.match("/"))),
  );
});
