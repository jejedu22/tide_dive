"""
Newsletters d'une structure : rédaction, envoi par Mailjet et suivi.

Accès : comptes de la structure ayant le profil « gestionnaire »
(accounts.PROFILES), y compris les administrateurs, qui se l'attribuent.

Cycle de vie d'une newsletter :

    draft ──envoyer──► queued ──worker──► sending ──► sent  (ou failed)
      │  ▲
      │  └─annuler la programmation
      └──programmer──► scheduled ──(planificateur, heure venue)──► queued

- Modifiable seulement en brouillon. Les destinataires sont figés au moment
  de l'envoi : membres de la structure visés par l'audience, ayant une adresse,
  moins les désinscrits.
- L'envoi est fait par le worker (newsletter_send.py), jamais par l'API : il
  peut durer, et une erreur de Mailjet n'interrompt pas l'interface.
- Chaque e-mail porte un lien de désinscription personnel (et l'en-tête
  List-Unsubscribe, désinscription en un clic des messageries) ; la
  désinscription vaut pour toutes les newsletters de la structure.
- Suivi : Mailjet envoie ses événements à /api/mailjet/events/<jeton de la
  structure> (activé dans l'onglet Mailjet de l'administration).
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator, model_validator

from . import accounts, db, jobs, mailer, mailjet, mailjet_admin, newsletter_render, security
from .auth import CurrentMember, CurrentUser

router = APIRouter(prefix="/api")

PARIS = ZoneInfo("Europe/Paris")
SUBJECT_MAX = 150
BODY_MAX = 50_000
MIN_SCHEDULE_DELAY = timedelta(minutes=5)
MAX_SCHEDULE_DELAY = timedelta(days=366)
AUDIENCE_LABELS = {
    "all": "Tous les membres",
    "managers": "Administrateurs de la structure",
    "viewers": "Membres en visualisation",
    "selection": "Inscrits à un créneau",
    "group": "Groupe d'envoi",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def current_newsletter_user(user: CurrentMember) -> sqlite3.Row:
    if not accounts.permissions(user)["newsletters"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Réservé au profil Gestionnaire de la structure")
    return user


NewsletterUser = Annotated[sqlite3.Row, Depends(current_newsletter_user)]


# ---------------------------------------------------------------------------
# Modèles
# ---------------------------------------------------------------------------

class Audience(BaseModel):
    kind: Literal["all", "managers", "viewers", "selection", "group"] = "all"
    selection_id: int | None = None
    group_id: int | None = None

    @model_validator(mode="after")
    def _target(self):
        if self.kind == "selection" and self.selection_id is None:
            raise ValueError("Choisissez le créneau dont les inscrits recevront la newsletter")
        if self.kind == "group" and self.group_id is None:
            raise ValueError("Choisissez le groupe d'envoi")
        if self.kind != "selection":
            self.selection_id = None
        if self.kind != "group":
            self.group_id = None
        return self


def _one_line(v: str | None) -> str | None:
    v = " ".join((v or "").split())
    return v or None


class NewsletterIn(BaseModel):
    subject: str = Field(max_length=SUBJECT_MAX)
    preheader: str | None = Field(None, max_length=200)
    body: str = Field("", max_length=BODY_MAX)
    audience: Audience = Audience()

    @field_validator("subject")
    @classmethod
    def _subject(cls, v: str) -> str:
        v = _one_line(v)
        if not v:
            raise ValueError("Objet obligatoire")
        return v

    @field_validator("preheader")
    @classmethod
    def _preheader(cls, v: str | None) -> str | None:
        return _one_line(v)


class NewsletterPatch(BaseModel):
    subject: str | None = Field(None, max_length=SUBJECT_MAX)
    preheader: str | None = Field(None, max_length=200)
    body: str | None = Field(None, max_length=BODY_MAX)
    audience: Audience | None = None

    @field_validator("subject")
    @classmethod
    def _subject(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = _one_line(v)
        if not v:
            raise ValueError("Objet obligatoire")
        return v

    @field_validator("preheader")
    @classmethod
    def _preheader(cls, v: str | None) -> str | None:
        return _one_line(v)


class PreviewIn(BaseModel):
    subject: str = Field("", max_length=SUBJECT_MAX)
    preheader: str | None = Field(None, max_length=200)
    body: str = Field("", max_length=BODY_MAX)


class ScheduleIn(BaseModel):
    at: datetime   # heure de Paris si sans fuseau (champ datetime-local du navigateur)


# ---------------------------------------------------------------------------
# Sorties
# ---------------------------------------------------------------------------

_STATS = ("total", "sent", "failed", "queued", "delivered", "opened", "clicked",
          "bounced", "blocked", "spam", "unsubscribed")


def _audience_label(structure_id: int, audience: dict) -> str:
    if audience.get("kind") == "group":
        group = db.get_mailing_group(structure_id, audience.get("group_id") or 0)
        return f"Groupe « {group['name']} »" if group else "Groupe supprimé"
    if audience.get("kind") != "selection":
        return AUDIENCE_LABELS.get(audience.get("kind", "all"), "Tous les membres")
    sel = db.get_selection(structure_id, audience.get("selection_id") or 0)
    if sel is None:
        return "Inscrits à un créneau supprimé"
    day = datetime.fromisoformat(sel["local_date"]).strftime("%d/%m/%Y")
    what = sel["note"] or sel["port_name"]
    return f"Inscrits au créneau du {day}{' (' + what + ')' if what else ''}"


def _out(row: sqlite3.Row, with_body: bool = False) -> dict:
    audience = json.loads(row["audience_json"] or "{}")
    out = {
        "id": row["id"],
        "subject": row["subject"],
        "preheader": row["preheader"],
        "audience": audience,
        "audience_label": _audience_label(row["structure_id"], audience),
        "status": row["status"],
        "scheduled_at": row["scheduled_at"],
        "created_at": row["created_at"],
        "created_by": row["created_by_name"],
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by_name"],
        "sent_by": row["sent_by_name"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "error": row["error"],
        "stats": {k: row[k] or 0 for k in _STATS},
    }
    if with_body:
        out["body"] = row["body"]
    return out


def _get_or_404(user: sqlite3.Row, newsletter_id: int) -> sqlite3.Row:
    row = db.get_newsletter(newsletter_id, user["structure_id"])
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Newsletter introuvable")
    return row


def _check_audience(structure_id: int, audience: Audience) -> None:
    if audience.kind == "selection" and db.get_selection(structure_id, audience.selection_id) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Créneau inconnu dans votre structure")
    if audience.kind == "group" and db.get_mailing_group(structure_id, audience.group_id) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Groupe d'envoi inconnu dans votre structure")


def slot_lister(structure_id: int) -> newsletter_render.SlotLister:
    """Créneaux choisis par la structure sur une période, mis en cache le temps d'un envoi."""
    cache: dict = {}

    def list_slots(start, end) -> list[dict]:
        if (start, end) not in cache:
            cache[(start, end)] = [{
                "date": s["local_date"], "end_date": s["end_date"], "rdv_date": s["rdv_date"],
                "rdv_time": s["rdv_time"], "place": s["port_name"], "note": s["note"],
                "type": s["type_label"], "type_color": s["type_color"], "kind": s["kind"],
                "time": s["local_time"], "coefficient": s["coefficient"],
            } for s in db.list_selections(structure_id, start.isoformat()) if s["local_date"] <= end.isoformat()]
        return cache[(start, end)]
    return list_slots


