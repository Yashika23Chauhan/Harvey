"""Where a company is based, from what its own homepage already shows.

Web search localizes results to wherever Harvey runs, so a search for
"web development company Toronto" made from India returns Indian firms too.
This reads three kinds of evidence from the homepage Scout already fetched
(no extra requests): the domain's country ending, phone numbers with a
country code, and schema.org addressCountry. `outside_targets` says whether
that evidence puts the company outside every country in icp.geography.

Unknown is never "outside": a site that names no country is kept.
"""
from __future__ import annotations

import json
import re

# Country code top-level domains (last label of the host). Generic-looking
# ccTLDs that companies everywhere use (.co, .io, .ai, .me, .tv) are left out.
CCTLD = {
    "us": "US", "ca": "CA", "au": "AU", "nz": "NZ", "uk": "GB", "ie": "IE",
    "in": "IN", "pk": "PK", "bd": "BD", "lk": "LK", "np": "NP",
    "ae": "AE", "sa": "SA", "qa": "QA", "sg": "SG", "my": "MY", "ph": "PH",
    "id": "ID", "vn": "VN", "th": "TH", "cn": "CN", "hk": "HK", "jp": "JP",
    "kr": "KR", "de": "DE", "fr": "FR", "es": "ES", "it": "IT", "nl": "NL",
    "be": "BE", "ch": "CH", "at": "AT", "se": "SE", "no": "NO", "dk": "DK",
    "fi": "FI", "pl": "PL", "pt": "PT", "ro": "RO", "ua": "UA", "ru": "RU",
    "tr": "TR", "il": "IL", "eg": "EG", "za": "ZA", "ng": "NG", "ke": "KE",
    "br": "BR", "mx": "MX", "ar": "AR", "cl": "CL", "pe": "PE",
}

# International calling codes. "+1" is shared by the US and Canada.
CALLING = {
    "1": {"US", "CA"}, "61": {"AU"}, "64": {"NZ"}, "44": {"GB"}, "353": {"IE"},
    "91": {"IN"}, "92": {"PK"}, "880": {"BD"}, "94": {"LK"}, "977": {"NP"},
    "971": {"AE"}, "966": {"SA"}, "974": {"QA"}, "65": {"SG"}, "60": {"MY"},
    "63": {"PH"}, "62": {"ID"}, "84": {"VN"}, "66": {"TH"}, "86": {"CN"},
    "852": {"HK"}, "81": {"JP"}, "82": {"KR"}, "49": {"DE"}, "33": {"FR"},
    "34": {"ES"}, "39": {"IT"}, "31": {"NL"}, "27": {"ZA"}, "234": {"NG"},
    "254": {"KE"}, "55": {"BR"}, "52": {"MX"}, "7": {"RU"}, "90": {"TR"},
    "972": {"IL"}, "20": {"EG"},
}
# Longest codes first, so "+880" isn't read as "+88" or "+8".
_CODES = sorted(CALLING, key=len, reverse=True)
_PHONE = re.compile(r"\+\s?(" + "|".join(_CODES) + r")[\s.\-()]*\d[\d\s.\-()]{6,}")

NAMES = {
    "usa": "US", "us": "US", "united states": "US", "united states of america": "US",
    "america": "US", "canada": "CA", "ca": "CA", "australia": "AU", "au": "AU",
    "india": "IN", "in": "IN", "new zealand": "NZ", "nz": "NZ",
    "united kingdom": "GB", "uk": "GB", "gb": "GB", "england": "GB",
    "pakistan": "PK", "bangladesh": "BD", "philippines": "PH", "singapore": "SG",
    "united arab emirates": "AE", "uae": "AE",
}
US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}


def country_of_place(place: str) -> str:
    """ISO code for one icp.geography entry: "Toronto, Canada" -> CA,
    "Austin, TX" -> US, "USA" -> US. "" when it can't tell."""
    parts = [p.strip() for p in (place or "").split(",") if p.strip()]
    if not parts:
        return ""
    last = parts[-1]
    # "Los Angeles, CA" / "Indianapolis, IN": two letters after a city are a
    # US state, checked first so they don't read as Canada or India.
    if len(parts) > 1 and len(last) == 2 and last.upper() in US_STATES:
        return "US"
    return NAMES.get(last.lower(), "")


def target_countries(geography: list[str] | None) -> set[str]:
    return {c for c in (country_of_place(g) for g in (geography or [])) if c}


def _schema_countries(html: str) -> list[str]:
    found = []
    for m in re.finditer(r'"addressCountry"\s*:\s*("([^"]{2,40})"|\{[^{}]*\})', html):
        raw = m.group(2)
        if raw is None:
            try:
                obj = json.loads(m.group(1))
                raw = obj.get("name") or obj.get("@id") or ""
            except (ValueError, AttributeError):
                raw = ""
        raw = raw.strip()
        code = NAMES.get(raw.lower()) or (raw.upper() if raw.upper() in CCTLD.values() else "")
        if code:
            found.append(code)
    return found


def evidence(domain: str, html: str) -> list[set[str]]:
    """Each piece of location evidence, as the set of countries it allows."""
    out: list[set[str]] = []
    tld = (domain or "").lower().rstrip(".").rsplit(".", 1)[-1]
    if tld in CCTLD:
        # The domain ending is the strongest single sign: count it twice.
        out += [{CCTLD[tld]}] * 2
    html = html or ""
    out += [{c} for c in _schema_countries(html)]
    out += [CALLING[m.group(1)] for m in _PHONE.finditer(html)]
    return out


def outside_targets(domain: str, html: str, geography: list[str] | None) -> str:
    """The foreign country this company appears to be in, or "" to keep it.

    Kept when there are no target countries to compare with, when the page
    shows no location, or when evidence for a target country is at least as
    strong as evidence against. An agency listing both a "+1" sales line and
    "+91" offices counts once for each, so the domain ending decides.
    """
    targets = target_countries(geography)
    if not targets:
        return ""
    ours = theirs = 0
    foreign: dict[str, int] = {}
    for countries in evidence(domain, html):
        if countries & targets:
            ours += 1
        else:
            theirs += 1
            for c in countries:
                foreign[c] = foreign.get(c, 0) + 1
    if theirs and theirs > ours:
        return max(foreign, key=foreign.get)
    return ""
