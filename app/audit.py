"""
Journal d'activité : chaque modification réussie faite par un compte connecté (POST, PUT, PATCH, DELETE sur /api)
est enregistrée avec son auteur, sa structure, un libellé lisible et l'objet concerné.

- Un middleware (install) observe les requêtes : rien à ajouter dans les routes. Les actions sans intérêt
  (préférences d'affichage, aperçus) ou sans compte (formulaires publics, webhooks) sont ignorées.
- L'objet concerné est nommé AVANT la requête (un compte supprimé n'a plus de nom après) à partir des
  identifiants du chemin, ou, pour une création, d'après la réponse.
- Consultation (GET /api/admin/audit) : super administrateurs (tout, filtre par structure) ; administrateurs
  de structure (leur structure).
- Conservation : RETENTION, purge au fil de l'eau.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, FastAPI, Request
from starlette.responses import Response

from . import accounts, db
from .auth import CurrentManager, scope_structure

RETENTION = timedelta(days=730)
_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}

# Libellés : (méthode, motif de route) → action
LABELS: dict[tuple[str, str], str] = {
    ("POST", "/api/auth/logout"): "Déconnexion",
    ("POST", "/api/me/password"): "Mot de passe changé (titulaire)",
    ("PATCH", "/api/me/profile"): "Profil modifié (titulaire)",
    ("PUT", "/api/me/preview"): "Aperçu d'un autre rôle",
    ("DELETE", "/api/me/preview"): "Fin de l'aperçu",
    ("POST", "/api/admin/users"): "Compte créé",
    ("PATCH", "/api/admin/users/{user_id}"): "Compte modifié",
    ("DELETE", "/api/admin/users/{user_id}"): "Compte supprimé ou retiré de la structure",
    ("POST", "/api/admin/users/{user_id}/send-link"): "Lien de connexion envoyé",
    ("POST", "/api/admin/users/import"): "Import CSV de comptes",
    ("PUT", "/api/admin/settings/tide-model"): "Modèle de marée changé",
    ("POST", "/api/admin/ports"): "Port créé",
    ("PATCH", "/api/admin/ports/{port_id}"): "Port modifié",
    ("DELETE", "/api/admin/ports/{port_id}"): "Port supprimé",
    ("DELETE", "/api/admin/ports/{port_id}/calibration"): "Recalage du port abandonné",
    ("POST", "/api/admin/jobs"): "Tâche mise en file",
    ("POST", "/api/admin/jobs/annual"): "Précalcul annuel mis en file",
    ("POST", "/api/admin/jobs/{job_id}/cancel"): "Tâche annulée",
    ("POST", "/api/admin/structures"): "Structure créée",
    ("PATCH", "/api/admin/structures/{structure_id}"): "Structure renommée",
    ("DELETE", "/api/admin/structures/{structure_id}"): "Structure supprimée",
    ("PATCH", "/api/admin/structures/{structure_id}/settings"): "Réglages de la structure modifiés",
    ("POST", "/api/selections"): "Créneau choisi",
    ("POST", "/api/selections/bulk"): "Créneaux choisis en groupe",
    ("POST", "/api/selections/custom"): "Créneau personnalisé ajouté",
    ("POST", "/api/selections/height"): "Créneau de hauteur d'eau choisi",
    ("PATCH", "/api/selections/{selection_id}"): "Créneau modifié",
    ("DELETE", "/api/selections/{selection_id}"): "Créneau retiré",
    ("POST", "/api/selections/{selection_id}/registration"): "Inscription",
    ("DELETE", "/api/selections/{selection_id}/registration"): "Désinscription",
    ("POST", "/api/selections/{selection_id}/registrations"): "Membres inscrits par un tiers",
    ("DELETE", "/api/selections/{selection_id}/registrations/{user_id}"): "Inscription d'un membre retirée",
    ("POST", "/api/admin/slot-types"): "Type de créneau créé",
    ("PUT", "/api/admin/slot-types/order"): "Types de créneaux réordonnés",
    ("PATCH", "/api/admin/slot-types/{type_id}"): "Type de créneau modifié",
    ("DELETE", "/api/admin/slot-types/{type_id}"): "Type de créneau supprimé",
    ("POST", "/api/admin/unavailabilities"): "Indisponibilité ajoutée",
    ("PUT", "/api/admin/unavailabilities/{unavailability_id}"): "Indisponibilité modifiée",
    ("DELETE", "/api/admin/unavailabilities/{unavailability_id}"): "Indisponibilité supprimée",
    ("POST", "/api/admin/ports/{port_id}/water-thresholds"): "Hauteur d'eau ajoutée",
    ("PUT", "/api/admin/water-thresholds/{threshold_id}"): "Hauteur d'eau modifiée",
    ("DELETE", "/api/admin/water-thresholds/{threshold_id}"): "Hauteur d'eau supprimée",
    ("POST", "/api/admin/dive-sites"): "Site de plongée ajouté",
    ("PUT", "/api/admin/dive-sites/{site_id}"): "Site de plongée modifié",
    ("DELETE", "/api/admin/dive-sites/{site_id}"): "Site de plongée supprimé",
    ("POST", "/api/admin/currents/sites"): "Courant des sites recalculé",
    ("POST", "/api/admin/structure-requests/{request_id}/create-structure"): "Structure créée depuis une demande",
    ("PATCH", "/api/admin/structure-requests/{request_id}"): "Demande de structure traitée",
    ("DELETE", "/api/admin/structure-requests/{request_id}"): "Demande de structure supprimée",
    ("PUT", "/api/admin/mailjet"): "Connexion Mailjet enregistrée",
    ("DELETE", "/api/admin/mailjet"): "Connexion Mailjet supprimée",
    ("POST", "/api/admin/structure-invitations"): "Invitation envoyée",
    ("DELETE", "/api/admin/structure-invitations/{invitation_id}"): "Invitation annulée",
    ("POST", "/api/me/invitations/{invitation_id}/accept"): "Invitation acceptée",
    ("POST", "/api/me/invitations/{invitation_id}/decline"): "Invitation refusée",
    ("POST", "/api/newsletters"): "Newsletter créée",
    ("PATCH", "/api/newsletters/{newsletter_id}"): "Newsletter modifiée",
    ("DELETE", "/api/newsletters/{newsletter_id}"): "Newsletter supprimée",
    ("POST", "/api/newsletters/{newsletter_id}/send"): "Newsletter envoyée",
    ("POST", "/api/newsletters/{newsletter_id}/schedule"): "Newsletter programmée",
    ("POST", "/api/newsletters/{newsletter_id}/unschedule"): "Programmation de newsletter annulée",
    ("POST", "/api/me/totp/setup"): "Double authentification en cours de mise en place",
    ("POST", "/api/me/totp/enable"): "Double authentification activée",
    ("POST", "/api/me/totp/disable"): "Double authentification désactivée",
    ("POST", "/api/me/totp/recovery-codes"): "Codes de secours renouvelés",
    ("POST", "/api/me/sessions/close-others"): "Autres sessions fermées (titulaire)",
    ("POST", "/api/admin/users/{user_id}/suspend"): "Compte suspendu",
    ("DELETE", "/api/admin/users/{user_id}/suspend"): "Compte réactivé",
    ("POST", "/api/admin/users/{user_id}/sessions/close"): "Sessions d'un compte fermées",
    ("DELETE", "/api/admin/users/{user_id}/totp"): "Double authentification d'un compte réinitialisée",
    ("POST", "/api/admin/accounts/merge"): "Comptes fusionnés",
    ("POST", "/api/admin/accounts/purge-inactive"): "Comptes inactifs supprimés",
    ("POST", "/api/admin/structures/{structure_id}/archive"): "Structure archivée",
    ("DELETE", "/api/admin/structures/{structure_id}/archive"): "Structure réactivée",
    ("POST", "/api/admin/structures/{structure_id}/transfer"): "Membres transférés",
    ("POST", "/api/admin/announcements"): "Bandeau d'annonce créé",
    ("PUT", "/api/admin/announcements/{announcement_id}"): "Bandeau d'annonce modifié",
    ("DELETE", "/api/admin/announcements/{announcement_id}"): "Bandeau d'annonce supprimé",
    ("POST", "/api/admin/broadcast"): "E-mail aux administrateurs de structure",
    ("PUT", "/api/divers/{user_id}"): "Fiche plongeur modifiée",
    ("POST", "/api/divers/{user_id}/caci/validate"): "Certificat médical (CACI) validé",
}

# Sans intérêt pour le journal, ou sans compte connecté
SKIPPED_PREFIXES = (
    "/api/auth/login", "/api/auth/forgot-password", "/api/auth/token-info", "/api/auth/reset-password",
    "/api/me/preferences", "/api/me/water-preferences", "/api/me/structure", "/api/me/calendar-feeds",
    "/api/me/newsletters", "/api/newsletters/preview", "/api/newsletters/unsubscribe", "/api/mailjet/events",
    "/api/structure-requests", "/api/admin/mailjet/test", "/api/admin/mailjet/events",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fr_date(iso: str | None) -> str:
    try:
        return date.fromisoformat(iso).strftime("%d/%m/%Y") if iso else ""
    except ValueError:
        return iso or ""


# ---- Objet concerné, nommé à partir des identifiants du chemin (avant la requête) ----

def _user_name(uid: int) -> str | None:
    row = db.get_user(uid)
    return accounts.display_name(row) if row else None


def _selection_name(sid: int) -> tuple[str | None, int | None]:
    with db.get_conn() as conn:
        row = conn.execute("SELECT s.structure_id, s.local_date, s.local_time, s.note, "
                           "COALESCE(p.name, s.location) AS place FROM slot_selections s "
                           "LEFT JOIN ports p ON p.id = s.port_id WHERE s.id = ?", (sid,)).fetchone()
    if row is None:
        return None, None
    label = f"créneau du {_fr_date(row['local_date'])}{' ' + row['local_time'] if row['local_time'] else ''}, {row['place']}"
    return label + (f" ({row['note']})" if row["note"] else ""), row["structure_id"]


def _simple(sql: str, key: int) -> sqlite3.Row | None:
    with db.get_conn() as conn:
        return conn.execute(sql, (key,)).fetchone()


_RESOLVERS = [
    # (motif dans le chemin, fonction id → (nom, structure_id ou None))
    (re.compile(r"/selections/(\d+)/registrations/(\d+)"),
     lambda m: (f"{_user_name(int(m[2])) or 'compte n° ' + m[2]} · {_selection_name(int(m[1]))[0] or ''}",
                _selection_name(int(m[1]))[1])),
    (re.compile(r"/selections/(\d+)"), lambda m: _selection_name(int(m[1]))),
    (re.compile(r"/(?:users|divers)/(\d+)"), lambda m: (_user_name(int(m[1])), None)),
    (re.compile(r"/structures/(\d+)"), lambda m: (
        (r := _simple("SELECT name FROM structures WHERE id = ?", int(m[1]))) and r["name"], int(m[1]))),
    (re.compile(r"/ports/(\d+)"), lambda m: (
        (r := _simple("SELECT name FROM ports WHERE id = ?", int(m[1]))) and r["name"], None)),
    (re.compile(r"/dive-sites/(\d+)"), lambda m: (
        (r := _simple("SELECT name, structure_id FROM dive_sites WHERE id = ?", int(m[1]))) and r["name"],
        r["structure_id"] if r else None)),
    (re.compile(r"/slot-types/(\d+)"), lambda m: (
        (r := _simple("SELECT label, structure_id FROM slot_types WHERE id = ?", int(m[1]))) and r["label"],
        r["structure_id"] if r else None)),
    (re.compile(r"/water-thresholds/(\d+)"), lambda m: (
        (r := _simple("SELECT label FROM water_thresholds WHERE id = ?", int(m[1]))) and r["label"], None)),
    (re.compile(r"/newsletters/(\d+)"), lambda m: (
        (r := _simple("SELECT subject, structure_id FROM newsletters WHERE id = ?", int(m[1]))) and r["subject"],
        r["structure_id"] if r else None)),
]


def resolve_target(path: str) -> tuple[str | None, int | None]:
    for pattern, fn in _RESOLVERS:
        m = pattern.search(path)
        if m:
            try:
                return fn(m)
            except (sqlite3.Error, ValueError, TypeError, KeyError):
                return None, None
    return None, None


def _from_response(body: bytes) -> str | None:
    """Nom de l'objet créé, lu dans la réponse JSON."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if isinstance(data, dict):
        for key in ("display_name", "name", "label", "subject", "username"):
            if isinstance(data.get(key), str):
                return data[key]
        if isinstance(data.get("user"), dict):
            return data["user"].get("display_name")
    return None


