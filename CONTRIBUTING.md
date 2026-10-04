# Contribuer

Merci de l'intérêt porté au projet ! Les contributions sont bienvenues :
signalements de bugs, idées, documentation et code.

## Signaler un bug ou proposer une idée

Ouvrez une [issue](../../issues) en décrivant :

- ce que vous avez fait, ce que vous attendiez et ce qui s'est passé ;
- votre environnement (navigateur, système, Docker ou non…) ;
- des captures ou extraits de logs si utile — **sans** mots de passe, jetons
  ni données personnelles.

Pour une faille de sécurité, suivez plutôt [SECURITY.md](SECURITY.md).

## Proposer une modification

1. Forkez le dépôt et créez une branche : `git checkout -b ma-modification`
2. Faites des commits courts et explicites.
3. Vérifiez que l'application démarre et que votre changement fonctionne
   (voir le README pour l'installation), puis lancez `pip install -r requirements-dev.txt`,
   `ruff check app tests` et `python -m pytest` : la CI exécute les mêmes. Un correctif ou une
   fonction qui touche aux marées, aux créneaux ou aux droits d'accès s'accompagne d'un test.
   Une évolution du schéma de la base passe par `app/migrations.py` (mode d'emploi en tête de fichier).
4. Ouvrez une Pull Request en expliquant le changement et comment il a été testé.

Ne committez jamais de fichier `.env` réel, de base de données ni de secret.

## Licence

En contribuant, vous acceptez que votre contribution soit publiée sous la
licence du projet, la [GNU GPL v3 ou ultérieure](LICENSE).
