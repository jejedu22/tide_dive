"""Vacances scolaires par académie : académie d'un port (position ou choix), d'une structure (choix ou port
d'attache), calendriers et synchronisation de toutes les académies utilisées."""

import pytest

from app import academies, calendar_fr, db
from tests.conftest import login


@pytest.mark.parametrize("lat, lon, expected", [
    (51.035, 2.377, "Lille"), (50.73, 1.59, "Lille"), (50.21, 1.62, "Amiens"), (49.93, 1.08, "Normandie"),
    (49.64, -1.62, "Normandie"), (48.87, -1.82, "Normandie"), (48.635, -1.51, "Normandie"),
    (48.65, -2.02, "Rennes"), (48.38, -4.49, "Rennes"), (47.39, -2.96, "Rennes"), (47.35, -3.15, "Rennes"),
    (47.27, -2.20, "Nantes"), (46.70, -2.35, "Nantes"), (46.2575, -1.19, "Nantes"),
    (46.16, -1.15, "Poitiers"), (46.21, -1.37, "Poitiers"), (45.62, -1.03, "Poitiers"), (45.95, -0.97, "Poitiers"),
    (45.569, -1.067, "Bordeaux"), (45.45, -0.92, "Bordeaux"), (44.66, -1.17, "Bordeaux"), (43.38, -1.78, "Bordeaux"),
    (43.29, 5.36, None), (45.76, 4.83, None),
])
def test_academie_d_apres_la_position(lat, lon, expected):
    assert academies.academy_for(lat, lon) == expected


def test_academie_des_ports_et_des_structures(tmp_db, make_structure):
    royan = db.create_port("Royan", 45.62, -1.03, "Europe/Paris", 3.42, False)
    marseille = db.create_port("Marseille", 43.29, 5.36, "Europe/Paris", 0.5, False)
    assert calendar_fr.port_academy(db.get_port(royan)) == "Poitiers"
    assert calendar_fr.port_academy(db.get_port(marseille)) == calendar_fr.SCHOOL_ACADEMY      # par défaut
    db.update_port(marseille, academy="Aix-Marseille")
    assert calendar_fr.port_academy(db.get_port(marseille)) == "Aix-Marseille"
    sid = make_structure("Club")
    assert calendar_fr.structure_academy(sid) == calendar_fr.SCHOOL_ACADEMY                   # sans port d'attache
    db.update_structure_settings(sid, default_port_id=royan)
    assert calendar_fr.structure_academy(sid) == "Poitiers"
    db.update_structure_settings(sid, school_academy="Nantes")
    assert calendar_fr.structure_academy(sid) == "Nantes"
    assert set(calendar_fr.academies_in_use()) == {calendar_fr.SCHOOL_ACADEMY, "Poitiers", "Aix-Marseille", "Nantes"}


def test_calendriers(make_structure, make_user, new_client, client):
    royan = db.create_port("Royan", 45.62, -1.03, "Europe/Paris", 3.42, False)
    db.replace_school_holidays("Poitiers", [("2027-02-06", "2027-02-22", "Vacances d'Hiver", "Zone A")])
    db.replace_school_holidays("Rennes", [("2027-02-13", "2027-03-01", "Vacances d'Hiver", "Zone B")])
    sid = make_structure("Club")
    db.update_structure_settings(sid, default_port_id=royan)
    make_user("alice", structure_id=sid, structure_role="manager")
    alice = new_client()
    login(alice, "alice")
    r = alice.get("/api/calendar-days?start=2027-02-01&end=2027-02-28").json()
    assert r["academy"] == "Poitiers" and r["zone"] == "Zone A" and "2027-02-08" in r["days"]
    assert "2027-02-25" not in r["days"]                                    # vacances de Rennes : non
    anon = client.get("/api/calendar-days?start=2027-02-01&end=2027-02-28").json()
    assert anon["academy"] == "Rennes" and "2027-02-25" in anon["days"]     # visiteur : académie par défaut
    by_port = client.get(f"/api/calendar-days?start=2027-02-01&end=2027-02-28&port_id={royan}").json()
    assert by_port["academy"] == "Poitiers"
    search = client.get(f"/api/dive-windows?port_id={royan}&start=2027-02-01&end=2027-02-28").json()
    assert search["school_holidays"]["academy"] == "Poitiers" and search["school_holidays"]["zone"] == "Zone A"


def test_reglages(make_structure, make_user, new_client, monkeypatch):
    synced = []
    monkeypatch.setattr(calendar_fr, "sync_school_holidays", lambda a: synced.append(a) or 3)
    sid = make_structure("Club")
    make_user("alice", structure_id=sid, structure_role="manager")
    make_user("root", is_admin=True)
    alice, root = new_client(), new_client()
    login(alice, "alice")
    login(root, "root")
    r = alice.patch(f"/api/admin/structures/{sid}/settings", json={"school_academy": "Poitiers"})
    assert r.status_code == 200 and r.json()["school_academy"] == "Poitiers" and synced == ["Poitiers"]
    assert alice.patch(f"/api/admin/structures/{sid}/settings", json={"school_academy": "Atlantide"}).status_code == 422
    r = alice.patch(f"/api/admin/structures/{sid}/settings", json={"school_academy": ""})
    assert r.json()["school_academy"] is None and r.json()["school_academy_effective"] == calendar_fr.SCHOOL_ACADEMY
    port = root.post("/api/admin/ports", json={"name": "Hendaye", "latitude": 43.38, "longitude": -1.78}).json()
    assert port["academy"] is None and port["academy_effective"] == "Bordeaux"
    port = root.patch(f"/api/admin/ports/{port['id']}", json={"academy": "Toulouse"}).json()
    assert port["academy_effective"] == "Toulouse"
    port = root.patch(f"/api/admin/ports/{port['id']}", json={"academy": None}).json()
    assert port["academy_effective"] == "Bordeaux"
    assert root.patch(f"/api/admin/ports/{port['id']}", json={"academy": "Atlantide"}).status_code == 422
    status = root.get("/api/admin/status").json()
    assert {h["academy"] for h in status["school_holidays"]} >= {"Bordeaux", calendar_fr.SCHOOL_ACADEMY}


def test_synchronisation_de_toutes_les_academies(tmp_db, monkeypatch):
    db.create_port("Royan", 45.62, -1.03, "Europe/Paris", 3.42, False)
    db.create_port("Calais", 50.97, 1.87, "Europe/Paris", 4.0, False)

    def fetch(academy):
        if academy == "Lille":
            raise OSError("réseau")
        return [("2027-02-06", "2027-02-22", "Hiver", "Zone A")]

    monkeypatch.setattr(calendar_fr, "fetch_school_holidays", fetch)
    out = calendar_fr.sync_all_school_holidays()
    assert out["Poitiers"] == 1 and out[calendar_fr.SCHOOL_ACADEMY] == 1 and str(out["Lille"]).startswith("échec")
    assert db.school_holidays_zone("Poitiers") == "Zone A"


def test_parse_records_zone():
    rows = calendar_fr.parse_records([{"start_date": "2027-02-05T23:00:00+00:00", "end_date": "2027-02-21T23:00:00+00:00",
                                       "description": "Vacances d'Hiver", "zones": "Zone A", "population": "Élèves"}])
    assert rows == [("2027-02-06", "2027-02-22", "Vacances d'Hiver", "Zone A")]
