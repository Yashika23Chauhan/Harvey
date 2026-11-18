"""SiteWatch website scans: the client, the two-phase stage, the writer's
wait and the facts it gets. All data is fictional (example.com domains)."""

import json
import os
import tempfile
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from harvey.collectors import sitewatch as sw
from harvey.config import SiteWatchConfig
from harvey.integrations.sitewatch import SiteWatchClient, SiteWatchError
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.signals import seed_signal_catalog
from harvey.state import StateManager

TOKEN = "tok-0123456789abcdefghijklmnopqrstuvwxyz"
BASE = "https://sitewatch.example"
REPORT = BASE + "/report/abc"


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "a.db"))
        await sm.init_db()
        await seed_signal_catalog(sm)
        yield sm


async def _confirm(state, *codes):
    for code in codes:
        await state.set_signal_status(code, "confirmed")


def _config(enabled=True, **kw):
    return SimpleNamespace(sitewatch=SiteWatchConfig(enabled=enabled, **kw))


ENV = SimpleNamespace(sitewatch_url=BASE, sitewatch_api_token=TOKEN)


UI_REPORT = REPORT + "?tab=ui"
SEO_REPORT = REPORT + "?tab=seo"
AUDIT = {
    "ui": {"score": 62, "grade": "fair", "report_url": UI_REPORT, "findings": [
        {"code": "UI_NO_VIEWPORT", "severity": "high", "title": "No mobile viewport",
         "plain": "The site isn't set up for phones.", "where": "/"},
        {"code": "UI_NO_FAVICON", "severity": "low", "title": "No favicon", "plain": "No icon.", "where": "/"},
    ]},
    "seo": {"score": 92, "grade": "good", "report_url": SEO_REPORT, "findings": [
        {"code": "SEO_NO_META_DESCRIPTION", "severity": "medium", "title": "Pages have no meta description",
         "plain": "Google writes its own snippet.", "where": "/, /about/"},
    ]},
}


class FakeSiteWatch:
    """SiteWatch's API, in memory. Scans finish when ``finish()`` is called.
    ``audit`` is the UI/SEO part (None: an older SiteWatch without it)."""

    def __init__(self, findings=None, audit=None):
        self.audit = audit
        self.requests = []
        self.scans = {}
        self.findings = findings if findings is not None else [
            {"code": "HIDDEN_LINKS", "severity": "critical", "title": "Hidden links",
             "plain": "Invisible links to other websites were added.", "where": "/"},
            {"code": "SSL_EXPIRING", "severity": "low", "title": "TLS certificate expiring",
             "plain": "The certificate expires soon.", "where": "ssl"},
        ]

    def finish(self, state="completed"):
        for scan in self.scans.values():
            scan["state"] = state

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        if request.method == "POST" and request.url.path == "/api/prospect-scans":
            body = json.loads(request.content)
            self.requests.append(body)
            scan_id = len(self.scans) + 1
            self.scans[scan_id] = {"state": "queued"}
            return httpx.Response(202, json={"scan_id": scan_id, "state": "queued", "reused": False})
        if request.url.path == "/api/ping":
            return httpx.Response(200, json={"ok": True, "prospects_client": "Prospects"})
        scan_id = int(request.url.path.rsplit("/", 1)[1])
        scan = self.scans.get(scan_id)
        if scan is None:
            return httpx.Response(404, json={"message": "Not Found"})
        done = scan["state"] == "completed"
        body = {
            "scan_id": scan_id, "state": scan["state"], "status": "critical" if done else None,
            "score": 42 if done else None, "report_url": REPORT if scan["state"] != "queued" else None,
            "findings": self.findings if done else [],
        }
        if self.audit is not None:
            body.update(ui=self.audit["ui"] if done else None, seo=self.audit["seo"] if done else None)
        return httpx.Response(200, json=body)

    def client(self):
        return SiteWatchClient(BASE, TOKEN, transport=httpx.MockTransport(self.handler))


async def _company_with_prospect(state, n=1, status="new"):
    cid = await state.add_company(Company(name=f"Plumbing {n}", domain=f"plumbing{n}.example"))
    pid = await state.add_prospect(Prospect(
        first_name="Ana", last_name="Doe", title="Owner", company=f"Plumbing {n}", company_id=cid,
        email=f"ana@plumbing{n}.example", email_status="verified", status=status))
    return cid, pid


# ── the client ──

