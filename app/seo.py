"""
Référencement (moteurs de recherche) : robots.txt, plan du site, balises des pages publiques, pages par port.

- `/robots.txt` : tout est ouvert aux robots sauf /api/ ; renvoie au plan du site.
- `/sitemap.xml` : pages publiques et une page par port (adresses absolues d'après APP_BASE_URL).
- Pages publiques servies avec leurs balises « absolues » ajoutées (PAGES) : lien canonique, Open Graph (titre et
  description repris de la page elle-même, image de partage /og-image.png), carte Twitter. Les adresses absolues
  ne peuvent pas être écrites dans les fichiers statiques : elles dépendent du domaine (`__BASE__` y est remplacé).
- `/marees` et `/marees/{port}` : horaires de marée de chaque port, rendus par le serveur (un moteur ne lit pas ce
  que le JavaScript charge après coup) : étales du jour, tableau de la semaine, liens vers la recherche.
  Données et sources comme pour un visiteur (tide_data.port_tides).
- Les pages privées (administration, créneaux choisis…) portent `noindex` dans leur fichier ; les réponses de
  /api/ portent l'en-tête `X-Robots-Tag: noindex` (security.py).

Adresse du site : APP_BASE_URL (obligatoire pour les e-mails), à défaut celle de la requête.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse

from . import db, mailer, tide_data

router = APIRouter()
STATIC = Path(__file__).resolve().parent.parent / "static"
SHARE_IMAGE = "/og-image.png"
WEEK_DAYS = 7

# Pages publiques à balises absolues : chemin demandé → (fichier statique, chemin canonique)
PAGES: dict[str, tuple[str, str]] = {
    "/": ("accueil.html", "/"),
    "/accueil.html": ("accueil.html", "/"),
    "/index.html": ("index.html", "/index.html"),
    "/hauteurs.html": ("hauteurs.html", "/hauteurs.html"),
    "/carte.html": ("carte.html", "/carte.html"),
    "/aide.html": ("aide.html", "/aide.html"),
    "/demande-structure.html": ("demande-structure.html", "/demande-structure.html"),
    "/mentions-legales.html": ("mentions-legales.html", "/mentions-legales.html"),
    "/api-docs.html": ("api-docs.html", "/api-docs.html"),
}
# Dans le plan du site (les pages « canoniques », pas leurs doublons)
SITEMAP_PAGES = ["/", "/index.html", "/hauteurs.html", "/carte.html", "/marees", "/aide.html",
                 "/demande-structure.html", "/api-docs.html", "/mentions-legales.html"]

MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
          "novembre", "décembre"]
WEEKDAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def base_url(request: Request) -> str:
    return mailer.BASE_URL or str(request.base_url).rstrip("/")


def fr_date(d: date, *, weekday: bool = True) -> str:
    return f"{WEEKDAYS[d.weekday()] + ' ' if weekday else ''}{d.day}{'er' if d.day == 1 else ''} {MONTHS[d.month - 1]}"


def fr_m(value: float) -> str:
    return f"{value:.2f}".replace(".", ",") + " m"


def slugify(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-") or "port"


def port_slugs() -> dict[str, object]:
    """slug → port, pour les ports proposés au public (déjà précalculés) ; un nom en double reçoit son numéro."""
    out: dict[str, object] = {}
    for p in db.list_ports(with_data_only=True):
        slug = slugify(p["name"])
        out[slug if slug not in out else f"{slug}-{p['id']}"] = p
    return out


# ---------------------------------------------------------------------------
# Réponses HTML avec ETag (le navigateur revalide : 304 si inchangé)
# ---------------------------------------------------------------------------

def html_response(request: Request, body: str, *, max_age: int = 0) -> Response:
    data = body.encode("utf-8")
    etag = '"' + hashlib.sha1(data).hexdigest()[:20] + '"'
    headers = {"ETag": etag, "Cache-Control": f"public, max-age={max_age}" if max_age else "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return HTMLResponse(body, headers=headers)


def head_tags(request: Request, page_html: str, canonical: str) -> str:
    """Balises absolues d'une page : canonique, Open Graph (reprend titre et description de la page), Twitter."""
    base = base_url(request)
    title = re.search(r"<title>(.*?)</title>", page_html, re.S)
    desc = re.search(r'<meta name="description" content="([^"]*)"', page_html)
    url = f"{base}{canonical}"
    tags = [f'<link rel="canonical" href="{html.escape(url)}">',
            '<meta property="og:type" content="website">',
            '<meta property="og:locale" content="fr_FR">',
            '<meta property="og:site_name" content="Calendive">',
            f'<meta property="og:url" content="{html.escape(url)}">',
            f'<meta property="og:image" content="{html.escape(base + SHARE_IMAGE)}">',
            '<meta property="og:image:width" content="1200">',
            '<meta property="og:image:height" content="630">',
            '<meta property="og:image:alt" content="Calendive : vos plongées, au rythme des marées">',
            '<meta name="twitter:card" content="summary_large_image">']
    if title:
        tags.append(f'<meta property="og:title" content="{html.escape(html.unescape(title.group(1).strip()))}">')
    if desc:
        tags.append(f'<meta property="og:description" content="{desc.group(1)}">')
    return "\n".join(tags)


