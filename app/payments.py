"""
Paiement des sorties et cotisation du club par HelloAsso.

- Réglages de la structure (administrateurs) : identifiant de l'association et clés API HelloAsso (chiffrées), mode
  test (bac à sable), usages activés (sorties, cotisation), montant de la cotisation, début de saison, cotisation
  exigée pour s'inscrire. Sans réglages, ou sans usage activé, RIEN n'est affiché aux membres (payments_core).
- Sorties : prix par type de créneau (administration → Créneaux), gratuit possible pour les encadrants. Le paiement
  est FACULTATIF : un membre inscrit (confirmé) peut payer depuis la liste des créneaux ; l'inscription ne dépend pas
  du paiement. Un paiement reçu pour une sortie annulée, ou dont l'inscription a disparu, est signalé « à rembourser » ;
  le remboursement se fait dans HelloAsso, puis se marque ici.
- Cotisation : montant unique, saison à date réglable ; payée en ligne ou enregistrée à la main (chèque, espèces) par
  un administrateur ; peut bloquer l'inscription (payments_core.registration_block).
- Confirmation d'un paiement : relue chez HelloAsso (helloasso.get_checkout), au retour du membre ou à la
  réception d'une notification — jamais d'après l'adresse de retour du navigateur.

Routes : GET / PUT / DELETE /api/admin/helloasso, POST /api/admin/helloasso/test, GET /api/admin/payments,
GET /api/admin/helloasso/members, POST /api/admin/helloasso/members/{id}/membership, POST …/payments/{id}/refunded,
DELETE …/payments/{id} (paiement manuel), POST /api/selections/{id}/pay, POST /api/me/membership/pay,
GET /api/me/payments/{id}, POST /api/helloasso/notifications/{jeton} (public, appelé par HelloAsso).
"""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import db, helloasso, mailer, payments_core, secrets_store, security
from .auth import CurrentManager, CurrentMember, can_manage_structure, scope_structure
from .selections import _today, split_registrations

router = APIRouter(prefix="/api")

PARIS = ZoneInfo("Europe/Paris")
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,100}$")
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{8,200}$")
MAX_AMOUNT_CENTS = 100_000
IP_CHECK = os.environ.get("HELLOASSO_IP_CHECK", "1").lower() not in ("0", "false", "no")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime | None = None) -> str:
    return (t or _now()).isoformat(timespec="seconds")


def _eur(cents: int | None) -> str:
    return f"{(cents or 0) / 100:.2f}".replace(".", ",") + " €"


# ---------------------------------------------------------------------------
# Réglages (administrateurs de la structure)
# ---------------------------------------------------------------------------

class SettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    org_slug: str
    client_id: str | None = None
    client_secret: str | None = None
    sandbox: bool = False
    use_slots: bool = False
    use_membership: bool = False
    membership_amount_eur: float | None = Field(None, ge=0, le=MAX_AMOUNT_CENTS / 100)
    membership_season_start: str = "09-01"
    membership_required: bool = False

    @field_validator("org_slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        v = v.strip().lower()
        if not SLUG_PATTERN.match(v):
            raise ValueError("identifiant d'association invalide (lettres minuscules, chiffres et tirets)")
        return v

    @field_validator("client_id", "client_secret")
    @classmethod
    def _key(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if not KEY_PATTERN.match(v):
            raise ValueError("clé invalide")
        return v

    @field_validator("membership_season_start")
    @classmethod
    def _season(cls, v: str) -> str:
        if not re.fullmatch(r"\d{2}-\d{2}", v) or payments_core.parse_season_start(v) != tuple(int(x) for x in v.split("-")):
            raise ValueError("début de saison invalide (MM-JJ, par exemple 09-01)")
        return v


def notify_url(token: str) -> str | None:
    return mailer.link(f"api/helloasso/notifications/{token}") if mailer.BASE_URL else None


def _out(structure_id: int) -> dict:
    row = db.get_helloasso(structure_id)
    reason = secrets_store.unavailable_reason()
    out = {"structure_id": structure_id, "configured": row is not None, "encryption_ok": reason is None,
           "encryption_error": reason, "https": mailer.BASE_URL.startswith("https://")}
    if row is not None:
        out.update({
            "org_slug": row["org_slug"], "client_id_hint": row["client_id_hint"], "sandbox": bool(row["sandbox"]),
            "use_slots": bool(row["use_slots"]), "use_membership": bool(row["use_membership"]),
            "membership_amount_cents": row["membership_amount_cents"],
            "membership_season_start": row["membership_season_start"],
            "membership_required": bool(row["membership_required"]),
            "notify_url": notify_url(row["notify_token"]),
            "checked_at": row["checked_at"], "check_ok": None if row["check_ok"] is None else bool(row["check_ok"]),
            "check_message": row["check_message"], "updated_at": row["updated_at"], "updated_by": row["updated_by"],
        })
    return out


def _scope(actor, structure_id: int | None) -> int:
    return scope_structure(actor, structure_id)


def _credentials(structure_id: int) -> tuple[sqlite3.Row, str, str]:
    row = db.get_helloasso(structure_id)
    if row is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "HelloAsso n'est pas connecté pour cette structure (administration → Paiements).")
    try:
        return row, secrets_store.decrypt(row["client_id_enc"]), secrets_store.decrypt(row["client_secret_enc"])
    except secrets_store.SecretsError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))


