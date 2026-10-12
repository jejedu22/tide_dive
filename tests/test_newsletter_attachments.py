"""Pièces jointes des newsletters : contrôle des fichiers, limites, envoi par lots réduits."""

import io
import types
import zipfile

import pytest

from app import attachments, db, mailer, mailjet, mailjet_admin, newsletter_send
from tests.conftest import login

PDF = b"%PDF-1.4\n" + b"x" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"y" * 200


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


DOCX = _zip({"[Content_Types].xml": b"<x/>", "word/document.xml": b"<w/>"})
XLSX = _zip({"[Content_Types].xml": b"<x/>", "xl/workbook.xml": b"<w/>"})
ODT = _zip({"mimetype": b"application/vnd.oasis.opendocument.text", "content.xml": b"<c/>"})


@pytest.fixture()
def club(make_structure, make_user):
    sid = make_structure("Club A")
    gina = make_user("gina", structure_id=sid, structure_role="manager")
    db.set_user_profiles(gina, sid, ["gestionnaire"])
    make_user("bob", structure_id=sid, structure_role="manager")        # administrateur sans profil Gestionnaire
    for i in range(6):
        make_user(f"m{i}", structure_id=sid, structure_role="viewer")
    return {"sid": sid}


def _client(new_client, username="gina"):
    c = new_client()
    login(c, username)
    return c


def _draft(c):
    r = c.post("/api/newsletters", json={"subject": "Sorties", "body": "Bonjour", "audience": {"kind": "all"}})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _attach(c, nid, name, data):
    return c.post(f"/api/newsletters/{nid}/attachments", params={"filename": name}, content=data,
                  headers={"Content-Type": "application/octet-stream"})


def test_formats_reconnus_a_la_signature():
    assert attachments.check("Programme.PDF", PDF) == ("Programme.pdf", "application/pdf")
    assert attachments.check("photo.jpeg", b"\xff\xd8\xff" + b"z" * 50) == ("photo.jpg", "image/jpeg")
    assert attachments.check("plan", PNG) == ("plan.png", "image/png")             # sans extension
    assert attachments.check("note.docx", DOCX)[1].endswith("wordprocessingml.document")
    assert attachments.check("budget.xlsx", XLSX)[1].endswith("spreadsheetml.sheet")
    assert attachments.check("compte-rendu.odt", ODT) == ("compte-rendu.odt", "application/vnd.oasis.opendocument.text")
    assert attachments.check("../../etc/Mon fichier?.pdf", PDF)[0] == "Mon fichier.pdf"      # chemin et caractères nettoyés
    for bad in (("virus.pdf", b"MZ\x90\x00" + b"\x00" * 100),                              # exécutable renommé
                ("a.docx", ODT), ("a.xlsx", DOCX), ("archive.docx", _zip({"x.txt": b"1"})),   # zip qui n'en est pas un
                ("a.zip", _zip({"x.txt": b"1"})), ("macro.docm", DOCX), ("vieux.doc", b"\xd0\xcf\x11\xe0" + b"\x00" * 50)):
        with pytest.raises(attachments.AttachmentError) as exc:
            attachments.check(*bad)
        assert exc.value.status == 415, bad


def test_ajouter_telecharger_retirer(club, new_client):
    gina = _client(new_client)
    nid = _draft(gina)
    r = _attach(gina, nid, "Programme.pdf", PDF)
    assert r.status_code == 201, r.text
    out = r.json()
    assert [a["filename"] for a in out["attachments"]] == ["Programme.pdf"] and out["attachments"][0]["size"] == len(PDF)
    assert out["attachment_limits"]["max_files"] == 5
    aid = out["attachments"][0]["id"]
    dl = gina.get(f"/api/newsletters/{nid}/attachments/{aid}")
    assert dl.status_code == 200 and dl.content == PDF and dl.headers["content-type"] == "application/pdf"
    assert "attachment" in dl.headers["content-disposition"]
    assert _attach(gina, nid, "Programme.pdf", PDF).status_code == 409               # même nom
    assert _attach(gina, nid, "faux.pdf", b"MZ" + b"0" * 100).status_code == 415
    assert gina.get(f"/api/newsletters/{nid}").json()["attachments"][0]["id"] == aid
    assert gina.delete(f"/api/newsletters/{nid}/attachments/{aid}").status_code == 204
    assert gina.get(f"/api/newsletters/{nid}").json()["attachments"] == []
    assert gina.delete(f"/api/newsletters/{nid}/attachments/{aid}").status_code == 404