def render_for(structure_id: int, subject: str, preheader: str | None, body: str, *,
               first_name: str | None, last_name: str | None, unsubscribe_url: str | None = None,
               slots: newsletter_render.SlotLister | None = None) -> tuple[str, str, str]:
    """Rendu d'une newsletter de la structure : nom, créneaux choisis (calculés maintenant), lien d'inscription."""
    return newsletter_render.render(
        subject, preheader, body, structure=db.get_structure(structure_id)["name"],
        first_name=first_name, last_name=last_name, unsubscribe_url=unsubscribe_url,
        slots=slots or slot_lister(structure_id), today=datetime.now(PARIS).date(),
        slots_url=mailer.link("mes-creneaux.html") if mailer.BASE_URL else None,
    )


def recipients_for(structure_id: int, audience: dict) -> tuple[list[sqlite3.Row], int]:
    """(comptes qui recevront la newsletter, nombre de désinscrits écartés)."""
    unsub = db.unsubscribed_emails(structure_id)
    members, seen, skipped = [], set(), 0
    for m in db.audience_members(structure_id, audience):
        email = m["email"].strip().lower()
        if email in seen:
            continue
        seen.add(email)
        if email in unsub:
            skipped += 1
        else:
            members.append(m)
    return members, skipped


# ---------------------------------------------------------------------------
# Newsletters (profil gestionnaire)
# ---------------------------------------------------------------------------

