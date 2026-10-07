"""
Créneaux choisis dans le calendrier du téléphone (Android, iPhone) ou de l'ordinateur.

- Fichier .ics à télécharger (GET /api/selections.ics) : une photo des créneaux, à importer.
- Abonnement (lien personnel et secret, GET /api/calendar/{jeton}.ics) : le calendrier le relit tout seul et
  suit les créneaux ajoutés, déplacés ou retirés. Un lien par compte et par structure ; le créer à nouveau
  remplace l'ancien (qui cesse de marcher), le désactiver le supprime. Seul le SHA-256 du jeton est stocké.

Dans les deux cas, au choix du membre : tous les créneaux de la structure ou seulement ceux où il est inscrit
(confirmé ou en file d'attente), éventuellement d'un seul type ; les créneaux passés depuis PAST_DAYS jours
au plus, et tous ceux à venir.
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
from .auth import CurrentMember
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


def _events(user_id: int, structure_id: int, mine: bool, type_id: int | None) -> list[dict]:
    since = (date.fromisoformat(_today()) - timedelta(days=PAST_DAYS)).isoformat()
    regs = _registrations_by_selection(structure_id)
    locks = db.get_lock_days(structure_id)
    out = [_selection_out(r, regs.get(r["id"]), user_id, locks) for r in db.list_selections(structure_id, since)]
    return [s for s in out if (not mine or s["registered"]) and (type_id is None or s["type"]["id"] == type_id)]


def _calendar(request: Request, user_id: int, structure_id: int, mine: bool, type_id: int | None) -> Response:
    structure = db.get_structure(structure_id)
    t = db.get_slot_type(type_id) if type_id is not None else None
    name = " — ".join(x for x in ("Calendive", structure["name"] if structure else None,
                                  "mes inscriptions" if mine else None, t["label"] if t else None) if x)
    base = _base_url(request)
    body = ical.calendar(_events(user_id, structure_id, mine, type_id), name,
                         urlsplit(base).hostname or "calendive", link=f"{base}/mes-creneaux.html")
    return Response(body, media_type="text/calendar; charset=utf-8", headers={
        "Content-Disposition": 'attachment; filename="calendive.ics"',
        "Cache-Control": "private, no-cache",
    })


@router.get("/selections.ics")
def download_calendar(request: Request, user: CurrentMember, mine: bool = False, type_id: int | None = None):
    """Fichier .ics des créneaux de la structure active (tous, ou ceux où le compte est inscrit)."""
    _check_type(user["structure_id"], type_id)
    return _calendar(request, user["id"], user["structure_id"], mine, type_id)


class FeedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mine: bool = False
    type_id: int | None = None


def _feed_out(row: sqlite3.Row | None) -> dict:
    if row is None:
        return {"active": False}
    return {"active": True, "mine": bool(row["mine"]), "type_id": row["type_id"],
            "created_at": row["created_at"], "last_used_at": row["last_used_at"]}


@router.get("/me/calendar-feed")
def get_feed(user: CurrentMember):
    """Abonnement du compte pour sa structure active (le lien lui-même n'est montré qu'à sa création)."""
    return _feed_out(db.get_calendar_feed(user["id"], user["structure_id"]))


@router.post("/me/calendar-feed", status_code=201)
def create_feed(body: FeedIn, request: Request, user: CurrentMember):
    """Crée le lien d'abonnement (ou le remplace : l'ancien cesse de marcher) et le renvoie, une seule fois."""
    _check_type(user["structure_id"], body.type_id)
    token = secrets.token_urlsafe(32)
    db.save_calendar_feed(user["id"], user["structure_id"], token_hash(token), body.mine, body.type_id, iso(now()))
    url = f"{_base_url(request)}/api/calendar/{token}.ics"
    return {**_feed_out(db.get_calendar_feed(user["id"], user["structure_id"])),
            "url": url, "webcal_url": "webcal://" + url.split("://", 1)[1]}


@router.delete("/me/calendar-feed", status_code=204)
def delete_feed(user: CurrentMember):
    db.delete_calendar_feed(user["id"], user["structure_id"])


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
