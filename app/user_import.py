"""
Création de comptes à partir d'un fichier CSV (administration → Utilisateurs).

Deux temps, même route :
  1. dry_run=true  : analyse du fichier, rien n'est écrit. Chaque ligne est
     renvoyée avec l'identifiant qui sera créé et ses erreurs éventuelles.
  2. dry_run=false : création des lignes valides en UNE transaction (les
     lignes en erreur sont ignorées), puis, selon le mode :
       - "invite"   : un e-mail d'invitation par compte (lien pour choisir
                      son mot de passe) ;
       - "password" : un mot de passe provisoire généré par compte, renvoyé
                      UNE seule fois dans la réponse (le frontend en fait un
                      CSV à télécharger), à changer à la première connexion.

Format accepté :
  - séparateur « ; », « , » ou tabulation (détecté sur la ligne d'en-tête) ;
  - encodage UTF-8 (avec ou sans BOM) ou Windows-1252 (export Excel) ;
  - en-têtes insensibles à la casse et aux accents :
      nom, prenom, email          obligatoires
      telephone, role, identifiant, structure   facultatifs
    (synonymes : mail, courriel, tel, portable, login…)
  - role : visualisation / administration (vide : rôle par défaut choisi à l'import) ;
  - identifiant vide : prenom.nom (suffixe 2, 3… si déjà pris) ;
  - structure : nom exact d'une structure existante, super administrateur
    uniquement (vide : structure par défaut choisie à l'import).

L'import ne crée jamais de super administrateur.
"""

from __future__ import annotations

import base64
import binascii
import csv
import io
import re
import unicodedata
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from . import accounts, db, mailer, passwords
from .accounts import USERNAME_PATTERN, clean_email, clean_name, clean_phone, display_name, iso, now
from .auth import CurrentManager, Role, hash_password, require_mail, scope_structure

router = APIRouter(prefix="/api/admin/users")

MAX_BYTES = 512 * 1024
MAX_ROWS = 500


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.strip().lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _fold(s))


_HEADERS = {
    "last_name": {"nom", "nomdefamille", "lastname", "surname", "familyname"},
    "first_name": {"prenom", "firstname", "givenname"},
    "email": {"email", "mail", "courriel", "adresseemail", "adressemail", "emailaddress", "adressecourriel",
              "adressedecourriel", "adressemel", "mel"},
    "phone": {"telephone", "tel", "portable", "mobile", "phone", "numerodetelephone", "telephoneportable", "gsm"},
    "role": {"role", "droit", "droits", "profil"},
    "username": {"identifiant", "login", "username", "utilisateur", "pseudo"},
    "structure": {"structure", "club", "groupe"},
}
_REQUIRED = ("last_name", "first_name", "email")
_FIELD_LABELS = {"last_name": "nom", "first_name": "prenom", "email": "email"}

_ROLES = {
    "viewer": {"visualisation", "viewer", "lecture", "lecteur", "membre", "consultation"},
    "manager": {"administration", "admin", "manager", "gestionnaire", "gestion", "administrateur"},
}


class ImportIn(BaseModel):
    content_b64: str = Field(max_length=MAX_BYTES * 4 // 3 + 16, description="Fichier CSV encodé en base64")
    dry_run: bool = True
    structure_id: int | None = Field(None, description="Structure des lignes sans colonne « structure »")
    role: Role = "viewer"
    mode: Literal["invite", "password"] = "password"


def _decode(content_b64: str) -> tuple[str, str]:
    try:
        raw = base64.b64decode(content_b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Fichier illisible")
    if len(raw) > MAX_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"Fichier trop gros ({MAX_BYTES // 1024} Ko au plus)")
    try:
        return raw.decode("utf-8-sig"), "UTF-8"
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace"), "Windows-1252"


def _parse(text: str) -> tuple[str, dict[int, str], list[str], list[tuple[int, dict]]]:
    lines = text.splitlines()
    header_line = next((ln for ln in lines if ln.strip()), "")
    if not header_line:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Fichier vide")
    delimiter = max(";,\t", key=header_line.count)

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header = next((r for r in reader if any(c.strip() for c in r)), [])
    columns: dict[int, str] = {}
    ignored: list[str] = []
    for i, name in enumerate(header):
        k = _key(name)
        field = next((f for f, names in _HEADERS.items() if k in names), None)
        if field and field not in columns.values():
            columns[i] = field
        elif name.strip():
            ignored.append(name.strip())
    missing = [_FIELD_LABELS[f] for f in _REQUIRED if f not in columns.values()]
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Colonne(s) manquante(s) : {', '.join(missing)}. En-têtes attendus : "
            "nom ; prenom ; email ; telephone ; role ; identifiant ; structure",
        )

    rows: list[tuple[int, dict]] = []
    for record in reader:
        if not any(c.strip() for c in record):
            continue
        values = {f: (record[i].strip() if i < len(record) else "") for i, f in columns.items()}
        rows.append((reader.line_num, values))
        if len(rows) > MAX_ROWS:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, f"{MAX_ROWS} comptes au plus par import : découpez le fichier",
            )
    if not rows:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Aucune ligne à importer sous l'en-tête")
    return delimiter, columns, ignored, rows


