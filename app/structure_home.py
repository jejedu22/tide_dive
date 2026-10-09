"""
Tableau de bord d'une structure, pour ses administrateurs (et les super administrateurs, structure au choix).

- À faire : réglages manquants (pas de type de créneau, pas de port par défaut…), certificats médicaux à valider,
  membres sans CACI valable (si la structure le vérifie), invitations et comptes en attente, créneaux passés dont
  la présence n'est pas pointée, créneaux à venir peu remplis ou complets avec une file d'attente.
- Statistiques sur une période (12 mois par défaut) : par type de créneau (créneaux, inscriptions, taux de
  remplissage, présents), par mois, et par membre (inscriptions, présences, absences), avec les membres sans
  aucune inscription.

Routes : GET /api/admin/structure-dashboard, GET /api/admin/structure-stats (?structure_id= pour un super
administrateur).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query

from . import accounts, db, diver
from .auth import CurrentManager, scope_structure

router = APIRouter(prefix="/api/admin")

SOON_DAYS = 14          # créneaux à venir examinés (peu remplis, complets)
LOW_FILL = 0.5          # « peu rempli » : moins de la moitié des places confirmées
ATTENDANCE_DAYS = 30    # créneaux passés récents dont la présence est attendue
ACTIVE_DAYS = 30


def _today() -> date:
    return datetime.now(ZoneInfo("Europe/Paris")).date()


def setting_issues(st, active_types: int, managers: int) -> list[str]:
    """Réglages manquants d'une structure (partagé avec le tableau de bord du super administrateur)."""
    issues = []
    if not managers:
        issues.append("aucun administrateur")
    if not active_types:
        issues.append("aucun type de créneau proposé")
    if st["default_port_id"] is None:
        issues.append("pas de port par défaut")
    return issues


def _slot_label(r) -> dict:
    return {"id": r["id"], "date": r["local_date"], "end_date": r["end_date"], "rdv_time": r["rdv_time"],
            "place": r["note"] or r["port_name"], "type": r["type_label"], "color": r["type_color"],
            "max_registrations": r["max_registrations"], "registrations": r["regs"]}


@router.get("/structure-dashboard")
def structure_dashboard(actor: CurrentManager, structure_id: int | None = None):
    sid = scope_structure(actor, structure_id)
    st = db.get_structure(sid)
    today = _today()
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=ACTIVE_DAYS)).isoformat()
    with db.get_conn() as conn:
        counts = conn.execute("""
            SELECT
              (SELECT COUNT(*) FROM memberships WHERE structure_id = :sid) AS members,
              (SELECT COUNT(*) FROM memberships WHERE structure_id = :sid AND role = 'manager') AS managers,
              (SELECT COUNT(*) FROM slot_types WHERE structure_id = :sid AND active) AS active_types,
              (SELECT COUNT(*) FROM slot_selections WHERE structure_id = :sid
                 AND COALESCE(end_date, local_date) >= :today) AS upcoming,
              (SELECT COUNT(*) FROM slot_registrations r JOIN slot_selections s ON s.id = r.selection_id
                 WHERE s.structure_id = :sid AND r.created_at >= :since) AS registrations_30d,
              (SELECT COUNT(*) FROM users u JOIN memberships m ON m.user_id = u.id
                 WHERE m.structure_id = :sid AND u.last_login_at >= :since) AS active_30d,
              (SELECT COUNT(*) FROM structure_invitations WHERE structure_id = :sid AND expires_at > :now)
                 AS invitations,
              (SELECT COUNT(*) FROM users u JOIN memberships m ON m.user_id = u.id
                 WHERE m.structure_id = :sid AND substr(u.password_hash, 1, 1) = '!') AS pending_accounts,
              (SELECT COUNT(*) FROM dive_sites WHERE structure_id = :sid) AS sites
        """, {"sid": sid, "today": today.isoformat(), "since": since, "now": now.isoformat()}).fetchone()
        slots_sql = """
            SELECT s.id, s.local_date, s.end_date, s.rdv_time, s.note, s.max_registrations,
                   COALESCE(p.name, s.location) AS port_name, t.label AS type_label, t.color AS type_color,
                   (SELECT COUNT(*) FROM slot_registrations r WHERE r.selection_id = s.id) AS regs,
                   (SELECT COUNT(*) FROM slot_registrations r WHERE r.selection_id = s.id
                      AND r.attendance IS NOT NULL) AS marked
            FROM slot_selections s
            JOIN slot_types t ON t.id = s.type_id
            LEFT JOIN ports p ON p.id = s.port_id
            WHERE s.structure_id = :sid AND s.local_date BETWEEN :start AND :end
            ORDER BY s.local_date, s.rdv_time
        """
        soon = conn.execute(slots_sql, {"sid": sid, "start": today.isoformat(),
                                        "end": (today + timedelta(days=SOON_DAYS)).isoformat()}).fetchall()
        recent = conn.execute(slots_sql, {"sid": sid, "start": (today - timedelta(days=ATTENDANCE_DAYS)).isoformat(),
                                          "end": today.isoformat()}).fetchall()
    issues = setting_issues(st, counts["active_types"], counts["managers"])
    low, full = [], []
    for r in soon:
        cap = r["max_registrations"]
        if cap is not None and r["regs"] > cap:
            full.append({**_slot_label(r), "waiting": r["regs"] - cap})
        elif r["regs"] == 0 or (cap is not None and r["regs"] < cap * LOW_FILL):
            low.append(_slot_label(r))
    unmarked = [_slot_label(r) for r in recent if r["regs"] and not r["marked"]]
    # certificats médicaux des membres
    divers = accounts.parse_features(st["disabled_features"])["divers"]   # fonction désactivée : rien à suivre
    caci = {"pending": 0, "missing": 0, "expired": 0, "expiring": 0, "check": divers and bool(st["caci_check"])}
    soon_limit = today + timedelta(days=30)
    for m in (db.list_users(sid) if divers else []):
        state = diver.caci_state(m, today, st["caci_validity_months"])
        if state["state"] in ("pending", "missing", "expired"):
            caci[state["state"]] += 1
        if state["state"] in ("valid", "pending") and date.fromisoformat(state["valid_until"]) <= soon_limit:
            caci["expiring"] += 1
    return {
        "structure": {"id": sid, "name": st["name"]},
        "counts": {k: counts[k] for k in counts.keys()},
        "issues": issues,
        "caci": caci,
        "low_fill": low,
        "full": full,
        "unmarked": unmarked,
        "soon_days": SOON_DAYS,
        "attendance_days": ATTENDANCE_DAYS,
        "recent": [{"at": r["at"], "actor": r["actor_name"], "action": r["action"], "target": r["target"]}
                   for r in db.list_audit(sid, limit=8)],
    }


