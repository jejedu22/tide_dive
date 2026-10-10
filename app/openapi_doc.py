"""
Documentation de l'API pour les outils tiers : description OpenAPI (/api/openapi.json) et page Swagger
(/api-docs.html, Swagger UI hébergé dans static/vendor/swagger-ui : aucun script chargé chez un tiers).

- Les routes sont rangées par catégorie d'après leur chemin (TAGS) : les routeurs n'ont rien à déclarer.
- Les deux façons de s'identifier (cookie de session, jeton d'API) sont décrites par auth.optional_user :
  FastAPI les attache à chaque route qui exige un compte.
- API_DOCS=0 coupe la description et la page (l'API reste utilisable).
"""

from __future__ import annotations

import os
import re

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.responses import RedirectResponse

ENABLED = os.environ.get("API_DOCS", "1").lower() not in ("0", "false", "no")
OPENAPI_URL = "/api/openapi.json"
DOCS_PAGE = "/api-docs.html"
VERSION = "1.0"

DESCRIPTION = """
API de Calendive : marées et créneaux de plongée, inscriptions, administration des structures.
C'est celle qu'utilise l'application elle-même : tout ce que fait un compte dans l'application,
un outil tiers peut le faire avec les mêmes droits.

## S'identifier

- **Jeton d'API** (outils tiers) : créé dans l'application, menu du compte → **Jetons d'API**, puis envoyé
  dans l'en-tête `Authorization: Bearer cdv_…`. Il agit avec les droits du compte (rôle et profils),
  dans la structure où il a été créé. Portée **lecture seule** (GET uniquement) ou **lecture et écriture**.
  Ce qui touche au compte lui-même (mot de passe, double authentification, sessions, jetons, export de ses
  données) et toute modification sous `/api/me` et `/api/auth` restent réservés à l'application.
- **Session** : le cookie posé par `POST /api/auth/login`. C'est lui qu'utilise cette page quand vous êtes
  connecté à l'application dans le même navigateur.

Bouton **Authorize** : collez un jeton pour essayer les routes avec « Try it out ».

```bash
curl -H "Authorization: Bearer cdv_…" https://<site>/api/selections?upcoming=true
```

## Sans compte

La liste des ports (`GET /api/ports`), leurs étales et hauteurs d'eau (`/api/ports/{port_id}/tides`,
`/api/ports/{port_id}/heights`) et les recherches par étale et par hauteur d'eau sont publiques. Identifié,
on y voit les horaires choisis par sa structure.

## Règles communes

- Dates `AAAA-MM-JJ` et heures `HH:MM` à l'heure locale de la structure ; instants (`*_utc`, `*_at`) en ISO 8601.
- Erreurs : `{"detail": "message en français"}` (ou la liste des champs invalides pour un 422).
  `401` : non identifié ou jeton invalide ; `403` : droits insuffisants ; `404` : introuvable ou hors de
  votre structure ; `409` : action impossible dans l'état actuel ; `429` : trop de requêtes (en-tête `Retry-After`).
- Au plus 120 requêtes par minute et par jeton (réglage `API_TOKEN_RATE_PER_MIN`).
- Requêtes de modification depuis un navigateur : seule l'origine de l'application est acceptée
  (un outil serveur, qui n'envoie pas d'en-tête `Origin`, n'est pas concerné).
"""

# Route publique qui reconnaît aussi un compte connecté (résultats propres à sa structure) : authentification
# facultative dans la description (`@router.get(..., openapi_extra=PUBLIC)`)
PUBLIC = {"x-public": True}

