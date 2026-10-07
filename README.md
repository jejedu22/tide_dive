# Calendive — aide au choix de créneaux de plongée

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
- [Recalage sur api-maree.fr](#recalage-sur-api-mareefr)
- [Administration des données](#administration-des-données)
- [Comptes, structures et préférences](#comptes-structures-et-préférences)
- [Créneaux choisis](#créneaux-choisis)
- [API](#api)
- [Exploitation : sauvegarde, surveillance, sécurité](#exploitation--sauvegarde-surveillance-sécurité)
- [Précision et limites](#précision-et-limites)
- [Structure du projet](#structure-du-projet)
- [Pistes](#pistes)
- [Contribuer](#contribuer)
- [Licence](#licence)

## Fonctionnement

```
AVISO+ (FES NetCDF) ──► precompute.py ──► data/plongee.db ──► FastAPI ──► navigateur
                          │  pyTMD : hauteurs au pas de 10 min, PM/BM
                          │  Brest : coefficients
                          └  astral : lever/coucher, crépuscule nautique
```

Pour un port et une année, `precompute.py` :

1. calcule la hauteur d'eau toute l'année au pas de 10 min (≈ 52 000 points) ;
2. détecte les pleines mers (PM) et basses mers (BM), puis affine l'heure et la hauteur de chacune par interpolation parabolique entre les points de 10 min (précision de la minute) ;
3. attribue à chaque PM le coefficient de la PM de **Brest** la plus proche (±6 h), la série de Brest étant calculée dans le même run ;
4. calcule les horaires solaires jour par jour ;
5. remplace en base les données de **cette année uniquement**, en une transaction : les autres années sont conservées, et un échec laisse la base intacte.

L'interface permet ensuite de filtrer par période, phase de marée (PM, BM ou les deux), coefficient maximum, marge autour de l'étale et lumière requise (nautique, civile ou aucune). Pour un compte connecté, chaque créneau affiche aussi une heure de rendez-vous (absente pour un visiteur non connecté) : l'étale moins un délai réglé par les administrateurs de chaque structure (2 h par défaut, onglet « Créneaux »), arrondie aux 5 minutes inférieures — étale à 9h37 et délai de 2h15 → rendez-vous à 7h20.

## Démarrage rapide avec Docker

Prérequis : Docker + Compose, et un compte gratuit [AVISO+](https://www.aviso.altimetry.fr) ayant accès au produit FES.

```bash
cp .env.example .env
# Renseigner AVISO_USERNAME / AVISO_PASSWORD, HOST_UID / HOST_GID (sortie de `id -u` / `id -g`),
# MAREE_HOST (domaine public)

docker compose up -d --build      # nécessite la stack Traefik (voir « Derrière Traefik »)

# Premier administrateur
docker compose run --rm --entrypoint python api -m app.auth create-admin jerome \
    --email jerome@example.fr --first-name Jérôme --last-name "Le Goff"
```

Puis ouvrir `https://<MAREE_HOST>/admin.html` (en local : <http://localhost:8000/admin.html>, voir ci-dessous) :

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
| `worker` | exécute les tâches en file, une à la fois (1 Go max par défaut, `WORKER_MEM_LIMIT`) | `docker compose up -d` |
| `scheduler` | ajoute les tâches périodiques à la file via [supercronic](https://github.com/aptible/supercronic) | `docker compose up -d` |
| `fetch-models` | téléchargement FES en direct | profil `tools`, `run --rm` |
| `precompute` | précalcul en direct | profil `tools`, `run --rm` |

Le `scheduler` ne calcule rien lui-même : il ajoute des tâches que le `worker` exécute, si bien qu'elles apparaissent dans l'administration comme celles lancées à la main. `docker/crontab` :

- **1er décembre, 02:00** : mise à jour du modèle FES (seuls les fichiers plus récents sont retéléchargés) ;
- **15 décembre, 03:00** : précalcul de l'année suivante pour chaque port coché « Recalculer automatiquement chaque année » et doté d'un niveau moyen ;
- **toutes les 5 minutes** : envoi des newsletters programmées dont l'heure est venue (rien n'est mis en file s'il n'y en a pas) ;
- **2 de chaque mois, 04:30** : recalage sur api-maree.fr de chaque port doté d'un site api-maree.fr (voir [Recalage](#recalage-sur-api-mareefr)) ;
- **tous les jours, 05:10** : horaires du mois glissant (J−1 à J+29) repris d'api-maree.fr pour ces mêmes ports ;
- **1er de chaque mois, 04:00** (et au démarrage) : vacances scolaires ;
- **tous les jours, 03:30** : sauvegarde de la base ; **07:05** : contrôle de santé et alertes (voir [Exploitation](#exploitation--sauvegarde-surveillance-sécurité)).

### Variables d'environnement (`.env`)

| Variable | Défaut | Description |
|---|---|---|
| `AVISO_USERNAME`, `AVISO_PASSWORD` | — | Identifiants AVISO+ |
| `API_MAREE_KEY` | — | Clé [api-maree.fr](https://api-maree.fr) pour le recalage du modèle (facultative : sans clé, hauteurs FES brutes) |
| `SECRETS_KEY` | — | Clé de chiffrement des clés Mailjet des structures (voir [Connexion Mailjet](#connexion-mailjet)) ; sans elle, Mailjet ne peut pas être connecté |
| `BACKUP_KEEP` | `14` | Sauvegardes quotidiennes conservées dans `data/backups/` |
| `ALERT_EMAIL` | — | Destinataires des alertes (séparés par des virgules) ; vide : les super administrateurs ayant une adresse e-mail |
| `TRUSTED_PROXY_HOPS` | `1` | Proxys de confiance devant l'API (Traefik : 1 ; 0 si exposée directement) : adresse IP réelle pour la limitation des tentatives |
| `CORS_ORIGINS` | — | Origines autorisées à appeler l'API depuis un navigateur ; vide : aucune |
| `RATE_LIMIT` | `1` | `0` désactive la limitation des tentatives (tests) |
| `FES_MODEL` | `FES2014` | Modèle par défaut (`FES2014` ou `FES2022`), pour le téléchargement et les calculs, tant qu'aucun n'est choisi dans l'administration |
| `FES_DIR` | `./models` | Dossier hôte des fichiers NetCDF |
| `MAREE_HOST` | — | Domaine public routé par Traefik (obligatoire) |
| `API_PORT` | `8000` | Port exposé sur l'hôte, en local uniquement (`docker-compose.local.yml`) |
| `HOST_UID`, `HOST_GID` | `1000` | Utilisateur propriétaire de `./data` et `./models` |
| `MAIL_BACKEND`, `APP_BASE_URL`, `SMTP_*` | — | Envoi d'e-mails (invitations, mot de passe oublié) : voir [Comptes](#comptes-structures-et-préférences) |

Côté application, `tide_model.py` lit `TIDE_MODEL_DIRECTORY` (fixé à `/models` dans les conteneurs) et `TIDE_MODEL_NAME` (défaut `FES2014`).

> **Choix du modèle** : le modèle utilisé pour les calculs se choisit dans l'administration (**Données et tâches → Modèle utilisé pour les calculs**) et est enregistré en base ; `FES_MODEL` ne sert que tant qu'aucun choix n'a été fait. Télécharger le modèle avant de lancer des calculs avec lui.

### Derrière Traefik

Le service `api` est exposé par le reverse proxy mutualisé [traefik-proxy](https://github.com/jejedu22/traefik) : il rejoint le réseau externe `proxy` et ne publie aucun port sur l'hôte. Le routeur `maree` sert `MAREE_HOST` en HTTPS (certificat Let's Encrypt, middlewares `default@file` : en-têtes de sécurité + compression). `worker` et `scheduler` restent sur le réseau interne du projet.

1. Démarrer la stack Traefik (elle crée le réseau `proxy`) ;
2. Faire pointer `MAREE_HOST` vers le serveur dans le DNS ;
3. Dans `.env` : `MAREE_HOST`, `COOKIE_SECURE=1`, `APP_BASE_URL=https://<MAREE_HOST>` ;
4. `docker compose up -d --build`.

Traefik ne route le conteneur qu'une fois son healthcheck au vert (quelques secondes après le démarrage).

**En local, sans Traefik** : `docker-compose.local.yml` publie l'API sur `API_PORT` et désactive le cookie sécurisé :

```bash
docker network create proxy    # une seule fois, si la stack Traefik ne tourne pas
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
```

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
export FES_MODEL=FES2014              # ou FES2022 (défaut, sauf choix fait dans l'administration)
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
| `--model` | Modèle pyTMD (défaut : modèle choisi dans l'administration, sinon `FES_MODEL`, sinon `FES2014`) |
| `--timezone` | Défaut `Europe/Paris` |
| `--step-minutes` | Pas de calcul, défaut 10 |
| `--no-checks` | Écrire même si les contrôles de cohérence échouent (diagnostic uniquement) |

Compter plusieurs minutes par port. Relancer une année déjà présente la remplace proprement.

**Contrôles de cohérence.** Avant d'écraser l'année précédente, le précalcul vérifie ses résultats (`app/checks.py`) : alternance PM/BM, 1 300 à 1 600 étales par an, 3 h 30 à 9 h entre deux étales, amplitude plausible, aucun trou aux bords de l'année ni de jour sans étale, coefficients présents et entre 20 et 120, horaires solaires complets et ordonnés. Au moindre doute la tâche échoue (code 1) et **la base reste inchangée** ; le journal de la tâche dit pourquoi.

Le script avertit si la basse mer la plus basse passe à plus de 0,30 m sous le zéro des cartes, signe d'un `offset_zh_m` probablement trop faible.

## Ports et zéro des cartes

FES donne des hauteurs par rapport au **niveau moyen**. Pour obtenir des hauteurs comparables à l'annuaire SHOM (au-dessus du **zéro hydrographique**), chaque port porte un décalage `offset_zh_m` dans `app/ports_catalog.py`.

Valeurs actuellement renseignées : Binic (6,68 m), Saint-Quay-Portrieux (6,57 m), Erquy (6,62 m), Paimpol (6,25 m), Brest (4,32 m). Les autres ports du catalogue sont à `0` : **le précalcul refuse de les traiter** tant qu'une valeur n'est pas renseignée ou passée avec `--offset-zh`.

Source officielle : colonne « NM » des Références Altimétriques Maritimes (RAM) du Shom, sur [data.shom.fr](https://data.shom.fr) ou [data.gouv.fr](https://www.data.gouv.fr).

### Coefficients

Le coefficient (échelle 20–120) est une notion française définie à Brest. Il n'est pas fourni par pyTMD : il est estimé à partir de la hauteur de chaque PM de Brest au-dessus du niveau moyen, divisée par l'unité de hauteur (`U_BREST = 3,05 m`), **sans** offset. Chaque PM d'un autre port reçoit le coefficient de la PM de Brest la plus proche ; dans l'API, une BM reçoit celui de la PM voisine. Ces coefficients sont **indicatifs**.

## Recalage sur api-maree.fr

[api-maree.fr](https://api-maree.fr) calcule ses hauteurs à partir de l'atlas régional **Ifremer/PREVIMER** (licence CC BY), plus précis près des côtes que FES, mais seulement sur une fenêtre glissante **J−30 / J+30**. Pour chaque port doté d'un identifiant api-maree.fr :

- **court terme** : les horaires du mois glissant sont **ceux d'api-maree.fr**, repris chaque jour ;
- **long terme** : au-delà, le calcul FES, **recalé** chaque mois sur api-maree.fr.

### Mois glissant (court terme)

Chaque jour à 05:10, la tâche **Mois glissant** (`app/short_term.py`) enregistre, de J−1 à J+29, les hauteurs (pas de 10 min), les pleines / basses mers et les coefficients d'api-maree.fr, **à côté** du calcul FES (qui reste en base) (`/water-levels` et `/tide-extrema` : 6 requêtes par port). Les étales sont déduites de la série api-maree.fr et affinées par interpolation parabolique ; elles coïncident à la minute avec celles d'api-maree.fr. Les jours passés gardent les valeurs api-maree.fr.

- **Coefficients** : ceux d'api-maree.fr (`/tide-extrema`), pris sur la PM api-maree.fr la plus proche (à 30 min près). À défaut, la PM garde le coefficient du calcul FES qu'elle remplace, et le journal le signale.
- **Créneaux déjà choisis** : recalés sur la nouvelle heure de l'étale, comme lors d'un recalcul.
- **Années** : seules les années déjà précalculées sont concernées ; un nouveau précalcul ne touche pas aux horaires api-maree.fr.
- **Contrôle du référentiel** : les hauteurs api-maree.fr et les hauteurs stockées (FES + `offset_zh_m`) sont au-dessus du zéro des cartes. Si leurs moyennes diffèrent de plus de 50 cm (niveau moyen du port erroné, ou site d'un autre port), rien n'est écrit ; au-delà de 15 cm, un avertissement est journalisé.
- Dans l'onglet Ports, l'étiquette **30 j → date** indique la fin de la fenêtre reprise (signalée si elle n'a pas été rafraîchie depuis 2 jours), et le bouton **30 jours** la rafraîchit tout de suite.

### Recalage du calcul FES (long terme)

**Pourquoi une correction onde par onde.** L'erreur de FES dans un port n'est pas un simple décalage horaire : elle varie d'une marée à l'autre, entre vives-eaux et mortes-eaux, car chaque onde (M2, S2, N2…) a sa propre erreur. De plus, seules les ondes principales de FES sont calculées : les ondes de petits fonds (M4, MS4, MN4…), fortes en Manche, manquent. Un décalage et un facteur d'amplitude uniques, ajustés sur deux mois, ne corrigeaient donc que l'erreur moyenne, souvent proche de zéro.

**Méthode** (`app/calibration.py`) :

1. la tâche **Recalage** récupère les hauteurs api-maree.fr de J−29 à J+29 au pas de 10 min (6 requêtes par port, sous le quota de 360 requêtes/heure) et calcule FES aux mêmes instants ;
2. l'écart référence − FES est lui-même une marée : il est ajusté par moindres carrés sur M2, S2, N2, K1, O1, M4, MS4, MN4, L2, Q1, M6 et 2MS6, toutes séparables sur deux mois. K2 et P1, trop proches de S2 et K1 pour être séparées sur cette durée, sont **inférées** : elles reçoivent la même correction relative que S2 et K1, dans leur rapport astronomique (0,272 et 0,331). Sans cela, l'erreur sur K2 reviendrait en sens inverse trois mois plus tard ;
3. le précalcul ajoute cette correction à FES pour **toute l'année** : `hauteur = FES(t) + correction(t) + offset_zh_m`.

Sur une marée synthétique de type Saint-Quay-Portrieux (FES sans ondes de petits fonds, erreurs de quelques degrés par onde), l'écart moyen des heures de PM/BM passe de 11 min à moins d'une minute (2 min au pire), dans la fenêtre comme 14 mois plus tard. Sur un vrai port, le résultat dépend de la qualité de la référence ; le journal de la tâche et l'info-bulle de l'administration donnent les écarts mesurés.

Le niveau moyen de la référence n'est pas appliqué, mais un écart de plus de 15 cm avec `offset_zh_m` est signalé dans le journal : c'est une bonne façon de contrôler le niveau moyen saisi.

Mise en place :

- renseigner `API_MAREE_KEY` dans `.env` (clé gratuite sur api-maree.fr) ;
- dans l'administration (onglet **Ports**, **Modifier**), saisir l'**identifiant du site api-maree.fr** du port (ex. `saint-quay-portrieux`, voir la liste des sites sur api-maree.fr), puis cliquer sur **Recaler**.

Le recalage est refait le 2 de chaque mois. S'il change sensiblement (correction modifiée d'au moins 3 cm), les années déjà calculées, à partir de l'année en cours, sont remises en file. Un recalage établi pour FES2014 n'est pas appliqué aux calculs FES2022 (et inversement) : relancer le recalage après un changement de modèle. Un résultat invraisemblable (correction de plus d'1 m sur une onde, écart résiduel de plus de 25 cm, moins de 15 jours de données) est refusé et l'ancien recalage est conservé. Dans l'onglet Ports, la colonne **Recalage** affiche l'écart moyen des heures de PM/BM avant → après, avec la correction de chaque onde en info-bulle. **Modifier → Abandonner le recalage** : les prochains calculs du port n'ont plus de version corrigée (tout le monde voit le calcul brut).

**Recalages faits avec la version précédente** (décalage et amplitude uniques) : ils restent appliqués et sont signalés dans l'administration. Cliquer sur **Recaler** (ou attendre le recalage mensuel) les remplace et relance le précalcul des années à venir.

### Recherche par hauteur d'eau

En plus de la recherche par étale, une page **`/hauteurs.html`** donne les **plages horaires où l'eau est à la bonne hauteur** : au moins X m pour mettre un bateau à l'eau ou passer une porte de bassin, au plus X m pour un site accessible à marée basse.

- **Hauteurs d'eau** : saisies par les super administrateurs, port par port (`/admin.html` → **Ports** → **Hauteurs d'eau**) : libellé, hauteur au-dessus du zéro des cartes (comme l'annuaire des marées) et sens (« au moins » / « au plus »). Un port peut en avoir plusieurs.
- **Recherche proposée** : réglée par les super administrateurs, structure par structure (`/admin.html` → **Structures**, colonne *Recherche*) : par étale (défaut), par hauteur d'eau, ou les deux. Les liens de l'en-tête suivent ; une structure réglée sur la seule hauteur d'eau arrive sur `/hauteurs.html` depuis la recherche. Un super administrateur a les deux, un visiteur la recherche par étale.
- **Calcul** (`app/water_windows.py`) : sur la série de hauteurs au pas de 10 min, dans les horaires de la structure (sources de marée), chaque franchissement de la hauteur est interpolé entre deux points ; début arrondi à la minute supérieure, fin à la minute inférieure. Filtres : période, **lumière du jour** (il faut au moins la durée minimale de jour dans la plage ; la colonne *De jour* donne la partie de jour), **durée minimale**. Les plages qui commencent dans la période sont listées, avec leur hauteur maximale (ou minimale), les jours fériés, vacances et indisponibilités.
- **Plage « limite »** : quand l'eau ne dépasse la hauteur que de moins de 20 cm, quelques centimètres d'erreur (niveau moyen du port, modèle) décalent beaucoup les heures : la plage est signalée.
- **Choisir une plage** : comme une étale (type, intitulé, plusieurs créneaux sur la même plage, places, inscriptions) : c'est un **créneau de hauteur d'eau**. **RDV** : début de la plage, arrondi aux 5 minutes inférieures. La hauteur est recopiée : la modifier ou la supprimer ne touche pas les créneaux déjà choisis. Seuls le type et l'intitulé se modifient.
- **Recalage** : quand les horaires changent (précalcul, mois glissant, changement de sources de la structure), le créneau suit sa plage (celle qui la recouvre le plus, sinon la plus proche à 1 h près) ; une plage disparue laisse le créneau tel quel.
- Dans **Créneaux choisis**, l'export Excel et les newsletters, le créneau affiche sa hauteur d'eau et sa plage (« ≥ 7,00 m · 09:56 → 14:54 »).
- API : `GET /api/water-thresholds`, `GET /api/water-windows?threshold_id&start&end&daylight&min_minutes`, `POST /api/selections/height` ; super administrateurs : `/api/admin/water-thresholds`, `POST /api/admin/ports/{id}/water-thresholds`.
- **Mise à jour d'une base existante** (migration n° 6) : toutes les structures restent sur la recherche par étale.

### Horaires de marée par structure

Chaque structure choisit les horaires qu'elle voit, dans `/admin.html` → **Créneaux** → *Horaires de marée* (ses administrateurs) :

- **Mois glissant api-maree.fr** (coché par défaut) : sur J−1 / J+29, les horaires d'api-maree.fr ;
- **Correction du calcul FES** (cochée par défaut) : ailleurs, le calcul FES recalé sur api-maree.fr ;
- tout décocher : le **calcul FES brut** partout.

Pour cela, la base garde jusqu'à trois séries par port (colonne `source` de `tide_extrema` et `tide_heights`) : `fes` (calcul brut), `cal` (calcul corrigé, pour un port recalé, ou dont Brest l'est : coefficients corrigés) et `api` (mois glissant). La table `tide_coverage` dit quelle période chaque série couvre. La lecture compose les séries dans l'ordre de préférence de la structure (`db.tide_sources`), les autres servant en dernier recours pour ne jamais laisser de trou. Le précalcul d'un port recalé écrit les deux calculs (un seul calcul FES : la correction s'ajoute) ; le mois glissant n'écrase plus le calcul FES.

- Les **visiteurs** et les comptes sans structure voient les réglages par défaut.
- Une structure ne **choisit** que des étales de ses horaires ; la recherche rappelle les réglages s'ils ne sont pas ceux par défaut.
- La **note de source** en bas des pages de recherche (étales et hauteurs d'eau) décrit les horaires réellement vus par la structure du compte (`structure.tide_sources` de `/api/auth/me`).
- **Changer de réglage** recale les créneaux choisis **à venir** de la structure sur l'étale correspondante de ses nouveaux horaires (même marée, à 2 h près) : heure, hauteur, coefficient et RDV sont mis à jour. Les autres structures ne sont pas touchées.
- Un recalcul (précalcul, mois glissant) recale les créneaux de chaque structure dans **ses** horaires.
- **Mise à jour d'une base existante** (migration n° 5) : la série unique devient `cal` pour un port recalé, `fes` sinon, et `api` sur la dernière fenêtre du mois glissant. Le calcul brut d'un port recalé n'existe pas encore : les années à venir concernées sont **remises en file de précalcul** automatiquement (onglet *Données et tâches*). D'ici là, une structure qui désactive la correction voit encore le calcul corrigé, et une structure qui désactive api-maree.fr voit encore ses horaires sur la dernière fenêtre.

## Administration des données

La page `/admin.html` (administrateurs) comporte trois onglets.

**Ports** : ajout depuis le catalogue (`app/ports_catalog.py`) ou en saisie libre, modification, suppression (avec toutes les données calculées du port), case « recalcul annuel », années calculées (le modèle utilisé en info-bulle ; une année calculée avec un autre modèle que le modèle actuel est signalée), et bouton **Calculer** par port et par année, ou pour tous les ports annuels d'un coup. Un port sans niveau moyen au-dessus du zéro des cartes peut être enregistré mais pas calculé. La page de recherche ne propose que les ports ayant au moins une année calculée.

**Données et tâches** : choix du modèle utilisé pour les calculs (FES2014 ou FES2022, avec l'état de téléchargement de chacun), état du modèle FES (taille, date de mise à jour), téléchargement / mise à jour depuis AVISO+, synchronisation des vacances scolaires, et liste des tâches avec statut, durée, journal en direct et annulation.

**Utilisateurs** : voir [Comptes et préférences](#comptes-et-préférences).

### File de tâches

L'API ne lance jamais de calcul : elle enregistre une tâche dans la table `jobs`, et le service `worker` (`python -m app.jobs worker`) les exécute **une par une** en sous-processus, en recopiant leur sortie dans le journal. Un précalcul peut donc occuper toute sa mémoire (`WORKER_MEM_LIMIT`, 1 Go par défaut) sans toucher au serveur web, et deux calculs ne se marchent pas dessus. Une tâche identique déjà en attente ou en cours n'est pas dupliquée. Chaque tâche de calcul retient le modèle choisi au moment de sa mise en file (visible dans son libellé) : changer de modèle ne modifie pas les tâches déjà prévues.

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
python -m app.jobs enqueue calibrate --port-id 3      # recalage api-maree.fr (--all : tous les ports dotés d'un site)
python -m app.jobs enqueue short-term --port-id 3     # mois glissant depuis api-maree.fr (--all : idem)
python -m app.jobs enqueue school-holidays
```

**Mise à jour d'une installation existante** : les ports déjà en base reçoivent automatiquement leur niveau moyen depuis le catalogue quand il y est connu, et sont cochés « annuel ». La variable `MAREE_PORTS` n'est plus utilisée : c'est la case de l'administration qui décide.

## Comptes, structures et préférences

L'application reste utilisable sans compte. Un compte permet d'accéder aux créneaux choisis par sa **structure** et d'**enregistrer ses préférences** : critères du formulaire (port, durée de la période, phase, coefficient max, marge, lumière) et filtres de la ligne de titre du tableau. Elles sont réappliquées à la connexion, puis une recherche est lancée automatiquement. La période est enregistrée comme une **durée** (« 13 jours à partir d'aujourd'hui »), pas comme des dates fixes. La recherche par hauteur d'eau a ses **propres préférences** (port, hauteur d'eau, durée de la période, lumière, durée minimale et filtres du tableau ; `GET`/`PUT /api/me/water-preferences`, table `user_water_preferences`). Sans préférence de port, la recherche propose le **port par défaut de la structure**, choisi par ses administrateurs dans **`/admin.html` → Créneaux**.

### Structures et rôles

Une **structure** (club, groupe…) regroupe des comptes, sa liste de **types de créneaux** et sa liste de **créneaux choisis**. Chaque compte appartient à une ou **plusieurs** structures ([voir plus bas](#plusieurs-structures-par-compte)), avec l'un de ces rôles dans chacune :

| Rôle | Peut |
|---|---|
| **Visualisation** | voir la liste des créneaux choisis par sa structure (et les voir grisés dans la recherche) |
| **Administration** | en plus : choisir et retirer les créneaux de la structure, gérer ses membres (création, rôle, mot de passe, suppression) et ses types de créneaux |
| **Super administrateur** | tout : structures, ports, données et tâches, comptes et types de toutes les structures. Peut aussi appartenir à une structure (il y a alors les droits d'administration) |

Il n'y a pas d'inscription libre : les comptes sont créés sur **`/admin.html` → Utilisateurs**, par un super administrateur (dans n'importe quelle structure) ou par un administrateur de structure (dans la sienne, sans pouvoir créer de super administrateur). Garde-fous : on ne peut ni supprimer son propre compte, ni se retirer ses droits de super administrateur, ni changer son propre rôle de structure ; il reste toujours au moins un super administrateur ; une structure n'est supprimable qu'une fois vide de membres (ses types et créneaux choisis partent avec elle).

### Plusieurs structures par compte

Un compte peut appartenir à **plusieurs structures**, avec un **rôle et des profils propres à chacune** (administrateur d'un club, simple membre d'un autre). Un **sélecteur dans l'en-tête** permet de passer de l'une à l'autre. La structure choisie est celle de **la session** : deux navigateurs peuvent être sur deux structures, et la dernière utilisée est reprise à la connexion suivante. Un super administrateur choisit parmi toutes les structures (ou aucune) et y est en administration, sans en être membre.

**Rejoindre une deuxième structure : par invitation.** Un administrateur de structure saisit l'adresse e-mail d'un compte existant dans **Utilisateurs → Inviter un compte existant**, avec le rôle et les profils proposés. Le titulaire du compte voit un bouton **Invitations** dans l'en-tête (et reçoit un e-mail si l'envoi d'e-mails est configuré) ; il rejoint la structure **s'il accepte**, rien ne change s'il refuse. Une invitation vaut 30 jours et peut être annulée par l'administrateur. Un super administrateur peut aussi rattacher directement un compte (Utilisateurs → **Modifier**, choix de la structure).

Garde-fous :

- l'invitation est liée au **compte** qui porte l'adresse au moment de l'invitation, pas à l'adresse (modifiable sans vérification) : changer d'adresse ne permet pas de réclamer l'invitation d'un autre ;
- l'administrateur reçoit **la même réponse** qu'un compte existe ou non à cette adresse, et le nombre d'invitations est limité (30 par heure) : il ne peut pas sonder les comptes des autres structures ;
- le **profil et le mot de passe d'un compte partagé** ne sont modifiables que par son titulaire ou un super administrateur : sinon l'administrateur d'une structure prendrait la main sur un compte qui en administre une autre. Il reste maître du rôle et des profils **dans sa structure** ;
- **retirer** un compte partagé de sa structure ne supprime pas le compte (il reste dans ses autres structures, ses inscriptions à ses créneaux sont retirées) ; supprimer un compte qui n'a qu'une structure le supprime, comme avant ;
- la liste des membres, les newsletters (audiences, désinscriptions, groupes) et les inscriptions se font **par structure**.

Techniquement : table `memberships` (compte, structure, rôle), `user_profiles` par structure, `sessions.structure_id` pour la structure active, `structure_invitations`. `users.structure_id` n'est plus que la structure par défaut (la dernière utilisée). Migration de schéma n° 2 (`app/migrations.py`), reprise automatique des comptes existants.

### Profils

En plus de son rôle (visualisation ou administration), un compte peut recevoir un ou plusieurs **profils**, cumulables, qui ouvrent des fonctions particulières. Un administrateur de structure les attribue aux comptes de sa structure (y compris le sien), un super administrateur à ceux de toutes les structures, dans **Utilisateurs** (création ou **Modifier**). Un profil exige une structure et vaut **dans cette structure seulement**.

| Profil | Ouvre |
|---|---|
| **Gestionnaire** | les [newsletters](#newsletters) de la structure : rédaction, envoi et suivi des envois. Un administrateur n'y a pas accès d'office : il se l'attribue s'il en a besoin |
| **Inscriptions** | [inscrire d'autres membres](#inscrire-dautres-membres) de la structure sur les créneaux, et retirer leur inscription (un encadrant qui inscrit ses élèves, par exemple). Un administrateur de structure a ce droit d'office |

Le catalogue des profils est dans `app/accounts.py` (`PROFILES`) ; les profils d'un compte, dans la table `user_profiles` (par structure).

### Voir comme un autre rôle (super administrateur)

Un super administrateur peut afficher l'application **comme un autre rôle**, pour vérifier ce que chacun voit : bouton **Voir comme…** de l'en-tête, puis le rôle (**administrateur de structure**, **membre en visualisation** ou **compte sans structure**), la structure et, éventuellement, des profils (Gestionnaire, Inscriptions). Un bandeau rappelle l'aperçu sur toutes les pages, avec **Changer** et **Quitter l'aperçu**.

- **Fidèle** : l'API applique les droits du rôle choisi (pages, menus, données visibles), comme pour un vrai compte de ce rôle ; le compte n'est plus super administrateur pendant l'aperçu.
- **Lecture seule** : toute modification est refusée (403) pendant l'aperçu, sauf le changer, le quitter ou se déconnecter. Rien n'est créé ni modifié dans la structure.
- **Propre à la session** : seul ce navigateur est en aperçu ; une nouvelle connexion repart sans aperçu.

`PUT /api/me/preview` (`{"role": "manager" | "viewer" | "none", "structure_id": …, "profiles": […]}`) le démarre, `DELETE /api/me/preview` le quitte ; colonnes `sessions.preview_role` et `preview_profiles` (migration n° 7). Pour voir l'application **sans être connecté**, une fenêtre de navigation privée suffit.

### Connexion Mailjet

Les newsletters partent par [Mailjet](https://www.mailjet.com), avec le compte Mailjet **de chaque structure**. Un administrateur de la structure (ou un super administrateur) le connecte dans **`/admin.html` → Mailjet** :

1. clé API et clé secrète (compte Mailjet → Paramètres du compte → Gestion des clés API) ;
2. adresse et nom d'expéditeur : l'adresse doit être **validée chez Mailjet** (Adresses et domaines d'expéditeur), seule ou par son domaine ;
3. **Enregistrer et tester** vérifie que les clés sont acceptées et que l'adresse est validée ; **M'envoyer un e-mail de test** écrit à l'adresse du compte connecté.

Les clés sont **chiffrées** en base (Fernet, bibliothèque `cryptography`) avec `SECRETS_KEY`, et ne sont plus jamais renvoyées par l'API : l'interface n'en montre que les 4 derniers caractères. Pour en changer, on saisit les deux de nouveau.

```bash
# Générer SECRETS_KEY (une fois), puis la mettre dans .env et la sauvegarder à part
docker compose run --rm --entrypoint python api -m app.secrets_store generate
```

Perdre ou changer `SECRETS_KEY` rend les clés enregistrées illisibles : le test de connexion le signale, et il suffit de les ressaisir. Les e-mails de service (invitations, mot de passe oublié) restent envoyés par le SMTP de l'application (`MAIL_BACKEND`).

**Suivi des envois** : **Activer le suivi** (même onglet) donne à la structure une adresse de suivi secrète (`/api/mailjet/events/<jeton>`) et la déclare chez Mailjet pour les événements remis, ouvert, cliqué, rebond, bloqué, indésirable et désinscription. Sans suivi, les rapports ne montrent que les envois et les refus. Changer de clés désactive le suivi (autre compte Mailjet possible) : il suffit de le réactiver. En cas d'échec de l'activation automatique, l'adresse à déclarer à la main est affichée.

### Newsletters

Page **`/newsletters.html`** (lien « Newsletters » de l'en-tête), réservée au profil **Gestionnaire** de la structure.

- **Rédaction** : objet, pré-en-tête facultatif, destinataires, contenu dans un format simple (titres `##`, `**gras**`, `*italique*`, liens `[texte](https://…)`, listes `- `, bouton `[[Texte|https://…]]`, image `![description](https://…)`, séparateur `---`, et `{{prenom}}`, `{{nom}}`, `{{structure}}` remplacés pour chaque destinataire). Barre de mise en forme et **aperçu en direct**, en HTML et en texte, rendu par le serveur exactement comme à l'envoi (`app/newsletter_render.py` ; tout le texte saisi est échappé, liens et images limités à `http(s)` et `mailto:`).
- **Destinataires** : tous les membres, les administrateurs, les membres en visualisation, un **groupe d'envoi**, ou les inscrits à un créneau à venir ; comptes ayant une adresse e-mail, moins les désinscrits. Le nombre est affiché ; la liste est **figée au moment de l'envoi**.
- **Groupes d'envoi** : listes nommées de membres de la structure (« Encadrants », « Préparants N1 »…), créées et modifiées par les gestionnaires depuis la page Newsletters (recherche, tout cocher / décocher). Un membre peut être dans plusieurs groupes ; un compte qui quitte la structure n'est plus visé. Supprimer un groupe ne change pas les newsletters déjà envoyées.
- **Créneaux choisis automatiques** : le bouton **Créneaux…** insère un bloc `[[creneaux:30]]` (les 30 prochains jours à partir du jour de l'envoi) ou `[[creneaux:2026-11-01:2026-11-30]]` (entre deux dates). Il devient la liste des créneaux choisis par la structure sur la période (date, RDV, lieu, intitulé, étale et coefficient, type), suivie d'un lien « Voir les créneaux et s'inscrire ». Elle est **calculée au moment de l'envoi** : une newsletter programmée montre les créneaux à jour.
- **M'envoyer un test** (objet préfixé « [TEST] »), **Envoyer…** (confirmation avec le nombre de destinataires), **Programmer…** (à 5 minutes près, annulable tant que l'heure n'est pas venue), **Dupliquer**, **Supprimer**. Seul un brouillon se modifie.
- **Envoi** par le worker (`app/newsletter_send.py`, tâche « Envoi de la newsletter #N »), par lots de 50 (Send API v3.1). Chaque e-mail porte le suivi des ouvertures et des clics, un lien de désinscription personnel et l'en-tête `List-Unsubscribe` (désinscription en un clic des messageries). Une panne de Mailjet arrête l'envoi en « Échec » : **Reprendre l'envoi** vise seulement les destinataires restants. Une même newsletter ne peut pas partir deux fois.
- **Rapport** : destinataires, envoyés, délivrés, ouverts, cliqués (avec les taux), rebonds, bloqués, indésirables, désinscrits, refusés ; liens cliqués ; détail par destinataire avec filtres et recherche ; export Excel. Suivi de l'envoi en cours. Les ouvertures sont sous-estimées (images bloquées par certaines messageries).
- **Désinscription** : le lien mène à `/desinscription.html`, qui demande confirmation (les messageries ouvrent les liens toutes seules). Elle vaut pour toutes les newsletters de la structure. Un signalement comme indésirable désinscrit aussi. La personne peut se réinscrire dans **Mon compte** (« Recevoir les newsletters de ma structure »), où elle peut aussi se désinscrire. La liste des désinscrits est visible des gestionnaires.

### Demande de création de structure

Un club qui n'a pas encore de structure peut la demander depuis la page publique **`/demande-structure.html`** (lien en bas de la page de recherche et dans la fenêtre de connexion) : nom de la structure, ville ou port d'attache, nom, e-mail, téléphone et message facultatifs, et accord sur le traitement des données.

- La demande est enregistrée, et les super administrateurs qui ont une adresse e-mail sont prévenus (si l'envoi d'e-mails est configuré ; un échec d'envoi n'empêche pas l'enregistrement). Aucun e-mail n'est envoyé à l'adresse saisie, pour que le formulaire ne puisse pas servir à écrire à un tiers.
- Anti-abus : champ piège invisible pour les robots (la demande est ignorée sans le dire), 3 demandes par adresse e-mail et 30 au total par 24 h.
- **`/admin.html` → Structures → Demandes de création** : **Créer la structure** (nom modifiable), puis **Préparer le compte**, qui ouvre « Ajouter un utilisateur » prérempli avec le contact, en administration de la nouvelle structure ; ou **Classer sans suite**, **Remettre en attente**, **Supprimer**.
- Une demande traitée est supprimée au bout d'un an ; une demande en attente reste jusqu'à son traitement.

**Mise à jour d'une base existante** : au premier démarrage, les comptes, types et créneaux existants sont rattachés à une structure « Structure principale » ; les super administrateurs y sont en administration, **les autres comptes en visualisation** (à promouvoir si besoin). Si plusieurs comptes avaient choisi le même créneau, seul le premier choix est conservé.

### Aide en ligne

Des pages d'aide, publiques, expliquent l'application selon le rôle : **`/aide.html`** (lien « Aide » dans l'en-tête et en bas de la recherche) mène aux guides **Membre** (`aide-membre.html`), **Administrateur de structure** (`aide-administrateur.html`), **Profil Gestionnaire** (`aide-gestionnaire.html`) et **Profil Inscriptions** (`aide-inscriptions.html`). Les guides qui concernent le compte connecté sont signalés « Pour vous ». Pages statiques, à tenir à jour avec les fonctions ; un test vérifie que chaque profil du catalogue (`PROFILES`) a son guide et que les liens internes sont valides.

### Application installable (PWA)

Calendive s'installe sur l'écran d'accueil (Android, iPhone, ordinateur) et s'ouvre alors en plein écran avec son icône : manifeste **`static/manifest.webmanifest`**, icônes `icon-192.png`, `icon-512.png` et `icon-maskable-512.png` (générées depuis `logo-sombre.svg`), service worker **`static/sw.js`** enregistré par `session.js`. Le guide Membre explique l'installation (section « Installer l'application », avec un bouton quand le navigateur le propose).

- **Données jamais en cache** : `/api/…` (et `/healthz`, la documentation de l'API) ne passent pas par le service worker ; comptes, créneaux, inscriptions et marées viennent toujours du serveur.
- **Interface « réseau d'abord »** : pages, scripts, styles et images sont pris sur le serveur et gardés en cache seulement pour le secours hors connexion ; une mise à jour du serveur est donc visible tout de suite. Les clés de cache ne gardent pas les paramètres d'URL (jetons de désinscription ou de mot de passe).
- **Hors connexion** : les pages de l'application sont remplacées par `hors-ligne.html` (« Pas de connexion ») ; les pages d'aide et les mentions légales déjà ouvertes restent lisibles. Les appels à l'API échouent avec un message lisible.
- **HTTPS obligatoire** (sauf `localhost`) : un service worker ne s'enregistre pas sur un site en HTTP.
- Changer `VERSION` dans `sw.js` vide les caches des appareils (utile seulement si la liste préchargée change).

### Profil

Chaque compte a un **identifiant**, un **prénom**, un **nom**, une **adresse e-mail** (unique) et un **téléphone** facultatif (numéros français mis en forme : `06 12 34 56 78`, `+33 6 12 34 56 78`). On se connecte avec l'identifiant **ou** l'adresse e-mail. Laissé vide à la création, l'identifiant est proposé sous la forme `prenom.nom` (suffixe 2, 3… s'il est pris).

Chacun modifie son profil via **Mon compte** dans l'en-tête ; changer d'adresse e-mail demande le mot de passe actuel (l'adresse permet de réinitialiser le mot de passe). Les comptes antérieurs à ces champs sont conservés tels quels, marqués « profil incomplet » dans la liste et signalés par une pastille sur « Mon compte ».

### Mots de passe

Politique appliquée à tout nouveau mot de passe (recommandation CNIL pour un mot de passe seul) : **au moins 12 caractères** (`PASSWORD_MIN_LENGTH`), avec **une minuscule, une majuscule, un chiffre et un caractère spécial** ; ne contenant ni l'identifiant, ni le prénom, le nom ou l'adresse e-mail ; ni mot de passe courant (« Motdepasse2026! »), ni caractère répété 4 fois. Les règles s'affichent et se cochent pendant la saisie ; le serveur les revérifie. Un compte existant garde son mot de passe jusqu'au prochain changement.

À la création d'un compte, l'administrateur choisit :

- **Invitation par e-mail** : la personne reçoit un lien (valable `INVITE_DAYS`, 7 jours) pour choisir elle-même son mot de passe ;
- **Mot de passe provisoire** (bouton « Générer ») : à lui transmettre ; coché par défaut, il devra être changé à la première connexion. Tant que ce n'est pas fait, l'API refuse tout le reste et l'interface impose le changement.

Depuis la liste, le bouton **Mot de passe…** renvoie l'invitation, envoie un lien de réinitialisation ou définit un nouveau mot de passe provisoire. Des étiquettes signalent les invitations en attente ou expirées et les mots de passe provisoires.

**Mot de passe oublié** : lien dans la fenêtre de connexion. L'utilisateur saisit son identifiant ou son adresse e-mail et reçoit un **mot de passe provisoire** valable `RESET_TOKEN_MINUTES` (60 min). Il s'ajoute au mot de passe actuel sans le remplacer (une demande faite par quelqu'un d'autre ne bloque pas le compte) ; à la première connexion avec lui, il devient le mot de passe du compte et l'utilisateur doit immédiatement en choisir un nouveau. Se connecter avec l'ancien mot de passe annule le provisoire. La réponse est la même que le compte existe ou non, la recherche et l'envoi ont lieu après la réponse, et un compte ne reçoit pas plus d'un envoi toutes les 2 minutes. Un administrateur peut aussi déclencher cet envoi depuis la liste des comptes.

Les liens pointent vers `/mot-de-passe.html#token=…` : le jeton est dans le fragment, donc jamais envoyé au serveur dans l'URL (absent des logs du reverse proxy et de l'en-tête Referer). Seule son empreinte SHA-256 est stockée ; il est invalidé dès que le mot de passe change. Choisir un mot de passe par ce lien ferme toutes les sessions du compte et connecte le navigateur.

### Import CSV

**`/admin.html` → Utilisateurs → Importer** crée des comptes en masse. Un [modèle](static/modele-import-utilisateurs.csv) est téléchargeable depuis la page.

| Colonne | | Contenu |
|---|---|---|
| `nom`, `prenom`, `email` | obligatoires | |
| `telephone` | facultatif | |
| `role` | facultatif | `visualisation` ou `administration` ; vide : rôle par défaut choisi à l'import |
| `identifiant` | facultatif | vide : `prenom.nom` |
| `structure` | facultatif | nom d'une structure existante, super administrateur uniquement ; vide : structure par défaut choisie à l'import |

Séparateur point-virgule, virgule ou tabulation ; UTF-8 ou export Excel (Windows-1252) ; en-têtes insensibles à la casse et aux accents (`Prénom`, `E-mail`, `Téléphone`, `mail`, `courriel`, `tel`… sont reconnus), colonnes inconnues ignorées. 500 comptes et 512 Ko au plus par fichier.

L'import se fait en deux temps : **analyse** (chaque ligne affichée avec l'identifiant qui sera créé, ou ses erreurs : e-mail invalide, doublon dans le fichier ou avec un compte existant, rôle ou structure inconnus), puis **confirmation**, qui crée les lignes valides en une transaction (les lignes en erreur sont ignorées). Mots de passe : invitation par e-mail pour chacun, ou mots de passe provisoires générés, proposés **une seule fois** en téléchargement CSV (à transmettre individuellement puis à supprimer). Un administrateur de structure n'importe que dans la sienne ; l'import ne crée jamais de super administrateur.

### E-mails

Invitations et mot de passe oublié nécessitent l'envoi d'e-mails. Sans configuration (`MAIL_BACKEND=none`), ils sont masqués dans l'interface : seul le mot de passe provisoire est proposé.

| Variable | Défaut | Description |
|---|---|---|
| `MAIL_BACKEND` | `none` | `none`, `console` (messages écrits dans les logs du conteneur `api`, pour tester) ou `smtp` |
| `APP_BASE_URL` | — | URL publique, ex. `https://calendive.fr` (**obligatoire** : les liens ne sont jamais construits à partir de l'en-tête `Host`, falsifiable) |
| `APP_NAME` | `Calendive` | Nom affiché dans les e-mails |
| `SMTP_HOST`, `SMTP_PORT` | —, `587` | Serveur d'envoi |
| `SMTP_SECURITY` | `starttls` | `starttls` (587), `ssl` (465) ou `none` |
| `SMTP_USER`, `SMTP_PASSWORD` | — | Authentification (facultative) |
| `MAIL_FROM` | `SMTP_USER` | Expéditeur, ex. `Calendive <no-reply@calendive.fr>` |
| `INVITE_DAYS` | `7` | Validité d'un lien d'invitation |
| `RESET_TOKEN_MINUTES` | `60` | Validité d'un lien de réinitialisation |

Pour essayer sans serveur SMTP : `MAIL_BACKEND=console` et `APP_BASE_URL=http://localhost:8000`, puis `docker compose logs -f api` pour lire les e-mails et leurs liens.

Pour les voir comme dans une vraie boîte de réception, le service **MailDev** (profil `dev`) capture tout ce que l'application envoie, sans rien délivrer :

```bash
# .env : MAIL_BACKEND=smtp, APP_BASE_URL=http://localhost:8000,
#        SMTP_HOST=maildev, SMTP_PORT=1025, SMTP_SECURITY=none, SMTP_USER= , SMTP_PASSWORD=
docker compose --profile dev up -d
# puis http://localhost:1080 (MAILDEV_WEB_PORT)
```

### Ligne de commande

Premier super administrateur (sans structure) et dépannage :

```bash
# Docker
docker compose run --rm --entrypoint python api -m app.auth create-admin jerome \
    --email jerome@example.fr --first-name Jérôme --last-name "Le Goff"
# Sans Docker
python -m app.auth create-admin jerome --email jerome@example.fr --first-name Jérôme --last-name "Le Goff"

# Changer un mot de passe (identifiant ou e-mail), lister les comptes
python -m app.auth set-password jerome
python -m app.auth list
```

Sécurité : mots de passe hachés avec scrypt (bibliothèque standard), session dans un cookie `HttpOnly` / `SameSite=Lax` dont seule l'empreinte SHA-256 est stockée en base. Changer un mot de passe ferme les sessions ouvertes du compte et invalide ses liens en cours. **Derrière HTTPS, mettre `COOKIE_SECURE=1`.**

| Variable | Défaut | Description |
|---|---|---|
| `COOKIE_SECURE` | `0` | `1` : cookie de session envoyé uniquement en HTTPS |
| `SESSION_DAYS` | `30` | Durée de validité d'une connexion |
| `PASSWORD_MIN_LENGTH` | `12` | Longueur minimale des nouveaux mots de passe (8 au minimum) |

## Créneaux choisis

Un administrateur de structure peut **choisir des créneaux** pour sa structure dans le tableau de recherche : la colonne « Choix » propose une liste déroulante de **types** (ex. « Sortie bateau », « Formation N2 »). Le créneau choisi est aussitôt **grisé** dans le tableau, avec son type ; la croix annule le choix. Le filtre de la colonne permet de n'afficher que les créneaux choisis ou non choisis.

Les membres en visualisation voient les créneaux déjà choisis grisés, avec leur type, sans pouvoir les modifier.

La page **`/mes-creneaux.html`** (lien « Créneaux choisis » dans l'en-tête) liste les créneaux de la structure par date, avec qui les a choisis : filtre par type, créneaux passés masqués par défaut ; en administration, changement de type et retrait (le créneau redevient disponible dans la recherche).

Le **calendrier** de cette page montre aussi les **jours fériés** (numéro et nom en corail ; nom masqué sur petit écran) et les **vacances scolaires** de l'académie configurée (`SCHOOL_ACADEMY`, filet violet horizontal en haut de la case, sans changer sa couleur), avec une légende ; le détail du jour les nomme. Ils viennent de `GET /api/calendar-days?start=…&end=…` (62 jours au plus, sans connexion).

### Indisponibilités

Les administrateurs d'une structure déclarent des **plages d'indisponibilité** (bateau au carénage, congés du club…) dans `/admin.html` → **Créneaux** → *Indisponibilités*. La plage vaut pour **tous les lieux** de la structure. Pendant une plage, **aucun créneau ne peut être choisi** dans la recherche, ni créé ou déplacé (créneau personnalisé) : l'API refuse avec le motif.

- **Plage** : du jour… (« au » facultatif : un seul jour), avec des heures facultatives : sans heure, journées entières ; « à partir de 14:00 » le premier jour, « jusqu'à 12:00 » le dernier (heure de fin exclue : un RDV à 12:00 reste possible). Motif facultatif (80 caractères). Durée : un an au plus.
- **Règle** : un créneau est bloqué si sa période **touche** la plage : du rendez-vous à l'étale pour une étale (un RDV pris pendant la plage suffit), l'heure de RDV pour un créneau personnalisé, tout le séjour pour un créneau sur plusieurs jours.
- **Créneaux déjà choisis** dans une nouvelle plage : ils sont **conservés**, inscrits compris. L'administration les liste à l'enregistrement de la plage ; à l'administrateur de les retirer s'il le souhaite.
- **Affichage** : dans la recherche, les étales concernées sont hachurées et marquées « Indisponible · motif », sans liste de choix. Dans « Créneaux choisis », un bandeau rappelle les plages à venir et les jours concernés sont hachurés dans le calendrier.
- Modification et suppression depuis la même liste ; les plages passées sont masquées (case « Afficher les plages passées »).

### Choisir tous les créneaux affichés

Dans la recherche, un administrateur de la structure peut **choisir d'un coup tous les créneaux affichés** : bouton **« Choisir les N créneaux affichés… »** au-dessus du tableau. Les **filtres de colonnes comptent** : on filtre d'abord (ex. étale PM, coefficient ≤ 80, RDV après 9 h), puis on choisit ce qui reste.

- Une fenêtre récapitule la période et le nombre de créneaux, puis demande le **type** (le même pour tous) et un **intitulé** facultatif commun.
- Sont **ignorées** : les étales déjà choisies par la structure (pas de doublon) et celles d'une plage d'indisponibilité. La fenêtre les compte, le serveur les écarte de toute façon.
- Après le choix, **« Annuler ce choix groupé »** retire d'un clic les créneaux qui viennent d'être créés.
- 500 créneaux au plus d'un coup (`POST /api/selections/bulk`) ; au-delà, réduire la période ou filtrer.

### Plusieurs créneaux sur une même étale

Une même étale peut porter **plusieurs créneaux choisis** (deux bateaux, une sortie et une formation…). Chacun a son type, son **intitulé** facultatif (80 caractères, ex. « Bateau 1 », « Bateau 2 »), ses inscrits et ses places.

- **Recherche** : une fois un créneau choisi, la cellule « Choix » liste les créneaux de l'étale (type, intitulé, croix pour retirer) et propose **« + Autre… »**. Ajouter un créneau à une étale déjà choisie ouvre une fenêtre pour saisir l'intitulé, conseillé quand le type est le même.
- **Créneaux choisis** : le bouton **« Modifier »** d'un créneau d'étale change son intitulé (la fenêtre rappelle les autres créneaux de la même étale) ; l'heure et le lieu restent ceux de l'étale.
- Quand une étale est recalculée (mois glissant api-maree.fr, nouveau calcul), **tous** ses créneaux suivent la nouvelle heure.
- **Mise à jour d'une base existante** : la migration n° 4 retire la contrainte « un seul choix par étale » ; créneaux et inscriptions sont conservés.

### Places limitées et file d'attente

Le nombre d'inscrits d'un créneau peut être **limité**. Au-delà, les inscriptions passent en **file d'attente**.

- **Par défaut : illimité.** Les administrateurs de la structure renseignent un **nombre de places par défaut** dans `/admin.html` → **Créneaux** → *Places*. Il est **copié sur chaque nouveau créneau** à sa création : le modifier ne touche pas les créneaux existants, et aucun inscrit ne change de statut à son insu.
- **Par créneau** : bouton **« Places… »** d'un créneau à venir (administration). Vide : illimité. Le nombre se règle aussi à la création d'un créneau personnalisé (champ prérempli avec la valeur par défaut) ; un créneau choisi depuis la recherche reprend la valeur par défaut.
- **Ordre d'inscription.** Les *N* premiers inscrits sont confirmés, les suivants attendent dans l'ordre. Le statut est **calculé à chaque lecture**, jamais stocké : il ne peut pas se désynchroniser. Se réinscrire après s'être désinscrit remet en fin de file.
- **Une place se libère** (désinscription, retrait par un administrateur, ou places ajoutées) : le premier de la file est confirmé et **prévenu par e-mail**, si l'envoi d'e-mails est configuré. Si le membre qui part est **retiré de la structure ou son compte supprimé**, le suivant est bien confirmé, mais **sans e-mail** : il le voit à sa prochaine visite. **Réduire** le nombre de places remet en file d'attente les derniers inscrits, sans e-mail (la boîte de dialogue annonce l'effet avant d'enregistrer).
- **Inscrire d'autres membres** respecte aussi les places : au-delà, ils sont placés en file d'attente (leur e-mail le dit, avec leur rang).
- **Affichage** : « 3/8 » (confirmés / places), « +2 » pour la file, « complet » ; le bouton devient « Rejoindre la file d'attente » ; l'info-bulle des inscrits sépare confirmés et file. L'export Excel ajoute les colonnes *Places* et *File d'attente*.
- **Newsletters** : l'audience « inscrits au créneau » ne contient que les inscrits **confirmés**.
- **Délais** : ils s'appliquent comme avant aux places confirmées. **Quitter la file d'attente reste possible après le délai de désinscription**, puisqu'on n'y occupe aucune place. Un membre promu après ce délai ne peut plus se désinscrire lui-même : son e-mail l'invite à prévenir un administrateur.
- Aucun e-mail n'est envoyé pour un créneau **passé** (inscription retirée ou places changées après coup).
- Limites : de 1 à 500 places.

### Inscrire d'autres membres

Les membres s'inscrivent eux-mêmes sur les créneaux à venir, dans les délais fixés par la structure. Un administrateur de la structure, ou un compte ayant le profil **Inscriptions**, peut aussi **inscrire d'autres membres** : bouton **« + Inscrire… »** de chaque créneau à venir, qui ouvre la liste des membres pas encore inscrits (recherche, plusieurs à la fois). Il peut aussi retirer une inscription (croix dans la liste des inscrits, au clic sur leur nombre).

- Les délais d'inscription et de désinscription ne s'appliquent pas à ces opérations.
- La liste des inscrits et l'export Excel indiquent **« inscrit par … »** quand ce n'est pas le membre lui-même (le nom est conservé même si ce compte est supprimé ensuite).
- Le membre inscrit reçoit un **e-mail** (créneau, RDV, lien vers ses créneaux), s'il a une adresse et si l'envoi d'e-mails est configuré. Il peut ensuite se désinscrire dans les délais habituels.

### Créneaux personnalisés

Un administrateur de structure peut aussi ajouter un créneau **en dehors des étales proposées** par la recherche (plongée de l'après-midi, sortie de nuit, épave à heure fixe…) : bouton **« + Créneau personnalisé »** de la page des créneaux choisis. Il saisit le lieu, le jour, l'**heure de rendez-vous**, le type et un **intitulé** facultatif (80 caractères, ex. « Épave du Pélican »). Le lieu est un port de la liste, ou **« Autre lieu… »** pour une sortie ailleurs (carrière, fosse, ville à l'étranger…) : un champ libre de 80 caractères apparaît alors, et ce lieu s'affiche partout à la place du port.

Pour un **séjour de plusieurs jours** (voyage, stage), le champ facultatif **« Jusqu'au »** fixe le dernier jour (60 jours au plus). Le séjour figure sur chacun de ses jours dans le calendrier (l'heure de RDV le premier jour, une flèche les suivants), avec la mention « Du … au … · N jours » ; dans la liste et l'export Excel (colonne « Date de fin »), il apparaît à son premier jour. Les délais d'inscription et de désinscription comptent depuis le premier jour, et le séjour reste parmi les créneaux à venir jusqu'à son dernier jour.

Le créneau apparaît avec les autres, marqué **Perso**, sans étale, hauteur ni coefficient ; l'intitulé s'affiche sous le port (et à la place du port dans le calendrier). Les membres s'y inscrivent comme sur tout créneau, avec les mêmes délais. Les administrateurs peuvent le **modifier** (lieu, jour ou plage de jours, heure, intitulé ; le type se change dans la liste comme pour les autres) ou le retirer. Son heure de RDV est celle saisie : un changement du délai de rendez-vous de la structure ne la modifie pas. Rien n'empêche deux créneaux personnalisés identiques (deux palanquées, deux sorties le même jour).

### Export Excel

La recherche et la page des créneaux choisis ont un bouton **« Exporter en Excel »** : il télécharge un fichier `.xlsx` des créneaux **affichés**, filtres compris (filtres de colonnes dans la recherche ; type, « mes inscriptions » et créneaux passés dans les créneaux choisis). Dates, heures, hauteurs et coefficients y sont de vraies valeurs Excel, triables et filtrables ; l'en-tête est figé et porte un filtre automatique. Le fichier est généré dans le navigateur (`static/xlsx-export.js`, sans dépendance ni appel serveur).

### Agenda du téléphone (Android, iPhone)

Les créneaux choisis peuvent aller dans le calendrier du téléphone ou de l'ordinateur :

- **Un créneau** : bouton **« Agenda »** sur chaque créneau (page des créneaux choisis) : fichier `.ics` de ce seul créneau (`GET /api/selections/{id}.ics`). Sur iPhone, Safari propose directement de l'ajouter ; sur Android, le fichier s'ouvre dans l'appli d'agenda.
- **Abonnement** : **« Mon agenda »** dans le menu du compte, une section par structure du compte. Au choix : **ses inscriptions** (confirmées ou en file d'attente) ou **tous les créneaux**, éventuellement d'un seul type ; créneaux passés depuis deux mois et tous ceux à venir. Un **lien personnel et secret** (`/api/calendar/<jeton>.ics`, lu sans session) que le calendrier relit tout seul : les créneaux ajoutés, déplacés ou retirés suivent. iPhone / Mac : « Ouvrir dans le calendrier » (`webcal://`) ; Android : Google Agenda → « Autres agendas » → « À partir de l'URL ». Un lien par compte et par structure (`GET /api/me/calendar-feeds`, `POST` / `DELETE /api/me/calendar-feeds/{structure_id}`) ; le recréer remplace l'ancien, qui cesse de marcher ; le lien ne marche plus si le compte quitte la structure. Seul le SHA-256 du jeton est stocké (table `calendar_feeds`) : le lien n'est affiché qu'à sa création. La même section propose aussi un **fichier `.ics`** de la structure, mêmes choix (`GET /api/selections.ics?structure_id=…&mine=…&type_id=…`).
- Événements : du RDV à une heure après l'étale ; du RDV à la fin de la plage (hauteur d'eau) ; RDV + 3 h (créneau personnalisé), journées entières pour un séjour. Heures en UTC, titre « type — intitulé · lieu », description avec l'étale ou la plage, les places et l'inscription du membre.
- Les liens utilisent **`APP_BASE_URL`** (à renseigner en production, en `https://`) ; à défaut, l'adresse de la requête.

Règles :

- les choix sont **propres à chaque structure** et communs à ses membres : deux structures peuvent choisir le même créneau, mais une structure ne peut pas le choisir deux fois (contrainte `UNIQUE (structure_id, port_id, ts_utc)` en base) ; supprimer un compte ne supprime pas les créneaux qu'il a choisis ;
- un créneau est identifié par son port et l'horodatage UTC de l'étale (un créneau personnalisé n'en a pas : `ts_utc` est vide) ; heure, hauteur, coefficient et RDV sont **recalculés par le serveur** au moment du choix puis figés (seule exception : un changement du délai de rendez-vous de la structure recalcule le RDV de ses créneaux à venir) ;
- chaque structure a sa liste de types, gérée dans **`/admin.html` → Types de créneaux** : libellé, couleur, ordre, « proposé » ou non. Un type non proposé reste affiché sur les choix existants ; un type utilisé ne peut pas être supprimé.

Tant qu'aucun type n'existe, la colonne « Choix » affiche « aucun type ».

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
| `POST /api/auth/login` | public | `{username, password}` (`username` : identifiant ou e-mail) → cookie de session |
| `POST /api/auth/logout` | public | ferme la session |
| `GET /api/auth/me` | public | `{user}` ou `{user: null}` |
| `GET /api/auth/config` | public | `{password_reset, password_policy}` : mot de passe oublié disponible, règles des mots de passe |
| `POST /api/auth/forgot-password` | public | `{login}` → 202 dans tous les cas ; 404 si l'envoi d'e-mails n'est pas configuré |
| `POST /api/auth/token-info` | public | `{token}` → compte et usage (`invite` / `reset`) du lien ; 400 s'il est expiré ou utilisé |
| `POST /api/auth/reset-password` | public | `{token, new_password}` → mot de passe changé, session ouverte |
| `PATCH /api/me/profile` | connecté | `{first_name?, last_name?, email?, phone?, current_password?}` (mot de passe requis pour changer d'e-mail) |
| `POST /api/me/password` | connecté | `{current_password, new_password}` |
| `GET` / `PUT` / `DELETE /api/me/preferences` | connecté | `{form, filters}` |
| `GET` / `POST /api/admin/users` | admin. structure / super admin | liste (`?structure_id=` pour le super admin) / création `{first_name, last_name, email, phone?, username?, send_invite, password?, must_change_password?, role, structure_id?, is_admin?}` |
| `PATCH` / `DELETE /api/admin/users/{id}` | admin. structure / super admin | `{first_name?, last_name?, email?, phone?, password?, must_change_password?, role?, structure_id?, is_admin?}` / suppression |
| `POST /api/admin/users/{id}/send-link` | admin. structure / super admin | renvoie l'invitation, ou envoie un lien de réinitialisation |
| `POST /api/admin/users/import` | admin. structure / super admin | `{content_b64, dry_run, mode: invite\|password, role, structure_id?}` : analyse ou création depuis un CSV |
| `GET /api/admin/structures` | admin. structure / super admin | structures avec effectifs (la sienne seulement pour un admin. de structure) |
| `POST /api/admin/structures` | super admin | `{name}` |
| `PATCH` / `DELETE /api/admin/structures/{id}` | super admin | `{name}` / suppression (409 s'il reste des membres) |
| `POST /api/structure-requests` | public | demande de création `{structure_name, city?, contact_name, email, phone?, message?, consent, website?}` (`website` : champ piège, à laisser vide) ; 429 au-delà des plafonds |
| `GET /api/admin/structure-requests` | super admin | demandes, en attente d'abord |
| `POST /api/admin/structure-requests/{id}/create-structure` | super admin | `{name?}` : crée la structure et classe la demande (409 si le nom existe) |
| `PATCH` / `DELETE /api/admin/structure-requests/{id}` | super admin | `{status: new \| done \| rejected}` / suppression |
| `GET /api/admin/profiles` | admin. structure / super admin | catalogue des profils `[{id, label, description}]` ; `profiles: [...]` dans la création et la modification de compte |
| `GET` / `PUT` / `DELETE /api/admin/mailjet` | admin. structure / super admin (`?structure_id=`) | état de la connexion (jamais les clés) / `{api_key?, api_secret?, sender_email, sender_name}` (clés : les deux, ou aucune pour les garder) / déconnexion |
| `POST /api/admin/mailjet/test` | idem | vérifie les clés et la validation de l'adresse d'expédition ; résultat enregistré |
| `POST /api/admin/mailjet/test-email` | idem | e-mail de test à l'adresse du compte connecté |
| `POST /api/admin/mailjet/events` | idem | active le suivi : adresse de suivi déclarée chez Mailjet |
| `GET` / `POST /api/newsletters` | gestionnaire | liste (avec statistiques) / création d'un brouillon `{subject, preheader?, body, audience: {kind: all \| managers \| viewers \| selection \| group, selection_id?, group_id?}}` |
| `GET` / `PATCH` / `DELETE /api/newsletters/{id}` | gestionnaire | détail / modification d'un brouillon / suppression (409 pendant l'envoi) |
| `GET /api/newsletters/settings`, `/audiences`, `/unsubscribes`, `/members` | gestionnaire | état de Mailjet et du suivi / audiences avec leurs effectifs / désinscrits / comptes de la structure |
| `GET` / `POST /api/newsletters/groups`, `PUT` / `DELETE /api/newsletters/groups/{id}` | gestionnaire | groupes d'envoi `{name, description?, member_ids}` (`member_ids` absent en modification : membres inchangés) ; audience `{kind: "group", group_id}` |
| `POST /api/newsletters/preview` | gestionnaire | rendu `{subject, html, text}` personnalisé avec le compte connecté |
| `POST /api/newsletters/{id}/test`, `/send`, `/schedule`, `/unschedule`, `/resume`, `/duplicate` | gestionnaire | test à soi / envoi / programmation `{at}` (ISO avec fuseau, ou heure de Paris) / annulation / reprise après échec / copie |
| `GET /api/newsletters/{id}/report` | gestionnaire | statistiques, destinataires, liens cliqués |
| `GET` / `POST /api/newsletters/unsubscribe/{jeton}` | public | désinscription par lien personnel (POST : aussi « en un clic », RFC 8058) |
| `GET` / `PUT /api/me/newsletters` | connecté | abonnement aux newsletters de sa structure `{subscribed}` |
| `POST /api/mailjet/events/{jeton}` | Mailjet | événements de suivi (lot ou événement seul) ; 200 même pour un événement ignoré, 404 si le jeton est inconnu |

Tant qu'un compte a un mot de passe provisoire (`must_change_password`), toutes les routes connectées répondent 403 (en-tête `X-Password-Change-Required: 1`) sauf `/api/auth/me`, `/api/auth/config`, `/api/auth/logout` et `/api/me/password`.

`GET /api/auth/me` renvoie aussi `structure`, `role` et `can` (`super_admin`, `admin_area`, `manage_structure`, `pick`, `view_selections`). `structure_id` et `is_admin` ne sont modifiables que par un super administrateur ; un administrateur de structure agit toujours sur la sienne.

### Créneaux choisis

| Route | Accès | Description |
|---|---|---|
| `GET /api/slot-types` | connecté | types proposés (actifs) de sa structure, dans l'ordre |
| `GET /api/selections` | membre d'une structure | créneaux de la structure ; `?upcoming=true` : à partir d'aujourd'hui |
| `POST /api/selections` | admin. structure | `{port_id, ts_utc, type_id}` ; 409 si déjà choisi par la structure |
| `POST /api/selections/custom` | admin. structure | créneau personnalisé `{port_id \| location, date, end_date?, time, type_id, note?}` : un port **ou** un lieu libre (`location`, 80 caractères), jamais les deux ; `end_date` : dernier jour d'un séjour (après `date`, 60 jours au plus) ; `time` : heure de RDV `HH:MM` du premier jour |
| `PATCH` / `DELETE /api/selections/{id}` | admin. structure | `{type_id?, port_id?, location?, date?, end_date?, time?, note?}` (`end_date: null` : un seul jour) (lieu, jour, heure et intitulé : créneau personnalisé uniquement, 422 sinon ; un `port_id` remplace le lieu libre et inversement) / retrait |
| `POST` / `DELETE /api/selections/{id}/registration` | membre d'une structure | s'inscrire / se désinscrire, dans les délais de la structure (409 sinon) |
| `GET /api/selections/members` | admin. structure ou profil Inscriptions | membres de la structure `{id, username, display_name, role}` |
| `POST /api/selections/{id}/registrations` | admin. structure ou profil Inscriptions | inscrire des membres `{user_ids}` sur un créneau à venir, délais non compris (déjà inscrits ignorés ; 422 hors structure) ; réponse : le créneau, plus `added` |
| `DELETE /api/selections/{id}/registrations/{user_id}` | admin. structure ou profil Inscriptions | retirer l'inscription d'un membre, même sur un créneau passé |
| `GET` / `POST /api/admin/slot-types` | admin. structure / super admin | liste (avec nombre d'usages) / création `{label, color, active}` |
| `PATCH` / `DELETE /api/admin/slot-types/{id}` | admin. structure / super admin | modification / suppression (409 si utilisé) |
| `PUT /api/admin/slot-types/order` | admin. structure / super admin | `{ids}` : nouvel ordre complet |

Routes `/api/admin/slot-types` : un super administrateur précise la structure par `?structure_id=` (à défaut, la sienne).

Chaque résultat de `/api/dive-windows` contient `port_id` et `ts_utc`, la clé à envoyer pour choisir le créneau. Dans `/api/selections`, `custom: true` signale un créneau personnalisé : `ts_utc`, `kind`, `time`, `height_m` et `coefficient` y valent `null`, `note` porte l'intitulé. Pour un créneau dans un autre lieu, `port_id` vaut `null` et `location` porte le lieu ; `port` contient toujours le lieu à afficher (nom du port, ou lieu libre). `end_date` porte le dernier jour d'un séjour, sinon `null`.

### Administration des données

Réservée au super administrateur.

| Méthode et route | Rôle |
|---|---|
| `GET /api/admin/status` | modèle FES, worker, vacances scolaires |
| `GET` / `POST /api/admin/ports` | liste (avec années calculées) / création |
| `GET /api/admin/ports/catalog` | ports du catalogue pas encore en base |
| `PATCH` / `DELETE /api/admin/ports/{id}` | modification / suppression avec ses données |
| `DELETE /api/admin/ports/{id}/calibration` | abandon du recalage api-maree.fr du port |
| `GET` / `POST /api/admin/jobs` | liste / mise en file `{kind, params}` ; `kind` : `precompute` (`port_id`, `year`), `calibrate` (`port_id`), `short_term` (`port_id`), `fetch_models` (`model`), `school_holidays` |
| `POST /api/admin/jobs/annual` | `{year}` : un précalcul par port annuel |
| `GET /api/admin/jobs/{id}` | détail avec journal |
| `POST /api/admin/jobs/{id}/cancel` | annulation |

## Exploitation : sauvegarde, surveillance, sécurité

### Sauvegarde et restauration

Le planificateur sauvegarde la base **chaque jour à 03:30** (`python -m app.backup`) dans `data/backups/`, avec l'API de sauvegarde de SQLite : la copie est cohérente même pendant une écriture (une copie brute du fichier en mode WAL ne l'est pas). Chaque sauvegarde est vérifiée (`integrity_check`, clés étrangères) avant d'être compressée ; une sauvegarde invalide n'est jamais conservée. `BACKUP_KEEP` (défaut 14) règle le nombre d'exemplaires.

```bash
docker compose exec scheduler python -m app.backup                  # sauvegarde immédiate
docker compose exec scheduler python -m app.backup verify           # tester la dernière sauvegarde
# Restauration : arrêter ce qui écrit, restaurer, relancer (l'ancienne base est gardée à côté)
docker compose stop api worker scheduler
docker compose run --rm --no-deps --entrypoint python api -m app.backup restore data/backups/plongee-AAAAMMJJ-HHMMSS.db.gz
docker compose up -d
```

> **`data/backups/` est sur le même disque que la base** : il protège d'une fausse manipulation ou d'une corruption, pas de la perte du serveur. À copier **hors du serveur** (cron `rsync`/`rclone` sur l'hôte, snapshot du VPS…). Sauvegarder aussi **`SECRETS_KEY` à part** : sans elle, les clés Mailjet stockées en base sont illisibles. **Testez une restauration** sur une copie de temps en temps.

### Surveillance et alertes

`python -m app.health` (lancé chaque jour à 07:05 avec `--notify`) contrôle :

- l'année en cours et, à partir du 20 décembre, l'année suivante pour chaque port à précalcul automatique ; dès le 1er octobre, les **prérequis** du précalcul du 15 décembre (modèle FES complet, niveau moyen des ports) pour apprendre tôt qu'il échouera ;
- la cohérence des données stockées (mêmes contrôles que le précalcul) ;
- les tâches en échec des 3 derniers jours, la fraîcheur du mois glissant api-maree.fr et du recalage, la couverture des vacances scolaires ;
- le worker, la dernière sauvegarde (moins de 36 h) et l'espace disque.

Le résultat s'affiche en haut de l'administration (`GET /api/admin/health`). Un **e-mail** part aux administrateurs (`ALERT_EMAIL`, sinon les super administrateurs ; nécessite `MAIL_BACKEND=smtp`) quand une erreur **nouvelle** apparaît, avec un rappel hebdomadaire tant qu'elle dure et un message quand tout est rentré dans l'ordre. Une tâche en échec déclenche aussi un e-mail immédiat (au plus un par type de tâche et par jour). Sans e-mail configuré, tout est écrit dans les logs. `python -m app.health` seul sort avec le code 1 s'il y a une erreur, utilisable par une supervision externe ; `GET /healthz` répond 200 si la base est lisible (sonde du conteneur).

### Sécurité

- **Tentatives de connexion limitées** : 8 échecs par identifiant et 30 par adresse IP sur 15 minutes, puis erreur 429 avec `Retry-After` (même avec le bon mot de passe, pendant le blocage). Mot de passe oublié, liens de réinitialisation, demande de structure et désinscription ont aussi leurs limites. Compteurs en mémoire (remis à zéro au redémarrage) ; `RATE_LIMIT=0` les désactive. L'adresse IP vient de `X-Forwarded-For` côté Traefik : `TRUSTED_PROXY_HOPS` (1 par défaut, 0 sans proxy).
- **CORS fermé** : le site est servi par l'API elle-même. `CORS_ORIGINS` ouvre l'API à d'autres origines au cas par cas.
- **Origine contrôlée** : une requête `POST/PUT/PATCH/DELETE` d'un navigateur venant d'une autre origine est refusée (403), en plus du cookie `SameSite=Lax`.
- **En-têtes** : `Content-Security-Policy` stricte (aucun script en ligne n'est autorisé), `X-Frame-Options: DENY`, `X-Content-Type-Options`, `Referrer-Policy`. Pas de HSTS côté application : à poser sur Traefik.
- **Chaîne d'approvisionnement** : supercronic est vérifié par empreinte dans le `Dockerfile` ; Dependabot et `pip-audit` (CI) surveillent les dépendances.

### Migrations de schéma

La base est versionnée (`PRAGMA user_version`, `python -m app.migrations status`). Les anciens `ALTER TABLE` conditionnels de `db_schema.py` amènent toute base à la version 1 ; **toute nouvelle évolution** s'ajoute à `app/migrations.py` (une transaction par migration, sauvegarde automatique dans `data/backups/avant-migration/` avant la première en attente). Mode d'emploi en tête de ce fichier.

### Tests et intégration continue

```bash
pip install -r requirements-dev.txt
python -m pytest        # marées, créneaux, soleil, calendrier, sauvegarde, sécurité, santé, migrations, API
ruff check app tests
```

La CI (`.github/workflows/ci.yml`) lance lint, tests, `pip-audit` (informatif) et le build Docker à chaque pull request. Les tests n'ont besoin d'aucun fichier FES : les marées sont synthétiques (`tests/synthetic.py`).

## Précision et limites

FES est un modèle **océanique global** : il est moins précis dans les ports, baies et zones à géométrie complexe qu'un atlas régional (Ifremer/PREVIMER) ou que les constantes harmoniques du SHOM.

**Avant toute sortie réelle, vérifier les horaires contre une source officielle** : [maree.shom.fr](https://maree.shom.fr) ou [maree.info](https://maree.info). Pour les ports dotés d'un site [api-maree.fr](#recalage-sur-api-mareefr), le mois à venir en reprend les horaires, et le calcul FES y est recalé au-delà.

Choix techniques à connaître :

- **Pourquoi pas une API ?** api-maree.fr limite ses horaires à une fenêtre glissante J−30 / J+30, et les API SHOM ne permettent pas de récupération multi-mois gratuite. Un précalcul annuel exige un calcul local ; api-maree.fr sert seulement à le recaler.
- **Mémoire** : seules les 8 ondes principales (+ 2N2, requise pour l'inférence des ondes secondaires) sont chargées, sur une fenêtre de grille de ±0,5° autour du port. Charger tout FES provoque des OOM.
- **Courants FES2014 non requis** : seul le groupe « z » (hauteurs) est utilisé ; la définition pyTMD est réduite en conséquence.
- **Heure des étales** : la série est calculée au pas de 10 min, mais chaque PM/BM est affinée par interpolation parabolique sur les trois points qui l'entourent, puis arrondie à la minute. Un pas d'une minute donnerait le même résultat pour ~10 fois plus de calcul et de place en base. Un recalcul qui décale une étale de quelques minutes (≤ 20 min) y recale automatiquement les créneaux choisis.
- **pyTMD** : l'API bas niveau est utilisée plutôt que `tide_elevations()`, dont le comportement s'est révélé instable.

## Structure du projet

```
app/
  main.py           API FastAPI + service du frontend
  precompute.py     précalcul annuel (CLI)
  tide_model.py     hauteurs d'eau, extrema, coefficient (pyTMD)
  calibration.py    recalage de FES sur api-maree.fr, onde par onde (+ CLI)
  short_term.py     mois glissant repris chaque jour d'api-maree.fr (+ CLI)
  tide_reference.py client api-maree.fr
  checks.py         contrôles de cohérence des données d'une année (avant écriture et en surveillance)
  health.py         surveillance : données, tâches, sauvegarde, disque (+ CLI)
  alerts.py         alertes e-mail aux administrateurs
  backup.py         sauvegarde / vérification / restauration de la base (+ CLI)
  migrations.py     migrations de schéma versionnées (+ CLI)
  security.py       limitation des tentatives, en-têtes de sécurité, contrôle d'origine
  twilight.py       lever/coucher civil, crépuscule nautique (astral)
  ports_catalog.py  ports préréglés et leurs offset_zh_m
  db.py             accès SQLite : façade (le code écrit `db.fonction()`), DB_PATH
  db_core.py        connexion (get_conn) et réglages de l'application
  db_schema.py      schéma SQL, migrations historiques, init_db
  db_tides.py       ports, recalage, marées, soleil, vacances scolaires
  db_structures.py  structures
  db_users.py       comptes, sessions, jetons, préférences
  db_memberships.py appartenances à plusieurs structures, structure active, invitations
  db_jobs.py        file de tâches, worker
  db_selections.py  types de créneaux, créneaux choisis, inscriptions
  db_requests.py    demandes de création de structure
  db_newsletters.py connexion Mailjet, newsletters, groupes d'envoi
  auth.py           comptes, sessions, rôles, profil, préférences, administration des comptes (+ CLI)
  accounts.py       profil (normalisation), identifiant proposé, jetons et e-mails de compte
  passwords.py      politique de mots de passe et génération
  recovery.py       mot de passe oublié (provisoire par e-mail), invitations (liens à usage unique)
  user_import.py    import CSV de comptes
  mailer.py         envoi d'e-mails (SMTP ou console)
  structures.py     API des structures (super administrateur)
  memberships.py    invitations à rejoindre une structure (administrateurs et titulaire du compte)
  contact.py        demandes de création de structure : formulaire public, notification, administration
  mailjet.py        client de l'API Mailjet (clés, expéditeurs, Send API v3.1)
  mailjet_admin.py  connexion Mailjet d'une structure : saisie, test, e-mail de test, activation du suivi
  newsletters.py    newsletters : brouillons, audiences, test, envoi, programmation, rapport, désinscription, événements Mailjet
  newsletter_render.py  format de rédaction → e-mail HTML et texte
  newsletter_send.py    envoi par le worker (lots de 50, reprise) (+ CLI)
  secrets_store.py  chiffrement des secrets en base (SECRETS_KEY) (+ CLI generate)
  admin.py          API d'administration : ports, tâches, état des données
  selections.py     types de créneaux et créneaux choisis, par structure
  slots.py          description d'une étale (coefficient, RDV), partagée
  jobs.py           file de tâches et worker (+ CLI enqueue)
  calendar_fr.py    jours fériés et vacances scolaires
static/             frontend (index.html, app.js, style.css)
  admin.html/.js    administration (structures, ports, données, types de créneaux, comptes)
  mes-creneaux.*    créneaux choisis par la structure de l'utilisateur connecté
  xlsx-export.js    export Excel (.xlsx) des tableaux, généré dans le navigateur
  session.js        connexion, profil, mots de passe, droits et appels API, partagé par les pages
  mot-de-passe.*    choix du mot de passe depuis un lien d'invitation ou de réinitialisation
  demande-structure.*  formulaire public de demande de création de structure
  newsletters.*     newsletters (profil Gestionnaire) : liste, édition avec aperçu, rapport d'envoi
  desinscription.*  désinscription des newsletters par lien personnel
  modele-import-utilisateurs.csv  modèle d'import CSV
  logo.svg, logo-sombre.svg  logo Calendive (page d'agenda dont le bas est la mer ; point sable : l'étale)
  favicon.svg, favicon-32.png, apple-touch-icon.png  icônes (onglet, écran d'accueil)
  manifest.webmanifest, icon-*.png, sw.js, hors-ligne.html  application installable (PWA), page hors connexion
  fonts/            police Sora (logo et titres), SIL OFL, hébergée localement
docker/crontab      tâches périodiques mises en file par le scheduler
tests/              suite pytest (voir « Tests et intégration continue »)
.github/workflows/  CI
Dockerfile
docker-compose.yml
.env.example
data/plongee.db     base générée (non versionnée)
models/             fichiers FES (non versionnés, licence AVISO+)
```

Les fichiers FES sont soumis à la licence AVISO+ (indépendante de la licence de ce projet) : ne pas les redistribuer ni les versionner.

## Pistes

- Valider les sorties sur une année complète contre maree.info / l'annuaire SHOM.
- Renseigner les `offset_zh_m` manquants depuis les RAM du Shom.
- Intégrer l'atlas régional Ifremer/PREVIMER ([accès sur demande](https://marc.ifremer.fr/produits/atlas_de_composantes_harmoniques)) : format non lu nativement par pyTMD, seul `tide_model.py` serait à adapter.
- Ajouter les courants de marée pour qualifier chaque site au-delà du coefficient.
- Mode « deux plongées dans la journée ».
- Notifications push (place libérée, nouveau créneau) pour l'application installée.

## Contribuer

Voir [CONTRIBUTING.md](CONTRIBUTING.md). Pour signaler une faille de sécurité : [SECURITY.md](SECURITY.md).

## Licence

Copyright © Jérôme Sourdin et contributeurs.

Ce programme est un logiciel libre : vous pouvez le redistribuer et/ou le modifier selon les termes de la [GNU General Public License](LICENSE) telle que publiée par la Free Software Foundation, version 3 de la licence ou (à votre choix) toute version ultérieure.

Il est distribué dans l'espoir qu'il sera utile, mais **sans aucune garantie** ; voir le fichier [LICENSE](LICENSE) pour plus de détails.
