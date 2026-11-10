"""Find leads page: it shows the targeting Harvey actually uses, and a
request for businesses the free map can't search goes to web search."""

import shutil
import tempfile
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import harvey.brain as brain_mod
import harvey.config as config_mod
import harvey.dashboard as dash

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cfg = tmp / "harvey.local.yaml"
        shutil.copy(ROOT / "harvey.yaml", cfg)
        monkeypatch.setattr(config_mod, "_find_config_file", lambda: str(cfg))
        monkeypatch.setattr(dash, "DB_PATH", tmp / "harvey.db")
        monkeypatch.setattr(dash, "WEB_DIR", ROOT / "harvey" / "web")
        dash.token_store.configure(tmp / "data" / "dashboard-token")
        woken = []
        monkeypatch.setattr(dash, "wake_harvey", lambda: woken.append(True))
        monkeypatch.setattr(dash, "_discovery_task", None)
        token = dash.token_store.get()
        with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                        headers={"Authorization": f"Bearer {token}"}) as c:
            c.cfg, c.woken = cfg, woken
            yield c


def _spec(monkeypatch, industries):
    async def think_json(self, prompt, **kw):
        return {"industries": industries, "titles": ["Founder"], "summary": "Agencies."}
    monkeypatch.setattr(brain_mod.Brain, "think_json", think_json)


def test_page_shows_the_live_config_not_a_stale_request(client):
    data = yaml.safe_load(client.cfg.read_text(encoding="utf-8"))
    data["icp"]["industries"] = ["web development company"]
    data["icp"]["geography"] = ["Sydney, Australia", "Austin, USA"]
    data.setdefault("channels", {}).setdefault("email", {})["require_approval"] = True
    client.cfg.write_text(yaml.safe_dump(data), encoding="utf-8")

    cur = client.get("/api/leads/request").json()["current"]
    assert cur["industries"] == ["web development company"]
    assert cur["cities"] == ["Sydney, Australia", "Austin, USA"]
    assert cur["auto_send"] is False


def test_agencies_go_to_web_search_instead_of_failing_on_the_map(client, monkeypatch):
    _spec(monkeypatch, ["digital marketing agency", "web development company"])
    r = client.post("/api/leads/request", json={
        "text": "digital marketing and web development agencies",
        "locations": ["Toronto, Canada", "New York, USA"], "auto_send": False})
    body = r.json()
    assert r.status_code == 200 and body["success"]
    assert body["provider"] == "web_search" and body["queries"] == []
    assert client.woken  # Scout starts on it now, not in 15 minutes

    saved = yaml.safe_load(client.cfg.read_text(encoding="utf-8"))
    assert saved["icp"]["industries"] == ["digital marketing agency", "web development company"]
    assert saved["icp"]["geography"] == ["Toronto, Canada", "New York, USA"]
    assert saved["channels"]["email"]["require_approval"] is True

    page = client.get("/api/leads/request").json()
    assert page["request"]["provider"] == "web_search"
    assert page["current"]["cities"] == ["Toronto, Canada", "New York, USA"]
