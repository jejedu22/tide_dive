from datetime import datetime, timedelta, timezone

import pytest

from app import alerts, backup, db, health, jobs, mailer
from app.health import ERROR, WARNING, Problem
from tests.synthetic import year_extrema, year_sun

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def _store(port_id, year, **kw):
    heights = [(f"{year}-06-01T00:00:00+00:00", 1.0)]    # years_available s'appuie sur les hauteurs
    db.replace_year(port_id, year, heights, year_extrema(year, **kw), year_sun(year), model="TEST")


def _keys(problems, level=None):
    return {p.key for p in problems if level in (None, p.level)}


@pytest.fixture()
def port(tmp_db):
    return db.upsert_port("Binic", 48.6, -2.82, offset_zh_m=6.68)


@pytest.fixture(autouse=True)
def quiet_system(monkeypatch, tmp_path):
    """Isole les contrôles système (modèle FES, sauvegardes, disque) pour tester les données."""
    monkeypatch.setattr(jobs, "MODEL_DIR", str(tmp_path / "models"))
    monkeypatch.setattr(health.tide_model, "missing_model_files", lambda model, directory=None: ([], []))
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path / "bk")


def test_annee_en_cours_absente_est_une_erreur(port):
    _store(port, 2025)
    assert f"missing-year:{port}:2026" in _keys(health.collect(NOW), ERROR)


def test_donnees_saines_sans_erreur_de_donnees(port):
    _store(port, 2026)
    assert not [p for p in health.collect(NOW) if p.key.startswith(("missing-year", "inconsistent"))]


def test_donnees_stockees_incoherentes_signalees(port):
    _store(port, 2026, with_coef=False)
    assert f"inconsistent:{port}:2026" in _keys(health.collect(NOW), ERROR)


def test_annee_suivante_absente_n_est_une_erreur_qu_apres_le_20_decembre(port):
    _store(port, 2026)
    assert f"missing-year:{port}:2027" not in _keys(health.collect(NOW))
    late = datetime(2026, 12, 21, 7, 0, tzinfo=timezone.utc)
    assert f"missing-year:{port}:2027" in _keys(health.collect(late), ERROR)


def test_prerequis_du_precalcul_annuel_verifies_des_octobre(port, monkeypatch):
    _store(port, 2026)
    monkeypatch.setattr(health.tide_model, "missing_model_files", lambda model, directory=None: (["a.nc"], ["a.nc", "b.nc"]))
    assert any(p.key.startswith("fes-missing") and p.level == WARNING for p in health.collect(NOW))
    assert any(p.key.startswith("fes-missing") and p.level == ERROR
               for p in health.collect(datetime(2026, 12, 21, 7, 0, tzinfo=timezone.utc)))
    # avant octobre, rien à signaler
    assert not any(p.key.startswith("fes-missing") for p in health.collect(datetime(2026, 8, 1, tzinfo=timezone.utc)))


def test_tache_en_echec_recente_signalee_puis_effacee_par_un_succes(port):
    params = '{"port_id": %d, "year": 2027, "model": "FES2014"}' % port
    job = db.enqueue_job("precompute", params, "test", NOW.isoformat())
    db.claim_next_job(NOW.isoformat())
    db.finish_job(job, "failed", 1, NOW.isoformat())
    assert any(p.key.startswith("job-failed:precompute") for p in health.collect(NOW))
    job2 = db.enqueue_job("precompute", params, "test", NOW.isoformat())
    db.claim_next_job(NOW.isoformat())
    db.finish_job(job2, "succeeded", 0, NOW.isoformat())
    assert not any(p.key.startswith("job-failed") for p in health.collect(NOW))


def test_sauvegarde_absente_puis_recente(tmp_db, tmp_path):
    assert "backup-none" in _keys(health.collect(NOW))
    backup.create_backup(directory=tmp_path / "bk")
    assert not any(p.key.startswith("backup") for p in health.collect(datetime.now(timezone.utc)))
    assert "backup-old" in _keys(health.collect(datetime.now(timezone.utc) + timedelta(days=3)), ERROR)


def test_worker_arrete(tmp_db):
    db.worker_heartbeat((NOW - timedelta(minutes=10)).isoformat(), None, "{}")
    assert "worker-down" in _keys(health.collect(NOW), ERROR)


def test_endpoint_admin_et_healthz(client, make_user):
    from tests.conftest import PASSWORD
    assert client.get("/healthz").json() == {"status": "ok"}
    make_user("root", is_admin=True)
    client.post("/api/auth/login", json={"username": "root", "password": PASSWORD})
    body = client.get("/api/admin/health").json()
    assert set(body) == {"ok", "errors", "warnings", "problems"}


# ---------------------------------------------------------------------------
# Alertes
# ---------------------------------------------------------------------------

@pytest.fixture()
def outbox(monkeypatch, tmp_db):
    sent = []
    monkeypatch.setattr(mailer, "enabled", lambda: True)
    monkeypatch.setattr(mailer, "BASE_URL", "https://exemple.test")
    monkeypatch.setenv("ALERT_EMAIL", "admin@example.org")

    def send_many(messages):
        messages = list(messages)
        sent.extend(messages)
        return [None] * len(messages)

    monkeypatch.setattr(mailer, "send_many", send_many)
    return sent


ERR = Problem(ERROR, "x", "Quelque chose ne va pas")


def test_alerte_une_seule_fois_puis_rappel_hebdomadaire_puis_resolution(outbox):
    assert alerts.notify([ERR], NOW) is True
    assert alerts.notify([ERR], NOW + timedelta(days=1)) is False        # même problème : pas de nouvel e-mail
    assert alerts.notify([ERR], NOW + timedelta(days=8)) is True         # rappel
    assert len(outbox) == 2
    assert alerts.notify([], NOW + timedelta(days=9)) is True            # résolu
    assert "ordre" in outbox[-1][1]
    assert alerts.notify([], NOW + timedelta(days=10)) is False


def test_nouvelle_erreur_declenche_un_email(outbox):
    alerts.notify([ERR], NOW)
    assert alerts.notify([ERR, Problem(ERROR, "y", "Autre chose")], NOW + timedelta(hours=1)) is True


def test_avertissements_seuls_ne_declenchent_rien(outbox):
    assert alerts.notify([Problem(WARNING, "w", "Bof")], NOW) is False and not outbox


def test_sans_courriel_configure_l_alerte_reste_dans_les_logs_et_sera_retentee(tmp_db, monkeypatch, capsys):
    monkeypatch.setattr(mailer, "enabled", lambda: False)
    assert alerts.notify([ERR], NOW) is False
    assert "Quelque chose ne va pas" in capsys.readouterr().err
    assert db.get_setting(alerts.STATE_KEY) is None


def test_alerte_tache_en_echec_limitee_a_une_par_jour_et_par_type(outbox):
    alerts.job_failed(1, "precompute", "Précalcul Binic 2027")
    alerts.job_failed(2, "precompute", "Précalcul Brest 2027")
    alerts.job_failed(3, "calibrate", "Recalage Binic")
    assert len(outbox) == 2


def test_destinataires_par_defaut_super_administrateurs(tmp_db, make_user, monkeypatch):
    monkeypatch.delenv("ALERT_EMAIL", raising=False)
    make_user("root", is_admin=True)
    make_user("alice")
    assert alerts.recipients() == ["root@example.org"]