@router.get("/newsletters/settings")
def newsletter_settings(user: NewsletterUser):
    """État de l'envoi pour le gestionnaire (sans rien de secret) : Mailjet prêt ? suivi activé ?"""
    sid = user["structure_id"]
    row = db.get_mailjet(sid)
    try:
        cred = mailjet_admin.credentials(sid)
        ready, reason = True, None
    except mailjet_admin.MailjetNotReady as exc:
        cred, ready, reason = None, False, str(exc)
    if ready and row["check_ok"] is False:
        reason = f"Le dernier test de connexion Mailjet a échoué : {row['check_message']}"
    return {
        "structure": db.get_structure(sid)["name"],
        "mailjet_ready": ready,
        "mailjet_warning": reason,
        "sender": f"{cred.sender_name} <{cred.sender_email}>" if cred else None,
        "tracking": bool(row and row["events_registered_at"]),
        "base_url_ok": bool(mailer.BASE_URL),
        "can_configure": accounts.permissions(user)["manage_mailjet"],
    }


@router.get("/newsletters")
def list_newsletters(user: NewsletterUser):
    return [_out(r) for r in db.list_newsletters(user["structure_id"])]


@router.post("/newsletters", status_code=201)
def create_newsletter(body: NewsletterIn, user: NewsletterUser):
    _check_audience(user["structure_id"], body.audience)
    nid = db.create_newsletter(
        user["structure_id"], body.subject, body.preheader, body.body.strip(),
        body.audience.model_dump_json(), user["id"], _iso(_now()),
    )
    return _out(db.get_newsletter(nid), with_body=True)


@router.get("/newsletters/audiences")
def audiences(user: NewsletterUser):
    """Audiences proposées, avec le nombre de destinataires de chacune."""
    sid = user["structure_id"]
    out = []
    for kind in ("all", "managers", "viewers"):
        members, skipped = recipients_for(sid, {"kind": kind})
        out.append({"kind": kind, "label": AUDIENCE_LABELS[kind], "recipients": len(members), "unsubscribed": skipped})
    for group in db.list_mailing_groups(sid):
        audience = {"kind": "group", "group_id": group["id"]}
        members, skipped = recipients_for(sid, audience)
        out.append({**audience, "label": f"Groupe « {group['name']} »", "recipients": len(members), "unsubscribed": skipped})
    today = datetime.now(PARIS).date().isoformat()
    for sel in db.list_selections(sid, today)[:60]:
        audience = {"kind": "selection", "selection_id": sel["id"]}
        members, skipped = recipients_for(sid, audience)
        out.append({**audience, "label": _audience_label(sid, audience), "recipients": len(members), "unsubscribed": skipped})
    return out


@router.post("/newsletters/preview")
def preview(body: PreviewIn, user: NewsletterUser):
    """Rendu de l'e-mail, personnalisé avec le nom du compte connecté."""
    subject, page, text = render_for(
        user["structure_id"], body.subject or "(sans objet)", body.preheader, body.body,
        first_name=user["first_name"], last_name=user["last_name"],
    )
    return {"subject": subject, "html": page, "text": text}


