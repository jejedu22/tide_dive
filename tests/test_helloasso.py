"""HelloAsso : réglages, paiement des sorties (facultatif, prix par type, gratuit pour les encadrants), cotisation
(saison, blocage), notifications, rien d'affiché sans réglages. Aucun appel réseau : helloasso est remplacé."""

from datetime import date

import pytest
from cryptography.fernet import Fernet

from app import db, helloasso, mailer, payments, payments_core
from tests.conftest import login

DAY = "2099-07-01"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("SECRETS_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(mailer, "BASE_URL", "https://exemple.org")


@pytest.fixture()
def fake(monkeypatch):
    """HelloAsso simulé : une commande n'existe que si `paid` contient l'identifiant de la demande."""
    state = {"created": [], "paid": set(), "refunded": set(), "fail_create": False, "org": {"name": "Club Alpha"}}

    def create(sandbox, cid, secret, slug, **kw):
        if state["fail_create"]:
            raise helloasso.HelloAssoError("HelloAsso : l'association n'est pas autorisée (HTTP 409).", 409)
        intent = str(1000 + len(state["created"]))
        state["created"].append({"intent": intent, "slug": slug, "sandbox": sandbox, **kw})
        return intent, f"https://checkout.helloasso.test/{intent}"

    def get(sandbox, cid, secret, slug, intent):
        if intent in state["refunded"]:
            return {"id": intent, "order": {"id": 7, "payments": [{"state": "Refunded"}]}}
        if intent in state["paid"]:
            return {"id": intent, "order": {"id": 7, "payments": [{"state": "Authorized", "paymentReceiptUrl": "https://r/1"}]}}
        return {"id": intent}

    monkeypatch.setattr(helloasso, "create_checkout", create)
    monkeypatch.setattr(helloasso, "get_checkout", get)
    monkeypatch.setattr(helloasso, "organization", lambda *a, **k: state["org"])
    return state


@pytest.fixture()
def club(make_structure, make_user):
    sid = make_structure("Club Alpha")
    make_user("alice", structure_id=sid, structure_role="manager")
    make_user("m1", structure_id=sid, structure_role="viewer")
    enc = make_user("enc", structure_id=sid, structure_role="viewer")
    db.update_user(enc, instructor_level="E2")
    other = make_structure("Club B")
    make_user("eve", structure_id=other, structure_role="manager")
    typ = db.create_slot_type(sid, "Exploration", "#118ab2", True)
    return {"sid": sid, "type": typ, "other": other}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


SETTINGS = {"org_slug": "club-alpha", "client_id": "client-id-1234", "client_secret": "secret-abcdef-1",
            "use_slots": True, "use_membership": True, "membership_amount_eur": 30,
            "membership_season_start": "09-01", "membership_required": False}


def _setup(alice, club, **over):
    r = alice.put("/api/admin/helloasso", json={**SETTINGS, **over})
    assert r.status_code == 200, r.text
    alice.patch(f"/api/admin/slot-types/{club['type']}", json={"price_cents": 2500})
    return r.json()


def _slot(c, club):
    r = c.post("/api/selections/custom", json={"location": "Carrière", "date": DAY, "time": "09:00", "type_id": club["type"]})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# Rien d'affiché sans réglages ; réglages
# ---------------------------------------------------------------------------

def test_rien_n_est_affiche_sans_reglages(club, new_client):
    m1 = _client(new_client, "m1")
    assert m1.get("/api/auth/me").json()["user"]["payments"] is None
    alice = _client(new_client, "alice")
    sid = _slot(alice, club)
    assert m1.post(f"/api/selections/{sid}/pay").status_code == 404
    assert m1.post("/api/me/membership/pay").status_code == 404
    # réglages sans aucun usage activé : toujours rien
    alice.put("/api/admin/helloasso", json={**SETTINGS, "use_slots": False, "use_membership": False})
    assert _client(new_client, "m1").get("/api/auth/me").json()["user"]["payments"] is None


def test_reglages_secrets_et_validation(club, new_client, fake):
    alice, m1, eve = (_client(new_client, u) for u in ("alice", "m1", "eve"))
    assert m1.get("/api/admin/helloasso").status_code == 403
    out = _setup(alice, club)
    assert out["configured"] and out["client_id_hint"] == "1234" and out["use_slots"] and out["membership_amount_cents"] == 3000
    assert "client_secret" not in str(out) and "secret-abcdef" not in str(out)
    assert out["notify_url"].startswith("https://exemple.org/api/helloasso/notifications/")
    stored = db.get_helloasso(club["sid"])
    assert "secret-abcdef" not in stored["client_secret_enc"]                        # chiffré en base
    assert eve.get("/api/admin/helloasso").json()["configured"] is False             # propre à chaque structure
    for bad in ({"client_secret": None}, {"org_slug": "Pas valide!"}, {"membership_season_start": "02-30"},
                {"membership_amount_eur": None}, {"use_membership": False, "membership_required": True}):
        assert alice.put("/api/admin/helloasso", json={**SETTINGS, **bad}).status_code == 422, bad
    # clés gardées si on ne les ressaisit pas
    kept = alice.put("/api/admin/helloasso", json={"org_slug": "club-alpha", "use_slots": True}).json()
    assert kept["client_id_hint"] == "1234" and kept["use_membership"] is False
    assert alice.post("/api/admin/helloasso/test").json()["check_message"].startswith("Connecté à HelloAsso")
    assert alice.delete("/api/admin/helloasso").status_code == 204
    assert alice.get("/api/admin/helloasso").json()["configured"] is False


def test_https_exige(club, new_client, monkeypatch):
    monkeypatch.setattr(mailer, "BASE_URL", "http://exemple.org")
    assert _client(new_client, "alice").put("/api/admin/helloasso", json=SETTINGS).status_code == 422


# ---------------------------------------------------------------------------
# Sorties
# ---------------------------------------------------------------------------

def test_prix_par_type_et_gratuit_pour_les_encadrants(club, new_client, fake):
    alice, m1, enc = (_client(new_client, u) for u in ("alice", "m1", "enc"))
    _setup(alice, club)
    cfg = m1.get("/api/auth/me").json()["user"]["payments"]
    assert cfg["slots"] is True and cfg["membership"]["amount_cents"] == 3000 and cfg["membership"]["valid"] is False
    sid = _slot(alice, club)
    listed = next(s for s in m1.get("/api/selections").json() if s["id"] == sid)
    assert listed["price"] == {"cents": 2500, "free_for_instructors": False}
    for c in (m1, enc):
        assert c.post(f"/api/selections/{sid}/registration").status_code == 200
    r = m1.post(f"/api/selections/{sid}/pay")
    assert r.status_code == 200, r.text
    created = fake["created"][-1]
    assert created["amount_cents"] == 2500 and created["slug"] == "club-alpha" and created["return_url"].startswith("https://")
    assert created["payer"]["email"] == "m1@example.org" and created["metadata"]["calendive_payment"] == r.json()["payment_id"]
    assert r.json()["redirect_url"].startswith("https://checkout.helloasso.test/")
    # encadrant : gratuit quand le type le prévoit
    alice.patch(f"/api/admin/slot-types/{club['type']}", json={"free_for_instructors": True})
    assert enc.post(f"/api/selections/{sid}/pay").status_code == 422
    assert m1.post(f"/api/selections/{sid}/pay").status_code == 200                  # les autres paient
    alice.patch(f"/api/admin/slot-types/{club['type']}", json={"price_cents": None})
    assert m1.post(f"/api/selections/{sid}/pay").status_code == 422                  # type gratuit


def test_payer_est_facultatif_et_suit_l_inscription(club, new_client, fake):
    alice, m1, m2 = _client(new_client, "alice"), _client(new_client, "m1"), _client(new_client, "enc")
    _setup(alice, club)
    sid = _slot(alice, club)
    assert m1.post(f"/api/selections/{sid}/pay").status_code == 409                  # pas inscrit
    assert m1.post(f"/api/selections/{sid}/registration").status_code == 200         # inscription sans payer
    alice.patch(f"/api/selections/{sid}", json={"max_registrations": 1})
    m2.post(f"/api/selections/{sid}/registration")
    assert m2.post(f"/api/selections/{sid}/pay").status_code == 409                  # file d'attente
    fake["fail_create"] = True
    assert m1.post(f"/api/selections/{sid}/pay").status_code == 502                  # refus de HelloAsso : message clair
    assert db.list_payments(club["sid"])[0]["status"] == "failed"


def test_confirmation_par_relecture_chez_helloasso(club, new_client, fake):
    alice, m1 = _client(new_client, "alice"), _client(new_client, "m1")
    _setup(alice, club)
    sid = _slot(alice, club)
    m1.post(f"/api/selections/{sid}/registration")
    pid = m1.post(f"/api/selections/{sid}/pay").json()["payment_id"]
    assert m1.get(f"/api/me/payments/{pid}").json()["status"] == "pending"          # pas encore payé
    assert alice.get(f"/api/me/payments/{pid}").status_code == 404                    # le paiement d'un autre
    intent = db.get_payment(pid)["checkout_intent_id"]
    fake["paid"].add(intent)
    paid = m1.get(f"/api/me/payments/{pid}").json()
    assert paid["status"] == "paid" and paid["receipt_url"] == "https://r/1"
    mine = next(s for s in m1.get("/api/selections").json() if s["id"] == sid)
    assert mine["my_pay_status"] == "paid"
    assert m1.post(f"/api/selections/{sid}/pay").status_code == 409                  # déjà payé
    # chacun ne voit que son paiement ; l'administration voit tout
    enc = _client(new_client, "enc")
    enc.post(f"/api/selections/{sid}/registration")
    seen = next(s for s in enc.get("/api/selections").json() if s["id"] == sid)
    assert {r["username"]: r["pay_status"] for r in seen["registrations"]}["m1"] is None
    admin_view = next(s for s in alice.get("/api/selections").json() if s["id"] == sid)
    assert {r["username"]: r["pay_status"] for r in admin_view["registrations"]}["m1"] == "paid"


def test_abandon_apres_45_minutes(club, new_client, fake):
    alice, m1 = _client(new_client, "alice"), _client(new_client, "m1")
    _setup(alice, club)
    sid = _slot(alice, club)
    m1.post(f"/api/selections/{sid}/registration")
    pid = m1.post(f"/api/selections/{sid}/pay").json()["payment_id"]
    with db.get_conn() as conn:
        conn.execute("UPDATE payments SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (pid,))
    assert m1.get(f"/api/me/payments/{pid}").json()["status"] == "failed"


def test_remboursements_a_faire_apres_annulation(club, new_client, fake):
    alice, m1 = _client(new_client, "alice"), _client(new_client, "m1")
    _setup(alice, club)
    sid = _slot(alice, club)
    m1.post(f"/api/selections/{sid}/registration")
    pid = m1.post(f"/api/selections/{sid}/pay").json()["payment_id"]
    fake["paid"].add(db.get_payment(pid)["checkout_intent_id"])
    m1.get(f"/api/me/payments/{pid}")
    listing = alice.get("/api/admin/payments").json()
    assert listing["summary"]["paid_cents"] == 2500 and listing["summary"]["refund_due"] == 0
    reason = next(r for r in alice.get("/api/cancellation-reasons").json())
    alice.post(f"/api/selections/{sid}/cancel", json={"reason_id": reason["id"]})
    row = alice.get("/api/admin/payments").json()
    assert row["summary"]["refund_due"] == 1 and row["payments"][0]["refund_reason"] == "sortie annulée"
    assert alice.post(f"/api/admin/payments/{pid}/refunded").json()["status"] == "refunded"
    assert alice.get("/api/admin/payments").json()["summary"]["refund_due"] == 0
    assert alice.post(f"/api/admin/payments/{pid}/refunded").status_code == 409


# ---------------------------------------------------------------------------
# Cotisation
# ---------------------------------------------------------------------------

def test_saison_de_la_cotisation():
    assert payments_core.season_bounds("09-01", date(2026, 10, 12)) == (date(2026, 9, 1), date(2027, 8, 31))
    assert payments_core.season_bounds("09-01", date(2026, 8, 31)) == (date(2025, 9, 1), date(2026, 8, 31))
    assert payments_core.season_bounds("01-01", date(2026, 3, 1)) == (date(2026, 1, 1), date(2026, 12, 31))
    assert payments_core.parse_season_start("02-30") == (9, 1) and payments_core.parse_season_start(None) == (9, 1)


def test_cotisation_en_ligne_puis_valide_pour_la_saison(club, new_client, fake):
    alice, m1 = _client(new_client, "alice"), _client(new_client, "m1")
    _setup(alice, club)
    r = m1.post("/api/me/membership/pay")
    assert r.status_code == 200 and fake["created"][-1]["amount_cents"] == 3000
    assert fake["created"][-1]["item_name"].startswith("Cotisation")
    fake["paid"].add(db.get_payment(r.json()["payment_id"])["checkout_intent_id"])
    assert m1.get(f"/api/me/payments/{r.json()['payment_id']}").json()["status"] == "paid"
    membership = m1.get("/api/auth/me").json()["user"]["payments"]["membership"]
    assert membership["valid"] is True and membership["valid_until"] >= date.today().isoformat()
    assert m1.post("/api/me/membership/pay").status_code == 409                       # déjà à jour


def test_cotisation_exigee_bloque_l_inscription(club, new_client, fake):
    alice, m1 = _client(new_client, "alice"), _client(new_client, "m1")
    _setup(alice, club, membership_required=True)
    sid = _slot(alice, club)
    r = m1.post(f"/api/selections/{sid}/registration")
    assert r.status_code == 409 and "cotisation" in r.json()["detail"]
    # inscrire un membre sans cotisation : refusé aussi, avec son nom
    members = alice.get("/api/selections/members").json()
    uid = next(m["id"] for m in members if m["username"] == "m1")
    r = alice.post(f"/api/selections/{sid}/registrations", json={"user_ids": [uid]})
    assert r.status_code == 409 and "cotisation" in r.json()["detail"]
    # cotisation enregistrée à la main par l'administration : l'inscription passe
    manual = alice.post(f"/api/admin/helloasso/members/{uid}/membership")
    assert manual.status_code == 201 and manual.json()["method"] == "manual"
    assert alice.post(f"/api/admin/helloasso/members/{uid}/membership").status_code == 409
    assert m1.post(f"/api/selections/{sid}/registration").status_code == 200
    states = {m["id"]: m["valid"] for m in alice.get("/api/admin/helloasso/members").json()}
    assert states[uid] is True
    # exigée mais pas bloquante par défaut : désactivée, tout le monde peut s'inscrire
    alice.put("/api/admin/helloasso", json={"org_slug": "club-alpha", "use_membership": True, "membership_amount_eur": 30})
    other = _client(new_client, "enc")
    assert other.post(f"/api/selections/{sid}/registration").status_code == 200
    # une cotisation manuelle peut être supprimée, pas un paiement HelloAsso
    pid = manual.json()["id"]
    assert alice.delete(f"/api/admin/payments/{pid}").status_code == 204


# ---------------------------------------------------------------------------
# Notifications (webhook)
# ---------------------------------------------------------------------------

def test_notification_declenche_la_relecture(club, new_client, fake, monkeypatch):
    alice, m1 = _client(new_client, "alice"), _client(new_client, "m1")
    out = _setup(alice, club)
    token = out["notify_url"].rsplit("/", 1)[1]
    pid = m1.post("/api/me/membership/pay").json()["payment_id"]
    fake["paid"].add(db.get_payment(pid)["checkout_intent_id"])
    body = {"eventType": "Payment", "data": {}, "metadata": {"calendive_payment": pid}}
    anon = new_client()
    assert anon.post(f"/api/helloasso/notifications/{token}", json=body).status_code == 403      # adresse d'envoi inconnue
    assert anon.post("/api/helloasso/notifications/mauvais", json=body).status_code == 404
    monkeypatch.setattr(payments, "IP_CHECK", False)
    assert anon.post(f"/api/helloasso/notifications/{token}", json=body).status_code == 200
    assert db.get_payment(pid)["status"] == "paid"
    # un paiement d'une autre structure n'est pas touché
    other_sid = club["other"]
    foreign = db.create_payment(other_sid, 1, "membership", "x", 100, "2026-01-01T00:00:00+00:00")
    assert anon.post(f"/api/helloasso/notifications/{token}",
                     json={"metadata": {"calendive_payment": foreign}}).status_code == 200
    assert db.get_payment(foreign)["status"] == "pending"
    assert anon.post(f"/api/helloasso/notifications/{token}", content=b"pas du json").status_code == 200


def test_issue_du_paiement():
    assert helloasso.outcome({"id": 1}) == ("pending", None, None)
    assert helloasso.outcome({"order": {"id": 5, "payments": [{"state": "Authorized", "paymentReceiptUrl": "u"}]}}) == ("paid", "5", "u")
    assert helloasso.outcome({"order": {"id": 5}})[0] == "paid"
    assert helloasso.outcome({"order": {"id": 5, "payments": [{"state": "Refunded"}]}})[0] == "refunded"
    assert helloasso.outcome({"order": {"id": 5, "payments": [{"state": "Refused"}]}})[0] == "failed"


# ---------------------------------------------------------------------------
# Client HTTP (réseau simulé au niveau de _http)
# ---------------------------------------------------------------------------

def test_client_jeton_gardé_et_corps_du_paiement(monkeypatch):
    calls = []

    def fake_http(method, url, *, data=None, headers=None):
        calls.append((method, url, data, headers or {}))
        if url.endswith("/oauth2/token"):
            return {"access_token": "tok1", "refresh_token": "ref1", "expires_in": "1800"}
        return {"id": 5, "redirectUrl": "https://pay.helloasso.test/5"}

    monkeypatch.setattr(helloasso, "_http", fake_http)
    helloasso._tokens.clear()
    kw = dict(amount_cents=2500, item_name="Sortie", back_url="https://x/b", error_url="https://x/e",
              return_url="https://x/r", payer={"firstName": "Vic", "lastName": None, "email": "v@x.fr"}, metadata={"calendive_payment": 3})
    assert helloasso.create_checkout(True, "cid", "sec", "club-alpha", **kw) == ("5", "https://pay.helloasso.test/5")
    helloasso.create_checkout(True, "cid", "sec", "club-alpha", **kw)
    auth = [c for c in calls if c[1].endswith("/oauth2/token")]
    assert len(auth) == 1 and b"grant_type=client_credentials" in auth[0][2]            # un seul jeton pour deux paiements
    assert auth[0][1].startswith("https://api.helloasso-sandbox.com/")                  # bac à sable
    post = [c for c in calls if "checkout-intents" in c[1]][0]
    import json as _json
    body = _json.loads(post[2])
    assert post[3]["Authorization"] == "Bearer tok1" and post[1].endswith("/v5/organizations/club-alpha/checkout-intents")
    assert body["totalAmount"] == body["initialAmount"] == 2500 and body["containsDonation"] is False
    assert body["payer"] == {"firstName": "Vic", "email": "v@x.fr"} and body["metadata"] == {"calendive_payment": 3}


def test_client_nouveau_jeton_apres_401(monkeypatch):
    tokens = iter(["old", "new"])
    seen = []

    def fake_http(method, url, *, data=None, headers=None):
        if url.endswith("/oauth2/token"):
            return {"access_token": next(tokens), "expires_in": 1800}
        seen.append(headers["Authorization"])
        if headers["Authorization"] == "Bearer old":
            raise helloasso.HelloAssoError("expiré", 401)
        return {"ok": True}

    monkeypatch.setattr(helloasso, "_http", fake_http)
    helloasso._tokens.clear()
    assert helloasso.request(False, "cid2", "sec", "GET", "/organizations/x") == {"ok": True}
    assert seen == ["Bearer old", "Bearer new"]
