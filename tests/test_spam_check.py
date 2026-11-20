"""harvey/spam_check.py: what in a cold email's wording looks like bulk mail.
Fictional companies and addresses only."""

import pytest

from harvey.spam_check import spam_signals, summary

# Shaped like a template that kept landing in spam.
BROCHURE_SUBJECT = "[Site Audit Team] complimentary digital audit"
BROCHURE_BODY = """Hi Sam,

We'd love to help you identify opportunities to improve your business's digital performance.

At Example Studio, we're offering a complimentary digital audit (100% FOC) to help you uncover gaps, improvement areas, and opportunities for growth across your digital ecosystem.

We've already run a quick audit of your website. You can view the report here: https://reports.example-staging.test/report/8wNAE9vwsASiw0ta5T2DFuh0elaJj8Tc

Based on the findings, we can also help you implement the right solutions, including:

- Website & Application Development
- Digital Marketing & SEO
- Automation & AI Solutions
- Business Process Optimisation
- Digital Transformation

Just actionable insights to help your business grow.

Would you be open to a quick conversation or a complimentary audit?

Best regards,
Team Example Studio
Digital Transformation & Technology Solutions
example.com | hello@example.com"""

PLAIN_SUBJECT = "Website audit report for Acme Plumbing"
PLAIN_BODY = """Hi Sam,

I had a quick look at Acme Plumbing's website and noted a few things that could be improved, like speed, mobile layout and SEO.

You can see the short report here: https://reports.example.com/acme-plumbing

We're a web team with 8+ years of experience helping businesses fix things like this.

Would it help if we walked you through what we found?

Best regards,
Team Example Studio"""


def _issues(findings, level=None):
    return [f["issue"] for f in findings if level is None or f["level"] == level]


def test_the_brochure_template_is_flagged_for_everything_that_sinks_it():
    found = spam_signals(BROCHURE_SUBJECT, BROCHURE_BODY)
    text = " ".join(_issues(found))
    assert "brackets" in text
    assert '"100%"' in text and '"foc"' in text
    assert "appears 3 times" in text                      # complimentary x3
    assert "digital ecosystem" in text and "actionable insights" in text
    assert "5 bullet points" in text
    assert "words long" in text
    assert "staging server" in text and "random code" in text
    assert len(_issues(found, "high")) >= 3
    assert summary(found).startswith("Spam check:") and "serious" in summary(found)


def test_a_short_plain_email_passes():
    assert spam_signals(PLAIN_SUBJECT, PLAIN_BODY) == []
    # A template's {placeholder} in the subject isn't a bracket.
    assert spam_signals("Website audit report for {company}", "Hi {first name}, a short note.") == []
    assert summary([]) == "Nothing in the wording looks like spam."


@pytest.mark.parametrize("subject,expect", [
    ("[Update] your site", "brackets"),
    ("Great news for you!", "exclamation"),
    ("URGENT website review", "capitals"),
    ("Free website audit", '"free"'),
    ("A very long subject line that keeps going well past what anyone reads in an inbox list", "long"),
])
def test_subject_signals(subject, expect):
    assert expect in " ".join(_issues(spam_signals(subject, "Hi Sam, a short note.")))


def test_one_mention_of_free_is_fine_in_the_body():
    assert spam_signals("Website audit report for Acme", "Hi Sam, the review is free. Want it?") == []
    two = spam_signals("Website audit report for Acme", "Hi Sam, a free review, complimentary too.")
    assert "appears 2 times" in " ".join(_issues(two))


def test_words_inside_other_words_dont_count():
    # "foc" in "focus" and "free" in "freelancer" are other words.
    assert spam_signals("Website audit report for Acme",
                        "Hi Sam, our focus is speed. A freelancer built the site.") == []


@pytest.mark.parametrize("link,expect", [
    ("https://staging.example.com/r/1", "staging server"),
    ("https://reports.example-stagingwork.com/r/1", "staging server"),
    ("https://example-dev.example.com/r/1", "staging server"),
    ("http://10.0.0.7/r/1", "staging server"),
    ("https://reports.example.com/r/AbCdEfGhIjKlMnOpQrStUvWxYz123", "random code"),
])
def test_link_signals(link, expect):
    assert expect in " ".join(_issues(spam_signals("Website audit report for Acme", f"Hi Sam, see {link}")))


def test_two_links_are_serious():
    body = "Hi Sam, see https://example.com/a and https://example.com/b"
    assert any("2 links" in f["issue"] and f["level"] == "high"
               for f in spam_signals("Website audit report for Acme", body))


def test_a_readable_link_on_your_own_domain_is_fine():
    assert spam_signals("Website audit report for Acme",
                        "Hi Sam, the report: https://reports.example.com/acme-plumbing") == []