@router.get("/newsletters/unsubscribes")
def unsubscribes(user: NewsletterUser):
    return [dict(r) for r in db.list_unsubscribes(user["structure_id"])]


# ---------------------------------------------------------------------------
# Groupes d'envoi
# ---------------------------------------------------------------------------

class GroupIn(BaseModel):
    name: str = Field(max_length=60)
    description: str | None = Field(None, max_length=200)
    member_ids: list[int] | None = Field(None, max_length=5000)   # None : membres inchangés (modification)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = _one_line(v)
        if not v:
            raise ValueError("Nom du groupe obligatoire")
        return v

    @field_validator("description")
    @classmethod
    def _description(cls, v: str | None) -> str | None:
        return _one_line(v)


def _group_out(row: sqlite3.Row, with_members: bool = True) -> dict:
    out = {"id": row["id"], "name": row["name"], "description": row["description"],
           "created_at": row["created_at"], "updated_at": row["updated_at"]}
    if with_members:
        out["member_ids"] = db.mailing_group_member_ids(row["id"])
        out["members"] = len(out["member_ids"])
    return out


def _group_or_404(user: sqlite3.Row, group_id: int) -> sqlite3.Row:
    row = db.get_mailing_group(user["structure_id"], group_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Groupe introuvable")
    return row


def _save_group(user: sqlite3.Row, group_id: int | None, body: GroupIn) -> dict:
    try:
        gid = db.save_mailing_group(user["structure_id"], group_id, body.name, body.description,
                                    body.member_ids, _iso(_now()))
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Un groupe « {body.name} » existe déjà")
    return _group_out(db.get_mailing_group(user["structure_id"], gid))


@router.get("/newsletters/groups")
def list_groups(user: NewsletterUser):
    return [_group_out(g) for g in db.list_mailing_groups(user["structure_id"])]


@router.post("/newsletters/groups", status_code=201)
def create_group(body: GroupIn, user: NewsletterUser):
    return _save_group(user, None, body)


@router.put("/newsletters/groups/{group_id}")
def update_group(group_id: int, body: GroupIn, user: NewsletterUser):
    _group_or_404(user, group_id)
    return _save_group(user, group_id, body)


@router.delete("/newsletters/groups/{group_id}", status_code=204)
def delete_group(group_id: int, user: NewsletterUser):
    """Les newsletters déjà envoyées gardent leurs destinataires ; un brouillon visant ce groupe n'en aura plus."""
    _group_or_404(user, group_id)
    db.delete_mailing_group(user["structure_id"], group_id)


@router.get("/newsletters/members")
def members(user: NewsletterUser):
    """Comptes de la structure, pour composer les groupes."""
    unsub = db.unsubscribed_emails(user["structure_id"])
    return [{
        "id": m["id"],
        "name": " ".join(p for p in (m["first_name"], m["last_name"]) if p) or m["username"],
        "email": m["email"],
        "role": m["structure_role"],
        "unsubscribed": bool(m["email"]) and m["email"].lower() in unsub,
    } for m in db.structure_members(user["structure_id"])]


@router.get("/newsletters/{newsletter_id}")
def get_newsletter(newsletter_id: int, user: NewsletterUser):
    return _out(_get_or_404(user, newsletter_id), with_body=True)


@router.patch("/newsletters/{newsletter_id}")
def update_newsletter(newsletter_id: int, body: NewsletterPatch, user: NewsletterUser):
    row = _get_or_404(user, newsletter_id)
    if row["status"] != "draft":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Seul un brouillon se modifie : annulez d'abord la programmation, ou dupliquez la newsletter.",
        )
    fields: dict = {}
    sent = body.model_fields_set
    if body.subject is not None:
        fields["subject"] = body.subject
    if "preheader" in sent:
        fields["preheader"] = body.preheader
    if body.body is not None:
        fields["body"] = body.body.strip()
    if body.audience is not None:
        _check_audience(user["structure_id"], body.audience)
        fields["audience_json"] = body.audience.model_dump_json()
    if fields:
        db.update_newsletter(newsletter_id, only_status=("draft",), updated_by=user["id"],
                             updated_at=_iso(_now()), **fields)
    return _out(db.get_newsletter(newsletter_id), with_body=True)


