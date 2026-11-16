"""Worker : une base momentanément verrouillée (un calcul écrit une année entière en une transaction) ne doit pas
l'arrêter. Le journal attend que la base se libère, la fin de la tâche est réessayée."""

import sqlite3
import sys

from app import db, jobs


def test_base_verrouillee(tmp_db, monkeypatch):
    job_id = jobs.enqueue("school_holidays", {}, "test")
    job = db.claim_next_job(jobs.now_iso())
    assert job["id"] == job_id
    script = "import time\nfor i in range(3):\n    print('ligne', i, flush=True)\n    time.sleep(1.1)\n"
    monkeypatch.setattr(jobs, "build_command", lambda kind, params: [sys.executable, "-c", script])
    monkeypatch.setattr(jobs, "preflight", lambda kind: None)
    monkeypatch.setattr(jobs.time, "sleep", lambda s, real=jobs.time.sleep: real(min(s, 0.05) if s > 1 else s))

    real_log, real_finish = db.append_job_log, db.finish_job
    state = {"log": 0, "finish": 0}

    def locked_log(jid, text):
        state["log"] += 1
        if state["log"] <= 3:                              # les premiers envois tombent sur une base verrouillée
            raise sqlite3.OperationalError("database is locked")
        real_log(jid, text)

    def locked_finish(*args):
        state["finish"] += 1
        if state["finish"] == 1:
            raise sqlite3.OperationalError("database is locked")
        real_finish(*args)

    def locked_cancel(jid):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "append_job_log", locked_log)
    monkeypatch.setattr(db, "finish_job", locked_finish)
    monkeypatch.setattr(db, "job_cancel_requested", locked_cancel)
    jobs.Worker().execute(job)                             # ne lève pas
    row = db.get_job(job_id, with_log=True)
    assert row["status"] == "succeeded" and state["finish"] == 2
    for i in range(3):
        assert f"ligne {i}" in row["log"]                  # rien de perdu : le texte a attendu


def test_autre_erreur_non_masquee(tmp_db, monkeypatch):
    def broken(*args):
        raise sqlite3.OperationalError("no such table: jobs")
    monkeypatch.setattr(db, "finish_job", broken)
    try:
        jobs.Worker._finish(1, "failed", None)
    except sqlite3.OperationalError as exc:
        assert "no such table" in str(exc)
    else:
        raise AssertionError("l'erreur aurait dû remonter")
