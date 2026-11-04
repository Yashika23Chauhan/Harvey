"""Checks for this machine's Harvey install, plus the safety rules it relies on.

Two groups:

- Local setup (marked ``local_setup``): the config file Harvey reads, the
  installed Claude CLI and the database. Skipped unless HARVEY_LOCAL_TESTS=1,
  since they read this checkout's real files.
- Behaviour: the send gate, do-not-contact list, opt-out detection, the
  heartbeat's decision and cohorts. Pure unit tests on a temporary database;
  no network, no model call.

Run everything:  HARVEY_LOCAL_TESTS=1 pytest tests/test_local_setup.py -v
"""

import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from harvey import claude_runtime
from harvey.agents.handler import _opt_out_reason
from harvey.config import load_config
from harvey.gate import do_not_contact_entries, do_not_contact_match, pre_send_check
from harvey.main import decide_next_action
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.signals import seed_signal_catalog
from harvey.state import StateManager

ROOT = Path(__file__).resolve().parent.parent
LOCAL = os.environ.get("HARVEY_LOCAL_TESTS") == "1"
local_setup = pytest.mark.skipif(not LOCAL, reason="set HARVEY_LOCAL_TESTS=1 to check this install")

ADDRESS = "100 Example Street, Test City, AZ 85000"
GOOD_BODY = ("Hi Gary, noticed customers can only book by phone on your site. "
             "Do after-hours calls slip through?\n\nSam\n\n" + ADDRESS)


def prospect(email="gary@plumbing.example", status="verified"):
    return Prospect(first_name="Gary", last_name="Doe", title="Owner",
                    email=email, email_status=status)


def gate(body=GOOD_BODY, subject="Booking after hours", **kw):
    kw.setdefault("prospect", prospect())
    kw.setdefault("postal_address", ADDRESS)
    return pre_send_check(kw["prospect"].email if kw["prospect"] else "gary@plumbing.example",
                          subject, body, **kw)


# ── Local setup ──────────────────────────────────────────────────────────

@local_setup
def test_python_is_new_enough():
    assert sys.version_info >= (3, 11)


@local_setup
def test_config_file_loads_with_real_values():
    path = ROOT / "harvey.local.yaml"
    if not path.exists():
        path = ROOT / "harvey.yaml"
    config = load_config(str(path))
    assert config.product.name and config.product.name != "Your Product"
    assert config.persona.company != "Your Company"
    assert config.icp.industries and config.icp.geography
    assert config.channels.email.require_approval, "autopilot is on: approval mode expected"


@local_setup
def test_postal_address_is_set():
    config = load_config(str(ROOT / "harvey.local.yaml"))
    assert config.compliance.postal_address.strip(), "CAN-SPAM needs compliance.postal_address"


@local_setup
@pytest.mark.real_claude_cli
def test_installed_claude_cli_has_every_isolation_flag():
    exe = claude_runtime.check_cli()  # raises with the reason if a flag is missing
    assert Path(exe).name.lower().startswith("claude")


@local_setup
def test_database_has_confirmed_signals_and_companies():
    db_path = ROOT / "data" / "harvey.db"
    assert db_path.exists(), "no database yet: run 'harvey discover' first"
    db = sqlite3.connect(db_path)
    try:
        confirmed = db.execute(
            "SELECT COUNT(*) FROM signal_codes WHERE status = 'confirmed'").fetchone()[0]
        companies = db.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    finally:
        db.close()
    assert confirmed > 0, "no signals confirmed: run 'harvey signals --confirm free'"
    assert companies > 0, "no companies found yet: run 'harvey discover'"


# ── Claude calls stay isolated ───────────────────────────────────────────

def test_claude_command_has_no_tools_and_no_user_setup():
    cmd = claude_runtime.build_command("claude")
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--tools") + 1] == ""
    for flag in claude_runtime.REQUIRED_FLAGS:
        assert flag in cmd, flag
    assert "--dangerously-skip-permissions" not in cmd


def test_cli_missing_a_flag_is_detected():
    help_text = "\n".join(f for f in claude_runtime.REQUIRED_FLAGS if f != "--safe-mode")
    assert claude_runtime.missing_flags(help_text) == ["--safe-mode"]


# ── Pre-send gate ────────────────────────────────────────────────────────

def test_clean_cold_email_passes():
    result = gate()
    assert result.ok, result.reasons


@pytest.mark.parametrize("body, reason", [
    ("Hi {{first_name}}, quick question.\n\n" + ADDRESS, "unrendered merge tags"),
    ("I hope this finds you well. Quick question.\n\n" + ADDRESS, "banned phrases"),
    ("See https://a.example and https://b.example\n\n" + ADDRESS, "too many links"),
    ("<div>Hi Gary</div>\n\n" + ADDRESS, "HTML markup"),
    ("word " * 230 + ADDRESS, "body too long"),
    ("Hi Gary, quick question about booking.", "postal address footer missing"),
    ("", "empty body"),
])
def test_bad_cold_email_is_blocked(body, reason):
    result = gate(body=body)
    assert not result.ok
    assert any(reason in r for r in result.reasons), result.reasons