def test_limites_de_nombre_et_de_taille(club, new_client):
    gina = _client(new_client)
    nid = _draft(gina)
    for i in range(attachments.MAX_FILES):
        assert _attach(gina, nid, f"f{i}.pdf", PDF).status_code == 201
    assert _attach(gina, nid, "de-trop.pdf", PDF).status_code == 422
    other = _draft(gina)
    big = b"%PDF-" + b"0" * (attachments.MAX_TOTAL // 2 + 1)
    assert _attach(gina, other, "a.pdf", big).status_code == 201
    r = _attach(gina, other, "b.pdf", big)                                            # le total dépasserait 8 Mo
    assert r.status_code == 413 and "Mo" in r.json()["detail"]
    assert len(gina.get(f"/api/newsletters/{other}").json()["attachments"]) == 1


def test_droits_et_etat_de_la_newsletter(club, new_client):
    gina, bob = _client(new_client), _client(new_client, "bob")
    nid = _draft(gina)
    aid = _attach(gina, nid, "a.pdf", PDF).json()["attachments"][0]["id"]
    assert _attach(bob, nid, "b.pdf", PDF).status_code == 403                         # profil Gestionnaire requis
    assert bob.get(f"/api/newsletters/{nid}/attachments/{aid}").status_code == 403
    db.update_newsletter(nid, status="scheduled", scheduled_at="2099-01-01T00:00:00+00:00")
    assert _attach(gina, nid, "b.pdf", PDF).status_code == 409                         # brouillon seulement
    assert gina.delete(f"/api/newsletters/{nid}/attachments/{aid}").status_code == 409
    assert gina.get(f"/api/newsletters/{nid}/attachments/{aid}").status_code == 200   # reste consultable


def test_dupliquer_copie_les_pieces_jointes_et_supprimer_les_efface(club, new_client):
    gina = _client(new_client)
    nid = _draft(gina)
    _attach(gina, nid, "a.pdf", PDF)
    _attach(gina, nid, "b.png", PNG)
    copy = gina.post(f"/api/newsletters/{nid}/duplicate").json()
    assert [a["filename"] for a in copy["attachments"]] == ["a.pdf", "b.png"]
    assert gina.delete(f"/api/newsletters/{nid}").status_code == 204
    assert db.list_newsletter_attachments(nid) == []
    assert len(db.list_newsletter_attachments(copy["id"])) == 2                        # la copie garde les siennes


def test_taille_des_lots_selon_les_pieces_jointes():
    assert mailjet.batch_size(0) == mailjet.MAX_MESSAGES_PER_CALL
    assert mailjet.batch_size(attachments.encoded_size([attachments.MAX_TOTAL])) == 1     # 8 Mo : un message par appel
    assert 1 < mailjet.batch_size(attachments.encoded_size([1024 * 1024])) < mailjet.MAX_MESSAGES_PER_CALL
    assert attachments.encoded_size([3]) == 4 and attachments.encoded_size([1]) == 4


def test_envoi_avec_pieces_jointes_par_lots_reduits(club, new_client, monkeypatch):
    gina = _client(new_client)
    nid = _draft(gina)
    _attach(gina, nid, "Programme.pdf", b"%PDF-" + b"0" * (2 * 1024 * 1024))
    _attach(gina, nid, "logo.png", PNG)
    monkeypatch.setattr(mailer, "BASE_URL", "https://exemple.org")
    monkeypatch.setattr(mailjet_admin, "credentials", lambda sid: types.SimpleNamespace(
        api_key="k", api_secret="s", sender_email="club@exemple.org", sender_name="Club A"))
    calls = []

    def fake_send(key, secret, messages, sandbox=False):
        calls.append(messages)
        return [{"Status": "success", "To": [{"MessageID": i}]} for i, _ in enumerate(messages)]

    monkeypatch.setattr(mailjet, "send", fake_send)
    db.update_newsletter(nid, only_status=("draft",), status="queued")
    assert newsletter_send.run(nid) == 0
    sizes = [len(c) for c in calls]
    assert sum(sizes) == 8 and max(sizes) <= 4 and len(calls) >= 2               # 8 destinataires, lots réduits
    for message in calls[0]:
        assert [a["Filename"] for a in message["Attachments"]] == ["Programme.pdf", "logo.png"]
        assert message["Attachments"][0]["ContentType"] == "application/pdf" and message["Attachments"][0]["Base64Content"]
    assert db.get_newsletter(nid)["status"] == "sent"


def test_envoi_sans_piece_jointe_inchange(club, new_client, monkeypatch):
    gina = _client(new_client)
    nid = _draft(gina)
    monkeypatch.setattr(mailer, "BASE_URL", "https://exemple.org")
    monkeypatch.setattr(mailjet_admin, "credentials", lambda sid: types.SimpleNamespace(
        api_key="k", api_secret="s", sender_email="club@exemple.org", sender_name="Club A"))
    calls = []
    monkeypatch.setattr(mailjet, "send", lambda k, s, messages, sandbox=False: (
        calls.append(messages), [{"Status": "success", "To": [{"MessageID": 1}]} for _ in messages])[1])
    db.update_newsletter(nid, only_status=("draft",), status="queued")
    newsletter_send.run(nid)
    assert len(calls) == 1 and all("Attachments" not in m for m in calls[0])
