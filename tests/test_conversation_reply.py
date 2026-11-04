"""Replying to a customer from the Conversations tab.

Your own reply replaces Harvey's waiting draft, goes out already approved
(by you), threads under the email they answered, and still passes the send
gate. Opted-out contacts can't be replied to from here.
"""

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import harvey.config as harvey_config
import harvey.dashboard as dash
from harvey.agents.handler import Handler
from harvey.models.conversation import Conversation
from harvey.models.prospect import Prospect
from harvey.state import StateManager

ORIGIN = "http://127.0.0.1:5555"


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "harvey.db"
    monkeypatch.setattr(dash, "DB_PATH", db)
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    config = SimpleNamespace(compliance=SimpleNamespace(do_not_contact=["blocked.example"]),
                             channels=SimpleNamespace(email=SimpleNamespace(provider="smtp")))
    monkeypatch.setattr(harvey_config, "load_config", lambda *a, **k: config)

    async def seed(email="sam@shop.example", status="replied"):
        s = StateManager(str(db))
        await s.init_db()
        pid = await s.add_prospect(Prospect(first_name="Sam", last_name="Doe", title="Owner",
                                            email=email, email_status="verified", status=status))
        await s.add_outbox_item(prospect_id=pid, to_email=email, subject="booking after hours",
                                body="Hi Sam", send_at="2026-01-01T00:00:00", status="sent",
                                campaign_id="c1")
        with sqlite3.connect(db) as c:
            c.execute("UPDATE outbox SET message_id = '<cold@x>', thread_ref = '<cold@x>', "
                      "sent_at = '2026-01-01T00:00:00' WHERE kind = 'sequence'")
        cid = await s.add_conversation(Conversation(id="convo1", prospect_id=pid, campaign_id="c1"))
        await s.add_outbox_item(prospect_id=pid, conversation_id=cid, kind="reply", to_email=email,
                                subject="Re: booking after hours", body="Harvey's draft",
                                send_at="2026-01-02T00:00:00")
        return cid

    return {"db": db, "seed": lambda **kw: asyncio.run(seed(**kw))}


@pytest.fixture
def client(env):
    with TestClient(dash.app, base_url=ORIGIN,
                    headers={"Authorization": f"Bearer {dash.token_store.get()}"}) as c:
        yield c


def replies(db):
    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(
            "SELECT * FROM outbox WHERE kind = 'reply' ORDER BY created_at, rowid")]


def test_reply_is_queued_approved_and_replaces_the_draft(client, env):
    cid = env["seed"]()
    r = client.post(f"/api/conversations/{cid}/reply", json={"body": "Tuesday at 10 works."})
    assert r.status_code == 200 and r.json()["success"]
    draft, mine = replies(env["db"])
    assert draft["status"] == "rejected"
    assert mine["status"] == "approved" and mine["approved_by"] == "you"
    assert mine["body"] == "Tuesday at 10 works."
    assert mine["subject"] == "Re: booking after hours"


def test_first_reply_threads_under_the_cold_email(client, env):
    cid = env["seed"]()
    with sqlite3.connect(env["db"]) as c:
        c.execute("DELETE FROM outbox WHERE kind = 'reply'")
    client.post(f"/api/conversations/{cid}/reply", json={"body": "Thanks!"})
    mine = replies(env["db"])[0]
    assert mine["subject"] == "Re: booking after hours"
    assert mine["in_reply_to"] == "<cold@x>" and mine["thread_ref"] == "<cold@x>"


def test_empty_reply_is_refused(client, env):
    cid = env["seed"]()
    assert client.post(f"/api/conversations/{cid}/reply", json={"body": "  "}).status_code == 400


def test_opted_out_contact_cannot_be_replied_to(client, env):
    cid = env["seed"](status="opted_out")
    r = client.post(f"/api/conversations/{cid}/reply", json={"body": "Hi again"})
    assert r.status_code == 409
    assert all(x["status"] != "approved" for x in replies(env["db"]))


def test_do_not_contact_still_blocks_a_manual_reply(client, env):
    cid = env["seed"](email="jo@blocked.example")
    r = client.post(f"/api/conversations/{cid}/reply", json={"body": "Hi"})
    assert r.status_code == 422 and "do-not-contact" in r.json()["message"]


def test_unknown_conversation(client, env):
    env["seed"]()
    assert client.post("/api/conversations/nope/reply", json={"body": "Hi"}).status_code == 404


def test_reply_needs_the_token(env):
    cid = env["seed"]()
    with TestClient(dash.app, base_url=ORIGIN) as c:
        assert c.post(f"/api/conversations/{cid}/reply", json={"body": "Hi"}).status_code == 401


def test_your_reply_shows_in_the_thread_at_once(client, env, monkeypatch):
    woken = []
    monkeypatch.setattr(dash, "wake_harvey", lambda: woken.append(1))
    cid = env["seed"]()
    client.post(f"/api/conversations/{cid}/reply", json={"body": "On my way."})
    convo = next(c for c in client.get("/api/conversations").json() if c["id"] == cid)
    assert convo["outgoing"] == [{"sender": "you", "content": "On my way.",
                                  "timestamp": convo["outgoing"][0]["timestamp"]}]
    assert woken, "the heartbeat must be woken to send it now"


def test_heartbeat_wakes_when_asked(tmp_path, monkeypatch):
    import time

    import harvey.main as main_mod
    import harvey.paths as paths
    wake = tmp_path / "harvey.wake"
    monkeypatch.setattr(paths, "WAKE_FILE", wake)

    async def go():
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.2, wake.touch)
        t = time.monotonic()
        stopped = await main_mod._heartbeat_sleep(60, stop)
        return stopped, time.monotonic() - t

    stopped, took = asyncio.run(go())
    assert stopped is False and took < 3 and not wake.exists()


@pytest.mark.parametrize("intent", ["interested", "question", "objection"])
def test_lost_conversation_reopens_at_engaged(intent):
    h = Handler.__new__(Handler)
    assert h._determine_stage(intent, "closed_lost", "tell me more") == "engaged"


def test_lost_stays_lost_on_another_no():
    h = Handler.__new__(Handler)
    assert h._determine_stage("not_interested", "closed_lost", "no") == "closed_lost"
