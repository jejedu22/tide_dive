"""
Page vitrine publique d'une structure (« site builder ») : une page composée de blocs, rendue par le serveur à
l'adresse /club/<adresse>.

- Édition (page /vitrine.html) : administrateurs de la structure, ou profil « Vitrine » (accounts.PROFILES).
  Brouillon enregistré à part : rien n'est visible avant « Publier » ; aperçu du brouillon réservé aux éditeurs
  (/club/<adresse>?apercu=1).
- Blocs (validés ici, jamais de HTML saisi) : bandeau d'accueil, texte (mise en forme simple, comme les newsletters :
  **gras**, *italique*, [lien](https://…), listes « - », titres « ## »), galerie, prochaines plongées (créneaux de la
  structure, mis à jour tout seuls), activités et tarifs, nous rejoindre (lien d'adhésion), contact et accès, bloc
  personnalisé (texte, photo placée au choix, boutons), cartes en colonnes, questions fréquentes, chiffres clés.
- Menu : chaque bloc peut y figurer (libellé facultatif) ; ancres calculées par anchors(). Fond au choix par bloc.
- Couleurs et logo : ceux de la structure (Ma structure). Photos : PNG, JPEG ou WebP, 1 Mo et 12 photos au plus,
  servies par une clé aléatoire (/api/sites/images/<clé>).

Routes : GET / PUT /api/site, POST / DELETE /api/site/publish, GET / POST /api/site/images,
DELETE /api/site/images/{clé} ; publiques : GET /club/{adresse}, GET /api/sites/images/{clé}.
"""

from __future__ import annotations

import html
import json
import re
import secrets
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal, Union

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import accounts, db, theme
from .auth import CurrentUser, optional_user
from .newsletter_render import _BOLD, _ITALIC, _ITEM, _LINK
from .structure_profile import card, logo_type

router = APIRouter()

MAX_IMAGES = 12
IMAGE_MAX = 1024 * 1024
MAX_BLOCKS = 30
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,48}[a-z0-9])$")
# lien externe : https, un vrai nom de domaine (avec un point), puis un chemin facultatif sans guillemets ni chevrons
URL_RE = re.compile(r"^https://[a-z0-9-]+(?:\.[a-z0-9-]+)+(?::\d+)?(?:[/?#][^\s\"'<>]*)?$", re.IGNORECASE)


def _base(request: Request) -> str:
    from .seo import base_url
    return base_url(request)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Blocs
# ---------------------------------------------------------------------------

def _text(v: str | None) -> str | None:
    v = (v or "").strip()
    return v or None


ANCHOR_RE = re.compile(r"^#[a-z0-9](?:[a-z0-9-]{0,58}[a-z0-9])?$")
EMAIL_RE = re.compile(r"^[^@\s\"'<>]+@[^@\s\"'<>]+\.[a-z]{2,}$", re.IGNORECASE)


def clean_link(v: str | None) -> str | None:
    """Cible d'un bouton : « join » (lien d'adhésion), « guest » (inscription des personnes extérieures), « #ancre »
    (une section de la page), une adresse https, « mailto:… » ou « tel:… »."""
    v = _text(v)
    if v is None or v in ("join", "guest") or ANCHOR_RE.match(v):
        return v
    low = v.lower()
    if low.startswith("mailto:"):
        if EMAIL_RE.match(v[7:]):
            return "mailto:" + v[7:]
        raise ValueError("adresse e-mail invalide")
    if low.startswith("tel:"):
        if re.fullmatch(r"\+?[0-9 .()-]{4,25}", v[4:]):
            return "tel:" + re.sub(r"[^0-9+]", "", v[4:])
        raise ValueError("numéro de téléphone invalide")
    if EMAIL_RE.match(v):
        return "mailto:" + v
    if low.startswith("http://"):
        v = "https://" + v[7:]
    elif "://" not in v:
        v = "https://" + v
    if len(v) > 300 or not URL_RE.match(v):
        raise ValueError("lien invalide : adresse https, e-mail, téléphone ou section de la page")
    return v


