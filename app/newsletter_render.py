"""
Rendu d'une newsletter : texte saisi dans un format simple (proche du Markdown)
→ e-mail HTML (mise en page par tableaux, styles en ligne, compatible avec les
messageries) et version texte.

Format (blocs séparés par une ligne vide) :

    ## Titre            (ou « # Titre ») ; « ### Sous-titre »
    - élément de liste  (ou « * ») : un bloc fait uniquement de ces lignes
    [[Texte du bouton|https://exemple.fr]]   bouton, seul dans son bloc
    ![Description](https://exemple.fr/image.jpg)   image, seule dans son bloc
    ---                 séparateur
    **gras**, *italique* ou _italique_, [lien](https://exemple.fr)
    {{prenom}}, {{nom}}, {{structure}}   remplacés pour chaque destinataire

Sécurité : tout le texte saisi est échappé AVANT la mise en forme ; seules les
balises produites ici apparaissent dans le HTML. Liens et images : http(s) et
mailto: uniquement (une autre adresse reste du texte).
"""

from __future__ import annotations

import html
import re

BRAND_DARK = "#073b4c"
BRAND_LINK = "#0b6e8f"
TEXT = "#0d2b36"
MUTED = "#5b7480"
FONT = "Helvetica, Arial, sans-serif"

_URL = r"(?:https?://|mailto:)[^\s<>()\[\]\"']+"
_LINK = re.compile(r"\[([^\]\n]+)\]\((" + _URL + r")\)")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_ITALIC = re.compile(r"(?<![\w*])[*_](?=\S)(.+?)(?<=\S)[*_](?![\w*])")
_BUTTON = re.compile(r"^\[\[([^|\]\n]+)\|(" + _URL + r")\]\]$")
_IMAGE = re.compile(r"^!\[([^\]\n]*)\]\((" + _URL + r")\)$")
_ITEM = re.compile(r"^[-*]\s+(.*)$")
PLACEHOLDERS = ("prenom", "nom", "structure")


def _blocks(source: str) -> list[str]:
    text = (source or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return [b.strip("\n") for b in re.split(r"\n\s*\n", text) if b.strip()]


def _emphasis(text: str) -> str:
    return _ITALIC.sub(r"<em>\1</em>", _BOLD.sub(r"<strong>\1</strong>", text))


def _inline_html(escaped: str) -> str:
    """
    Mise en forme en ligne sur du texte DÉJÀ échappé. Les liens sont mis de côté
    pendant le gras / l'italique : un « _ » dans une adresse ne doit pas la casser.
    """
    links: list[str] = []

    def keep(m: re.Match) -> str:
        links.append(f'<a href="{m.group(2)}" style="color:{BRAND_LINK};text-decoration:underline">'
                     f'{_emphasis(m.group(1))}</a>')
        return f"\x00{len(links) - 1}\x00"

    out = _emphasis(_LINK.sub(keep, escaped))
    return re.sub(r"\x00(\d+)\x00", lambda m: links[int(m.group(1))], out)


def _inline_text(raw: str) -> str:
    out = _LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})", raw)
    out = _BOLD.sub(r"\1", out)
    return _ITALIC.sub(r"\1", out)


