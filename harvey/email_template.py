"""The dashboard's email editor: three emails in, Writer rules out.

The operator writes the intro and two follow-ups as plain text with a few
placeholders. That is saved as JSON (so the editor can load it again) and
rendered into the markdown rules file the Writer already reads
(writer.email_rules), so drafting and sending don't change at all.
"""
from __future__ import annotations

import json

PLACEHOLDERS = {
    "{first name}": "the person's first name. With no first name (a team inbox), "
                    "drop it and the space or comma before it, e.g. \"Hi {first name},\" becomes \"Hi,\".",
    "{agency name}": "their agency's name as its own website writes it. If unknown, write \"your agency\".",
    "{sender name}": "the sender's first name.",
    "{website issues}": "one or two problems the FACTS list on their website, in plain words, as "
                        "something a scan flagged. If the FACTS list none, drop the whole sentence "
                        "it's in.",
    "{report link}": "the website report link from the FACTS, as plain text. If there is none, "
                     "drop the whole sentence it's in.",
}
MODES = ("exact", "adapt")
STEPS = (
    ("Email 1 (the intro)", "first email"),
    ("Email 2 (follow-up, a few days later)", "first follow-up"),
    ("Email 3 (last note)", "last follow-up"),
)
MAX_SUBJECT = 120
MAX_BODY = 3000

# One email by default: the dashboard edits a single template, and
# writer.follow_ups: 0 keeps Harvey from writing any more.
DEFAULT = {
    "mode": "exact",
    "emails": [
        {"subject": "", "body": (
            "Hi,\n\nI came across your agency and wanted to introduce ourselves.\n\n"
            "We support agencies with web, marketing, and digital projects on a white-label basis, "
            "with all work delivered under your brand.\n\n"
            "If it's relevant, could I send our sample work and pricing for your review?\n\n"
            "Best,\n{sender name}")},
    ],
}


class TemplateError(ValueError):
    """The editor sent something that can't become email rules."""


def clean(data) -> dict:
    """Validate what the editor sent. Raises TemplateError with a plain message."""
    if not isinstance(data, dict):
        raise TemplateError("The template must be an object with emails.")
    mode = data.get("mode") or "exact"
    if mode not in MODES:
        raise TemplateError("Mode must be 'exact' or 'adapt'.")
    emails = data.get("emails")
    if not isinstance(emails, list) or not 1 <= len(emails) <= len(STEPS):
        raise TemplateError(f"Send 1 to {len(STEPS)} emails: the intro, then any follow-ups.")
    out = []
    for i, e in enumerate(emails):
        e = e if isinstance(e, dict) else {}
        subject, body = e.get("subject", ""), e.get("body", "")
        if not isinstance(subject, str) or not isinstance(body, str):
            raise TemplateError(f"{STEPS[i][0]}: subject and message must be text.")
        subject = " ".join(subject.split())
        body = body.replace("\r\n", "\n").replace("\x00", "").strip()
        if i == 0 and not body:
            raise TemplateError("The intro email can't be empty.")
        if len(subject) > MAX_SUBJECT:
            raise TemplateError(f"{STEPS[i][0]}: the subject is over {MAX_SUBJECT} characters.")
        if len(body) > MAX_BODY:
            raise TemplateError(f"{STEPS[i][0]}: the message is over {MAX_BODY:,} characters.")
        if "```" in body or "```" in subject:
            raise TemplateError(f"{STEPS[i][0]}: remove the ``` marks.")
        out.append({"subject": subject, "body": body})
    return {"mode": mode, "emails": out}


def render(template: dict) -> str:
    """The Writer's rules (markdown) for a cleaned template."""
    t = clean(template)
    exact = t["mode"] == "exact"
    used = [p for p in PLACEHOLDERS if any(p in e["body"] or p in e["subject"] for e in t["emails"])]
    lines = [
        "# Email rules (written in the dashboard's email editor)",
        "",
        "These replace the built-in email shape.",
        ("Use each email's text EXACTLY as written below, word for word, with the same "
         "line breaks. Only replace the placeholders. Add nothing: no names, compliments, "
         "services, links or claims that aren't in the text.") if exact else
        ("Follow each email below closely: keep its message, order, tone and length. You "
         "may change a few words so it reads naturally for this agency, using only the "
         "FACTS. Don't add claims, services, numbers or links that aren't in the text."),
        "",
    ]
    if used:
        lines += ["Placeholders:"] + [f"- `{p}`: {PLACEHOLDERS[p]}" for p in used] + [""]
    if len(t["emails"]) == 1:
        lines += ["This is the only email: there are no follow-ups.", ""]
    for (title, what), e in zip(STEPS, t["emails"]):
        lines += [f"## {'The email' if len(t['emails']) == 1 else title}", ""]
        if e["body"]:
            lines += ["```", e["body"], "```", ""]
        else:
            lines += [f"Write a short {what}: two or three plain sentences in the same tone as the "
                      "intro, same thread, no pressure, and make it easy to say no.", ""]
        if e["subject"]:
            lines += [f"Subject: exactly \"{e['subject']}\"" + (" (fill in placeholders)." if "{" in e["subject"] else "."), ""]
        else:
            lines += ["Subject: short and plain, lowercase, 2 to 5 words.", ""]
    lines += [
        "## Always",
        "",
        "- Never invent clients, numbers, awards, turnaround times or guarantees.",
        "- No postal address, unsubscribe or opt-out line: Harvey adds them when it sends.",
    ]
    return "\n".join(lines).strip() + "\n"


def dumps(template: dict) -> str:
    return json.dumps(clean(template), indent=2, ensure_ascii=False) + "\n"
