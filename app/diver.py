"""
Fiche plongeur d'un compte : niveaux, licence FFESSM, certificat médical (CACI), et sa vérification à l'inscription.

- Niveau de plongeur et niveau d'encadrement (catalogues ci-dessous, cursus FFESSM), autres qualifications (texte
  libre : Nitrox, RIFAP…), numéro de licence et lien du QR code de la licence numérique (fiche fédérale du licencié).
- CACI : le membre saisit la DATE de son certificat ; elle est « en attente » jusqu'à sa validation par un compte
  ayant le profil « Gestionnaire » dans une structure du membre (ou un super administrateur). Une date saisie par
  un gestionnaire est validée d'office. Changer la date annule la validation.
- Validité : `caci_validity_months` mois à compter de la date du certificat (12 par défaut, réglable par structure).
- Vérification (réglage `caci_check` de la structure, désactivé par défaut) : inscription refusée sans CACI, ou
  avec un CACI expiré le jour du créneau (validé ou non). Une date en attente de validation, non expirée, ne
  bloque pas. Le contrôle vaut aussi quand un administrateur inscrit un membre.
"""

from __future__ import annotations

import calendar
import sqlite3
from datetime import date, timedelta

DIVER_LEVELS = {
    "debutant": "Débutant (baptême)",
    "n1": "Niveau 1 (PE20)",
    "pe40": "PE40",
    "pa20": "PA20",
    "n2": "Niveau 2 (PE40 / PA20)",
    "pa40": "PA40",
    "pe60": "PE60",
    "n3": "Niveau 3 (PA60)",
    "n4": "Niveau 4 (guide de palanquée)",
    "n5": "Niveau 5 (directeur de plongée)",
}

INSTRUCTOR_LEVELS = {
    "e1": "Initiateur (E1)",
    "e2": "Encadrant E2",
    "e3": "Moniteur fédéral 1er degré (E3)",
    "e4": "Moniteur fédéral 2e degré (E4)",
}

DEFAULT_VALIDITY_MONTHS = 12


def add_months(d: date, months: int) -> date:
    """Même jour, `months` mois plus tard (dernier jour du mois s'il n'existe pas : 29/02 → 28/02)."""
    m = d.month - 1 + months
    year, month = d.year + m // 12, m % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def valid_until(caci_date: str, months: int) -> date:
    """Dernier jour de validité du certificat."""
    return add_months(date.fromisoformat(caci_date), months) - timedelta(days=1)


def _field(row, key):
    if isinstance(row, dict):
        return row.get(key)
    return row[key] if key in row.keys() else None


def caci_state(row, on: date, months: int | None = None) -> dict:
    """CACI d'un compte vu le jour `on` : state = missing (pas de date), expired, pending (non validé, non
    expiré) ou valid."""
    months = months or DEFAULT_VALIDITY_MONTHS
    d = _field(row, "caci_date")
    if not d:
        return {"date": None, "valid_until": None, "validated": False, "validated_by": None,
                "validated_at": None, "state": "missing"}
    until = valid_until(d, months)
    validated = bool(_field(row, "caci_validated_at"))
    state = "expired" if on > until else ("valid" if validated else "pending")
    return {"date": d, "valid_until": until.isoformat(), "validated": validated,
            "validated_by": _field(row, "caci_validated_by"), "validated_at": _field(row, "caci_validated_at"),
            "state": state}


def diver_out(row) -> dict:
    """Fiche plongeur d'un compte (sans le CACI)."""
    level, instructor = _field(row, "diver_level"), _field(row, "instructor_level")
    return {
        "diver_level": level, "diver_level_label": DIVER_LEVELS.get(level),
        "instructor_level": instructor, "instructor_level_label": INSTRUCTOR_LEVELS.get(instructor),
        "qualifications": _field(row, "qualifications"),
        "licence_number": _field(row, "licence_number"),
        "licence_url": _field(row, "licence_url"),
    }


