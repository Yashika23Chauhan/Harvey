"""SiteWatch's separate UI and SEO reports on Harvey's side: parsing odd
answers, which observations get written, the writer's facts, the dashboard
endpoints and JavaScript, and the CLI. All data is fictional (example.com)."""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from harvey.collectors import sitewatch as sw
from harvey.integrations.sitewatch import AuditReport, ScanResult, SiteWatchClient
from harvey.signals import SIGNAL_CATALOG, seed_signal_catalog
from harvey.state import StateManager
from tests.test_sitewatch import AUDIT, BASE, ENV, REPORT, SEO_REPORT, TOKEN, UI_REPORT, FakeSiteWatch, _company_with_prospect, _config, _confirm, _writer

WEB = Path(__file__).resolve().parent.parent / "harvey" / "web"


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "a.db"))
        await sm.init_db()
        await seed_signal_catalog(sm)
        yield sm


def _finding(code="UI_NO_VIEWPORT", severity="high", plain="Not set up for phones."):
    return {"code": code, "severity": severity, "title": code.title(), "plain": plain, "where": "/"}


# ── parsing what SiteWatch sends ──


@pytest.mark.parametrize("data", [None, [], "ui", 42, True])
def test_a_report_that_isnt_an_object_is_no_report(data):
    assert AuditReport.parse("ui", data) is None


@pytest.mark.parametrize("score,expected", [(150, 100), (-5, 0), ("73", 73), ("abc", 100), (None, 100), (61.9, 61)])
def test_scores_are_clamped_to_0_100(score, expected):
    assert AuditReport.parse("seo", {"score": score}).score == expected


def test_bad_findings_are_dropped_and_missing_fields_become_empty():
    report = AuditReport.parse("ui", {"findings": [_finding(), "junk", None, 7, {"code": "UI_X"}]})
    assert [f["code"] for f in report.findings] == ["UI_NO_VIEWPORT", "UI_X"]
    assert report.findings[1] == {"code": "UI_X", "severity": "", "title": "", "plain": "", "where": ""}
    assert report.issues("low") == [report.findings[0]]           # no severity: never counted
    assert AuditReport.parse("ui", {"findings": "nope"}).findings == []


def test_issues_respect_the_severity_floor():
    report = AuditReport.parse("seo", {"findings": [_finding(severity=s) for s in ("high", "medium", "low", "info")]})
    assert [f["severity"] for f in report.issues("high")] == ["high"]
    assert [f["severity"] for f in report.issues("medium")] == ["high", "medium"]
    assert len(report.issues("low")) == 3
    assert len(report.issues("unknown")) == 2                     # unknown floor falls back to medium


@pytest.mark.asyncio
async def test_one_report_can_be_present_without_the_other():
    def handler(request):
        return httpx.Response(200, json={"scan_id": 3, "state": "completed", "status": "clean", "score": 0,
                                         "report_url": REPORT, "findings": [], "ui": AUDIT["ui"], "seo": None})
    result = await SiteWatchClient(BASE, TOKEN, transport=httpx.MockTransport(handler)).get_scan(3)
    assert result.ui.report_url == UI_REPORT and result.seo is None


# ── observations ──


def _result(ui=None, seo=None):
    return ScanResult(scan_id=1, state="completed", status="clean", score=0, report_url=REPORT, ui=ui, seo=seo)


def test_no_report_from_an_older_sitewatch_writes_no_ui_or_seo_rows():
    rows = sw._observations("c1", _result(), set(sw.CODES), "medium")
    assert {r["signal_code"] for r in rows} == {"SITEWATCH_ISSUES", "SITEWATCH_STATUS"}


def test_a_clean_report_is_still_recorded_as_zero():
    rows = sw._observations("c1", _result(ui=AuditReport("ui", 100, "good", UI_REPORT, [])), {"SITEWATCH_UI_ISSUES"}, "medium")
    assert rows == [{"signal_code": "SITEWATCH_UI_ISSUES", "company_id": "c1", "collector": "sitewatch",
                     "value_num": 0, "value_text": "", "evidence_url": UI_REPORT}]


def test_only_the_worst_three_are_named_and_the_text_is_capped():
    findings = [_finding(code=f"SEO_{i}", severity="medium", plain=f"Problem {i} " + "x" * 400) for i in range(5)]
    rows = sw._observations("c1", _result(seo=AuditReport("seo", 60, "fair", SEO_REPORT, findings)), {"SITEWATCH_SEO_ISSUES"}, "medium")
    assert rows[0]["value_num"] == 5
    assert rows[0]["value_text"].count("Problem") == 3
    assert len(rows[0]["value_text"]) <= 1000


