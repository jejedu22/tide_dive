"""
Chiffrement des secrets enregistrés en base (clés API de services tiers,
par exemple Mailjet).

Les secrets sont chiffrés avec Fernet (AES-128-CBC + HMAC-SHA256, bibliothèque
`cryptography`) par la clé de la variable d'environnement SECRETS_KEY. Ils ne
sont jamais renvoyés par l'API : seul le serveur les déchiffre, au moment de
s'en servir. La base seule (sauvegarde, fichier copié) ne suffit donc pas à les lire.

Génération de la clé (à faire une fois, puis à mettre dans .env et à sauvegarder
À PART de la base) :

    python -m app.secrets_store generate

Perdre la clé ou la changer rend les secrets enregistrés illisibles : il faut
alors les saisir de nouveau, rien d'autre n'est perdu.
"""

from __future__ import annotations

import os
import sys

from cryptography.fernet import Fernet, InvalidToken

KEY_ENV = "SECRETS_KEY"


class SecretsError(RuntimeError):
    """Message lisible : clé absente, invalide, ou différente de celle d'origine."""


def _fernet() -> Fernet:
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        raise SecretsError(
            f"{KEY_ENV} n'est pas définie : générez une clé avec « python -m app.secrets_store generate » "
            "et ajoutez-la au .env du serveur."
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise SecretsError(
            f"{KEY_ENV} est invalide : générez-en une avec « python -m app.secrets_store generate »."
        ) from exc


def unavailable_reason() -> str | None:
    """Pourquoi le chiffrement est inutilisable, ou None s'il fonctionne."""
    try:
        _fernet()
    except SecretsError as exc:
        return str(exc)
    return None


def available() -> bool:
    return unavailable_reason() is None


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretsError(
            f"Secret illisible : {KEY_ENV} a changé depuis son enregistrement. Saisissez-le de nouveau."
        ) from exc


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args != ["generate"]:
        print("Usage : python -m app.secrets_store generate", file=sys.stderr)
        return 2
    print(Fernet.generate_key().decode("ascii"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
