"""Auth, admin and premium gates.

Every one of these has been wrong in this codebase before: is_admin() once
returned True when ADMIN_USER was unset, which made every logged-in beta
tester an admin on a fresh deploy.
"""

import pytest

import app as holo
from conftest import login, make_user

PAGES = ["/", "/cards", "/add", "/movers", "/profile", "/watchlist", "/sold",
         "/sets", "/insights", "/compare", "/import", "/market", "/scan", "/stats"]
API = ["/api/search", "/api/stats", "/api/dashboard"]


# ------------------------------------------------------------------ logged out

@pytest.mark.parametrize("path", PAGES)
def test_pages_redirect_to_login_when_logged_out(client, path):
    r = client.get(path)
    if path == "/":                       # landing page, not a redirect
        assert r.status_code == 200
        assert b"login" in r.data.lower()
        return
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


@pytest.mark.parametrize("path", API)
def test_api_returns_401_json_not_a_redirect(client, path):
    """A fetch() following a 302 to an HTML login page is a confusing failure."""
    r = client.get(path)
    assert r.status_code == 401
    assert r.get_json()["error"] == "login required"


def test_login_redirect_preserves_the_next_path(client):
    r = client.get("/watchlist")
    assert r.headers["Location"] == "/login?next=/watchlist"


def test_login_returns_you_to_the_page_you_asked_for(client):
    make_user("alice")
    r = client.post("/login?next=/watchlist",
                    data={"username": "alice", "password": "password123"})
    assert r.headers["Location"] == "/watchlist"


@pytest.mark.parametrize("hostile", [
    "https://evil.example/phish",
    "http://evil.example",
    "//evil.example/phish",              # protocol-relative
    "/\\evil.example",                 # browsers normalise \ to /
    "\\\\evil.example",
    "javascript:alert(1)",
    "/watchlist\r\nSet-Cookie: a=b",   # header splitting
])
def test_login_never_redirects_off_site(client, hostile):
    """An open redirect on a real login form is a complete phishing hop."""
    make_user("victim")
    r = client.post("/login", query_string={"next": hostile},
                    data={"username": "victim", "password": "password123"})
    assert r.status_code == 302
    assert r.headers["Location"] == "/", r.headers["Location"]
    assert "evil.example" not in r.headers["Location"]


def test_safe_next_accepts_only_site_relative_paths():
    assert holo.safe_next("/watchlist") == "/watchlist"
    assert holo.safe_next("/card/12?x=1") == "/card/12?x=1"
    for bad in (None, "", "watchlist", "//evil", "https://evil", "/a\\b", "/a\nb"):
        assert holo.safe_next(bad) is None, bad


def test_wrong_password_does_not_start_a_session(client):
    make_user("alice")
    r = client.post("/login", data={"username": "alice", "password": "wrong"})
    assert r.status_code == 200
    with client.session_transaction() as s:
        assert "uid" not in s


def test_login_does_not_say_which_half_was_wrong(client):
    """Distinct messages turn the form into a username oracle."""
    make_user("alice")
    a = client.post("/login", data={"username": "alice", "password": "wrong"}).data
    b = client.post("/login", data={"username": "nobody", "password": "wrong"}).data
    assert a == b


def test_passwords_are_hashed_not_stored(client):
    client.post("/signup", data={"username": "newbie", "password": "hunter2hunter2"})
    with holo.raw_db() as c:
        pw = c.execute("SELECT pw FROM users WHERE username='newbie'").fetchone()["pw"]
    assert "hunter2hunter2" not in pw
    assert pw.startswith(("pbkdf2:", "scrypt:", "argon2"))


@pytest.mark.parametrize("username,password,ok", [
    ("ab", "password123", False),                 # too short
    ("a" * 21, "password123", False),             # too long
    ("has space", "password123", False),
    ("bad!char", "password123", False),
    ("ok_user1", "short", False),                 # password under 8
    ("ok_user1", "password123", True),
])
def test_signup_validation(client, username, password, ok):
    r = client.post("/signup", data={"username": username, "password": password})
    with holo.raw_db() as c:
        exists = c.execute("SELECT 1 FROM users WHERE username=%s", (username,)).fetchone()
    assert bool(exists) is ok
    assert (r.status_code == 302) is ok


def test_signup_rejects_a_taken_username(client):
    make_user("alice")
    client.post("/signup", data={"username": "alice", "password": "password123"})
    with holo.raw_db() as c:
        assert c.execute("SELECT COUNT(*) n FROM users WHERE username='alice'").fetchone()["n"] == 1


def test_usernames_are_normalised_so_case_cannot_duplicate_an_account(client):
    make_user("alice")
    client.post("/signup", data={"username": "ALICE", "password": "password123"})
    with holo.raw_db() as c:
        assert c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"] == 1


def test_export_endpoints_require_login(client):
    for path in ("/export/cards.csv", "/export/history.csv", "/export/prices.csv"):
        assert client.get(path).status_code == 302


def test_write_endpoints_require_login(client):
    assert client.post("/api/holdings", json={"card_id": "x"}).status_code == 401
    assert client.post("/api/alerts", json={"card_id": "x"}).status_code == 401
    assert client.post("/api/watchlist", json={"card_id": "x"}).status_code == 401
    assert client.post("/api/sales", json={"card_id": "x"}).status_code == 401
    assert client.post("/api/settings", json={}).status_code == 401
    assert client.post("/api/premium/redeem", json={"code": "x"}).status_code == 401
    assert client.post("/api/scan", json={}).status_code == 401


