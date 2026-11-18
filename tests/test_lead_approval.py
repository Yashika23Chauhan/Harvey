"""Lead approval: leads Scout finds wait as pending, Harvey only writes to
approved ones, and the dashboard's Leads page lists and approves them.
Fictional data only."""

import os
import sqlite3
import tempfile
from contextlib import closing
from types import SimpleNamespace

import pytest
import pytest_asyncio

from harvey.agents.scout import Scout
from harvey.agents.writer import Writer
from harvey.config import HarveyConfig, LeadsConfig
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.state import StateManager
from tests.test_dashboard_signals import client  # noqa: F401  (fixture)


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "a.db"))
        await sm.init_db()
        yield sm


async def _lead(state, n, approval="approved", industry="dental clinic", status="new"):
    cid = await state.add_company(Company(name=f"Clinic {n}", domain=f"clinic{n}.example", industry=industry))
    return await state.add_prospect(Prospect(
        first_name="Ana", last_name="Test", company=f"Clinic {n}", company_id=cid,
        email=f"ana@clinic{n}.example", email_status="verified", status=status, lead_approval=approval))


def test_approval_is_on_by_default():
    assert LeadsConfig().require_approval is True
    assert "leads" in HarveyConfig.model_fields


@pytest.mark.asyncio
async def test_scout_saves_new_leads_as_pending_only_when_approval_is_on(state):
    scout = Scout.__new__(Scout)
    scout.state = state
    scout.config = SimpleNamespace(leads=LeadsConfig(require_approval=True))
    pid = await scout._add_lead(Prospect(first_name="Ana", last_name="Test", email="ana@one.example"))
    assert (await state.get_prospect(pid)).lead_approval == "pending"

    scout.config = SimpleNamespace(leads=LeadsConfig(require_approval=False))
    pid = await scout._add_lead(Prospect(first_name="Bo", last_name="Test", email="bo@two.example"))
    assert (await state.get_prospect(pid)).lead_approval == "approved"


@pytest.mark.asyncio
async def test_only_approved_leads_count_as_ready_to_write(state):
    await _lead(state, 1, "pending")
    await _lead(state, 2, "rejected")
    assert await state.count_writable_prospects() == 0
    await _lead(state, 3, "approved")
    assert await state.count_writable_prospects() == 1


@pytest.mark.asyncio
async def test_writer_skips_pending_and_rejected_leads(state, caplog):
    await _lead(state, 1, "pending")
    await _lead(state, 2, "rejected")
    brain = SimpleNamespace(load_skills_for_agent=lambda name: "")
    cfg = SimpleNamespace(writer=SimpleNamespace(email_rules=""),
                          channels=SimpleNamespace(email=SimpleNamespace(provider="gmail")))
    w = Writer(brain, state, cfg)
    with caplog.at_level("INFO", logger="harvey.writer"):
        await w.run()                       # returns before any model call
    assert "No approved leads" in caplog.text
    with closing(sqlite3.connect(state.db_path)) as c:
        assert c.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_set_lead_approval(state):
    a = await _lead(state, 1, "pending")
    b = await _lead(state, 2, "pending")
    assert await state.set_lead_approval([a, b], "approved") == 2
    assert await state.set_lead_approval([a], "approved") == 0      # already approved
    assert await state.set_lead_approval([b], "rejected") == 1
    assert (await state.get_prospect(b)).lead_approval == "rejected"
    with pytest.raises(ValueError):
        await state.set_lead_approval([a], "maybe")


# ── the Leads page's API ──


def _seed(db_path):
    with closing(sqlite3.connect(db_path)) as c, c:
        c.execute("INSERT INTO companies (id, name, domain, industry, location) "
                  "VALUES ('c1', 'Smile Clinic', 'smile.example', 'dental clinic', 'Springfield')")
        c.execute("INSERT INTO companies (id, name, domain, industry) VALUES ('c2', 'Top Roofs', 'roofs.example', 'roofing')")
        c.execute("INSERT INTO prospects (id, company_id, first_name, email, email_status, status, lead_approval) "
                  "VALUES ('p1', 'c1', 'Ana', 'ana@smile.example', 'verified', 'new', 'pending')")
        c.execute("INSERT INTO prospects (id, company_id, first_name, email, email_status, status, lead_approval) "
                  "VALUES ('p2', 'c2', 'Bo', 'bo@roofs.example', 'risky', 'new', 'pending')")
        c.execute("INSERT INTO prospects (id, company_id, first_name, email, status, lead_approval) "
                  "VALUES ('p3', 'c2', 'Cy', 'cy@roofs.example', 'contacted', 'approved')")


