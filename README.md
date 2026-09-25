# Aide au choix de plongées

Application web qui croise, pour un port choisi :
- les horaires et coefficients de marée (calculés localement, sur un an entier, via [pyTMD](https://pytmd.readthedocs.io/) + modèle harmonique **FES2014/FES2022**),
- les horaires de crépuscule nautique et de lever/coucher civil (calcul astronomique via [astral](https://astral.readthedocs.io/), sans API externe),

pour proposer des créneaux de plongée selon des critères réglables (coefficient max, phase de marée préférée, marge autour de l'étale, exigence de lumière du jour).

## Pourquoi pas simplement l'API api-maree.fr ?

C'est une excellente API (basée sur les mêmes composantes harmoniques Ifremer/PREVIMER), mais ses endpoints d'horaires (`/tide-extrema`, `/sun-times`, `/water-levels`) sont limités à une fenêtre glissante **J-30 à J+30**. Pour précalculer un an d'un coup et ne plus dépendre du réseau, ce projet calcule les marées lui-même avec **pyTMD**, à partir d'un modèle harmonique téléchargé une fois pour toutes.

### ⚠️ À savoir sur la précision

pyTMD utilise ici un modèle **océanique global** (FES2014/2022, produit par CNES/LEGOS/Noveltis), pas l'atlas régional Ifremer/PREVIMER (250 m à 2 km de résolution) utilisé par api-maree.fr ni les constantes harmoniques officielles du SHOM. Un modèle global est moins précis dans les ports, baies et zones à géométrie compliquée.

**Avant une vraie sortie, vérifie toujours les horaires calculés contre une source officielle** : [maree.shom.fr](https://maree.shom.fr) (annuaire gratuit). Si tu observes un décalage systématique pour ton port (ex. +15 min sur les pleines mers), tu peux l'ajouter comme correction manuelle dans `tide_model.py`.

Si tu veux gagner en précision plus tard, l'atlas Ifremer/PREVIMER est accessible gratuitement sur demande : <https://marc.ifremer.fr/produits/atlas_de_composantes_harmoniques>. Le format n'est pas lu nativement par pyTMD ; il faudrait écrire un parseur pour en extraire amplitude/phase par point de grille et les injecter dans un moteur de prédiction harmonique générique (le pipeline de stockage de ce projet — `tide_extrema`, `sun_times` — resterait inchangé, seul `tide_model.py` serait à adapter).

## Installation

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Récupérer le modèle de marée (FES2014 ou FES2022)

1. Créer un compte gratuit sur <https://www.aviso.altimetry.fr> (rubrique "My Aviso+").
2. Demander l'accès au produit **FES2014 (ocean_tide)** ou **FES2022**, puis télécharger les fichiers NetCDF. Tu peux te limiter à la zone Manche/Atlantique si l'outil de téléchargement le permet, pour réduire la taille.
3. Placer les fichiers dans un dossier, par exemple `/data/tide_models/fes2014/`, en respectant l'arborescence attendue par pyTMD (voir [doc pyTMD - Getting Started](https://pytmd.readthedocs.io/en/latest/getting_started/Getting-Started.html)).
4. Définir les variables d'environnement :

```bash
export TIDE_MODEL_NAME=FES2014          # ou FES2022
export TIDE_MODEL_DIRECTORY=/data/tide_models
```

## Précalculer une année pour un port

```bash
# Port du catalogue intégré (voir app/ports_catalog.py)
python -m app.precompute --port "Binic" --year 2027

# Point personnalisé (un spot précis plutôt que le port d'attache)
python -m app.precompute --name "Caffa (Erquy)" --lat 48.646 --lon -2.478 --year 2027
```

Ce script calcule ~52 000 points de hauteur d'eau sur l'année, en déduit les pleines/basses mers et coefficients indicatifs, calcule le crépuscule nautique jour par jour, et stocke tout dans `data/plongee.db` (SQLite). Ça peut prendre plusieurs minutes par port selon la machine.

**À planifier une fois par an** (ex. fin décembre pour l'année suivante) via une tâche cron :

```cron
0 3 1 12 * cd /chemin/vers/plongee-app && venv/bin/python -m app.precompute --port "Binic" --year $(date -d '+1 year' +\%Y)
```

## Lancer l'application

```bash
uvicorn app.main:app --reload
```

Puis ouvrir <http://localhost:8000>.

## Structure du projet

```
app/
  db.py             schéma et accès SQLite
  tide_model.py     calcul des hauteurs d'eau + détection des extrema (pyTMD)
  twilight.py       crépuscule nautique / lever-coucher civil (astral)
  ports_catalog.py  liste de ports préréglés (Bretagne principalement)
  precompute.py     script de précalcul annuel (à lancer soi-même/cron)
  main.py           API FastAPI + service du frontend
static/             frontend (HTML/CSS/JS, aucune dépendance de build)
data/plongee.db     base SQLite générée par precompute.py (non versionnée)
```

## Prochaines pistes possibles

- Ajouter les courants de marée (l'atlas Ifremer en fournit aussi, jusqu'à 38 composantes) pour affiner la force du courant sur chaque site, au-delà du seul coefficient.
- Un mode "plusieurs plongées dans la journée" qui cherche deux étales.
- Export iCal des créneaux retenus.
