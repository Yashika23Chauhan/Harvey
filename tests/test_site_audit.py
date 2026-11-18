"""Website problems Harvey can name in an email: each must be true of the page."""
from datetime import date

import httpx
import pytest

from harvey.collectors import site_audit as sa

MODERN = """<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Family dentist in Austin.">
<meta name="generator" content="WordPress 6.6.2">
</head><body><footer>© 2025 Smile Studio</footer></body></html>"""

NEGLECTED = """<html><head><title>Joe's Plumbing</title>
<meta name="generator" content="WordPress 4.9.8">
<link rel="stylesheet" href="/wp-includes/css/dist/block-library/style.min.css?ver=4.9.8">
</head><body><p>Call us!</p><footer>Copyright &copy; 2012 - 2019 Joe's Plumbing</footer></body></html>"""


def test_https():
    assert sa.is_insecure("http://plumbing.example")
    assert not sa.is_insecure("https://plumbing.example")
    assert not sa.is_insecure("")


def test_mobile_viewport():
    assert not sa.lacks_mobile_viewport(MODERN)
    assert sa.lacks_mobile_viewport(NEGLECTED)
    assert not sa.lacks_mobile_viewport("<meta content='width=device-width' name='viewport'>")


def test_meta_description():
    assert not sa.lacks_meta_description(MODERN)
    assert sa.lacks_meta_description(NEGLECTED)
    assert sa.lacks_meta_description('<meta name="description" content="   ">')
    # og:description isn't the meta description Google shows.
    assert sa.lacks_meta_description('<meta property="og:description" content="Hi">')


def test_copyright_year_reads_ranges_by_their_end():
    assert sa.copyright_year(NEGLECTED) == 2019
    assert sa.copyright_year(MODERN) == 2025
    assert sa.copyright_year("<p>No notice here. Founded 1998.</p>") is None
    # A year in a script isn't on the page.
    assert sa.copyright_year("<script>var c='© 2010'</script><p>© 2024</p>") == 2024


def test_outdated_copyright_allows_last_year():
    today = date(2026, 1, 15)
    assert sa.outdated_copyright(NEGLECTED, today) == 2019
    assert sa.outdated_copyright("<p>© 2025</p>", today) is None   # normal in January
    assert sa.outdated_copyright("<p>© 2024</p>", today) == 2024
    assert sa.outdated_copyright("<p>nothing</p>", today) is None


def test_wordpress_version():
    assert sa.wordpress_version(MODERN) == (6, 6, 2)
    assert sa.wordpress_version(NEGLECTED) == (4, 9, 8)
    assert sa.wordpress_version('<script src="/wp-includes/js/wp-emoji-release.min.js?ver=5.8.1">') == (5, 8, 1)
    # Libraries WordPress bundles carry their own version, not WordPress's
    assert sa.wordpress_version('<script src="/wp-includes/js/jquery/jquery.min.js?ver=3.7.1">') is None
    assert sa.wordpress_version('<script src="/wp-includes/js/jquery/jquery-migrate.min.js?ver=3.4.1">') is None
    assert sa.old_wordpress('<script src="/wp-includes/js/jquery/jquery.min.js?ver=3.7.1">') == ""
    assert sa.wordpress_version("<p>Built on Shopify</p>") is None
    assert sa.old_wordpress(NEGLECTED) == "4.9.8"
    assert sa.old_wordpress(MODERN) == ""
    assert sa.old_wordpress("<p>no WordPress</p>") == ""


@pytest.mark.asyncio
async def test_pagespeed_score_and_failures(monkeypatch):
    replies = []

    def handler(request):
        assert request.url.params["strategy"] == "mobile"
        assert request.url.params["url"] == "https://plumbing.example"
        return replies.pop(0)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(sa.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    replies.append(httpx.Response(200, json={"lighthouseResult": {"categories": {"performance": {"score": 0.31}}}}))
    assert await sa.mobile_pagespeed("https://plumbing.example") == 31
    replies.append(httpx.Response(429, json={"error": "quota"}))
    assert await sa.mobile_pagespeed("https://plumbing.example") is None
    replies.append(httpx.Response(200, json={"lighthouseResult": {}}))
    assert await sa.mobile_pagespeed("https://plumbing.example") is None


# ── Inside the profile step: collected only when confirmed ──

from harvey.collectors.profile import ProfileCollector  # noqa: E402

AUDIT_CODES = {"NO_HTTPS", "NOT_MOBILE_FRIENDLY", "NO_META_DESCRIPTION",
               "OUTDATED_COPYRIGHT", "OLD_WORDPRESS", "MOBILE_SPEED_SCORE"}


def _collector(confirmed, home, scheme="http"):
    c = ProfileCollector.__new__(ProfileCollector)
    c.state, c.confirmed, c.site_names, c.challenged, c.role_keywords = None, set(confirmed), {}, set(), []

    async def resolve(self, domain):
        return f"{scheme}://{domain}", home
    c._resolve_site = resolve.__get__(c)

    async def get(url):
        return ""
    c._get = get
    return c


@pytest.mark.asyncio
async def test_profile_records_website_problems(monkeypatch):
    async def fake_speed(url):
        assert url == "http://plumbing.example"
        return 23
    monkeypatch.setattr(sa, "mobile_pagespeed", fake_speed)
    obs = await _collector(AUDIT_CODES, NEGLECTED).profile_company("c1", "plumbing.example")
    got = {o["signal_code"]: o for o in obs}
    assert set(got) == AUDIT_CODES
    assert got["NO_HTTPS"]["value_num"] == 1
    assert got["NOT_MOBILE_FRIENDLY"]["value_num"] == 1
    assert got["NO_META_DESCRIPTION"]["value_num"] == 1
    assert (got["OUTDATED_COPYRIGHT"]["value_num"], got["OUTDATED_COPYRIGHT"]["value_text"]) == (2019, "© 2019")
    assert got["OLD_WORDPRESS"]["value_text"] == "WordPress 4.9.8"
    assert got["MOBILE_SPEED_SCORE"]["value_num"] == 23
    assert "pagespeed.web.dev" in got["MOBILE_SPEED_SCORE"]["evidence_url"]


@pytest.mark.asyncio
async def test_a_healthy_site_records_no_problems(monkeypatch):
    async def no_speed(url):
        return None  # rate-limited: no score, not a score of 0
    monkeypatch.setattr(sa, "mobile_pagespeed", no_speed)
    obs = await _collector(AUDIT_CODES, MODERN, scheme="https").profile_company("c1", "smile.example")
    got = {o["signal_code"]: o for o in obs}
    assert all(got[c]["value_num"] == 0 for c in ("NO_HTTPS", "NOT_MOBILE_FRIENDLY", "NO_META_DESCRIPTION",
                                                   "OUTDATED_COPYRIGHT", "OLD_WORDPRESS"))
    assert "MOBILE_SPEED_SCORE" not in got


@pytest.mark.asyncio
async def test_nothing_is_collected_until_confirmed(monkeypatch):
    called = []

    async def speed(url):
        called.append(url)
        return 10
    monkeypatch.setattr(sa, "mobile_pagespeed", speed)
    obs = await _collector(set(), NEGLECTED).profile_company("c1", "plumbing.example")
    assert not any(o["signal_code"] in AUDIT_CODES for o in obs)
    assert called == []  # not even the free API is called
