"""Jours fériés et vacances scolaires pour le calendrier des créneaux choisis (GET /api/calendar-days)."""

from app import calendar_fr, db


def test_jours_feries_et_vacances(client):
    db.replace_school_holidays(calendar_fr.SCHOOL_ACADEMY, [("2026-10-17", "2026-11-02", "Vacances de la Toussaint")])
    db.replace_school_holidays("Autre académie", [("2026-10-01", "2026-12-01", "Ailleurs")])
    r = client.get("/api/calendar-days", params={"start": "2026-10-26", "end": "2026-11-15"})
    assert r.status_code == 200                       # pas de connexion requise
    body = r.json()
    assert body["academy"] == calendar_fr.SCHOOL_ACADEMY
    days = body["days"]
    # vacances : du 17 octobre au 1er novembre inclus (le 2 est le jour de la reprise)
    assert days["2026-10-26"] == {"holiday": None, "school_holiday": "Vacances de la Toussaint"}
    assert days["2026-11-01"] == {"holiday": "Toussaint", "school_holiday": "Vacances de la Toussaint"}
    assert days["2026-11-11"] == {"holiday": "Armistice 1918", "school_holiday": None}
    assert "2026-11-02" not in days and "2026-11-03" not in days   # jours ordinaires omis
    assert sorted(days) == ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30",
                            "2026-10-31", "2026-11-01", "2026-11-11"]


def test_periode_validee(client):
    assert client.get("/api/calendar-days", params={"start": "2026-11-15", "end": "2026-11-01"}).status_code == 422
    assert client.get("/api/calendar-days", params={"start": "2026-01-01", "end": "2026-03-04"}).status_code == 422
    assert client.get("/api/calendar-days", params={"start": "2026-01-01", "end": "2026-03-03"}).status_code == 200
    assert client.get("/api/calendar-days", params={"start": "pas-une-date", "end": "2026-03-03"}).status_code == 422
