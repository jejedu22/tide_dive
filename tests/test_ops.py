"""Exploitation : sauvegardes, tâches automatiques, mode maintenance, suivi des e-mails ; qualité des données."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import backup, db, mailer, migrations, ops
from tests.conftest import login

T = "2026-01-01T00:00:00+00:00"
CRONTAB = Path(__file__).resolve().parent.parent / "docker" / "crontab"


@pytest.fixture()
def setup(make_structure, make_user):
    a = make_structure("Club A")
    make_user("root", is_admin=True)
    make_user("bob", structure_id=a, structure_role="manager")
    return {"a": a}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


# ---------------------------------------------------------------------------
# Tâches automatiques
# ---------------------------------------------------------------------------

def test_planning_conforme_a_la_crontab():
    lines = {" ".join(line.split()) for line in CRONTAB.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.lstrip().startswith("#")}
    planned = {f"{s.cron} {s.command}" for s in ops.SCHEDULE}
    assert planned == lines


@pytest.mark.parametrize("cron, after, expected", [
    ("*/5 * * * *", "2026-10-09T10:02:00+02:00", "2026-10-09T10:05:00+02:00"),
    ("30 3 * * *", "2026-10-09T10:00:00+02:00", "2026-10-10T03:30:00+02:00"),
    ("30 4 2 * *", "2026-10-09T10:00:00+02:00", "2026-11-02T04:30:00+01:00"),   # heure d'hiver
    ("0 3 15 12 *", "2026-12-15T03:00:00+01:00", "2027-12-15T03:00:00+01:00"),
    ("0 9 * * 1", "2026-10-09T10:00:00+02:00", "2026-10-12T09:00:00+02:00"),    # lundi
])
def test_prochaine_execution(cron, after, expected):
    tz = ZoneInfo("Europe/Paris")
    got = ops.next_run(cron, datetime.fromisoformat(after), tz)
    assert got == datetime.fromisoformat(expected).astimezone(timezone.utc)


def test_planning_et_lancer_maintenant(setup, new_client):
    root = _client(new_client, "root")
    data = root.get("/api/admin/schedule").json()
    assert [e["key"] for e in data["entries"]][:3] == ["newsletters", "short_term", "backup"]
    assert all(e["next_run"] for e in data["entries"])
    assert root.post("/api/admin/schedule/backup/run").json()["queued"]
    assert root.post("/api/admin/schedule/backup/run").json()["queued"] == []    # déjà en file
    assert root.post("/api/admin/schedule/inconnue/run").status_code == 404
    entry = next(e for e in root.get("/api/admin/schedule").json()["entries"] if e["key"] == "backup")
    assert entry["last_job"]["status"] == "queued"
    log = root.get("/api/admin/audit").json()
    assert (log[0]["action"], log[0]["target"]) == ("Tâche automatique lancée", "Sauvegarde de la base")
    assert _client(new_client, "bob").get("/api/admin/schedule").status_code == 403


# ---------------------------------------------------------------------------
# Sauvegardes
# ---------------------------------------------------------------------------

def test_sauvegardes(setup, new_client, tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path / "backups")
    root = _client(new_client, "root")
    assert root.get("/api/admin/backups").json()["backups"] == []
    made = backup.create_backup(directory=backup.BACKUP_DIR)
    listing = root.get("/api/admin/backups").json()
    assert [b["name"] for b in listing["backups"]] == [made.name] and listing["backups"][0]["size"] > 0
    check = root.post(f"/api/admin/backups/{made.name}/verify").json()
    assert check["ok"] is True and check["counts"]["users"] == 2
    r = root.get(f"/api/admin/backups/{made.name}")
    assert r.status_code == 200 and r.content[:2] == b"\x1f\x8b"
    # nom hors motif ou fichier corrompu
    assert root.get("/api/admin/backups/..%2Fplongee.db").status_code == 404
    bad = backup.BACKUP_DIR / "plongee-20260101-000000.db.gz"
    bad.write_bytes(b"pas un gzip")
    assert root.post(f"/api/admin/backups/{bad.name}/verify").json()["ok"] is False
    assert root.post("/api/admin/backups").json()["queued"] is True


# ---------------------------------------------------------------------------
# Mode maintenance
# ---------------------------------------------------------------------------

def test_mode_maintenance(setup, new_client, client):
    root, bob = _client(new_client, "root"), _client(new_client, "bob")
    assert bob.put("/api/admin/maintenance", json={"enabled": True}).status_code == 403
    out = root.put("/api/admin/maintenance", json={"enabled": True, "message": "Mise à jour du serveur"}).json()
    assert out["enabled"] and out["message"] == "Mise à jour du serveur"
    r = bob.post("/api/admin/dive-sites", json={"name": "X", "lat": 48.7, "lon": -2.7})
    assert r.status_code == 503 and r.json()["detail"] == "Mise à jour du serveur"
    assert bob.get("/api/admin/dive-sites").status_code == 200                    # lecture permise
    assert root.post(f"/api/admin/dive-sites?structure_id={setup['a']}",
                     json={"name": "X", "lat": 48.7, "lon": -2.7}).status_code == 201   # super administrateur
    login(new_client(), "bob")                                                     # connexion permise
    banner = client.get("/api/announcements").json()
    assert banner[0]["id"] == "maintenance" and banner[0]["closable"] is False
    root.put("/api/admin/maintenance", json={"enabled": False})
    assert bob.post("/api/admin/dive-sites", json={"name": "Y", "lat": 48.7, "lon": -2.7}).status_code == 201
    assert client.get("/api/announcements").json() == []


# ---------------------------------------------------------------------------
# E-mails
# ---------------------------------------------------------------------------

def test_suivi_des_emails_et_test(setup, new_client, monkeypatch, capsys):
    root = _client(new_client, "root")
    monkeypatch.setattr(mailer, "BACKEND", "console")
    monkeypatch.setattr(mailer, "BASE_URL", "https://calendive.test")
    assert root.post("/api/admin/mail-test").json() == {"sent_to": "root@example.org"}
    assert "e-mail de test" in capsys.readouterr().out
    log = root.get("/api/admin/mail-log").json()
    assert log["enabled"] and log["last_30_days"] == {"total": 1, "failed": 0}
    assert (log["entries"][0]["recipient"], log["entries"][0]["status"]) == ("root@example.org", "sent")
    db.add_mail_log([(datetime.now(timezone.utc).isoformat(), "x@example.org", "Invitation", "failed", "adresse refusée")])
    failed = root.get("/api/admin/mail-log?failed=true").json()["entries"]
    assert [e["recipient"] for e in failed] == ["x@example.org"]


# ---------------------------------------------------------------------------
# Qualité des données
# ---------------------------------------------------------------------------

def test_qualite_niveau_moyen_faux(setup, new_client):
    from tests.synthetic import year_extrema, year_sun
    y = datetime.now(timezone.utc).year
    ok = db.upsert_port("Binic", 48.6, -2.82, offset_zh_m=6.68)
    bad = db.upsert_port("Roscoff", 48.72, -3.97, offset_zh_m=0.01)
    db.upsert_port("Sans niveau", 48.0, -4.0)
    for pid, shift in ((ok, 6.68), (bad, 0.01)):
        extrema = [(ts, kind, h - 6.68 + shift, coef) for ts, kind, h, coef in year_extrema(y)]
        db.replace_year(pid, y, [(f"{y}-06-01T00:00:00+00:00", 1.0)], extrema, year_sun(y), model="TEST")
    root = _client(new_client, "root")
    data = root.get("/api/admin/quality").json()
    by_name = {p["name"]: p for p in data["ports"]}
    assert by_name["Binic"]["issues"] == []
    assert any("niveau moyen" in i["message"] and i["level"] == "error" for i in by_name["Roscoff"]["issues"])
    assert by_name["Sans niveau"]["issues"][0]["level"] == "error"
    assert data["summary"]["ports_with_errors"] == 2
    assert _client(new_client, "bob").get("/api/admin/quality").status_code == 403


def test_sites_sans_courant(setup, new_client):
    db.create_dive_site(setup["a"], "Le Moulin", 48.7, -2.7, None, T)
    data = _client(new_client, "root").get("/api/admin/quality").json()
    assert data["sites_without_current"] == [{"id": 1, "name": "Le Moulin", "structure": "Club A",
                                              "reason": "courant pas encore calculé"}]


def test_migration_13():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrations._m013_mail_log(conn)
    migrations._m013_mail_log(conn)
    assert "recipient" in migrations._columns(conn, "mail_log")
