"""Email templates page: read, save and preview the operator's email rules
(writer.email_rules) without touching anything outside the Harvey folder."""

import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

import harvey.agents.writer as writer_mod
import harvey.config as config_mod
import harvey.dashboard as dash
import harvey.state as state_mod

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = "Hi {first name},\n\nWe support agencies on a white-label basis.\n\nBest,"


@pytest.fixture
def client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cfg = tmp / "harvey.local.yaml"
        shutil.copy(ROOT / "harvey.yaml", cfg)
        with cfg.open("a", encoding="utf-8") as f:
            f.write("# a comment the save must keep\n")
        monkeypatch.setattr(config_mod, "_find_config_file", lambda: str(cfg))
        monkeypatch.setattr(config_mod, "PROJECT_ROOT", tmp)
        monkeypatch.setattr(dash, "PROJECT_ROOT", tmp)
        monkeypatch.setattr(dash, "DB_PATH", tmp / "harvey.db")
        monkeypatch.setattr(dash, "WEB_DIR", ROOT / "harvey" / "web")
        dash.token_store.configure(tmp / "data" / "dashboard-token")
        token = dash.token_store.get()
        with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                        headers={"Authorization": f"Bearer {token}"}) as c:
            c.root, c.cfg = tmp, cfg
            yield c


def test_no_template_yet(client):
    d = client.get("/api/email-template").json()
    assert d["path"] == "skills/email_rules.md"
    assert d["content"] == "" and d["active"] is False


def test_save_writes_the_file_and_turns_it_on(client):
    r = client.post("/api/email-template", json={"content": TEMPLATE + "\r\n\r\n"})
    assert r.json()["success"]
    assert (client.root / "skills" / "email_rules.md").read_text(encoding="utf-8") == TEMPLATE + "\n"
    text = client.cfg.read_text(encoding="utf-8")
    assert yaml.safe_load(text)["writer"]["email_rules"] == "skills/email_rules.md"
    assert "# a comment the save must keep" in text  # only that line was added
    d = client.get("/api/email-template").json()
    assert d["active"] is True and d["content"] == TEMPLATE + "\n"
    # The Writer can load what was saved.
    assert config_mod.load_email_rules(config_mod.load_config()) == TEMPLATE


@pytest.mark.parametrize("content", ["", "   ", None, 42, "x" * 20_001])
def test_bad_templates_are_refused(client, content):
    r = client.post("/api/email-template", json={"content": content})
    assert r.status_code == 400 and not r.json()["success"]
    assert not (client.root / "skills" / "email_rules.md").exists()


def test_never_writes_outside_the_harvey_folder(client):
    data = yaml.safe_load(client.cfg.read_text(encoding="utf-8"))
    data["writer"] = {"email_rules": "../outside.md"}
    client.cfg.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert client.get("/api/email-template").status_code == 400
    r = client.post("/api/email-template", json={"content": TEMPLATE})
    assert r.status_code == 400
    assert not (client.root.parent / "outside.md").exists()


def test_preview_drafts_from_the_text_on_screen(client, monkeypatch):
    seen = {}
    lead = SimpleNamespace(id="p1", company="Acme Studio", email_status="guess",
                           full_name=lambda: "Jane Doe")

    async def fake_get(self, pid):
        return lead if pid == "p1" else None

    async def fake_write(self, prospect):
        seen["rules"], seen["prospect"] = self.email_rules, prospect
        return {"subject": "quick intro", "body": "Hi Jane,"}

    monkeypatch.setattr(state_mod.StateManager, "get_prospect", fake_get)
    monkeypatch.setattr(writer_mod.Writer, "_write_personal_email", fake_write)
    r = client.post("/api/email-template/preview", json={"content": TEMPLATE, "prospect_id": "p1"})
    body = r.json()
    assert body["success"] and body["subject"] == "quick intro"
    assert body["to"] == {"name": "Jane Doe", "company": "Acme Studio", "email_status": "guess"}
    assert seen["rules"] == TEMPLATE and seen["prospect"] is lead
    # Unsaved text is only previewed: nothing was written.
    assert not (client.root / "skills" / "email_rules.md").exists()


def test_preview_without_leads_says_so(client):
    r = client.post("/api/email-template/preview", json={"content": TEMPLATE})
    assert r.status_code == 400 and "leads" in r.json()["message"]


# ── The editor: three emails in, Writer rules out ──

from harvey import email_template  # noqa: E402

EDITOR = {"mode": "exact", "emails": [
    {"subject": "quick intro", "body": "Hi {first name},\n\nWe support {agency name}.\n\nBest,\n{sender name}"},
    {"subject": "", "body": "Hi,\n\nFollowing up.\n\nBest,\n{sender name}"},
    {"subject": "", "body": ""},
]}


def test_render_exact_keeps_the_text_and_explains_placeholders():
    rules = email_template.render(EDITOR)
    assert "Hi {first name},\n\nWe support {agency name}." in rules
    assert "EXACTLY as written" in rules
    assert "`{first name}`" in rules and "`{agency name}`" in rules and "`{sender name}`" in rules
    assert 'Subject: exactly "quick intro".' in rules
    # An empty follow-up is left to the model, not sent blank.
    assert "Write a short last follow-up" in rules


