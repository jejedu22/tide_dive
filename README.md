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
- [Comptes, structures et préférences](#comptes-structures-et-préférences)
- [Créneaux choisis](#créneaux-choisis)
- [API](#api)
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
- **1er de chaque mois, 04:00** (et au démarrage) : vacances scolaires.

### Variables d'environnement (`.env`)

| Variable | Défaut | Description |
|---|---|---|
| `AVISO_USERNAME`, `AVISO_PASSWORD` | — | Identifiants AVISO+ |
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
python -m app.jobs enqueue school-holidays
```

**Mise à jour d'une installation existante** : les ports déjà en base reçoivent automatiquement leur niveau moyen depuis le catalogue quand il y est connu, et sont cochés « annuel ». La variable `MAREE_PORTS` n'est plus utilisée : c'est la case de l'administration qui décide.

## Comptes, structures et préférences

L'application reste utilisable sans compte. Un compte permet d'accéder aux créneaux choisis par sa **structure** et d'**enregistrer ses préférences** : critères du formulaire (port, durée de la période, phase, coefficient max, marge, lumière) et filtres de la ligne de titre du tableau. Elles sont réappliquées à la connexion, puis une recherche est lancée automatiquement. La période est enregistrée comme une **durée** (« 13 jours à partir d'aujourd'hui »), pas comme des dates fixes. Sans préférence de port, la recherche propose le **port par défaut de la structure**, choisi par ses administrateurs dans **`/admin.html` → Créneaux**.

### Structures et rôles

Une **structure** (club, groupe…) regroupe des comptes, sa liste de **types de créneaux** et sa liste de **créneaux choisis**. Chaque compte appartient à une structure avec l'un de ces rôles :

| Rôle | Peut |
|---|---|
| **Visualisation** | voir la liste des créneaux choisis par sa structure (et les voir grisés dans la recherche) |
| **Administration** | en plus : choisir et retirer les créneaux de la structure, gérer ses membres (création, rôle, mot de passe, suppression) et ses types de créneaux |
| **Super administrateur** | tout : structures, ports, données et tâches, comptes et types de toutes les structures. Peut aussi appartenir à une structure (il y a alors les droits d'administration) |

Il n'y a pas d'inscription libre : les comptes sont créés sur **`/admin.html` → Utilisateurs**, par un super administrateur (dans n'importe quelle structure) ou par un administrateur de structure (dans la sienne, sans pouvoir créer de super administrateur). Garde-fous : on ne peut ni supprimer son propre compte, ni se retirer ses droits de super administrateur, ni changer son propre rôle de structure ; il reste toujours au moins un super administrateur ; une structure n'est supprimable qu'une fois vide de membres (ses types et créneaux choisis partent avec elle).

**Mise à jour d'une base existante** : au premier démarrage, les comptes, types et créneaux existants sont rattachés à une structure « Structure principale » ; les super administrateurs y sont en administration, **les autres comptes en visualisation** (à promouvoir si besoin). Si plusieurs comptes avaient choisi le même créneau, seul le premier choix est conservé.

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
| `APP_BASE_URL` | — | URL publique, ex. `https://maree.example.fr` (**obligatoire** : les liens ne sont jamais construits à partir de l'en-tête `Host`, falsifiable) |
| `APP_NAME` | `Marée` | Nom affiché dans les e-mails |
| `SMTP_HOST`, `SMTP_PORT` | —, `587` | Serveur d'envoi |
| `SMTP_SECURITY` | `starttls` | `starttls` (587), `ssl` (465) ou `none` |
| `SMTP_USER`, `SMTP_PASSWORD` | — | Authentification (facultative) |
| `MAIL_FROM` | `SMTP_USER` | Expéditeur, ex. `Marée <no-reply@example.fr>` |
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

### Créneaux personnalisés

Un administrateur de structure peut aussi ajouter un créneau **en dehors des étales proposées** par la recherche (plongée de l'après-midi, sortie de nuit, épave à heure fixe…) : bouton **« + Créneau personnalisé »** de la page des créneaux choisis. Il saisit le port, le jour, l'**heure de rendez-vous**, le type et un **intitulé** facultatif (80 caractères, ex. « Épave du Pélican »).

Le créneau apparaît avec les autres, marqué **Perso**, sans étale, hauteur ni coefficient ; l'intitulé s'affiche sous le port (et à la place du port dans le calendrier). Les membres s'y inscrivent comme sur tout créneau, avec les mêmes délais. Les administrateurs peuvent le **modifier** (port, jour, heure, intitulé ; le type se change dans la liste comme pour les autres) ou le retirer. Son heure de RDV est celle saisie : un changement du délai de rendez-vous de la structure ne la modifie pas. Rien n'empêche deux créneaux personnalisés identiques (deux palanquées, deux sorties le même jour).

### Export Excel

La recherche et la page des créneaux choisis ont un bouton **« Exporter en Excel »** : il télécharge un fichier `.xlsx` des créneaux **affichés**, filtres compris (filtres de colonnes dans la recherche ; type, « mes inscriptions » et créneaux passés dans les créneaux choisis). Dates, heures, hauteurs et coefficients y sont de vraies valeurs Excel, triables et filtrables ; l'en-tête est figé et porte un filtre automatique. Le fichier est généré dans le navigateur (`static/xlsx-export.js`, sans dépendance ni appel serveur).

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

Tant qu'un compte a un mot de passe provisoire (`must_change_password`), toutes les routes connectées répondent 403 (en-tête `X-Password-Change-Required: 1`) sauf `/api/auth/me`, `/api/auth/config`, `/api/auth/logout` et `/api/me/password`.

`GET /api/auth/me` renvoie aussi `structure`, `role` et `can` (`super_admin`, `admin_area`, `manage_structure`, `pick`, `view_selections`). `structure_id` et `is_admin` ne sont modifiables que par un super administrateur ; un administrateur de structure agit toujours sur la sienne.

### Créneaux choisis

| Route | Accès | Description |
|---|---|---|
| `GET /api/slot-types` | connecté | types proposés (actifs) de sa structure, dans l'ordre |
| `GET /api/selections` | membre d'une structure | créneaux de la structure ; `?upcoming=true` : à partir d'aujourd'hui |
| `POST /api/selections` | admin. structure | `{port_id, ts_utc, type_id}` ; 409 si déjà choisi par la structure |
| `POST /api/selections/custom` | admin. structure | créneau personnalisé `{port_id, date, time, type_id, note?}` (`time` : heure de RDV `HH:MM`) |
| `PATCH` / `DELETE /api/selections/{id}` | admin. structure | `{type_id?, port_id?, date?, time?, note?}` (port, jour, heure et intitulé : créneau personnalisé uniquement, 422 sinon) / retrait |
| `GET` / `POST /api/admin/slot-types` | admin. structure / super admin | liste (avec nombre d'usages) / création `{label, color, active}` |
| `PATCH` / `DELETE /api/admin/slot-types/{id}` | admin. structure / super admin | modification / suppression (409 si utilisé) |
| `PUT /api/admin/slot-types/order` | admin. structure / super admin | `{ids}` : nouvel ordre complet |

Routes `/api/admin/slot-types` : un super administrateur précise la structure par `?structure_id=` (à défaut, la sienne).

Chaque résultat de `/api/dive-windows` contient `port_id` et `ts_utc`, la clé à envoyer pour choisir le créneau. Dans `/api/selections`, `custom: true` signale un créneau personnalisé : `ts_utc`, `kind`, `time`, `height_m` et `coefficient` y valent `null`, `note` porte l'intitulé.

### Administration des données

Réservée au super administrateur.

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
- **Heure des étales** : la série est calculée au pas de 10 min, mais chaque PM/BM est affinée par interpolation parabolique sur les trois points qui l'entourent, puis arrondie à la minute. Un pas d'une minute donnerait le même résultat pour ~10 fois plus de calcul et de place en base. Un recalcul qui décale une étale de quelques minutes (≤ 20 min) y recale automatiquement les créneaux choisis.
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
  auth.py           comptes, sessions, rôles, profil, préférences, administration des comptes (+ CLI)
  accounts.py       profil (normalisation), identifiant proposé, jetons et e-mails de compte
  passwords.py      politique de mots de passe et génération
  recovery.py       mot de passe oublié (provisoire par e-mail), invitations (liens à usage unique)
  user_import.py    import CSV de comptes
  mailer.py         envoi d'e-mails (SMTP ou console)
  structures.py     API des structures (super administrateur)
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
  modele-import-utilisateurs.csv  modèle d'import CSV
docker/crontab      tâches périodiques mises en file par le scheduler
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
- Export iCal des créneaux retenus.

## Contribuer

Voir [CONTRIBUTING.md](CONTRIBUTING.md). Pour signaler une faille de sécurité : [SECURITY.md](SECURITY.md).

## Licence

Copyright © Jérôme Sourdin et contributeurs.

Ce programme est un logiciel libre : vous pouvez le redistribuer et/ou le modifier selon les termes de la [GNU General Public License](LICENSE) telle que publiée par la Free Software Foundation, version 3 de la licence ou (à votre choix) toute version ultérieure.

Il est distribué dans l'espoir qu'il sera utile, mais **sans aucune garantie** ; voir le fichier [LICENSE](LICENSE) pour plus de détails.