def test_logout_clears_the_session(client):
    uid = make_user("alice")
    login(client, uid, "alice")
    client.get("/logout")
    with client.session_transaction() as s:
        assert "uid" not in s
    assert client.get("/cards").status_code == 302


# ----------------------------------------------------------------- admin gate

def test_stats_is_403_for_a_normal_user(client, monkeypatch):
    monkeypatch.setattr(holo, "ADMIN_USER", "oscar")
    login(client, make_user("mallory"), "mallory")
    assert client.get("/stats").status_code == 403


def test_stats_allows_the_named_admin(client, monkeypatch):
    monkeypatch.setattr(holo, "ADMIN_USER", "oscar")
    login(client, make_user("oscar"), "oscar")
    assert client.get("/stats").status_code == 200


def test_is_admin_fails_closed_when_admin_user_is_unset(client, monkeypatch):
    """The regression that made every beta tester an admin on a fresh deploy."""
    monkeypatch.setattr(holo, "ADMIN_USER", "")
    login(client, make_user("oscar"), "oscar")
    assert client.get("/stats").status_code == 403
    with holo.app.test_request_context():
        from flask import session
        session["uid"], session["username"] = 1, "oscar"
        assert holo.is_admin() is False


def test_is_admin_is_false_with_no_session(monkeypatch):
    monkeypatch.setattr(holo, "ADMIN_USER", "oscar")
    with holo.app.test_request_context():
        assert holo.is_admin() is False


def test_admin_status_comes_from_the_session_not_a_request_field(client, monkeypatch):
    """No header, arg or form field may promote a user to admin."""
    monkeypatch.setattr(holo, "ADMIN_USER", "oscar")
    login(client, make_user("mallory"), "mallory")
    assert client.get("/stats?username=oscar&admin=1",
                      headers={"X-Admin": "1", "X-Forwarded-User": "oscar"}).status_code == 403


def test_there_are_no_admin_write_endpoints():
    """Admin write routes were an account-takeover primitive and were removed.

    If one comes back it belongs behind its own login, not a flag on a normal
    account — so this fails loudly rather than letting one slip in quietly.
    """
    writes = {r.rule for r in holo.app.url_map.iter_rules()
              if r.methods & {"POST", "PATCH", "PUT", "DELETE"}}
    assert not [r for r in writes if "admin" in r or r.startswith("/stats")]


# --------------------------------------------------------------- premium gate

def test_scan_page_shows_the_locked_screen_to_a_free_user(client):
    login(client, make_user("free", premium=False), "free")
    r = client.get("/scan")
    assert r.status_code == 200            # a proper locked screen, not a 403
    assert b"Camera scan" in r.data


def test_scan_page_opens_for_a_premium_user(client):
    login(client, make_user("paid", premium=True), "paid")
    r = client.get("/scan")
    assert r.status_code == 200
    assert b"Camera scan" not in r.data or b"scan" in r.data.lower()


def test_is_premium_is_false_with_no_session():
    with holo.app.test_request_context():
        assert holo.is_premium() is False


def test_is_premium_reads_the_database_not_the_session(client):
    """A forged session value must not grant premium."""
    uid = make_user("free", premium=False)
    login(client, uid, "free")
    with client.session_transaction() as s:
        s["premium"] = True
    assert b"Camera scan" in client.get("/scan").data


def test_redeem_rejects_a_wrong_code(client, monkeypatch):
    monkeypatch.setattr(holo, "PREMIUM_CODE", "letmein")
    login(client, make_user("free"), "free")
    r = client.post("/api/premium/redeem", json={"code": "nope"})
    assert r.status_code == 400
    assert b"Camera scan" in client.get("/scan").data


def test_redeem_accepts_the_right_code_case_insensitively(client, monkeypatch):
    monkeypatch.setattr(holo, "PREMIUM_CODE", "LetMeIn")
    login(client, make_user("free"), "free")
    assert client.post("/api/premium/redeem", json={"code": " letmein "}).status_code == 200
    with holo.raw_db() as c:
        row = c.execute("SELECT premium, premium_since FROM users WHERE username='free'").fetchone()
    assert row["premium"] and row["premium_since"] is not None


def test_redeem_refuses_when_no_code_is_configured(client, monkeypatch):
    """An empty PREMIUM_CODE must not mean 'any code works', including ''."""
    monkeypatch.setattr(holo, "PREMIUM_CODE", "")
    login(client, make_user("free"), "free")
    for body in ({"code": ""}, {"code": "anything"}, {}):
        assert client.post("/api/premium/redeem", json=body).status_code == 400
    with holo.raw_db() as c:
        assert not c.execute("SELECT premium FROM users WHERE username='free'").fetchone()["premium"]


def test_redeem_only_upgrades_the_caller(client, monkeypatch):
    monkeypatch.setattr(holo, "PREMIUM_CODE", "letmein")
    make_user("bystander")
    login(client, make_user("free"), "free")
    client.post("/api/premium/redeem", json={"code": "letmein"})
    with holo.raw_db() as c:
        assert not c.execute(
            "SELECT premium FROM users WHERE username='bystander'").fetchone()["premium"]
