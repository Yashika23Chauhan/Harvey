"""Scout's company-search phrases: plain "<industry> <place>", every place
gets a turn, and no quoted firmographics that small firms never write."""
from types import SimpleNamespace

from harvey.agents.scout import Scout


def _scout(industries, geography, size="1-50 employees"):
    scout = Scout.__new__(Scout)
    scout.config = SimpleNamespace(icp=SimpleNamespace(
        industries=industries, geography=geography, company_size=size))
    return scout


def test_plain_phrases_without_quotes_or_size():
    queries = _scout(["seo agency"], ["Toronto, Canada"])._build_company_search_queries()
    assert queries == ["seo agency Toronto, Canada"]
    assert '"' not in queries[0] and "employees" not in queries[0]


def test_every_city_can_be_searched():
    scout = _scout(["seo agency", "web design agency"], ["Toronto, Canada", "Sydney, Australia"])
    seen = set()
    for _ in range(50):
        queries = scout._build_company_search_queries()
        assert len(queries) == 3
        seen.update(queries)
    assert seen == {
        "seo agency Toronto, Canada", "seo agency Sydney, Australia",
        "web design agency Toronto, Canada", "web design agency Sydney, Australia",
    }


def test_missing_industry_or_place_still_searches():
    assert _scout(["dentist"], [])._build_company_search_queries() == ["dentist"]
    assert _scout([], ["Austin, TX"])._build_company_search_queries() == ["Austin, TX"]
    assert _scout(None, None)._build_company_search_queries() == []


def test_blank_entries_are_ignored_and_whitespace_trimmed():
    queries = _scout(["  dentist ", "", "   ", None], [" Austin, TX ", ""])._build_company_search_queries()
    assert queries == ["dentist Austin, TX"]
    assert _scout(["", "  "], ["", None])._build_company_search_queries() == []


def test_duplicate_pairs_are_searched_once():
    queries = _scout(["dentist", "dentist ", "dentist"], ["Austin, TX", "Austin, TX"])._build_company_search_queries()
    assert queries == ["dentist Austin, TX"]


def test_capped_at_three_distinct_queries_per_cycle():
    industries = ["dentist", "orthodontist", "chiropractor", "physiotherapist"]
    geos = ["Austin, TX", "Denver, CO", "Boise, ID"]
    for _ in range(20):
        queries = _scout(industries, geos)._build_company_search_queries()
        assert len(queries) == 3
        assert len(set(queries)) == 3
        valid = {f"{i} {g}" for i in industries for g in geos}
        assert set(queries) <= valid


def test_company_size_never_reaches_the_query():
    for size in ("1-50 employees", "SMB", "50-200"):
        queries = _scout(["seo agency"], ["Toronto, Canada"], size=size)._build_company_search_queries()
        assert queries == ["seo agency Toronto, Canada"]
    assert _scout(["seo agency"], [], size="")._build_company_search_queries() == ["seo agency"]


def test_no_leftover_company_suffix_or_quotes():
    scout = _scout(["seo agency", "web design agency"], ["Toronto, Canada", "Sydney, Australia"])
    for _ in range(10):
        for q in scout._build_company_search_queries():
            assert '"' not in q
            assert not q.endswith(" company")
            assert not q.startswith("top ")