def test_leads_page_lists_business_types_and_counts(client):  # noqa: F811
    client.get("/api/signals")                                    # creates the database
    _seed(client.db_path)
    d = client.get("/api/leads/found").json()
    assert d["counts"] == {"pending": 2, "approved": 1, "rejected": 0}
    types = {l["id"]: l["business_type"] for l in d["leads"]}
    assert types == {"p1": "dental clinic", "p2": "roofing", "p3": "roofing"}
    smile = next(l for l in d["leads"] if l["id"] == "p1")
    assert smile["company"] == "Smile Clinic" and smile["location"] == "Springfield"


@pytest.mark.asyncio
async def test_only_approved_leads_are_sent_to_sitewatch(state):
    await _lead(state, 1, "pending")
    await _lead(state, 2, "rejected")
    await _lead(state, 3, "approved")
    assert [c["domain"] for c in await state.companies_needing_sitewatch()] == ["clinic3.example"]


def test_approving_sends_leads_to_sitewatch(client, monkeypatch):  # noqa: F811
    import harvey.dashboard as dash
    client.get("/api/signals")
    _seed(client.db_path)
    calls = []

    async def fake_sync():
        calls.append(1)
        return (2, 0)
    monkeypatch.setattr(dash, "_sitewatch_sync", fake_sync)
    r = client.post("/api/leads/approval", json={"ids": ["p1", "p2"], "approval": "approved"}).json()
    assert calls and r["sent_to_sitewatch"] == 2 and "sent to SiteWatch" in r["message"]

    client.post("/api/leads/approval", json={"ids": ["p2"], "approval": "rejected"})
    assert len(calls) == 1                                        # rejecting sends nothing


def test_leads_list_carries_each_companys_report(client):  # noqa: F811
    client.get("/api/signals")
    _seed(client.db_path)
    with closing(sqlite3.connect(client.db_path)) as c, c:
        c.execute("INSERT INTO sitewatch_scans (company_id, scan_id, state) VALUES ('c1', 5, 'done')")
        c.execute("INSERT INTO sitewatch_scans (company_id, scan_id, state) VALUES ('c2', 6, 'pending')")
        c.execute("INSERT INTO observations (id, company_id, signal_code, collector, value_num, value_text, evidence_url) "
                  "VALUES ('o1', 'c1', 'SITEWATCH_ISSUES', 'sitewatch', 3, 'Hidden links', 'https://sitewatch.example/report/x')")
        c.execute("INSERT INTO observations (id, company_id, signal_code, collector, value_num, value_text) "
                  "VALUES ('o2', 'c1', 'SITEWATCH_STATUS', 'sitewatch', 60, 'critical')")
    leads = {l["id"]: l for l in client.get("/api/leads/found").json()["leads"]}
    assert leads["p1"]["report"] == {"state": "done", "error": "", "status": "critical", "score": 60,
                                     "issues": 3, "issue_text": "Hidden links",
                                     "report_url": "https://sitewatch.example/report/x"}
    assert leads["p2"]["report"]["state"] == "pending"


def test_sync_endpoint_reports_when_scans_are_off(client, monkeypatch):  # noqa: F811
    import harvey.dashboard as dash

    async def off():
        return None
    monkeypatch.setattr(dash, "_sitewatch_sync", off)
    assert client.post("/api/sitewatch/sync").json() == {"success": True, "active": False}


def test_leads_page_approves_and_rejects(client, monkeypatch):  # noqa: F811
    import harvey.dashboard as dash

    async def off():
        return None
    monkeypatch.setattr(dash, "_sitewatch_sync", off)
    client.get("/api/signals")
    _seed(client.db_path)
    r = client.post("/api/leads/approval", json={"ids": ["p1", "p2"], "approval": "approved"}).json()
    assert r["success"] and r["changed"] == 2 and "next cycles" in r["message"]
    client.post("/api/leads/approval", json={"ids": ["p2"], "approval": "rejected"})
    counts = client.get("/api/leads/found").json()["counts"]
    assert counts == {"pending": 0, "approved": 2, "rejected": 1}

    assert client.post("/api/leads/approval", json={"ids": [], "approval": "approved"}).status_code == 400
    assert client.post("/api/leads/approval", json={"ids": ["p1"], "approval": "maybe"}).status_code == 400


def test_sidebar_has_lead_settings_and_leads(client):  # noqa: F811
    html = client.get("/").text
    sidebar = html[html.index('<nav aria-label="Sections">'):html.index("</nav>")]
    assert ">Lead settings<" in sidebar and ">Find leads<" not in sidebar
    assert "showTab('found', this)" in sidebar and 'id="found"' in html
    # Outbox and Settings are hidden from the sidebar; Settings is a tab in Lead settings.
    assert "<!-- <button onclick=\"showTab('outbox'" in sidebar
    assert "<!-- <button onclick=\"showTab('settings'" in sidebar
    assert 'id="leads-pane-settings"' in html and 'id="settings"' not in html
    assert "<!-- <button onclick=\"showTab('integrations'" in sidebar
    assert 'id="leads-pane-integrations"' in html and 'id="integrations"' not in html
    assert "<!-- <button onclick=\"showTab('controls'" in sidebar
    assert 'id="leads-pane-controls"' in html and 'id="controls"' not in html