def test_the_min_severity_setting_applies_to_ui_and_seo_too():
    ui = AuditReport("ui", 80, "good", UI_REPORT, [_finding(severity="high"), _finding(code="UI_NO_FAVICON", severity="low")])
    assert sw._observations("c1", _result(ui=ui), {"SITEWATCH_UI_ISSUES"}, "high")[0]["value_num"] == 1
    assert sw._observations("c1", _result(ui=ui), {"SITEWATCH_UI_ISSUES"}, "low")[0]["value_num"] == 2


def test_the_new_signals_are_in_the_catalog_as_sitewatch_numbers():
    by_code = {s["code"]: s for s in SIGNAL_CATALOG}
    for code in ("SITEWATCH_UI_ISSUES", "SITEWATCH_SEO_ISSUES"):
        assert by_code[code]["collector"] == "sitewatch" and by_code[code]["value_type"] == "num"
    assert set(sw.CODES) <= set(by_code)


@pytest.mark.asyncio
async def test_new_signals_start_proposed_and_a_rescan_updates_them(state):
    proposed = {r["code"] for r in await state.get_signal_codes(status="proposed")}
    assert set(sw.AUDIT_CODES.values()) <= proposed
    assert not set(sw.AUDIT_CODES.values()) & await state.confirmed_signal_codes()

    await _confirm(state, *sw.CODES)
    cid, _ = await _company_with_prospect(state)
    fake = FakeSiteWatch(audit=AUDIT)
    await sw.run_sitewatch_stage(state, _config(), ENV, client=fake.client())
    fake.finish()
    await sw.run_sitewatch_stage(state, _config(), ENV, client=fake.client())
    # The site gets fixed; the next scan's numbers replace the old ones
    await state.add_observation("SITEWATCH_UI_ISSUES", company_id=cid, collector="sitewatch", value_num=0,
                                evidence_url=UI_REPORT, observed_at="2999-01-01 00:00:00")
    latest = {o["signal_code"]: o for o in await state.get_observations(company_id=cid, latest_only=True)}
    assert latest["SITEWATCH_UI_ISSUES"]["value_num"] == 0
    facts = await sw.website_issue_facts(state, cid) or []
    assert not any("UI report" in f for f in facts)
    assert any("flagged 1 SEO problem(s)" in f for f in facts)


# ── the writer's facts ──


@pytest.mark.asyncio
async def test_facts_without_a_report_link_name_the_problems_only(state):
    cid, _ = await _company_with_prospect(state)
    await state.add_observation("SITEWATCH_SEO_ISSUES", company_id=cid, collector="sitewatch", value_num=2, value_text="No titles")
    facts = await sw.website_issue_facts(state, cid)
    assert facts == ["- A website scan (SiteWatch) flagged 2 SEO problem(s) on their site: No titles"]


@pytest.mark.asyncio
async def test_profile_checks_stay_when_sitewatch_has_no_matching_report(state):
    cid, _ = await _company_with_prospect(state)
    await state.add_observation("NO_HTTPS", company_id=cid, collector="profile", value_num=1)
    await state.add_observation("NO_META_DESCRIPTION", company_id=cid, collector="profile", value_num=1)
    # Only an SEO report: the HTTPS line (UI) stays, the meta description line (SEO) goes
    await state.add_observation("SITEWATCH_SEO_ISSUES", company_id=cid, collector="sitewatch", value_num=0, evidence_url=SEO_REPORT)
    facts = "\n".join(await sw.website_issue_facts(state, cid) or [])
    assert "Not secure" in facts
    assert "no meta description" not in facts
    assert SEO_REPORT not in facts                                # nothing flagged, no link


@pytest.mark.asyncio
async def test_no_facts_at_all_for_a_company_without_observations(state):
    cid, _ = await _company_with_prospect(state)
    assert await sw.website_issue_facts(state, cid) is None
    assert await sw.website_issue_facts(state, "") is None
    assert await sw.website_issue_facts(None, cid) is None


