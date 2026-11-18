"""The dashboard's SiteWatch page: the on/off switch in the config file, the
connection settings in .env, and the scan list. Fictional data only."""

import shutil
import sqlite3
from contextlib import closing

import pytest

import harvey.config as harvey_config
import harvey.dashboard as dash
from harvey.config import ConfigError, load_config, set_sitewatch_enabled
from harvey.paths import PROJECT_ROOT
from tests.test_dashboard_signals import client  # noqa: F401  (fixture)


@pytest.fixture
def yaml_file(tmp_path):
    path = tmp_path / "harvey.yaml"
    shutil.copy(PROJECT_ROOT / "harvey.yaml", path)
    return path


@pytest.fixture
def page(client, tmp_path, yaml_file, monkeypatch):  # noqa: F811
    """The dashboard with its .env and config file in a temp folder."""
    env = tmp_path / ".env"
    env.write_text("SMTP_HOST=mail.example.com\n", encoding="utf-8")
    monkeypatch.setattr(dash, "ENV_FILE", env)
    monkeypatch.setattr(harvey_config, "_find_config_file", lambda: str(yaml_file))
    client.env_file, client.yaml_file = env, yaml_file
    return client


# ── the switch in the config file ──


def test_switch_adds_a_section_and_keeps_everything_else(yaml_file):
    before = yaml_file.read_text().splitlines()
    set_sitewatch_enabled(True, yaml_file)
    after = yaml_file.read_text().splitlines()
    assert after[:len(before)] == before                      # nothing above is touched
    assert load_config(str(yaml_file)).sitewatch.enabled is True
    set_sitewatch_enabled(False, yaml_file)
    assert load_config(str(yaml_file)).sitewatch.enabled is False
    assert len(yaml_file.read_text().splitlines()) == len(after)   # the same line flipped


def test_switch_fills_in_an_existing_section(yaml_file):
    with yaml_file.open("a", encoding="utf-8") as f:
        f.write("\nsitewatch:\n  # how long drafts wait\n  max_wait_minutes: 45\n")
    set_sitewatch_enabled(True, yaml_file)
    cfg = load_config(str(yaml_file)).sitewatch
    assert cfg.enabled is True and cfg.max_wait_minutes == 45
    assert "# how long drafts wait" in yaml_file.read_text()


def test_switch_refuses_a_section_it_cant_edit_safely(yaml_file):
    with yaml_file.open("a", encoding="utf-8") as f:
        f.write("\nsitewatch: {max_wait_minutes: 45}\n")
    before = yaml_file.read_text()
    with pytest.raises(ConfigError, match="by hand"):
        set_sitewatch_enabled(True, yaml_file)
    assert yaml_file.read_text() == before


# ── the page's API ──


def test_page_turns_scans_on_and_confirms_the_signals(page):
    r = page.post("/api/sitewatch/enabled", json={"enabled": True}).json()
    assert r["success"] and "Restart Harvey" in r["message"]
    assert load_config(str(page.yaml_file)).sitewatch.enabled is True
    d = page.get("/api/sitewatch").json()
    assert d["enabled"] is True and sorted(d["confirmed"]) == [
        "SITEWATCH_ISSUES", "SITEWATCH_SEO_ISSUES", "SITEWATCH_STATUS", "SITEWATCH_UI_ISSUES"]

    page.post("/api/sitewatch/enabled", json={"enabled": False})
    assert page.get("/api/sitewatch").json()["enabled"] is False
    assert page.post("/api/sitewatch/enabled", json={"enabled": "yes"}).status_code == 400


def test_connection_settings_are_saved_and_the_token_never_shown(page):
    r = page.post("/api/settings/env", json={"SITEWATCH_URL": "https://sitewatch.example/",
                                             "SITEWATCH_API_TOKEN": "tok-secret-123"}).json()
    assert r["success"]
    text = page.env_file.read_text()
    assert "SITEWATCH_URL=https://sitewatch.example\n" in text and "tok-secret-123" in text
    assert "SMTP_HOST=mail.example.com" in text
    d = page.get("/api/sitewatch")
    assert d.json()["url"] == "https://sitewatch.example" and d.json()["token_set"] is True
    assert "tok-secret-123" not in d.text

    bad = page.post("/api/settings/env", json={"SITEWATCH_URL": "sitewatch.example"})
    assert bad.status_code == 400


