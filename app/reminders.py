"""
Rappels et alertes par e-mail, réglés par chaque structure (administration → Créneaux → Rappels et alertes).
Tous sont désactivés par défaut.

- Rappel de créneau : chaque inscrit (confirmé) reçoit un rappel N jours avant le créneau (1 : la veille).
- Créneau peu rempli : les administrateurs de la structure sont prévenus N jours avant un créneau qui n'a aucun
  inscrit, ou moins de la moitié de ses places confirmées.
- Certificat médical : le membre dont le CACI expire dans les N jours est prévenu (une fois par certificat).
- Désinscription tardive (selections.py, au moment de la désinscription) : les administrateurs sont prévenus quand
  un membre confirmé se désinscrit moins de N jours avant le créneau.

Chaque rappel n'est envoyé qu'une fois (table reminders_sent) : relancer la commande le même jour ne renvoie
rien. Lancé chaque matin par le planificateur :

    python -m app.reminders [--dry-run]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import accounts, db, diver, mailer

LOW_FILL = 0.5
KEEP = timedelta(days=400)      # rappels envoyés gardés en mémoire


def _today() -> date:
    return datetime.now(ZoneInfo("Europe/Paris")).date()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fr(d: str) -> str:
    return date.fromisoformat(d).strftime("%d/%m/%Y")


def managers(structure_id: int) -> list[sqlite3.Row]:
    """Administrateurs de la structure qui ont une adresse e-mail."""
    return [m for m in db.structure_members(structure_id) if m["structure_role"] == "manager" and m["email"]]


def _upcoming(conn: sqlite3.Connection, structure_id: int, start: date, end: date) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT s.*, COALESCE(p.name, s.location) AS port_name, t.label AS type_label,
               (SELECT COUNT(*) FROM slot_registrations r WHERE r.selection_id = s.id) AS regs
        FROM slot_selections s JOIN slot_types t ON t.id = s.type_id LEFT JOIN ports p ON p.id = s.port_id
        WHERE s.structure_id = ? AND s.local_date BETWEEN ? AND ? ORDER BY s.local_date, s.rdv_time
    """, (structure_id, start.isoformat(), end.isoformat())).fetchall()


Key = tuple[str, str, int]   # (kind, ref, compte) : un envoi


def _claim(conn: sqlite3.Connection, key: Key, record: bool) -> bool:
    """Réserve l'envoi `key` (record) ou vérifie seulement qu'il n'a pas eu lieu ; False s'il a déjà eu lieu."""
    if not record:
        return conn.execute("SELECT 1 FROM reminders_sent WHERE kind = ? AND ref = ? AND user_id = ?",
                            key).fetchone() is None
    try:
        conn.execute("INSERT INTO reminders_sent (kind, ref, user_id, sent_at) VALUES (?, ?, ?, ?)", (*key, _now()))
        return True
    except sqlite3.IntegrityError:
        return False


def release(keys: list[Key]) -> None:
    """Envois en échec : ils seront retentés au prochain passage."""
    with db.get_conn() as conn:
        conn.executemany("DELETE FROM reminders_sent WHERE kind = ? AND ref = ? AND user_id = ?", keys)


def _footer() -> str:
    return f"\n-- \n{mailer.APP_NAME}\n"


def slot_reminder(member, row, structure_name: str) -> tuple[str, str, str]:
    from .selections import place, when
    body = (f"{accounts.greeting(member)}\n\nPetit rappel : vous êtes inscrit(e) au créneau {when(row)} : "
            f"{place(row)} ({row['type_label']}, {structure_name}).\n\nSi vous ne pouvez plus venir, prévenez votre "
            f"structure ou désinscrivez-vous dans les délais :\n{mailer.link('mes-creneaux.html')}\n" + _footer())
    return member["email"], f"{mailer.APP_NAME} : rappel, créneau du {_fr(row['local_date'])}", body


def low_fill_alert(admin, rows: list, structure_name: str) -> tuple[str, str, str]:
    from .selections import place, when
    def line(r) -> str:
        places = f" sur {r['max_registrations']} place(s)" if r["max_registrations"] else ""
        return f"- {when(r)} : {place(r)} ({r['type_label']}), {r['regs']} inscrit(s){places}"
    lines = "\n".join(line(r) for r in rows)
    body = (f"{accounts.greeting(admin)}\n\nCes créneaux de {structure_name} sont encore peu remplis :\n\n{lines}\n\n"
            f"Une relance des membres, ou une newsletter, peut aider :\n{mailer.link('mes-creneaux.html')}\n" + _footer())
    return admin["email"], f"{mailer.APP_NAME} : créneaux peu remplis ({structure_name})", body


def caci_reminder(member, valid_until: str, structure_name: str) -> tuple[str, str, str]:
    body = (f"{accounts.greeting(member)}\n\nVotre certificat médical (CACI) n'est plus valable après le "
            f"{_fr(valid_until)}. Pensez à le renouveler, puis saisissez la date du nouveau certificat dans "
            f"« Ma fiche plongeur » (menu du compte), pour que {structure_name} puisse le valider :\n"
            f"{mailer.link('index.html')}\n" + _footer())
    return member["email"], f"{mailer.APP_NAME} : votre certificat médical expire bientôt", body


