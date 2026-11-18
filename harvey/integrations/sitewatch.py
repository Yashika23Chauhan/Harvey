"""Optional website scan through your own SiteWatch.

Off by default. With ``sitewatch.enabled: true`` in harvey.yaml and
SITEWATCH_URL / SITEWATCH_API_TOKEN in .env, Harvey asks SiteWatch to scan
each prospect company's website before writing to them. SiteWatch looks for
signs of a hacked or broken site (injected spam, hidden links, fake CAPTCHAs,
redirects to other sites, certificate and DNS problems) and keeps a public,
no-login report for every scan.

Besides security, each scan checks things the owner could improve: responsive
design and UI (mobile layout, speed, HTTPS, a way to get in touch) and SEO
(titles, descriptions, headings, indexing, sitemap), each with its own 0-100
score (higher is better) and findings. Older SiteWatch versions don't send
them; ``ScanResult.ui`` / ``.seo`` are then None.

The public report comes in two: ``report_url`` is the Basic report (the three
scores and the main problems in plain words, the one to share) and
``advanced_report_url`` has every check.

Scans are queued on SiteWatch's side and take a few minutes, so this is two
calls: ``request_scan`` files the site under SiteWatch's Prospects client and
returns a scan id, and ``get_scan`` reads it back later. SiteWatch's API can
only read prospect scans, never the scans of the clients it monitors.

SiteWatch receives the company's website address and name, nothing else.
"""

import asyncio
import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger("harvey.sitewatch")

TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
TOTAL_TIMEOUT = 40.0
SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
DONE_STATES = ("completed", "failed", "skipped")


class SiteWatchError(Exception):
    """SiteWatch couldn't be asked or didn't answer usefully. The message is
    plain language and never contains the API token."""


def _at_least(findings: list[dict], min_severity: str) -> list[dict]:
    floor = SEVERITY_RANK.get(min_severity, 2)
    return [f for f in findings if SEVERITY_RANK.get(f.get("severity", ""), 0) >= floor]


def _findings(raw) -> list[dict]:
    return [
        {"code": str(f.get("code") or ""), "severity": str(f.get("severity") or ""),
         "title": str(f.get("title") or ""), "plain": str(f.get("plain") or ""),
         "where": str(f.get("where") or "")}
        for f in raw or [] if isinstance(f, dict)
    ]


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class AuditReport:
    """The UI or SEO report of a scan. ``score`` is health, 0-100, higher is better."""
    kind: str
    score: int = 100
    grade: str = ""
    report_url: str = ""
    findings: list[dict] = field(default_factory=list)

    def issues(self, min_severity: str = "medium") -> list[dict]:
        return _at_least(self.findings, min_severity)

    @classmethod
    def parse(cls, kind: str, data) -> "AuditReport | None":
        if not isinstance(data, dict):
            return None
        return cls(kind=kind, score=max(0, min(100, _int(data.get("score"), 100))),
                   grade=str(data.get("grade") or ""), report_url=str(data.get("report_url") or ""),
                   findings=_findings(data.get("findings")))


@dataclass
class ScanResult:
    scan_id: int
    state: str
    status: str = ""
    score: int = 0
    report_url: str = ""
    error: str = ""
    findings: list[dict] = field(default_factory=list)
    ui: AuditReport | None = None
    seo: AuditReport | None = None
    # report_url is the Basic report (scores and main problems); this one has every check
    advanced_report_url: str = ""

    @property
    def done(self) -> bool:
        return self.state in DONE_STATES

    def issues(self, min_severity: str = "medium") -> list[dict]:
        """Findings at or above ``min_severity``, worst first (SiteWatch
        already sorts them)."""
        return _at_least(self.findings, min_severity)


class SiteWatchClient:
    def __init__(self, base_url: str, api_token: str,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self._transport = transport

    @classmethod
    def from_config(cls, config, env) -> "SiteWatchClient | None":
        """A client when the scan is switched on and configured, else None."""
        sw = getattr(config, "sitewatch", None)
        if not (sw and sw.enabled):
            return None
        url = str(getattr(env, "sitewatch_url", "") or "")
        token = str(getattr(env, "sitewatch_api_token", "") or "")
        if not (url and token):
            logger.warning("SiteWatch is enabled but SITEWATCH_URL or SITEWATCH_API_TOKEN "
                           "is missing from .env; skipping website scans.")
            return None
        return cls(url, token)

    def _redact(self, text: str) -> str:
        return text.replace(self.api_token, "***") if self.api_token else text

    async def _call(self, method: str, path: str, json_body: dict | None = None) -> dict:
        if not self.api_token.isascii() or not self.api_token.isprintable():
            raise SiteWatchError("SITEWATCH_API_TOKEN has characters a header can't carry.")
        headers = {"Authorization": f"Bearer {self.api_token}", "Accept": "application/json"}
        url = f"{self.base_url}/api{path}"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport) as client:
                resp = await asyncio.wait_for(
                    client.request(method, url, headers=headers, json=json_body), TOTAL_TIMEOUT)
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            raise SiteWatchError(self._redact(f"Couldn't reach SiteWatch at {self.base_url}: "
                                              f"{type(e).__name__} {e}".strip()))
        if resp.status_code == 401:
            raise SiteWatchError("SiteWatch refused the API token (SITEWATCH_API_TOKEN).")
        if resp.status_code == 503:
            raise SiteWatchError("SiteWatch's API is turned off: set SITEWATCH_API_TOKEN in SiteWatch's .env.")
        if resp.status_code == 404:
            raise SiteWatchError(f"SiteWatch has no such scan, or no API at {self.base_url}/api.")
        if resp.status_code == 429:
            raise SiteWatchError("SiteWatch is rate-limiting requests; trying again next cycle.")
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code >= 400 or not isinstance(data, dict):
            detail = data.get("error") or data.get("message") if isinstance(data, dict) else ""
            raise SiteWatchError(self._redact(
                f"SiteWatch answered {resp.status_code}" + (f": {detail}" if detail else ".")))
        return data

    async def ping(self) -> str:
        """Check the address and token. Returns the Prospects client's name."""
        data = await self._call("GET", "/ping")
        return str(data.get("prospects_client") or "")

    async def request_scan(self, url: str, name: str = "") -> ScanResult:
        """Ask for a scan of ``url``. A recent finished scan is reused."""
        data = await self._call("POST", "/prospect-scans", {"url": url, "name": name or None})
        try:
            return ScanResult(scan_id=int(data["scan_id"]), state=str(data.get("state") or ""))
        except (KeyError, TypeError, ValueError):
            raise SiteWatchError("SiteWatch's answer had no scan id.")

    async def get_scan(self, scan_id: int) -> ScanResult:
        data = await self._call("GET", f"/scans/{int(scan_id)}")
        return ScanResult(
            scan_id=int(scan_id),
            state=str(data.get("state") or ""),
            status=str(data.get("status") or ""),
            score=_int(data.get("score") or 0),
            report_url=str(data.get("report_url") or ""),
            error=str(data.get("error") or ""),
            findings=_findings(data.get("findings")),
            ui=AuditReport.parse("ui", data.get("ui")),
            seo=AuditReport.parse("seo", data.get("seo")),
            advanced_report_url=str(data.get("advanced_report_url") or ""),
        )