@router.delete("/newsletters/{newsletter_id}", status_code=204)
def delete_newsletter(newsletter_id: int, user: NewsletterUser):
    _get_or_404(user, newsletter_id)
    if not db.delete_newsletter(newsletter_id, user["structure_id"]):
        raise HTTPException(status.HTTP_409_CONFLICT, "Envoi en cours : la newsletter ne peut pas être supprimée")


@router.post("/newsletters/{newsletter_id}/duplicate", status_code=201)
def duplicate_newsletter(newsletter_id: int, user: NewsletterUser):
    row = _get_or_404(user, newsletter_id)
    subject = row["subject"] if row["subject"].startswith("Copie de ") else f"Copie de {row['subject']}"
    nid = db.create_newsletter(user["structure_id"], subject[:SUBJECT_MAX], row["preheader"], row["body"],
                               row["audience_json"], user["id"], _iso(_now()))
    return _out(db.get_newsletter(nid), with_body=True)


def _require_ready(user: sqlite3.Row) -> mailjet_admin.Credentials:
    try:
        return mailjet_admin.credentials(user["structure_id"])
    except mailjet_admin.MailjetNotReady as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))


@router.post("/newsletters/{newsletter_id}/test")
def send_test(newsletter_id: int, user: NewsletterUser):
    """Envoie la newsletter, marquée [TEST], à l'adresse du compte connecté."""
    row = _get_or_404(user, newsletter_id)
    if not user["email"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Votre compte n'a pas d'adresse e-mail (Mon compte).")
    cred = _require_ready(user)
    subject, page, text = render_for(
        user["structure_id"], row["subject"], row["preheader"], row["body"],
        first_name=user["first_name"], last_name=user["last_name"],
        unsubscribe_url=mailer.link("desinscription.html") if mailer.BASE_URL else None,
    )
    message = {
        "From": {"Email": cred.sender_email, "Name": cred.sender_name},
        "To": [{"Email": user["email"], "Name": accounts.display_name(user)}],
        "Subject": f"[TEST] {subject}",
        "TextPart": text,
        "HTMLPart": page,
        "CustomID": f"test-{newsletter_id}",
    }
    try:
        result = mailjet.send(cred.api_key, cred.api_secret, [message])[0]
    except mailjet.MailjetError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    error = mailjet.message_error(result)
    if error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Envoi refusé par Mailjet : {error}")
    return {"sent": True, "to": user["email"]}


def _check_sendable(user: sqlite3.Row, row: sqlite3.Row) -> int:
    """Vérifications avant envoi ou programmation ; renvoie le nombre de destinataires."""
    if not row["body"].strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "La newsletter est vide.")
    if not mailer.BASE_URL:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "APP_BASE_URL n'est pas renseignée : impossible de construire les liens de désinscription.",
        )
    _require_ready(user)
    members, _ = recipients_for(user["structure_id"], json.loads(row["audience_json"]))
    if not members:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Aucun destinataire pour cette audience.")
    return len(members)


@router.post("/newsletters/{newsletter_id}/send")
def send_now(newsletter_id: int, user: NewsletterUser):
    row = _get_or_404(user, newsletter_id)
    if row["status"] != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, "Seul un brouillon s'envoie.")
    _check_sendable(user, row)
    if not db.update_newsletter(newsletter_id, only_status=("draft",), status="queued", sent_by=user["id"],
                                scheduled_at=None, error=None):
        raise HTTPException(status.HTTP_409_CONFLICT, "La newsletter a changé entre-temps : rechargez la page.")
    jobs.enqueue("newsletter_send", {"newsletter_id": newsletter_id}, accounts.display_name(user))
    return _out(db.get_newsletter(newsletter_id), with_body=True)


