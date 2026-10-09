import pytest

from app import checks, db
from tests.synthetic import year_extrema, year_sun

YEAR = 2027


@pytest.fixture(scope="module")
def good():
    return year_extrema(YEAR), year_sun(YEAR)


def test_annee_saine_acceptee(good):
    report = checks.validate_year(*good, YEAR)
    assert report.ok, report.errors


def test_annee_vide_refusee():
    assert not checks.check_extrema([], YEAR).ok


def test_etale_manquante_detectee(good):
    ex = list(good[0])
    del ex[300:302]                       # une marée entière absente : trou de ~12 h
    assert any("cadence" in e or "alternance" in e for e in checks.check_extrema(ex, YEAR).errors)


def test_alternance_rompue_detectee(good):
    ex = list(good[0])
    ts, kind, h, c = ex[10]
    ex[10] = (ts, "PM" if kind == "BM" else "BM", h, c)
    assert any("alternance" in e for e in checks.check_extrema(ex, YEAR).errors)


def test_pm_sous_la_bm_detectee(good):
    ex = [(ts, k, -h, c) for ts, k, h, c in good[0]]   # hauteurs inversées
    assert any("incohérentes" in e for e in checks.check_extrema(ex, YEAR).errors)


def test_amplitude_hors_plage_detectee(good):
    ex = [(ts, k, 6 + (h - 6) * 10, c) for ts, k, h, c in good[0]]   # erreur d'unité ×10
    assert any("amplitude" in e for e in checks.check_extrema(ex, YEAR).errors)


def test_coefficients_absents_ou_aberrants(good):
    assert any("sans coefficient" in e for e in checks.check_extrema(year_extrema(YEAR, with_coef=False), YEAR).errors)
    ex = [(ts, k, h, 250.0 if c else c) for ts, k, h, c in good[0]]
    assert any("coefficient" in e for e in checks.check_extrema(ex, YEAR).errors)


def test_annee_tronquee_detectee(good):
    ex = good[0][: len(good[0]) // 2]
    errors = checks.check_extrema(ex, YEAR).errors
    assert any("fin d'année" in e for e in errors) and any("étales sur l'année" in e for e in errors)


def test_jour_sans_etale_detecte(good):
    ex = [e for e in good[0] if not e[0].startswith(f"{YEAR}-03-10")]
    assert not checks.validate_year(ex, good[1], YEAR).ok


def test_etale_du_31_decembre_au_soir_tombe_l_an_suivant_en_heure_locale(good):
    # 31/12 23:10 UTC = 1er janvier 00:10 à Paris : seule étale de ce jour-là, qui n'est pas dans l'année
    ex = [e for e in good[0] if e[0] < f"{YEAR}-12-31T23:00"] + [(f"{YEAR}-12-31T23:10:00+00:00", "BM", 1.0, None)]
    assert checks.check_per_day(ex, YEAR, "Europe/Paris").ok
    # dans l'année, un jour à une seule étale reste une erreur
    one = [e for e in good[0] if not e[0].startswith(f"{YEAR}-03-10")] + [(f"{YEAR}-03-10T12:00:00+00:00", "PM", 9.0, 80.0)]
    assert any("anormal le 10/03" in e for e in checks.check_per_day(one, YEAR, "Europe/Paris").errors)


def test_horaires_solaires_incomplets_ou_desordonnes(good):
    assert any("jours d'horaires" in e for e in checks.check_sun(good[1][:-5], YEAR).errors)
    sun = list(good[1])
    d, sr, ss, dawn, dusk = sun[100]
    sun[100] = (d, ss, sr, dawn, dusk)       # lever et coucher inversés
    assert any("désordre" in e for e in checks.check_sun(sun, YEAR).errors)


def test_crepuscule_apres_minuit_n_est_pas_une_erreur():
    # Brest, juin : crépuscule nautique à 00:04 (lendemain)
    assert checks.check_sun(year_sun(YEAR, 48.38, -4.49), YEAR).ok


def test_controle_des_donnees_stockees(tmp_db):
    port_id = db.upsert_port("Binic", 48.6, -2.82)
    ex, sun = year_extrema(YEAR), year_sun(YEAR)
    db.replace_year(port_id, YEAR, [], ex, sun, model="TEST")
    assert checks.validate_stored_year(port_id, YEAR).ok
    assert not checks.validate_stored_year(port_id, YEAR + 1).ok      # année absente
