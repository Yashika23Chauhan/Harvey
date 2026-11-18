"""The Activity page's feed: Harvey's check-ins hidden unless asked for,
last-7-days totals, and lead approvals logged. Fictional data only."""

import json
import sqlite3
from contextlib import closing

from tests.test_dashboard_signals import client  # noqa: F401  (fixture)


def _seed(db_path):
    rows = [
        ("a1", "idle", "main", {}),
        ("a2", "prospect", "main", {}),
        ("a3", "prospect", "scout", {"prospects_found": 4}),
        ("a4", "email_sent", "sender", {"prospect_email": "ana@clinic.example", "step": 1}),
        ("a5", "reply_queued", "handler", {"prospect_email": "ana@clinic.example", "intent": "interested"}),
        ("a6", "bounce", "handler", {"prospect": "bo@roofs.example"}),
    ]
    with closing(sqlite3.connect(db_path)) as c, c:
        c.executemany("INSERT INTO actions (id, action_type, agent, details_json) VALUES (?, ?, ?, ?)",
                      [(i, t, a, json.dumps(d)) for i, t, a, d in rows])


def test_feed_hides_checkins_and_counts_the_week(client):  # noqa: F811
    client.get("/api/signals")                                    # creates the database
    _seed(client.db_path)
    d = client.get("/api/activity/feed").json()
    assert {a["agent"] for a in d["items"]} == {"scout", "sender", "handler"}
    assert all("details" in a and "details_json" not in a for a in d["items"])
    assert d["summary"]["leads_found"] == 4 and d["summary"]["emails_sent"] == 1
    assert d["summary"]["replies"] == 1 and d["summary"]["problems"] == 1
    assert d["summary"]["checkins_week"] == 2 and d["summary"]["last_checkin"]

    shown = client.get("/api/activity/feed?checkins=true").json()["items"]
    assert len(shown) == 6


def test_old_activity_endpoint_is_unchanged(client):  # noqa: F811
    client.get("/api/signals")
    _seed(client.db_path)
    rows = client.get("/api/activity").json()
    assert isinstance(rows, list) and len(rows) == 6               # Today still reads this


def test_lead_approvals_are_logged(client, monkeypatch):  # noqa: F811
    import harvey.dashboard as dash

    async def off():
        return None
    monkeypatch.setattr(dash, "_sitewatch_sync", off)
    client.get("/api/signals")
    with closing(sqlite3.connect(client.db_path)) as c, c:
        c.execute("INSERT INTO prospects (id, email, status, lead_approval) VALUES ('p1', 'ana@clinic.example', 'new', 'pending')")
    client.post("/api/leads/approval", json={"ids": ["p1"], "approval": "approved"})
    items = client.get("/api/activity/feed").json()["items"]
    assert [(a["action_type"], a["details"]) for a in items] == [("lead_approval", {"approval": "approved", "count": 1})]
