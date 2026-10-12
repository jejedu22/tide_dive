"""
Pièces jointes des newsletters : contrôle des fichiers et mise en forme pour l'API d'envoi de Mailjet.

- Formats acceptés : PDF, images (PNG, JPEG, WebP, GIF), Word (.docx), Excel (.xlsx), OpenDocument texte et
  tableur (.odt, .ods). Le format est reconnu à la SIGNATURE du fichier, pas à son nom : un exécutable renommé
  en .pdf est refusé, et le nom enregistré reçoit l'extension du format reconnu. Les formats à macros (.docm,
  .xlsm), les anciens formats .doc / .xls et les archives ne sont pas acceptés.
- Limites : MAX_FILES fichiers, MAX_TOTAL octets au total par newsletter (Mailjet plafonne un appel d'envoi à
  15 Mo, pièces jointes en base64 comprises : l'envoi réduit alors le nombre de messages par appel, voir
  mailjet.batch_size).
- Stockage : table newsletter_attachments (avec la newsletter : supprimées, copiées avec elle).
"""

from __future__ import annotations

import base64
import io
import re
import zipfile

MAX_FILES = 5
MAX_TOTAL = 8 * 1024 * 1024
NAME_MAX = 100


class AttachmentError(ValueError):
    """Fichier refusé ; status : code HTTP à renvoyer (415 : format, 413 : taille, 422 : nombre)."""

    def __init__(self, message: str, status: int = 415):
        super().__init__(message)
        self.status = status


# extension canonique → type MIME
TYPES = {
    "pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "odt": "application/vnd.oasis.opendocument.text", "ods": "application/vnd.oasis.opendocument.spreadsheet",
}
FORMATS_LABEL = "PDF, image (PNG, JPEG, WebP, GIF), Word (.docx), Excel (.xlsx), OpenDocument (.odt, .ods)"


def clean_name(name: str) -> str:
    """Nom de fichier sans chemin ni caractère de contrôle, de 100 caractères au plus."""
    name = re.split(r"[\\/]", name or "")[-1]
    name = "".join(c for c in name if c.isprintable() and c not in '<>:"|?*').strip(" .")
    return name[:NAME_MAX]


def _sniff(data: bytes) -> str | None:
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def _zip_kind(data: bytes, ext: str) -> str | None:
    """Format d'une archive ZIP (docx, xlsx, odt, ods) d'après son contenu ; None si ce n'en est pas un."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
            if "word/document.xml" in names and ext == "docx":
                return "docx"
            if "xl/workbook.xml" in names and ext == "xlsx":
                return "xlsx"
            if "mimetype" in names and ext in ("odt", "ods"):
                if z.read("mimetype")[:100].decode("ascii", "ignore").strip() == TYPES[ext]:
                    return ext
    except (zipfile.BadZipFile, KeyError, RuntimeError, NotImplementedError):
        return None
    return None


def check(filename: str, data: bytes) -> tuple[str, str]:
    """(nom enregistré, type MIME) d'un fichier accepté ; AttachmentError sinon."""
    name = clean_name(filename)
    if not name:
        raise AttachmentError("Nom de fichier vide.", 422)
    if not data:
        raise AttachmentError("Fichier vide.", 422)
    stem, _, ext = name.rpartition(".")
    ext = ext.lower() if stem else ""
    kind = _sniff(data)
    if kind is None and data.startswith(b"PK\x03\x04"):
        kind = _zip_kind(data, ext)
    if kind is None:
        raise AttachmentError(f"Format non accepté : {FORMATS_LABEL} seulement.")
    # le nom reçoit l'extension du format reconnu (photo.jpeg, SCAN.PDF…)
    base = stem if stem and (ext in TYPES or ext in ("jpeg",)) else name
    return f"{base}.{kind}", TYPES[kind]


def encoded_size(sizes: list[int]) -> int:
    """Taille des pièces jointes en base64 dans UN message (4 octets pour 3)."""
    return sum(4 * ((s + 2) // 3) for s in sizes)


def mailjet_files(rows) -> list[dict]:
    """Pièces jointes au format de l'API d'envoi Mailjet v3.1 (lignes avec filename, content_type, data)."""
    return [{"ContentType": r["content_type"], "Filename": r["filename"],
             "Base64Content": base64.b64encode(r["data"]).decode("ascii")} for r in rows]