@router.get("/structure-stats")
def structure_stats(actor: CurrentManager, structure_id: int | None = None,
                    months: int = Query(12, ge=1, le=36)):
    """Statistiques sur les `months` derniers mois (jusqu'à aujourd'hui, créneaux passés et du jour)."""
    sid = scope_structure(actor, structure_id)
    today = _today()
    first = today.year * 12 + today.month - 1 - (months - 1)     # premier mois de la période
    start = date(first // 12, first % 12 + 1, 1)
    params = {"sid": sid, "start": start.isoformat(), "end": today.isoformat()}
    with db.get_conn() as conn:
        slots = conn.execute("""
            SELECT s.id, s.local_date, s.max_registrations, s.type_id, t.label, t.color,
                   COUNT(r.user_id) AS regs,
                   SUM(r.attendance = 'present') AS present,
                   SUM(r.attendance = 'absent') AS absent,
                   SUM(r.attendance = 'excused') AS excused,
                   SUM(r.attendance IS NOT NULL) AS marked
            FROM slot_selections s
            JOIN slot_types t ON t.id = s.type_id
            LEFT JOIN slot_registrations r ON r.selection_id = s.id
            WHERE s.structure_id = :sid AND s.local_date BETWEEN :start AND :end
            GROUP BY s.id ORDER BY s.local_date
        """, params).fetchall()
        per_member = conn.execute("""
            SELECT u.id, u.username, u.first_name, u.last_name, m.role,
                   COUNT(r.selection_id) AS regs,
                   SUM(r.attendance = 'present') AS present,
                   SUM(r.attendance = 'absent') AS absent,
                   SUM(r.attendance = 'excused') AS excused,
                   MAX(s.local_date) AS last_slot
            FROM memberships m
            JOIN users u ON u.id = m.user_id
            LEFT JOIN slot_selections s ON s.structure_id = m.structure_id AND s.local_date BETWEEN :start AND :end
            LEFT JOIN slot_registrations r ON r.selection_id = s.id AND r.user_id = u.id
            WHERE m.structure_id = :sid
            GROUP BY u.id
        """, params).fetchall()
    by_type: dict[int, dict] = {}
    by_month: dict[str, dict] = {}
    m = start
    while m <= today:
        by_month[m.isoformat()[:7]] = {"month": m.isoformat()[:7], "slots": 0, "registrations": 0, "present": 0}
        m = (m + timedelta(days=32)).replace(day=1)
    totals = {"slots": 0, "registrations": 0, "present": 0, "absent": 0, "excused": 0, "marked": 0,
              "capacity": 0, "confirmed_in_capped": 0}
    for s in slots:
        # inscriptions confirmées : au plus le nombre de places (au-delà : file d'attente)
        cap = s["max_registrations"]
        confirmed = min(s["regs"], cap) if cap is not None else s["regs"]
        t = by_type.setdefault(s["type_id"], {"type": s["label"], "color": s["color"], "slots": 0, "registrations": 0,
                                              "present": 0, "marked": 0, "capacity": 0, "confirmed_in_capped": 0})
        month = by_month.get(s["local_date"][:7])
        for bucket in (t, totals):
            bucket["slots"] += 1
            bucket["registrations"] += confirmed
            bucket["present"] += s["present"] or 0
            bucket["marked"] += s["marked"] or 0
            if cap is not None:
                bucket["capacity"] += cap
                bucket["confirmed_in_capped"] += confirmed
        totals["absent"] += s["absent"] or 0
        totals["excused"] += s["excused"] or 0
        if month:
            month["slots"] += 1
            month["registrations"] += confirmed
            month["present"] += s["present"] or 0

    def rates(b: dict) -> dict:
        return {**b,
                "fill_rate": round(b["confirmed_in_capped"] / b["capacity"], 3) if b["capacity"] else None,
                "attendance_rate": round(b["present"] / b["marked"], 3) if b["marked"] else None}

    members = sorted(({
        "id": r["id"], "display_name": accounts.display_name(r), "username": r["username"], "role": r["role"],
        "registrations": r["regs"], "present": r["present"] or 0, "absent": r["absent"] or 0,
        "excused": r["excused"] or 0, "last_slot": r["last_slot"] if r["regs"] else None,
    } for r in per_member), key=lambda x: (-x["registrations"], x["display_name"].lower()))
    return {
        "start": start.isoformat(), "end": today.isoformat(), "months": months,
        "totals": rates(totals),
        "by_type": sorted((rates(b) for b in by_type.values()), key=lambda b: -b["slots"]),
        "by_month": list(by_month.values()),
        "members": members,
        "inactive": [x for x in members if not x["registrations"]],
    }
