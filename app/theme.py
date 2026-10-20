"""
Couleurs personnalisées d'une structure : couleur principale (en-tête des pages, titres de tableaux, texte des boutons
principaux) et couleur d'accent (boutons principaux, mises en avant). Elles s'appliquent aux membres connectés à la
structure (session.js, d'après user.structure.theme) et à ses pages publiques (adhésion, inscription des personnes
extérieures). Absentes : couleurs de Calendive.

Lisibilité garantie à l'enregistrement : le texte blanc de l'en-tête sur la couleur principale, et la couleur
principale (texte des boutons) sur la couleur d'accent, doivent avoir un contraste d'au moins 4,5:1 (WCAG AA).
"""

from __future__ import annotations

import re

DEFAULT_PRIMARY = "#073b4c"
DEFAULT_ACCENT = "#06d6a0"
MIN_CONTRAST = 4.5
HEX = re.compile(r"^#[0-9a-f]{6}$")


def clean(value: str | None) -> str | None:
    """« #RRGGBB » en minuscules ; None (vide) : couleur par défaut. Lève ValueError sinon."""
    v = (value or "").strip().lower()
    if not v:
        return None
    if not HEX.match(v):
        raise ValueError("couleur invalide (format #RRGGBB)")
    return v


def _luminance(color: str) -> float:
    def channel(c: int) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def problem(primary: str | None, accent: str | None) -> str | None:
    """Pourquoi ce jeu de couleurs serait illisible ; None s'il convient."""
    p, a = primary or DEFAULT_PRIMARY, accent or DEFAULT_ACCENT
    if contrast(p, "#ffffff") < MIN_CONTRAST:
        return ("Couleur principale trop claire : le texte blanc de l'en-tête serait illisible "
                f"(contraste {contrast(p, '#ffffff'):.1f}:1, il faut au moins {MIN_CONTRAST}:1). Choisissez une teinte plus foncée.")
    if contrast(p, a) < MIN_CONTRAST:
        return ("Couleurs principale et d'accent trop proches : le texte des boutons serait illisible "
                f"(contraste {contrast(p, a):.1f}:1, il faut au moins {MIN_CONTRAST}:1). Choisissez un accent plus clair.")
    return None


def theme_out(primary: str | None, accent: str | None) -> dict | None:
    """Ce que reçoit l'interface ; None : couleurs de Calendive."""
    if not primary and not accent:
        return None
    return {"primary": primary or DEFAULT_PRIMARY, "accent": accent or DEFAULT_ACCENT}
