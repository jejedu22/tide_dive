// Service worker de Calendive (application installable) : l'interface reste disponible hors connexion.
//
// - Les données (/api/…) ne passent jamais par le cache : comptes, créneaux, inscriptions et marées viennent
//   toujours du serveur.
// - L'interface (pages, scripts, styles, images) est servie « réseau d'abord » : la version du serveur est
//   toujours préférée, le cache ne sert qu'en secours hors connexion. Une mise à jour est donc visible tout
//   de suite, sans changer VERSION.
// - Hors connexion, les pages de l'application sont remplacées par hors-ligne.html (« Pas de connexion ») ;
//   les pages d'aide déjà ouvertes restent lisibles.

const VERSION = "calendive-v1";
const OFFLINE = "hors-ligne.html";
const READABLE_OFFLINE = /\/(aide[\w-]*|mentions-legales)\.html$/;
const PRECACHE = [
  OFFLINE, "style.css", "session.js", "aide.js",
  "logo.svg", "logo-sombre.svg", "favicon.svg", "favicon-32.png", "icon-192.png",
  "fonts/sora-latin-600.woff2", "fonts/sora-latin-700.woff2",
];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(VERSION).then(c => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k))))
      .then(() => self.clients.claim()));
});

// Ce qui ne passe pas par le service worker : écritures, autres sites, API, sondes et documentation
function bypass(request) {
  if (request.method !== "GET") return true;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return true;
  const path = url.pathname.slice(new URL(self.registration.scope).pathname.length - 1);
  return /^\/(api\/|healthz|docs|redoc|openapi\.json)/.test(path);
}

// Clé de cache sans les paramètres : rien de ce qu'ils portent (jeton de désinscription, de mot de passe…)
// n'est conservé sur l'appareil
function cacheKey(request) {
  const url = new URL(request.url);
  return url.origin + url.pathname;
}

async function networkFirst(request) {
  const cache = await caches.open(VERSION);
  try {
    const response = await fetch(request);
    if (response.ok && response.type === "basic") cache.put(cacheKey(request), response.clone());
    return response;
  } catch (err) {
    // Une page de l'application sans le serveur n'afficherait que des erreurs : page « Pas de connexion ».
    // Les pages sans données (aide, mentions légales) restent lisibles si elles ont déjà été ouvertes.
    if (request.mode === "navigate" && !READABLE_OFFLINE.test(new URL(request.url).pathname)) {
      return cache.match(OFFLINE);
    }
    const cached = await cache.match(cacheKey(request));
    if (cached) return cached;
    if (request.mode === "navigate") return cache.match(OFFLINE);
    throw err;
  }
}

self.addEventListener("fetch", event => {
  if (bypass(event.request)) return;
  event.respondWith(networkFirst(event.request));
});