@pytest.mark.asyncio
async def test_client_reads_a_scan_and_filters_by_severity():
    fake = FakeSiteWatch()
    client = fake.client()
    started = await client.request_scan("https://plumbing1.example", "Plumbing 1")
    assert started.scan_id == 1 and started.state == "queued" and not started.done
    fake.finish()
    result = await client.get_scan(1)
    assert result.done and result.status == "critical" and result.report_url == REPORT
    assert [f["code"] for f in result.issues("medium")] == ["HIDDEN_LINKS"]
    assert len(result.issues("low")) == 2
    assert await client.ping() == "Prospects"


@pytest.mark.asyncio
async def test_client_reads_the_ui_and_seo_reports():
    fake = FakeSiteWatch(audit=AUDIT)
    client = fake.client()
    await client.request_scan("https://plumbing1.example")
    assert (await client.get_scan(1)).ui is None                 # not finished yet
    fake.finish()
    result = await client.get_scan(1)
    assert result.ui.score == 62 and result.ui.grade == "fair" and result.ui.report_url == UI_REPORT
    assert [f["code"] for f in result.ui.issues("medium")] == ["UI_NO_VIEWPORT"]
    assert result.seo.score == 92 and result.seo.report_url == SEO_REPORT
    assert result.report_url == REPORT                           # the security link is unchanged


@pytest.mark.asyncio
async def test_an_older_sitewatch_without_reports_still_works():
    fake = FakeSiteWatch()
    client = fake.client()
    await client.request_scan("https://plumbing1.example")
    fake.finish()
    result = await client.get_scan(1)
    assert result.ui is None and result.seo is None and result.status == "critical"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,needle", [
    (401, "refused the API token"),
    (503, "turned off"),
    (404, "no such scan"),
    (500, "answered 500"),
])
async def test_client_errors_are_plain_and_never_leak_the_token(status, needle):
    def handler(request):
        return httpx.Response(status, json={"error": f"bad {TOKEN}"})
    client = SiteWatchClient(BASE, TOKEN, transport=httpx.MockTransport(handler))
    with pytest.raises(SiteWatchError) as e:
        await client.get_scan(1)
    assert needle in str(e.value)
    assert TOKEN not in str(e.value)


@pytest.mark.asyncio
async def test_client_unreachable():
    def handler(request):
        raise httpx.ConnectError("refused")
    client = SiteWatchClient(BASE, TOKEN, transport=httpx.MockTransport(handler))
    with pytest.raises(SiteWatchError, match="Couldn't reach SiteWatch"):
        await client.ping()


def test_from_config_needs_enabled_and_both_env_values():
    assert SiteWatchClient.from_config(_config(enabled=False), ENV) is None
    assert SiteWatchClient.from_config(_config(), SimpleNamespace(sitewatch_url=BASE, sitewatch_api_token="")) is None
    assert SiteWatchClient.from_config(_config(), ENV) is not None


def test_config_rejects_unknown_severity():
    with pytest.raises(ValueError):
        SiteWatchConfig(min_severity="urgent")


# ── the stage ──

@pytest.mark.asyncio
async def test_nothing_is_scanned_until_a_signal_is_confirmed(state):
    fake = FakeSiteWatch()
    await _company_with_prospect(state)
    # The client is built from config; nothing confirmed means no client.
    assert await sw.active_client(state, _config(), ENV) is None
    assert await sw.run_sitewatch_stage(state, _config(), ENV) == (0, 0)
    assert fake.requests == []


@pytest.mark.asyncio
async def test_scan_is_requested_then_read_back_into_observations(state):
    await _confirm(state, "SITEWATCH_ISSUES", "SITEWATCH_STATUS")
    fake = FakeSiteWatch()
    cid, _ = await _company_with_prospect(state)
    await _company_with_prospect(state, n=2, status="contacted")   # nobody left to write to

    # Cycle 1: asked for, still queued.
    assert await sw.run_sitewatch_stage(state, _config(), ENV, client=fake.client()) == (1, 0)
    assert fake.requests == [{"url": "https://plumbing1.example", "name": "Plumbing 1"}]
    assert await state.sitewatch_waiting(cid, 60) is True

    # Cycle 2: finished, read back; never asked for twice.
    fake.finish()
    assert await sw.run_sitewatch_stage(state, _config(), ENV, client=fake.client()) == (0, 2)
    assert len(fake.requests) == 1
    assert await state.sitewatch_waiting(cid, 60) is False

    latest = {o["signal_code"]: o for o in await state.get_observations(company_id=cid, latest_only=True)}
    assert latest["SITEWATCH_ISSUES"]["value_num"] == 1          # the low one isn't counted
    assert "Invisible links" in latest["SITEWATCH_ISSUES"]["value_text"]
    assert latest["SITEWATCH_ISSUES"]["evidence_url"] == REPORT
    assert latest["SITEWATCH_STATUS"]["value_text"] == "critical"
    assert latest["SITEWATCH_STATUS"]["value_num"] == 42