def _fr(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def registration_block(member: sqlite3.Row, structure: sqlite3.Row, dive_date: str, *, own: bool = True) -> str | None:
    """Motif de refus d'inscription au créneau du `dive_date` (jour local, dernier jour pour un séjour) si la
    structure vérifie le CACI ; None si l'inscription est permise. own : message adressé au membre lui-même."""
    if not structure or not structure["caci_check"] or "divers" in (structure["disabled_features"] or "").split(","):
        return None
    on = date.fromisoformat(dive_date)
    st = caci_state(member, on, structure["caci_validity_months"])
    if st["state"] == "missing":
        return ("Inscription impossible : votre structure demande un certificat médical (CACI) valable. "
                "Saisissez sa date dans « Ma fiche plongeur » (menu du compte)." if own
                else f"Inscription impossible pour {_name(member)} : aucun certificat médical (CACI) enregistré.")
    if st["state"] == "expired":
        end = _fr(date.fromisoformat(st["valid_until"]))
        return (f"Inscription impossible : votre certificat médical (CACI) n'est plus valable le jour de la plongée "
                f"(jusqu'au {end}). Saisissez la date de votre nouveau certificat dans « Ma fiche plongeur » (menu du compte)." if own
                else f"Inscription impossible pour {_name(member)} : certificat médical (CACI) valable jusqu'au "
                     f"{end}, avant la plongée.")
    return None


def _name(member) -> str:
    first, last = _field(member, "first_name"), _field(member, "last_name")
    return " ".join(x for x in (first, last) if x) or _field(member, "username") or "ce membre"


# ---------------------------------------------------------------------------
# Saisie (titulaire du compte, gestionnaire, administration)
# ---------------------------------------------------------------------------

from datetime import datetime, timezone  # noqa: E402
from typing import Literal  # noqa: E402

from pydantic import BaseModel, Field, field_validator  # noqa: E402

from . import db  # noqa: E402

DiverLevel = Literal[tuple(DIVER_LEVELS)]                 # type: ignore[valid-type]
InstructorLevel = Literal[tuple(INSTRUCTOR_LEVELS)]       # type: ignore[valid-type]


class DiverFields(BaseModel):
    """Champs de la fiche plongeur ; absent : inchangé, null ou "" : effacé."""
    diver_level: DiverLevel | None = None
    instructor_level: InstructorLevel | None = None
    qualifications: str | None = Field(None, max_length=200)
    licence_number: str | None = Field(None, max_length=30)
    licence_url: str | None = Field(None, max_length=500)
    caci_date: date | None = None

    @field_validator("diver_level", "instructor_level", mode="before")
    @classmethod
    def _empty_level(cls, v):
        return v or None

    @field_validator("qualifications", "licence_number")
    @classmethod
    def _text(cls, v):
        v = " ".join((v or "").split())
        return v or None

    @field_validator("licence_url")
    @classmethod
    def _url(cls, v):
        v = (v or "").strip()
        if not v:
            return None
        if not v.startswith("https://") or any(c.isspace() for c in v):
            raise ValueError("lien du QR code de la licence : adresse https:// attendue")
        return v

    @field_validator("caci_date", mode="before")
    @classmethod
    def _caci_empty(cls, v):
        return v or None

    @field_validator("caci_date")
    @classmethod
    def _caci(cls, v):
        if v is not None and v > date.today():
            raise ValueError("la date du certificat médical ne peut pas être dans le futur")
        return v


DIVER_KEYS = tuple(DiverFields.model_fields)


def apply(user_id: int, body: BaseModel, sent: set[str], *, validator_name: str | None) -> None:
    """Enregistre les champs envoyés de la fiche plongeur. validator_name : nom de l'auteur s'il peut valider
    un CACI (la date qu'il saisit est validée d'office), sinon None (date en attente de validation)."""
    fields = {k: getattr(body, k) for k in sent if k in DIVER_KEYS and k != "caci_date"}
    if fields:
        db.update_user(user_id, **fields)
    if "caci_date" in sent:
        d = body.caci_date.isoformat() if body.caci_date else None
        db.set_caci(user_id, d, validator_name, datetime.now(timezone.utc).isoformat(timespec="seconds"))
