"""
Créneaux choisis dans le calendrier du téléphone (Android, iPhone) ou de l'ordinateur.

- Un créneau (GET /api/selections/{id}.ics) : bouton « Ajouter à mon agenda » de chaque créneau.
- Abonnement (lien personnel et secret, GET /api/calendar/{jeton}.ics) : le calendrier le relit tout seul et
  suit les créneaux ajoutés, déplacés ou retirés. Géré dans « Mon agenda » (menu du compte), pour chaque
  structure du compte : un lien par compte et par structure ; le créer à nouveau remplace l'ancien (qui cesse de
  marcher), le désactiver le supprime. Seul le SHA-256 du jeton est stocké.
- Fichier .ics de toute une structure (GET /api/selections.ics), mêmes choix que l'abonnement : une photo.

Abonnement et fichier de structure, au choix du membre : tous les créneaux de la structure ou seulement ceux où
il est inscrit (confirmé ou en file d'attente), éventuellement d'un seul type ; les créneaux passés depuis
PAST_DAYS jours au plus, et tous ceux à venir.
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import date, timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict

from . import db, ical, mailer
from .accounts import iso, now, token_hash
from .auth import CurrentMember, CurrentUser, in_preview
from .selections import _registrations_by_selection, _selection_out, _today

router = APIRouter(prefix="/api")

PAST_DAYS = 60


def _base_url(request: Request) -> str:
    """URL publique de l'application (APP_BASE_URL), à défaut celle de la requête."""
    return mailer.BASE_URL or str(request.base_url).rstrip("/")


def _check_type(structure_id: int, type_id: int | None) -> sqlite3.Row | None:
    if type_id is None:
        return None
    t = db.get_slot_type(type_id)
    if t is None or t["structure_id"] != structure_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Type de créneau inconnu dans votre structure")
    return t


def _structures_of(user) -> list[sqlite3.Row | dict]:
    """Structures dont le compte peut suivre les créneaux : les siennes (en aperçu : celle de l'aperçu) ;
    un super administrateur, en plus, la structure active de sa session."""
    if in_preview(user):
        return [{"structure_id": user["structure_id"], "structure_name": user["structure_name"]}] \
            if user["structure_id"] is not None else []
    out = list(db.list_memberships(user["id"]))
    if user["is_admin"] and user["structure_id"] is not None and all(
            m["structure_id"] != user["structure_id"] for m in out):
        out.append({"structure_id": user["structure_id"], "structure_name": user["structure_name"]})
    return out


def _check_structure(user, structure_id: int) -> None:
    if all(s["structure_id"] != structure_id for s in _structures_of(user)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Vous n'êtes pas membre de cette structure")


def _out_selections(user_id: int, structure_id: int, rows: list[sqlite3.Row]) -> list[dict]:
    regs = _registrations_by_selection(structure_id)
    locks = db.get_lock_days(structure_id)
    return [_selection_out(r, regs.get(r["id"]), user_id, locks) for r in rows]


def _events(user_id: int, structure_id: int, mine: bool, type_id: int | None) -> list[dict]:
    since = (date.fromisoformat(_today()) - timedelta(days=PAST_DAYS)).isoformat()
    out = _out_selections(user_id, structure_id, db.list_selections(structure_id, since))
    return [s for s in out if (not mine or s["registered"]) and (type_id is None or s["type"]["id"] == type_id)]


def _ics(request: Request, events: list[dict], name: str, filename: str) -> Response:
    base = _base_url(request)
    body = ical.calendar(events, name, urlsplit(base).hostname or "calendive", link=f"{base}/mes-creneaux.html")
    return Response(body, media_type="text/calendar; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "private, no-cache",
    })


def _calendar(request: Request, user_id: int, structure_id: int, mine: bool, type_id: int | None) -> Response:
    structure = db.get_structure(structure_id)
    t = db.get_slot_type(type_id) if type_id is not None else None
    name = " — ".join(x for x in ("Calendive", structure["name"] if structure else None,
                                  "mes inscriptions" if mine else None, t["label"] if t else None) if x)
    return _ics(request, _events(user_id, structure_id, mine, type_id), name, "calendive.ics")