def test_render_adapt_allows_small_changes_only():
    rules = email_template.render({**EDITOR, "mode": "adapt"})
    assert "may change a few words" in rules and "EXACTLY" not in rules


@pytest.mark.parametrize("bad", [
    None, {"mode": "wild", "emails": EDITOR["emails"]}, {"emails": []}, {"emails": EDITOR["emails"] * 2},
    {"emails": [{"subject": "", "body": ""}] * 3},
    {"emails": [{"subject": "x" * 121, "body": "Hi"}, {}, {}]},
    {"emails": [{"subject": "", "body": "```\nsneaky"}, {}, {}]},
])
def test_editor_refuses_bad_input(bad):
    with pytest.raises(email_template.TemplateError):
        email_template.clean(bad)


def test_default_is_a_valid_template():
    assert email_template.render(email_template.DEFAULT)


def test_saving_the_editor_writes_rules_and_reloads(client):
    r = client.post("/api/email-template", json={"editor": EDITOR})
    assert r.json()["success"]
    saved = client.root / "skills" / "email_template.json"
    assert json.loads(saved.read_text(encoding="utf-8"))["emails"][0]["subject"] == "quick intro"
    d = client.get("/api/email-template").json()
    assert d["editor_in_sync"] is True and d["editor"]["emails"][0]["subject"] == "quick intro"
    assert config_mod.load_email_rules(config_mod.load_config()) == email_template.render(EDITOR).strip()


def test_raw_rules_take_over_from_the_editor(client):
    client.post("/api/email-template", json={"editor": EDITOR})
    client.post("/api/email-template", json={"content": "Write a short plain intro."})
    d = client.get("/api/email-template").json()
    assert d["editor_in_sync"] is False  # the page says the editor isn't in charge
    assert d["content"] == "Write a short plain intro.\n"


def test_editor_starts_from_the_default_when_nothing_is_saved(client):
    d = client.get("/api/email-template").json()
    assert d["editor"] == email_template.DEFAULT and d["editor_in_sync"] is False


def test_preview_uses_the_editor_on_screen(client, monkeypatch):
    seen = {}
    lead = SimpleNamespace(id="p1", company="Acme Studio", email_status="verified",
                           full_name=lambda: "Jane Doe")

    async def fake_get(self, pid):
        return lead

    async def fake_write(self, prospect):
        seen["rules"] = self.email_rules
        return {"subject": "quick intro", "body": "Hi Jane,"}

    monkeypatch.setattr(state_mod.StateManager, "get_prospect", fake_get)
    monkeypatch.setattr(writer_mod.Writer, "_write_personal_email", fake_write)
    r = client.post("/api/email-template/preview", json={"editor": EDITOR, "prospect_id": "p1"})
    assert r.json()["success"]
    assert seen["rules"] == email_template.render(EDITOR).strip()
    assert not (client.root / "skills" / "email_template.json").exists()  # preview saves nothing


# ── One email, no follow-ups ──


def test_one_email_template_says_there_are_no_follow_ups():
    rules = email_template.render(email_template.DEFAULT)
    assert len(email_template.DEFAULT["emails"]) == 1
    assert "no follow-ups" in rules and "## The email" in rules and "Email 2" not in rules


@pytest.mark.parametrize("value", [-1, 3, "two", True, 1.5])
def test_follow_ups_must_be_0_1_or_2(value):
    with pytest.raises(Exception):
        config_mod.WriterConfig(follow_ups=value)


@pytest.mark.parametrize("follow_ups,expected", [(0, 1), (1, 2), (2, 3)])
@pytest.mark.asyncio
async def test_writer_writes_only_as_many_emails_as_configured(follow_ups, expected, monkeypatch):
    prompts = []

    async def think_json(prompt, **kw):
        prompts.append(prompt)
        # A model that ignores the instruction and returns three anyway.
        return [{"step": i + 1, "subject": "hi", "body": "Hello {{first_name}}", "delay_days": 0 if i == 0 else 3}
                for i in range(3)]

    async def no_humanize(brain, drafts, **kw):
        return drafts

    brain = SimpleNamespace(load_prompt=lambda *a, **k: "PROMPT", think_json=think_json, last_reply_empty=False)
    cfg = SimpleNamespace(writer=SimpleNamespace(follow_ups=follow_ups, humanizer=False),
                          persona=SimpleNamespace(name="Sam", company="Acme", role="Founder", tone="plain"),
                          product=SimpleNamespace(name="P", description="D", key_benefits=["b"], pricing="x"),
                          compliance=SimpleNamespace(postal_address="", opt_out_line=""))
    w = writer_mod.Writer.__new__(writer_mod.Writer)
    w.brain, w.config, w.skills, w.email_rules = brain, cfg, "", "Use this text exactly."

    async def no_feedback(ids):
        return ""

    async def no_startup(cid):
        return None

    w._feedback_block, w._startup = no_feedback, no_startup
    monkeypatch.setattr(writer_mod, "humanize", no_humanize)
    lead = SimpleNamespace(full_name=lambda: "Jane Doe", title="Founder", company="Acme Studio",
                           personalization_notes="", is_company_inbox=False, company_id="c1")
    steps = await w._write_sequence([lead])
    assert len(steps) == expected
    if expected == 1:
        assert "ONE cold outreach email (no follow-ups)" in prompts[0]
        assert '"step": 2' not in prompts[0]
