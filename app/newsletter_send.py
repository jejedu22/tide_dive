"""
Envoi d'une newsletter par Mailjet, exécuté par le worker (tâche
« newsletter_send », voir jobs.py), jamais par l'API.

    python -m app.newsletter_send --id 12

1. Prise en main atomique : queued → sending (une tâche en double ne renvoie rien).
2. Destinataires figés au premier passage (newsletter_recipients), avec un
   jeton de désinscription personnel chacun.
3. Envoi par lots de 50 (Send API v3.1) des destinataires encore « queued » :
   chaque message porte CustomID = nl-<newsletter>-<destinataire> (le suivi
   le retrouve), un lien et un en-tête de désinscription, le suivi des
   ouvertures et des clics. Le résultat est enregistré après chaque lot.
4. Erreur de Mailjet sur un lot entier (clés refusées, panne) : la newsletter
   passe en « failed », les destinataires restants restent « queued » ; un
   gestionnaire peut reprendre l'envoi, sans renvoyer aux autres.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from . import db, mailer, mailjet, mailjet_admin, newsletter_render, newsletters

BATCH = mailjet.MAX_MESSAGES_PER_CALL


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fail(newsletter_id: int, message: str) -> int:
    db.update_newsletter(newsletter_id, status="failed", error=message, finished_at=_now())
    print(f"ERREUR : {message}", file=sys.stderr)
    return 1


def _message(nl, structure: str, cred, r) -> dict:
    token = r["unsubscribe_token"]
    subject, page, text = newsletter_render.render(
        nl["subject"], nl["preheader"], nl["body"], structure=structure,
        first_name=r["first_name"], last_name=r["last_name"],
        unsubscribe_url=mailer.link(f"desinscription.html?t={token}"),
    )
    one_click = mailer.link(f"api/newsletters/unsubscribe/{token}")
    to = {"Email": r["email"]}
    if r["name"]:
        to["Name"] = r["name"]
    return {
        "From": {"Email": cred.sender_email, "Name": cred.sender_name},
        "To": [to],
        "Subject": subject,
        "TextPart": text,
        "HTMLPart": page,
        "CustomID": f"nl-{nl['id']}-{r['id']}",
        "TrackOpens": "enabled",
        "TrackClicks": "enabled",
        "Headers": {
            "List-Unsubscribe": f"<{one_click}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    }


def prepare_recipients(nl) -> int:
    """Fige les destinataires (une seule fois) ; renvoie leur nombre."""
    members, skipped = newsletters.recipients_for(nl["structure_id"], json.loads(nl["audience_json"]))
    rows = []
    for m in members:
        name = " ".join(p for p in (m["first_name"], m["last_name"]) if p) or None
        rows.append((m["user_id"], m["email"].strip().lower(), name, newsletters.new_unsubscribe_token()))
    db.insert_recipients(nl["id"], rows)
    print(f"{len(rows)} destinataire(s) ; {skipped} désinscrit(s) écarté(s).")
    return len(rows)


def run(newsletter_id: int) -> int:
    if not db.update_newsletter(newsletter_id, only_status=("queued",), status="sending", started_at=_now()):
        print(f"Newsletter #{newsletter_id} : rien à envoyer (déjà envoyée, en cours ou annulée).")
        return 0
    nl = db.get_newsletter(newsletter_id)
    print(f"Newsletter #{newsletter_id} « {nl['subject']} »")
    if not mailer.BASE_URL:
        return _fail(newsletter_id, "APP_BASE_URL n'est pas renseignée : liens de désinscription impossibles.")
    try:
        cred = mailjet_admin.credentials(nl["structure_id"])
    except mailjet_admin.MailjetNotReady as exc:
        return _fail(newsletter_id, str(exc))
    structure = db.get_structure(nl["structure_id"])["name"]

    if not nl["total"]:
        prepare_recipients(nl)
    pending = db.queued_recipients(newsletter_id)
    sent = failed = 0
    for i in range(0, len(pending), BATCH):
        batch = pending[i:i + BATCH]
        messages = [_message(nl, structure, cred, r) for r in batch]
        try:
            results = mailjet.send(cred.api_key, cred.api_secret, messages)
        except mailjet.MailjetError as exc:
            db.update_newsletter(newsletter_id, status="failed", finished_at=_now(),
                                 error=f"{exc} ({sent} envoyé(s), {len(pending) - i} restant(s) : reprenez l'envoi)")
            print(f"ERREUR Mailjet : {exc}", file=sys.stderr)
            return 1
        marks = []
        for r, res in zip(batch, results):
            error = mailjet.message_error(res)
            if error:
                failed += 1
                marks.append((r["id"], "failed", None, error))
            else:
                to = (res.get("To") or [{}])[0]
                marks.append((r["id"], "sent", str(to.get("MessageID") or "") or None, None))
                sent += 1
        db.mark_recipients(marks, _now())
        print(f"Lot {i // BATCH + 1} : {sent} envoyé(s), {failed} refusé(s) au total.")

    final = "failed" if sent == 0 and failed else "sent"
    error = f"{failed} adresse(s) refusée(s) par Mailjet" if failed else None
    db.update_newsletter(newsletter_id, status=final, finished_at=_now(), error=error)
    print(f"Terminé : {sent} envoyé(s), {failed} refusé(s).")
    return 0 if final == "sent" else 1


def queue_due() -> list[int]:
    """Newsletters programmées dont l'heure est venue : scheduled → queued (planificateur)."""
    ids = []
    for nid in db.due_newsletters(_now()):
        if db.update_newsletter(nid, only_status=("scheduled",), status="queued"):
            ids.append(nid)
    return ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Envoie une newsletter par Mailjet.")
    parser.add_argument("--id", type=int, required=True)
    args = parser.parse_args(argv)
    db.init_db()
    return run(args.id)


if __name__ == "__main__":
    sys.exit(main())
