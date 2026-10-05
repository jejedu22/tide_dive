"""Le modèle d'import téléchargeable depuis l'administration doit être importable tel quel."""

import base64
from pathlib import Path

from app import db
from tests.conftest import login

TEMPLATE = Path(__file__).resolve().parent.parent / "static" / "modele-import-utilisateurs.csv"


def test_le_modele_commence_par_une_vraie_marque_utf8():
    raw = TEMPLATE.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "BOM UTF-8 attendu : Excel ouvre sinon le fichier en Windows-1252"
    # le texte « \xef\xbb\xbf » écrit en clair (et non les trois octets) rendait la colonne « nom » illisible
    assert b"\\x" not in raw


def test_le_modele_s_importe_sans_erreur(client, make_user, make_structure):
    sid = make_structure("Club A")
    make_user("root", is_admin=True)
    login(client, "root")
    content = base64.b64encode(TEMPLATE.read_bytes()).decode()
    r = client.post("/api/admin/users/import", json={"content_b64": content, "structure_id": sid, "dry_run": True})
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert len(rows) == 2 and not any(row["errors"] for row in rows), rows
    assert [row["last_name"] for row in rows] == ["Le Bihan", "Morvan"]
    assert rows[0]["first_name"] == "Maël"                       # accent conservé : le fichier est bien lu en UTF-8
    # import réel : les comptes sont créés et rattachés à la structure
    r = client.post("/api/admin/users/import", json={"content_b64": content, "structure_id": sid, "dry_run": False})
    assert r.status_code == 200, r.text
    # rôles du fichier : « visualisation » et « administration » ; le super administrateur n'est pas membre
    assert {u["username"]: u["structure_role"] for u in db.list_users(sid)} == {"mael.le-bihan": "viewer", "amorvan": "manager"}