@router.get("/admin/helloasso")
def get_settings(actor: CurrentManager, structure_id: int | None = None):
    return _out(_scope(actor, structure_id))


@router.put("/admin/helloasso")
def save_settings(body: SettingsIn, actor: CurrentManager, structure_id: int | None = None):
    sid = _scope(actor, structure_id)
    reason = secrets_store.unavailable_reason()
    if reason:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Chiffrement des clés indisponible : {reason}")
    row = db.get_helloasso(sid)
    if (body.client_id is None) != (body.client_secret is None):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Saisissez le client_id ET le client_secret (ils vont ensemble), ou aucun des deux pour les garder.")
    if body.client_id is None and row is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Saisissez le client_id et le client_secret HelloAsso.")
    cents = round(body.membership_amount_eur * 100) if body.membership_amount_eur else None
    if body.use_membership and not cents:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Indiquez le montant de la cotisation.")
    if body.membership_required and not body.use_membership:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "La cotisation ne peut être exigée que si elle est activée.")
    if (body.use_slots or body.use_membership) and not mailer.BASE_URL.startswith("https://"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "APP_BASE_URL doit être une adresse https : HelloAsso n'accepte que des adresses de retour sécurisées.")
    fields = dict(org_slug=body.org_slug, sandbox=body.sandbox, use_slots=body.use_slots,
                  use_membership=body.use_membership, membership_amount_cents=cents,
                  membership_season_start=body.membership_season_start, membership_required=body.membership_required,
                  updated_at=_iso(), updated_by=actor["username"])
    if body.client_id is not None:
        if row is not None:
            helloasso.forget_token(bool(row["sandbox"]), secrets_store.decrypt(row["client_id_enc"]))
        fields.update(client_id_enc=secrets_store.encrypt(body.client_id),
                      client_secret_enc=secrets_store.encrypt(body.client_secret), client_id_hint=body.client_id[-4:])
    if row is None:
        fields["notify_token"] = secrets.token_urlsafe(32)
    db.save_helloasso(sid, **fields)
    return _out(sid)


@router.post("/admin/helloasso/test")
def test_connection(actor: CurrentManager, structure_id: int | None = None):
    """Vérifie les clés et l'identifiant de l'association auprès de HelloAsso."""
    sid = _scope(actor, structure_id)
    row, client_id, client_secret = _credentials(sid)
    try:
        org = helloasso.organization(bool(row["sandbox"]), client_id, client_secret, row["org_slug"])
        name = org.get("name") or row["org_slug"]
        db.save_helloasso_check(sid, True, f"Connecté à HelloAsso : association « {name} ».", _iso())
    except helloasso.HelloAssoError as exc:
        db.save_helloasso_check(sid, False, str(exc), _iso())
    return _out(sid)


@router.delete("/admin/helloasso", status_code=204)
def delete_settings(actor: CurrentManager, structure_id: int | None = None):
    """Retire la connexion HelloAsso : plus rien n'est proposé aux membres (les paiements enregistrés restent)."""
    sid = _scope(actor, structure_id)
    if not db.delete_helloasso(sid):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "HelloAsso n'est pas connecté pour cette structure")