@pytest.mark.asyncio
async def test_ui_and_seo_reports_become_their_own_observations(state):
    await _confirm(state, *sw.CODES)
    fake = FakeSiteWatch(audit=AUDIT)
    cid, _ = await _company_with_prospect(state)
    await sw.run_sitewatch_stage(state, _config(), ENV, client=fake.client())
    fake.finish()
    assert await sw.run_sitewatch_stage(state, _config(), ENV, client=fake.client()) == (0, 4)

    latest = {o["signal_code"]: o for o in await state.get_observations(company_id=cid, latest_only=True)}
    assert latest["SITEWATCH_UI_ISSUES"]["value_num"] == 1            # the low one isn't counted
    assert "set up for phones" in latest["SITEWATCH_UI_ISSUES"]["value_text"]
    assert latest["SITEWATCH_UI_ISSUES"]["evidence_url"] == UI_REPORT
    assert latest["SITEWATCH_SEO_ISSUES"]["value_num"] == 1
    assert latest["SITEWATCH_SEO_ISSUES"]["evidence_url"] == SEO_REPORT
    assert latest["SITEWATCH_ISSUES"]["evidence_url"] == REPORT


@pytest.mark.asyncio
async def test_ui_and_seo_need_their_own_confirmation(state):
    await _confirm(state, "SITEWATCH_ISSUES", "SITEWATCH_STATUS")      # confirmed before UI/SEO existed
    fake = FakeSiteWatch(audit=AUDIT)
    cid, _ = await _company_with_prospect(state)
    await sw.run_sitewatch_stage(state, _config(), ENV, client=fake.client())
    fake.finish()
    assert await sw.run_sitewatch_stage(state, _config(), ENV, client=fake.client()) == (0, 2)
    assert await state.get_observations(company_id=cid, signal_code="SITEWATCH_UI_ISSUES") == []


@pytest.mark.asyncio
async def test_failed_scan_is_closed_without_observations(state):
    await _confirm(state, "SITEWATCH_ISSUES")
    fake = FakeSiteWatch()
    cid, _ = await _company_with_prospect(state)
    await sw.run_sitewatch_stage(state, _config(), ENV, client=fake.client())
    fake.finish("failed")
    assert await sw.run_sitewatch_stage(state, _config(), ENV, client=fake.client()) == (0, 0)
    assert await state.sitewatch_waiting(cid, 60) is False
    assert await state.get_observations(company_id=cid, signal_code="SITEWATCH_ISSUES") == []


@pytest.mark.asyncio
async def test_unreachable_sitewatch_never_holds_email(state):
    await _confirm(state, "SITEWATCH_ISSUES")
    cid, _ = await _company_with_prospect(state)
    await _company_with_prospect(state, n=2)

    def handler(request):
        raise httpx.ConnectError("refused")
    client = SiteWatchClient(BASE, TOKEN, transport=httpx.MockTransport(handler))
    assert await sw.run_sitewatch_stage(state, _config(), ENV, client=client) == (0, 0)
    # The first failure is recorded so the writer doesn't wait on it; the
    # stage stops asking for the rest this cycle.
    assert await state.sitewatch_waiting(cid, 60) is False


@pytest.mark.asyncio
async def test_a_slow_scan_stops_holding_email_after_max_wait(state):
    await _confirm(state, "SITEWATCH_ISSUES")
    fake = FakeSiteWatch()
    cid, _ = await _company_with_prospect(state)
    await sw.run_sitewatch_stage(state, _config(), ENV, client=fake.client())
    async with state._connect() as db:
        await db.execute("UPDATE sitewatch_scans SET requested_at = datetime('now', '-2 hours')")
        await db.commit()
    assert await state.sitewatch_waiting(cid, 60) is False


# ── the writer ──

class RecordingBrain:
    def __init__(self):
        self.prompts = []

    def load_skills_for_agent(self, name):
        return ""

    def load_prompt(self, name, **kw):
        return "PROMPT"

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return {"subject": "hi", "body": "Hi"}


