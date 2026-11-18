"""SITEWATCH: scan each prospect company's website before Harvey writes.

Two phases every heartbeat, because a SiteWatch scan is queued and finishes
minutes later:

1. Ask: companies with someone still to write to, never scanned (or not for
   ``sitewatch.rescan_days``), get a scan requested. The scan id is kept in
   the ``sitewatch_scans`` table.
2. Read back: pending scans that have finished become observations
   (SITEWATCH_ISSUES, SITEWATCH_STATUS) with the public report as evidence,
   plus SITEWATCH_UI_ISSUES and SITEWATCH_SEO_ISSUES from the scan's separate
   UI and SEO reports, each with its own report link.

Nothing happens unless ``sitewatch.enabled`` is on, SITEWATCH_URL and
SITEWATCH_API_TOKEN are set, and at least one of the two signals is
confirmed. While a scan is pending the writer holds that company's email for
up to ``sitewatch.max_wait_minutes`` (state.sitewatch_waiting), then writes
without it: SiteWatch being slow or down never stops outreach.

``website_issue_facts`` turns what is known about a site's problems (these
scans plus the profile's own checks in site_audit.py) into fact lines for the
writer.
"""

import logging

from harvey.integrations.sitewatch import SiteWatchClient, SiteWatchError
from harvey.signals import seed_signal_catalog

logger = logging.getLogger("harvey.collectors.sitewatch")

CODES = ("SITEWATCH_ISSUES", "SITEWATCH_STATUS", "SITEWATCH_UI_ISSUES", "SITEWATCH_SEO_ISSUES")
# The scan's separate UI and SEO reports: signal code per ScanResult attribute.
AUDIT_CODES = {"ui": "SITEWATCH_UI_ISSUES", "seo": "SITEWATCH_SEO_ISSUES"}
REQUESTS_PER_CYCLE = 25
# Issues named in SITEWATCH_ISSUES's text, worst first.
NAMED_ISSUES = 3
# PageSpeed mobile scores under this are worth naming.
SLOW_MOBILE_SCORE = 50


async def sitewatch_codes(state) -> set[str]:
    """The SiteWatch signals the user confirmed (empty: collect nothing)."""
    await seed_signal_catalog(state)
    return set(CODES) & await state.confirmed_signal_codes()


async def active_client(state, config, env) -> SiteWatchClient | None:
    """A client when scanning is on, configured and wanted, else None."""
    client = SiteWatchClient.from_config(config, env)
    if client is None or not await sitewatch_codes(state):
        return None
    return client


async def reports_required(state, config, env) -> bool:
    """Must a lead's website report be finished before Harvey writes? Yes
    whenever scans before writing are on and working; no otherwise, so a
    switched-off or unconfigured SiteWatch never holds email back."""
    try:
        return await active_client(state, config, env) is not None
    except Exception:
        return False


def _site_url(company: dict) -> str:
    website = str(company.get("website") or "").strip()
    if website.startswith(("http://", "https://")):
        return website
    return "https://" + str(company.get("domain") or "").strip()


def _observations(company_id: str, result, wanted: set[str], min_severity: str) -> list[dict]:
    rows = []
    if "SITEWATCH_STATUS" in wanted:
        rows.append({"signal_code": "SITEWATCH_STATUS", "company_id": company_id,
                     "collector": "sitewatch", "value_num": result.score,
                     "value_text": result.status, "evidence_url": result.report_url})
    if "SITEWATCH_ISSUES" in wanted:
        issues = result.issues(min_severity)
        named = "; ".join(i["plain"] or i["title"] for i in issues[:NAMED_ISSUES])
        rows.append({"signal_code": "SITEWATCH_ISSUES", "company_id": company_id,
                     "collector": "sitewatch", "value_num": len(issues),
                     "value_text": named[:1000], "evidence_url": result.report_url})
    for kind, code in AUDIT_CODES.items():
        report = getattr(result, kind, None)
        if code not in wanted or report is None:      # an older SiteWatch sends no UI/SEO report
            continue
        issues = report.issues(min_severity)
        named = "; ".join(i["plain"] or i["title"] for i in issues[:NAMED_ISSUES])
        rows.append({"signal_code": code, "company_id": company_id,
                     "collector": "sitewatch", "value_num": len(issues),
                     "value_text": named[:1000], "evidence_url": report.report_url})
    return rows


async def run_sitewatch_stage(state, config, env, client: SiteWatchClient | None = None,
                              limit: int = REQUESTS_PER_CYCLE) -> tuple[int, int]:
    """Ask for new scans and read back finished ones.

    Returns (scans requested, observations written). Free on Harvey's side:
    no model call, two small HTTP requests a company.
    """
    client = client or await active_client(state, config, env)
    if client is None:
        return 0, 0
    wanted = await sitewatch_codes(state)
    sw = config.sitewatch
    run_id = await state.start_run("sitewatch", provider="sitewatch")
    requested = observed = 0
    try:
        # Ask first, then read back: a scan SiteWatch reuses from the last
        # week is already finished and is read in the same cycle.
        requested = await _request_scans(state, client, sw, limit)
        observed = await _read_back(state, client, wanted, sw.min_severity, run_id)
    except Exception as e:
        logger.exception("sitewatch stage failed")
        await state.finish_run(run_id, status="failed", records=observed, error=str(e))
        return requested, observed
    await state.finish_run(run_id, records=observed)
    if requested or observed:
        logger.info(f"SiteWatch: {requested} scan(s) requested, {observed} observation(s) recorded.")
    return requested, observed


