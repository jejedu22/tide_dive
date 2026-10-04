"""API publique : recherche de créneaux, droits d'accès."""


import pytest

from app import db

PORT = dict(name="Binic", latitude=48.6, longitude=-2.82)


@pytest.fixture()
def port_with_data(tmp_db):
    port_id = db.upsert_port(PORT["name"], PORT["latitude"], PORT["longitude"])
    # PM 06:00 UTC (08:00 locale, coef 95) et BM 12:15 UTC (14:15 locale) le 2026-07-01
    extrema = [
        ("2026-07-01T06:00:00+00:00", "PM", 9.0, 95.0),
        ("2026-07-01T12:15:00+00:00", "BM", 1.2, None),
        ("2026-07-01T18:30:00+00:00", "PM", 9.1, 90.0),
    ]
    sun = [("2026-07-01", "06:00", "21:45", "04:30", "23:15")]
    db.replace_year(port_id, 2026, [], extrema, sun, model="TEST")
    return port_id


def _search(client, port_id, **kw):
    params = dict(port_id=port_id, start="2026-07-01", end="2026-07-01", daylight="none", **kw)
    return client.get("/api/dive-windows", params=params)


def test_ports_vide(client):
    assert client.get("/api/ports").json() == []


def test_recherche_renvoie_les_etales_en_heure_locale(client, port_with_data):
    data = _search(client, port_with_data).json()
    assert [(r["kind"], r["time"]) for r in data["results"]] == [("PM", "08:00"), ("BM", "14:15"), ("PM", "20:30")]
    assert "rdv" not in data["results"][0], "pas de RDV pour un visiteur anonyme"


def test_bm_prend_le_coefficient_de_la_pm_voisine(client, port_with_data):
    bm = [r for r in _search(client, port_with_data).json()["results"] if r["kind"] == "BM"][0]
    assert bm["coefficient"] in (95.0, 90.0)


def test_filtre_coefficient_maximum(client, port_with_data):
    res = _search(client, port_with_data, max_coefficient=92).json()["results"]
    assert all(r["coefficient"] <= 92 for r in res)
    assert [r["time"] for r in res if r["kind"] == "PM"] == ["20:30"]


def test_filtre_phase(client, port_with_data):
    assert {r["kind"] for r in _search(client, port_with_data, tide_phase="BM").json()["results"]} == {"BM"}


def test_lumiere_nautique_exclut_l_etale_trop_tardive(client, port_with_data):
    res = client.get("/api/dive-windows", params=dict(
        port_id=port_with_data, start="2026-07-01", end="2026-07-01", daylight="nautical", margin_minutes=45,
    )).json()["results"]
    assert "20:30" in [r["time"] for r in res]            # dusk 23:15 : fenêtre 19:45–21:15 OK
    res = client.get("/api/dive-windows", params=dict(
        port_id=port_with_data, start="2026-07-01", end="2026-07-01", daylight="civil", margin_minutes=120,
    )).json()["results"]
    assert "20:30" not in [r["time"] for r in res]        # coucher 21:45 : fenêtre 18:30–22:30 dépasse


def test_port_inconnu_404(client):
    assert client.get("/api/dive-windows", params=dict(port_id=999, start="2026-07-01", end="2026-07-02")).status_code == 404


def test_periode_inversee_ou_trop_longue_refusee(client, port_with_data):
    r = client.get("/api/dive-windows", params=dict(port_id=port_with_data, start="2026-07-05", end="2026-07-01"))
    assert r.status_code == 422
    r = client.get("/api/dive-windows", params=dict(port_id=port_with_data, start="2026-01-01", end="2030-01-01"))
    assert r.status_code == 422


@pytest.mark.parametrize("path", ["/api/admin/jobs", "/api/admin/structures"])
def test_routes_protegees_refusent_l_anonyme(client, path):
    assert client.get(path).status_code in (401, 403)


def test_membre_simple_n_est_pas_administrateur(client, make_user):
    from tests.conftest import PASSWORD
    make_user("alice")
    client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    assert client.get("/api/admin/structures").status_code == 403
    assert client.get("/api/admin/jobs").status_code == 403


def test_creneaux_de_juin_conserves_quand_le_crepuscule_nautique_tombe_apres_minuit(client, tmp_db):
    """Brest, 21 juin : crépuscule nautique à 00:04 (lendemain). Une PM à 21:00 doit rester proposée."""
    port_id = db.upsert_port("Brest", 48.38, -4.49)
    extrema = [("2026-06-21T19:00:00+00:00", "PM", 7.0, 90.0)]    # 21:00 heure locale
    sun = [("2026-06-21", "06:16", "22:22", "04:35", "00:04")]
    db.replace_year(port_id, 2026, [], extrema, sun, model="TEST")
    res = client.get("/api/dive-windows", params=dict(
        port_id=port_id, start="2026-06-21", end="2026-06-21", daylight="nautical", margin_minutes=45,
    )).json()["results"]
    assert [r["time"] for r in res] == ["21:00"]
    assert res[0]["fully_in_daylight"] is True
