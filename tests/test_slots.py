from datetime import datetime
from zoneinfo import ZoneInfo

from app import slots

PARIS = ZoneInfo("Europe/Paris")


def test_rdv_arrondi_aux_5_minutes_inferieures():
    # exemple du README : étale 9h37, délai 2h15 → 7h22 → RDV 7h20
    assert slots.rdv_time(datetime(2026, 7, 1, 9, 37, tzinfo=PARIS), 135).strftime("%H:%M") == "07:20"


def test_rdv_sur_un_multiple_de_5_inchange():
    assert slots.rdv_time(datetime(2026, 7, 1, 9, 40, tzinfo=PARIS), 120).strftime("%H:%M") == "07:40"


def test_rdv_la_veille_quand_l_etale_est_tot():
    rdv = slots.rdv_time(datetime(2026, 7, 2, 1, 10, tzinfo=PARIS), 120)
    assert (rdv.date().isoformat(), rdv.strftime("%H:%M")) == ("2026-07-01", "23:10")


def test_local_time_gere_l_heure_d_ete():
    # 2026-07-01 07:00 UTC = 09:00 à Paris (UTC+2) ; 2026-01-15 07:00 UTC = 08:00 (UTC+1)
    assert slots.local_time("2026-07-01T07:00:00+00:00", PARIS).strftime("%H:%M") == "09:00"
    assert slots.local_time("2026-01-15T07:00:00+00:00", PARIS).strftime("%H:%M") == "08:00"


def test_local_time_sans_fuseau_est_en_utc():
    assert slots.local_time("2026-07-01T07:00:00", PARIS).strftime("%H:%M") == "09:00"


def test_coefficient_de_la_pleine_mer_la_plus_proche():
    pm = [(datetime(2026, 7, 1, 3, 0, tzinfo=PARIS), 80.0), (datetime(2026, 7, 1, 15, 30, tzinfo=PARIS), 84.0)]
    assert slots.nearest_pm_coef(datetime(2026, 7, 1, 10, 0, tzinfo=PARIS), pm) == 84.0
    assert slots.nearest_pm_coef(datetime(2026, 7, 1, 8, 0, tzinfo=PARIS), pm) == 80.0
    assert slots.nearest_pm_coef(datetime(2026, 7, 1, 8, 0, tzinfo=PARIS), []) is None
