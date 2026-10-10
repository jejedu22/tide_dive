"""Jetons d'API (api_tokens.py) et documentation OpenAPI / Swagger (openapi_doc.py)."""

import pytest

from app import db, security
from conftest import login


@pytest.fixture()
def club(make_structure, make_user):
    sid = make_structure("Club Test")
    return {"sid": sid, "admin1": make_user("admin1", structure_id=sid, structure_role="manager"),
            "membre1": make_user("membre1", structure_id=sid, structure_role="viewer")}


def _token(client, username, scope="read", **extra):
    login(client, username)
    r = client.post("/api/me/api-tokens", json={"name": "Mon outil", "scope": scope, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_creation_liste_et_revocation(client, new_client, club):
    made = _token(client, "membre1")
    tool = new_client()                                             # l'outil : sans cookie de session
    assert tool.get("/api/selections", headers=_bearer(made["token"])).status_code == 200
    assert made["token"].startswith("cdv_") and made["token"].startswith(made["prefix"])
    assert made["scope"] == "read" and made["structure"] == "Club Test" and made["expires_at"]
    listed = client.get("/api/me/api-tokens").json()
    assert [t["id"] for t in listed["tokens"]] == [made["id"]]
    assert "token" not in listed["tokens"][0]                       # jamais réaffiché
    assert made["token"] not in str(tuple(db.list_api_tokens(club["membre1"])[0]))
    assert client.delete(f"/api/me/api-tokens/{made['id']}").status_code == 204
    assert tool.get("/api/selections", headers=_bearer(made["token"])).status_code == 401


def test_lecture_avec_un_jeton_sans_cookie(new_client, club):
    token = _token(new_client(), "membre1")["token"]
    tool = new_client()                                             # autre « outil » : aucun cookie
    r = tool.get("/api/auth/me", headers=_bearer(token))
    assert r.status_code == 200 and r.json()["user"]["username"] == "membre1"
    assert tool.get("/api/selections", headers=_bearer(token)).status_code == 200
    assert tool.get("/api/selections").status_code == 401
    r = tool.get("/api/selections", headers=_bearer("cdv_inconnu"))
    assert r.status_code == 401 and r.json()["detail"] == "Jeton d'API invalide ou expiré"


def test_portee_lecture_seule_et_ecriture(new_client, club):
    read = _token(new_client(), "admin1", "read")["token"]
    write = _token(new_client(), "admin1", "write")["token"]
    tool = new_client()
    body = {"label": "Par jeton", "color": "#118ab2"}
    r = tool.post("/api/admin/slot-types", json=body, headers=_bearer(read))
    assert r.status_code == 403 and r.json()["detail"] == "Jeton d'API en lecture seule"
    assert tool.post("/api/admin/slot-types", json=body, headers=_bearer(write)).status_code == 201
    # écrit au journal d'activité, au nom du compte, en précisant le jeton
    entry = db.list_audit(None, None, None, None, 5)[0]
    assert entry["actor_name"] == "Alice Test" and "jeton d'API" in entry["action"]


def test_droits_du_compte_seulement(new_client, club):
    token = _token(new_client(), "membre1", "write")["token"]
    r = new_client().post("/api/admin/slot-types", json={"label": "x"}, headers=_bearer(token))
    assert r.status_code == 403                                     # un membre n'administre pas


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/me/api-tokens"), ("POST", "/api/me/api-tokens"), ("GET", "/api/me/export"),
    ("GET", "/api/me/sessions"), ("GET", "/api/me/totp"), ("POST", "/api/me/password"),
    ("PATCH", "/api/me/profile"), ("PUT", "/api/me/notifications"),
])
def test_le_compte_lui_meme_reste_hors_d_atteinte(new_client, club, method, path):
    token = _token(new_client(), "membre1", "write")["token"]
    r = new_client().request(method, path, json={}, headers=_bearer(token))
    assert r.status_code == 403 and "jeton d'API" in r.json()["detail"]