class _Block(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hidden: bool = False
    menu: str | None = Field(None, max_length=30)          # libellé dans le menu de la page (facultatif)
    style: Literal["card", "plain", "tinted", "primary"] = "card"   # fond de la section

    @field_validator("menu")
    @classmethod
    def _menu(cls, v: str | None) -> str | None:
        return _text(v)


class Button(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=40)
    link: str = Field(max_length=300)

    @field_validator("link")
    @classmethod
    def _link(cls, v: str) -> str:
        v = clean_link(v)
        if v is None:
            raise ValueError("lien manquant")
        return v


class Hero(_Block):
    type: Literal["hero"]
    title: str = Field(min_length=1, max_length=80)
    subtitle: str | None = Field(None, max_length=200)
    image: str | None = Field(None, max_length=40)
    button: Literal["none", "join", "dives", "contact"] = "none"
    button_label: str | None = Field(None, max_length=40)


class Text(_Block):
    type: Literal["text"]
    title: str | None = Field(None, max_length=80)
    body: str = Field(min_length=1, max_length=5000)


class Photo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image: str = Field(max_length=40)
    caption: str | None = Field(None, max_length=120)


class Gallery(_Block):
    type: Literal["gallery"]
    title: str | None = Field(None, max_length=80)
    photos: list[Photo] = Field(min_length=1, max_length=MAX_IMAGES)


class Dives(_Block):
    type: Literal["dives"]
    title: str | None = Field(None, max_length=80)
    days: int = Field(60, ge=7, le=180)
    max_items: int = Field(8, ge=1, le=30)
    guest_link: bool = True


class Price(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=80)
    price: str | None = Field(None, max_length=30)
    detail: str | None = Field(None, max_length=200)


class Pricing(_Block):
    type: Literal["pricing"]
    title: str | None = Field(None, max_length=80)
    items: list[Price] = Field(min_length=1, max_length=30)


class Join(_Block):
    type: Literal["join"]
    title: str | None = Field(None, max_length=80)
    text: str | None = Field(None, max_length=500)


class Links(BaseModel):
    model_config = ConfigDict(extra="forbid")
    facebook: str | None = None
    instagram: str | None = None
    youtube: str | None = None
    website: str | None = None

    @field_validator("facebook", "instagram", "youtube", "website")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        v = _text(v)
        if v is None:
            return None
        if v.lower().startswith("http://"):
            v = "https://" + v[7:]
        elif "://" not in v and ":" not in v.split("/")[0]:
            v = "https://" + v
        if len(v) > 300 or not URL_RE.match(v):
            raise ValueError("adresse invalide")
        return v


class Contact(_Block):
    type: Literal["contact"]
    title: str | None = Field(None, max_length=80)
    text: str | None = Field(None, max_length=500)
    show_map: bool = True
    links: Links = Field(default_factory=Links)


class Custom(_Block):
    """Bloc personnalisé : titre, texte, photo (à gauche, à droite, au-dessus ou en fond) et jusqu'à deux boutons."""
    type: Literal["custom"]
    title: str | None = Field(None, max_length=80)
    body: str | None = Field(None, max_length=5000)
    image: str | None = Field(None, max_length=40)
    image_position: Literal["left", "right", "top", "background"] = "right"
    align: Literal["left", "center"] = "left"
    buttons: list[Button] = Field(default_factory=list, max_length=2)


class Card(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=80)
    text: str | None = Field(None, max_length=800)
    image: str | None = Field(None, max_length=40)
    link_label: str | None = Field(None, max_length=40)
    link: str | None = Field(None, max_length=300)

    @field_validator("link")
    @classmethod
    def _link(cls, v: str | None) -> str | None:
        return clean_link(v)


class Cards(_Block):
    """Cartes en colonnes : formations, activités, équipe, bateau…"""
    type: Literal["cards"]
    title: str | None = Field(None, max_length=80)
    intro: str | None = Field(None, max_length=1000)
    columns: int = Field(3, ge=2, le=4)
    items: list[Card] = Field(min_length=1, max_length=12)


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=200)
    answer: str = Field(min_length=1, max_length=2000)