@pytest.mark.asyncio
async def test_writer_prompt_has_one_link_the_basic_report(state):
    cid, pid = await _company_with_prospect(state)
    await state.add_observation("SITEWATCH_ISSUES", company_id=cid, collector="sitewatch", value_num=1,
                                value_text="Hidden links", evidence_url=REPORT)
    await state.add_observation("SITEWATCH_UI_ISSUES", company_id=cid, collector="sitewatch", value_num=1,
                                value_text="Not set up for phones", evidence_url=UI_REPORT)
    await state.add_observation("SITEWATCH_SEO_ISSUES", company_id=cid, collector="sitewatch", value_num=1,
                                value_text="No titles", evidence_url=SEO_REPORT)
    w, brain = _writer(state)
    await w._write_personal_email(await state.get_prospect(pid))
    prompt = brain.prompts[0]
    for what in ("security", "responsive & UI", "SEO"):
        assert f"flagged 1 {what} problem(s)" in prompt
    # Three areas' problems, one link: the Basic report
    assert prompt.count(REPORT) == 1 and UI_REPORT not in prompt and SEO_REPORT not in prompt
    assert "scores and the main problems in plain words" in prompt


@pytest.mark.asyncio
async def test_the_basic_link_is_found_from_any_section_link(state):
    cid, _ = await _company_with_prospect(state)
    await state.add_observation("SITEWATCH_SEO_ISSUES", company_id=cid, collector="sitewatch", value_num=1,
                                value_text="No titles", evidence_url=REPORT + "?tab=advanced#seo")
    facts = await sw.website_issue_facts(state, cid)
    assert facts[-1].endswith(f"open without logging in: {REPORT}")
    assert sw.basic_report_url("", None, "javascript:x", UI_REPORT) == REPORT
    assert sw.basic_report_url("not a url") == ""


def test_email_editor_tells_the_writer_about_the_report_link():
    from harvey.email_template import render
    rules = render({"mode": "exact", "emails": [{"subject": "", "body": "Hi,\n\n{website issues} {report link}\n\nSam"}]})
    assert "the website report link from the FACTS" in rules


# ── the dashboard ──


from tests.test_dashboard_signals import client  # noqa: E402,F401  (fixture)


def test_scan_result_endpoint_returns_the_ui_and_seo_reports(client, monkeypatch):  # noqa: F811
    import harvey.dashboard as dash
    fake = FakeSiteWatch(audit=AUDIT)
    monkeypatch.setattr(dash, "_sitewatch_client", fake.client)
    started = client.post("/api/sitewatch/scan", json={"url": "plumbing.example"}).json()
    queued = client.get(f"/api/sitewatch/scans/{started['scan_id']}").json()
    assert queued["ui"] is None and queued["seo"] is None
    fake.finish()
    done = client.get(f"/api/sitewatch/scans/{started['scan_id']}").json()
    assert done["ui"]["score"] == 62 and done["ui"]["report_url"] == UI_REPORT
    assert done["seo"]["grade"] == "good" and [f["code"] for f in done["seo"]["findings"]] == ["SEO_NO_META_DESCRIPTION"]
    assert TOKEN not in json.dumps(done)


def test_scan_result_endpoint_from_an_older_sitewatch(client, monkeypatch):  # noqa: F811
    import harvey.dashboard as dash
    fake = FakeSiteWatch()
    monkeypatch.setattr(dash, "_sitewatch_client", fake.client)
    started = client.post("/api/sitewatch/scan", json={"url": "plumbing.example"}).json()
    fake.finish()
    done = client.get(f"/api/sitewatch/scans/{started['scan_id']}").json()
    assert done["success"] and done["ui"] is None and done["seo"] is None


def test_lead_details_keep_sitewatch_numbers_out_of_the_signal_list(client):  # noqa: F811
    import sqlite3
    from contextlib import closing
    client.get("/api/signals")
    with closing(sqlite3.connect(client.db_path)) as c, c:
        c.execute("INSERT INTO companies (id, name, domain) VALUES ('c1', 'Plumbing Co', 'plumbing.example')")
        c.execute("INSERT INTO prospects (id, company_id, first_name, email) VALUES ('p1', 'c1', 'Ana', 'ana@plumbing.example')")
        c.execute("INSERT INTO observations (id, company_id, signal_code, collector, value_num, value_text, evidence_url) "
                  "VALUES ('o1', 'c1', 'SITEWATCH_UI_ISSUES', 'sitewatch', 2, 'Not set up for phones', ?)", (UI_REPORT,))
    d = client.get("/api/leads/p1/details").json()
    assert d["success"]
    assert not any("UI problems" in s["label"] for s in d["needs"]["signals"])
    assert any("flagged 2 responsive & UI problem(s)" in p for p in d["needs"]["website_problems"])
    assert any(p.endswith(REPORT) for p in d["needs"]["website_problems"])     # the Basic report link


