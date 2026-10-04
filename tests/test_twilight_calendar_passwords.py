from datetime import date

from app import calendar_fr, passwords, twilight

BINIC = (48.6, -2.82, "Europe/Paris")


def test_ordre_des_horaires_solaires():
    for day in (date(2026, 1, 15), date(2026, 6, 21), date(2026, 10, 25)):
        t = twilight.day_sun_times(*BINIC, day)
        assert t["nautical_dawn"] < t["sunrise"] < t["sunset"] < t["nautical_dusk"], (day, t)


def test_jour_plus_long_en_ete_qu_en_hiver():
    summer = twilight.day_sun_times(*BINIC, date(2026, 6, 21))
    winter = twilight.day_sun_times(*BINIC, date(2026, 12, 21))
    assert summer["sunrise"] < winter["sunrise"] and summer["sunset"] > winter["sunset"]


def test_une_ligne_par_jour_et_annee_bissextile():
    assert len(twilight.year_sun_times(*BINIC, 2026)) == 365
    assert len(twilight.year_sun_times(*BINIC, 2028)) == 366


def test_jours_feries_2026():
    h = calendar_fr.public_holidays(2026)
    assert len(h) == 11
    assert h[date(2026, 4, 6)] == "Lundi de Pâques"      # Pâques 2026 : 5 avril
    assert date(2026, 5, 14) in h                          # Ascension
    assert date(2026, 5, 25) in h                          # Lundi de Pentecôte


def test_jours_feries_sur_plusieurs_annees():
    h = calendar_fr.public_holidays_range(date(2026, 12, 20), date(2027, 1, 2))
    assert set(h) == {date(2026, 12, 25), date(2027, 1, 1)}


def test_mots_de_passe_faibles_refuses():
    assert passwords.problems("court1A!")
    assert passwords.problems("motdepasse123456")
    assert passwords.problems("Zaaaa-1234-xyzw-qq")                       # 4 caractères répétés
    assert passwords.problems("Jerome-Legoff-2026!", ["jerome", "Le Goff"])  # contient le nom


def test_mot_de_passe_genere_conforme():
    for _ in range(20):
        assert passwords.problems(passwords.generate()) == []
