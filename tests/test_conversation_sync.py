"""Conversations show both sides, live.

Replies you write from Gmail or Outlook (the Sent folder) join the thread
they answer, Harvey's own sends aren't added twice, and the Conversations
badge counts threads where the customer wrote last.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

import harvey.dashboard as dash
from harvey.agents.handler import Handler
from harvey.integrations.mail_provider import InboundMessage
from harvey.integrations.smtp_mail import SmtpImapProvider
from harvey.models.conversation import Conversation, Message
from harvey.models.prospect import Prospect
from harvey.state import StateManager


class FakeConn:
    def __init__(self, rows):
        self.rows = rows

    def list(self):
        return "OK", [r.encode() for r in self.rows]


@pytest.mark.parametrize("rows, expected", [
    (['(\\HasNoChildren) "/" "INBOX"', '(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"'],
     '"[Gmail]/Sent Mail"'),
    (['(\\HasNoChildren) "/" "Inbox"', '(\\HasNoChildren) "/" "Sent Items"'], '"Sent Items"'),
    (['(\\HasNoChildren) "/" "INBOX"'], None),
])
def test_sent_folder_is_found(rows, expected):
    assert SmtpImapProvider._sent_mailbox(FakeConn(rows)) == expected


def test_parsed_message_carries_its_recipient():
    raw = (b"From: Me <me@mine.example>\r\nTo: Sam <sam@shop.example>\r\n"
           b"Subject: Re: hi\r\nMessage-ID: <m1@x>\r\n\r\nSure, Tuesday works.\r\n")
    msg = SmtpImapProvider._parse_rfc822(raw)
    assert msg.to_email == "sam@shop.example" and msg.from_email == "me@mine.example"


class FakeMailbox:
    """IMAP inbox: {uid: (seen, raw)}. Records what was fetched."""

    def __init__(self, messages):
        self.messages, self.fetched, self.flags_changed = messages, [], False

    def __call__(self, *a, **k):
        return self

    def login(self, *a):
        pass

    def select(self, box, readonly=False):
        return "OK", [b"1"]

    def response(self, code):
        if code == "UIDVALIDITY":
            return code, [b"7"]
        return code, [str(max(self.messages) + 1).encode()]

    def uid(self, command, *args):
        if command == "SEARCH":
            criteria = args[1]
            if criteria == "UNSEEN":
                hits = [u for u, (seen, _) in self.messages.items() if not seen]
            else:
                start = int(criteria.split()[1].split(":")[0])
                hits = [u for u in self.messages if u >= start]
            return "OK", [" ".join(map(str, sorted(hits))).encode()]
        uid, what = int(args[0]), args[1]
        assert what == "(BODY.PEEK[])", "fetching must never mark mail read"
        self.fetched.append(uid)
        return "OK", [(b"x", self.messages[uid][1])]

    def logout(self):
        pass


def raw(n):
    return (f"From: c{n}@shop.example\r\nTo: me@mine.example\r\nSubject: s{n}\r\n"
            f"Message-ID: <in{n}@x>\r\n\r\nhello {n}\r\n").encode()


def test_inbox_reads_only_new_mail_and_never_marks_it_read(monkeypatch):
    import harvey.integrations.smtp_mail as sm
    box = FakeMailbox({1: (True, raw(1)), 2: (False, raw(2))})
    monkeypatch.setattr(sm, "_IMAP4_SSL", box)
    p = SmtpImapProvider(SimpleNamespace(), SimpleNamespace(imap_host="imap.example"))
    assert [m.message_id for m in p._fetch_unseen(50)] == ["<in2@x>"]  # first read: unread only
    p.confirm_read()
    box.messages[3] = (True, raw(3))  # arrives, and you open it in Gmail first
    box.fetched.clear()
    assert [m.message_id for m in p._fetch_unseen(50)] == ["<in3@x>"]
    assert box.fetched == [3]


def test_unhandled_read_is_read_again(monkeypatch):
    import harvey.integrations.smtp_mail as sm
    box = FakeMailbox({1: (False, raw(1))})
    monkeypatch.setattr(sm, "_IMAP4_SSL", box)
    p = SmtpImapProvider(SimpleNamespace(), SimpleNamespace(imap_host="imap.example"))
    p._fetch_unseen(50)
    p.confirm_read()
    box.messages[2] = (False, raw(2))
    assert [m.message_id for m in p._fetch_unseen(50)] == ["<in2@x>"]
    # Not confirmed (a reply couldn't be handled): the next read returns it again.
    assert [m.message_id for m in p._fetch_unseen(50)] == ["<in2@x>"]


class FakeProvider:
    name = "smtp"

    def __init__(self, sent):
        self.sent = sent

    async def get_sent(self, limit=30):
        return self.sent


def sent(message_id, to="sam@shop.example", body="Tuesday at 10 works.\n\nOn Mon, Sam wrote:\n> hi"):
    return InboundMessage(provider_id=message_id, from_email="me@mine.example", to_email=to,
                          subject="Re: hi", body=body, message_id=message_id,
                          date="Tue, 03 Nov 2026 10:00:00 +0000")


@pytest.fixture
def world(tmp_path):
    async def build():
        state = StateManager(str(tmp_path / "h.db"))
        await state.init_db()
        pid = await state.add_prospect(Prospect(first_name="Sam", last_name="Doe", title="Owner",
                                                email="sam@shop.example", email_status="verified"))
        await state.add_conversation(Conversation(
            id="c1", prospect_id=pid,
            thread=[Message(sender="prospect", content="hi", timestamp="2026-11-03T09:00:00")]))
        await state.add_outbox_item(prospect_id=pid, conversation_id="c1", kind="reply",
                                    to_email="sam@shop.example", subject="Re: hi",
                                    body="Harvey's draft", send_at="2026-11-03T09:01:00")
        return state
    return asyncio.run(build())


def run_sync(state, messages):
    h = Handler.__new__(Handler)
    h.state, h.provider = state, FakeProvider(messages)
    asyncio.run(h._sync_own_replies())
    return asyncio.run(state.get_conversation("c1"))


def test_your_reply_joins_the_thread_without_the_quote(world):
    convo = run_sync(world, [sent("<m1@x>")])
    assert [m.sender for m in convo.thread] == ["prospect", "you"]
    assert convo.thread[-1].content == "Tuesday at 10 works."


def test_your_reply_withdraws_harveys_waiting_draft(world):
    run_sync(world, [sent("<m1@x>")])
    drafts = asyncio.run(world.get_outbox(status="pending_review", kind="reply"))
    assert drafts == []


def test_the_same_sent_mail_is_added_once(world):
    run_sync(world, [sent("<m1@x>")])
    convo = run_sync(world, [sent("<m1@x>")])
    assert [m.sender for m in convo.thread] == ["prospect", "you"]


def test_mail_harvey_sent_is_not_added_again(world):
    async def mark():
        items = await world.get_outbox(status="pending_review", kind="reply")
        async with world._connect() as db:
            await db.execute("UPDATE outbox SET status='sent', message_id='<h1@x>' WHERE id=?",
                             (items[0]["id"],))
            await db.commit()
    asyncio.run(mark())
    convo = run_sync(world, [sent("<h1@x>")])
    assert [m.sender for m in convo.thread] == ["prospect"]


def test_mail_to_someone_who_isnt_a_contact_is_ignored(world):
    convo = run_sync(world, [sent("<m2@x>", to="friend@else.example")])
    assert [m.sender for m in convo.thread] == ["prospect"]


def test_a_new_message_keeps_the_reply_you_approved(world):
    async def go():
        p = await world.get_prospect_by_email("sam@shop.example")
        mine = await world.add_outbox_item(prospect_id=p.id, conversation_id="c1", kind="reply",
                                           to_email=p.email, subject="Re: hi", body="Tuesday works",
                                           send_at="2026-11-03T09:02:00", status="approved",
                                           approved_by="you")
        await world.cancel_pending_outbox_for_prospect(p.id, keep_your_replies=True)
        kept = (await world.get_outbox_item(mine))["status"]
        drafts = await world.get_outbox(status="pending_review", kind="reply")
        await world.cancel_pending_outbox_for_prospect(p.id, reason="opted_out")
        return kept, drafts, (await world.get_outbox_item(mine))["status"]
    kept, drafts, after_opt_out = asyncio.run(go())
    assert kept == "approved"           # your reply still goes
    assert drafts == []                 # Harvey's stale draft is withdrawn
    assert after_opt_out == "cancelled" # an opt-out still stops everything


def test_badge_counts_threads_where_the_customer_wrote_last(world, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(dash, "DB_PATH", Path(world.db_path))
    assert asyncio.run(dash._unanswered_conversation_ids()) == {"c1"}
    run_sync(world, [sent("<m1@x>")])
    assert asyncio.run(dash._unanswered_conversation_ids()) == set()