class Faq(_Block):
    type: Literal["faq"]
    title: str | None = Field(None, max_length=80)
    items: list[Question] = Field(min_length=1, max_length=30)


class Figure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=1, max_length=20)
    label: str = Field(min_length=1, max_length=60)


class Stats(_Block):
    type: Literal["stats"]
    title: str | None = Field(None, max_length=80)
    items: list[Figure] = Field(min_length=1, max_length=6)


Block = Annotated[Union[Hero, Text, Gallery, Dives, Pricing, Join, Contact, Custom, Cards, Faq, Stats],
                  Field(discriminator="type")]


class SiteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(max_length=50)
    title: str | None = Field(None, max_length=80)          # titre de la page (défaut : nom de la structure)
    description: str | None = Field(None, max_length=300)   # description pour les moteurs de recherche
    blocks: list[Block] = Field(max_length=MAX_BLOCKS)

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        v = v.strip().lower()
        if not SLUG_RE.match(v):
            raise ValueError("adresse invalide : 3 à 50 caractères, lettres minuscules, chiffres et tirets")
        return v


def _images_in(blocks: list) -> set[str]:
    keys = set()
    for b in blocks:
        if b.get("type") in ("hero", "custom") and b.get("image"):
            keys.add(b["image"])
        if b.get("type") == "gallery":
            keys.update(p["image"] for p in b.get("photos", []))
        if b.get("type") == "cards":
            keys.update(c["image"] for c in b.get("items", []) if c.get("image"))
    return keys


def anchors(blocks: list) -> list[str | None]:
    """Ancre (id HTML) de chaque bloc : « plongees », « contact », sinon d'après son libellé de menu ; dédoublonnées
    (même calcul dans l'éditeur, vitrine.js, pour proposer les sections comme cibles de boutons)."""
    used: set[str] = set()
    out: list[str | None] = []
    for b in blocks:
        base = {"dives": "plongees", "contact": "contact"}.get(b.get("type"))
        if base is None and b.get("menu"):
            text = unicodedata.normalize("NFKD", b["menu"]).encode("ascii", "ignore").decode().lower()
            base = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:50].strip("-") or "section"
        if base is None:
            out.append(None)
            continue
        anchor, n = base, 2
        while anchor in used:
            anchor, n = f"{base}-{n}", n + 1
        used.add(anchor)
        out.append(anchor)
    return out


# ---------------------------------------------------------------------------
# Édition
# ---------------------------------------------------------------------------