def static_page(request: Request, filename: str, canonical: str) -> Response:
    """Page statique avec ses balises absolues ; `__BASE__` y est remplacé par l'adresse du site."""
    page = (STATIC / filename).read_text(encoding="utf-8")
    page = page.replace("__BASE__", base_url(request))
    page = page.replace("</head>", head_tags(request, page, canonical) + "\n</head>", 1)
    return html_response(request, page)


def _register_pages() -> None:
    for path, (filename, canonical) in PAGES.items():
        if path == "/":
            continue            # « / » : main.home (les adhérents vont à leurs créneaux choisis)

        def handler(request: Request, filename=filename, canonical=canonical):
            return static_page(request, filename, canonical)

        router.add_api_route(path, handler, methods=["GET"], include_in_schema=False)


_register_pages()


# ---------------------------------------------------------------------------
# robots.txt et plan du site
# ---------------------------------------------------------------------------

@router.get("/robots.txt", include_in_schema=False)
def robots(request: Request):
    return PlainTextResponse(f"User-agent: *\nDisallow: /api/\n\nSitemap: {base_url(request)}/sitemap.xml\n",
                             headers={"Cache-Control": "public, max-age=3600"})


@router.get("/sitemap.xml", include_in_schema=False)
def sitemap(request: Request):
    base = base_url(request)
    today = date.today().isoformat()
    urls = [f"  <url><loc>{xml_escape(base + path)}</loc></url>" for path in SITEMAP_PAGES]
    urls += [f"  <url><loc>{xml_escape(f'{base}/marees/{slug}')}</loc><lastmod>{today}</lastmod>"
             f"<changefreq>daily</changefreq></url>" for slug in port_slugs()]
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           + "\n".join(urls) + "\n</urlset>\n")
    return Response(xml, media_type="application/xml", headers={"Cache-Control": "public, max-age=3600"})


# ---------------------------------------------------------------------------
# Pages « Horaires de marée »
# ---------------------------------------------------------------------------

def _tides(port, days: int) -> tuple[dict, date]:
    """Marées du port pour `days` jours à partir d'aujourd'hui (heure locale du port), comme pour un visiteur."""
    today = datetime.now(ZoneInfo(port["timezone"])).date()
    data = tide_data.port_tides(port_id=port["id"], start=today, end=today + timedelta(days=days - 1),
                                kind="both", user=None)
    return data, today


def _by_day(tides: list[dict]) -> dict[str, list[dict]]:
    days: dict[str, list[dict]] = {}
    for t in tides:
        days.setdefault(t["date"], []).append(t)
    return days


def _tide_cards(tides: list[dict]) -> str:
    items = []
    for t in tides:
        kind = "Pleine mer" if t["kind"] == "PM" else "Basse mer"
        coef = f" · coefficient {t['coefficient']}" if t["coefficient"] is not None else ""
        items.append(f'<li class="{"pm" if t["kind"] == "PM" else "bm"}"><span class="today-kind">{kind}</span>'
                     f'<span class="today-time">{html.escape(t["time"])}</span>'
                     f'<span class="today-meta">{fr_m(t["height_m"])}{coef}</span></li>')
    return "\n".join(items)