# (catégorie, description, motifs de chemin) : la première qui correspond l'emporte
TAGS: list[tuple[str, str, tuple[str, ...]]] = [
    ("Jetons d'API", "Jetons du compte pour les outils tiers (gérés depuis l'application uniquement).",
     (r"^/api/me/api-tokens",)),
    ("Connexion", "Connexion, déconnexion, mot de passe oublié, compte connecté.", (r"^/api/auth/",)),
    ("Agenda", "Créneaux au format iCalendar (.ics) et abonnements d'agenda.",
     (r"^/api/selections.*\.ics$", r"^/api/me/calendar-feeds")),
    ("Mon compte", "Profil, préférences, notifications, carnet de plongées, invitations du compte connecté.",
     (r"^/api/me/", r"^/api/push/")),
    ("Marées et recherche", "Ports, calendrier, fenêtres de plongée autour des étales, hauteurs d'eau, météo.",
     (r"^/api/ports", r"^/api/calendar-days", r"^/api/dive-windows", r"^/api/water-", r"^/api/weather/")),
    ("Créneaux et inscriptions", "Types de créneaux, créneaux choisis par la structure, inscriptions, "
     "file d'attente, présence, indisponibilités.",
     (r"^/api/selections", r"^/api/slot-types", r"^/api/unavailabilities")),
    ("Sites et courants", "Sites de plongée de la structure et courants de marée (atlas du SHOM).",
     (r"^/api/dive-sites", r"^/api/admin/dive-sites", r"^/api/admin/currents")),
    ("Plongeurs", "Fiches plongeurs : niveaux, licence, certificat médical (CACI).", (r"^/api/divers",)),
    ("Communication", "Newsletters, Mailjet, annonces, messages groupés.",
     (r"^/api/newsletters", r"^/api/mailjet", r"^/api/admin/mailjet", r"^/api/announcements",
      r"^/api/admin/announcements", r"^/api/admin/broadcast")),
    ("Structures", "Fiche publique d'une structure, adhésion par lien, demandes de création.",
     (r"^/api/structures/", r"^/api/join/", r"^/api/structure-requests")),
    ("Administration de la structure", "Comptes, types de créneaux, indisponibilités, seuils de hauteur "
     "d'eau, tableau de bord, statistiques, journal d'activité (administrateurs de structure).",
     (r"^/api/admin/users", r"^/api/admin/slot-types", r"^/api/admin/unavailabilities",
      r"^/api/admin/water-thresholds", r"^/api/admin/structure-(dashboard|stats|invitations)",
      r"^/api/admin/join-requests", r"^/api/admin/audit", r"^/api/admin/profiles")),
    ("Super administration", "Ports et précalculs, structures, comptes, tâches, sauvegardes, santé, "
     "qualité des données, exploitation (super administrateurs, inaccessibles par jeton d'API).",
     (r"^/api/admin/",)),
    ("Divers", "Autres routes.", (r"^/",)),
]
_COMPILED = [(name, [re.compile(p) for p in patterns]) for name, _, patterns in TAGS]


def tag_for(path: str) -> str:
    return next(name for name, patterns in _COMPILED if any(p.search(path) for p in patterns))