def _block_html(block: str) -> str:
    esc = html.escape(block, quote=True)
    p = f"margin:0 0 16px;font-family:{FONT};font-size:16px;line-height:1.55;color:{TEXT}"
    if block == "---":
        return '<hr style="border:0;border-top:1px solid #d5e3e8;margin:24px 0">'
    m = re.match(r"^(#{1,3})\s+(.+)$", block)
    if m and "\n" not in block:
        size, tag = ("20px", "h3") if len(m.group(1)) == 3 else ("24px", "h2")
        return (f'<{tag} style="margin:24px 0 12px;font-family:{FONT};font-size:{size};line-height:1.3;'
                f'color:{BRAND_DARK}">{_inline_html(html.escape(m.group(2), quote=True))}</{tag}>')
    m = _BUTTON.match(block)
    if m:
        label, url = html.escape(m.group(1).strip(), quote=True), html.escape(m.group(2), quote=True)
        return (f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:8px 0 24px"><tr>'
                f'<td style="background:{BRAND_DARK};border-radius:8px">'
                f'<a href="{url}" style="display:inline-block;padding:12px 24px;font-family:{FONT};font-size:16px;'
                f'font-weight:bold;color:#ffffff;text-decoration:none">{label}</a></td></tr></table>')
    m = _IMAGE.match(block)
    if m:
        alt, url = html.escape(m.group(1), quote=True), html.escape(m.group(2), quote=True)
        return (f'<img src="{url}" alt="{alt}" width="544" '
                f'style="display:block;width:100%;max-width:544px;height:auto;border:0;margin:0 0 16px;border-radius:8px">')
    lines = block.split("\n")
    if all(_ITEM.match(line) for line in lines):
        items = "".join(
            f'<li style="margin:0 0 6px">{_inline_html(html.escape(_ITEM.match(line).group(1), quote=True))}</li>'
            for line in lines)
        return f'<ul style="{p};padding-left:22px">{items}</ul>'
    return f'<p style="{p}">{_inline_html(esc).replace(chr(10), "<br>")}</p>'


def _block_text(block: str) -> str:
    if block == "---":
        return "----------"
    m = re.match(r"^(#{1,3})\s+(.+)$", block)
    if m and "\n" not in block:
        title = _inline_text(m.group(2))
        return f"{title}\n{'=' * min(len(title), 60)}"
    m = _BUTTON.match(block)
    if m:
        return f"{m.group(1).strip()} : {m.group(2)}"
    m = _IMAGE.match(block)
    if m:
        return f"[image{' : ' + m.group(1) if m.group(1) else ''}]"
    lines = block.split("\n")
    if all(_ITEM.match(line) for line in lines):
        return "\n".join(f"• {_inline_text(_ITEM.match(line).group(1))}" for line in lines)
    return _inline_text(block)


def _fill(text: str, values: dict[str, str], escape: bool) -> str:
    for key in PLACEHOLDERS:
        value = values.get(key) or ""
        text = text.replace("{{" + key + "}}", html.escape(value, quote=True) if escape else value)
    return text


def render(subject: str, preheader: str | None, body: str, *, structure: str,
           first_name: str | None = None, last_name: str | None = None,
           unsubscribe_url: str | None = None) -> tuple[str, str, str]:
    """(sujet, html, texte) pour un destinataire ; unsubscribe_url None : aperçu."""
    values = {"prenom": first_name or "", "nom": last_name or "", "structure": structure}
    blocks = _blocks(body)
    content = "\n".join(_block_html(b) for b in blocks) or f'<p style="color:{MUTED};font-family:{FONT}">(newsletter vide)</p>'
    unsub = html.escape(unsubscribe_url or "#", quote=True)
    pre = html.escape(_fill(preheader or "", values, escape=False), quote=True)
    s_structure = html.escape(structure, quote=True)
    page = f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(_fill(subject, values, escape=False), quote=True)}</title>
</head>
<body style="margin:0;padding:0;background:#f4f9fb">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent">{pre}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f9fb">
<tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:100%;max-width:600px;background:#ffffff;border-radius:12px">
<tr><td style="background:{BRAND_DARK};border-radius:12px 12px 0 0;padding:18px 28px;font-family:{FONT};font-size:20px;font-weight:bold;color:#ffffff">{s_structure}</td></tr>
<tr><td style="padding:28px 28px 12px">
{_fill(content, values, escape=True)}
</td></tr>
</table>
<p style="max-width:600px;margin:16px auto 0;font-family:{FONT};font-size:12px;line-height:1.5;color:{MUTED}">
Vous recevez cet e-mail en tant que membre de {s_structure} sur Calendive.<br>
<a href="{unsub}" style="color:{MUTED};text-decoration:underline">Se désinscrire de ces e-mails</a>
</p>
</td></tr>
</table>
</body>
</html>"""
    text = "\n\n".join(_block_text(b) for b in blocks)
    text = _fill(text, values, escape=False)
    text += (f"\n\n--\nVous recevez cet e-mail en tant que membre de {structure} sur Calendive."
             f"\nSe désinscrire : {unsubscribe_url or '(lien personnel ajouté à l’envoi)'}\n")
    return _fill(subject, values, escape=False), page, text