async def _request_scans(state, client, sw, limit: int) -> int:
    requested = 0
    for company in await state.companies_needing_sitewatch(limit=limit, rescan_days=sw.rescan_days):
        try:
            result = await client.request_scan(_site_url(company), company.get("name") or "")
        except SiteWatchError as e:
            # Recorded as failed so the writer doesn't wait on it; tried
            # again after rescan_days. One unreachable SiteWatch is the same
            # for every company, so stop asking this cycle.
            await state.set_sitewatch_scan(company["id"], None, state="failed", error=str(e))
            logger.warning(f"SiteWatch: {e}")
            if "Couldn't reach" in str(e) or "refused" in str(e) or "turned off" in str(e):
                break
            continue
        await state.set_sitewatch_scan(company["id"], result.scan_id)
        requested += 1
    return requested


async def _read_back(state, client, wanted: set[str], min_severity: str, run_id: str) -> int:
    observed = 0
    for row in await state.pending_sitewatch_scans():
        try:
            result = await client.get_scan(row["scan_id"])
        except SiteWatchError as e:
            if "no such scan" in str(e):
                await state.finish_sitewatch_scan(row["company_id"], "failed", str(e))
                continue
            logger.warning(f"SiteWatch: {e}")
            break
        if not result.done:
            continue
        if result.state != "completed":
            await state.finish_sitewatch_scan(row["company_id"], "failed",
                                              result.error or f"scan {result.state}")
            continue
        rows = _observations(row["company_id"], result, wanted, min_severity)
        observed += await state.add_observations(rows, run_id=run_id)
        await state.finish_sitewatch_scan(row["company_id"], "done")
    return observed


def basic_report_url(*urls) -> str:
    """SiteWatch's Basic report link from any of a scan's report links (the
    Advanced one, or a section of it: same address with ?tab=… or #…)."""
    for url in urls:
        url = str(url or "").strip()
        if url.startswith(("http://", "https://")):
            return url.split("#", 1)[0].split("?", 1)[0]
    return ""


async def website_issue_facts(state, company_id: str) -> list[str] | None:
    """Fact lines about problems on the company's website, or None.

    From the latest SiteWatch scan and the profile's own homepage checks
    (site_audit.py). A check that found nothing adds no line, and a failed
    read is just "nothing known", never a reason not to write."""
    if not company_id or state is None:
        return None
    try:
        rows = await state.get_observations(company_id=company_id, latest_only=True, limit=200)
        latest = {r["signal_code"]: r for r in rows if isinstance(r, dict)}
    except Exception:
        return None

    def num(code):
        value = (latest.get(code) or {}).get("value_num")
        return value if isinstance(value, (int, float)) else None

    def text(code):
        return str((latest.get(code) or {}).get("value_text") or "").strip()

    facts = []
    flagged = []                                  # SiteWatch codes that named a problem
    for code, what in (("SITEWATCH_ISSUES", "security"), ("SITEWATCH_UI_ISSUES", "responsive & UI"),
                       ("SITEWATCH_SEO_ISSUES", "SEO")):
        found = num(code)
        if not found:
            continue
        line = f"- A website scan (SiteWatch) flagged {int(found)} {what} problem(s) on their site"
        if text(code):
            line += f": {text(code)}"
        facts.append(line)
        flagged.append(code)
    # One link for all of them: SiteWatch's Basic report (scores and the main problems in
    # plain words). Every report link of a scan starts with it; ?tab=… and #… pick a section.
    report = basic_report_url(*(latest[c].get("evidence_url") for c in flagged))
    if report:
        facts.append("- Their website report (scores and the main problems in plain words), "
                     f"which anyone can open without logging in: {report}")
    # The profile's own checks, unless SiteWatch's UI/SEO report already covers them.
    ui_report = "SITEWATCH_UI_ISSUES" in latest
    if num("NO_HTTPS") == 1 and not ui_report:
        facts.append("- Their site only loads over http://, so browsers mark it \"Not secure\"")
    if num("NOT_MOBILE_FRIENDLY") == 1 and not ui_report:
        facts.append("- Their homepage has no mobile viewport tag, so it likely displays badly on phones")
    if num("NO_META_DESCRIPTION") == 1 and "SITEWATCH_SEO_ISSUES" not in latest:
        facts.append("- Their homepage has no meta description (the text Google shows under the link)")
    if num("OUTDATED_COPYRIGHT"):
        facts.append(f"- Their site's footer still says {text('OUTDATED_COPYRIGHT') or 'an old year'}")
    if num("OLD_WORDPRESS") == 1:
        facts.append(f"- Their site runs an old WordPress version ({text('OLD_WORDPRESS') or 'below 6.0'})")
    speed = num("MOBILE_SPEED_SCORE")
    if speed is not None and speed < SLOW_MOBILE_SCORE:
        facts.append(f"- Google PageSpeed scores their mobile site {int(speed)}/100 (slow on phones)")
    return facts or None