def _editor_scope(user, structure_id: int | None) -> int:
    """Structure dont on édite la vitrine : la structure active (administration ou profil « Vitrine ») ; un super
    administrateur peut en choisir une autre (?structure_id=)."""
    if structure_id is not None and user["is_admin"]:
        if db.get_structure(structure_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Structure inconnue")
        return structure_id
    if user["structure_id"] is None or not accounts.permissions(user)["edit_site"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Réservé aux administrateurs de la structure et au profil Vitrine")
    return user["structure_id"]


def _default_site(st: sqlite3.Row) -> dict:
    """Première version proposée : un bandeau, une présentation à compléter, les prochaines plongées, le contact."""
    from .seo import slugify
    return {
        "slug": slugify(st["name"])[:50].strip("-") or f"club-{st['id']}", "title": None, "description": None,
        "blocks": [
            {"type": "hero", "title": st["name"], "subtitle": "Plongée sous-marine", "image": None, "button": "dives",
             "button_label": "Prochaines plongées", "hidden": False},
            {"type": "text", "title": "Le club", "body": "Présentez votre club : son histoire, ses valeurs, son bateau, "
             "ses sites de plongée…", "hidden": False},
            {"type": "dives", "title": "Prochaines plongées", "days": 60, "max_items": 8, "guest_link": True,
             "hidden": False},
            {"type": "contact", "title": "Contact et accès", "text": None, "show_map": True,
             "links": {"facebook": None, "instagram": None, "youtube": None, "website": None}, "hidden": False},
        ],
    }


def _site_out(sid: int, request: Request) -> dict:
    st = db.get_structure(sid)
    row = db.get_site(sid)
    draft = json.loads(row["draft_json"]) if row else _default_site(st)
    slug = row["slug"] if row else draft["slug"]
    base = _base(request)
    return {
        "structure_id": sid, "structure_name": st["name"], "saved": row is not None, "slug": slug,
        "draft": draft, "published": row is not None and row["published_json"] is not None,
        "published_at": row["published_at"] if row else None,
        "unpublished_changes": bool(row and row["published_json"] != row["draft_json"]),
        "public_url": f"{base}/club/{slug}", "updated_at": row["updated_at"] if row else None,
        "updated_by": row["updated_by"] if row else None,
        "images": [_image_out(i) for i in db.list_site_images(sid)],
        "has_join_link": bool(st["join_token"]), "has_guest_link": bool(st["guest_token"]),
    }


def _image_out(row) -> dict:
    return {"key": row["key"], "url": f"/api/sites/images/{row['key']}", "size": row["size"]}


@router.get("/api/site")
def get_site(request: Request, user: CurrentUser, structure_id: int | None = None):
    """La vitrine de la structure (brouillon, état de publication, photos) ; une première version si elle n'existe pas."""
    return _site_out(_editor_scope(user, structure_id), request)


@router.put("/api/site")
def save_site(body: SiteIn, request: Request, user: CurrentUser, structure_id: int | None = None):
    """Enregistre le brouillon (rien ne change sur la page publiée avant « Publier »)."""
    sid = _editor_scope(user, structure_id)
    data = body.model_dump()
    known = {i["key"] for i in db.list_site_images(sid)}
    if missing := _images_in(data["blocks"]) - known:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Photo inconnue : {sorted(missing)[0]}")
    if db.slug_taken(body.slug, sid):
        raise HTTPException(status.HTTP_409_CONFLICT, f"L'adresse « {body.slug} » est déjà prise par une autre structure")
    try:
        db.save_site_draft(sid, body.slug, json.dumps(data, ensure_ascii=False), _now(), accounts.display_name(user))
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, f"L'adresse « {body.slug} » est déjà prise par une autre structure")
    return _site_out(sid, request)


@router.post("/api/site/publish")
def publish(request: Request, user: CurrentUser, structure_id: int | None = None):
    """Publie le brouillon : il devient la page publique."""
    sid = _editor_scope(user, structure_id)
    if not db.publish_site(sid, _now()):
        raise HTTPException(status.HTTP_409_CONFLICT, "Enregistrez d'abord la vitrine")
    return _site_out(sid, request)


@router.delete("/api/site/publish")
def unpublish(request: Request, user: CurrentUser, structure_id: int | None = None):
    """Retire la page publique (le brouillon est gardé)."""
    sid = _editor_scope(user, structure_id)
    db.unpublish_site(sid)
    return _site_out(sid, request)


@router.get("/api/site/images")
def list_images(user: CurrentUser, structure_id: int | None = None):
    """Photos de la vitrine."""
    return [_image_out(i) for i in db.list_site_images(_editor_scope(user, structure_id))]


@router.post("/api/site/images", status_code=201)
async def upload_image(request: Request, user: CurrentUser, structure_id: int | None = None):
    """Ajoute une photo (corps de la requête : l'image, PNG, JPEG ou WebP, 1 Mo au plus)."""
    sid = _editor_scope(user, structure_id)
    data = await request.body()
    if len(data) > IMAGE_MAX:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Photo trop lourde : 1 Mo au plus")
    kind = logo_type(data)
    if kind is None:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Format non reconnu : PNG, JPEG ou WebP")
    if len(db.list_site_images(sid)) >= MAX_IMAGES:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{MAX_IMAGES} photos au plus : supprimez-en une d'abord")
    key = secrets.token_urlsafe(18)
    db.add_site_image(sid, key, kind, data, _now())
    return {"key": key, "url": f"/api/sites/images/{key}", "size": len(data)}


@router.delete("/api/site/images/{key}", status_code=204)
def delete_image(key: str, user: CurrentUser, structure_id: int | None = None):
    """Supprime une photo, si aucun bloc ne s'en sert plus."""
    sid = _editor_scope(user, structure_id)
    row = db.get_site(sid)
    used = set()
    for raw in (row["draft_json"], row["published_json"]) if row else ():
        if raw:
            used |= _images_in(json.loads(raw)["blocks"])
    if key in used:
        raise HTTPException(status.HTTP_409_CONFLICT, "Cette photo sert encore dans la vitrine (brouillon ou page "
                                                      "publiée) : retirez-la des blocs d'abord")
    if not db.delete_site_image(sid, key):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Photo inconnue")


@router.get("/api/sites/images/{key}", include_in_schema=False)
def serve_image(key: str):
    row = db.get_site_image(key) if len(key) <= 40 else None
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Photo inconnue")
    return Response(row["data"], media_type=row["content_type"], headers={
        "Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'"})


# ---------------------------------------------------------------------------
# Page publique
# ---------------------------------------------------------------------------

_DAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
_MONTHS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
           "novembre", "décembre")


