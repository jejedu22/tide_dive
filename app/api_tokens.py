"""
Jetons d'API : un outil tiers (tableur, automate, site du club…) appelle l'API au nom d'un compte.

- Le membre crée ses jetons dans le menu du compte → « Jetons d'API » : un nom (l'outil qui s'en sert),
  une portée (lecture seule, ou lecture et écriture) et une durée. Le jeton n'est montré qu'à sa création ;
  seul son SHA-256 est gardé. Il se révoque à tout moment.
- Usage : en-tête `Authorization: Bearer cdv_…`. Le jeton a les droits du compte dans la structure où il a été
  créé (rôle et profils du moment : un changement de rôle s'applique tout de suite, un départ de la structure
  coupe l'accès). En lecture seule : GET uniquement.
- Jamais avec un jeton : ce qui touche au compte lui-même (mot de passe, double authentification, sessions,
  jetons, export de ses données, liens d'agenda) ni aucune modification sous /api/me et /api/auth
  (voir auth._check_api_token). Pas de jeton pour un super administrateur : son accès exige la double
  authentification, qu'un jeton contournerait.
- Au plus API_TOKEN_RATE_PER_MIN requêtes par minute et par jeton (120 par défaut).
- Documentation interactive de l'API : /api-docs.html (Swagger), description OpenAPI : /api/openapi.json.

Routes : GET/POST /api/me/api-tokens, DELETE /api/me/api-tokens/{id}.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from . import db
from .accounts import iso, now, token_hash
from .auth import API_TOKEN_PREFIX, API_TOKEN_RATE_PER_MIN, CurrentMember

router = APIRouter(prefix="/api/me", tags=["Jetons d'API"])

MAX_TOKENS = 20
SCOPES = {"read": "Lecture seule", "write": "Lecture et écriture"}


class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=60, description="Outil qui se sert du jeton")
    scope: Literal["read", "write"] = Field("read", description="read : GET uniquement ; write : toutes les méthodes")
    expires_days: int | None = Field(365, ge=1, le=730, description="Durée de validité en jours ; null : sans expiration")

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("nom vide")
        return v


def _out(r) -> dict:
    return {"id": r["id"], "name": r["name"], "prefix": r["prefix"], "scope": r["scope"],
            "scope_label": SCOPES[r["scope"]], "structure_id": r["structure_id"], "structure": r["structure_name"],
            "created_at": r["created_at"], "expires_at": r["expires_at"], "last_used_at": r["last_used_at"]}


def _forbid_super_admin(user) -> None:
    if user["is_admin"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Pas de jeton d'API pour un super administrateur : "
                                                       "son accès exige la double authentification")


@router.get("/api-tokens")
def list_tokens(user: CurrentMember):
    """Jetons du compte (jamais les jetons eux-mêmes : leur début seulement)."""
    _forbid_super_admin(user)
    return {"tokens": [_out(r) for r in db.list_api_tokens(user["id"])], "scopes": SCOPES,
            "max": MAX_TOKENS, "rate_per_min": API_TOKEN_RATE_PER_MIN}


@router.post("/api-tokens", status_code=201)
def create_token(body: TokenIn, user: CurrentMember):
    """Crée un jeton pour la structure active du compte. La réponse contient le jeton, montré cette fois-ci
    seulement."""
    _forbid_super_admin(user)
    if len(db.list_api_tokens(user["id"])) >= MAX_TOKENS:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Au plus {MAX_TOKENS} jetons : révoquez-en un d'abord")
    token = API_TOKEN_PREFIX + secrets.token_urlsafe(32)
    t = now()
    expires = iso(t + timedelta(days=body.expires_days)) if body.expires_days else None
    token_id = db.create_api_token(user["id"], user["structure_id"], body.name, token_hash(token),
                                   token[:len(API_TOKEN_PREFIX) + 6], body.scope, iso(t), expires)
    created = next(r for r in db.list_api_tokens(user["id"]) if r["id"] == token_id)
    return {**_out(created), "token": token}


@router.delete("/api-tokens/{token_id}", status_code=204)
def revoke_token(token_id: int, user: CurrentMember):
    if not db.delete_api_token(user["id"], token_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Jeton introuvable")