def _week_table(port_name: str, days: dict[str, list[dict]]) -> str:
    rows = []
    for iso, tides in days.items():
        d = date.fromisoformat(iso)
        pm = [t for t in tides if t["kind"] == "PM"]
        bm = [t for t in tides if t["kind"] == "BM"]
        cell = lambda ts: "<br>".join(f'{html.escape(t["time"])} <span class="muted">{fr_m(t["height_m"])}</span>'
                                      for t in ts) or "–"
        coefs = " · ".join(str(t["coefficient"]) for t in pm if t["coefficient"] is not None) or "–"
        rows.append(f'<tr><th scope="row">{fr_date(d)}</th><td data-label="Pleines mers">{cell(pm)}</td>'
                    f'<td data-label="Basses mers">{cell(bm)}</td><td data-label="Coefficients">{coefs}</td></tr>')
    return (f'<div class="table-wrap"><table class="tide-table"><caption class="visually-hidden">Marées à '
            f'{html.escape(port_name)} : pleines mers, basses mers et coefficients des {len(days)} prochains jours'
            f'</caption><thead><tr><th scope="col">Jour</th><th scope="col">Pleines mers</th>'
            f'<th scope="col">Basses mers</th><th scope="col">Coefficients</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def _page(request: Request, *, title: str, description: str, canonical: str, body: str,
          jsonld: list[dict] | None = None) -> str:
    def ld(data: dict) -> str:
        text = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")      # jamais de </script> dans les données
        return f'<script type="application/ld+json">{text}</script>\n'

    scripts = "".join(ld(j) for j in jsonld or [])
    page = f'''<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<base href="../">
<title>{html.escape(title)}</title>
<meta name="description" content="{html.escape(description, quote=True)}">
<link rel="stylesheet" href="style.css">
<link rel="stylesheet" href="accueil.css">
<link rel="stylesheet" href="marees.css">
<link rel="icon" href="favicon.svg" type="image/svg+xml">
<link rel="icon" href="favicon-32.png" sizes="32x32" type="image/png">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<meta name="theme-color" content="#073b4c">
<link rel="manifest" href="manifest.webmanifest">
{scripts}</head>
<body class="home marees">
  <header>
    <div class="brand">
      <a class="logo" href="./" title="Calendive — accueil">
        <img src="logo-sombre.svg" width="44" height="44" alt="">
        <span class="wordmark">calen<span>dive</span></span>
      </a>
    </div>
    <nav class="account" id="account" aria-label="Compte"></nav>
  </header>
  <main>
{body}
  </main>
  <footer>
    <p><a href="./">Accueil</a> · <a href="marees">Horaires de marée par port</a> · <a href="index.html">Recherche de créneaux</a> · <a href="carte.html">Carte des ports</a> · <a href="aide.html">Aide</a> · <a href="mentions-legales.html">Mentions légales et confidentialité</a></p>
  </footer>
  <script src="session.js"></script>
  <script src="marees.js"></script>
</body>
</html>
'''
    return page.replace("</head>", head_tags(request, page, canonical) + "\n</head>", 1)


def _breadcrumb(base: str, trail: list[tuple[str, str]]) -> dict:
    return {"@context": "https://schema.org", "@type": "BreadcrumbList",
            "itemListElement": [{"@type": "ListItem", "position": i + 1, "name": name, "item": base + path}
                                for i, (name, path) in enumerate(trail)]}


def _port_links(slugs: dict[str, object], current: str | None = None) -> str:
    return "".join(f'<li><a href="marees/{slug}">{html.escape(p["name"])}</a></li>'
                   for slug, p in slugs.items() if slug != current)


@router.get("/marees", include_in_schema=False)
def tides_index(request: Request):
    slugs = port_slugs()
    base = base_url(request)
    cards = []
    for slug, port in slugs.items():
        try:
            data, today = _tides(port, 1)
            today_tides = data["tides"]
        except HTTPException:
            today_tides = []
        line = " · ".join(f'{"PM" if t["kind"] == "PM" else "BM"} {html.escape(t["time"])}' for t in today_tides)
        text = f"Aujourd'hui : {line}" if line else "Horaires de marée, hauteurs et coefficients."
        cards.append(f'<article class="card"><h2><a href="marees/{slug}">{html.escape(port["name"])}</a></h2>'
                     f'<p>{text}</p></article>')
    body = f'''    <section class="block">
      <h1>Horaires de marée par port</h1>
      <p class="lead-dark">Pleines mers, basses mers, hauteurs d'eau et coefficients pour aujourd'hui et la semaine, port par port. Horaires indicatifs, à vérifier auprès du Shom avant de plonger.</p>
      <div class="cards">{"".join(cards) or "<p>Aucun port pour l'instant.</p>"}</div>
      <p class="more"><a class="btn-primary" href="index.html">Chercher des créneaux de plongée</a></p>
    </section>'''
    page = _page(request, title="Horaires de marée par port — Calendive",
                 description="Horaires de marée de chaque port suivi par Calendive : pleines mers, basses mers, hauteurs "
                             "d'eau et coefficients du jour et de la semaine.",
                 canonical="/marees", body=body,
                 jsonld=[_breadcrumb(base, [("Accueil", "/"), ("Horaires de marée", "/marees")])])
    return html_response(request, page, max_age=600)


