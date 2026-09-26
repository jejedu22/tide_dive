# Marée — aide au choix de créneaux de plongée

Application web qui croise, pour un port donné, les **marées** (horaires, hauteurs, coefficients) et la **lumière du jour** (crépuscule nautique, lever/coucher civil) pour proposer des créneaux de plongée autour de l'étale, selon des critères réglables.

Tout est **précalculé une fois par an** et stocké dans une base SQLite locale : l'API ne fait aucun calcul de marée ni d'astronomie à la volée, et ne dépend d'aucun service externe à l'exécution.

- Marées : [pyTMD](https://pytmd.readthedocs.io/) + modèle harmonique global **FES2014 / FES2022** (CNES/LEGOS/Noveltis, via AVISO+)
- Soleil : [astral](https://astral.readthedocs.io/), calcul local (crépuscule nautique = soleil à 12° sous l'horizon)
- API : FastAPI · Front : HTML/CSS/JS sans étape de build · Base : SQLite

## Sommaire

- [Fonctionnement](#fonctionnement)
- [Démarrage rapide avec Docker](#démarrage-rapide-avec-docker)
- [Installation sans Docker](#installation-sans-docker)
- [Précalcul](#précalcul)
- [Ports et zéro des cartes](#ports-et-zéro-des-cartes)
- [Administration des données](#administration-des-données)
- [Comptes et préférences](#comptes-et-préférences)
- [API](#api)
- [Précision et limites](#précision-et-limites)
- [Structure du projet](#structure-du-projet)
- [Pistes](#pistes)

## Fonctionnement

```
AVISO+ (FES NetCDF) ──► precompute.py ──► data/plongee.db ──► FastAPI ──► navigateur
                          │  pyTMD : hauteurs au pas de 10 min, PM/BM
                          │  Brest : coefficients
                          └  astral : lever/coucher, crépuscule nautique
```

Pour un port et une année, `precompute.py` :

1. calcule la hauteur d'eau toute l'année au pas de 10 min (≈ 52 000 points) ;
2. détecte les pleines mers (PM) et basses mers (BM) ;
3. attribue à chaque PM le coefficient de la PM de **Brest** la plus proche (±6 h), la série de Brest étant calculée dans le même run ;
4. calcule les horaires solaires jour par jour ;
5. remplace en base les données de **cette année uniquement**, en une transaction : les autres années sont conservées, et un échec laisse la base intacte.

L'interface permet ensuite de filtrer par période, phase de marée (PM, BM ou les deux), coefficient maximum, marge autour de l'étale et lumière requise (nautique, civile ou aucune). Chaque créneau affiche aussi une heure de rendez-vous (étale − 2 h).

## Démarrage rapide avec Docker

Prérequis : Docker + Compose, et un compte gratuit [AVISO+](https://www.aviso.altimetry.fr) ayant accès au produit FES.

```bash
cp .env.example .env
# Renseigner AVISO_USERNAME / AVISO_PASSWORD, HOST_UID / HOST_GID (sortie de `id -u` / `id -g`)

docker compose up -d --build

# Premier administrateur
docker compose run --rm --entrypoint python api -m app.auth create-admin jerome
```

Puis ouvrir <http://localhost:8000/admin.html> (port configurable via `API_PORT`) :

1. onglet **Données et tâches** : lancer le téléchargement du modèle FES (plusieurs Go la première fois) ;
2. onglet **Ports** : ajouter les ports depuis le catalogue, vérifier leur niveau moyen, puis **Calculer** l'année voulue ;
3. suivre l'avancement et le journal de chaque tâche dans **Données et tâches**.

Tout reste faisable en ligne de commande, sans passer par l'administration :

```bash
docker compose run --rm fetch-models
docker compose run --rm precompute --year 2026 --port binic
```

### Services

| Service | Rôle | Lancement |
|---|---|---|
| `api` | FastAPI + frontend statique + administration | `docker compose up -d` |
| `worker` | exécute les tâches en file, une à la fois (4 Go max) | `docker compose up -d` |
| `scheduler` | ajoute les tâches périodiques à la file via [supercronic](https://github.com/aptible/supercronic) | `docker compose up -d` |
| `fetch-models` | téléchargement FES en direct | profil `tools`, `run --rm` |
| `precompute` | précalcul en direct | profil `tools`, `run --rm` |

Le `scheduler` ne calcule rien lui-même : il ajoute des tâches que le `worker` exécute, si bien qu'elles apparaissent dans l'administration comme celles lancées à la main. `docker/crontab` :

- **1er décembre, 02:00** : mise à jour du modèle FES (seuls les fichiers plus récents sont retéléchargés) ;
- **15 décembre, 03:00** : précalcul de l'année suivante pour chaque port coché « Recalculer automatiquement chaque année » et doté d'un niveau moyen ;
- **1er de chaque mois, 04:00** (et au démarrage) : vacances scolaires.

### Variables d'environnement (`.env`)

| Variable | Défaut | Description |
|---|---|---|
| `AVISO_USERNAME`, `AVISO_PASSWORD` | — | Identifiants AVISO+ |
| `FES_MODEL` | `FES2014` | Modèle à télécharger (`FES2014` ou `FES2022`) |
| `FES_DIR` | `./models` | Dossier hôte des fichiers NetCDF |
| `API_PORT` | `8000` | Port exposé sur l'hôte |
| `HOST_UID`, `HOST_GID` | `1000` | Utilisateur propriétaire de `./data` et `./models` |

Côté application, `tide_model.py` lit `TIDE_MODEL_DIRECTORY` (fixé à `/models` dans les conteneurs) et `TIDE_MODEL_NAME` (défaut `FES2014`).

> **FES2022** : `FES_MODEL` ne pilote que le téléchargement. Pour calculer avec FES2022, ajouter aussi `TIDE_MODEL_NAME: FES2022` dans l'environnement du compose, ou passer `--model FES2022` à `precompute`.

### Derrière Traefik

Dans `docker-compose.yml`, retirer la section `ports` du service `api`, décommenter les `labels` et le réseau `traefik`, et adapter la règle `Host(...)`.

## Installation sans Docker

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Télécharger le modèle FES (le script `fetch_aviso_fes.py` est fourni par pyTMD) :
Télécharger le modèle FES (le script `fetch_aviso_fes.py` est fourni par pyTMD) :

```bash
export AVISO_USERNAME=... AVISO_PASSWORD=...
fetch_aviso_fes.py --directory /data/tide_models --tide FES2014

export AVISO_USERNAME=... AVISO_PASSWORD=...
fetch_aviso_fes.py --directory /data/tide_models --tide FES2014

export TIDE_MODEL_DIRECTORY=/data/tide_models
export TIDE_MODEL_NAME=FES2014        # ou FES2022
```

Précalculer puis lancer :

```bash
python -m app.precompute --port binic --year 2026
uvicorn app.main:app --reload
```

## Précalcul

```bash
# Port du catalogue (nom insensible à la casse)
python -m app.precompute --port "Saint-Quay-Portrieux" --year 2027

# Point personnalisé : un spot précis plutôt que le port d'attache
python -m app.precompute --name "Caffa (Erquy)" --lat 48.646 --lon -2.478 --offset-zh 6.2 --year 2027
# Point personnalisé : un spot précis plutôt que le port d'attache
python -m app.precompute --name "Caffa (Erquy)" --lat 48.646 --lon -2.478 --offset-zh 6.2 --year 2027
```

| Option | Description |
|---|---|
| `--port` | Port du catalogue (`app/ports_catalog.py`) |
| `--name`, `--lat`, `--lon` | Point personnalisé (remplace `--port`) |
| `--offset-zh` | Niveau moyen au-dessus du zéro des cartes, en m ; prioritaire sur le catalogue |
| `--year` | Année à calculer (obligatoire) |
| `--model` | Modèle pyTMD (défaut : `TIDE_MODEL_NAME` ou `FES2014`) |
| `--timezone` | Défaut `Europe/Paris` |
| `--step-minutes` | Pas de calcul, défaut 10 |

Compter plusieurs minutes par port. Relancer une année déjà présente la remplace proprement.

Le script avertit si la basse mer la plus basse passe à plus de 0,30 m sous le zéro des cartes, signe d'un `offset_zh_m` probablement trop faible.

## Ports et zéro des cartes

FES donne des hauteurs par rapport au **niveau moyen**. Pour obtenir des hauteurs comparables à l'annuaire SHOM (au-dessus du **zéro hydrographique**), chaque port porte un décalage `offset_zh_m` dans `app/ports_catalog.py`.

Valeurs actuellement renseignées : Binic (6,68 m), Saint-Quay-Portrieux (6,57 m), Erquy (6,62 m), Paimpol (6,25 m), Brest (4,32 m). Les autres ports du catalogue sont à `0` : **le précalcul refuse de les traiter** tant qu'une valeur n'est pas renseignée ou passée avec `--offset-zh`.

Source officielle : colonne « NM » des Références Altimétriques Maritimes (RAM) du Shom, sur [data.shom.fr](https://data.shom.fr) ou [data.gouv.fr](https://www.data.gouv.fr).

### Coefficients

Le coefficient (échelle 20–120) est une notion française définie à Brest. Il n'est pas fourni par pyTMD : il est estimé à partir de la hauteur de chaque PM de Brest au-dessus du niveau moyen, divisée par l'unité de hauteur (`U_BREST = 3,05 m`), **sans** offset. Chaque PM d'un autre port reçoit le coefficient de la PM de Brest la plus proche ; dans l'API, une BM reçoit celui de la PM voisine. Ces coefficients sont **indicatifs**.

## Administration des données

La page `/admin.html` (administrateurs) comporte trois onglets.

**Ports** : ajout depuis le catalogue (`app/ports_catalog.py`) ou en saisie libre, modification, suppression (avec toutes les données calculées du port), case « recalcul annuel », et bouton **Calculer** par port et par année, ou pour tous les ports annuels d'un coup. Un port sans niveau moyen au-dessus du zéro des cartes peut être enregistré mais pas calculé. La page de recherche ne propose que les ports ayant au moins une année calculée.

**Données et tâches** : état du modèle FES (taille, date de mise à jour), téléchargement / mise à jour depuis AVISO+, synchronisation des vacances scolaires, et liste des tâches avec statut, durée, journal en direct et annulation.

**Utilisateurs** : voir [Comptes et préférences](#comptes-et-préférences).

### File de tâches

L'API ne lance jamais de calcul : elle enregistre une tâche dans la table `jobs`, et le service `worker` (`python -m app.jobs worker`) les exécute **une par une** en sous-processus, en recopiant leur sortie dans le journal. Un précalcul peut donc occuper ses 4 Go sans toucher au serveur web, et deux calculs ne se marchent pas dessus. Une tâche identique déjà en attente ou en cours n'est pas dupliquée.

- Si le worker est arrêté, un bandeau le signale dans l'administration et les tâches restent en attente.
- Sans identifiants AVISO+, le téléchargement est refusé avec un message clair (le script de pyTMD attendrait sinon une saisie au clavier).
- Une tâche tuée par manque de mémoire est signalée comme telle dans son journal.
- Si le worker redémarre pendant une tâche, celle-ci est marquée en échec : il suffit de la relancer.

La base passe en mode WAL pour que l'API continue de répondre pendant qu'un précalcul écrit une année entière.

```bash
# Mettre des tâches en file depuis un terminal
python -m app.jobs enqueue fetch-models --model FES2022
python -m app.jobs enqueue precompute --port-id 3 --year 2027
python -m app.jobs enqueue precompute --auto          # tous les ports annuels, année suivante
python -m app.jobs enqueue school-holidays
```

**Mise à jour d'une installation existante** : les ports déjà en base reçoivent automatiquement leur niveau moyen depuis le catalogue quand il y est connu, et sont cochés « annuel ». La variable `MAREE_PORTS` n'est plus utilisée : c'est la case de l'administration qui décide.

## Comptes et préférences

L'application reste utilisable sans compte. Un compte permet d'**enregistrer ses préférences** : critères du formulaire (port, durée de la période, phase, coefficient max, marge, lumière) et filtres de la ligne de titre du tableau. Elles sont réappliquées à la connexion, puis une recherche est lancée automatiquement. La période est enregistrée comme une **durée** (« 13 jours à partir d'aujourd'hui »), pas comme des dates fixes.

Il n'y a pas d'inscription libre : les comptes sont créés par un administrateur sur **`/admin.html`** (création, nouveau mot de passe, droits d'administration, suppression). Un administrateur ne peut ni supprimer son propre compte ni retirer ses propres droits, et il reste toujours au moins un administrateur.

Premier administrateur, en ligne de commande :

```bash
# Docker
docker compose run --rm --entrypoint python api -m app.auth create-admin jerome
# Sans Docker
python -m app.auth create-admin jerome

# Dépannage : changer un mot de passe, lister les comptes
python -m app.auth set-password jerome
python -m app.auth list
```

Sécurité : mots de passe hachés avec scrypt (bibliothèque standard), session dans un cookie `HttpOnly` / `SameSite=Lax` dont seule l'empreinte SHA-256 est stockée en base. Changer un mot de passe ferme les sessions ouvertes du compte. **Derrière HTTPS, mettre `COOKIE_SECURE=1`.**

| Variable | Défaut | Description |
|---|---|---|
| `COOKIE_SECURE` | `0` | `1` : cookie de session envoyé uniquement en HTTPS |
| `SESSION_DAYS` | `30` | Durée de validité d'une connexion |

## API

### `GET /api/ports`

Liste des ports présents en base : `id`, `name`, `latitude`, `longitude`.

### `GET /api/dive-windows`

| Paramètre | Défaut | Valeurs |
|---|---|---|
| `port_id` | — | obligatoire |
| `start`, `end` | — | `YYYY-MM-DD`, bornes incluses |
| `max_coefficient` | `100` | 0–120 |
| `tide_phase` | `both` | `PM`, `BM`, `both` |
| `daylight` | `nautical` | `nautical`, `civil`, `none` |
| `margin_minutes` | `45` | 0–240, demi-largeur de la fenêtre autour de l'étale |

Un créneau n'est retenu que si toute la fenêtre `[étale − marge, étale + marge]` tient dans la plage de lumière demandée.

```bash
curl "http://localhost:8000/api/dive-windows?port_id=1&start=2026-10-01&end=2026-10-31&max_coefficient=70&tide_phase=PM"
```

Chaque résultat contient la date, le type d'étale, l'heure locale, la hauteur (m, zéro des cartes), le coefficient, l'heure de rendez-vous, la fenêtre de plongée et les horaires solaires du jour. La documentation interactive est disponible sur `/docs`.

### Comptes

| Méthode et route | Accès | Rôle |
|---|---|---|
| `POST /api/auth/login` | public | `{username, password}` → cookie de session |
| `POST /api/auth/logout` | public | ferme la session |
| `GET /api/auth/me` | public | `{user}` ou `{user: null}` |
| `POST /api/me/password` | connecté | `{current_password, new_password}` |
| `GET` / `PUT` / `DELETE /api/me/preferences` | connecté | `{form, filters}` |
| `GET` / `POST /api/admin/users` | admin | liste / création `{username, password, is_admin}` |
| `PATCH` / `DELETE /api/admin/users/{id}` | admin | `{password?, is_admin?}` / suppression |

### Administration des données

| Méthode et route | Rôle |
|---|---|
| `GET /api/admin/status` | modèle FES, worker, vacances scolaires |
| `GET` / `POST /api/admin/ports` | liste (avec années calculées) / création |
| `GET /api/admin/ports/catalog` | ports du catalogue pas encore en base |
| `PATCH` / `DELETE /api/admin/ports/{id}` | modification / suppression avec ses données |
| `GET` / `POST /api/admin/jobs` | liste / mise en file `{kind, params}` ; `kind` : `precompute` (`port_id`, `year`), `fetch_models` (`model`), `school_holidays` |
| `POST /api/admin/jobs/annual` | `{year}` : un précalcul par port annuel |
| `GET /api/admin/jobs/{id}` | détail avec journal |
| `POST /api/admin/jobs/{id}/cancel` | annulation |

## Précision et limites

FES est un modèle **océanique global** : il est moins précis dans les ports, baies et zones à géométrie complexe qu'un atlas régional (Ifremer/PREVIMER) ou que les constantes harmoniques du SHOM.

**Avant toute sortie réelle, vérifier les horaires contre une source officielle** : [maree.shom.fr](https://maree.shom.fr) ou [maree.info](https://maree.info). Un décalage systématique observé sur un port peut être corrigé dans `tide_model.py`.

Choix techniques à connaître :

- **Pourquoi pas une API ?** api-maree.fr limite ses horaires à une fenêtre glissante J−30 / J+30, et les API SHOM ne permettent pas de récupération multi-mois gratuite. Un précalcul annuel exige un calcul local.
- **Mémoire** : seules les 8 ondes principales (+ 2N2, requise pour l'inférence des ondes secondaires) sont chargées, sur une fenêtre de grille de ±0,5° autour du port. Charger tout FES provoque des OOM.
- **Courants FES2014 non requis** : seul le groupe « z » (hauteurs) est utilisé ; la définition pyTMD est réduite en conséquence.
- **pyTMD** : l'API bas niveau est utilisée plutôt que `tide_elevations()`, dont le comportement s'est révélé instable.

## Structure du projet

```
app/
  main.py           API FastAPI + service du frontend
  precompute.py     précalcul annuel (CLI)
  tide_model.py     hauteurs d'eau, extrema, coefficient (pyTMD)
  twilight.py       lever/coucher civil, crépuscule nautique (astral)
  ports_catalog.py  ports préréglés et leurs offset_zh_m
  db.py             schéma et accès SQLite
  auth.py           comptes, sessions, préférences, administration (+ CLI)
  admin.py          API d'administration : ports, tâches, état des données
  jobs.py           file de tâches et worker (+ CLI enqueue)
  calendar_fr.py    jours fériés et vacances scolaires
static/             frontend (index.html, app.js, style.css)
  admin.html/.js    administration des comptes
  session.js        connexion et appels API, partagé par les deux pages
docker/crontab      tâches périodiques mises en file par le scheduler
Dockerfile
docker-compose.yml
.env.example
data/plongee.db     base générée (non versionnée)
models/             fichiers FES (non versionnés, licence AVISO+)
```

Les fichiers FES sont soumis à la licence AVISO+ : ne pas les redistribuer ni les versionner.

## Pistes

- Valider les sorties sur une année complète contre maree.info / l'annuaire SHOM.
- Renseigner les `offset_zh_m` manquants depuis les RAM du Shom.
- Intégrer l'atlas régional Ifremer/PREVIMER ([accès sur demande](https://marc.ifremer.fr/produits/atlas_de_composantes_harmoniques)) : format non lu nativement par pyTMD, seul `tide_model.py` serait à adapter.
- Ajouter les courants de marée pour qualifier chaque site au-delà du coefficient.
- Mode « deux plongées dans la journée ».
Les fichiers FES sont soumis à la licence AVISO+ : ne pas les redistribuer ni les versionner.

## Pistes

- Valider les sorties sur une année complète contre maree.info / l'annuaire SHOM.
- Renseigner les `offset_zh_m` manquants depuis les RAM du Shom.
- Intégrer l'atlas régional Ifremer/PREVIMER ([accès sur demande](https://marc.ifremer.fr/produits/atlas_de_composantes_harmoniques)) : format non lu nativement par pyTMD, seul `tide_model.py` serait à adapter.
- Ajouter les courants de marée pour qualifier chaque site au-delà du coefficient.
- Mode « deux plongées dans la journée ».
- Export iCal des créneaux retenus.