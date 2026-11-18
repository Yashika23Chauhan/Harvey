"""Website problems Harvey can name in an email, from the homepage it already
read. Each check is a plain function of the page so it can be tested without
a network; only the PageSpeed score makes a request of its own.

Every finding has to be true of the page Harvey fetched: an email that
claims a problem the site doesn't have loses the reader at once.
"""
from __future__ import annotations

import os
import re
from datetime import date
from html import unescape

import httpx

# WordPress releases older than this are years out of date (6.0 shipped May
# 2022) and miss security fixes, so naming one in an email is fair.
OLD_WORDPRESS_BELOW = (6, 0)
PAGESPEED_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
# Lighthouse calls 0-49 poor; a site this slow on a phone loses visitors.
SLOW_MOBILE_BELOW = 50


def _visible(html: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", html or "")
    return unescape(re.sub(r"<[^>]+>", " ", text))


def is_insecure(site_url: str) -> bool:
    """The only address that answered is plain http://."""
    return (site_url or "").lower().startswith("http://")


def lacks_mobile_viewport(html: str) -> bool:
    """No <meta name="viewport">: phones render the desktop layout shrunk down."""
    return not re.search(r"""<meta[^>]+name\s*=\s*["']?viewport""", html or "", re.I)


def lacks_meta_description(html: str) -> bool:
    """No meta description (or an empty one): Google writes its own snippet."""
    for m in re.finditer(r"<meta\b[^>]*>", html or "", re.I):
        tag = m.group(0)
        if re.search(r"""name\s*=\s*["']?description["'\s>]""", tag, re.I):
            content = re.search(r"""content\s*=\s*["']([^"']*)""", tag, re.I)
            if content and content.group(1).strip():
                return False
    return True


def copyright_year(html: str) -> int | None:
    """The newest year in a copyright notice (© 2019, Copyright 2018-2021),
    or None when the page has none. Ranges count by their last year."""
    text = _visible(html)
    years = []
    for m in re.finditer(r"(?i)(?:©|&copy;|\(c\)|copyright)\s*(?:\d{4}\s*[-–—]\s*)?(\d{4})", text):
        y = int(m.group(1))
        if 1995 <= y <= date.today().year + 1:
            years.append(y)
    return max(years) if years else None


def outdated_copyright(html: str, today: date | None = None) -> int | None:
    """The copyright year when it's two or more years old, else None.
    Last year's notice is normal in January; older says nobody's updating."""
    year = copyright_year(html)
    today = today or date.today()
    return year if year is not None and year <= today.year - 2 else None


def wordpress_version(html: str) -> tuple[int, ...] | None:
    """The WordPress version the site advertises (meta generator, or ?ver= on
    core files), or None when it doesn't say."""
    m = re.search(r"""<meta[^>]+content\s*=\s*["']WordPress\s+(\d+(?:\.\d+){0,2})""", html or "", re.I)
    if not m:
        # Only WordPress's own core files carry its version; the libraries it
        # bundles in wp-includes (jQuery 3.7.1, Migrate...) carry their own.
        m = re.search(r"wp-includes/(?:js/(?:wp-emoji-release|wp-emoji|wp-embed|comment-reply|admin-bar)(?:\.min)?\.js"
                      r"|css/(?:dist/block-library/style|dashicons|admin-bar)(?:\.min)?\.css)\?ver=(\d+\.\d+(?:\.\d+)?)",
                      html or "")
    if not m:
        return None
    try:
        return tuple(int(p) for p in m.group(1).split("."))
    except ValueError:
        return None


def old_wordpress(html: str) -> str:
    """The version string when the site runs an out-of-date WordPress, else ""."""
    v = wordpress_version(html)
    return ".".join(map(str, v)) if v and v[:2] < OLD_WORDPRESS_BELOW else ""


async def mobile_pagespeed(url: str, timeout: float = 60.0) -> int | None:
    """Google PageSpeed Insights' mobile performance score, 0-100, or None
    when it can't be had (rate limit, timeout, a site Google can't load).
    Free; PAGESPEED_API_KEY in .env raises the daily allowance. A fixed API,
    so it has its own client (public_http is for untrusted websites); Google
    is the one that fetches the business's site."""
    params = {"url": url, "strategy": "mobile", "category": "performance"}
    key = os.getenv("PAGESPEED_API_KEY", "").strip()
    if key:
        params["key"] = key
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(PAGESPEED_URL, params=params)
        if resp.status_code != 200:
            return None
        score = resp.json()["lighthouseResult"]["categories"]["performance"]["score"]
        return round(float(score) * 100) if score is not None else None
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None