@router.get("/marees/{slug}", include_in_schema=False)
def port_tides_page(slug: str, request: Request):
    slugs = port_slugs()
    port = slugs.get(slug)
    if port is None:
        body = (f'    <section class="block"><h1>Port inconnu</h1><p class="lead-dark">Aucun horaire de marée pour '
                f'« {html.escape(slug)} ». Choisissez un port :</p><ul class="port-links">{_port_links(slugs)}</ul>'
                f'</section>')
        page = _page(request, title="Port inconnu — Calendive", description="Port inconnu : choisissez un port.",
                     canonical=f"/marees/{slug}", body=body)
        return HTMLResponse(page.replace("</head>", '<meta name="robots" content="noindex">\n</head>', 1),
                            status_code=404)
    base = base_url(request)
    name = html.escape(port["name"])
    data, today = _tides(port, WEEK_DAYS)
    days = _by_day(data["tides"])
    today_tides = days.get(today.isoformat(), [])
    sources = ", ".join(s["label"] for s in data["port"]["sources"])
    first_pm = next((t for t in today_tides if t["kind"] == "PM"), None)
    summary = " ; ".join(f'{"PM" if t["kind"] == "PM" else "BM"} {t["time"]}' for t in today_tides)
    description = (f"Horaires de marée à {port['name']} le {fr_date(today)} : {summary}. " if summary else
                   f"Horaires de marée à {port['name']}. ") + \
        "Pleines mers, basses mers, hauteurs d'eau et coefficients de la semaine."
    today_block = (f'<ul class="today-list">{_tide_cards(today_tides)}</ul>' if today_tides else
                   '<p class="today-note">Pas d\'étale enregistrée aujourd\'hui pour ce port.</p>')
    coef_note = (f" Le coefficient du jour est de {first_pm['coefficient']}." if first_pm and
                 first_pm["coefficient"] is not None else "")
    body = f'''    <section class="block">
      <p class="crumbs"><a href="./">Accueil</a> › <a href="marees">Horaires de marée</a> › {name}</p>
      <h1>Horaires de marée à {name}</h1>
      <p class="lead-dark">Pleines mers, basses mers, hauteurs d'eau et coefficients à {name} pour aujourd'hui ({html.escape(fr_date(today))}) et les six jours suivants. Heures locales ; hauteurs au-dessus du zéro des cartes.{coef_note}</p>
    </section>
    <section class="today" aria-labelledby="t-today">
      <div class="today-head"><h2 id="t-today">Aujourd'hui à {name}</h2></div>
      {today_block}
    </section>
    <section class="block" aria-labelledby="t-week">
      <h2 id="t-week">Les marées de la semaine</h2>
      {_week_table(port["name"], days)}
      <p class="today-note">Source des horaires : {html.escape(sources)}. Ils restent <strong>indicatifs</strong> : vérifiez-les auprès du <a href="https://maree.shom.fr" rel="noopener">Shom</a> avant de plonger ou de prendre la mer.</p>
    </section>
    <section class="block cta-block">
      <h2>Plonger à {name}</h2>
      <p>Calendive repère les créneaux autour de l'étale où la marée et la lumière du jour sont au rendez-vous à {name}, selon le coefficient et votre marge de sécurité. Les clubs y organisent leur saison : sorties, inscriptions, rappels.</p>
      <p class="more"><a class="btn-primary" href="index.html?port={port['id']}">Chercher des créneaux à {name}</a>
        <a class="btn-secondary" href="demande-structure.html">Créer l'espace de mon club</a></p>
    </section>
    <section class="block" aria-labelledby="t-others">
      <h2 id="t-others">Autres ports</h2>
      <ul class="port-links">{_port_links(slugs, slug)}</ul>
    </section>'''
    place = {"@context": "https://schema.org", "@type": "Place", "name": port["name"],
             "geo": {"@type": "GeoCoordinates", "latitude": port["latitude"], "longitude": port["longitude"]},
             "url": f"{base}/marees/{slug}"}
    page = _page(request, title=f"Horaires de marée à {port['name']} aujourd'hui et cette semaine — Calendive",
                 description=description, canonical=f"/marees/{slug}", body=body,
                 jsonld=[place, _breadcrumb(base, [("Accueil", "/"), ("Horaires de marée", "/marees"),
                                                   (port["name"], f"/marees/{slug}")])])
    return html_response(request, page, max_age=300)