def test_jeton_expire_ou_compte_suspendu(new_client, club):
    made = _token(new_client(), "membre1")
    tool = new_client()
    with db.get_conn() as conn:
        conn.execute("UPDATE api_tokens SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (made["id"],))
    assert tool.get("/api/selections", headers=_bearer(made["token"])).status_code == 401
    other = _token(new_client(), "membre1")
    db.suspend_user(club["membre1"], "2026-01-01T00:00:00+00:00", "test")
    assert tool.get("/api/selections", headers=_bearer(other["token"])).status_code == 401


def test_pas_de_jeton_pour_un_super_administrateur(client, make_user, monkeypatch):
    from app import auth
    monkeypatch.setattr(auth, "totp_setup_required", lambda u: False)
    make_user("root", is_admin=True)
    login(client, "root")
    assert client.post("/api/me/api-tokens", json={"name": "x"}).status_code == 403


def test_limite_par_minute(new_client, club, monkeypatch):
    from app import auth
    monkeypatch.setattr(security, "ENABLED", True)
    monkeypatch.setattr(auth, "API_TOKEN_RATE_PER_MIN", 3)
    token = _token(new_client(), "membre1")["token"]
    tool = new_client()
    codes = [tool.get("/api/auth/me", headers=_bearer(token)).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_nombre_de_jetons_limite(client, club, monkeypatch):
    from app import api_tokens
    monkeypatch.setattr(api_tokens, "MAX_TOKENS", 2)
    login(client, "membre1")
    for _ in range(2):
        assert client.post("/api/me/api-tokens", json={"name": "a"}).status_code == 201
    assert client.post("/api/me/api-tokens", json={"name": "a"}).status_code == 409


def test_derniere_utilisation_enregistree(new_client, club):
    made = _token(new_client(), "membre1")
    new_client().get("/api/auth/me", headers=_bearer(made["token"]))
    row = db.list_api_tokens(club["membre1"])[0]
    assert row["last_used_at"] is not None


# ---------------------------------------------------------------------------
# Documentation OpenAPI et page Swagger
# ---------------------------------------------------------------------------

def test_description_openapi(client):
    spec = client.get("/api/openapi.json").json()
    assert spec["info"]["title"] == "Calendive" and "Jeton d'API" in spec["info"]["description"]
    schemes = spec["components"]["securitySchemes"]
    assert schemes["jeton"]["scheme"] == "bearer" and schemes["session"]["in"] == "cookie"
    ops = [(m, p, op) for p, v in spec["paths"].items() for m, op in v.items()]
    assert all(op.get("tags") and op.get("summary") for _, _, op in ops)       # rangées et résumées en français
    names = {t["name"] for t in spec["tags"]}
    assert {"Créneaux et inscriptions", "Jetons d'API", "Administration de la structure"} <= names
    assert "Super administration" not in names
    sel = spec["paths"]["/api/selections"]["get"]
    assert {"jeton": []} in sel["security"] and sel["tags"] == ["Créneaux et inscriptions"]
    assert "security" not in spec["paths"]["/api/ports"]["get"]                # route publique


def test_page_swagger_hebergee(client):
    r = client.get("/api-docs.html")
    assert r.status_code == 200 and "vendor/swagger-ui/swagger-ui-bundle.js" in r.text
    assert "cdn" not in r.text.lower()
    assert client.get("/vendor/swagger-ui/swagger-ui-bundle.js").status_code == 200
    assert client.get("/docs", follow_redirects=False).headers["location"] == "/api-docs.html"


def test_routes_de_super_administration_absentes_de_la_documentation(client):
    """Réservées aux super administrateurs (pas de jeton possible) : servies, mais non documentées."""
    spec = client.get("/api/openapi.json").json()
    paths = set(spec["paths"])
    for hidden in ("/api/admin/ports", "/api/admin/jobs", "/api/admin/backups", "/api/admin/health",
                   "/api/admin/dashboard", "/api/admin/accounts/search", "/api/admin/maintenance"):
        assert not any(p == hidden or p.startswith(hidden + "/") for p in paths), hidden
    assert "/api/admin/users" in paths and "/api/admin/slot-types" in paths      # administrateurs de structure
    assert client.get("/api/admin/jobs").status_code == 401                        # la route existe toujours
