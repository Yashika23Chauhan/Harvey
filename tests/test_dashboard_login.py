"""The dashboard's sign-in page: accounts that hand out the dashboard token.

Sign-up and sign-in are the only API routes that answer without the token.
They must still refuse other websites and non-loopback hosts, never store a
password as text, and only hand the token to a correct password.
"""

import json

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey import dashboard_auth as auth

ORIGIN = "http://127.0.0.1:5555"
CROSS_SITE = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    users = tmp_path / "data" / "dashboard-users.json"
    monkeypatch.setattr(dash, "user_store", auth.UserStore(users))
    monkeypatch.setattr(auth, "_PBKDF2_ROUNDS", 1000)  # fast tests
    monkeypatch.setattr(dash.asyncio, "sleep", _no_sleep)
    monkeypatch.delenv("HARVEY_DASHBOARD_SIGNUP", raising=False)
    monkeypatch.delenv("HARVEY_DASHBOARD_NO_TOKEN", raising=False)
    return {"users": users, "token": dash.token_store.get()}


async def _no_sleep(_):
    return None


@pytest.fixture
def client(env):
    with TestClient(dash.app, base_url=ORIGIN) as c:
        yield c


def signup(client, username="demo", password="correct horse"):
    return client.post("/auth/signup", json={"username": username, "password": password})


def test_status_says_when_no_account_exists(client):
    assert client.get("/auth/status").json() == {"has_users": False, "signup_open": True}
    signup(client)
    assert client.get("/auth/status").json() == {"has_users": True, "signup_open": False}


def test_signup_returns_the_dashboard_token(client, env):
    r = signup(client)
    assert r.status_code == 200
    assert r.json()["token"] == env["token"]


def test_password_is_stored_hashed_not_as_text(client, env):
    signup(client, password="correct horse")
    stored = env["users"].read_text(encoding="utf-8")
    assert "correct horse" not in stored
    user = json.loads(stored)["users"][0]
    assert user["username"] == "demo" and len(user["hash"]) == 64 and user["salt"]


def test_login_with_the_right_password_returns_the_token(client, env):
    signup(client)
    r = client.post("/auth/login", json={"username": "DEMO", "password": "correct horse"})
    assert r.status_code == 200 and r.json()["token"] == env["token"]


@pytest.mark.parametrize("username, password", [
    ("demo", "wrong password"), ("nobody", "correct horse"), ("demo", ""),
])
def test_wrong_credentials_get_no_token(client, username, password):
    signup(client)
    r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 401
    assert "token" not in r.json()


def test_the_token_from_login_unlocks_the_api(client):
    assert client.get("/api/stats").status_code == 401
    token = signup(client).json()["token"]
    assert client.get("/api/stats", headers={"Authorization": f"Bearer {token}"}).status_code == 200


@pytest.mark.parametrize("username, password, message", [
    ("ab", "correct horse", "Username"),
    ("has space", "correct horse", "Username"),
    ("demo", "short", "at least 8"),
])
def test_signup_rejects_bad_input(client, username, password, message):
    r = signup(client, username, password)
    assert r.status_code == 400 and message in r.json()["message"]


@pytest.mark.parametrize("username", ["Demo", "someone-else"])
def test_only_one_account_can_be_created(client, env, username):
    signup(client)
    r = signup(client, username)
    assert r.status_code == 400 and "token" not in r.json()
    assert "already has an account" in r.json()["message"]
    assert len(json.loads(env["users"].read_text(encoding="utf-8"))["users"]) == 1


def test_signup_can_be_closed(client, monkeypatch):
    monkeypatch.setenv("HARVEY_DASHBOARD_SIGNUP", "closed")
    r = signup(client)
    assert r.status_code == 403 and "token" not in r.json()
    assert client.get("/auth/status").json()["signup_open"] is False


@pytest.mark.parametrize("path", ["/auth/login", "/auth/signup"])
def test_other_websites_cannot_sign_in(client, path):
    signup(client)
    r = client.post(path, json={"username": "demo", "password": "correct horse"},
                    headers=CROSS_SITE)
    assert r.status_code == 403


@pytest.mark.parametrize("path", ["/auth/login", "/auth/signup"])
def test_form_posts_are_refused(client, path):
    r = client.post(path, content="username=demo&password=correct+horse",
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415


def test_non_loopback_host_is_refused(env):
    with TestClient(dash.app, base_url="http://rebind.example:5555") as c:
        assert c.post("/auth/login", json={"username": "a", "password": "b"}).status_code == 400
