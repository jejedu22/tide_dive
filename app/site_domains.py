"""
Noms de domaine des structures pointés sur leur page vitrine (www.club-plongee.fr → /club/<adresse>).

- La structure déclare son domaine dans l'éditeur Vitrine, puis crée chez son registraire :
  * un enregistrement TXT `_calendive.<domaine>` = `calendive-verify=<jeton>` (preuve que le domaine est à elle) ;
  * un CNAME vers l'adresse de Calendive (ou, pour un domaine sans « www », des enregistrements A / AAAA vers les
    adresses IP du serveur).
  « Vérifier » contrôle les deux ; le domaine n'est actif qu'une fois vérifié (et la vitrine publiée).
- Traefik apprend les domaines actifs en interrogeant GET /api/internal/traefik (fournisseur HTTP, protégé par le
  jeton TRAEFIK_PROVIDER_TOKEN dans l'en-tête X-Calendive-Token) : un routeur par domaine, certificat Let's Encrypt
  obtenu tout seul. Sans jeton configuré, l'adresse n'existe pas.
- Sur un domaine de structure, l'application ne sert que la vitrine (« / ») et ce dont elle a besoin (feuille de
  style, photos, logo) ; toute autre adresse est renvoyée vers Calendive (connexion, inscriptions…) : aucune session
  ni cookie n'existe sur les domaines des structures.

Routes : GET / POST /api/site/domains, POST /api/site/domains/{id}/verify, DELETE /api/site/domains/{id} (éditeurs
de la vitrine) ; GET /api/internal/traefik (Traefik).
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, Field

from . import db, mailer
from .auth import CurrentUser

router = APIRouter()

MAX_DOMAINS = 3
TXT_PREFIX = "_calendive"
DNS_TIMEOUT = 5
LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
DOMAIN_RE = re.compile(rf"^{LABEL}(?:\.{LABEL})+$")
CACHE_SECONDS = 30
# sur un domaine de structure, seules ces adresses sont servies (le reste part vers Calendive)
PASSTHROUGH = re.compile(r"^/(?:vitrine-public\.(?:css|js)|favicon\.svg|api/sites/images/[\w-]+|api/structures/\d+/logo)$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main_host() -> str | None:
    """Nom d'hôte de Calendive (d'après APP_BASE_URL)."""
    return (urlsplit(mailer.BASE_URL).hostname or "").lower() or None if mailer.BASE_URL else None


def clean_domain(raw: str) -> str:
    """« https://www.Club.fr/ » → « www.club.fr » ; lève ValueError si ce n'est pas un nom de domaine utilisable."""
    v = (raw or "").strip().lower()
    v = re.sub(r"^[a-z]+://", "", v).split("/")[0].split(":")[0].rstrip(".")
    try:
        v = v.encode("idna").decode("ascii")          # domaines accentués (IDN)
    except UnicodeError:
        raise ValueError("nom de domaine invalide")
    try:
        ipaddress.ip_address(v)
        raise ValueError("indiquez un nom de domaine, pas une adresse IP")
    except ValueError as exc:
        if "adresse IP" in str(exc):
            raise
    if len(v) > 253 or not DOMAIN_RE.match(v):
        raise ValueError("nom de domaine invalide (ex. www.club-plongee.fr)")
    host = main_host()
    if host and (v == host or v.endswith("." + host)):
        raise ValueError("ce domaine est celui de Calendive")
    return v


# ---------------------------------------------------------------------------
# Vérification DNS
# ---------------------------------------------------------------------------

def _txt_records(name: str) -> list[str]:
    import dns.resolver
    try:
        answer = dns.resolver.resolve(name, "TXT", lifetime=DNS_TIMEOUT)
    except Exception:            # domaine inexistant, pas d'enregistrement, délai dépassé…
        return []
    return [b"".join(r.strings).decode("utf-8", "replace") for r in answer]


def _addresses(host: str) -> set[str]:
    try:
        return {info[4][0] for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)}
    except OSError:
        return set()


def check(row: sqlite3.Row) -> tuple[bool, str]:
    """(vérifié, explication) d'un domaine : enregistrement TXT et adresses du serveur."""
    host = main_host()
    if not host:
        return False, "APP_BASE_URL n'est pas configuré : impossible de comparer avec l'adresse de Calendive."
    domain = row["domain"]
    expected = f"calendive-verify={row['verify_token']}"
    txt_ok = expected in _txt_records(f"{TXT_PREFIX}.{domain}")
    server, target = _addresses(host), _addresses(domain)
    addr_ok = bool(server & target)
    if txt_ok and addr_ok:
        return True, "Domaine vérifié : il pointe vers Calendive. Le certificat HTTPS est obtenu dans les minutes qui suivent."
    problems = []
    if not txt_ok:
        problems.append(f"l'enregistrement TXT « {TXT_PREFIX}.{domain} » n'a pas (encore) la valeur attendue")
    if not addr_ok:
        problems.append("le domaine ne pointe pas (encore) vers Calendive" + (
            f" (il pointe vers {', '.join(sorted(target))})" if target else " (aucune adresse trouvée)"))
    return False, "Pas encore vérifié : " + " ; ".join(problems) + \
        ". Les modifications DNS peuvent mettre de quelques minutes à quelques heures à se propager."


# ---------------------------------------------------------------------------
# Routes de l'éditeur
# ---------------------------------------------------------------------------