def collect(today: date | None = None, record: bool = True) -> list[tuple[tuple[str, str, str], list[Key]]]:
    """Messages à envoyer aujourd'hui, chacun avec ses envois (réservés dans reminders_sent si `record`)."""
    today = today or _today()
    out: list[tuple[tuple[str, str, str], list[Key]]] = []
    with db.get_conn() as conn:
        if record:
            conn.execute("DELETE FROM reminders_sent WHERE sent_at < ?",
                         ((datetime.now(timezone.utc) - KEEP).isoformat(timespec="seconds"),))
        for st in db.list_structures():
            sid, name = st["id"], st["name"]
            if st["remind_slot_days"]:
                for row in _upcoming(conn, sid, today + timedelta(days=1), today + timedelta(days=st["remind_slot_days"])):
                    regs = [r["user_id"] for r in db.list_registrations(sid, row["id"])]
                    confirmed = regs[:row["max_registrations"]] if row["max_registrations"] else regs
                    for uid in confirmed:
                        member = db.get_user(uid, sid)
                        key = ("slot", str(row["id"]), uid)
                        if member and member["email"] and _claim(conn, key, record):
                            out.append((slot_reminder(member, row, name), [key]))
            if st["alert_low_fill_days"]:
                low = [r for r in _upcoming(conn, sid, today, today + timedelta(days=st["alert_low_fill_days"]))
                       if r["regs"] == 0 or (r["max_registrations"] and r["regs"] < r["max_registrations"] * LOW_FILL)]
                for admin in managers(sid):
                    keys = [("low_fill", str(r["id"]), admin["id"]) for r in low]
                    fresh = [(r, k) for r, k in zip(low, keys) if _claim(conn, k, record)]
                    if fresh:
                        out.append((low_fill_alert(admin, [r for r, _ in fresh], name), [k for _, k in fresh]))
            if st["remind_caci_days"]:
                limit = today + timedelta(days=st["remind_caci_days"])
                for member in db.list_users(sid):
                    state = diver.caci_state(member, today, st["caci_validity_months"])
                    if state["state"] not in ("valid", "pending") or not member["email"]:
                        continue
                    key = ("caci", state["date"], member["id"])
                    if date.fromisoformat(state["valid_until"]) <= limit and _claim(conn, key, record):
                        out.append((caci_reminder(member, state["valid_until"], name), [key]))
    return out


def send_due(today: date | None = None) -> tuple[int, list[str]]:
    """Envoie les rappels du jour ; renvoie (envoyés, erreurs). Les envois en échec seront retentés."""
    due = collect(today)
    if not due:
        return 0, []
    errors = mailer.send_many([m for m, _ in due])
    failed = [(m, keys, err) for (m, keys), err in zip(due, errors) if err]
    if failed:
        release([k for _, keys, _ in failed for k in keys])
    return len(due) - len(failed), [f"{m[0]} : {err}" for m, _, err in failed]


def late_unregister_alert(structure_id: int, row, member) -> list[tuple[str, str, str]]:
    """Membre confirmé qui se désinscrit moins de N jours avant le créneau : e-mail aux administrateurs."""
    st = db.get_structure(structure_id)
    days = st["alert_late_unregister_days"] if st else None
    if not days or not mailer.enabled():
        return []
    left = (date.fromisoformat(row["local_date"]) - _today()).days
    if left < 0 or left >= days:
        return []
    from .selections import place, when
    who = accounts.display_name(member)
    out = []
    for admin in managers(structure_id):
        if admin["id"] == member["id"]:
            continue
        body = (f"{accounts.greeting(admin)}\n\n{who} vient de se désinscrire du créneau {when(row)} : {place(row)}, "
                f"{'aujourd’hui même' if left == 0 else f'à {left} jour(s)'}.\n\n{mailer.link('mes-creneaux.html')}\n"
                + _footer())
        out.append((admin["email"], f"{mailer.APP_NAME} : désinscription tardive ({who}, {_fr(row['local_date'])})", body))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.reminders", description="Rappels et alertes par e-mail")
    parser.add_argument("--dry-run", action="store_true", help="Afficher les e-mails dus sans les envoyer ni les noter")
    args = parser.parse_args(argv)
    db.init_db()
    if args.dry_run:
        for (to, subject, _), _ in collect(record=False):
            print(f"{to} : {subject}")
        return 0
    if not mailer.enabled():
        print(f"Rappels non envoyés : {mailer.disabled_reason()}.", file=sys.stderr)
        return 0
    sent, errors = send_due()
    for e in errors:
        print(f"[rappels] e-mail non envoyé à {e}", file=sys.stderr)
    print(f"[rappels] {sent} e-mail(s) envoyé(s).")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
