"""Historique des tâches : 30 jours et 1 000 tâches au plus (assez pour un calcul groupé de tous les ports),
liste paginée et filtrée par statut dans l'administration."""

from datetime import datetime, timedelta, timezone

from app import db, db_jobs, jobs
from tests.conftest import login


def _job(when: datetime, status: str = "succeeded") -> int:
    job_id = db.enqueue_job("precompute", f'{{"n": {when.timestamp()}}}', "test", when.isoformat(timespec="seconds"))
    db.finish_job(job_id, status, 0, when.isoformat(timespec="seconds")) if status != "queued" else None
    return job_id


def test_conservation(tmp_db, monkeypatch):
    now = datetime.now(timezone.utc)
    old = [_job(now - timedelta(days=40, minutes=i)) for i in range(30)]          # trop anciennes
    recent = [_job(now - timedelta(hours=i)) for i in range(250)]                  # calcul groupé récent
    waiting = _job(now - timedelta(days=60), status="queued")                     # en attente : jamais supprimée
    with db.get_conn() as conn:
        db_jobs._prune_jobs(conn)
        ids = {r[0] for r in conn.execute("SELECT id FROM jobs")}
    assert set(recent) <= ids and waiting in ids and not set(old) & ids
    # plafond : 1 000 au plus
    monkeypatch.setattr(db_jobs, "JOBS_KEPT", 100)
    with db.get_conn() as conn:
        db_jobs._prune_jobs(conn)
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 100
        assert conn.execute("SELECT 1 FROM jobs WHERE id = ?", (waiting,)).fetchone()


def test_liste_paginee_et_filtree(make_user, new_client):
    make_user("root", is_admin=True)
    c = new_client()
    login(c, "root")
    now = datetime.now(timezone.utc)
    for i in range(70):
        _job(now - timedelta(minutes=i), status="failed" if i % 10 == 0 else "succeeded")
    assert len(c.get("/api/admin/jobs").json()) == db.JOBS_PAGE == 50
    assert len(c.get("/api/admin/jobs?limit=100").json()) == 70
    failed = c.get("/api/admin/jobs?status=failed").json()
    assert len(failed) == 7 and {j["status"] for j in failed} == {"failed"}
    assert c.get("/api/admin/jobs?status=nimporte").status_code == 422
    assert jobs.now_iso()
