"""
Tableau de bord du super administrateur : l'application en un coup d'œil.

- Par structure : membres, administrateurs, créneaux à venir, inscriptions des 30 derniers jours, sites, dernière
  connexion d'un administrateur, et les réglages manquants (pas d'administrateur, pas de type de créneau, pas de
  port par défaut…).
- Global : comptes, comptes actifs (30 jours), ports et années calculées, dernières
  actions du journal.

Route : GET /api/admin/dashboard (super administrateurs).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter

from . import db
from .auth import CurrentSuperAdmin
from .structure_home import setting_issues

router = APIRouter(prefix="/api/admin")

ACTIVE_DAYS = 30
INACTIVE_ADMIN_DAYS = 90     # aucun administrateur connecté depuis : structure signalée


def _structures(today: date, since: str) -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT st.id, st.name, st.default_port_id, st.caci_check,
              (SELECT COUNT(*) FROM memberships m WHERE m.structure_id = st.id) AS members,
              (SELECT COUNT(*) FROM memberships m WHERE m.structure_id = st.id AND m.role = 'manager') AS managers,
              (SELECT COUNT(*) FROM slot_types t WHERE t.structure_id = st.id AND t.active) AS active_types,
              (SELECT COUNT(*) FROM slot_selections s WHERE s.structure_id = st.id
                 AND COALESCE(s.end_date, s.local_date) >= :today) AS upcoming,
              (SELECT COUNT(*) FROM slot_selections s WHERE s.structure_id = st.id) AS selections,
              (SELECT COUNT(*) FROM slot_registrations r JOIN slot_selections s ON s.id = r.selection_id
                 WHERE s.structure_id = st.id AND r.created_at >= :since) AS registrations_30d,
              (SELECT COUNT(*) FROM dive_sites d WHERE d.structure_id = st.id) AS sites,
              (SELECT MAX(u.last_login_at) FROM users u JOIN memberships m ON m.user_id = u.id
                 WHERE m.structure_id = st.id AND m.role = 'manager') AS last_admin_login,
              (SELECT MAX(u.last_login_at) FROM users u JOIN memberships m ON m.user_id = u.id
                 WHERE m.structure_id = st.id) AS last_login
            FROM structures st ORDER BY st.name COLLATE NOCASE
        """, {"today": today.isoformat(), "since": since}).fetchall()
    out = []
    stale = (datetime.now(timezone.utc) - timedelta(days=INACTIVE_ADMIN_DAYS)).isoformat()
    for r in rows:
        issues = setting_issues(r, r["active_types"], r["managers"])
        if r["managers"] and (not r["last_admin_login"] or r["last_admin_login"] < stale):
            issues.append(f"aucun administrateur connecté depuis {INACTIVE_ADMIN_DAYS} jours")
        if r["members"] and not r["selections"]:
            issues.append("aucun créneau choisi")
        out.append({**{k: r[k] for k in r.keys() if k not in ("default_port_id", "caci_check")},
                    "caci_check": bool(r["caci_check"]), "issues": issues})
    return out


@router.get("/dashboard")
def dashboard(admin: CurrentSuperAdmin):
    today = date.today()
    since = (datetime.now(timezone.utc) - timedelta(days=ACTIVE_DAYS)).isoformat()
    with db.get_conn() as conn:
        users = conn.execute("SELECT COUNT(*) AS n, SUM(last_login_at >= ?) AS active, SUM(is_admin) AS supers, "
                             "SUM(substr(password_hash, 1, 1) = '!') AS pending FROM users", (since,)).fetchone()
        upcoming = conn.execute("SELECT COUNT(*) FROM slot_selections WHERE COALESCE(end_date, local_date) >= ?",
                                (today.isoformat(),)).fetchone()[0]
        regs = conn.execute("SELECT COUNT(*) FROM slot_registrations WHERE created_at >= ?", (since,)).fetchone()[0]
    ports = db.list_ports()
    years = db.years_by_port()
    # la santé (contrôles des données, plus longs) se lit à part : GET /api/admin/health, chargé par la page en
    # même temps, pour que le tableau de bord s'affiche tout de suite
    return {
        "users": {"total": users["n"], "active_30d": users["active"] or 0, "super_admins": users["supers"] or 0,
                  "pending_invites": users["pending"] or 0},
        "selections": {"upcoming": upcoming, "registrations_30d": regs},
        "ports": [{"id": p["id"], "name": p["name"], "years": years.get(p["id"], []),
                   "has_current_year": today.year in years.get(p["id"], [])} for p in ports],
        "structures": _structures(today, since),
        "recent": [{"at": r["at"], "actor": r["actor_name"], "structure": r["structure_name"], "action": r["action"],
                    "target": r["target"]} for r in db.list_audit(limit=10)],
    }