@router.post("/newsletters/{newsletter_id}/schedule")
def schedule(newsletter_id: int, body: ScheduleIn, user: NewsletterUser):
    row = _get_or_404(user, newsletter_id)
    if row["status"] != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, "Seul un brouillon se programme.")
    at = body.at if body.at.tzinfo else body.at.replace(tzinfo=PARIS)
    now = _now()
    if at < now + MIN_SCHEDULE_DELAY:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Programmez l'envoi au moins 5 minutes à l'avance.")
    if at > now + MAX_SCHEDULE_DELAY:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Programmez l'envoi dans l'année.")
    _check_sendable(user, row)
    db.update_newsletter(newsletter_id, only_status=("draft",), status="scheduled", scheduled_at=_iso(at),
                         sent_by=user["id"], error=None)
    return _out(db.get_newsletter(newsletter_id), with_body=True)


@router.post("/newsletters/{newsletter_id}/unschedule")
def unschedule(newsletter_id: int, user: NewsletterUser):
    _get_or_404(user, newsletter_id)
    if not db.update_newsletter(newsletter_id, only_status=("scheduled",), status="draft", scheduled_at=None):
        raise HTTPException(status.HTTP_409_CONFLICT, "Cette newsletter n'est plus programmée (envoi peut-être en cours).")
    return _out(db.get_newsletter(newsletter_id), with_body=True)


@router.post("/newsletters/{newsletter_id}/resume")
def resume(newsletter_id: int, user: NewsletterUser):
    """Relance un envoi interrompu (échec Mailjet) : seuls les destinataires restants sont visés."""
    row = _get_or_404(user, newsletter_id)
    # « sending » : envoi interrompu par un redémarrage du worker
    if row["status"] not in ("failed", "sending") or not (row["queued"] or 0):
        raise HTTPException(status.HTTP_409_CONFLICT, "Rien à reprendre pour cette newsletter.")
    _require_ready(user)
    db.update_newsletter(newsletter_id, only_status=("failed", "sending"), status="queued", error=None)
    if jobs.enqueue("newsletter_send", {"newsletter_id": newsletter_id}, accounts.display_name(user)) is None:
        # une tâche d'envoi est encore en file ou en cours : c'est elle qui envoie
        db.update_newsletter(newsletter_id, only_status=("queued",), status=row["status"], error=row["error"])
        raise HTTPException(status.HTTP_409_CONFLICT, "L'envoi est déjà en cours : patientez.")
    return _out(db.get_newsletter(newsletter_id), with_body=True)


@router.get("/newsletters/{newsletter_id}/report")
def report(newsletter_id: int, user: NewsletterUser):
    row = _get_or_404(user, newsletter_id)
    return {
        "newsletter": _out(row),
        "recipients": [dict(r) for r in db.list_recipients(newsletter_id)],
        "links": [dict(r) for r in db.clicked_links(newsletter_id)],
    }


# ---------------------------------------------------------------------------
# Désinscription (publique, par lien personnel) et préférence du compte
# ---------------------------------------------------------------------------

def _mask(email: str) -> str:
    name, _, domain = email.partition("@")
    return f"{name[:2]}{'•' * max(1, len(name) - 2)}@{domain}"


@router.get("/newsletters/unsubscribe/{token}")
def unsubscribe_info(token: str, request: Request):
    security.limiter.hit(f"unsub:ip:{security.client_ip(request)}", 60, 600)
    r = db.recipient_by_token(token)
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lien de désinscription inconnu ou expiré")
    return {"structure": r["structure_name"], "email": _mask(r["email"]),
            "unsubscribed": r["email"] in db.unsubscribed_emails(r["structure_id"])}