# ── the dashboard's JavaScript ──


def _js(call: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    source = (WEB / "app.js").read_text(encoding="utf-8")
    parts = []
    for pattern in (r"^function escHtml\(.*?^}\n", r"^function safeHref\(.*?^}\n", r"^function icon\(.*?^}\n",
                    r"^function swLink\(.*?^}\n", r"^const SW_AUDITS = .*?;\n", r"^function swAuditSummary\(.*?^}\n",
                    r"^function swBasicUrl\(.*?^}\n", r"^function swReportLinks\(.*?^}\n", r"^function swAuditHtml\(.*?^}\n"):
        match = re.search(pattern, source, re.S | re.M)
        assert match, pattern
        parts.append(match.group(0))
    script = "\n".join(parts) + f"\nprocess.stdout.write({call});"
    return subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8",
                          check=True, timeout=30).stdout


def test_js_summary_shows_counts_and_escapes_text():
    html = _js("swAuditSummary(" + json.dumps({"ui_issues": 2, "ui_text": "<img src=x onerror=alert(1)>",
                                               "seo_issues": 0, "seo_text": ""}) + ")")
    assert "<b>Responsive &amp; UI:</b> 2 problems" in html
    assert "&lt;img" in html and "<img" not in html
    assert "<b>SEO:</b> <span class=\"muted\">nothing worth mentioning</span>" in html
    assert _js("swAuditSummary({})") == ""                       # no report yet: nothing shown
    assert "1 problem<" in _js("swAuditSummary({ui_issues: 1})")


def test_js_one_reports_link_and_unsafe_urls_dropped():
    html = _js("swReportLinks(" + json.dumps({"ui_report_url": UI_REPORT, "seo_report_url": "javascript:alert(1)"}) + ", "
               + json.dumps(REPORT) + ")")
    # One link, named Reports, to the Basic report (the page itself has the Basic / Advanced tabs)
    assert html == f'<a href="{REPORT}" target="_blank" rel="noopener noreferrer">Reports</a>'
    # Only a section link known: the Basic link is still found from it
    assert f'href="{REPORT}"' in _js("swReportLinks(" + json.dumps({"seo_report_url": SEO_REPORT}) + ", '')")
    assert _js("swReportLinks({}, '')") == ""
    assert _js("swReportLinks({ui_report_url: 'javascript:x'}, '')") == ""


def test_js_report_block_lists_findings_and_hides_info():
    report = {"score": 62, "grade": "fair", "report_url": UI_REPORT, "findings": [
        _finding(), {"code": "X", "severity": "info", "title": "Just a note", "plain": "", "where": ""}]}
    html = _js(f"swAuditHtml({json.dumps(report)}, 'UI')")
    assert "UI report" in html and "62/100" in html and "1 improvement<" in html
    assert "Not set up for phones." in html and "Just a note" not in html
    assert "href=" not in html                                   # no per-section link: one Reports link per scan
    assert _js("swAuditHtml(null, 'UI')") == ""
    assert "Nothing to fix" in _js("swAuditHtml({score: 100, grade: 'good', findings: []}, 'SEO')")


# ── the CLI ──


def test_cli_scan_prints_all_three_reports(monkeypatch, capsys):
    import harvey.cli as cli
    import harvey.config as harvey_config
    import harvey.integrations.sitewatch as integration

    def handler(request):
        if request.method == "POST":
            return httpx.Response(202, json={"scan_id": 9, "state": "queued"})
        return httpx.Response(200, json={"scan_id": 9, "state": "completed", "status": "clean", "score": 0,
                                         "report_url": REPORT, "findings": [], **AUDIT})

    real = integration.SiteWatchClient
    monkeypatch.setattr(integration, "SiteWatchClient",
                        lambda url, token: real(url, token, transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(harvey_config, "load_config", lambda *a, **k: SimpleNamespace(sitewatch=SimpleNamespace(enabled=True)))
    monkeypatch.setattr(harvey_config, "load_env", lambda *a, **k: ENV)
    cli.cmd_sitewatch(SimpleNamespace(sitewatch_action="scan", target="plumbing.example", wait=1))

    out = capsys.readouterr().out
    assert f"Report: {REPORT}" in out
    assert "Responsive & UI report: fair (62/100, higher is better)" in out and UI_REPORT in out
    assert "[high] No mobile viewport (/)" in out
    assert "SEO report: good (92/100" in out and SEO_REPORT in out