def _actor(request: Request) -> sqlite3.Row | None:
    from .auth import COOKIE_NAME, _token_hash   # import tardif : auth importe ce module indirectement
    token = request.cookies.get(COOKIE_NAME)
    return db.get_session_user(_token_hash(token), _now()) if token else None


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def _audit(request: Request, call_next):
        path = request.url.path
        if request.method not in _UNSAFE or not path.startswith("/api/") or path.startswith(SKIPPED_PREFIXES):
            return await call_next(request)
        actor = _actor(request)          # avant : la déconnexion ferme la session
        target, target_structure = resolve_target(path) if actor is not None else (None, None)
        response = await call_next(request)
        if actor is None or response.status_code >= 400:
            return response
        route = getattr(request.scope.get("route"), "path", path)
        if request.method == "POST" and target is None and response.headers.get("content-type", "").startswith("application/json"):
            body = b"".join([chunk async for chunk in response.body_iterator])
            target = _from_response(body)
            response = Response(body, status_code=response.status_code, headers=dict(response.headers),
                                media_type=response.media_type)
        if route == "/api/admin/users/{user_id}" and request.method == "DELETE" \
                and db.get_user(int(request.path_params["user_id"])) is None:
            target = "compte supprimé"   # effacement : son nom ne reste pas dans le journal
        query_sid = request.query_params.get("structure_id")
        structure_id = (int(query_sid) if query_sid and query_sid.isdigit() else None) or target_structure \
            or actor["structure_id"]
        action = LABELS.get((request.method, route), f"{request.method} {route}")
        try:
            entry = db.add_audit(_now(), actor["id"], accounts.display_name(actor), structure_id, request.method,
                                 route, action, target, response.status_code)
            if entry % 500 == 0:
                db.purge_audit((datetime.now(timezone.utc) - RETENTION).isoformat(timespec="seconds"))
        except sqlite3.Error:
            pass   # le journal ne doit jamais faire échouer l'action
        return response


# ---------------------------------------------------------------------------
# Consultation
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/admin")


@router.get("/audit")
def list_audit(actor: CurrentManager, structure_id: int | None = None, q: str | None = None,
               before_id: int | None = None, limit: int = 100):
    """Journal le plus récent d'abord. Administrateur de structure : sa structure seulement."""
    sid = scope_structure(actor, structure_id, required=False)
    rows = db.list_audit(sid, None, (q or "").strip() or None, before_id, min(max(limit, 1), 500))
    return [{"id": r["id"], "at": r["at"], "actor_id": r["actor_id"], "actor": r["actor_name"],
             "structure_id": r["structure_id"], "structure": r["structure_name"], "action": r["action"],
             "target": r["target"], "method": r["method"], "route": r["route"]} for r in rows]