@router.post("/newsletters/unsubscribe/{token}")
async def unsubscribe(token: str, request: Request):
    """Désinscription depuis la page, ou « en un clic » par la messagerie (RFC 8058, corps ignoré)."""
    security.limiter.hit(f"unsub:ip:{security.client_ip(request)}", 60, 600)
    r = db.recipient_by_token(token)
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lien de désinscription inconnu ou expiré")
    db.add_unsubscribe(r["structure_id"], r["email"], "lien", _iso(_now()))
    db.record_event(r["id"], "unsub", _iso(_now()), detail="lien")
    return {"structure": r["structure_name"], "email": _mask(r["email"]), "unsubscribed": True}


class SubscriptionIn(BaseModel):
    subscribed: bool


@router.get("/me/newsletters")
def my_subscription(user: CurrentUser):
    if user["structure_id"] is None or not user["email"]:
        return {"available": False, "subscribed": False}
    return {"available": True,
            "subscribed": user["email"].lower() not in db.unsubscribed_emails(user["structure_id"])}


@router.put("/me/newsletters")
def set_my_subscription(body: SubscriptionIn, user: CurrentUser):
    if user["structure_id"] is None or not user["email"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Il faut une structure et une adresse e-mail.")
    if body.subscribed:
        db.remove_unsubscribe(user["structure_id"], user["email"])
    else:
        db.add_unsubscribe(user["structure_id"], user["email"], "compte", _iso(_now()))
    return my_subscription(user)


# ---------------------------------------------------------------------------
# Événements Mailjet (webhook)
# ---------------------------------------------------------------------------

def _recipient_from_event(ev: dict, structure_id: int) -> sqlite3.Row | None:
    """Destinataire visé, retrouvé par CustomID (« nl-<newsletter>-<destinataire> »)."""
    parts = str(ev.get("CustomID") or "").split("-")
    if len(parts) != 3 or parts[0] != "nl" or not parts[1].isdigit() or not parts[2].isdigit():
        return None
    r = db.recipient_for_event(int(parts[2]))
    if r is None or r["newsletter_id"] != int(parts[1]) or r["structure_id"] != structure_id:
        return None
    return r


def handle_event(ev: dict, structure_id: int) -> bool:
    """Applique un événement Mailjet ; False s'il est ignoré (inconnu, étranger, doublon)."""
    kind = str(ev.get("event", "")).lower()
    if kind not in mailjet.TRACKED_EVENTS:
        return False
    r = _recipient_from_event(ev, structure_id)
    if r is None:
        return False
    try:
        at = _iso(datetime.fromtimestamp(int(ev.get("time")), timezone.utc))
    except (TypeError, ValueError, OverflowError, OSError):
        at = _iso(_now())
    detail = None
    if kind in ("bounce", "blocked"):
        detail = " · ".join(str(x) for x in (ev.get("error_related_to"), ev.get("error")) if x) or None
        if kind == "bounce" and ev.get("hard_bounce"):
            detail = f"rebond définitif{' · ' + detail if detail else ''}"
    url = str(ev.get("url") or "")[:2000] if kind == "click" else ""
    added = db.record_event(r["id"], kind, at, url, detail)
    if added and kind == "spam":
        db.add_unsubscribe(structure_id, r["email"], "plainte", at)   # plus jamais de newsletter
    elif added and kind == "unsub":
        db.add_unsubscribe(structure_id, r["email"], "mailjet", at)
    return added


@router.post("/mailjet/events/{token}")
async def mailjet_events(token: str, request: Request):
    """
    Adresse de suivi déclarée chez Mailjet. Le jeton secret identifie la
    structure. Réponse 200 même pour un événement ignoré : sinon Mailjet
    renvoie le lot en boucle. Un jeton inconnu reçoit 404 (Mailjet cesse).
    """
    structure_id = db.structure_by_events_token(token)
    if structure_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Adresse de suivi inconnue")
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        return {"accepted": 0}
    events = payload if isinstance(payload, list) else [payload]
    accepted = sum(1 for ev in events[:5000] if isinstance(ev, dict) and handle_event(ev, structure_id))
    return {"accepted": accepted}


def new_unsubscribe_token() -> str:
    return secrets.token_urlsafe(24)