@router.get("/selections.ics")
def download_calendar(request: Request, user: CurrentUser, mine: bool = False, type_id: int | None = None,
                      structure_id: int | None = None):
    """Fichier .ics des créneaux d'une structure du compte (à défaut, la structure active)."""
    sid = structure_id if structure_id is not None else user["structure_id"]
    if sid is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Votre compte n'est rattaché à aucune structure")
    _check_structure(user, sid)
    _check_type(sid, type_id)
    return _calendar(request, user["id"], sid, mine, type_id)


@router.get("/selections/{selection_id}.ics")
def download_one(selection_id: int, request: Request, user: CurrentMember):
    """Un créneau de la structure active, à ajouter au calendrier (bouton « Ajouter à mon agenda »)."""
    row = db.get_selection(user["structure_id"], selection_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Créneau introuvable")
    events = _out_selections(user["id"], user["structure_id"], [row])
    return _ics(request, events, f"Calendive — {user['structure_name']}", f"calendive-{row['local_date']}.ics")


# ---- Abonnements (« Mon agenda ») ----

class FeedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mine: bool = False
    type_id: int | None = None


def _feed_out(row: sqlite3.Row | None) -> dict:
    if row is None:
        return {"active": False}
    return {"active": True, "mine": bool(row["mine"]), "type_id": row["type_id"],
            "created_at": row["created_at"], "last_used_at": row["last_used_at"]}


@router.get("/me/calendar-feeds")
def list_feeds(user: CurrentUser):
    """Pour chaque structure du compte : son abonnement (le lien lui-même n'est montré qu'à sa création) et ses
    types de créneaux (choix « un seul type »)."""
    return [{
        "structure": {"id": s["structure_id"], "name": s["structure_name"]},
        "feed": _feed_out(db.get_calendar_feed(user["id"], s["structure_id"])),
        "types": [{"id": t["id"], "label": t["label"]} for t in db.list_slot_types(s["structure_id"])],
    } for s in _structures_of(user)]


@router.post("/me/calendar-feeds/{structure_id}", status_code=201)
def create_feed(structure_id: int, body: FeedIn, request: Request, user: CurrentUser):
    """Crée le lien d'abonnement (ou le remplace : l'ancien cesse de marcher) et le renvoie, une seule fois."""
    _check_structure(user, structure_id)
    _check_type(structure_id, body.type_id)
    token = secrets.token_urlsafe(32)
    db.save_calendar_feed(user["id"], structure_id, token_hash(token), body.mine, body.type_id, iso(now()))
    url = f"{_base_url(request)}/api/calendar/{token}.ics"
    return {**_feed_out(db.get_calendar_feed(user["id"], structure_id)),
            "url": url, "webcal_url": "webcal://" + url.split("://", 1)[1]}


@router.delete("/me/calendar-feeds/{structure_id}", status_code=204)
def delete_feed(structure_id: int, user: CurrentUser):
    db.delete_calendar_feed(user["id"], structure_id)


@router.get("/calendar/{token}.ics", include_in_schema=False)
def subscribed_calendar(token: str, request: Request):
    """Lien d'abonnement : lu par le calendrier, sans session. Le compte doit toujours appartenir à la structure
    (sinon le lien ne marche plus) ; ses droits et son inscription sont ceux d'aujourd'hui."""
    feed = db.get_calendar_feed_by_token(token_hash(token))
    if feed is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lien d'abonnement inconnu ou désactivé")
    user = db.get_user(feed["user_id"])
    if user is None or not (user["is_admin"] or db.get_membership(feed["user_id"], feed["structure_id"])):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lien d'abonnement inconnu ou désactivé")
    db.touch_calendar_feed(feed["id"], iso(now()))
    return _calendar(request, feed["user_id"], feed["structure_id"], bool(feed["mine"]), feed["type_id"])
