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


# ---------------------------------------------------------------------------
# Propositions de jeux de couleurs (toutes lisibles : même règle que l'enregistrement)
# ---------------------------------------------------------------------------

# (nom, principale, accent) : palettes prêtes à l'emploi, vérifiées par les tests
PRESETS: list[tuple[str, str, str]] = [
    ("Calendive", DEFAULT_PRIMARY, DEFAULT_ACCENT),
    ("Marine et soleil", "#1b2a6b", "#ffcc00"),
    ("Abysses et corail", "#0b2545", "#ff8c69"),
    ("Lagon", "#004e64", "#7ae7c7"),
    ("Récif", "#3d1a4b", "#ffb703"),
    ("Kelp", "#1e3d2f", "#a7e37c"),
    ("Bordeaux et sable", "#5c1a2e", "#f4d58d"),
    ("Ardoise et turquoise", "#263238", "#4dd0e1"),
    ("Nuit et rose", "#1f1a3d", "#ff9ec7"),
    ("Basalte et orange", "#2b2d42", "#ff9f1c"),
]


def _rgb(color: str) -> tuple[float, float, float]:
    return tuple(int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{round(max(0.0, min(1.0, c)) * 255):02x}" for c in rgb)


def _hls(h: float, l: float, s: float) -> str:   # noqa: E741 — notation usuelle (teinte, luminosité, saturation)
    import colorsys
    return _hex(colorsys.hls_to_rgb(h % 1.0, l, s))


def _primary_from(base: str) -> str:
    """La couleur de base assombrie jusqu'à porter du texte blanc avec de la marge (contraste ≥ 7:1)."""
    import colorsys
    h, l, s = colorsys.rgb_to_hls(*_rgb(base))
    s = min(max(s, 0.35), 0.85) if s > 0.08 else s          # une base grise reste grise
    l = min(l, 0.32)
    while contrast(_hls(h, l, s), "#ffffff") < 7 and l > 0.04:
        l -= 0.02
    return _hls(h, l, s)


def _accent_for(primary: str, hue: float, sat: float = 0.7) -> str | None:
    """Un accent clair de cette teinte, assez contrasté avec la couleur principale ; None si impossible."""
    for l in (0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85):
        accent = _hls(hue, l, sat)
        if contrast(primary, accent) >= MIN_CONTRAST + 1:          # un peu de marge au-dessus du minimum
            return accent
    return None


def suggest(base: str) -> list[dict]:
    """Jeux de couleurs construits sur une couleur (celle du logo, ou la couleur principale choisie) :
    principale dérivée de cette couleur, accents de teintes associées (complémentaire, analogues, triadique)."""
    import colorsys
    base = clean(base) or DEFAULT_PRIMARY
    primary = _primary_from(base)
    h, _, s = colorsys.rgb_to_hls(*_rgb(base))
    if s < 0.08:                                    # base grise : accents de teintes franches
        offsets = [("Turquoise", 0.5 - h), ("Soleil", 0.13 - h), ("Corail", 0.02 - h), ("Lagon", 0.45 - h)]
    else:
        offsets = [("Ton sur ton", 0.0), ("Complémentaire", 0.5), ("Analogue chaud", -1 / 12),
                   ("Analogue froid", 1 / 12), ("Triadique", 1 / 3), ("Triadique 2", -1 / 3)]
    out, seen = [], set()
    for name, offset in offsets:
        accent = _accent_for(primary, h + offset, 0.6 if name == "Ton sur ton" else 0.7)
        if accent and accent not in seen and problem(primary, accent) is None:
            seen.add(accent)
            out.append({"name": name, "primary": primary, "accent": accent})
    return out


def presets() -> list[dict]:
    return [{"name": n, "primary": p, "accent": a} for n, p, a in PRESETS]
