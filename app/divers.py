"""
Fiches plongeurs des membres d'une structure : niveaux, licence, certificat médical (CACI), voir diver.py.

- Liste et modification : administrateurs de la structure et comptes au profil « Gestionnaire » (view_divers).
- Validation d'une date de CACI : profil « Gestionnaire » ou super administrateur (validate_caci). Une date saisie
  ou modifiée par un compte qui peut valider est validée d'office.

Routes : /api/divers (structure active du compte).
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

from fastapi import APIRouter, HTTPException, status

from . import accounts, db, diver
from .auth import CurrentMember

router = APIRouter(prefix="/api/divers")


def _require(user: sqlite3.Row, permission: str, message: str) -> None:
    if not accounts.permissions(user)[permission]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, message)


def _member_or_404(user_id: int, structure_id: int) -> sqlite3.Row:
    row = db.get_user(user_id, structure_id)
    if row is None or row["structure_id"] != structure_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Membre inconnu dans votre structure")
    return row


def diver_row_out(row: sqlite3.Row, months: int | None) -> dict:
    return {"id": row["id"], "display_name": accounts.display_name(row), "username": row["username"],
            "role": row["structure_role"], **diver.diver_out(row),
            "caci": diver.caci_state(row, date.today(), months)}


@router.get("/levels")
def levels():
    """Catalogues des niveaux (listes de la fiche plongeur)."""
    return {"levels": diver.DIVER_LEVELS, "instructor_levels": diver.INSTRUCTOR_LEVELS}


@router.get("")
def list_divers(user: CurrentMember):
    """Fiches plongeurs des membres de la structure active, avec l'état de leur CACI aujourd'hui."""
    _require(user, "view_divers", "Réservé aux administrateurs de la structure et au profil « Gestionnaire »")
    sid = user["structure_id"]
    structure = db.get_structure(sid)
    months = structure["caci_validity_months"]
    return {
        "caci_check": bool(structure["caci_check"]),
        "caci_validity_months": months,
        "can_validate": accounts.permissions(user)["validate_caci"],
        "divers": [diver_row_out(r, months) for r in db.list_users(sid)],
        "levels": diver.DIVER_LEVELS,
        "instructor_levels": diver.INSTRUCTOR_LEVELS,
    }


@router.put("/{user_id}")
def update_diver(user_id: int, body: diver.DiverFields, user: CurrentMember):
    """Modifier la fiche plongeur d'un membre (champs envoyés seulement)."""
    _require(user, "view_divers", "Réservé aux administrateurs de la structure et au profil « Gestionnaire »")
    sid = user["structure_id"]
    _member_or_404(user_id, sid)
    can_validate = accounts.permissions(user)["validate_caci"]
    diver.apply(user_id, body, body.model_fields_set,
                validator_name=accounts.display_name(user) if can_validate else None)
    return diver_row_out(db.get_user(user_id, sid), db.get_structure(sid)["caci_validity_months"])


@router.post("/{user_id}/caci/validate")
def validate_caci(user_id: int, user: CurrentMember):
    """Valider la date de CACI saisie par un membre."""
    _require(user, "validate_caci", "Réservé au profil « Gestionnaire »")
    sid = user["structure_id"]
    member = _member_or_404(user_id, sid)
    if not member["caci_date"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Aucune date de certificat médical à valider")
    db.validate_caci(user_id, accounts.display_name(user), datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return diver_row_out(db.get_user(user_id, sid), db.get_structure(sid)["caci_validity_months"])