def _analyse(body: ImportIn, actor) -> dict:
    text, encoding = _decode(body.content_b64)
    delimiter, columns, ignored, raw_rows = _parse(text)

    default_structure = scope_structure(actor, body.structure_id, required=False)
    structures = {_fold(s["name"]): s for s in db.list_structures()}
    own = db.get_structure(actor["structure_id"]) if actor["structure_id"] else None

    rows: list[dict] = []
    for line, v in raw_rows:
        r = {"line": line, "errors": [], "username": None, "first_name": v.get("first_name") or None,
             "last_name": v.get("last_name") or None, "email": v.get("email") or None,
             "phone": v.get("phone") or None, "structure": None, "role": body.role, "generated_username": False}
        for field, fn in (("first_name", lambda x: clean_name(x, "Prénom")),
                          ("last_name", lambda x: clean_name(x, "Nom")),
                          ("email", clean_email), ("phone", clean_phone)):
            try:
                r[field] = fn(v.get(field) or ("" if field != "phone" else None))
            except ValueError as e:
                r["errors"].append(str(e))

        role_value = _fold(v.get("role", ""))
        if role_value:
            role = next((k for k, names in _ROLES.items() if role_value in names), None)
            if role is None:
                r["errors"].append(f"Rôle inconnu : « {v['role']} » (visualisation ou administration)")
            else:
                r["role"] = role

        st_value = v.get("structure", "")
        if not actor["is_admin"]:
            if st_value and own and _fold(st_value) != _fold(own["name"]):
                r["errors"].append(f"Vous ne pouvez importer que dans votre structure (« {own['name']} »)")
            st_id = actor["structure_id"]
        elif st_value:
            st = structures.get(_fold(st_value))
            st_id = st["id"] if st else None
            if st is None:
                r["errors"].append(f"Structure inconnue : « {st_value} »")
        else:
            st_id = default_structure
            if st_id is None:
                r["errors"].append("Structure manquante : choisissez une structure par défaut")
        if st_id is not None:
            st = db.get_structure(st_id)
            r["structure"] = {"id": st["id"], "name": st["name"]}

        username = v.get("username", "")
        if username:
            if re.match(USERNAME_PATTERN, username):
                r["username"] = username
            else:
                r["errors"].append(f"Identifiant invalide : « {username} » (3 à 32 caractères : lettres, chiffres, . _ -)")
        rows.append(r)

    # doublons dans le fichier et avec les comptes existants
    taken_u, taken_e = db.existing_logins(
        [r["username"] for r in rows if r["username"]], [r["email"] for r in rows if r["email"]],
    )
    seen_u: dict[str, int] = {}
    seen_e: dict[str, int] = {}
    for r in rows:
        if r["email"]:
            if r["email"] in taken_e:
                r["errors"].append("Adresse e-mail déjà utilisée par un compte existant")
            elif r["email"] in seen_e:
                r["errors"].append(f"Adresse e-mail en double (ligne {seen_e[r['email']]})")
            seen_e.setdefault(r["email"], r["line"])
        if r["username"]:
            u = r["username"].lower()
            if u in taken_u:
                r["errors"].append(f"Identifiant « {r['username']} » déjà pris")
            elif u in seen_u:
                r["errors"].append(f"Identifiant « {r['username']} » en double (ligne {seen_u[u]})")
            seen_u.setdefault(u, r["line"])

    # identifiants proposés pour les lignes valides qui n'en ont pas
    for r in rows:
        if not r["errors"] and not r["username"]:
            r["username"] = accounts.suggest_username(r["first_name"], r["last_name"], r["email"], set(seen_u))
            r["generated_username"] = True
            seen_u[r["username"].lower()] = r["line"]

    valid = [r for r in rows if not r["errors"]]
    return {
        "encoding": encoding,
        "delimiter": {";": "point-virgule", ",": "virgule", "\t": "tabulation"}[delimiter],
        "columns": sorted(set(columns.values())),
        "ignored_columns": ignored,
        "rows": rows,
        "valid": len(valid),
        "invalid": len(rows) - len(valid),
    }


@router.post("/import")
def import_users(body: ImportIn, actor: CurrentManager):
    if body.mode == "invite":
        require_mail()
    result = _analyse(body, actor)
    result["dry_run"] = body.dry_run
    result["created"] = []
    valid = [r for r in result["rows"] if not r["errors"]]
    if body.dry_run or not valid:
        return result

    created_at = iso(now())
    passwords_by_line: dict[int, str] = {}
    to_create = []
    for r in valid:
        if body.mode == "password":
            pw = passwords.generate()
            passwords_by_line[r["line"]] = pw
            password_hash, must_change = hash_password(pw), True
        else:
            password_hash, must_change = db.UNUSABLE_PASSWORD, False
        to_create.append({
            "username": r["username"], "password_hash": password_hash, "created_at": created_at,
            "structure_id": r["structure"]["id"], "structure_role": r["role"],
            "first_name": r["first_name"], "last_name": r["last_name"], "email": r["email"], "phone": r["phone"],
            "must_change_password": must_change,
        })
    try:
        ids = db.create_users_bulk(to_create)
    except Exception as e:  # sqlite3.IntegrityError : compte créé entre l'analyse et l'import
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Import annulé, aucun compte créé : un identifiant ou une adresse e-mail vient d'être pris ({e}). Relancez l'analyse.",
        )

    created = []
    for r, user_id in zip(valid, ids):
        item = {"line": r["line"], "id": user_id, "username": r["username"], "first_name": r["first_name"],
                "last_name": r["last_name"], "email": r["email"], "structure": r["structure"]["name"], "role": r["role"]}
        if body.mode == "password":
            item["password"] = passwords_by_line[r["line"]]
        created.append(item)

    if body.mode == "invite":
        inviter = display_name(actor)
        messages = [accounts.invite_message(db.get_user(c["id"]), accounts.issue_token(c["id"], "invite"), inviter)
                    for c in created]
        try:
            errors = mailer.send_many(messages)
        except mailer.MailError as e:
            errors = [str(e)] * len(created)
        for c, err in zip(created, errors):
            c["invitation_error"] = err

    result["created"] = created
    return result