def test_subject_too_long_is_blocked():
    result = gate(subject="x" * 91)
    assert any("subject too long" in r for r in result.reasons)


@pytest.mark.parametrize("status", ["guess", "invalid", ""])
def test_cold_email_to_unverified_address_is_blocked(status):
    result = gate(prospect=prospect(status=status))
    assert any("not deliverable" in r for r in result.reasons)


def test_catch_all_address_needs_send_to_risky():
    assert not gate(prospect=prospect(status="risky")).ok
    assert gate(prospect=prospect(status="risky"), allow_risky=True).ok


def test_reply_to_guessed_address_is_allowed():
    # They emailed us, so the address is proven; replies skip the footer too.
    result = gate(body="Thanks Gary, Tuesday at 10 works.", kind="reply",
                  prospect=prospect(status="guess"))
    assert result.ok, result.reasons


def test_recipient_must_match_prospect_record():
    result = pre_send_check("someone.else@plumbing.example", "Hi", GOOD_BODY,
                            prospect=prospect(), postal_address=ADDRESS)
    assert any("does not match" in r for r in result.reasons)


# ── Do-not-contact list ──────────────────────────────────────────────────

def test_do_not_contact_domain_covers_subdomains_only():
    entries = do_not_contact_entries("competitor.example")
    assert do_not_contact_match("a@competitor.example", entries)
    assert do_not_contact_match("a@mail.competitor.example", entries)
    assert not do_not_contact_match("a@notcompetitor.example", entries)


def test_do_not_contact_address_covers_plus_tags():
    entries = do_not_contact_entries("Jane Doe <jane@acme.example>")
    assert do_not_contact_match("jane+news@acme.example", entries)
    assert not do_not_contact_match("john@acme.example", entries)


def test_pasted_link_counts_as_its_domain():
    assert do_not_contact_entries("https://www.acme.example/about") == ["acme.example"]


def test_junk_do_not_contact_entry_is_refused():
    with pytest.raises(ValueError):
        do_not_contact_entries("not a domain!")


def test_gate_blocks_do_not_contact_even_for_replies():
    result = pre_send_check("jane@acme.example", "Re: hi", "Thanks!", kind="reply",
                            do_not_contact=["acme.example"])
    assert any("do-not-contact" in r for r in result.reasons)


# ── Opt-out detection (no model) ─────────────────────────────────────────

@pytest.mark.parametrize("reply", [
    "STOP", "No thanks", "Please unsubscribe me", "remove me from your list",
    "BAJA", "הסר",
])
def test_opt_out_is_detected(reply):
    assert _opt_out_reason(reply, reply), reply


@pytest.mark.parametrize("reply", [
    "Sounds interesting, what does it cost?",
    "Can we talk Tuesday?",
])
def test_ordinary_reply_is_not_an_opt_out(reply):
    assert _opt_out_reason(reply, reply) is None


# ── Heartbeat decision and cohorts ───────────────────────────────────────

CONFIG = SimpleNamespace(channels=SimpleNamespace(email=SimpleNamespace(
    provider="smtp", require_approval=True, enabled=True)))


@pytest_asyncio.fixture
async def state(tmp_path):
    state = StateManager(str(tmp_path / "local.db"))
    await state.init_db()
    return state


@pytest.mark.asyncio
async def test_empty_pipeline_prospects(state):
    assert await decide_next_action(None, state, CONFIG) == "prospect"


@pytest.mark.asyncio
async def test_verified_prospect_gets_written_for(state):
    await state.add_prospect(prospect())
    assert await decide_next_action(None, state, CONFIG) == "write_campaign"


@pytest.mark.asyncio
async def test_guessed_prospect_does_not_trigger_writing(state):
    # What this install hit: a guessed address must send Harvey back to Scout.
    await state.add_prospect(prospect(status="guess"))
    assert await decide_next_action(None, state, CONFIG) == "prospect"


@pytest.mark.asyncio
async def test_cohort_requires_and_excludes_signals(state):
    await seed_signal_catalog(state)
    for code in ("RUNNING_GOOGLE_ADS", "NO_ONLINE_BOOKING"):
        await state.set_signal_status(code, "confirmed")
    ads_no_booking = await state.add_company(Company(name="A", domain="a.example"))
    ads_only = await state.add_company(Company(name="B", domain="b.example"))
    await state.add_observation("RUNNING_GOOGLE_ADS", company_id=ads_no_booking, value_num=1)
    await state.add_observation("NO_ONLINE_BOOKING", company_id=ads_no_booking, value_num=1)
    await state.add_observation("RUNNING_GOOGLE_ADS", company_id=ads_only, value_num=1)

    assert await state.cohort(["RUNNING_GOOGLE_ADS", "NO_ONLINE_BOOKING"]) == [ads_no_booking]
    assert await state.cohort(["RUNNING_GOOGLE_ADS"], exclude=["NO_ONLINE_BOOKING"]) == [ads_only]


@pytest.mark.asyncio
async def test_unknown_signal_code_fails_loudly(state):
    await seed_signal_catalog(state)
    with pytest.raises(sqlite3.IntegrityError):
        await state.add_observation("NOT_A_SIGNAL", company_id="x")