def test_scan_and_test_need_the_settings_first(page):
    assert page.post("/api/sitewatch/test").status_code == 400
    assert page.post("/api/sitewatch/scan", json={"url": "plumbing.example"}).status_code == 400
    page.post("/api/settings/env", json={"SITEWATCH_URL": "https://sitewatch.example",
                                         "SITEWATCH_API_TOKEN": "tok"})
    assert page.post("/api/sitewatch/scan", json={"url": ""}).status_code == 400


def test_scan_list_shows_each_companys_latest_result(page):
    page.get("/api/signals")                                  # creates the database
    with closing(sqlite3.connect(page.db_path)) as c, c:
        c.execute("INSERT INTO companies (id, name, domain) VALUES ('c1', 'Plumbing Co', 'plumbing.example')")
        c.execute("INSERT INTO companies (id, name, domain) VALUES ('c2', 'Roofing Co', 'roofing.example')")
        c.execute("INSERT INTO sitewatch_scans (company_id, scan_id, state) VALUES ('c1', 7, 'done')")
        c.execute("INSERT INTO sitewatch_scans (company_id, scan_id, state) VALUES ('c2', 8, 'pending')")
        c.execute("INSERT INTO observations (id, company_id, signal_code, collector, value_num, value_text, evidence_url) "
                  "VALUES ('o1', 'c1', 'SITEWATCH_ISSUES', 'sitewatch', 2, 'Hidden links', 'https://sitewatch.example/report/a')")
        c.execute("INSERT INTO observations (id, company_id, signal_code, collector, value_num, value_text) "
                  "VALUES ('o2', 'c1', 'SITEWATCH_STATUS', 'sitewatch', 48, 'critical')")
    d = page.get("/api/sitewatch").json()
    assert d["pending"] == 1
    done = next(s for s in d["scans"] if s["company"] == "Plumbing Co")
    assert done["status"] == "critical" and done["issues"] == 2
    assert done["report_url"] == "https://sitewatch.example/report/a"
    assert done["ui_issues"] is None and done["seo_issues"] is None     # no UI/SEO report yet


def test_scan_list_and_leads_show_the_ui_and_seo_reports(page):
    page.get("/api/signals")
    with closing(sqlite3.connect(page.db_path)) as c, c:
        c.execute("INSERT INTO companies (id, name, domain) VALUES ('c1', 'Plumbing Co', 'plumbing.example')")
        c.execute("INSERT INTO prospects (id, first_name, company, company_id, email) "
                  "VALUES ('p1', 'Ana', 'Plumbing Co', 'c1', 'ana@plumbing.example')")
        c.execute("INSERT INTO sitewatch_scans (company_id, scan_id, state) VALUES ('c1', 7, 'done')")
        rows = [("o1", "SITEWATCH_ISSUES", 0, "", "https://sitewatch.example/report/a"),
                ("o2", "SITEWATCH_STATUS", 0, "clean", "https://sitewatch.example/report/a"),
                ("o3", "SITEWATCH_UI_ISSUES", 2, "Not set up for phones", "https://sitewatch.example/report/a?tab=ui"),
                ("o4", "SITEWATCH_SEO_ISSUES", 1, "No description", "https://sitewatch.example/report/a?tab=seo")]
        c.executemany("INSERT INTO observations (id, company_id, signal_code, collector, value_num, value_text, evidence_url) "
                      "VALUES (?, 'c1', ?, 'sitewatch', ?, ?, ?)", rows)
    done = page.get("/api/sitewatch").json()["scans"][0]
    assert done["ui_issues"] == 2 and done["ui_text"] == "Not set up for phones"
    assert done["ui_report_url"].endswith("?tab=ui") and done["seo_report_url"].endswith("?tab=seo")
    assert done["report_url"] == "https://sitewatch.example/report/a"   # the security link stays the security link

    lead = next(l for l in page.get("/api/leads/found").json()["leads"] if l["id"] == "p1")
    assert lead["report"]["status"] == "clean" and lead["report"]["issues"] == 0
    assert lead["report"]["ui_issues"] == 2 and lead["report"]["seo_report_url"].endswith("?tab=seo")
    assert lead["report"]["report_url"] == "https://sitewatch.example/report/a"


def test_the_sidebar_has_the_page(page):
    html = page.get("/").text
    assert "showTab('sitewatch', this)" in html and 'id="sitewatch"' in html
