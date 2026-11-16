"""Ports et niveaux moyens du Shom (RAM, ram.py) : données, correspondances, import dans l'onglet Ports."""

from app import db, ram
from app.ports_catalog import PORTS
from tests.conftest import login


def test_donnees_du_shom():
    sites = ram.sites()
    assert len(sites) >= 180 and {s["zone"] for s in sites} == set(ram.ZONES)
    by = {s["name"]: s for s in sites}
    assert by["Saint-Malo"]["nm"] == 6.78 and by["Brest"]["nm"] == 4.14 and by["Hendaye"]["nm"] == 2.52
    for s in sites:
        assert 43 < s["latitude"] < 51.5 and -5.2 < s["longitude"] < 2.6 and 0.5 < s["nm"] < 8, s["name"]
    assert "Licence Ouverte" in ram.metadata()["source"]


def test_correspondances():
    assert ram.match("Audierne", 48.0059, -4.5336)["name"] == "Audierne (Quai Pelletan)"         # mot commun, 1,8 km
    assert ram.match("Quiberon (Port-Maria)", 47.4808, -3.1197)["name"] == "Port-Maria"
    assert ram.match("Le Tréport", 50.06, 1.37)["name"] == "Le Tréport"                            # même nom
    assert ram.match("Spot perdu", 47.0, -6.5) is None                                             # au large, loin de tout
    fouesnant = next(p for p in PORTS if p["name"] == "Fouesnant")                                 # aucun mot commun, > 1,5 km
    assert ram.match("Fouesnant", fouesnant["latitude"], fouesnant["longitude"]) is None


def test_import(make_user, new_client):
    make_user("root", is_admin=True)
    c = new_client()
    login(c, "root")
    # ports existants : sans niveau, estimé, saisi à la main (différent du Shom), hors des zones choisies
    sm = db.create_port("Saint-Malo", 48.6497, -2.0256, "Europe/Paris", None, True)
    brest = db.create_port("Brest", 48.3833, -4.4931, "Europe/Paris", 4.32, True)
    binic = db.create_port("Binic", 48.6017, -2.8244, "Europe/Paris", 6.20, True)
    db.update_port(binic, offset_estimated=True)
    db.create_port("Hendaye", 43.37933, -1.78467, "Europe/Paris", None, True)
    zones = ["Abords de Saint-Malo", "Iroise, Sud Bretagne"]
    plan = c.post("/api/admin/ports/import-ram/preview", json={"zones": zones}).json()
    assert [i["name"] for i in plan["fill"]] == ["Saint-Malo"] and [i["name"] for i in plan["replace"]] == ["Binic"]
    assert [i["name"] for i in plan["differ"]] == ["Brest"] and plan["differ"][0]["nm"] == 4.14
    names = {p["name"] for p in plan["create"]}
    assert "Cancale" in names and "Douarnenez" in names and "Saint-Malo" not in names and "Hendaye" not in names
    assert db.get_port(sm)["offset_zh_m"] is None                                                  # aperçu : rien de changé
    r = c.post("/api/admin/ports/import-ram", json={"zones": zones}).json()
    assert r["created"] == len(plan["create"])
    assert db.get_port(sm)["offset_zh_m"] == 6.78 and db.get_port(binic)["offset_zh_m"] == 6.36
    assert not db.get_port(binic)["offset_estimated"] and db.get_port(brest)["offset_zh_m"] == 4.32   # saisi : gardé
    cancale = next(p for p in db.list_ports() if p["name"] == "Cancale")
    assert cancale["offset_zh_m"] == 7.2 and not cancale["auto_precompute"] and cancale["api_maree_site"] == "cancale"
    # deuxième passage : plus rien à créer ; remplacer les valeurs saisies sur demande
    again = c.post("/api/admin/ports/import-ram/preview", json={"zones": zones, "replace_manual": True}).json()
    assert again["create"] == [] and [i["name"] for i in again["replace"]] == ["Brest"]
    c.post("/api/admin/ports/import-ram", json={"zones": zones, "replace_manual": True})
    assert db.get_port(brest)["offset_zh_m"] == 4.14
    # aperçu global et contrôles
    assert c.get("/api/admin/ports/ram").json()["zones"][0] == {"name": "Abords de Dunkerque", "sites": 4}
    assert c.post("/api/admin/ports/import-ram", json={"zones": ["Méditerranée"]}).status_code == 422
    assert c.post("/api/admin/ports/import-ram", json={"zones": []}).status_code == 422


def test_reserve_aux_super_administrateurs(make_structure, make_user, new_client):
    sid = make_structure("Club")
    make_user("alice", structure_id=sid, structure_role="manager")
    c = new_client()
    login(c, "alice")
    assert c.post("/api/admin/ports/import-ram", json={"zones": ram.ZONES}).status_code == 403
    assert c.get("/api/admin/ports/ram").status_code == 403
