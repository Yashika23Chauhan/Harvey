"""Keeping prospects inside icp.geography's countries, from the homepage."""
from types import SimpleNamespace

from harvey.agents.scout import Scout
from harvey.site_country import country_of_place, outside_targets, site_location, target_countries

GEO = ["Sydney, Australia", "Toronto, Canada", "New York, USA", "Austin, TX"]


def test_places_to_countries():
    assert country_of_place("Sydney, Australia") == "AU"
    assert country_of_place("Toronto, Canada") == "CA"
    assert country_of_place("New York, USA") == "US"
    assert country_of_place("United States") == "US"
    # Two letters after a city are a US state, not Canada / India.
    assert country_of_place("Los Angeles, CA") == "US"
    assert country_of_place("Indianapolis, IN") == "US"
    assert country_of_place("Canada") == "CA"
    assert country_of_place("Springfield") == ""
    assert country_of_place("") == ""
    assert target_countries(GEO) == {"AU", "CA", "US"}


def test_indian_domain_is_outside():
    assert outside_targets("pixelcraft.example.co.in", "<html></html>", GEO) == "IN"
    assert outside_targets("webworks.example.in", "", GEO) == "IN"


def test_indian_phone_on_a_dotcom_is_outside():
    html = '<a href="tel:+91 90000 00000">Call</a> <p>Office: +91-22-0000-0000</p>'
    assert outside_targets("brightagency.example", html, GEO) == "IN"


def test_schema_address_country_counts():
    html = '{"@type":"Organization","address":{"addressCountry":"India"}}'
    assert outside_targets("agency.example", html, GEO) == "IN"
    html = '{"address":{"addressCountry":{"@type":"Country","name":"Australia"}}}'
    assert outside_targets("agency.example", html, GEO) == ""


def test_target_country_sites_are_kept():
    assert outside_targets("studio.example.com.au", "<p>Call +61 2 0000 0000</p>", GEO) == ""
    assert outside_targets("maple.example.ca", "", GEO) == ""
    assert outside_targets("growth.example", '<a href="tel:+1 (512) 555-0100">', GEO) == ""


def test_no_location_is_kept():
    assert outside_targets("agency.example", "<h1>We build websites</h1>", GEO) == ""
    assert outside_targets("agency.example.io", "", GEO) == ""


def test_mixed_offices_follow_the_stronger_evidence():
    # A US sales line and an Indian office: one each, so it's kept...
    both = "<p>US: +1 212 555 0100</p><p>India: +91 80 0000 0000</p>"
    assert outside_targets("agency.example", both, GEO) == ""
    # ...but an Indian domain with one US number is still Indian.
    assert outside_targets("agency.example.co.in", both, GEO) == "IN"


def test_no_targets_means_nothing_is_filtered():
    assert outside_targets("agency.example.co.in", "+91 90000 00000", []) == ""
    assert outside_targets("agency.example.co.in", "+91 90000 00000", ["Springfield"]) == ""


def test_scout_skips_companies_outside_geography():
    scout = Scout.__new__(Scout)
    scout.config = SimpleNamespace(icp=SimpleNamespace(geography=GEO))
    assert scout._outside_geography("agency.example.co.in", {"_homepage_html": ""})
    assert not scout._outside_geography("agency.example.com.au", {"_homepage_html": ""})
    assert not scout._outside_geography("agency.example", {})


# ── Location shown on the Companies tab ──


def test_location_prefers_the_published_address():
    html = ('{"@type":"LocalBusiness","address":{"@type":"PostalAddress",'
            '"addressLocality":"Toronto","addressRegion":"ON","addressCountry":"CA"}}')
    assert site_location("studio.example", html, "Vancouver, Canada") == "Toronto, ON, Canada"


def test_location_falls_back_to_the_searched_place():
    assert site_location("studio.example", "", "Sydney, Australia") == "Sydney, Australia"
    # The site agrees with the search (an Australian domain): still the place.
    assert site_location("studio.example.com.au", "", "Melbourne, Australia") == "Melbourne, Australia"


def test_location_never_contradicts_the_site():
    # Searched Toronto, but the site is Australian: say Australia, not Toronto.
    assert site_location("studio.example.com.au", "", "Toronto, Canada") == "Australia"
    assert site_location("maple.example.ca", "") == "Canada"


def test_location_does_not_guess_between_us_and_canada():
    assert site_location("growth.example", '<a href="tel:+1 (512) 555-0100">') == "USA or Canada"


def test_no_evidence_no_location():
    assert site_location("agency.example", "<h1>Hi</h1>") == ""


def test_search_queries_remember_their_place():
    scout = Scout.__new__(Scout)
    scout.config = SimpleNamespace(icp=SimpleNamespace(
        industries=["web development company"], geography=["Sydney, Australia", "Austin, TX"],
        company_size=""))
    queries = scout._build_company_search_queries()
    assert {scout._query_places[q] for q in queries} == {"Sydney, Australia", "Austin, TX"}


def test_city_and_country_come_from_the_same_address():
    # Two offices: the first address has no city, the second is complete.
    html = ('{"address":{"addressCountry":"CA"}} '
            '{"address":{"addressLocality":"Seattle","addressRegion":"WA","addressCountry":"US"}}')
    assert site_location("tools.example", html) == "Seattle, WA, USA"


def test_street_lines_are_not_a_city():
    html = '{"address":{"addressLocality":"Unit 4, 12 Example Road, near Gate No. 4","addressCountry":"IN"}}'
    assert site_location("news.example", html) == "India"


def test_missing_country_comes_from_the_domain():
    html = '{"address":{"addressLocality":"Surry Hills","addressRegion":"NSW"}}'
    assert site_location("studio.example.com.au", html) == "Surry Hills, NSW, Australia"


def test_country_codes_read_as_names():
    assert site_location("tools.example", '{"address":{"addressCountry":"SG"}}') == "Singapore"


def test_three_letter_country_codes():
    html = '{"address":{"addressLocality":"Surry Hills","addressRegion":"NSW","addressCountry":"AUS"}}'
    assert site_location("studio.example", html) == "Surry Hills, NSW, Australia"
    assert country_of_place("Perth, AUS") == "AU"