# ---------------------------------------------------------------------------
# Paiements : liste, cotisations des membres, enregistrement à la main
# ---------------------------------------------------------------------------

def _payment_out(p: sqlite3.Row) -> dict:
    due = bool(p["kind"] == "slot" and p["status"] == "paid" and (p["slot_cancelled"] or p["registration_gone"]))
    return {"id": p["id"], "kind": p["kind"], "label": p["label"], "user_id": p["user_id"], "user": p["user_name"],
            "amount_cents": p["amount_cents"], "status": p["status"], "method": p["method"],
            "created_at": p["created_at"], "paid_at": p["paid_at"], "receipt_url": p["receipt_url"],
            "valid_until": p["valid_until"], "recorded_by": p["recorded_by"],
            "refund_due": due, "refund_reason": ("sortie annulée" if p["slot_cancelled"] else "inscription retirée") if due else None}


@router.get("/admin/payments")
def list_payments(actor: CurrentManager, structure_id: int | None = None, kind: str | None = None,
                  payment_status: str | None = None):
    """Paiements de la structure (plus récents d'abord), avec les totaux encaissés et les remboursements à faire."""
    sid = _scope(actor, structure_id)
    rows = db.list_payments(sid, kind=kind, status=payment_status)
    out = [_payment_out(p) for p in rows]
    paid = [p for p in out if p["status"] == "paid"]
    return {"payments": out, "summary": {
        "paid_count": len(paid), "paid_cents": sum(p["amount_cents"] for p in paid),
        "slots_cents": sum(p["amount_cents"] for p in paid if p["kind"] == "slot"),
        "membership_cents": sum(p["amount_cents"] for p in paid if p["kind"] == "membership"),
        "refund_due": sum(1 for p in out if p["refund_due"])}}


def _managed_payment_or_404(actor, payment_id: int) -> sqlite3.Row:
    p = db.get_payment(payment_id)
    if p is None or not can_manage_structure(actor, p["structure_id"]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Paiement inconnu")
    return p


@router.post("/admin/payments/{payment_id}/refunded")
def mark_refunded(payment_id: int, actor: CurrentManager):
    """Marque un paiement remboursé (le remboursement lui-même se fait dans l'administration de HelloAsso)."""
    p = _managed_payment_or_404(actor, payment_id)
    if p["status"] != "paid":
        raise HTTPException(status.HTTP_409_CONFLICT, "Seul un paiement reçu peut être marqué remboursé")
    db.update_payment(payment_id, status="refunded", recorded_by=actor["username"])
    return _payment_out(db.get_payment(payment_id))


@router.delete("/admin/payments/{payment_id}", status_code=204)
def delete_manual_payment(payment_id: int, actor: CurrentManager):
    """Supprime une cotisation enregistrée à la main par erreur (les paiements HelloAsso ne se suppriment pas)."""
    p = _managed_payment_or_404(actor, payment_id)
    if p["method"] != "manual":
        raise HTTPException(status.HTTP_409_CONFLICT, "Seul un paiement enregistré à la main peut être supprimé")
    with db.get_conn() as conn:
        conn.execute("DELETE FROM payments WHERE id = ?", (payment_id,))


@router.get("/admin/helloasso/members")
def membership_members(actor: CurrentManager, structure_id: int | None = None):
    """Cotisation de chaque membre de la structure aujourd'hui."""
    sid = _scope(actor, structure_id)
    settings = db.get_helloasso(sid)
    today = datetime.now(PARIS).date()
    out = []
    for m in db.structure_members(sid):
        state = payments_core.membership_state(settings, sid, m["id"], today)
        out.append({"id": m["id"], "name": " ".join(x for x in (m["first_name"], m["last_name"]) if x) or m["username"],
                    "role": m["structure_role"], **state})
    return out


@router.post("/admin/helloasso/members/{user_id}/membership", status_code=201)
def record_membership(user_id: int, actor: CurrentManager, structure_id: int | None = None):
    """Enregistre une cotisation reçue hors ligne (chèque, espèces) pour la saison en cours."""
    sid = _scope(actor, structure_id)
    settings = db.get_helloasso(sid)
    if settings is None or not (settings["use_membership"] and settings["membership_amount_cents"]):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "La cotisation n'est pas activée pour cette structure")
    if db.get_membership(user_id, sid) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Membre inconnu dans cette structure")
    today = datetime.now(PARIS).date()
    state = payments_core.membership_state(settings, sid, user_id, today)
    if state["valid"]:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Cotisation déjà à jour jusqu'au {state['valid_until']}")
    start, end = payments_core.season_bounds(settings["membership_season_start"], today)
    pid = db.create_payment(sid, user_id, "membership", payments_core.membership_label(settings, today),
                            settings["membership_amount_cents"], _iso(), status="paid", method="manual",
                            valid_from=start.isoformat(), valid_until=end.isoformat(), paid_at=_iso(),
                            recorded_by=actor["username"])
    return _payment_out(db.get_payment(pid))