def _writer(state, enabled=True):
    from harvey.agents.writer import Writer
    cfg = SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(provider="gmail", require_approval=True)),
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
        persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
        sitewatch=SiteWatchConfig(enabled=enabled),
    )
    brain = RecordingBrain()
    w = Writer(brain, state, cfg, env=ENV)
    w.skills = ""
    return w, brain


@pytest.mark.asyncio
async def test_writer_gets_the_issues_report_link_and_the_rule(state):
    cid, pid = await _company_with_prospect(state)
    await state.add_observation("SITEWATCH_ISSUES", company_id=cid, collector="sitewatch",
                                value_num=1, value_text="Invisible links to other websites were added.",
                                evidence_url=REPORT)
    await state.add_observation("NO_HTTPS", company_id=cid, collector="profile", value_num=1)
    await state.add_observation("NOT_MOBILE_FRIENDLY", company_id=cid, collector="profile", value_num=0)
    w, brain = _writer(state)
    await w._write_personal_email(await state.get_prospect(pid))
    prompt = brain.prompts[0]
    assert "flagged 1 security problem(s)" in prompt and "Invisible links" in prompt
    assert REPORT in prompt
    assert "Not secure" in prompt
    assert "mobile viewport" not in prompt          # a check that found nothing adds no line
    assert "WEBSITE ISSUES" in prompt


@pytest.mark.asyncio
async def test_writer_gets_the_ui_and_seo_problems_and_one_basic_report_link(state):
    cid, pid = await _company_with_prospect(state)
    await state.add_observation("SITEWATCH_ISSUES", company_id=cid, collector="sitewatch", value_num=0, evidence_url=REPORT)
    await state.add_observation("SITEWATCH_UI_ISSUES", company_id=cid, collector="sitewatch", value_num=1,
                                value_text="The site isn't set up for phones.", evidence_url=UI_REPORT)
    await state.add_observation("SITEWATCH_SEO_ISSUES", company_id=cid, collector="sitewatch", value_num=1,
                                value_text="Google writes its own snippet.", evidence_url=SEO_REPORT)
    # The profile's own check says the same as the UI report: named once, not twice
    await state.add_observation("NOT_MOBILE_FRIENDLY", company_id=cid, collector="profile", value_num=1)
    w, brain = _writer(state)
    await w._write_personal_email(await state.get_prospect(pid))
    prompt = brain.prompts[0]
    assert "flagged 1 responsive & UI problem(s)" in prompt and "flagged 1 SEO problem(s)" in prompt
    assert "mobile viewport tag" not in prompt
    assert "security problem(s)" not in prompt                         # none flagged, no line
    # One link, the Basic report (the section links point into the Advanced one)
    assert f"open without logging in: {REPORT}\n" in prompt
    assert prompt.count(REPORT) == 1 and UI_REPORT not in prompt and SEO_REPORT not in prompt


@pytest.mark.asyncio
async def test_clean_site_adds_nothing(state):
    cid, pid = await _company_with_prospect(state)
    await state.add_observation("SITEWATCH_ISSUES", company_id=cid, collector="sitewatch",
                                value_num=0, evidence_url=REPORT)
    w, brain = _writer(state)
    await w._write_personal_email(await state.get_prospect(pid))
    assert "WEBSITE ISSUES" not in brain.prompts[0]
    assert REPORT not in brain.prompts[0]


@pytest.mark.asyncio
async def test_writer_waits_for_the_scan_only_when_scanning_is_on(state):
    _, pid = await _company_with_prospect(state)
    prospect = await state.get_prospect(pid)

    w, _ = _writer(state)
    assert await w._scanned_or_not_waiting([prospect]) == [prospect]   # no signal confirmed
    await _confirm(state, "SITEWATCH_ISSUES")
    assert await w._scanned_or_not_waiting([prospect]) == []           # never asked for yet

    off, _ = _writer(state, enabled=False)
    assert await off._scanned_or_not_waiting([prospect]) == [prospect]


def test_report_links_pass_the_link_check(monkeypatch):
    from harvey.gate import allowed_link_domains
    monkeypatch.setenv("SITEWATCH_URL", BASE)
    cfg = SimpleNamespace(persona=SimpleNamespace(email="sam@agency.example"),
                          channels=SimpleNamespace(email=SimpleNamespace(link_domains=[])),
                          sitewatch=SiteWatchConfig(enabled=True))
    assert "sitewatch.example" in allowed_link_domains(cfg)
    cfg.sitewatch = SiteWatchConfig(enabled=False)
    assert "sitewatch.example" not in allowed_link_domains(cfg)
