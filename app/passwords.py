"""
Politique de mots de passe, partagée par l'API, la ligne de commande et
(via /api/auth/config) le frontend qui affiche les règles en direct.

Règles par défaut (recommandation CNIL 2022 pour un mot de passe seul) :
  - au moins 12 caractères (PASSWORD_MIN_LENGTH, 8 au minimum) ;
  - au moins une minuscule, une majuscule, un chiffre et un caractère spécial ;
  - ne contient ni l'identifiant, ni le prénom, le nom ou l'adresse e-mail ;
  - n'est pas un mot de passe courant (« Motdepasse2026! ») ni une répétition
    (« aaaa »).

La politique ne s'applique qu'aux nouveaux mots de passe : un compte existant
garde le sien jusqu'à son prochain changement.
"""

from __future__ import annotations

import os
import re
import secrets
import unicodedata
from typing import Iterable

MIN_LENGTH = max(8, int(os.environ.get("PASSWORD_MIN_LENGTH", "12")))
MAX_LENGTH = 128

# Caractères spéciaux : tout ce qui n'est ni lettre ni chiffre (espace compris)
_CLASSES = [
    ("lower", "une minuscule", lambda c: c.islower()),
    ("upper", "une majuscule", lambda c: c.isupper()),
    ("digit", "un chiffre", lambda c: c.isdigit()),
    ("special", "un caractère spécial", lambda c: not c.isalnum()),
]

# Racines de mots de passe trop courants (comparées sans chiffres ni symboles)
_COMMON = {
    "password", "motdepasse", "azerty", "azertyuiop", "qwerty", "qwertyuiop", "soleil",
    "bonjour", "admin", "administrateur", "welcome", "bienvenue", "maree", "plongee",
    "plongeur", "binic", "saintquay", "abcdef", "abcabc", "iloveyou", "jetaime", "loulou",
    "doudou", "chouchou", "letmein", "changeme", "changeit", "secret", "motdepass", "passw",
}


def _fold(s: str) -> str:
    """Minuscules sans accents, pour comparer « Jérôme » et « jerome »."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c)
    )


def policy() -> dict:
    """Description envoyée au frontend (liste de contrôle affichée en direct)."""
    return {
        "min_length": MIN_LENGTH,
        "max_length": MAX_LENGTH,
        "classes": [{"key": k, "label": label} for k, label, _ in _CLASSES],
    }


def problems(password: str, personal: Iterable[str | None] = ()) -> list[str]:
    """Liste des règles non respectées (vide = mot de passe accepté)."""
    out: list[str] = []
    if len(password) < MIN_LENGTH:
        out.append(f"au moins {MIN_LENGTH} caractères")
    if len(password) > MAX_LENGTH:
        out.append(f"au plus {MAX_LENGTH} caractères")
    missing = [label for _, label, test in _CLASSES if not any(test(c) for c in password)]
    if missing:
        out.append("au moins " + ", ".join(missing))

    folded = _fold(password)
    for value in personal:
        if not value:
            continue
        # identifiant, prénom, nom, et chaque morceau de l'adresse e-mail (≥ 3 caractères)
        for part in re.split(r"[\s@._+-]+", _fold(value)):
            if len(part) >= 3 and part in folded:
                out.append("ne pas contenir votre nom, identifiant ou adresse e-mail")
                break
        else:
            continue
        break

    core = re.sub(r"[^a-z]", "", folded)
    if core in _COMMON or any(core.startswith(w) and len(core) - len(w) <= 2 for w in _COMMON if len(w) >= 6):
        out.append("ne pas être un mot de passe courant")
    if re.search(r"(.)\1{3,}", password):
        out.append("ne pas répéter 4 fois de suite le même caractère")
    return out


def error_message(issues: list[str]) -> str:
    return "Mot de passe trop faible : " + " ; ".join(issues) + "."


# Alphabet lisible pour les mots de passe générés : sans 0/O ni 1/l/I
_LOWER = "abcdefghjkmnpqrstuvwxyz"
_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_DIGITS = "23456789"
_SPECIAL = "-_!?@#%+="


def generate(length: int | None = None) -> str:
    """Mot de passe aléatoire conforme à la politique (au moins une lettre de chaque classe)."""
    length = max(length or 0, MIN_LENGTH + 2, 14)
    pools = [_LOWER, _UPPER, _DIGITS, _SPECIAL]
    chars = [secrets.choice(p) for p in pools]
    everything = "".join(pools)
    chars += [secrets.choice(everything) for _ in range(length - len(chars))]
    # mélange cryptographique (random.shuffle n'utilise pas secrets)
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    pw = "".join(chars)
    return pw if not problems(pw) else generate(length)