# ── campaigns from approved leads, after the website report ──


@pytest.mark.asyncio
async def test_with_reports_required_only_leads_with_a_finished_report_count(state):
    a = await _lead(state, 1, "approved")
    p = await state.get_prospect(a)
    assert await state.count_writable_prospects() == 1
    assert await state.count_writable_prospects(require_report=True) == 0      # never scanned
    await state.set_sitewatch_scan(p.company_id, 7)                              # pending
    assert await state.count_writable_prospects(require_report=True) == 0
    await state.finish_sitewatch_scan(p.company_id, "failed", "timed out")
    assert not await state.sitewatch_report_ready(p.company_id)                  # failed holds it
    await state.finish_sitewatch_scan(p.company_id, "done")
    assert await state.count_writable_prospects(require_report=True) == 1
    assert await state.sitewatch_report_ready(p.company_id)


@pytest.mark.asyncio
async def test_write_without_report_and_no_website_are_ready(state):
    a = await _lead(state, 1, "approved")
    p = await state.get_prospect(a)
    await state.set_sitewatch_scan(p.company_id, None, state="skipped")
    assert await state.sitewatch_report_ready(p.company_id)
    nosite = await state.add_company(Company(name="Corner Shop", domain=""))
    assert await state.sitewatch_report_ready(nosite)
    assert await state.sitewatch_report_ready("")


def test_rescan_and_skip_from_the_leads_page(client, monkeypatch):  # noqa: F811
    import harvey.dashboard as dash

    async def fake_sync():
        return (1, 0)
    monkeypatch.setattr(dash, "_sitewatch_sync", fake_sync)
    client.get("/api/signals")
    _seed(client.db_path)
    with closing(sqlite3.connect(client.db_path)) as c, c:
        c.execute("INSERT INTO sitewatch_scans (company_id, scan_id, state) VALUES ('c1', 5, 'failed')")
    r = client.post("/api/leads/report", json={"ids": ["p1"], "action": "rescan"}).json()
    assert r["success"] and "sent to SiteWatch again" in r["message"]
    with closing(sqlite3.connect(client.db_path)) as c:
        assert c.execute("SELECT COUNT(*) FROM sitewatch_scans WHERE company_id='c1'").fetchone()[0] == 0
    client.post("/api/leads/report", json={"ids": ["p1"], "action": "skip"})
    with closing(sqlite3.connect(client.db_path)) as c:
        assert c.execute("SELECT state FROM sitewatch_scans WHERE company_id='c1'").fetchone()[0] == "skipped"
    assert client.post("/api/leads/report", json={"ids": ["p1"], "action": "x"}).status_code == 400


def test_create_campaign_writes_only_ready_leads(client, monkeypatch):  # noqa: F811
    import time
    import harvey.dashboard as dash
    from harvey.agents import writer as writer_mod
    from harvey.config import load_config
    from harvey.paths import PROJECT_ROOT

    cfg = load_config(str(PROJECT_ROOT / "harvey.yaml"))
    monkeypatch.setattr(dash, "_config_env", lambda: (cfg, SimpleNamespace(sitewatch_url="", sitewatch_api_token="")))

    async def required(*a):
        return True
    import harvey.collectors.sitewatch as sw
    monkeypatch.setattr(sw, "reports_required", required)
    got = {}

    async def fake_run(self, only_ids=None):
        got["ids"] = only_ids
        self.written = len(only_ids)
    monkeypatch.setattr(writer_mod.Writer, "run", fake_run)

    client.get("/api/signals")
    _seed(client.db_path)
    with closing(sqlite3.connect(client.db_path)) as c, c:
        c.execute("UPDATE prospects SET lead_approval='approved' WHERE id IN ('p1','p2')")
        c.execute("INSERT INTO sitewatch_scans (company_id, scan_id, state) VALUES ('c1', 5, 'done')")
        c.execute("INSERT INTO sitewatch_scans (company_id, scan_id, state) VALUES ('c2', 6, 'pending')")

    r = client.post("/api/campaigns/create", json={"ids": ["p1", "p2", "p3"]}).json()
    assert r["started"] and r["ready"] == 1
    assert r["skipped"] == {"already contacted": 1, "waiting for website report": 1}
    for _ in range(50):
        st = client.get("/api/campaigns/create").json()
        if not st["running"]:
            break
        time.sleep(0.05)
    assert got["ids"] == ["p1"] and st["report"]["written"] == 1

    none = client.post("/api/campaigns/create", json={"ids": ["p2"]}).json()
    assert not none["success"] and "waiting for website report" in none["message"]