# Routes sans libellé du journal ni description
SUMMARIES = {
    ("POST", "/api/auth/login"): "Connexion (pose le cookie de session)",
    ("GET", "/api/auth/me"): "Compte connecté : profil, structure, rôle, droits",
    ("POST", "/api/auth/forgot-password"): "Mot de passe oublié : envoi d'un lien",
    ("POST", "/api/auth/token-info"): "Validité d'un lien d'invitation ou de réinitialisation",
    ("POST", "/api/auth/reset-password"): "Nouveau mot de passe depuis un lien",
    ("GET", "/api/me/preferences"): "Critères de recherche enregistrés",
    ("PUT", "/api/me/preferences"): "Enregistrer les critères de recherche",
    ("DELETE", "/api/me/preferences"): "Oublier les critères de recherche",
    ("PUT", "/api/me/water-preferences"): "Enregistrer les préférences de hauteur d'eau",
    ("DELETE", "/api/me/calendar-feeds/{structure_id}"): "Supprimer le lien d'abonnement d'agenda",
    ("GET", "/api/me/invitations"): "Invitations à rejoindre une structure",
    ("GET", "/api/me/newsletters"): "Abonnements aux newsletters",
    ("PUT", "/api/me/newsletters"): "Modifier les abonnements aux newsletters",
    ("GET", "/api/me/notifications"): "Préférences de notification (e-mails, push)",
    ("PUT", "/api/me/notifications"): "Modifier les préférences de notification",
    ("GET", "/api/me/logbook"): "Carnet de plongées",
    ("GET", "/api/push/key"): "Clé publique des notifications push (VAPID)",
    ("POST", "/api/me/push"): "Abonner cet appareil aux notifications push",
    ("DELETE", "/api/me/push"): "Désabonner cet appareil",
    ("POST", "/api/me/push/test"): "Notification push d'essai",
    ("GET", "/api/me/totp"): "État de la double authentification",
    ("GET", "/api/me/sessions"): "Sessions ouvertes du compte",
    ("GET", "/api/ports"): "Ports disponibles et leurs années calculées",
    ("GET", "/api/dive-windows"): "Fenêtres de plongée autour des étales d'un port",
    ("GET", "/api/admin/users"): "Comptes de la structure",
    ("GET", "/api/admin/slot-types"): "Types de créneaux de la structure",
    ("GET", "/api/admin/water-thresholds"): "Seuils de hauteur d'eau de la structure",
    ("GET", "/api/admin/structure-invitations"): "Invitations envoyées par la structure",
    ("GET", "/api/admin/structure-dashboard"): "Tableau de bord de la structure",
    ("GET", "/api/admin/join-requests"): "Demandes d'adhésion en attente",
    ("GET", "/api/admin/mailjet"): "Connexion Mailjet de la structure",
    ("GET", "/api/newsletters"): "Newsletters de la structure",
    ("GET", "/api/newsletters/unsubscribes"): "Désinscriptions des newsletters",
    ("GET", "/api/newsletters/groups"): "Groupes d'envoi",
    ("POST", "/api/newsletters/groups"): "Créer un groupe d'envoi",
    ("PUT", "/api/newsletters/groups/{group_id}"): "Modifier un groupe d'envoi",
    ("GET", "/api/newsletters/{newsletter_id}"): "Une newsletter",
    ("POST", "/api/newsletters/{newsletter_id}/duplicate"): "Dupliquer une newsletter",
    ("GET", "/api/newsletters/{newsletter_id}/report"): "Rapport d'envoi d'une newsletter",
    ("GET", "/api/newsletters/unsubscribe/{token}"): "Désinscription d'une newsletter (lien d'e-mail)",
    ("GET", "/api/structures/{structure_id}/logo"): "Logo d'une structure",
    ("GET", "/api/join/{token}"): "Structure d'un lien d'adhésion",
    ("POST", "/api/join/{token}"): "Demander à rejoindre une structure par son lien",
    ("GET", "/api/admin/structures/{structure_id}/profile"): "Fiche d'une structure",
    ("GET", "/api/admin/status"): "État des données de marée",
    ("GET", "/api/admin/ports"): "Ports, années calculées, recalages",
    ("GET", "/api/admin/jobs"): "Tâches de fond récentes",
    ("GET", "/api/admin/jobs/{job_id}"): "Une tâche et son journal",
    ("GET", "/api/admin/structure-requests"): "Demandes de création de structure",
    ("GET", "/api/admin/dashboard"): "Tableau de bord général",
    ("GET", "/api/admin/accounts/search"): "Recherche de comptes",
    ("GET", "/api/admin/announcements"): "Bandeaux d'annonce",
    ("GET", "/api/admin/backups"): "Sauvegardes de la base",
    ("GET", "/api/admin/backups/{name}"): "Télécharger une sauvegarde",
    ("GET", "/api/admin/schedule"): "Tâches programmées",
    ("GET", "/api/admin/maintenance"): "Mode maintenance",
    ("GET", "/api/admin/mail-log"): "Suivi des e-mails de service",
    ("GET", "/api/admin/quality"): "Qualité des données de marée",
}


def _summary(method: str, path: str, op: dict) -> str | None:
    """Résumé en français : libellé du journal d'activité, sinon 1re phrase de la description de la route
    (sinon aucun : Swagger affiche la méthode et le chemin, plutôt qu'un nom de fonction en anglais)."""
    from .audit import LABELS
    label = LABELS.get((method.upper(), path)) or SUMMARIES.get((method.upper(), path))
    if label:
        return label
    text = (op.get("description") or "").strip()
    if not text:
        return None
    first = re.split(r"(?<=[.!?:;])\s|\n", text, maxsplit=1)[0].rstrip(".:; ")
    return first if len(first) <= 90 else first[:87].rstrip() + "…"


def build(app: FastAPI) -> dict:
    spec = get_openapi(title=app.title, version=VERSION, description=DESCRIPTION, routes=app.routes)
    used = set()
    for path, operations in spec.get("paths", {}).items():
        for method, op in operations.items():
            if isinstance(op, dict):
                op["tags"] = [tag_for(path)]
                used.add(op["tags"][0])
                if op.pop("x-public", False):
                    op["security"] = [{}, *op.get("security", [])]   # {} : sans identification
                summary = _summary(method, path, op)
                if summary:
                    op["summary"] = summary
                else:
                    op.pop("summary", None)
    spec["tags"] = [{"name": n, "description": d} for n, d, _ in TAGS if n in used]
    return spec


def install(app: FastAPI) -> None:
    if not ENABLED:
        return

    def openapi() -> dict:
        if app.openapi_schema is None:
            app.openapi_schema = build(app)
        return app.openapi_schema

    app.openapi = openapi

    @app.get("/docs", include_in_schema=False)
    def docs_redirect():
        return RedirectResponse(DOCS_PAGE)
