"""
Créneaux choisis au format iCalendar (RFC 5545), pour les calendriers des téléphones (Android, iPhone) et
des ordinateurs : fichier .ics à importer, ou abonnement (lien personnel, voir calendar_feed.py).

Module sans accès à la base : il met en forme les créneaux tels que les sert l'API (selections._selection_out).
Heures écrites en UTC (suffixe Z) : pas de fuseau à décrire, chaque calendrier les affiche dans le sien.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Paris")          # heures locales des créneaux (ports de la côte française)
PRODID = "-//Calendive//Creneaux de plongee//FR"
AFTER_TIDE = timedelta(hours=1)        # créneau d'étale : jusqu'à une heure après l'étale
CUSTOM_LENGTH = timedelta(hours=3)     # créneau personnalisé d'un jour : 3 h à partir du RDV
REFRESH = "PT1H"                       # abonnement : rafraîchi toutes les heures (si le calendrier le permet)


def escape(text: str) -> str:
    """Texte d'une propriété : antislash, point-virgule, virgule et retours à la ligne échappés."""
    return (text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n"))


def fold(line: str) -> str:
    """Lignes de 75 octets au plus, suites précédées d'une espace (sans couper un caractère UTF-8)."""
    out, current, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > (75 if not out else 74):
            out.append(current)
            current, size = "", 0
        current += ch
        size += n
    out.append(current)
    return "\r\n ".join(out)


def _utc(local_date: str, local_time: str) -> str:
    dt = datetime.fromisoformat(f"{local_date}T{local_time}").replace(tzinfo=TZ)
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _fmt_m(h: float) -> str:
    return f"{h:.2f}".replace(".", ",") + " m"


def _times(s: dict) -> tuple[str, str, bool]:
    """(début, fin, journée entière) d'un créneau."""
    water = s.get("water")
    if water:                                       # plage de hauteur d'eau : du RDV à la fin de la plage
        return _utc(s["rdv"]["date"], s["rdv"]["time"]), _utc(water["end_date"], water["end"]), False
    if s.get("custom"):
        if s.get("end_date"):                       # séjour : journées entières (fin exclue)
            end = date.fromisoformat(s["end_date"]) + timedelta(days=1)
            return s["date"].replace("-", ""), end.strftime("%Y%m%d"), True
        start = datetime.fromisoformat(f"{s['rdv']['date']}T{s['rdv']['time']}").replace(tzinfo=TZ)
        end = start + CUSTOM_LENGTH
        return (start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                end.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), False)
    # étale : du RDV à une heure après l'étale
    tide = datetime.fromisoformat(f"{s['date']}T{s['time']}").replace(tzinfo=TZ) + AFTER_TIDE
    return _utc(s["rdv"]["date"], s["rdv"]["time"]), tide.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), False


def summary(s: dict) -> str:
    parts = [s["type"]["label"]]
    if s.get("note"):
        parts[0] += f" — {s['note']}"
    if s.get("port"):
        parts.append(s["port"])
    title = " · ".join(parts)
    if s.get("my_status") == "waiting":
        title += f" (file d'attente, rang {s['my_position']})"
    return title


def description(s: dict, link: str | None = None) -> str:
    lines = []
    water = s.get("water")
    if s.get("custom") and s.get("end_date"):
        lines.append(f"RDV {s['rdv']['time']} le premier jour")
    else:
        lines.append(f"RDV {s['rdv']['time']}")
    if water:
        sens = "au-dessus" if water["direction"] == "above" else "au-dessous"
        lines.append(f"Plage {water['start']} → {water['end']} : eau {sens} de {_fmt_m(water['height_m'])} "
                     f"({water['label']})")
    elif not s.get("custom"):
        tide = f"Étale {s['kind']} {s['time']}"
        extra = [x for x in (s.get("height_m") is not None and _fmt_m(s["height_m"]),
                             s.get("coefficient") is not None and f"coef {round(s['coefficient'])}") if x]
        lines.append(tide + (f" ({', '.join(extra)})" if extra else ""))
    places = (f"{s['confirmed_count']}/{s['max_registrations']} inscrits" if s.get("max_registrations")
              else f"{s['confirmed_count']} inscrit(s)")
    if s.get("waiting_count"):
        places += f", {s['waiting_count']} en file d'attente"
    lines.append(places)
    if s.get("my_status") == "confirmed":
        lines.append("Vous êtes inscrit.")
    elif s.get("my_status") == "waiting":
        lines.append(f"Vous êtes en file d'attente (rang {s['my_position']}).")
    if link:
        lines.append(link)
    return "\n".join(lines)


def calendar(selections: list[dict], name: str, uid_domain: str, link: str | None = None,
             now: datetime | None = None) -> str:
    """Fichier iCalendar complet (texte, fins de ligne CRLF)."""
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape(name)}", "X-WR-TIMEZONE:Europe/Paris",
        f"REFRESH-INTERVAL;VALUE=DURATION:{REFRESH}", f"X-PUBLISHED-TTL:{REFRESH}",
    ]
    for s in selections:
        start, end, all_day = _times(s)
        lines += [
            "BEGIN:VEVENT",
            f"UID:selection-{s['id']}@{uid_domain}",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{start}" if all_day else f"DTSTART:{start}",
            f"DTEND;VALUE=DATE:{end}" if all_day else f"DTEND:{end}",
            f"SUMMARY:{escape(summary(s))}",
            f"DESCRIPTION:{escape(description(s, link))}",
        ]
        if s.get("port"):
            lines.append(f"LOCATION:{escape(s['port'])}")
        if link:
            lines.append(f"URL:{link}")
        lines += ["TRANSP:OPAQUE", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(line) for line in lines) + "\r\n"
