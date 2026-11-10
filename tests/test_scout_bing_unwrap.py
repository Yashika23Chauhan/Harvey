"""Bing wraps every result in a bing.com/ck/a redirect; Scout must unwrap it,
or every Bing result looks like bing.com and is thrown away as junk."""
import base64
from types import SimpleNamespace

import pytest

from harvey.agents.scout import Scout


def _wrapped(real: str) -> str:
    u = "a1" + base64.urlsafe_b64encode(real.encode()).decode().rstrip("=")
    return f"https://www.bing.com/ck/a?!&&p=abc123&ptn=3&ver=2&hsh=4&u={u}&ntb=1"


def test_unwraps_click_tracking_redirect():
    assert Scout._unwrap_bing(_wrapped("https://acme-seo.example/about")) == \
        "https://acme-seo.example/about"


def test_unwraps_urls_whose_base64_needs_padding():
    for real in ("https://a.example/", "https://ab.example/x", "https://abc.example/xy?q=1"):
        assert Scout._unwrap_bing(_wrapped(real)) == real


def test_leaves_plain_links_alone():
    assert Scout._unwrap_bing("https://plumbing.example/") == "https://plumbing.example/"


def test_leaves_undecodable_or_non_http_redirects_alone():
    bad = "https://www.bing.com/ck/a?u=a1%%%notbase64&ntb=1"
    assert Scout._unwrap_bing(bad) == bad
    js = _wrapped("javascript:alert(1)")
    assert Scout._unwrap_bing(js) == js
    no_prefix = "https://www.bing.com/ck/a?u=zzaHR0cHM6Ly9hLmV4YW1wbGUv"
    assert Scout._unwrap_bing(no_prefix) == no_prefix


def test_other_bing_paths_are_not_unwrapped():
    other = "https://www.bing.com/search?q=x&u=a1aHR0cHM6Ly9hLmV4YW1wbGUv"
    assert Scout._unwrap_bing(other) == other


def test_unwraps_plain_http_and_regional_bing_hosts():
    real = "http://roofing.example/contact"
    assert Scout._unwrap_bing(_wrapped(real)) == real
    regional = _wrapped(real).replace("www.bing.com", "cn.bing.com")
    assert Scout._unwrap_bing(regional) == real


def test_unwraps_non_ascii_urls():
    real = "https://café.example/über-uns"
    assert Scout._unwrap_bing(_wrapped(real)) == real


def test_redirect_without_u_or_with_empty_u_is_left_alone():
    for href in ("https://www.bing.com/ck/a?p=abc&ntb=1",
                 "https://www.bing.com/ck/a?u=&ntb=1",
                 "https://www.bing.com/ck/a?u=a1&ntb=1"):
        assert Scout._unwrap_bing(href) == href


def test_empty_and_relative_hrefs_are_left_alone():
    assert Scout._unwrap_bing("") == ""
    assert Scout._unwrap_bing("/ck/a?u=a1aHR0cHM6Ly9hLmV4YW1wbGUv") == \
        "/ck/a?u=a1aHR0cHM6Ly9hLmV4YW1wbGUv"


@pytest.mark.asyncio
async def test_search_bing_returns_real_urls_not_bing_redirects():
    html = f"""
    <ol id="b_results">
      <li class="b_algo"><h2><a href="{_wrapped('https://acme-seo.example/')}">Acme</a></h2>
        <div class="b_caption"><p>Local SEO in Toronto</p></div></li>
      <li class="b_algo"><h2><a href="https://plumbing.example/">Plumbing</a></h2></li>
      <li class="b_algo"><h2><a href="{_wrapped('javascript:alert(1)')}">Bad</a></h2></li>
      <li class="b_algo"><h2><a>No href</a></h2></li>
    </ol>"""
    scout = Scout.__new__(Scout)

    async def fake_fetch(url, **kwargs):
        assert url.startswith("https://www.bing.com/search?q=seo+agency+Toronto")
        return SimpleNamespace(text=html)

    scout._fetch = fake_fetch
    results = await scout._search_bing("seo agency Toronto")

    assert results[0] == ("https://acme-seo.example/", "Local SEO in Toronto")
    assert results[1] == ("https://plumbing.example/", "")
    # The javascript: redirect stays wrapped (so still a bing.com URL, which
    # junk filtering drops later); nothing decodes to a non-http target.
    assert all(href.startswith("http") for href, _ in results)
    assert not any(href.startswith("javascript:") for href, _ in results)


@pytest.mark.asyncio
async def test_search_bing_returns_nothing_when_fetch_fails():
    scout = Scout.__new__(Scout)

    async def fake_fetch(url, **kwargs):
        return None

    scout._fetch = fake_fetch
    assert await scout._search_bing("anything") == []
