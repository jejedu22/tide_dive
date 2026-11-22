"""Quota d'api-maree.fr (360 requêtes par heure) : budget local, réponse 429, tâches remises en attente jusqu'à
l'heure de reprise ; mois glissant quotidien des seuls ports utilisés."""

import io
import json
import sys
import urllib.error
from datetime import date, datetime, timedelta, timezone

import pytest

from app import db, jobs, tide_reference
from tests.conftest import login


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def api(tmp_db, monkeypatch):
    calls = []
    monkeypatch.setattr(tide_reference, "API_MAREE_KEY", "cle")

    def urlopen(req, timeout=None):
        calls.append(req.full_url)
        return _Resp(b'{"data": []}')

    monkeypatch.setattr(tide_reference.urllib.request, "urlopen", urlopen)
    return calls


def test_budget_local(api, monkeypatch):
    monkeypatch.setattr(tide_reference, "HOURLY_BUDGET", 2)
    tide_reference._get("/water-levels", {})
    tide_reference._get("/water-levels", {})
    with pytest.raises(tide_reference.QuotaExceeded) as exc:
        tide_reference._get("/water-levels", {})
    assert len(api) == 2 and exc.value.reset_at.minute == 0
    assert tide_reference.blocked_until() == exc.value.reset_at        # les tâches attendront l'heure suivante
    with pytest.raises(tide_reference.QuotaExceeded):                  # bloqué : aucune requête ne part
        tide_reference._get("/tide-extrema", {})
    assert len(api) == 2
    u = tide_reference.usage()
    assert u["used"] == 2 and u["budget"] == 2 and u["blocked_until"]


def test_reponse_429(tmp_db, monkeypatch):
    monkeypatch.setattr(tide_reference, "API_MAREE_KEY", "cle")
    reset = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
    body = json.dumps({"error": "quota_exceeded", "quota_limit": 360, "quota_remaining": 0,
                       "quota_reset_at": reset.isoformat().replace("+00:00", "Z")}).encode()

    def urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, io.BytesIO(body))

    monkeypatch.setattr(tide_reference.urllib.request, "urlopen", urlopen)
    with pytest.raises(tide_reference.QuotaExceeded) as exc:
        tide_reference._get("/water-levels", {})
    assert exc.value.reset_at == reset and tide_reference.blocked_until() == reset


def test_tache_remise_en_attente(tmp_db, monkeypatch):
    port = db.create_port("Binic", 48.6, -2.82, "Europe/Paris", 6.0, True, "binic")
    api_job = jobs.enqueue("short_term", {"port_id": port}, "test")
    other = jobs.enqueue("school_holidays", {}, "test")
    script = "import sys; print('[quota] budget épuisé'); sys.exit(75)"
    monkeypatch.setattr(jobs, "build_command", lambda kind, params: [sys.executable, "-c", script])
    monkeypatch.setattr(jobs, "preflight", lambda kind: None)
    reset = datetime.now(timezone.utc) + timedelta(minutes=30)
    tide_reference._block(reset, "test")
    worker = jobs.Worker()
    job = db.claim_next_job(jobs.now_iso(), jobs.API_MAREE_KINDS if worker._api_blocked() else ())
    assert job["id"] == other                                      # la tâche api-maree.fr attend, l'autre passe
    db.finish_job(other, "succeeded", 0, jobs.now_iso())
    job = db.claim_next_job(jobs.now_iso())                        # (forcée, comme si le quota venait de tomber)
    worker.execute(job)
    row = db.get_job(api_job, with_log=True)
    assert row["status"] == "queued" and "Quota api-maree.fr atteint" in row["log"]
    assert row["finished_at"] is None


def test_ports_utilises(make_structure, make_user, new_client, client):
    used_default = db.create_port("Binic", 48.6, -2.82, "Europe/Paris", 6.0, True, "binic")
    used_search = db.create_port("Erquy", 48.63, -2.46, "Europe/Paris", 6.3, True, "erquy")
    idle = db.create_port("Dahouet", 48.58, -2.57, "Europe/Paris", 6.4, True, "dahouet")
    sid = make_structure("Club")
    db.update_structure_settings(sid, default_port_id=used_default)
    assert jobs.used_port_ids() == {used_default}
    client.get(f"/api/dive-windows?port_id={used_search}&start={date.today()}&end={date.today() + timedelta(days=3)}")
    assert jobs.used_port_ids() == {used_default, used_search}
    ids = jobs.enqueue_calibrations("test", "short_term", only_used=True)
    queued = {json.loads(db.get_job(i)["params_json"])["port_id"] for i in ids}
    assert queued == {used_default, used_search} and idle not in queued
    # tous les ports le dimanche
    assert len(jobs.enqueue_calibrations("test", "calibrate")) == 3
    # l'administration le montre
    make_user("root", is_admin=True)
    root = new_client()
    login(root, "root")
    daily = {p["name"]: p["short_term_daily"] for p in root.get("/api/admin/ports").json()}
    assert daily == {"Binic": True, "Erquy": True, "Dahouet": False}
    assert root.get("/api/admin/status").json()["api_maree_usage"]["budget"] == tide_reference.HOURLY_BUDGET
