"""
Carnet de plongées d'un membre : ses inscriptions aux créneaux passés (et du jour), dans toutes ses structures,
avec la présence pointée par l'encadrement. Les plongées comptées sont les présences pointées ; un créneau passé
non pointé apparaît comme « non pointé ».

Route : GET /api/me/logbook.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter

from . import db
from .auth import CurrentUser

router = APIRouter(prefix="/api/me")


@router.get("/logbook")
def logbook(user: CurrentUser):
    today = datetime.now(ZoneInfo("Europe/Paris")).date().isoformat()
    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT s.id, s.local_date, s.end_date, s.rdv_time, s.kind, s.local_time, s.note, s.coefficient,
                   COALESCE(p.name, s.location) AS place, t.label AS type_label, t.color AS type_color,
                   st.name AS structure, r.attendance, r.comment
            FROM slot_registrations r
            JOIN slot_selections s ON s.id = r.selection_id
            JOIN slot_types t ON t.id = s.type_id
            JOIN structures st ON st.id = s.structure_id
            LEFT JOIN ports p ON p.id = s.port_id
            WHERE r.user_id = ? AND s.local_date <= ?
            ORDER BY s.local_date DESC, s.rdv_time DESC
        """, (user["id"], today)).fetchall()
    entries = [{
        "id": r["id"], "date": r["local_date"], "end_date": r["end_date"], "rdv_time": r["rdv_time"],
        "kind": r["kind"], "time": r["local_time"], "note": r["note"], "coefficient": r["coefficient"],
        "place": r["place"], "type": {"label": r["type_label"], "color": r["type_color"]},
        "structure": r["structure"], "attendance": r["attendance"], "comment": r["comment"],
    } for r in rows]
    present = [e for e in entries if e["attendance"] == "present"]
    by_year = Counter(e["date"][:4] for e in present)
    places = Counter(e["place"] for e in present)
    return {
        "entries": entries,
        "totals": {"dives": len(present), "unmarked": sum(1 for e in entries if e["attendance"] is None),
                   "absent": sum(1 for e in entries if e["attendance"] in ("absent", "excused")),
                   "last": present[0]["date"] if present else None},
        "by_year": [{"year": y, "dives": n} for y, n in sorted(by_year.items(), reverse=True)],
        "places": [{"place": p, "dives": n} for p, n in places.most_common(5)],
    }
