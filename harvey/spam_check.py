"""What in a cold email's wording looks like bulk or spam mail to a filter.

The pre-send gate (harvey/gate.py) blocks the few things that must never go
out. This check is softer: it names what makes an email look like marketing
mail, so the operator can fix the template before Harvey sends it to every
lead. Nothing here stops an email; whether one lands in the inbox also
depends on the sending account's age and reputation, which no wording fixes.

Each finding is {"level": "high" | "medium" | "low", "issue": ..., "fix": ...}.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# Words and phrases spam filters weigh heavily in cold mail.
HIGH_PHRASES = (
    "100%", "foc", "free of cost", "no obligation", "risk-free", "risk free", "act now",
    "limited time", "special offer", "exclusive offer", "click here", "guaranteed",
    "100% free", "free trial", "earn money", "cash bonus",
)
# Marketing wording that reads like a brochure, not a person.
JARGON = (
    "digital ecosystem", "actionable insights", "digital transformation",
    "opportunities for growth", "unlock", "supercharge", "synergy", "cutting-edge",
    "game-changer", "game changer", "take your business to the next level",
    "boost your", "skyrocket",
)
FREE_WORDS = ("complimentary", "free")
MAX_WORDS = 100
MAX_BULLETS = 3
MAX_SUBJECT_CHARS = 60
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_BULLET_RE = re.compile(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+\S")
_WORD_RE = re.compile(r"[A-Za-z0-9'%]+")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{24,}")
# "staging" anywhere in a host name reads as a test server ("wp-stagingwork");
# the shorter words only as their own part of the name ("dev.", "-test.").
_TEST_HOST_RE = re.compile(r"staging|(?:^|[.-])(?:stage|test|dev|demo|sandbox|localhost)(?:[.-]|$)")
_IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _find(text: str, phrases) -> list[str]:
    low = (text or "").lower()
    found = []
    for p in phrases:
        if p.isalpha():
            hit = re.search(r"\b" + re.escape(p) + r"\b", low)
        else:
            hit = p in low
        if hit:
            found.append(p)
    return found


def _count(text: str, word: str) -> int:
    return len(re.findall(r"\b" + re.escape(word) + r"\b", (text or "").lower()))


def spam_signals(subject: str, body: str) -> list[dict]:
    """Everything in this email's wording that makes it look like bulk mail."""
    subject, body = subject or "", body or ""
    out: list[dict] = []

    def add(level, issue, fix):
        out.append({"level": level, "issue": issue, "fix": fix})

    # Subject
    if re.search(r"[\[\]{}]", re.sub(r"\{[a-z ]+\}", "", subject)):   # a {placeholder} isn't a bracket
        add("medium", "The subject has brackets, which read like a newsletter or an automated email.",
            "Write it like a person would, e.g. \"Website audit report for {company}\".")
    if "!" in subject:
        add("medium", "The subject has an exclamation mark.", "Drop it.")
    caps = [w for w in _WORD_RE.findall(subject) if len(w) > 3 and w.isupper()]
    if caps:
        add("medium", f"The subject shouts in capitals ({', '.join(caps)}).", "Use normal case.")
    if len(subject) > MAX_SUBJECT_CHARS:
        add("low", f"The subject is long ({len(subject)} characters).",
            f"Keep it under {MAX_SUBJECT_CHARS}: short subjects look personal.")
    for p in _find(subject, HIGH_PHRASES + FREE_WORDS):
        add("high", f"The subject says \"{p}\", a word spam filters weigh heavily.",
            "Say what the email is about instead (a report, a question).")

    # Body wording
    text = f"{subject}\n{body}"
    for p in _find(body, HIGH_PHRASES):
        add("high", f"\"{p}\" is a classic spam phrase.", "Remove it or say it in plain words.")
    free = sum(_count(text, w) for w in FREE_WORDS)
    if free > 1:
        add("medium", f"\"Free\"/\"complimentary\" appears {free} times.", "Say it once, or not at all.")
    for p in _find(body, JARGON):
        add("medium", f"\"{p}\" is marketing jargon.", "Say what you found or what you'd fix instead.")
    words = len(_WORD_RE.findall(body))
    if words > MAX_WORDS:
        add("medium", f"The email is {words} words long.",
            f"Cold emails that land in the inbox are short: aim for under {MAX_WORDS}.")
    bullets = len(_BULLET_RE.findall(body))
    if bullets > MAX_BULLETS:
        add("medium", f"It lists {bullets} bullet points, which reads like a brochure.",
            "Name the one or two that matter in a sentence.")
    if body.count("!") > 1:
        add("low", "It uses several exclamation marks.", "One at most.")
    shouting = [w for w in _WORD_RE.findall(body) if len(w) > 4 and w.isupper()]
    if shouting:
        add("low", f"Words in capitals ({', '.join(shouting[:3])}).", "Use normal case.")

    # Links
    links = _URL_RE.findall(body)
    if len(links) > 1:
        add("high", f"It has {len(links)} links.", "Keep one link at most in a first email.")
    for link in links:
        parsed = urlparse(link.rstrip(".,);"))
        host = (parsed.hostname or "").lower()
        if _IP_RE.match(host) or _TEST_HOST_RE.search(host):
            add("high", f"The link goes to {host}, which looks like a test or staging server.",
                "Link to a page on your own main domain (e.g. reports.yourcompany.com).")
        if _TOKEN_RE.search(parsed.path or ""):
            add("low", "The link ends in a long random code, which phishing links also have.",
                "A shorter, readable link looks safer; or offer the report in a reply.")
    return out


def summary(findings: list[dict]) -> str:
    """One line for a toast: how many high and medium findings."""
    high = sum(1 for f in findings if f["level"] == "high")
    medium = sum(1 for f in findings if f["level"] == "medium")
    if not high and not medium:
        return "Nothing in the wording looks like spam."
    parts = [f"{high} serious" if high else "", f"{medium} to look at" if medium else ""]
    return "Spam check: " + ", ".join(p for p in parts if p) + "."
