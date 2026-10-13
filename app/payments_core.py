"""
Règles de paiement HelloAsso, sans réseau (voir payments.py pour les routes) : prix d'une sortie, saison de la
cotisation, état de la cotisation d'un membre, blocage d'inscription, configuration montrée aux membres.

- Prix : par type de créneau (types.price_cents) ; 0 ou vide : gratuit. « Gratuit pour les encadrants » : les membres
  qui ont un niveau d'encadrant dans leur fiche plongeur ne paient pas.
- Cotisation : montant unique ; la saison commence à une date réglable (MM-JJ, 1er septembre par défaut) et dure un
  an ; un paiement vaut pour la saison où il tombe, jusqu'à la veille du début de la suivante. Elle peut être exigée
  (pas d'inscription sans cotisation à jour le jour de l'inscription) ou seulement proposée.
- Rien n'est montré aux membres sans réglages, ni sans usage activé : `member_config` renvoie None.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import db

PARIS = ZoneInfo("Europe/Paris")


def parse_season_start(value: str | None) -> tuple[int, int]:
    """(mois, jour) d'un début de saison « MM-JJ » ; 1er septembre si invalide."""
    try:
        month, day = (int(x) for x in (value or "").split("-"))
        date(2001, month, day)       # année non bissextile : le 29 février est refusé
        return month, day
    except (ValueError, TypeError):
        return 9, 1


def season_bounds(season_start: str | None, on: date) -> tuple[date, date]:
    """Premier et dernier jour de la saison qui contient `on`."""
    month, day = parse_season_start(season_start)
    start = date(on.year, month, day)
    if on < start:
        start = date(on.year - 1, month, day)
    return start, date(start.year + 1, month, day) - timedelta(days=1)


def is_instructor(user: sqlite3.Row | dict | None) -> bool:
    keys = user.keys() if user is not None and hasattr(user, "keys") else ()
    return bool(user is not None and "instructor_level" in keys and user["instructor_level"])


def slot_price_cents(price_cents: int | None, free_for_instructors: bool, user) -> int:
    """Ce que doit payer ce membre pour une sortie de ce type : 0 si gratuit."""
    if not price_cents:
        return 0
    return 0 if free_for_instructors and is_instructor(user) else int(price_cents)


def membership_state(settings: sqlite3.Row | None, structure_id: int, user_id: int, on: date, *,
                     license: bool = False) -> dict:
    """{valid, valid_until, method} de la cotisation du membre le jour `on` (la plus longue des cotisations payées).
    license=True : même chose pour la licence FFESSM (payée avec la cotisation ou seule)."""
    best = None
    for p in db.membership_payments(structure_id, user_id):
        part = p["license_cents"] if license else p["amount_cents"] - p["license_cents"]
        if part > 0 and p["valid_from"] and p["valid_until"] and p["valid_from"] <= on.isoformat() <= p["valid_until"]:
            if best is None or p["valid_until"] > best["valid_until"]:
                best = p
    return {"valid": best is not None, "valid_until": best["valid_until"] if best else None,
            "method": best["method"] if best else None}


def license_enabled(settings: sqlite3.Row | None) -> bool:
    return bool(settings is not None and settings["use_membership"] and settings["membership_amount_cents"]
                and settings["license_amount_cents"])


def payment_label(settings: sqlite3.Row, on: date, *, membership: bool = True, license: bool = False) -> str:
    start, end = season_bounds(settings["membership_season_start"], on)
    season = f"{start.year}-{end.year}" if start.year != end.year else str(start.year)
    what = {(True, False): "Cotisation", (True, True): "Cotisation et licence FFESSM",
            (False, True): "Licence FFESSM"}[(membership, license)]
    return f"{what} {season}"


def member_config(structure_id: int | None, user_id: int, today: date) -> dict | None:
    """Ce que voient les membres de HelloAsso ; None sans réglages ou sans usage activé (rien n'est affiché)."""
    if structure_id is None:
        return None
    s = db.get_helloasso(structure_id)
    if s is None or not (s["use_slots"] or (s["use_membership"] and s["membership_amount_cents"])):
        return None
    membership = None
    if s["use_membership"] and s["membership_amount_cents"]:
        start, end = season_bounds(s["membership_season_start"], today)
        membership = {"amount_cents": s["membership_amount_cents"], "required": bool(s["membership_required"]),
                      "season_start": start.isoformat(), "season_end": end.isoformat(),
                      **membership_state(s, structure_id, user_id, today)}
        if license_enabled(s):
            lic = membership_state(s, structure_id, user_id, today, license=True)
            membership["license"] = {"amount_cents": s["license_amount_cents"], **lic}
    return {"slots": bool(s["use_slots"]), "membership": membership, "sandbox": bool(s["sandbox"])}


def registration_block(structure_id: int, member, *, own: bool = True) -> str | None:
    """Motif de refus d'inscription si la structure exige la cotisation et que le membre n'est pas à jour AUJOURD'HUI
    (la cotisation est administrative : on ne la compare pas à la date de la sortie) ; None sinon.
    own : message adressé au membre lui-même."""
    s = db.get_helloasso(structure_id)
    if s is None or not (s["use_membership"] and s["membership_amount_cents"] and s["membership_required"]):
        return None
    if membership_state(s, structure_id, member["id"], datetime.now(PARIS).date())["valid"]:
        return None
    amount = f"{s['membership_amount_cents'] / 100:.2f}".replace(".", ",")
    if own:
        return (f"Inscription impossible : votre cotisation au club n'est pas à jour ({amount} €). "
                "Réglez-la depuis « Ma cotisation » (menu du compte).")
    name = " ".join(x for x in (member["first_name"], member["last_name"]) if x) or member["username"]
    return f"Inscription impossible pour {name} : cotisation au club non à jour."
