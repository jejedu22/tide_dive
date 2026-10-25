"""
Petit générateur de PDF pour des documents en tableau (planning des pilotes et DP) : A4, polices standard
Helvetica (aucun fichier de police à embarquer), encodage WinAnsi (cp1252 : tout le français, €, œ…). Les
caractères hors cp1252 deviennent « ? ». Pas de dépendance : le PDF est écrit à la main (objets, xref).

`table_pdf(title, subtitle, columns, rows, footer_lines)` : columns = [(en-tête, largeur relative)], rows = listes
de textes ; les cellules trop longues sont coupées sur plusieurs lignes ; nouvelle page automatique, en-têtes de
colonnes répétés, pagination « page x / n ».
"""

from __future__ import annotations

PAGE_W, PAGE_H = 595.28, 841.89          # A4 portrait, en points
MARGIN = 40
FONT_SIZE = 9
LINE_H = 11.5
PAD = 3

# Largeurs Helvetica (millièmes d'em) des caractères ASCII 32 à 126 ; 556 pour les autres (lettres accentuées
# proches de leur lettre de base : suffisant pour couper les lignes)
_W = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556, 556,
      556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667,
      556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556, 333, 556,
      556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722,
      500, 500, 500, 334, 260, 334, 584]


def _enc(text: str) -> bytes:
    return str(text).encode("cp1252", errors="replace")


def text_width(text: str, size: float = FONT_SIZE, bold: bool = False) -> float:
    w = sum(_W[ord(c) - 32] if 32 <= ord(c) <= 126 else 556 for c in str(text))
    return w * size / 1000 * (1.05 if bold else 1.0)


def wrap(text: str, width: float, size: float = FONT_SIZE, bold: bool = False) -> list[str]:
    """Coupe le texte en lignes tenant dans `width` (mots, puis lettres si un mot est trop long)."""
    lines: list[str] = []
    for paragraph in str(text or "").split("\n"):
        line = ""
        for word in paragraph.split(" "):
            candidate = f"{line} {word}" if line else word
            if text_width(candidate, size, bold) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
            while text_width(word, size, bold) > width and len(word) > 1:
                cut = len(word)
                while cut > 1 and text_width(word[:cut], size, bold) > width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            line = word
        lines.append(line)
    return lines or [""]


def _esc(text: str) -> bytes:
    return _enc(text).replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


class _Page:
    def __init__(self) -> None:
        self.ops: list[bytes] = []

    def text(self, x: float, y: float, text: str, size: float = FONT_SIZE, bold: bool = False,
             color: tuple[float, float, float] = (0, 0, 0)) -> None:
        font = b"/F2" if bold else b"/F1"
        self.ops.append(b"BT %s %.2f Tf %.3f %.3f %.3f rg %.2f %.2f Td (%s) Tj ET" % (
            font, size, *color, x, y, _esc(text)))

    def rect(self, x: float, y: float, w: float, h: float, fill: tuple[float, float, float]) -> None:
        self.ops.append(b"%.3f %.3f %.3f rg %.2f %.2f %.2f %.2f re f" % (*fill, x, y, w, h))

    def line(self, x1: float, y1: float, x2: float, y2: float, gray: float = 0.75) -> None:
        self.ops.append(b"%.3f G 0.5 w %.2f %.2f m %.2f %.2f l S" % (gray, x1, y1, x2, y2))


def table_pdf(title: str, subtitle: str, columns: list[tuple[str, float]], rows: list[list[str]],
              footer_lines: list[str] | None = None, header_color: tuple[float, float, float] = (0.027, 0.231, 0.298)) -> bytes:
    usable = PAGE_W - 2 * MARGIN
    total = sum(w for _, w in columns)
    widths = [usable * w / total for _, w in columns]
    pages: list[_Page] = []

    def new_page(first: bool) -> tuple[_Page, float]:
        page = _Page()
        pages.append(page)
        y = PAGE_H - MARGIN
        if first:
            page.text(MARGIN, y - 14, title, 15, bold=True, color=header_color)
            y -= 22
            for line in wrap(subtitle, usable, 9):
                page.text(MARGIN, y - 10, line, 9, color=(0.3, 0.38, 0.42))
                y -= 12
            y -= 8
        # en-têtes de colonnes
        page.rect(MARGIN, y - LINE_H - 2 * PAD, usable, LINE_H + 2 * PAD, header_color)
        x = MARGIN
        for (head, _), w in zip(columns, widths):
            page.text(x + PAD, y - LINE_H - PAD + 2.5, head, FONT_SIZE, bold=True, color=(1, 1, 1))
            x += w
        return page, y - LINE_H - 2 * PAD

    page, y = new_page(True)
    for i, row in enumerate(rows):
        cells = [wrap(cell, w - 2 * PAD) for cell, w in zip(row, widths)]
        height = max(len(c) for c in cells) * LINE_H + 2 * PAD
        if y - height < MARGIN + 20:
            page, y = new_page(False)
        if i % 2:
            page.rect(MARGIN, y - height, usable, height, (0.95, 0.97, 0.98))
        x = MARGIN
        for lines, w in zip(cells, widths):
            for k, line in enumerate(lines):
                page.text(x + PAD, y - PAD - (k + 1) * LINE_H + 2.5, line)
            x += w
        y -= height
        page.line(MARGIN, y, MARGIN + usable, y)
    if not rows:
        page.text(MARGIN + PAD, y - LINE_H - PAD, "Aucun créneau sur cette période.", color=(0.3, 0.38, 0.42))
        y -= LINE_H + 2 * PAD
    for line in footer_lines or []:
        for part in wrap(line, usable):
            if y - LINE_H < MARGIN + 20:
                page, y = new_page(False)
            y -= LINE_H + (4 if part == line else 0)
            page.text(MARGIN, y, part)

    # pagination
    for n, p in enumerate(pages, 1):
        label = f"page {n} / {len(pages)}"
        p.text(PAGE_W - MARGIN - text_width(label, 8), MARGIN - 18, label, 8, color=(0.45, 0.5, 0.55))
    return _assemble(pages)


def _assemble(pages: list[_Page]) -> bytes:
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    font2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    pages_id = len(objects) + 1 + 2 * len(pages)          # numéro réservé pour l'arbre des pages
    kids = []
    for p in pages:
        stream = b"\n".join(p.ops)
        content = add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
        kids.append(add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] /Contents %d 0 R "
                        b"/Resources << /Font << /F1 %d 0 R /F2 %d 0 R >> >> >>" % (
                            pages_id, PAGE_W, PAGE_H, content, font1, font2)))
    assert add(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % k for k in kids), len(kids))) == pages_id
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, catalog, xref)
    return bytes(out)