def _domain_out(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "domain": row["domain"], "verified": row["verified_at"] is not None,
            "verified_at": row["verified_at"], "checked_at": row["checked_at"], "message": row["check_message"],
            "txt_name": f"{TXT_PREFIX}.{row['domain']}", "txt_value": f"calendive-verify={row['verify_token']}",
            "url": f"https://{row['domain']}/"}


def _list_out(sid: int) -> dict:
    host = main_host()
    return {"main_host": host, "server_addresses": sorted(_addresses(host)) if host else [],
            "max": MAX_DOMAINS, "domains": [_domain_out(r) for r in db.list_site_domains(sid)]}


def _scope(user, structure_id: int | None) -> int:
    from .site import _editor_scope
    return _editor_scope(user, structure_id)


@router.get("/api/site/domains")
def list_domains(user: CurrentUser, structure_id: int | None = None):
    """Noms de domaine de la vitrine, avec les enregistrements DNS à créer et l'état de leur vérification."""
    return _list_out(_scope(user, structure_id))


class DomainIn(BaseModel):
    domain: str = Field(max_length=300)


@router.post("/api/site/domains", status_code=201)
def add_domain(body: DomainIn, user: CurrentUser, structure_id: int | None = None):
    """Déclare un nom de domaine : il faudra ensuite créer les enregistrements DNS, puis le vérifier."""
    sid = _scope(user, structure_id)
    try:
        domain = clean_domain(body.domain)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc).capitalize())
    if len(db.list_site_domains(sid)) >= MAX_DOMAINS:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{MAX_DOMAINS} noms de domaine au plus")
    try:
        db.add_site_domain(sid, domain, secrets.token_urlsafe(18), _now())
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Le domaine {domain} est déjà déclaré")
    _forget()
    return _list_out(sid)


@router.post("/api/site/domains/{domain_id}/verify")
def verify_domain(domain_id: int, user: CurrentUser, structure_id: int | None = None):
    """Contrôle les enregistrements DNS ; le domaine devient actif s'ils sont en place (ou cesse de l'être sinon)."""
    sid = _scope(user, structure_id)
    row = db.get_site_domain(sid, domain_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Domaine inconnu")
    ok, message = check(row)
    now = _now()
    db.set_site_domain_check(domain_id, (row["verified_at"] or now) if ok else None, now, message)
    _forget()
    return _list_out(sid)


@router.delete("/api/site/domains/{domain_id}", status_code=204)
def delete_domain(domain_id: int, user: CurrentUser, structure_id: int | None = None):
    """Retire un nom de domaine (Traefik cesse de le servir au prochain passage)."""
    if not db.delete_site_domain(_scope(user, structure_id), domain_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Domaine inconnu")
    _forget()


# ---------------------------------------------------------------------------
# Fournisseur HTTP de Traefik
# ---------------------------------------------------------------------------

def traefik_config() -> dict:
    service = os.environ.get("TRAEFIK_SERVICE", "maree@docker")
    entrypoint = os.environ.get("TRAEFIK_ENTRYPOINT", "websecure")
    resolver = os.environ.get("TRAEFIK_CERTRESOLVER", "letsencrypt")
    middlewares = [m.strip() for m in os.environ.get("TRAEFIK_MIDDLEWARES", "default@file").split(",") if m.strip()]
    routers = {}
    for d in db.active_site_domains():
        router_conf = {"rule": f"Host(`{d['domain']}`)", "entryPoints": [entrypoint], "service": service,
                       "tls": {"certResolver": resolver}}
        if middlewares:
            router_conf["middlewares"] = middlewares
        routers[f"calendive-site-{d['id']}"] = router_conf
    return {"http": {"routers": routers}} if routers else {}


@router.get("/api/internal/traefik", include_in_schema=False)
def traefik_provider(x_calendive_token: str | None = Header(None)):
    token = os.environ.get("TRAEFIK_PROVIDER_TOKEN", "")
    if not token or not x_calendive_token or not secrets.compare_digest(token, x_calendive_token):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    return Response(json.dumps(traefik_config()), media_type="application/json", headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# Requêtes arrivant sur un domaine de structure
# ---------------------------------------------------------------------------

_cache: dict = {"at": 0.0, "domains": {}}


def _forget() -> None:
    _cache["at"] = 0.0


def domain_map() -> dict[str, str]:
    """{domaine actif : adresse de la vitrine}, relu toutes les 30 secondes (et après chaque changement)."""
    if time.monotonic() - _cache["at"] > CACHE_SECONDS:
        _cache["domains"] = {d["domain"]: d["slug"] for d in db.active_site_domains()}
        _cache["at"] = time.monotonic()
    return _cache["domains"]


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def _structure_domains(request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0].lower().rstrip(".")
        main = main_host()
        if not host or host == main or not DOMAIN_RE.match(host):
            return await call_next(request)
        slug = domain_map().get(host)
        if slug is None:
            return await call_next(request)
        path = request.url.path
        if path == "/" and request.method in ("GET", "HEAD"):
            from .site import public_page
            try:
                return public_page(slug, request, apercu=0, user=None)
            except HTTPException:
                return Response("Page introuvable", status_code=404, media_type="text/plain; charset=utf-8")
        if PASSTHROUGH.match(path):
            return await call_next(request)
        # tout le reste (connexion, inscriptions, pages de l'application) : sur Calendive
        target = (mailer.BASE_URL or f"https://{main}" if main else "") + path
        if request.url.query:
            target += "?" + request.url.query
        return RedirectResponse(target or "/", status_code=status.HTTP_302_FOUND)