def _fr(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{_DAYS[d.weekday()]} {d.day} {_MONTHS[d.month - 1]}"


def _e(text) -> str:
    return html.escape(str(text or ""), quote=True)


def _inline(raw: str) -> str:
    """Mise en forme en ligne sur un texte saisi : échappé d'abord, puis gras, italique, liens https."""
    links: list[str] = []

    def keep(m: re.Match) -> str:
        links.append(f'<a href="{m.group(2)}" rel="noopener">{m.group(1)}</a>')
        return f"\x00{len(links) - 1}\x00"

    out = _LINK.sub(keep, html.escape(raw, quote=True))
    out = _ITALIC.sub(r"<em>\1</em>", _BOLD.sub(r"<strong>\1</strong>", out))
    return re.sub(r"\x00(\d+)\x00", lambda m: links[int(m.group(1))], out)


def rich_text(source: str) -> str:
    out = []
    for block in [b.strip("\n") for b in re.split(r"\n\s*\n", (source or "").replace("\r", "")) if b.strip()]:
        m = re.match(r"^(#{2,3})\s+(.+)$", block)
        if m and "\n" not in block:
            out.append(f"<h3>{_inline(m.group(2))}</h3>")
            continue
        lines = block.split("\n")
        if all(_ITEM.match(line) for line in lines):
            out.append("<ul>" + "".join(f"<li>{_inline(_ITEM.match(line).group(1))}</li>" for line in lines) + "</ul>")
            continue
        out.append("<p>" + "<br>".join(_inline(line) for line in lines) + "</p>")
    return "".join(out)


def _dives(st: sqlite3.Row, block: dict, base: str) -> str:
    from .selections import _registrations_by_selection, _selection_out, _today
    today = _today()
    until = (date.fromisoformat(today) + timedelta(days=block["days"])).isoformat()
    regs = _registrations_by_selection(st["id"])
    locks = db.get_lock_days(st["id"])
    rows = []
    for row in db.list_selections(st["id"], today):
        if row["local_date"] > until or row["cancelled_at"]:
            continue
        s = _selection_out(row, regs.get(row["id"]), None, locks)
        rows.append(s)
        if len(rows) >= block["max_items"]:
            break
    if not rows:
        return '<p class="v-muted">Pas de plongée programmée pour le moment : revenez bientôt !</p>'
    items = []
    for s in rows:
        places = ""
        if s["max_registrations"] is not None:
            left = s["max_registrations"] - s["confirmed_count"]
            places = f'<span class="v-places{" v-full" if left <= 0 else ""}">' + (
                f"{left} place(s)" if left > 0 else "Complet") + "</span>"
        detail = " · ".join(x for x in (_e(s["type"]["label"]), _e(s["note"]), _e(s["port"]),
                                        _e(s["site"]["name"]) if s["site"] else "") if x)
        items.append(f'<li><span class="v-date">{_e(_fr(s["date"]))}</span> <span class="v-time">RDV {_e(s["rdv"]["time"])}</span>'
                     f'<span class="v-detail">{detail}</span>{places}</li>')
    link = ""
    if block["guest_link"] and st["guest_token"]:
        link = f'<p><a class="v-button" href="{_e(base)}/sortie.html#{_e(st["guest_token"])}">S\'inscrire à une plongée</a></p>'
    return f'<ul class="v-dives">{"".join(items)}</ul>{link}'


def _target(link: str | None, info: dict) -> str | None:
    if link == "join":
        return info["join_url"]
    if link == "guest":
        return info["guest_url"]
    return link


def _button(label: str | None, link: str | None, info: dict, cls: str = "v-button") -> str:
    target = _target(link, info)
    if not target or not label:
        return ""
    external = ' rel="noopener"' if target.startswith("https://") else ""
    return f'<a class="{cls}" href="{_e(target)}"{external}>{_e(label)}</a>'


def _img(key: str, alt: str = "", cls: str = "") -> str:
    return f'<img{f" class={cls}" if cls else ""} src="/api/sites/images/{_e(key)}" alt="{_e(alt)}" loading="lazy">'


def _block_html(st: sqlite3.Row, b: dict, base: str, info: dict, anchor: str | None = None) -> str:
    """Section d'un bloc, avec son ancre (menu, boutons) et son fond."""
    inner = _block_inner(st, b, base, info)
    if not inner:
        return ""
    if b["type"] != "hero":
        inner = inner.replace('class="v-section', f'class="v-section v-style-{b.get("style") or "card"}', 1)
    return inner.replace("<section", f'<section id="{_e(anchor)}"', 1) if anchor else inner


def _block_inner(st: sqlite3.Row, b: dict, base: str, info: dict) -> str:
    title = f'<h2>{_e(b["title"])}</h2>' if b.get("title") else ""
    kind = b["type"]
    if kind == "hero":
        bg = f' style="background-image:url(\'/api/sites/images/{_e(b["image"])}\')"' if b.get("image") else ""
        target = {"join": info["join_url"], "dives": "#plongees", "contact": "#contact"}.get(b["button"])
        button = (f'<p><a class="v-button" href="{_e(target)}">{_e(b.get("button_label") or "En savoir plus")}</a></p>'
                  if target and b["button"] != "none" else "")
        return (f'<section class="v-hero{" v-hero-image" if b.get("image") else ""}"{bg}><div class="v-hero-inner">'
                f'<h1>{_e(b["title"])}</h1>{f"<p class=v-subtitle>{_e(b.get("subtitle"))}</p>" if b.get("subtitle") else ""}'
                f'{button}</div></section>')
    if kind == "text":
        return f'<section class="v-section">{title}<div class="v-text">{rich_text(b["body"])}</div></section>'
    if kind == "gallery":
        photos = "".join(f'<figure><img src="/api/sites/images/{_e(p["image"])}" alt="{_e(p.get("caption"))}" loading="lazy">'
                         f'{f"<figcaption>{_e(p.get("caption"))}</figcaption>" if p.get("caption") else ""}</figure>'
                         for p in b["photos"])
        return f'<section class="v-section">{title}<div class="v-gallery">{photos}</div></section>'
    if kind == "dives":
        return f'<section class="v-section">{title}{_dives(st, b, base)}</section>'
    if kind == "pricing":
        items = "".join(f'<li><span class="v-label">{_e(i["label"])}</span>'
                        f'{f"<span class=v-price>{_e(i.get("price"))}</span>" if i.get("price") else ""}'
                        f'{f"<span class=v-detail>{_e(i.get("detail"))}</span>" if i.get("detail") else ""}</li>'
                        for i in b["items"])
        return f'<section class="v-section">{title}<ul class="v-pricing">{items}</ul></section>'
    if kind == "join":
        if not info["join_url"]:
            return ""
        text = f'<p>{_e(b.get("text"))}</p>' if b.get("text") else ""
        return (f'<section class="v-section v-join">{title or "<h2>Nous rejoindre</h2>"}{text}'
                f'<p><a class="v-button" href="{_e(info["join_url"])}">Demander à rejoindre le club</a></p></section>')
    if kind == "contact":
        lines = []
        if st["address"]:
            lines.append(f'<li><strong>Adresse :</strong> {_e(st["address"])}</li>')
        if st["contact_phone"]:
            lines.append(f'<li><strong>Téléphone :</strong> <a href="tel:{_e(st["contact_phone"].replace(" ", ""))}">{_e(st["contact_phone"])}</a></li>')
        if st["contact_email"]:
            lines.append(f'<li><strong>E-mail :</strong> <a href="mailto:{_e(st["contact_email"])}">{_e(st["contact_email"])}</a></li>')
        if st["website"]:
            lines.append(f'<li><strong>Site :</strong> <a href="{_e(st["website"])}" rel="noopener">{_e(st["website"])}</a></li>')
        social = [(label, b["links"].get(key)) for key, label in
                  (("facebook", "Facebook"), ("instagram", "Instagram"), ("youtube", "YouTube"), ("website", "Site web"))]
        social_html = " · ".join(f'<a href="{_e(url)}" rel="noopener">{label}</a>' for label, url in social if url)
        map_link = ""
        if b.get("show_map") and info["port"]:
            p = info["port"]
            map_link = (f'<p><a href="https://www.openstreetmap.org/?mlat={p["latitude"]:.5f}&amp;mlon={p["longitude"]:.5f}'
                        f'#map=14/{p["latitude"]:.5f}/{p["longitude"]:.5f}" rel="noopener">Voir le port d\'attache '
                        f'({_e(p["name"])}) sur la carte</a></p>')
        text = f'<p>{_e(b.get("text"))}</p>' if b.get("text") else ""
        return (f'<section class="v-section">{title}{text}<ul class="v-contact">{"".join(lines)}</ul>'
                f'{f"<p class=v-social>{social_html}</p>" if social_html else ""}{map_link}</section>')
    if kind == "custom":
        buttons = "".join(_button(x["label"], x["link"], info) for x in b.get("buttons") or [])
        text = (f'{title}{rich_text(b["body"]) if b.get("body") else ""}'
                f'{f"<p class=v-buttons>{buttons}</p>" if buttons else ""}')
        pos, image = b.get("image_position") or "right", b.get("image")
        align = " v-center" if b.get("align") == "center" else ""
        if image and pos == "background":
            return (f'<section class="v-section v-custom v-bgimage{align}" style="background-image:url(\'/api/sites/images/{_e(image)}\')">'
                    f'<div class="v-custom-text v-text">{text}</div></section>')
        if not text and not image:
            return ""
        figure = f'<div class="v-custom-image">{_img(image)}</div>' if image else ""
        layout = f" v-image-{pos}" if image else ""
        return (f'<section class="v-section v-custom{layout}{align}">{figure if pos != "right" else ""}'
                f'<div class="v-custom-text v-text">{text}</div>{figure if pos == "right" else ""}</section>')
    if kind == "cards":
        cards = []
        for c in b["items"]:
            link = _button(c.get("link_label") or "En savoir plus", c.get("link"), info, "v-card-link") if c.get("link") else ""
            cards.append(f'<article class="v-card">{_img(c["image"], c["title"]) if c.get("image") else ""}'
                         f'<div class="v-card-body"><h3>{_e(c["title"])}</h3>'
                         f'{rich_text(c["text"]) if c.get("text") else ""}{f"<p>{link}</p>" if link else ""}</div></article>')
        intro = f'<div class="v-text">{rich_text(b["intro"])}</div>' if b.get("intro") else ""
        return (f'<section class="v-section">{title}{intro}<div class="v-cards v-cols-{int(b.get("columns") or 3)}">'
                f'{"".join(cards)}</div></section>')
    if kind == "faq":
        items = "".join(f'<details><summary>{_e(q["question"])}</summary><div class="v-text">{rich_text(q["answer"])}</div></details>'
                        for q in b["items"])
        return f'<section class="v-section">{title}<div class="v-faq">{items}</div></section>'
    if kind == "stats":
        items = "".join(f'<li><strong>{_e(x["value"])}</strong><span>{_e(x["label"])}</span></li>' for x in b["items"])
        return f'<section class="v-section">{title}<ul class="v-stats">{items}</ul></section>'
    return ""


def render(st: sqlite3.Row, data: dict, base: str, *, preview: bool = False) -> str:
    colors = theme.theme_out(st["theme_primary"], st["theme_accent"]) or {
        "primary": theme.DEFAULT_PRIMARY, "accent": theme.DEFAULT_ACCENT}
    info = {
        "join_url": f"{base}/rejoindre.html#{st['join_token']}" if st["join_token"] else None,
        "port": db.get_port(st["default_port_id"]) if st["default_port_id"] else None,
        "guest_url": f"{base}/sortie.html#{st['guest_token']}" if st["guest_token"] else None,
    }
    logo = card(st)["logo_url"]
    title = data.get("title") or st["name"]
    description = data.get("description") or f"{st['name']} : club de plongée sous-marine."
    ids = anchors(data["blocks"])
    visible = [(b, a) for b, a in zip(data["blocks"], ids) if not b.get("hidden")]
    body = "".join(_block_html(st, b, base, info, a) for b, a in visible)
    menu = "".join(f'<a href="#{_e(a)}">{_e(b["menu"])}</a>' for b, a in visible if b.get("menu") and a
                   and (b["type"] != "join" or info["join_url"]))
    nav = (f'<nav class="v-nav" aria-label="Menu">{menu}</nav>'
           f'<details class="v-burger"><summary aria-label="Menu">☰ Menu</summary><nav aria-label="Menu">{menu}'
           f'<a href="{_e(base)}/">Espace membres</a></nav></details>') if menu else ""
    banner = ('<p class="v-preview">Aperçu du brouillon : cette version n\'est pas encore publiée.</p>' if preview else "")
    return f'''<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_e(title)}</title>
<meta name="description" content="{_e(description)}">
{'<meta name="robots" content="noindex">' if preview else ''}
<meta property="og:title" content="{_e(title)}">
<meta property="og:description" content="{_e(description)}">
<meta property="og:type" content="website">
<meta name="theme-color" content="{_e(colors["primary"])}">
<link rel="stylesheet" href="/vitrine-public.css">
<script src="/vitrine-public.js" defer></script>
<link rel="icon" href="{_e(logo) if logo else '/favicon.svg'}">
<style>:root{{--v-primary:{_e(colors["primary"])};--v-accent:{_e(colors["accent"])}}}</style>
</head>
<body id="top">
{banner}
<header class="v-header{' v-has-menu' if menu else ''}">
  <a class="v-brand" href="#top">{f'<img class="v-logo" src="{_e(logo)}" alt="Logo de {_e(st["name"])}">' if logo else ''}
  <span class="v-name">{_e(st["name"])}</span></a>
  {nav}
  <a class="v-login" href="{_e(base)}/">Espace membres</a>
</header>
<main>
{body}
</main>
<footer class="v-footer">
  <p>{_e(st["name"])} · <a href="{_e(base)}/mentions-legales.html">Mentions légales</a> · Site propulsé par <a href="{_e(base)}/">Calendive</a></p>
</footer>
</body>
</html>
'''


@router.get("/club/{slug}", include_in_schema=False)
def public_page(slug: str, request: Request, apercu: int = 0, user=Depends(optional_user)):
    row = db.site_by_slug(slug.lower()) if len(slug) <= 50 else None
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Page introuvable")
    st = db.get_structure(row["structure_id"])
    preview = bool(apercu)
    if preview:
        # aperçu du brouillon : les éditeurs de CETTE structure (ou un super administrateur) seulement
        allowed = user is not None and (user["is_admin"] or (
            user["structure_id"] == row["structure_id"] and accounts.permissions(user)["edit_site"]))
        if not allowed:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Page introuvable")
        data = json.loads(row["draft_json"])
    else:
        if row["published_json"] is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Page introuvable")
        data = json.loads(row["published_json"])
    base = _base(request)
    page = render(st, data, base, preview=preview)
    return Response(page, media_type="text/html; charset=utf-8",
                    headers={"Cache-Control": "no-cache" if preview else "public, max-age=300"})
