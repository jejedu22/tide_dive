"""
Protections transverses de l'API : limitation des tentatives, en-têtes de
sécurité, contrôle de l'origine des requêtes qui modifient des données.

Limitation des tentatives
-------------------------
Compteurs en mémoire (fenêtre glissante) : l'API tourne dans un seul processus
uvicorn, et un redémarrage remet les compteurs à zéro, ce qui est acceptable
pour freiner le brute-force et le spam. RATE_LIMIT=0 les désactive.

Adresse IP du client
--------------------
Derrière Traefik, l'adresse réelle est la dernière de X-Forwarded-For (celle
ajoutée par le proxy ; les précédentes sont fournies par le client et
falsifiables). TRUSTED_PROXY_HOPS = nombre de proxys de confiance (1 par défaut ;
0 sans proxy : l'en-tête est alors ignoré).
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

ENABLED = os.environ.get("RATE_LIMIT", "1").lower() not in ("0", "false", "no")
TRUSTED_PROXY_HOPS = int(os.environ.get("TRUSTED_PROXY_HOPS", "1"))
# Origines autorisées en plus de l'origine de l'application (séparées par des virgules) ; vide : aucune
ALLOWED_ORIGINS = [o.strip().rstrip("/") for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    hops = [h.strip() for h in forwarded.split(",") if h.strip()]
    if TRUSTED_PROXY_HOPS > 0 and len(hops) >= TRUSTED_PROXY_HOPS:
        return hops[-TRUSTED_PROXY_HOPS]
    return request.client.host if request.client else "inconnue"


class RateLimiter:
    """Fenêtre glissante par clé : au plus `limit` événements pendant `window` secondes."""

    _PURGE_EVERY = 5000   # nombre de clés au-delà duquel on nettoie les clés expirées

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, window: float, now: float) -> deque[float]:
        q = self._events.setdefault(key, deque())
        while q and q[0] <= now - window:
            q.popleft()
        return q

    def retry_after(self, key: str, limit: int, window: float) -> int:
        """Secondes à attendre avant qu'un nouvel événement soit permis (0 : permis). N'enregistre rien."""
        if not ENABLED:
            return 0
        now = time.monotonic()
        with self._lock:
            q = self._recent(key, window, now)
            if len(q) < limit:
                return 0
            return max(1, int(q[0] + window - now) + 1)

    def add(self, key: str, window: float) -> None:
        if not ENABLED:
            return
        now = time.monotonic()
        with self._lock:
            self._recent(key, window, now).append(now)
            if len(self._events) > self._PURGE_EVERY:
                for k in [k for k, q in self._events.items() if not q or q[-1] <= now - 86400]:
                    del self._events[k]

    def clear(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()

    def hit(self, key: str, limit: int, window: float, message: str = "Trop de requêtes : réessayez plus tard.") -> None:
        """Enregistre un événement ; lève 429 (avec Retry-After) si la limite est déjà atteinte."""
        wait = self.retry_after(key, limit, window)
        if wait:
            raise too_many(wait, message)
        self.add(key, window)


def too_many(wait: int, message: str) -> HTTPException:
    return HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, message, headers={"Retry-After": str(wait)})


limiter = RateLimiter()


# ---------------------------------------------------------------------------
# En-têtes de sécurité et contrôle d'origine
# ---------------------------------------------------------------------------

# Aucun script en ligne dans le frontend : script-src 'self' suffit. Les styles en ligne
# (attributs style, <style> de demande-structure.html) imposent 'unsafe-inline' pour les styles.
# Les images de newsletters peuvent venir de n'importe quel site https (aperçu en iframe srcdoc).
# Application installable : manifeste et service worker (sw.js) servis par le site lui-même.
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: https:",
    "font-src 'self'",
    "connect-src 'self'",
    "manifest-src 'self'",
    "worker-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Appelés par des serveurs (Mailjet, messageries) : pas d'en-tête Origin de navigateur à contrôler
_SERVER_TO_SERVER = ("/api/mailjet/events/", "/api/newsletters/unsubscribe/")


def _origin_allowed(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:              # pas un navigateur (curl, serveur) ou même origine sans en-tête
        return True
    host = request.headers.get("host", "")
    origin = origin.rstrip("/")
    return origin in ALLOWED_ORIGINS or origin.split("://", 1)[-1] == host


def install(app: FastAPI) -> None:
    """Branche les protections sur l'application (à appeler après la création de `app`)."""

    @app.middleware("http")
    async def _security(request: Request, call_next):
        path = request.url.path
        if (request.method in _UNSAFE_METHODS and path.startswith("/api/")
                and not path.startswith(_SERVER_TO_SERVER) and not _origin_allowed(request)):
            return JSONResponse({"detail": "Origine de la requête non autorisée"}, status_code=403)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("Content-Security-Policy", CSP)
        return response