# ---------------------------------------------------------------------------
# Payer en ligne (membres)
# ---------------------------------------------------------------------------

def _require_https() -> None:
    if not mailer.BASE_URL.startswith("https://"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Paiement indisponible : APP_BASE_URL doit être une adresse https.")


def _start_checkout(user, structure_id: int, payment_id: int, amount_cents: int, item_name: str) -> dict:
    """Crée la demande chez HelloAsso et renvoie l'adresse de la page de paiement."""
    _require_https()
    row, client_id, client_secret = _credentials(structure_id)
    base = mailer.link("mes-creneaux.html")
    try:
        intent_id, redirect = helloasso.create_checkout(
            bool(row["sandbox"]), client_id, client_secret, row["org_slug"], amount_cents=amount_cents,
            item_name=item_name, back_url=base, error_url=f"{base}?paiement={payment_id}&erreur=1",
            return_url=f"{base}?paiement={payment_id}",
            payer={"firstName": user["first_name"], "lastName": user["last_name"], "email": user["email"]},
            metadata={"calendive_payment": payment_id, "structure_id": structure_id})
    except helloasso.HelloAssoError as exc:
        db.update_payment(payment_id, status="failed")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Paiement impossible : {exc}")
    db.update_payment(payment_id, checkout_intent_id=intent_id)
    return {"payment_id": payment_id, "redirect_url": redirect, "amount_cents": amount_cents}


@router.post("/selections/{selection_id}/pay")
def pay_slot(selection_id: int, user: CurrentMember):
    """Paie une sortie avec HelloAsso (inscrit confirmé) : renvoie l'adresse de la page de paiement."""
    sid = user["structure_id"]
    settings = db.get_helloasso(sid)
    if settings is None or not settings["use_slots"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Le paiement des sorties n'est pas proposé par votre structure")
    row = db.get_selection(sid, selection_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Créneau introuvable")
    if row["cancelled_at"]:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ce créneau est annulé")
    if (row["end_date"] or row["local_date"]) < _today():
        raise HTTPException(status.HTTP_409_CONFLICT, "Ce créneau est passé")
    regs = [r["user_id"] for r in db.list_registrations(sid, selection_id)]
    if user["id"] not in regs:
        raise HTTPException(status.HTTP_409_CONFLICT, "Inscrivez-vous d'abord à cette sortie")
    if user["id"] not in split_registrations(regs, row["max_registrations"])[0]:
        raise HTTPException(status.HTTP_409_CONFLICT, "Vous êtes en file d'attente : le paiement est possible une fois votre place confirmée")
    amount = payments_core.slot_price_cents(row["type_price_cents"], bool(row["type_free_for_instructors"]), user)
    if amount <= 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Cette sortie est gratuite pour vous")
    existing = db.open_slot_payment(user["id"], selection_id)
    if existing is not None and existing["status"] == "paid":
        raise HTTPException(status.HTTP_409_CONFLICT, "Cette sortie est déjà payée")
    day = row["local_date"]
    label = f"{row['type_label']} du {day[8:10]}/{day[5:7]}/{day[:4]}, {row['port_name']}"
    pid = db.create_payment(sid, user["id"], "slot", label, amount, _iso(), selection_id=selection_id)
    return _start_checkout(user, sid, pid, amount, f"{label} — {user['structure_name']}")


@router.post("/me/membership/pay")
def pay_membership(user: CurrentMember):
    """Paie la cotisation de la saison avec HelloAsso."""
    sid = user["structure_id"]
    settings = db.get_helloasso(sid)
    if settings is None or not (settings["use_membership"] and settings["membership_amount_cents"]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "La cotisation en ligne n'est pas proposée par votre structure")
    today = datetime.now(PARIS).date()
    state = payments_core.membership_state(settings, sid, user["id"], today)
    if state["valid"]:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Votre cotisation est à jour jusqu'au {state['valid_until']}")
    label = payments_core.membership_label(settings, today)
    amount = settings["membership_amount_cents"]
    pid = db.create_payment(sid, user["id"], "membership", label, amount, _iso())
    return _start_checkout(user, sid, pid, amount, f"{label} — {user['structure_name']}")


def sync_payment(p: sqlite3.Row) -> sqlite3.Row:
    """Relit un paiement en attente chez HelloAsso et met à jour son état ; renvoie la ligne à jour."""
    if p["status"] not in ("pending", "paid") or p["method"] != "helloasso" or not p["checkout_intent_id"]:
        return p
    settings = db.get_helloasso(p["structure_id"])
    if settings is None:
        return p
    try:
        client_id = secrets_store.decrypt(settings["client_id_enc"])
        client_secret = secrets_store.decrypt(settings["client_secret_enc"])
        checkout = helloasso.get_checkout(bool(settings["sandbox"]), client_id, client_secret, settings["org_slug"],
                                          p["checkout_intent_id"])
    except (helloasso.HelloAssoError, secrets_store.SecretsError):
        return p                                    # injoignable : on réessaiera au prochain passage
    state, order_id, receipt = helloasso.outcome(checkout)
    if state == "paid" and p["status"] == "pending":
        fields = dict(status="paid", order_id=order_id, receipt_url=receipt, paid_at=_iso())
        if p["kind"] == "membership":
            start, end = payments_core.season_bounds(settings["membership_season_start"], datetime.now(PARIS).date())
            fields.update(valid_from=start.isoformat(), valid_until=end.isoformat())
        db.update_payment(p["id"], **fields)
    elif state == "refunded" and p["status"] in ("pending", "paid"):
        db.update_payment(p["id"], status="refunded", order_id=order_id)
    elif state == "failed" and p["status"] == "pending":
        db.update_payment(p["id"], status="failed")
    elif state == "pending" and p["status"] == "pending":
        started = datetime.fromisoformat(p["created_at"])
        if (_now() - started).total_seconds() > helloasso.ABANDON_AFTER_S:
            db.update_payment(p["id"], status="failed")      # abandonné : pas de paiement après 45 minutes
    return db.get_payment(p["id"])


@router.get("/me/payments/{payment_id}")
def my_payment(payment_id: int, user: CurrentMember):
    """État d'un de ses paiements, relu chez HelloAsso s'il est en attente (retour de la page de paiement)."""
    p = db.get_payment(payment_id)
    if p is None or p["user_id"] != user["id"] or p["structure_id"] != user["structure_id"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Paiement inconnu")
    p = sync_payment(p)
    return {"id": p["id"], "kind": p["kind"], "label": p["label"], "amount_cents": p["amount_cents"],
            "status": p["status"], "receipt_url": p["receipt_url"], "selection_id": p["selection_id"]}


# ---------------------------------------------------------------------------
# Notifications de HelloAsso (webhook)
# ---------------------------------------------------------------------------

@router.post("/helloasso/notifications/{token}", include_in_schema=False)
async def notifications(token: str, request: Request):
    """Adresse à déclarer dans HelloAsso (Mon compte → Intégrations et API). Ne fait que déclencher la relecture du
    paiement concerné chez HelloAsso : le contenu reçu n'est jamais cru. Contrôles : jeton secret de la structure,
    adresse d'envoi de HelloAsso (HELLOASSO_IP_CHECK=0 pour la désactiver derrière un proxy mal configuré)."""
    security.limiter.hit(f"helloasso:{token[:12]}", 120, 60)
    settings = db.get_helloasso_by_token(token)
    if settings is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Adresse inconnue")
    if IP_CHECK and security.client_ip(request) != helloasso.NOTIFY_IPS[bool(settings["sandbox"])]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Origine non reconnue")
    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    meta = payload.get("metadata") if isinstance(payload, dict) else None
    ref = meta.get("calendive_payment") if isinstance(meta, dict) else None
    if isinstance(ref, int):
        p = db.get_payment(ref)
        if p is not None and p["structure_id"] == settings["structure_id"]:
            sync_payment(p)
    return {"ok": True}
