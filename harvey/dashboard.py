"""Harvey Dashboard — local web UI to set up, control, and monitor Harvey."""

import asyncio
import ipaddress
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import aiosqlite
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse, JSONResponse, PlainTextResponse, Response,
)

logger = logging.getLogger("harvey.dashboard")

from harvey import dashboard_auth as auth  # noqa: E402
from harvey.paths import PROJECT_ROOT, wake_harvey  # noqa: E402
from harvey.pidfile import LOCK_FILE, loop_lock_held, pid_alive  # noqa: E402
from harvey.textio import read_user_text  # noqa: E402
DB_PATH = PROJECT_ROOT / "data" / "harvey.db"
ENV_FILE = PROJECT_ROOT / ".env"
CONFIG_FILE = PROJECT_ROOT / "harvey.yaml"
PID_FILE = PROJECT_ROOT / "data" / "harvey.pid"
LOG_FILE = PROJECT_ROOT / "data" / "harvey.log"
TOKEN_FILE = PROJECT_ROOT / "data" / "dashboard-token"


def _resolve_config_file() -> Path:
    """harvey.local.yaml wins when present — same resolution as config.py's
    loader, so the dashboard doesn't judge setup complete against the
    tracked harvey.yaml template while the real trained config sits in the
    gitignored local override."""
    from harvey.config import _find_config_file, ConfigFileNotFoundError
    try:
        return Path(_find_config_file())
    except ConfigFileNotFoundError:
        return CONFIG_FILE

# No /docs, /redoc or /openapi.json: the API is for this UI only, and every
# route except the static shell needs the token (see dashboard_auth).
app = FastAPI(title="Harvey Dashboard", docs_url=None, redoc_url=None, openapi_url=None)
token_store = auth.TokenStore(TOKEN_FILE)
user_store = auth.UserStore(PROJECT_ROOT / "data" / "dashboard-users.json")
app.add_middleware(auth.DashboardGuard, store=token_store)

# Harvey process tracking
_harvey_process: subprocess.Popen | None = None
_harvey_started_at: datetime | None = None
_env_lock = asyncio.Lock()


# ── Helpers ──


async def query_db(sql: str, params: tuple = ()) -> list[dict]:
    """Run a query and return results as list of dicts.

    Never raises: a missing DB file, missing table, or malformed schema
    returns [] so no dashboard route can 500 on an empty install.
    """
    if not DB_PATH.exists():
        return []
    try:
        async with aiosqlite.connect(str(DB_PATH)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("query_db failed (%s): %s", sql.split(None, 4)[:4], e)
        return []


def _mask_key(key: str) -> str:
    """Mask an API key for display: show first 4 and last 4 chars."""
    if not key or len(key) < 10:
        return "****" if key else ""
    return key[:4] + "****" + key[-4:]


def _read_env_file() -> dict[str, str]:
    """Read .env file and return as dict."""
    env_vars = {}
    if ENV_FILE.exists():
        for line in read_user_text(ENV_FILE).splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env_vars[key.strip()] = value.strip()
    return env_vars


def _env_key(line: str) -> str:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return ""
    key = stripped.partition("=")[0].strip()
    if key.startswith("export "):
        key = key[len("export "):].strip()
    return key


def _write_env_file(updates: dict[str, str]):
    """Set keys in .env, keeping every other line (comments, order, other
    keys) as it was.

    Written to a private temporary file in the same directory and swapped
    in atomically, so a crash never leaves a half-written .env and the
    credentials are never readable by other users, even for a moment: the
    result is owner-only (0600 on POSIX) whether .env existed or not. A
    symlinked .env is refused rather than replaced or followed.
    """
    if ENV_FILE.is_symlink():
        raise OSError(f"{ENV_FILE} is a symbolic link; edit it by hand.")
    if ENV_FILE.exists() and not ENV_FILE.is_file():
        raise OSError(f"{ENV_FILE} is not a regular file.")
    lines = (read_user_text(ENV_FILE).splitlines()
             if ENV_FILE.exists() else [])

    out, written = [], set()
    for line in lines:
        key = _env_key(line)
        if key in updates:
            if key in written:
                continue  # a later duplicate would override the new value
            out.append(f"{key}={updates[key]}")
            written.add(key)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in updates.items() if k not in written]

    fd, tmp = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=str(ENV_FILE.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            if os.name != "nt":
                os.fchmod(f.fileno(), stat.S_IRUSR | stat.S_IWUSR)
            f.write("\n".join(out) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ENV_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    load_dotenv(str(ENV_FILE), override=True)


def _check_harvey_pid() -> int | None:
    """Check if there's a running Harvey process from a PID file."""
    global _harvey_process, _harvey_started_at
    if _harvey_process and _harvey_process.poll() is None:
        return _harvey_process.pid
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = None
        # pid_alive, never os.kill(pid, 0): on Windows that terminates the loop.
        # And only while a loop holds the lock: after a crash or a reboot the
        # PID can belong to an unrelated program, which Stop must not signal.
        if pid is not None and pid_alive(pid) and loop_lock_held(LOCK_FILE):
            return pid
        try:
            PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass
    return None


# ── Setup Status ──


@app.get("/api/setup-status")
async def get_setup_status():
    """Check what's configured and what still needs setup."""
    checks = []

    # 1. Venv
    checks.append({
        "id": "venv", "label": "Python virtual environment",
        "done": (PROJECT_ROOT / ".venv").is_dir(),
        "required": True,
        "help": "Run: python3 -m venv .venv && source .venv/bin/activate && pip install -e .",
    })

    # 2. Env file
    env_vars = _read_env_file()
    env_exists = ENV_FILE.exists() and bool(env_vars)
    checks.append({
        "id": "env_file", "label": "Environment file (.env)",
        "done": env_exists,
        "required": True,
        "help": "Go to the Settings tab to enter your API keys.",
    })

    # 3. Email provider configured (matches channels.email.provider)
    provider = _current_provider()

    def _has(*keys):
        return all((env_vars.get(k, "") or os.getenv(k, "")).strip() for k in keys)

    if provider == "gmail":
        provider_done = _has("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET") and \
            (PROJECT_ROOT / "data" / "gmail_token.json").is_file()
        provider_help = ("Set GMAIL_CLIENT_ID/SECRET in Settings, then run "
                         "'harvey gmail auth' in your terminal.")
    elif provider == "microsoft":
        provider_done = _has("M365_CLIENT_ID") and \
            (PROJECT_ROOT / "data" / "m365_token.json").is_file()
        provider_help = ("Set the Microsoft 365 client and tenant IDs in Settings, then run "
                         "'harvey microsoft auth' in your terminal.")
    elif provider == "smtp":
        provider_done = _has("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD")
        provider_help = "Enter your SMTP host, username, and password in Settings."
    else:
        instantly_key = env_vars.get("INSTANTLY_API_KEY", "") or os.getenv("INSTANTLY_API_KEY", "")
        provider_done = bool(instantly_key) and instantly_key != "your_instantly_api_key_here"
        provider_help = "Enter your Instantly API key in Settings."
    checks.append({
        "id": "email_provider", "label": f"Email provider configured ({provider})",
        "done": provider_done,
        "required": True,
        "help": provider_help,
    })

    # 4. Email verification (needed for addresses to be sendable, not 'guess')
    verifier_set = _has("REOON_API_KEY") or _has("ZEROBOUNCE_API_KEY") or _has("HUNTER_API_KEY")
    checks.append({
        "id": "verifier", "label": "Email verification key",
        "done": verifier_set,
        "required": True,
        "help": "Add a Reoon (free 600/mo), ZeroBounce, or Hunter key in Settings — "
                "without one, found emails stay 'guess' and are never sent.",
    })

    # 5. Config valid
    config_valid = False
    config_path = _resolve_config_file()
    if config_path.exists():
        try:
            cfg = yaml.safe_load(read_user_text(config_path))
            company = cfg.get("persona", {}).get("company", "")
            product = cfg.get("product", {}).get("name", "")
            config_valid = company not in ("Your Company", "") and product not in ("Your Product", "")
        except Exception:
            pass
    checks.append({
        "id": "config", "label": f"Harvey configured ({config_path.name})",
        "done": config_valid,
        "required": True,
        "help": "Train Harvey on your product. Use the trainer or set up manually through Claude.",
    })

    # 6. Product trained
    product_trained = (PROJECT_ROOT / "skills" / "product_knowledge.md").exists()
    checks.append({
        "id": "product_trained", "label": "Product knowledge trained",
        "done": product_trained,
        "required": True,
        "help": "Run: harvey train https://yourwebsite.com (or set up through Claude).",
    })

    # 7. LinkedIn (optional)
    linkedin_email = env_vars.get("LINKEDIN_EMAIL", "") or os.getenv("LINKEDIN_EMAIL", "")
    linkedin_pass = env_vars.get("LINKEDIN_PASSWORD", "") or os.getenv("LINKEDIN_PASSWORD", "")
    checks.append({
        "id": "linkedin", "label": "LinkedIn credentials",
        "done": bool(linkedin_email) and bool(linkedin_pass),
        "required": False,
        "help": "Optional. Enter your LinkedIn credentials in Settings to enable LinkedIn prospecting.",
    })

    # 8. Cloudflare (optional)
    cf_id = env_vars.get("CLOUDFLARE_ACCOUNT_ID", "") or os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
    cf_token = env_vars.get("CLOUDFLARE_API_TOKEN", "") or os.getenv("CLOUDFLARE_API_TOKEN", "")
    checks.append({
        "id": "cloudflare", "label": "Cloudflare deep crawling",
        "done": bool(cf_id) and bool(cf_token),
        "required": False,
        "help": "Optional. For JavaScript-rendered website crawling during training.",
    })

    # 9. Warmup service: TrulyInbox connected, or another service the user named
    from harvey.integrations import trulyinbox as ti
    try:
        state = _state()
        await state.init_db()
        choice = await ti.get_choice(state)
    except Exception:
        choice = ""
    if _has("TRULYINBOX_API_KEY"):
        choice = "connected"
    checks.append({
        "id": "warmup_service",
        "label": {"connected": "Warmup: TrulyInbox connected",
                  "other": "Warmup: your own warmup service"}.get(choice, "Warm up your mailbox"),
        "done": bool(choice),
        # Instantly warms its own accounts, so there it's a suggestion, not a step.
        "required": provider != "instantly",
        "help": ("A new mailbox needs 2-4 weeks of warmup before cold email, and warmup "
                 "keeps it out of spam after that. We recommend TrulyInbox: connect it and "
                 "Harvey shows whether your mailbox is warmed."),
        "link": ti.AFFILIATE_URL,
    })

    required_checks = [c for c in checks if c["required"]]
    completed_required = sum(1 for c in required_checks if c["done"])

    return {
        "checks": checks,
        "completed": completed_required,
        "total_required": len(required_checks),
        "percent": int(completed_required / len(required_checks) * 100) if required_checks else 0,
    }


# ── Settings ──


def _current_provider() -> str:
    """Read channels.email.provider from the active config (best-effort)."""
    try:
        cfg = yaml.safe_load(read_user_text(_resolve_config_file())) or {}
        return ((cfg.get("channels") or {}).get("email") or {}).get("provider", "instantly")
    except Exception:
        return "instantly"


@app.get("/api/settings")
async def get_settings():
    """Get current settings — presence flags only for secrets, never raw values."""
    env_vars = _read_env_file()
    all_keys = [
        "INSTANTLY_API_KEY", "LINKEDIN_EMAIL", "LINKEDIN_PASSWORD",
        "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN",
        "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET",
        "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD",
        "IMAP_HOST", "IMAP_PORT", "IMAP_USERNAME", "IMAP_PASSWORD",
        "M365_CLIENT_ID", "M365_TENANT_ID",
        "REOON_API_KEY", "ZEROBOUNCE_API_KEY", "HUNTER_API_KEY",
    ]
    for key in all_keys:
        if key not in env_vars:
            env_vars[key] = os.getenv(key, "")

    def is_set(k):
        return bool((env_vars.get(k) or "").strip())

    # Gmail is authorized once harvey gmail auth has stored a token file.
    gmail_token = (PROJECT_ROOT / "data" / "gmail_token.json").is_file()

    return {
        "provider": _current_provider(),
        # Non-secret values echo back so fields repopulate; secrets are
        # presence-only so keys never leave the box.
        "instantly_api_key_set": is_set("INSTANTLY_API_KEY"),
        "linkedin_email": env_vars.get("LINKEDIN_EMAIL", ""),
        "linkedin_password_set": is_set("LINKEDIN_PASSWORD"),
        "cloudflare_account_id": env_vars.get("CLOUDFLARE_ACCOUNT_ID", ""),
        "cloudflare_api_token_set": is_set("CLOUDFLARE_API_TOKEN"),
        "gmail_client_id": env_vars.get("GMAIL_CLIENT_ID", ""),
        "gmail_client_secret_set": is_set("GMAIL_CLIENT_SECRET"),
        "gmail_authorized": gmail_token,
        "smtp_host": env_vars.get("SMTP_HOST", ""),
        "smtp_port": env_vars.get("SMTP_PORT", ""),
        "smtp_username": env_vars.get("SMTP_USERNAME", ""),
        "smtp_password_set": is_set("SMTP_PASSWORD"),
        "imap_host": env_vars.get("IMAP_HOST", ""),
        "imap_port": env_vars.get("IMAP_PORT", ""),
        "imap_username": env_vars.get("IMAP_USERNAME", ""),
        "imap_password_set": is_set("IMAP_PASSWORD"),
        # Microsoft 365: ids aren't secret (no client secret: Harvey signs
        # in as a public client with PKCE); the token file means signed in.
        "m365_client_id": env_vars.get("M365_CLIENT_ID", ""),
        "m365_tenant_id": env_vars.get("M365_TENANT_ID", ""),
        "m365_authorized": (PROJECT_ROOT / "data" / "m365_token.json").is_file(),
        "reoon_api_key_set": is_set("REOON_API_KEY"),
        "zerobounce_api_key_set": is_set("ZEROBOUNCE_API_KEY"),
        "hunter_api_key_set": is_set("HUNTER_API_KEY"),
        "tinyfish_api_key_set": is_set("TINYFISH_API_KEY"),
    }


@app.post("/api/settings/env")
async def save_env_settings(request: Request):
    """Save environment variables to .env file."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid request body."}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"success": False, "message": "Invalid request body."}, status_code=400)
    async with _env_lock:
        updates = {}
        for key in ["INSTANTLY_API_KEY", "LINKEDIN_EMAIL", "LINKEDIN_PASSWORD",
                     "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN",
                     "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET",
                     "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD",
                     "IMAP_HOST", "IMAP_PORT", "IMAP_USERNAME", "IMAP_PASSWORD",
                     "M365_CLIENT_ID", "M365_TENANT_ID",
                     "REOON_API_KEY", "ZEROBOUNCE_API_KEY", "HUNTER_API_KEY",
                     "TINYFISH_API_KEY", "SITEWATCH_URL", "SITEWATCH_API_TOKEN"]:
            if key in data and data[key] is not None:
                value = data[key]
                if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                    return JSONResponse({"success": False, "message": f"{key} must be text."},
                                        status_code=400)
                value = str(value)
                # A port Harvey can't parse would stop it starting at all.
                if key.endswith("_PORT") and value.strip() and not value.strip().isdigit():
                    return JSONResponse({"success": False, "message": f"{key} must be a number."},
                                        status_code=400)
                if len(value) > 4096 or "\x00" in value:
                    return JSONResponse({"success": False, "message": f"{key} is invalid."},
                                        status_code=400)
                if key == "SITEWATCH_URL" and value.strip() and not value.strip().lower().startswith(("http://", "https://")):
                    return JSONResponse({"success": False, "message": "SITEWATCH_URL must start with http:// or https://."},
                                        status_code=400)
                # Strip newlines so a crafted value can't inject extra .env entries
                updates[key] = value.replace("\n", " ").replace("\r", " ").strip()
                if key == "SITEWATCH_URL":
                    updates[key] = updates[key].rstrip("/")
        if updates:
            try:
                _write_env_file(updates)
            except Exception as e:
                logger.warning("Failed to write .env: %s", e)
                return {"success": False, "message": "Could not write .env file."}
    return {"success": True}


# ── SiteWatch: website scans of prospects before writing ──

SITEWATCH_CODES = ("SITEWATCH_ISSUES", "SITEWATCH_STATUS", "SITEWATCH_UI_ISSUES", "SITEWATCH_SEO_ISSUES")
# The scan's separate UI and SEO reports: field prefix per signal code.
SITEWATCH_AUDIT_FIELDS = {"SITEWATCH_UI_ISSUES": "ui", "SITEWATCH_SEO_ISSUES": "seo"}


def _sitewatch_audit_fields(latest: dict, company_id: str) -> dict:
    """ui_issues / ui_text / ui_report_url and the seo_ ones, from the latest observations."""
    out = {}
    for code, prefix in SITEWATCH_AUDIT_FIELDS.items():
        o = latest.get((company_id, code)) or {}
        out.update({f"{prefix}_issues": o.get("value_num"), f"{prefix}_text": o.get("value_text") or "",
                    f"{prefix}_report_url": o.get("evidence_url") or ""})
    return out


def _audit_json(report) -> dict | None:
    if report is None:
        return None
    return {"score": report.score, "grade": report.grade, "report_url": report.report_url,
            "findings": report.findings}


def _sitewatch_client():
    """A client from the .env on disk (so a just-saved key is used), or None."""
    from harvey.integrations.sitewatch import SiteWatchClient
    env_vars = _read_env_file()
    url = (env_vars.get("SITEWATCH_URL") or "").strip().rstrip("/")
    token = (env_vars.get("SITEWATCH_API_TOKEN") or "").strip()
    return SiteWatchClient(url, token) if url and token else None


def _sitewatch_missing():
    return JSONResponse({"success": False,
                         "message": "Save the SiteWatch address and API token first."}, status_code=400)


_sitewatch_sync_lock = asyncio.Lock()


async def _sitewatch_sync():
    """Send approved leads' websites to SiteWatch and read back finished
    scans, the same stage `harvey run` runs every cycle. Returns
    (requested, observed), or None when scans before writing are off, not
    configured, or their signals aren't confirmed."""
    from harvey.collectors.sitewatch import active_client, run_sitewatch_stage
    from harvey.config import load_config, load_env
    try:
        config, env = load_config(), load_env()
    except Exception:
        return None
    env_vars = _read_env_file()                      # a key just saved on the page wins
    env.sitewatch_url = (env_vars.get("SITEWATCH_URL") or env.sitewatch_url or "").strip().rstrip("/")
    env.sitewatch_api_token = (env_vars.get("SITEWATCH_API_TOKEN") or env.sitewatch_api_token or "").strip()
    async with _sitewatch_sync_lock:
        state = _state()
        await state.init_db()
        client = await active_client(state, config, env)
        if client is None:
            return None
        return await run_sitewatch_stage(state, config, env, client=client)


@app.post("/api/sitewatch/sync")
async def sitewatch_sync():
    result = await _sitewatch_sync()
    if result is None:
        return {"success": True, "active": False}
    requested, observed = result
    return {"success": True, "active": True, "requested": requested, "observed": observed}


@app.get("/api/sitewatch")
async def get_sitewatch():
    """Connection, on/off, confirmed signals, and the latest scan per company."""
    env_vars = _read_env_file()
    enabled, config_file, config_error = False, "", ""
    try:
        from harvey.config import _find_config_file, load_config
        enabled = bool(load_config().sitewatch.enabled)
        config_file = Path(_find_config_file()).name
    except Exception as e:
        config_error = str(e)[:300]
    confirmed = [r["code"] for r in await query_db(
        f"SELECT code FROM signal_codes WHERE status = 'confirmed' AND code IN ({_in(SITEWATCH_CODES)})",
        SITEWATCH_CODES)]
    scans = await query_db(
        """SELECT s.company_id, s.scan_id, s.state, s.error, s.requested_at, s.finished_at,
                  c.name AS company, c.domain
           FROM sitewatch_scans s JOIN companies c ON c.id = s.company_id
           ORDER BY s.requested_at DESC LIMIT 100""")
    if scans:
        ids = [s["company_id"] for s in scans]
        obs = await query_db(
            f"""SELECT company_id, signal_code, value_num, value_text, evidence_url FROM observations
                WHERE company_id IN ({_in(ids)}) AND signal_code IN ({_in(SITEWATCH_CODES)})
                ORDER BY observed_at""", (*ids, *SITEWATCH_CODES))
        latest = {(o["company_id"], o["signal_code"]): o for o in obs}   # newest wins
        for s in scans:
            st = latest.get((s["company_id"], "SITEWATCH_STATUS")) or {}
            iss = latest.get((s["company_id"], "SITEWATCH_ISSUES")) or {}
            s.update({"status": st.get("value_text") or "", "score": st.get("value_num"),
                      "issues": iss.get("value_num"), "issue_text": iss.get("value_text") or "",
                      "report_url": iss.get("evidence_url") or st.get("evidence_url") or "",
                      **_sitewatch_audit_fields(latest, s["company_id"])})
    return {
        "url": (env_vars.get("SITEWATCH_URL") or "").strip(),
        "token_set": bool((env_vars.get("SITEWATCH_API_TOKEN") or "").strip()),
        "enabled": enabled, "config_file": config_file, "config_error": config_error,
        "confirmed": confirmed, "codes": list(SITEWATCH_CODES),
        "pending": sum(1 for s in scans if s["state"] == "pending"),
        "scans": scans,
    }


@app.post("/api/sitewatch/test")
async def sitewatch_test():
    from harvey.integrations.sitewatch import SiteWatchError
    client = _sitewatch_client()
    if client is None:
        return _sitewatch_missing()
    try:
        name = await client.ping()
    except SiteWatchError as e:
        return {"success": False, "message": str(e)}
    return {"success": True, "message": f'Connected. Prospect sites are filed under "{name}".'}


@app.post("/api/sitewatch/scan")
async def sitewatch_scan(request: Request):
    """Scan one website by hand. Poll /api/sitewatch/scans/{id} for the result."""
    from harvey.integrations.sitewatch import SiteWatchError
    try:
        data = await request.json()
    except Exception:
        data = None
    url = data.get("url") if isinstance(data, dict) else None
    if not isinstance(url, str) or not url.strip() or len(url) > 255:
        return JSONResponse({"success": False, "message": "Enter a website address."}, status_code=400)
    client = _sitewatch_client()
    if client is None:
        return _sitewatch_missing()
    try:
        started = await client.request_scan(url.strip())
    except SiteWatchError as e:
        return {"success": False, "message": str(e)}
    return {"success": True, "scan_id": started.scan_id, "state": started.state}


@app.get("/api/sitewatch/scans/{scan_id}")
async def sitewatch_scan_result(scan_id: int):
    from harvey.integrations.sitewatch import SiteWatchError
    client = _sitewatch_client()
    if client is None:
        return _sitewatch_missing()
    try:
        result = await client.get_scan(scan_id)
    except SiteWatchError as e:
        return {"success": False, "message": str(e)}
    return {"success": True, "scan_id": result.scan_id, "state": result.state, "done": result.done,
            "status": result.status, "score": result.score, "report_url": result.report_url,
            "error": result.error, "findings": result.findings,
            "advanced_report_url": result.advanced_report_url,
            "ui": _audit_json(result.ui), "seo": _audit_json(result.seo)}


@app.post("/api/sitewatch/enabled")
async def sitewatch_enabled(request: Request):
    """Turn scans before writing on or off. Turning on also confirms the
    SiteWatch signals (security, UI and SEO): this switch is the user saying
    yes to them."""
    from pydantic import ValidationError
    from harvey.config import ConfigError, set_sitewatch_enabled
    try:
        data = await request.json()
    except Exception:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("enabled"), bool):
        return JSONResponse({"success": False, "message": "Send enabled: true or false."}, status_code=400)
    on = data["enabled"]
    try:
        path = set_sitewatch_enabled(on)
    except (ConfigError, ValidationError) as e:
        return {"success": False, "message": str(e)[:300]}
    if on:
        from harvey.signals import seed_signal_catalog
        state = _state()
        await state.init_db()
        await seed_signal_catalog(state)
        for code in SITEWATCH_CODES:
            await state.set_signal_status(code, "confirmed")
    return {"success": True, "message": (
        f"Website scans turned {'on' if on else 'off'} in {path.name}. "
        "Restart Harvey (harvey run) for a running Harvey to pick it up.")}


@app.post("/api/trulyinbox/connect")
async def trulyinbox_connect(request: Request):
    """Check a TrulyInbox key against the sending mailbox, then save it to .env."""
    from harvey import deliverability as d
    from harvey.integrations import trulyinbox as ti
    try:
        data = await request.json()
    except Exception:
        data = None
    key = data.get("key") if isinstance(data, dict) else None
    if not isinstance(key, str) or not key.strip() or len(key) > 512:
        return JSONResponse({"success": False, "message": "Paste your TrulyInbox API key."},
                            status_code=400)
    key = key.strip()
    try:
        from harvey.config import load_config
        config = load_config()
        address, warmup = ti.sending_address(config), config.channels.email.warmup
    except Exception:
        address, warmup = "", None
    if not address:
        return JSONResponse({"success": False, "message": ti.NO_ADDRESS}, status_code=400)
    workspace = (_read_env_file().get("TRULYINBOX_WORKSPACE_ID", "")
                 or os.getenv("TRULYINBOX_WORKSPACE_ID", "")).strip()
    ti.forget_rejected(key)
    try:
        client = ti.TrulyInbox(key, workspace)
        health = await client.mailbox_health(address)
    except ti.TrulyInboxError as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=400)
    updates = {"TRULYINBOX_API_KEY": key}
    if not workspace:
        updates["TRULYINBOX_WORKSPACE_ID"] = client.workspace_id
    async with _env_lock:
        try:
            _write_env_file(updates)
        except Exception as e:
            logger.warning("Failed to write .env: %s", e)
            return {"success": False, "message": "Could not write .env file."}
    state = _state()
    await state.init_db()
    await ti.set_choice(state, "connected")
    return {"success": True, "message": d.check_warmup_service(health, warmup).detail}


@app.post("/api/trulyinbox/other")
async def trulyinbox_other():
    """The user warms their mailbox with another service: stop recommending one."""
    from harvey.integrations import trulyinbox as ti
    state = _state()
    await state.init_db()
    await ti.set_choice(state, "other")
    return {"success": True}


@app.post("/api/settings/test-instantly")
async def test_instantly(request: Request):
    """Test an Instantly API key."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    api_key = str(data.get("api_key", "") or "")
    if not api_key:
        return {"success": False, "message": "No API key provided."}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.instantly.ai/api/v2/accounts",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code == 200:
                return {"success": True, "message": "Connected to Instantly."}
            else:
                return {"success": False, "message": f"API returned {resp.status_code}. Check your key."}
    except Exception as e:
        return {"success": False, "message": f"Connection failed: {str(e)}"}


# ── Companies ──


# The Companies tab shows the best-fit companies, up to this many; the CSV has all.
COMPANIES_TAB_LIMIT = 2000


@app.get("/api/companies")
async def get_companies():
    """Companies with contact counts, fit score and register status, best fit first."""
    rows = await query_db("""
        SELECT c.*,
            (SELECT COUNT(*) FROM prospects p WHERE p.company_id = c.id) as contact_count,
            -- Same groups as the Account tab's Contacted / Replied filters.
            (SELECT COUNT(*) FROM prospects p WHERE p.company_id = c.id
                AND p.status IN ('contacted', 'replied', 'interested', 'meeting')) as contacted_count,
            (SELECT COUNT(*) FROM prospects p WHERE p.company_id = c.id
                AND p.status IN ('replied', 'interested', 'meeting')) as replied_count
        FROM companies c ORDER BY c.created_at DESC
    """)
    try:
        from harvey.config import load_config
        from harvey.register import build_register
        register = {r["id"]: r for r in await build_register(query_db, load_config())}
        for c in rows:
            r = register.get(c["id"])
            if r:
                c["fit"], c["fit_reasons"] = r["fit"], r["fit_reasons"]
                c["register_status"], c["register_why"] = r["status"], r["why_not_contacted"]
        rows.sort(key=lambda c: c.get("fit", 0), reverse=True)
    except Exception as e:
        # A config that won't load must not take the Companies tab down with it.
        logger.warning(f"Company fit unavailable: {e}")
    return rows[:COMPANIES_TAB_LIMIT]


@app.get("/api/export/companies.csv")
async def export_companies():
    """The company register: every company, why it fits, why it wasn't contacted."""
    from harvey.config import load_config
    from harvey.register import build_register, to_csv
    try:
        text = to_csv(await build_register(query_db, load_config()))
    except Exception as e:
        logger.warning("Company register export failed: %s", e)
        return PlainTextResponse(f"Company register export failed: {e}", status_code=500)
    return PlainTextResponse(
        "﻿" + text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="company-register.csv"'},
    )


@app.get("/api/companies/{company_id}/profile")
async def get_company_profile(company_id: str):
    """One company's facts for its detail card: the register row plus what
    Harvey observed on its site (latest value per signal, with the page)."""
    rows = await query_db("SELECT * FROM companies WHERE id = ?", (company_id,))
    if not rows:
        return JSONResponse({"error": "not found"}, status_code=404)
    observations = await query_db(
        "SELECT o.signal_code, o.value_text, o.value_num, o.evidence_url, o.observed_at, "
        "COALESCE(s.label, o.signal_code) AS label FROM observations o "
        "LEFT JOIN signal_codes s ON s.code = o.signal_code "
        "WHERE o.company_id = ? AND o.prospect_id = '' ORDER BY o.observed_at DESC LIMIT 200",
        (company_id,),
    )
    register = {}
    try:
        from harvey.config import load_config
        from harvey.register import build_register
        register = next(iter(await build_register(query_db, load_config(), company_id)), {})
    except Exception as e:
        logger.warning(f"Company profile register unavailable: {e}")
    return {"company": rows[0], "register": register, "observations": observations}


@app.get("/api/companies/{company_id}/contacts")
async def get_company_contacts(company_id: str):
    """Get all contacts for a specific company."""
    rows = await query_db(
        "SELECT * FROM prospects WHERE company_id = ? ORDER BY score DESC",
        (company_id,),
    )
    return rows


# ── Feedback ──


@app.post("/api/feedback")
async def add_feedback(request: Request):
    """Add a comment/feedback on any entity."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    entity_type = str(data.get("entity_type", "") or "")[:50]
    entity_id = str(data.get("entity_id", "") or "")[:100]
    comment = str(data.get("comment", "") or "").strip()[:4000]
    if not comment:
        return {"success": False, "message": "Comment is required."}
    feedback_id = uuid.uuid4().hex[:12]
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(str(DB_PATH)) as db:
            # Ensure the table exists so feedback works even on a fresh install
            await db.execute(
                """CREATE TABLE IF NOT EXISTS feedback (
                    id TEXT PRIMARY KEY,
                    entity_type TEXT,
                    entity_id TEXT,
                    comment TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )"""
            )
            await db.execute(
                "INSERT INTO feedback (id, entity_type, entity_id, comment) VALUES (?, ?, ?, ?)",
                (feedback_id, entity_type, entity_id, comment),
            )
            await db.commit()
    except Exception as e:
        logger.warning("Failed to save feedback: %s", e)
        return {"success": False, "message": "Could not save feedback."}
    return {"success": True, "id": feedback_id}


@app.get("/api/feedback/{entity_type}/{entity_id}")
async def get_feedback(entity_type: str, entity_id: str):
    """Get feedback for an entity."""
    rows = await query_db(
        "SELECT * FROM feedback WHERE entity_type = ? AND entity_id = ? ORDER BY created_at DESC",
        (entity_type, entity_id),
    )
    return rows


# ── Harvey Controls ──


@app.get("/api/harvey/status")
async def get_harvey_status():
    """Check if Harvey is currently running."""
    pid = _check_harvey_pid()
    started = _harvey_started_at.isoformat() if _harvey_started_at else None
    return {"running": pid is not None, "pid": pid, "started_at": started}


@app.post("/api/harvey/start")
async def start_harvey():
    """Start Harvey's heartbeat loop as a subprocess."""
    global _harvey_process, _harvey_started_at

    if _check_harvey_pid():
        return {"success": False, "message": "Harvey is already running."}
    from harvey.main import _needs_setup
    if _needs_setup():
        return {"success": False,
                "message": "Harvey isn't set up yet. Run `harvey setup` in a terminal first."}

    # Ensure data dir exists
    (PROJECT_ROOT / "data").mkdir(parents=True, exist_ok=True)

    # The native Claude CLI installer puts claude in ~/.local/bin without
    # always adding it to PATH; the heartbeat can't think without it.
    env = os.environ.copy()
    local_bin = Path.home() / ".local" / "bin"
    if not shutil.which("claude") and local_bin.is_dir():
        env["PATH"] = str(local_bin) + os.pathsep + env.get("PATH", "")

    try:
        log_handle = open(LOG_FILE, "a", encoding="utf-8")
        try:
            _harvey_process = subprocess.Popen(
                [sys.executable, "-m", "harvey", "run"],
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
                env=env,
            )
        finally:
            # Child holds its own copies of the fds; don't leak ours.
            log_handle.close()
    except Exception as e:
        logger.warning("Failed to start Harvey: %s", e)
        return {"success": False, "message": f"Failed to start Harvey: {e}"}
    _harvey_started_at = datetime.now()
    # The loop writes its own PID file once it holds the loop lock. Writing
    # the child's PID here would overwrite a running loop's record if this
    # child then finds the lock taken and exits.
    return {"success": True, "pid": _harvey_process.pid}


@app.post("/api/harvey/stop")
async def stop_harvey():
    """Stop the Harvey subprocess."""
    global _harvey_process, _harvey_started_at

    pid = _check_harvey_pid()
    if not pid:
        return {"success": False, "message": "Harvey is not running."}

    own = _harvey_process if _harvey_process and _harvey_process.pid == pid else None

    def alive() -> bool:
        # Our own child is reaped by poll(); left unreaped it is a zombie,
        # which still answers signal 0.
        if own is not None:
            return own.poll() is None
        return pid_alive(pid)

    try:
        os.kill(pid, signal.SIGTERM)
        # Wait briefly for graceful shutdown
        for _ in range(10):
            if not alive():
                break
            await asyncio.sleep(0.5)
        else:
            # Force kill if still running (Windows has no SIGKILL; SIGTERM
            # there is already TerminateProcess)
            try:
                os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            except OSError:
                pass
            await asyncio.sleep(0.5)
    except OSError as e:
        logger.warning("Couldn't signal Harvey (pid %s): %s", pid, e)

    if alive():
        # Started by another user, or from an elevated terminal on Windows.
        return {"success": False,
                "message": f"Couldn't stop Harvey (pid {pid}). Stop it where it was started."}

    _harvey_process = None
    _harvey_started_at = None
    PID_FILE.unlink(missing_ok=True)

    return {"success": True}


@app.get("/api/harvey/logs")
async def get_harvey_logs():
    """Get recent log lines."""
    if not LOG_FILE.exists():
        return {"lines": []}
    try:
        # Tail only the last 64KB so a huge log file never blocks the UI
        with open(LOG_FILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            text = f.read().decode("utf-8", errors="replace")
        lines = text.strip().splitlines()[-100:]
        return {"lines": lines}
    except Exception:
        return {"lines": []}


# ── Pipeline Data (existing endpoints) ──


def _pipeline_bucket(status: str | None, stage: str | None, intent: str | None,
                     email_status: str | None = None) -> str:
    """One funnel stage per contact, from its status and latest conversation."""
    status = status or "new"
    if status in ("new", "queued"):
        return "to_write"
    if status == "contacted":
        # A bounce leaves the status alone and marks the address invalid.
        return "closed_out" if email_status == "invalid" else "contacted"
    if status == "meeting":
        return "ready_to_meet"
    if status == "replied" and stage != "closed_lost":
        if stage == "closing":
            return "ready_to_meet"
        if intent == "interested" or stage in ("presenting", "negotiating"):
            return "interested"
        return "replied"
    return "closed_out"   # lost, opted out, bounced, unwritable


@app.get("/api/stats")
async def get_stats():
    """Pipeline overview stats."""
    try:
        prospects = await query_db(
            "SELECT status, COUNT(*) as count FROM prospects GROUP BY status"
        )
        prospect_total = sum(r["count"] for r in prospects)
        prospect_map = {r["status"]: r["count"] for r in prospects}

        campaigns = await query_db(
            "SELECT status, COUNT(*) as count FROM campaigns GROUP BY status"
        )
        campaign_map = {r["status"]: r["count"] for r in campaigns}

        conversations = await query_db(
            "SELECT status, COUNT(*) as count FROM conversations GROUP BY status"
        )
        convo_map = {r["status"]: r["count"] for r in conversations}

        # Where each contact stands. Prospect status alone stops at
        # "replied"; how a reply went lives on its latest conversation.
        rows = await query_db("""
            SELECT p.status, p.email_status, l.stage, l.intent, COUNT(*) AS count FROM prospects p
            LEFT JOIN (SELECT prospect_id, stage, intent, ROW_NUMBER() OVER (
                         PARTITION BY prospect_id ORDER BY updated_at DESC, created_at DESC) AS rn
                       FROM conversations) l ON l.prospect_id = p.id AND l.rn = 1
            GROUP BY p.status, p.email_status, l.stage, l.intent
        """)
        pipeline = dict.fromkeys(
            ("to_write", "contacted", "replied", "interested", "ready_to_meet", "closed_out"), 0)
        for r in rows:
            pipeline[_pipeline_bucket(r["status"], r["stage"], r["intent"],
                                      r["email_status"])] += r["count"]

        actions = await query_db("SELECT COUNT(*) as count FROM actions")
        action_count = actions[0]["count"] if actions else 0

        # usage_log rows are keyed by the LOCAL date (increment_usage /
        # get_usage_today use date.today()); SQLite's date('now') is UTC and
        # can select a different day's counts on either side of UTC midnight.
        usage = await query_db(
            "SELECT claude_calls, codex_calls FROM usage_log WHERE date = ?",
            (date.today().isoformat(),),
        )
        usage_today = (usage[0]["claude_calls"] or 0) if usage else 0
        codex_today = (usage[0]["codex_calls"] or 0) if usage else 0

        return {
            "prospects": {"total": prospect_total, "by_status": prospect_map},
            "campaigns": {"total": sum(campaign_map.values()), "by_status": campaign_map},
            "conversations": {"total": sum(convo_map.values()), "by_status": convo_map},
            "pipeline": pipeline,
            "actions_total": action_count,
            "claude_calls_today": usage_today,
            "codex_calls_today": codex_today,
            "brain_provider": _active_provider(),
            "brain_status": await _brain_status(),
        }
    except Exception as e:
        return {"error": str(e)}


_USAGE_SUM = (
    "COUNT(DISTINCT CASE WHEN session_id != '' THEN session_id ELSE id END) AS calls, "
    "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
    "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
    "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, "
    "COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens, "
    "ROUND(COALESCE(SUM(cost_usd), 0), 4) AS cost_usd"
)

_quota_clients: dict = {}


def _active_brain():
    """The brain config harvey.yaml selects (None if unreadable)."""
    try:
        from harvey.config import load_brain_config
        return load_brain_config()
    except Exception:
        return None


async def _brain_status() -> dict:
    """Last recorded brain health transition ({} if none): lets the
    dashboard say "Codex paused: <why>" instead of looking healthy."""
    from harvey.brain import BRAIN_STATUS_KEY, active_brain_status
    rows = await query_db("SELECT value FROM settings WHERE key = ?", (BRAIN_STATUS_KEY,))
    # Only the configured provider's health (a stale Codex pause isn't
    # Claude's).
    return active_brain_status(rows[0]["value"], _active_provider()) if rows else {}


def _active_provider() -> str:
    brain = _active_brain()
    return brain.provider if brain else ""


def _quota_client_for(provider: str, model: str = ""):
    """One cached quota reader per provider (and Codex model). Each reads
    only its own subscription, so a Claude gauge is never shown as Codex or
    vice versa, and Codex shows the bucket metering the configured model."""
    key = (provider, model if provider == "codex" else "")
    if key not in _quota_clients:
        if provider == "codex":
            from harvey.codex import CodexClient, CodexQuotaClient
            _quota_clients[key] = CodexQuotaClient(CodexClient(model=model))
        elif provider == "claude":
            from harvey.integrations.quota import QuotaClient
            _quota_clients[key] = QuotaClient()
        else:
            return None
    return _quota_clients[key]


@app.get("/api/usage")
async def get_usage():
    """Token/cost accounting + live subscription quota for the Usage tab."""

    totals = {}
    for label, where in (
        ("today", "date(created_at) = date('now')"),
        ("week", "created_at >= datetime('now', '-7 days')"),
        ("month", "created_at >= datetime('now', '-30 days')"),
    ):
        rows = await query_db(f"SELECT {_USAGE_SUM} FROM usage_events WHERE {where}")
        totals[label] = rows[0] if rows else {}

    def grouped(expr, alias):
        return (
            f"SELECT {expr} AS {alias}, {_USAGE_SUM} FROM usage_events "
            f"WHERE created_at >= datetime('now', '-30 days') "
            f"GROUP BY {alias} ORDER BY output_tokens DESC LIMIT 25"
        )

    by_agent = await query_db(grouped("CASE WHEN agent = '' THEN 'other' ELSE agent END", "agent"))
    by_task = await query_db(grouped("CASE WHEN task = '' THEN 'other' ELSE task END", "task"))
    by_model = await query_db(grouped("CASE WHEN model = '' THEN 'unknown' ELSE model END", "model"))
    by_provider = await query_db(grouped("provider", "provider"))
    by_day = await query_db(
        f"SELECT date(created_at) AS day, {_USAGE_SUM} FROM usage_events "
        f"WHERE created_at >= datetime('now', '-30 days') "
        f"GROUP BY day ORDER BY day ASC"
    )

    brain = _active_brain()
    provider = brain.provider if brain else ""
    quota = None
    try:
        client = _quota_client_for(provider, brain.model if brain else "")
        if client is not None:
            quota = await client.get_utilization()
    except Exception as e:
        logger.debug("Quota lookup failed: %s", e)

    return {
        "provider": provider,
        "quota": quota,
        # Whose quota this is, even when it couldn't be read, so the UI shows
        # "unavailable" for the active provider instead of hiding the gauge.
        "quota_provider": provider,
        "quota_status": ("available" if quota else "unavailable") if provider else "",
        "by_provider": by_provider,
        "totals": totals,
        "by_day": by_day,
        "by_agent": by_agent,
        "by_task": by_task,
        "by_model": by_model,
    }


@app.get("/api/prospects")
async def get_prospects():
    rows = await query_db("SELECT * FROM prospects ORDER BY created_at DESC LIMIT 200")
    # Someone whose opt-out was undone keeps a way to see what happened.
    undone = {r["pid"] for r in await query_db(
        "SELECT DISTINCT json_extract(details_json, '$.prospect_id') AS pid FROM actions "
        "WHERE action_type = 'opt_out_undone' AND json_valid(details_json)")}
    for r in rows:
        r["opt_out_undone"] = r.get("id") in undone
    return rows


def _state():
    from harvey.state import StateManager
    return StateManager(db_path=str(DB_PATH))


# What a reviewer needs next to a draft to check its claims against the facts.
OUTBOX_CONTEXT_SIGNALS = (
    "TECH_STACK", "INCUMBENT_AGENCY", "HIRING_ROLE", "RUNNING_GOOGLE_ADS", "NO_ONLINE_BOOKING",
    "SITEWATCH_ISSUES", "SITEWATCH_STATUS", "SITEWATCH_UI_ISSUES", "SITEWATCH_SEO_ISSUES",
)


def _in(ids) -> str:
    return ",".join("?" for _ in ids)


async def _with_context(items: list[dict]) -> list[dict]:
    """Attach the campaign, company, contact and signals behind each draft."""
    if not items:
        return items
    pids = sorted({i["prospect_id"] for i in items if i.get("prospect_id")})
    cids = sorted({i["campaign_id"] for i in items if i.get("campaign_id")})

    people = {r["id"]: r for r in (await query_db(
        "SELECT p.id, p.first_name, p.last_name, p.title, p.source, p.email_status, "
        "p.score, p.personalization_notes, p.company_id, c.name AS company_name, "
        "c.domain, c.website, c.description, c.industry AS found_by "
        "FROM prospects p LEFT JOIN companies c ON c.id = p.company_id "
        f"WHERE p.id IN ({_in(pids)})", tuple(pids)) if pids else [])}
    campaigns = {r["id"]: r["name"] for r in (await query_db(
        f"SELECT id, name FROM campaigns WHERE id IN ({_in(cids)})",
        tuple(cids)) if cids else [])}

    company_ids = sorted({p["company_id"] for p in people.values() if p.get("company_id")})
    signals: dict[str, dict[str, str]] = {}
    if company_ids:
        rows = await query_db(
            "SELECT o.company_id, o.signal_code, o.value_num, o.value_text, "
            "o.observed_at, o.run_id, "
            "COALESCE(NULLIF(sc.label, ''), o.signal_code) AS label FROM observations o "
            "LEFT JOIN signal_codes sc ON sc.code = o.signal_code "
            f"WHERE o.company_id IN ({_in(company_ids)}) "
            f"AND o.signal_code IN ({_in(OUTBOX_CONTEXT_SIGNALS)}) "
            "ORDER BY o.observed_at, o.id",
            (*company_ids, *OUTBOX_CONTEXT_SIGNALS),
        )
        # Oldest first, so the latest observation wins. A signal can be seen
        # several times at once (one HIRING_ROLE row per open role): every
        # value from the latest sighting shows, not just one of them.
        latest: dict[tuple, tuple] = {}
        for r in rows:
            if r["value_text"]:
                value = r["value_text"]
            elif r["value_num"] is None:
                value = ""
            else:
                value = "yes" if r["value_num"] else "no"
            key = (r["company_id"], r["label"])
            seen = (r["run_id"] or r["observed_at"])
            prev_seen, values = latest.get(key, (None, []))
            if seen != prev_seen:
                values = []
            if value not in values:
                values = values + [value]
            latest[key] = (seen, values)
        for (company_id, label), (_, values) in latest.items():
            signals.setdefault(company_id, {})[label] = ", ".join(v for v in values if v)

    for item in items:
        p = people.get(item.get("prospect_id")) or {}
        item["context"] = {
            "campaign": campaigns.get(item.get("campaign_id"), ""),
            "company": p.get("company_name", ""),
            "website": p.get("website") or (f"https://{p['domain']}" if p.get("domain") else ""),
            "description": p.get("description", ""),
            "found_by": p.get("found_by", ""),
            "contact": " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x),
            "contact_title": p.get("title", ""),
            "contact_source": p.get("source", ""),
            "email_status": p.get("email_status", ""),
            "score": p.get("score"),
            "notes": p.get("personalization_notes", ""),
            "signals": signals.get(p.get("company_id"), {}),
        }
    return items


@app.get("/api/outbox")
async def get_outbox_api():
    """Outbox queue + kill-switch state for the Outbox tab."""
    try:
        state = _state()
        await state.init_db()
        # A send cut off mid-way shows up under Didn't send even when Harvey
        # isn't running.
        await state.fail_stale_sending_outbox()
        return {
            **_compliance_view(),
            "paused": await state.get_setting("sending_paused"),
            "pending": await _with_context(
                await state.get_outbox(status="pending_review", limit=100)),
            # Mail going out right now leads the list.
            "approved": (await state.get_outbox(status="sending", limit=10)
                         + await state.get_outbox(status="approved", limit=50)),
            "sent": (await query_db(
                "SELECT * FROM outbox WHERE status = 'sent' "
                "ORDER BY sent_at DESC LIMIT 25")),
            "failed": (await query_db(
                "SELECT * FROM outbox WHERE status IN ('failed','rejected','cancelled') "
                "ORDER BY updated_at DESC LIMIT 25")),
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/sending-mode")
async def get_sending_mode():
    """Approval (every email waits for you) or autopilot, and what that means here."""
    from harvey.config import load_config
    from harvey.gate import _booking_prefix, allowed_link_domains
    path = _resolve_config_file()
    try:
        config = load_config(str(path))
    except Exception as e:
        return JSONResponse({"error": f"Couldn't read {path.name}: {e}"}, status_code=500)
    state = _state()
    await state.init_db()
    return {
        "mode": "approval" if config.channels.email.require_approval else "autopilot",
        "provider": config.channels.email.provider,
        "file": path.name,
        "your_domains": sorted(allowed_link_domains(config)),
        "booking_page": _booking_prefix(config),
        "held": await state.count_held_for_review(),
    }


@app.post("/api/sending-mode")
async def set_sending_mode(request: Request):
    """Switch it. Autopilot needs {"mode": "autopilot", "confirm": "autopilot"};
    {"mode": "approval"} works at once and sends back to the Outbox whatever
    autopilot approved that hasn't gone out."""
    from harvey.config import ConfigError, set_require_approval
    try:
        body = await request.json()
    except ValueError:
        body = None
    mode = body.get("mode") if isinstance(body, dict) else None
    if mode not in ("approval", "autopilot"):
        return _refuse('expected {"mode": "approval"} or {"mode": "autopilot", "confirm": "autopilot"}', 400)
    if mode == "autopilot" and body.get("confirm") != "autopilot":
        return _refuse('Type "autopilot" to confirm: Harvey will send email without asking you.', 400)
    path = _resolve_config_file()
    try:
        set_require_approval(mode == "approval", path)
    except ConfigError as e:
        return _refuse(str(e), 409)
    state = _state()
    await state.init_db()
    requeued = await state.requeue_autopilot_approved() if mode == "approval" else 0
    await state.log_action("sending_mode", "dashboard", {"mode": mode, "file": path.name,
                                                         "requeued": requeued})
    out = {"success": True, "mode": mode}
    if mode == "approval":
        out["requeued"] = requeued
    return out


@app.post("/api/outbox/approve-all")
async def outbox_approve_all():
    try:
        state = _state()
        await state.init_db()
        n = await state.approve_outbox()
        # Held ones (possible planted link or leak) are approved one by one.
        if n:
            wake_harvey()
        return {"success": True, "approved": n, "held": await state.count_held_for_review()}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/outbox/{item_id}/approve")
async def outbox_approve(item_id: str):
    try:
        state = _state()
        await state.init_db()
        n = await state.approve_outbox(item_id)
        if n:
            wake_harvey()
        return {"success": bool(n)}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/outbox/{item_id}/edit")
async def outbox_edit(item_id: str, request: Request):
    """Rewrite a draft awaiting review. Never approves it."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    subject = str(body.get("subject") or "").strip()
    text = str(body.get("body") or "").strip()
    try:
        state = _state()
        await state.init_db()
        item = await state.get_outbox_item(item_id)
        if not item or item.get("status") != "pending_review":
            return JSONResponse({"success": False,
                                 "message": "Only drafts awaiting review can be edited."},
                                status_code=409)
        if item.get("kind") == "reply":
            # Gmail threads a reply by its subject: a new one would land as a
            # fresh, unthreaded email to someone who wrote in. Only the body
            # of a reply is editable; its stored subject is kept exactly.
            subject = item.get("subject") or ""
        if not subject.strip() or not text:
            return JSONResponse({"success": False,
                                 "message": "Subject and body are both required."},
                                status_code=400)
        # The sender re-checks every email before it goes out; an edit it
        # would block is refused now, while the reviewer can still fix it,
        # instead of failing quietly at send time. Content rules only: the
        # recipient isn't something an edit can change.
        from harvey.gate import pre_send_check
        checked = text
        footer = _compliance_view()["footer"]
        if item.get("kind") == "sequence" and footer and not text.rstrip().endswith(footer):
            checked = text.rstrip() + "\n\n" + footer  # as the Sender will send it
        gate = pre_send_check("reviewer@example.com", subject, checked)
        if not gate:
            return JSONResponse({"success": False,
                                 "message": "The sender would have blocked this: "
                                            + "; ".join(gate.reasons)},
                                status_code=400)
        if not await state.edit_outbox_draft(item_id, subject, text):
            return JSONResponse({"success": False,
                                 "message": "Only drafts awaiting review can be edited."},
                                status_code=409)
        await state.log_action("outbox_edited", "dashboard", {
            "outbox_id": item_id, "to_email": item.get("to_email", ""),
            "old_subject": item.get("subject", ""), "old_body": item.get("body", ""),
        })
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


# A hand send is only offered to someone Harvey could still email: never to
# someone who replied, opted out, bounced or was already contacted.
HAND_SENDABLE_STATUSES = ("new", "queued")
HAND_SENDABLE_EMAIL = ("verified", "risky")


def _harvey_can_send() -> bool:
    """Can Harvey send email itself (a connected mailbox, or Instantly)?

    Sending by hand from the dashboard is only offered when it can't: with
    two senders, any timing gap between them emails someone twice. If the
    config can't be read, the answer is yes (the safe side: no hand sends).
    """
    try:
        from harvey.config import load_config, load_env
        from harvey.integrations.mail_provider import NATIVE_PROVIDERS, get_mail_provider
        config, env = load_config(), load_env()
        from harvey.config import legacy_sending_held
        email = config.channels.email
        if not email.enabled or legacy_sending_held(config):
            return False
        if (email.provider or "").strip().lower() in NATIVE_PROVIDERS:
            provider = get_mail_provider(config, env)
            return bool(provider and provider.is_configured())
        return bool(env.instantly_api_key)
    except Exception:
        return True


def _send_as() -> str:
    """The address Harvey sends as, so Gmail opens the right account."""
    try:
        from harvey.config import load_config
        return load_config().persona.email or ""
    except Exception:
        return ""


def _compliance_view() -> dict:
    """What the Sender adds under each cold email, so a reviewer sees the
    email as it will leave, and the warning when the address is missing."""
    try:
        from harvey.config import compliance_footer_lines, compliance_notice, load_config
        config = load_config()
        return {"footer": "\n".join(compliance_footer_lines(config)),
                "compliance_notice": compliance_notice(config)}
    except Exception:
        return {"footer": "", "compliance_notice": ""}


async def _harvey_committed_ids() -> set[str]:
    """Everyone Harvey has approved or sent mail for (Outbox or Instantly).

    Approved mail can be going out at this moment, so these people are past
    the point where a hand send can be recorded safely. So is anyone whose
    send was cut off mid-way: it may have reached them. A draft that only
    awaits review isn't: marking the person sent cancels it.
    """
    from harvey.state import MAYBE_SENT_PREFIX
    ids = {r["prospect_id"] for r in await query_db(
        "SELECT DISTINCT prospect_id FROM outbox WHERE status IN ('approved', 'sending', 'sent') "
        "OR (status = 'failed' AND substr(error, 1, ?) = ?)",
        (len(MAYBE_SENT_PREFIX), MAYBE_SENT_PREFIX))}
    for row in await query_db(
            "SELECT prospect_ids_json FROM campaigns WHERE COALESCE(instantly_campaign_id, '') != ''"):
        try:
            found = json.loads(row.get("prospect_ids_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(found, list):
            ids.update(i for i in found if isinstance(i, str))
    return ids


async def _hand_sent_ids() -> set[str]:
    from harvey.state import MANUAL_SEND_KEY
    rows = await query_db("SELECT key FROM settings WHERE key LIKE ?", (MANUAL_SEND_KEY + "%",))
    return {r["key"][len(MANUAL_SEND_KEY):] for r in rows}


async def _harvey_sent_to(prospect_id: str) -> bool:
    """Did Harvey itself email this person (native send, or an Instantly campaign)?

    A send in flight, or cut off mid-way, counts: it may have reached them.
    """
    from harvey.state import MAYBE_SENT_PREFIX
    if await query_db(
            "SELECT 1 FROM outbox WHERE prospect_id = ? AND (status IN ('sending', 'sent') "
            "OR (status = 'failed' AND substr(error, 1, ?) = ?)) LIMIT 1",
            (prospect_id, len(MAYBE_SENT_PREFIX), MAYBE_SENT_PREFIX)):
        return True
    for row in await query_db(
            "SELECT prospect_ids_json FROM campaigns WHERE COALESCE(instantly_campaign_id, '') != ''"):
        try:
            if prospect_id in json.loads(row.get("prospect_ids_json") or "[]"):
                return True
        except (json.JSONDecodeError, TypeError):
            continue
    return False


def _refuse(message: str, code: int = 409):
    return JSONResponse({"success": False, "message": message}, status_code=code)


@app.post("/api/prospects/{prospect_id}/sent")
async def mark_prospect_sent(prospect_id: str, request: Request):
    """The user sent this email by hand (e.g. from Gmail): record it, or undo it."""
    from harvey.state import HAND_SENT_REASON, MANUAL_SEND_KEY
    try:
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if body is None:
            body = {}
        if not isinstance(body, dict) or not isinstance(body.get("sent", True), bool):
            return _refuse('expected {"sent": true|false}', 400)
        sent = body.get("sent", True)
        rows = await query_db("SELECT status FROM prospects WHERE id = ?", (prospect_id,))
        if not rows:
            return _refuse("unknown prospect", 404)
        current = rows[0].get("status") or "new"
        state = _state()
        await state.init_db()
        key = MANUAL_SEND_KEY + prospect_id
        marked = bool(await state.get_setting(key))

        if sent:
            if marked:
                return {"success": True, "sent": True, "already": True}
            if current not in HAND_SENDABLE_STATUSES:
                # A reply, opt-out or close is never touched; neither is
                # someone Harvey already emailed.
                return {"success": True, "sent": True, "status": current}
            # Allowed even when Harvey can send: recording "I already emailed
            # them" can only ever stop Harvey sending. Not once Harvey has
            # approved or sent mail to them, though: that may be going out now.
            # The marker goes down FIRST: a drain that checks after this sees
            # it and cancels; one that checked before only holds an approved
            # row, which the check below then sees.
            await state.set_setting(key, current)
            if prospect_id in await _harvey_committed_ids():
                await state.delete_setting(key)
                return _refuse("Harvey has already approved or sent an email to this person. "
                               "Check the Outbox.")
            await state.update_prospect_status(prospect_id, "contacted")
            # Anything Harvey had queued for them must not go out as well.
            changed = await state.cancel_pending_outbox_for_prospect(
                prospect_id, reason=HAND_SENT_REASON)
        else:
            if not marked:
                if current in HAND_SENDABLE_STATUSES:
                    return {"success": True, "sent": False}
                if current == "contacted":
                    # Setting them back would make Harvey write and send again.
                    return _refuse("This wasn't marked as sent by hand here, so it stays sent.")
                return {"success": True, "sent": True, "status": current}
            if current != "contacted":
                # They replied, opted out or bounced since: that stands.
                return {"success": True, "sent": True, "status": current}
            previous = await state.get_setting(key) or "queued"
            await state.update_prospect_status(
                prospect_id, previous if previous in HAND_SENDABLE_STATUSES else "queued")
            await state.delete_setting(key)
            # What marking sent cancelled comes back for review, never straight out.
            changed = await state.restore_hand_cancelled_outbox(
                prospect_id, reason=HAND_SENT_REASON)
        if sent:
            # Open in Gmail adds today's footer; remember it as Harvey's text.
            footer = _compliance_view()["footer"]
            await state.remember_footer_lines(footer.split("\n"))
        await state.log_action(
            action_type="manual_send" if sent else "manual_send_undone",
            agent="user",
            details={"prospect_id": prospect_id, "outbox_items": changed},
        )
        return {"success": True, "sent": sent}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/outbox/{item_id}/reject")
async def outbox_reject(item_id: str):
    try:
        state = _state()
        await state.init_db()
        if not await state.reject_outbox_item(item_id):
            return _refuse("That email is already sending or sent, so it can't be rejected.")
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/sending/{action}")
async def sending_toggle(action: str):
    if action not in ("pause", "resume"):
        return JSONResponse({"success": False, "message": "unknown action"}, status_code=400)
    try:
        state = _state()
        await state.init_db()
        if action == "pause":
            await state.set_setting("sending_paused", "paused from dashboard")
        else:
            await state.set_setting("sending_paused", "")
            await state.set_setting("bounce_count", "0")
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.get("/api/export/prospects.csv")
async def export_prospects(all: bool = False, min_score: int = 0, email_status: str = ""):
    """Sequencer-ready CSV download of the prospect list."""
    from harvey.state import StateManager
    from harvey.export import export_prospects_csv

    state = StateManager(db_path=str(DB_PATH))
    try:
        await state.init_db()
        statuses = [s.strip() for s in email_status.split(",") if s.strip()] or None
        _, text = await export_prospects_csv(
            state, email_statuses=statuses, min_score=min_score, include_all=all,
        )
    except Exception as e:
        logger.warning("Prospect export failed: %s", e)
        text = ""
    return PlainTextResponse(
        text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="prospects.csv"'},
    )


@app.get("/api/campaigns")
async def get_campaigns():
    rows = await query_db("SELECT * FROM campaigns ORDER BY created_at DESC LIMIT 100")
    harvey_sends = _harvey_can_send()
    send_as = "" if harvey_sends else _send_as()
    footer = _compliance_view()["footer"]
    committed = await _harvey_committed_ids()
    hand_sent = await _hand_sent_ids()
    for row in rows:
        try:
            row["sequence"] = json.loads(row.get("sequence_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            row["sequence"] = []
        try:
            row["prospect_ids"] = json.loads(row.get("prospect_ids_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            row["prospect_ids"] = []
        # Who each draft goes to, so the UI can open it in the user's own mail client.
        ids = row["prospect_ids"]
        row["recipients"] = await query_db(
            f"SELECT id, email, first_name, last_name, company, title, status, email_status, "
            f"source, personalization_notes FROM prospects "
            f"WHERE id IN ({','.join('?' * len(ids))}) AND email != ''",
            tuple(ids),
        ) if ids else []
        row["send_as"] = send_as
        # A cold email sent by hand needs the same footer Harvey adds.
        row["footer"] = footer
        for r in row["recipients"]:
            r["sent_by_hand"] = r["id"] in hand_sent
            # "I already emailed them": always recordable before Harvey commits.
            r["can_mark_sent"] = (
                r["id"] not in hand_sent and r["id"] not in committed
                and (r.get("status") or "new") in HAND_SENDABLE_STATUSES
            )
            # The Gmail button: only when Harvey can't send itself.
            r["can_send_by_hand"] = (
                r["can_mark_sent"] and not harvey_sends
                and (r.get("email_status") or "guess") in HAND_SENDABLE_EMAIL
            )
    return rows


@app.get("/api/prospects/{prospect_id}/opt-out")
async def get_opt_out(prospect_id: str):
    """Why this person is opted out: the rule, the words and their message."""
    rows = await query_db("SELECT status FROM prospects WHERE id = ?", (prospect_id,))
    if not rows:
        return _refuse("unknown prospect", 404)
    status = rows[0].get("status") or "new"
    state = _state()
    await state.init_db()
    history = await state.opt_out_history(prospect_id)
    last = next((h for h in reversed(history) if h["action"] == "opt_out"), None)
    out = {
        "opted_out": status == "opted_out", "status": status, "recorded": bool(last),
        "at": None, "rule": None, "matched": "", "why": "", "message": "",
        "conversation_id": "", "history": history,
    }
    if last:
        out.update({k: last.get(k) or out[k] for k in
                    ("rule", "matched", "why", "message", "conversation_id")})
        out["at"] = last["at"]
    elif status == "opted_out" or history:
        # Opted out before Harvey kept reasons (and maybe undone since):
        # show the last thing they wrote.
        for c in await query_db(
                "SELECT id, thread_json, updated_at FROM conversations "
                "WHERE prospect_id = ? ORDER BY updated_at DESC", (prospect_id,)):
            try:
                thread = json.loads(c.get("thread_json") or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            theirs = [m for m in thread if isinstance(m, dict) and m.get("sender") == "prospect"]
            if theirs:
                out.update(message=str(theirs[-1].get("content") or ""),
                           conversation_id=c["id"], at=theirs[-1].get("timestamp") or c.get("updated_at"))
                break
    return out


@app.post("/api/prospects/{prospect_id}/opt-out/undo")
async def undo_opt_out(prospect_id: str, request: Request):
    """The owner says this opt-out was a mistake. Needs {"confirm": true}."""
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or body.get("confirm") is not True:
        return _refuse('expected {"confirm": true}', 400)
    rows = await query_db("SELECT email FROM prospects WHERE id = ?", (prospect_id,))
    if not rows:
        return _refuse("unknown prospect", 404)
    state = _state()
    await state.init_db()
    history = await state.opt_out_history(prospect_id)
    last = next((h for h in reversed(history) if h["action"] == "opt_out"), {})
    if not await state.undo_opt_out(prospect_id, details={
            "prospect_email": rows[0].get("email") or "",
            "previous_why": last.get("why", ""), "previous_at": last.get("at", "")}):
        return _refuse("This person isn't opted out.")
    return {"success": True, "status": "replied"}


@app.get("/api/conversations")
async def get_conversations(awaiting: bool = False):
    rows = await query_db("""
        SELECT c.*, p.first_name, p.last_name, p.email as prospect_email, p.company,
               p.status as prospect_status
        FROM conversations c
        LEFT JOIN prospects p ON c.prospect_id = p.id
        ORDER BY c.updated_at DESC LIMIT 100
    """)
    # The stored thread holds replies and Harvey's answers; the cold emails
    # a reply answers live in the Outbox. Attach those (sent ones only) and
    # any answer still waiting for approval, so the page shows the exchange.
    pids = sorted({r["prospect_id"] for r in rows if r.get("prospect_id")})
    emails: dict[str, list] = {}
    if pids:
        for e in await query_db(
                "SELECT prospect_id, campaign_id, step, subject, body, sent_at FROM outbox "
                f"WHERE kind = 'sequence' AND status = 'sent' AND prospect_id IN ({_in(pids)}) "
                "ORDER BY sent_at", tuple(pids)):
            emails.setdefault(e.pop("prospect_id"), []).append(e)
    cids = [r["id"] for r in rows]
    drafts = {d.pop("conversation_id"): d for d in (await query_db(
        "SELECT id, conversation_id, subject, body, send_at FROM outbox "
        f"WHERE kind = 'reply' AND status = 'pending_review' AND conversation_id IN ({_in(cids)}) "
        "ORDER BY created_at", tuple(cids)) if cids else [])}
    for row in rows:
        try:
            row["thread"] = json.loads(row.get("thread_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            row["thread"] = []
        # A prospect in two campaigns: show the sequence this reply answers.
        camp = row.get("campaign_id") or ""
        row["emails"] = [{k: v for k, v in e.items() if k != "campaign_id"}
                         for e in emails.get(row.get("prospect_id"), [])
                         if not camp or e.get("campaign_id") == camp]
        row["draft"] = drafts.get(row["id"])
    unanswered = await _unanswered_conversation_ids() if rows else set()
    # Replies on their way out show in the thread at once, marked sending.
    outgoing: dict[str, list] = {}
    for o in (await query_db(
            "SELECT conversation_id, body, created_at, campaign_id FROM outbox "
            f"WHERE kind = 'reply' AND status IN ('approved', 'sending') AND conversation_id IN ({_in(cids)}) "
            "ORDER BY created_at", tuple(cids)) if cids else []):
        outgoing.setdefault(o["conversation_id"], []).append(
            {"sender": "you" if o["campaign_id"] == "manual-reply" else "harvey",
             "content": o["body"], "timestamp": o["created_at"]})
    modes = {m["key"][len("reply_mode:"):]: m["value"] for m in await query_db(
        "SELECT key, value FROM settings WHERE key LIKE 'reply_mode:%'")}
    seen = await _convo_seen()
    # What a conversation with no choice of its own does: Settings → Sending.
    default_mode = "approve" if _require_approval_now() else "auto"
    for row in rows:
        row["new"] = row["id"] in unanswered
        row["unseen"] = _is_unseen(row.get("thread"), seen.get(row["id"], ""))
        row["outgoing"] = outgoing.get(row["id"], [])
        row["reply_mode"] = modes.get(row["id"], "")
        row["reply_mode_default"] = default_mode
    if awaiting:
        rows += await _awaiting_reply()
    return rows


async def _awaiting_reply() -> list[dict]:
    """Prospects Harvey emailed who haven't replied yet, shaped like a
    conversation so the tab can show every sent email, not only the ones
    that got an answer. Read-only: no conversation row is created, and the
    card moves to the real conversation the moment a reply arrives."""
    waiting: dict[str, dict] = {}
    # Only people still waiting: anyone whose company has a conversation (even
    # one past the tab's latest 100), a bounced address, or who's otherwise done
    # isn't. Bounded because this runs on every load of the tab.
    sent = await query_db(
        "SELECT o.prospect_id, o.step, o.subject, o.body, o.sent_at, "
        "p.first_name, p.last_name, p.email AS prospect_email, p.company, "
        "p.status AS prospect_status FROM outbox o "
        "JOIN prospects p ON p.id = o.prospect_id "
        "WHERE o.kind = 'sequence' AND o.status = 'sent' "
        "AND p.status NOT IN ('replied', 'opted_out', 'lost', 'meeting', 'closed') "
        "AND COALESCE(p.email_status, '') != 'invalid' "
        "AND NOT EXISTS (SELECT 1 FROM conversations c JOIN prospects q ON q.id = c.prospect_id "
        "WHERE q.id = p.id OR (q.company_id = p.company_id AND COALESCE(p.company_id, '') != '')) "
        "ORDER BY o.sent_at DESC LIMIT 2000")
    for e in reversed(sent):
        pid = e["prospect_id"]
        row = waiting.setdefault(pid, {
            "id": "awaiting-" + pid, "prospect_id": pid, "campaign_id": "",
            "channel": "email", "intent": "", "stage": "", "status": "awaiting_reply",
            "thread": [], "draft": None, "emails": [],
            "first_name": e.get("first_name") or "", "last_name": e.get("last_name") or "",
            "prospect_email": e.get("prospect_email") or "", "company": e.get("company") or "",
            "prospect_status": e.get("prospect_status") or "",
        })
        row["emails"].append({"step": e["step"], "subject": e["subject"],
                              "body": e["body"], "sent_at": e["sent_at"]})
        row["created_at"] = row.get("created_at") or e["sent_at"]
        row["updated_at"] = e["sent_at"]
    return sorted(waiting.values(), key=lambda r: r.get("updated_at") or "", reverse=True)


@app.post("/api/conversations/{convo_id}/status")
async def set_conversation_status(convo_id: str, request: Request):
    """Close a conversation, or hand one Harvey escalated back to it.

    Harvey answers new replies only on open conversations, so "open" means
    "Harvey, carry on" and "closed" means it's done."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    status = data.get("status") if isinstance(data, dict) else None
    if status not in ("open", "closed"):
        return JSONResponse({"success": False, "message": "status must be open or closed"},
                            status_code=400)
    rows = await query_db("SELECT status FROM conversations WHERE id = ?", (convo_id,))
    if not rows:
        return JSONResponse({"success": False, "message": "No such conversation."},
                            status_code=404)
    state = _state()
    await state.update_conversation(convo_id, status=status)
    await state.log_action("conversation_status", "dashboard",
                           {"conversation_id": convo_id, "from": rows[0]["status"], "to": status})
    return {"success": True, "status": status}


REPLY_MODE_KEY = "reply_mode:"   # + conversation id -> "auto" | "approve"
CONVO_SEEN_KEY = "convo_seen:"   # + conversation id -> time of the last customer message you opened


def _last_prospect_ts(thread) -> str:
    """When the customer last wrote in this thread, or ""."""
    times = [str(m.get("timestamp") or "") for m in (thread or [])
             if isinstance(m, dict) and m.get("sender") == "prospect"]
    return max(times) if times else ""


def _is_unseen(thread, seen: str) -> bool:
    """The customer wrote something you haven't opened yet."""
    last = _last_prospect_ts(thread)
    return bool(last) and last > (seen or "")


async def _convo_seen() -> dict[str, str]:
    return {r["key"][len(CONVO_SEEN_KEY):]: r["value"] or "" for r in await query_db(
        "SELECT key, value FROM settings WHERE key LIKE ?", (CONVO_SEEN_KEY + "%",))}


async def _unseen_conversation_ids() -> set[str]:
    """Conversations with a customer message you haven't opened (the
    Conversations badge), whether or not Harvey has answered it."""
    seen = await _convo_seen()
    out = set()
    for r in await query_db("SELECT id, thread_json FROM conversations"):
        try:
            thread = json.loads(r.get("thread_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        if _is_unseen(thread, seen.get(r["id"], "")):
            out.add(r["id"])
    return out


@app.post("/api/conversations/{convo_id}/seen")
async def mark_conversation_seen(convo_id: str):
    """You opened this conversation: its messages so far are seen."""
    rows = await query_db("SELECT thread_json FROM conversations WHERE id = ?", (convo_id,))
    if not rows:
        return JSONResponse({"success": False, "message": "No such conversation."}, status_code=404)
    try:
        thread = json.loads(rows[0].get("thread_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        thread = []
    last = _last_prospect_ts(thread)
    if last:
        await _state().set_setting(CONVO_SEEN_KEY + convo_id, last)
    return {"success": True, "unseen": len(await _unseen_conversation_ids())}


@app.post("/api/conversations/{convo_id}/mode")
async def set_conversation_reply_mode(convo_id: str, request: Request):
    """Per conversation: Harvey answers new replies on its own ("auto"), or
    drafts them for you to approve ("approve"). "" follows Settings."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    mode = (data or {}).get("mode", "") if isinstance(data, dict) else ""
    if mode not in ("auto", "approve", ""):
        return JSONResponse({"success": False, "message": "mode must be auto or approve"}, status_code=400)
    if not await query_db("SELECT id FROM conversations WHERE id = ?", (convo_id,)):
        return JSONResponse({"success": False, "message": "No such conversation."}, status_code=404)
    state = _state()
    if mode:
        await state.set_setting(REPLY_MODE_KEY + convo_id, mode)
    else:
        await state.delete_setting(REPLY_MODE_KEY + convo_id)
    await state.log_action("conversation_reply_mode", "dashboard", {"conversation_id": convo_id, "mode": mode})
    return {"success": True, "mode": mode}


async def _unanswered_conversation_ids() -> set[str]:
    """Conversations where the customer wrote last and nothing has gone back
    yet: new messages waiting on you (the Conversations badge)."""
    rows = await query_db("SELECT id, thread_json, status FROM conversations WHERE status != 'closed'")
    out = set()
    for r in rows:
        try:
            thread = json.loads(r.get("thread_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        if thread and thread[-1].get("sender") == "prospect":
            out.add(r["id"])
    if out:
        answered = await query_db(
            f"SELECT conversation_id FROM outbox WHERE kind = 'reply' AND status IN ('approved', 'sending') "
            f"AND conversation_id IN ({','.join('?' * len(out))})", tuple(out))
        out -= {a["conversation_id"] for a in answered}
    return out


@app.post("/api/conversations/{convo_id}/reply")
async def reply_to_conversation(convo_id: str, request: Request):
    """Send your own reply from the Conversations tab.

    It replaces any draft Harvey had waiting, is queued already approved
    (you wrote it), threads under the last email like Harvey's replies do,
    and still passes the send gate (do-not-contact, opt-outs, length)."""
    from harvey.config import load_config
    from harvey.gate import pre_send_check

    try:
        data = await request.json()
    except Exception:
        data = {}
    text = str((data or {}).get("body") or "").strip() if isinstance(data, dict) else ""
    if not text:
        return JSONResponse({"success": False, "message": "Write a reply first."}, status_code=400)
    state = _state()
    await state.init_db()
    convo = await state.get_conversation(convo_id)
    if not convo:
        return JSONResponse({"success": False, "message": "No such conversation."}, status_code=404)
    prospect = await state.get_prospect(convo.prospect_id)
    if not prospect or not prospect.email:
        return JSONResponse({"success": False, "message": "This contact has no email address."},
                            status_code=409)
    if prospect.status == "opted_out":
        return JSONResponse({"success": False, "message": "They opted out. Harvey won't email them."},
                            status_code=409)
    config = load_config()
    # Thread under the last reply Harvey queued here, else the cold email they answered.
    meta = await query_db(
        "SELECT subject, thread_ref, in_reply_to, message_id FROM outbox "
        "WHERE conversation_id = ? AND kind = 'reply' ORDER BY created_at DESC LIMIT 1", (convo_id,))
    if not meta:
        meta = await query_db(
            "SELECT subject, thread_ref, message_id AS in_reply_to FROM outbox "
            "WHERE prospect_id = ? AND status = 'sent' ORDER BY sent_at DESC LIMIT 1", (prospect.id,))
    m = meta[0] if meta else {}
    subject = m.get("subject") or "your note"
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    gate = pre_send_check(prospect.email, subject, text, kind="reply",
                          do_not_contact=config.compliance.do_not_contact)
    if not gate.ok:
        return JSONResponse({"success": False, "message": "Not sent: " + "; ".join(gate.reasons)},
                            status_code=422)
    for d in await query_db("SELECT id FROM outbox WHERE conversation_id = ? AND kind = 'reply' "
                            "AND status = 'pending_review'", (convo_id,)):
        await state.reject_outbox_item(d["id"])
    from harvey.agents.sender import MANUAL_REPLY
    item_id = await state.add_outbox_item(
        prospect_id=prospect.id, conversation_id=convo_id, kind="reply", campaign_id=MANUAL_REPLY,
        to_email=prospect.email, subject=subject, body=text,
        send_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        status="approved", provider=config.channels.email.provider,
        thread_ref=m.get("thread_ref") or "", in_reply_to=m.get("in_reply_to") or "",
        approved_by="you")
    await state.update_conversation(convo_id, status="open")
    wake_harvey()
    await state.log_action("manual_reply", "dashboard",
                           {"conversation_id": convo_id, "prospect_email": prospect.email})
    return {"success": True, "id": item_id}


@app.get("/api/trend")
async def get_trend(days: int = 14, tz: int = 0):
    """Daily counts for the last `days` days in the viewer's time zone
    (`tz`: minutes east of UTC; stored times are UTC): contacts found,
    emails sent, first replies. Days with nothing are 0."""
    from datetime import datetime, timedelta, timezone
    days = max(1, min(90, days))
    tz = max(-840, min(840, tz))
    shift = f"{tz:+d} minutes"
    today = (datetime.now(timezone.utc) + timedelta(minutes=tz)).date()
    labels = [(today - timedelta(days=n)).isoformat() for n in range(days - 1, -1, -1)]

    async def per_day(table: str, column: str, where: str = "") -> list[int]:
        rows = await query_db(
            f"SELECT date({column}, ?) AS d, COUNT(*) AS n FROM {table} "
            f"WHERE date({column}, ?) >= ? {where} GROUP BY d", (shift, shift, labels[0]))
        counts = {r["d"]: r["n"] for r in rows}
        return [counts.get(day, 0) for day in labels]

    return {
        "days": labels,
        "found": await per_day("prospects", "created_at"),
        "sent": await per_day("outbox", "sent_at", "AND status = 'sent'"),
        "replies": await per_day("conversations", "created_at"),
    }


@app.get("/api/activity")
async def get_activity():
    rows = await query_db("SELECT * FROM actions ORDER BY created_at DESC LIMIT 100")
    for row in rows:
        try:
            row["details"] = json.loads(row.get("details_json", "{}"))
        except (json.JSONDecodeError, TypeError):
            row["details"] = {}
    return rows


@app.get("/api/activity/feed")
async def get_activity_feed(limit: int = 400, checkins: bool = False):
    """The Activity page: the latest actions (Harvey's own check-ins only when
    asked for), plus last-7-days totals and when Harvey last checked in."""
    limit = max(1, min(int(limit), 2000))
    where = "" if checkins else "WHERE agent != 'main'"
    rows = await query_db(f"SELECT * FROM actions {where} ORDER BY created_at DESC LIMIT ?", (limit,))
    for row in rows:
        try:
            row["details"] = json.loads(row.get("details_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            row["details"] = {}
        row.pop("details_json", None)

    week = await query_db(
        """SELECT action_type, agent, details_json FROM actions
           WHERE datetime(created_at) >= datetime('now', '-7 days') AND agent != 'main'""")
    summary = {"leads_found": 0, "emails_sent": 0, "replies": 0, "problems": 0}
    for r in week:
        t = r["action_type"]
        if t == "prospect":
            try:
                summary["leads_found"] += int(json.loads(r["details_json"] or "{}").get("prospects_found") or 0)
            except (ValueError, TypeError, json.JSONDecodeError):
                pass
        elif t == "email_sent":
            summary["emails_sent"] += 1
        elif t in ("reply", "reply_queued", "manual_reply"):
            summary["replies"] += 1
        elif t in ("bounce", "write_failed", "escalation", "reply_after_opt_out"):
            summary["problems"] += 1
    last = await query_db("SELECT created_at, action_type FROM actions WHERE agent = 'main' "
                          "ORDER BY created_at DESC LIMIT 1")
    checked = await query_db("SELECT COUNT(*) AS n FROM actions WHERE agent = 'main' "
                             "AND datetime(created_at) >= datetime('now', '-7 days')")
    summary["last_checkin"] = last[0]["created_at"] if last else None
    summary["checkins_week"] = checked[0]["n"] if checked else 0
    return {"items": rows, "summary": summary}


# ── Dashboard UI ──


# ── Discovery: the provider menu, an estimate, and a run ──
#
# This is the only stage that spends money, so the UI never starts one without
# showing what it will cost first.

_discovery_task: asyncio.Task | None = None
_discovery_report: dict | None = None
_discovery_kind = "discover"   # or "profile": reading sites has no stop switch


def _discovery_queries(body: dict, config):
    from harvey.collectors.discover import build_queries, preset_problem

    preset = str(body.get("preset") or "")
    problem = preset_problem(preset, str(body.get("provider") or ""), config=config)
    if problem:
        raise ValueError(problem)
    cities = [c.strip() for c in (body.get("cities") or []) if c.strip()] or None
    return build_queries(
        config, cities=cities,
        depth=int(body.get("depth") or 30),
        limit=int(body.get("limit") or 100),
        preset=preset,
    )


@app.get("/api/discover/providers")
async def get_discovery_providers():
    """What each source does, what it costs, and whether it's ready to use."""
    try:
        from harvey.collectors.discover import DEFAULT_PROVIDER, provider_menu
        from harvey.config import load_env

        state = _state()
        await state.init_db()
        return {
            "providers": provider_menu(load_env().model_dump()),
            "default": DEFAULT_PROVIDER,
            "selected": await state.get_setting("discovery_provider") or DEFAULT_PROVIDER,
            "paused": await state.get_setting("discovery_paused"),
            "running": bool(_discovery_task and not _discovery_task.done()),
            "running_kind": _discovery_kind,
            "unprofiled": await state.count_companies_needing_profile(),
            "last_report": _discovery_report,
        }
    except Exception as e:
        logger.exception("discovery providers failed")
        return {"providers": [], "error": str(e)}


@app.post("/api/discover/estimate")
async def estimate_discovery(request: Request):
    """Projected spend and the exact query list, before anything is called."""
    try:
        from harvey.collectors.discover import PROVIDERS, estimate_cost
        from harvey.config import load_config

        body = await request.json()
        provider = body.get("provider") or ""
        if provider not in PROVIDERS:
            return JSONResponse({"error": f"unknown provider {provider!r}"},
                                status_code=400)

        try:
            queries = _discovery_queries(body, load_config())
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {
            "provider": provider,
            "queries": [q.keyword() for q in queries],
            "query_count": len(queries),
            "estimated_cost": round(estimate_cost(provider, queries), 4),
            "free": PROVIDERS[provider].estimate(queries) == 0,
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/discover/run")
async def start_discovery(request: Request):
    """Kick off a run in the background and hand back immediately.

    Discovery takes minutes, not milliseconds — holding the request open
    would just time out. Progress shows up in the run log.
    """
    global _discovery_task, _discovery_report

    if _discovery_task and not _discovery_task.done():
        return JSONResponse({"success": False, "message": "a run is already going"},
                            status_code=409)
    try:
        from harvey.collectors.discover import PROVIDERS
        from harvey.config import load_config
        from harvey.pipeline import run_prospecting

        body = await request.json()
        provider = body.get("provider") or ""
        if provider not in PROVIDERS:
            return JSONResponse({"success": False,
                                 "message": f"unknown provider {provider!r}"},
                                status_code=400)

        config = load_config()
        try:
            queries = _discovery_queries(body, config)
        except ValueError as e:
            return JSONResponse({"success": False, "message": str(e)}, status_code=400)
        max_spend = float(body.get("max_spend") or 1.0)

        state = _state()
        await state.init_db()
        await state.set_setting("discovery_provider", provider)
        await state.set_setting("discovery_paused", "")

        global _discovery_kind
        _discovery_kind = "discover"

        async def _go():
            global _discovery_report
            try:
                # Discovery chains straight into profiling: reading the sites
                # is free, and it is what makes the results worth anything.
                result = await run_prospecting(state, config, provider, queries,
                                               max_spend=max_spend)
                _discovery_report = {
                    **(result.discover or {}),
                    "profiled_companies": result.profiled_companies,
                    "profile_observations": result.profile_observations,
                    "errors": result.errors,
                }
            except Exception as exc:
                logger.exception("discovery run failed")
                _discovery_report = {"errors": [str(exc)], "stopped": "failed"}

        _discovery_report = None
        _discovery_task = asyncio.create_task(_go())
        return {"success": True, "queries": len(queries)}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


LEAD_REQUEST_PROMPT = """You turn a salesperson's plain-language lead request into a search spec
for finding businesses. Request:

{text}

Return JSON with these keys:
- "industries": 1-5 short business categories as people search them on a map ("dentist", "plumber", "hvac contractor")
- "cities": 1-5 places, each as "City, Region/Country" ("Phoenix, AZ", "Delhi, India")
- "titles": 2-5 job titles of the person to email ("Owner", "Practice Manager")
- "company_size": e.g. "1-50 employees", or "" if not stated
- "summary": one sentence restating what you'll look for
Only use what the request says or clearly implies; never invent a city it doesn't name."""


@app.get("/api/leads/found")
async def get_found_leads():
    """Every lead Harvey found, with its business type and approval, for the
    Leads page (filtered and sorted in the page)."""
    rows = await query_db(
        """SELECT p.id, p.first_name, p.last_name, p.title, p.email, p.email_status,
                  p.status, COALESCE(p.lead_approval, 'approved') AS approval, p.created_at,
                  p.source, p.company_id,
                  COALESCE(NULLIF(c.name, ''), p.company) AS company,
                  c.domain, c.website, c.location,
                  COALESCE(NULLIF(TRIM(c.industry), ''), NULLIF(TRIM(p.industry), ''), 'Other') AS business_type
           FROM prospects p LEFT JOIN companies c ON c.id = p.company_id
           ORDER BY p.created_at DESC LIMIT 5000""")
    counts = {"pending": 0, "approved": 0, "rejected": 0}
    for r in rows:
        if r["approval"] in counts:
            counts[r["approval"]] += 1

    # Each company's SiteWatch scan: its state, and the latest result.
    company_ids = sorted({r["company_id"] for r in rows if r["company_id"]})
    reports = {}
    if company_ids:
        for s in await query_db(
                f"SELECT company_id, state, error FROM sitewatch_scans WHERE company_id IN ({_in(company_ids)})",
                tuple(company_ids)):
            reports[s["company_id"]] = {"state": s["state"], "error": s["error"] or ""}
        for o in await query_db(
                f"""SELECT company_id, signal_code, value_num, value_text, evidence_url FROM observations
                    WHERE company_id IN ({_in(company_ids)}) AND signal_code IN ({_in(SITEWATCH_CODES)})
                    ORDER BY observed_at""", (*company_ids, *SITEWATCH_CODES)):
            rep = reports.setdefault(o["company_id"], {"state": "done", "error": ""})
            prefix = SITEWATCH_AUDIT_FIELDS.get(o["signal_code"])
            if prefix:                              # the separate UI / SEO report
                rep.update({f"{prefix}_issues": o["value_num"], f"{prefix}_text": o["value_text"] or "",
                            f"{prefix}_report_url": o["evidence_url"] or ""})
                continue
            if o["signal_code"] == "SITEWATCH_STATUS":
                rep.update(status=o["value_text"] or "", score=o["value_num"])
            else:
                rep.update(issues=o["value_num"], issue_text=o["value_text"] or "")
            if o["evidence_url"]:
                rep["report_url"] = o["evidence_url"]
    for r in rows:
        r["report"] = reports.get(r["company_id"])
    return {"leads": rows, "counts": counts}


@app.get("/api/leads/{prospect_id}/details")
async def get_lead_details(prospect_id: str):
    """One lead in full, for the Leads page: why Harvey picked it (your
    request, how it was found, its fit), what the company seems to need (its
    own description, website problems, signals), and the emails written for
    it."""
    from harvey.collectors.sitewatch import website_issue_facts
    from harvey.register import build_register
    rows = await query_db("SELECT * FROM prospects WHERE id = ?", (prospect_id,))
    if not rows:
        return JSONResponse({"success": False, "message": "No such lead."}, status_code=404)
    p = rows[0]
    cid = p.get("company_id") or ""
    company = (await query_db("SELECT * FROM companies WHERE id = ?", (cid,)) or [{}])[0] if cid else {}

    state = _state()
    await state.init_db()
    request_text, request_summary = "", ""
    try:
        saved = json.loads(await state.get_setting("lead_request") or "null") or {}
        request_text = saved.get("text") or ""
        request_summary = (saved.get("spec") or {}).get("summary") or ""
    except (ValueError, TypeError, AttributeError):
        pass

    fit, fit_reasons, hiring = None, [], ""
    if cid:
        try:
            from harvey.config import load_config
            reg = await build_register(query_db, load_config(), cid)
            if reg:
                fit, fit_reasons, hiring = reg[0]["fit"], reg[0]["fit_reasons"], reg[0]["hiring"]
        except Exception as e:
            logger.warning("lead details: register failed: %s", e)

    issues = [f.lstrip("- ").strip() for f in (await website_issue_facts(state, cid) or [])] if cid else []
    signals = []
    if cid:
        for o in await query_db(
                f"""SELECT sc.label, o.value_text, o.value_num FROM observations o
                   JOIN signal_codes sc ON sc.code = o.signal_code
                   WHERE o.company_id = ? AND o.signal_code NOT IN ({_in(SITEWATCH_CODES)})
                   ORDER BY o.observed_at DESC LIMIT 60""", (cid, *SITEWATCH_CODES)):
            value = o["value_text"] or ("yes" if o["value_num"] == 1 else "" if o["value_num"] in (None, 0) else str(o["value_num"]))
            if value and (o["label"], value) not in signals:
                signals.append((o["label"], value))
    messages = await query_db(
        "SELECT step, kind, subject, body, status, send_at, sent_at FROM outbox WHERE prospect_id = ? "
        "ORDER BY step, created_at", (prospect_id,))
    return {
        "success": True,
        "purpose": {
            "your_request": request_text, "request_summary": request_summary,
            "business_type": company.get("industry") or p.get("industry") or "",
            "found_via": company.get("source") or p.get("source") or "",
            "found_url": company.get("source_url") or p.get("source_url") or "",
            "fit": fit, "fit_reasons": fit_reasons,
        },
        "needs": {
            "about": company.get("description") or "",
            "website_problems": issues,
            "hiring": hiring,
            "notes": p.get("personalization_notes") or "",
            "signals": [{"label": l, "value": v[:200]} for l, v in signals[:12]],
        },
        "messages": messages,
    }


@app.post("/api/leads/approval")
async def set_leads_approval(request: Request):
    """Approve, reject or reset leads: {ids: [...], approval: approved|rejected|pending}."""
    from harvey.state import LEAD_APPROVALS
    try:
        data = await request.json()
    except Exception:
        data = None
    ids = data.get("ids") if isinstance(data, dict) else None
    approval = data.get("approval") if isinstance(data, dict) else None
    if (not isinstance(ids, list) or not ids or len(ids) > 1000
            or not all(isinstance(i, str) and 0 < len(i) <= 64 for i in ids)):
        return JSONResponse({"success": False, "message": "Pick at least one lead."}, status_code=400)
    if approval not in LEAD_APPROVALS:
        return JSONResponse({"success": False, "message": "Unknown approval."}, status_code=400)
    state = _state()
    await state.init_db()
    changed = await state.set_lead_approval(ids, approval)
    if changed:
        try:
            await state.log_action(action_type="lead_approval", agent="dashboard",
                                   details={"approval": approval, "count": changed})
        except Exception as e:
            logger.warning("Couldn't log lead approval: %s", e)
    word = {"approved": "approved", "rejected": "rejected", "pending": "moved back to pending"}[approval]
    message = f"{changed} lead{'s' if changed != 1 else ''} {word}."
    sent = 0
    if approval == "approved" and changed:
        # Approved leads go to SiteWatch now, so the report is ready by the
        # time Harvey writes; the Leads page reads the results back.
        try:
            synced = await _sitewatch_sync()
        except Exception as e:
            logger.warning("SiteWatch after approval failed: %s", e)
            synced = None
        if synced is not None:
            sent = synced[0]
            message += (f" {sent} website{'s' if sent != 1 else ''} sent to SiteWatch for a report."
                        if sent else " Their websites already have a SiteWatch report or one on the way.")
        else:
            message += " Harvey writes to them on its next cycles."
    return {"success": True, "changed": changed, "sent_to_sitewatch": sent, "message": message}


def _config_env():
    """Harvey's config and env, with SiteWatch keys from the .env on disk
    (a key just saved on a page wins)."""
    from harvey.config import load_config, load_env
    config, env = load_config(), load_env()
    env_vars = _read_env_file()
    env.sitewatch_url = (env_vars.get("SITEWATCH_URL") or env.sitewatch_url or "").strip().rstrip("/")
    env.sitewatch_api_token = (env_vars.get("SITEWATCH_API_TOKEN") or env.sitewatch_api_token or "").strip()
    return config, env


def _lead_ids(data):
    ids = data.get("ids") if isinstance(data, dict) else None
    if (not isinstance(ids, list) or not ids or len(ids) > 1000
            or not all(isinstance(i, str) and 0 < len(i) <= 64 for i in ids)):
        return None
    return ids


@app.post("/api/leads/report")
async def leads_report_action(request: Request):
    """For leads whose website report failed or is stuck: 'rescan' asks
    SiteWatch again; 'skip' lets Harvey write to them without a report."""
    try:
        data = await request.json()
    except Exception:
        data = None
    ids = _lead_ids(data)
    action = data.get("action") if isinstance(data, dict) else None
    if ids is None or action not in ("rescan", "skip"):
        return JSONResponse({"success": False, "message": "Pick leads and rescan or skip."}, status_code=400)
    rows = await query_db(f"SELECT DISTINCT company_id FROM prospects WHERE id IN ({_in(ids)}) "
                          "AND COALESCE(company_id, '') != ''", tuple(ids))
    companies = [r["company_id"] for r in rows]
    state = _state()
    await state.init_db()
    if action == "skip":
        for cid in companies:
            await state.set_sitewatch_scan(cid, None, state="skipped", error="You chose to write without a report")
        return {"success": True, "message": f"{len(companies)} compan{'y' if len(companies) == 1 else 'ies'} will be "
                                            "written to without a website report."}
    await state.sitewatch_reset(companies)
    synced = await _sitewatch_sync()
    if synced is None:
        return {"success": False, "message": "SiteWatch scans are off or not set up (SiteWatch page)."}
    return {"success": True, "message": f"{synced[0]} website{'s' if synced[0] != 1 else ''} sent to SiteWatch again."}


_campaign_task: asyncio.Task | None = None
_campaign_report: dict | None = None


@app.post("/api/campaigns/create")
async def create_campaign(request: Request):
    """Write a campaign now for the leads you picked: approved, not yet
    contacted, a deliverable address and (with SiteWatch on) a finished
    website report. Runs in the background; GET this path for the result."""
    global _campaign_task, _campaign_report
    from harvey.state import WRITABLE_EMAIL_STATUSES
    if _campaign_task and not _campaign_task.done():
        return JSONResponse({"success": False, "message": "A campaign is already being written."}, status_code=409)
    try:
        data = await request.json()
    except Exception:
        data = None
    ids = _lead_ids(data)
    if ids is None:
        return JSONResponse({"success": False, "message": "Pick at least one lead."}, status_code=400)
    try:
        config, env = _config_env()
    except Exception as e:
        return JSONResponse({"success": False, "message": f"Harvey's config can't be read: {e}"}, status_code=400)

    from harvey.collectors.sitewatch import reports_required
    state = _state()
    await state.init_db()
    need_report = await reports_required(state, config, env)
    ready, skipped = [], {"not approved": 0, "already contacted": 0, "no usable email": 0,
                          "waiting for website report": 0}
    for pid in ids:
        p = await state.get_prospect(pid)
        if not p:
            continue
        if (p.lead_approval or "approved") != "approved":
            skipped["not approved"] += 1
        elif p.status != "new":
            skipped["already contacted"] += 1
        elif not p.email or (p.email_status or "guess") not in WRITABLE_EMAIL_STATUSES:
            skipped["no usable email"] += 1
        elif need_report and not await state.sitewatch_report_ready(p.company_id):
            skipped["waiting for website report"] += 1
        else:
            ready.append(pid)
    skipped = {k: v for k, v in skipped.items() if v}
    if not ready:
        return {"success": False, "started": False, "skipped": skipped,
                "message": "None of these leads can be written to yet: "
                           + ", ".join(f"{v} {k}" for k, v in skipped.items()) + "."}

    async def _go():
        global _campaign_report
        from harvey.agents.writer import Writer
        from harvey.brain import Brain
        _campaign_report = {"running": True, "requested": len(ready), "skipped": skipped}
        try:
            writer = Writer(Brain(state, config.brain), state, config, env)
            await writer.run(only_ids=ready)
            _campaign_report = {"running": False, "requested": len(ready), "written": writer.written,
                                "skipped": skipped}
        except Exception as e:
            logger.exception("create campaign failed")
            _campaign_report = {"running": False, "requested": len(ready), "written": 0,
                                "skipped": skipped, "error": str(e)[:300]}

    _campaign_report = {"running": True, "requested": len(ready), "skipped": skipped}
    _campaign_task = asyncio.create_task(_go())
    return {"success": True, "started": True, "ready": len(ready), "skipped": skipped,
            "message": f"Writing a campaign for {len(ready)} lead{'s' if len(ready) != 1 else ''}…"}


@app.get("/api/campaigns/create")
async def create_campaign_status():
    return {"running": bool(_campaign_task and not _campaign_task.done()), "report": _campaign_report}


@app.get("/api/leads/request")
async def get_lead_request():
    state = _state()
    await state.init_db()
    raw = await state.get_setting("lead_request")
    try:
        saved = json.loads(raw) if raw else None
    except ValueError:
        saved = None
    # What Harvey searches with right now, from the config it reads: the page
    # shows this, so it never disagrees with a hand-edited config.
    current = None
    try:
        from harvey.config import load_config
        cfg = load_config()
        current = {"industries": list(cfg.icp.industries or []),
                   "cities": list(cfg.icp.geography or []),
                   "titles": list(cfg.icp.titles or []),
                   "company_size": cfg.icp.company_size or "",
                   "auto_send": not cfg.channels.email.require_approval}
    except Exception as e:
        logger.warning("Lead request: config unavailable: %s", e)
    return {"request": saved, "current": current,
            "running": bool(_discovery_task and not _discovery_task.done()),
            "report": _discovery_report}


@app.post("/api/leads/request")
async def find_leads(request: Request):
    """Plain-language requirement -> Claude turns it into targeting (saved to
    the config Harvey reads) -> discovery and site reading start. Scout then
    finds the people to email on the heartbeat."""
    global _discovery_task, _discovery_report, _discovery_kind
    try:
        data = await request.json()
    except Exception:
        data = {}
    data = data if isinstance(data, dict) else {}
    text = str(data.get("text") or "").strip()
    if len(text) < 5:
        return JSONResponse({"success": False, "message": "Describe the leads you want first."},
                            status_code=400)
    raw_locs = data.get("locations") or []
    if isinstance(raw_locs, str):
        raw_locs = raw_locs.replace(";", "\n").split("\n")
    locations = [str(x).strip() for x in raw_locs if str(x).strip()][:10]
    auto_send = bool(data.get("auto_send"))
    run = data.get("run", True) is not False
    if not locations:
        return JSONResponse({"success": False, "message": "Choose at least one location."}, status_code=400)
    if run and _discovery_task and not _discovery_task.done():
        return JSONResponse({"success": False, "message": "A search is already running. Wait for it to finish."},
                            status_code=409)
    from harvey.brain import Brain
    from harvey.collectors.discover import DEFAULT_PROVIDER, PROVIDERS
    from harvey.config import load_config
    from harvey.pipeline import run_prospecting

    state = _state()
    await state.init_db()
    config = load_config()
    spec = await Brain(state, config.brain).think_json(
        LEAD_REQUEST_PROMPT.format(text=text[:2000] + "\nLocations: " + "; ".join(locations)),
        agent="scout", task="lead_request")
    if not isinstance(spec, dict) or not spec.get("industries"):
        return JSONResponse({"success": False, "message": "Couldn't understand that. Say what kind of "
                             "business you want, e.g. 'dental clinics with no online booking'."}, status_code=422)
    clean = lambda v, n: [str(x).strip() for x in (v or []) if str(x).strip()][:n]
    spec = {"industries": clean(spec.get("industries"), 5), "cities": locations,
            "titles": clean(spec.get("titles"), 5) or ["Owner"],
            "company_size": str(spec.get("company_size") or "").strip(),
            "summary": str(spec.get("summary") or "").strip()}

    # Save it as the targeting Harvey (and Scout) works from.
    path = _resolve_config_file()
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    icp = cfg.setdefault("icp", {})
    icp.update(industries=spec["industries"], geography=spec["cities"], titles=spec["titles"])
    if spec["company_size"]:
        icp["company_size"] = spec["company_size"]
    # Automatic cold email = autopilot: drafts send on their own (daily cap,
    # verified addresses only, held-for-review checks all still apply).
    cfg.setdefault("channels", {}).setdefault("email", {})["require_approval"] = not auto_send
    path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    config = load_config()
    saved = {"text": text, "locations": locations, "auto_send": auto_send, "spec": spec,
             "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if not run:
        await state.set_setting("lead_request", json.dumps(saved))
        return {"success": True, "spec": spec, "saved": True}

    provider = str((data or {}).get("provider") or await state.get_setting("discovery_provider")
                   or DEFAULT_PROVIDER)
    if provider not in PROVIDERS:
        provider = DEFAULT_PROVIDER
    # OpenStreetMap only knows mapped trades (dentists, plumbers...). Agencies
    # and software firms aren't on the map, so every query would fail: let
    # Scout's web search find them on the heartbeat instead of reporting an
    # error for a request Harvey can still answer.
    if provider == "osm" and not any(PROVIDERS["osm"]._tags(i) for i in spec["industries"]):
        await state.set_setting("lead_request", json.dumps({**saved, "provider": "web_search"}))
        _discovery_report = None
        wake_harvey()
        return {"success": True, "spec": spec, "provider": "web_search", "queries": []}
    try:
        queries = _discovery_queries({"provider": provider, "cities": spec["cities"]}, config)
    except ValueError as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=400)
    await state.set_setting("lead_request", json.dumps({**saved, "provider": provider}))
    await state.set_setting("discovery_paused", "")

    async def _go():
        global _discovery_report
        try:
            result = await run_prospecting(state, config, provider, queries, max_spend=1.0)
            _discovery_report = {**(result.discover or {}),
                                 "profiled_companies": result.profiled_companies,
                                 "profile_observations": result.profile_observations,
                                 "errors": result.errors}
        except Exception as exc:
            logger.exception("lead request run failed")
            _discovery_report = {"errors": [str(exc)], "stopped": "failed"}
        wake_harvey()  # Scout picks the new companies up for contacts

    _discovery_kind = "discover"
    _discovery_report = None
    _discovery_task = asyncio.create_task(_go())
    return {"success": True, "spec": spec, "provider": provider,
            "queries": [q.keyword() for q in queries]}


# ── Email template: the operator's own email rules (writer.email_rules) ──

DEFAULT_EMAIL_RULES = "skills/email_rules.md"


def _email_rules_file(config) -> tuple[str, Path]:
    """The rules file the Writer reads (writer.email_rules), or the default
    one when none is set. Only files inside the Harvey folder are offered:
    the page must never read or overwrite anything else on the machine."""
    raw = str(getattr(getattr(config, "writer", None), "email_rules", "") or "").strip()
    raw = raw or DEFAULT_EMAIL_RULES
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path = path.resolve()
    if not path.is_relative_to(PROJECT_ROOT.resolve()):
        raise ValueError(f"{raw} is outside the Harvey folder; edit it by hand.")
    return raw, path


def _clean_rules(value) -> str:
    from harvey.config import MAX_EMAIL_RULES_CHARS
    if not isinstance(value, str):
        raise ValueError("The template must be text.")
    text = value.replace("\r\n", "\n").replace("\x00", "").strip()
    if not text:
        raise ValueError("The template is empty.")
    if len(text) > MAX_EMAIL_RULES_CHARS:
        raise ValueError(f"The template is {len(text):,} characters; the limit is {MAX_EMAIL_RULES_CHARS:,}.")
    return text


def _point_writer_at(rel: str):
    """Make writer.email_rules name the template, editing only that line so
    the rest of the config file (comments, order) stays as it is."""
    path = _resolve_config_file()
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    if str((data.get("writer") or {}).get("email_rules") or "").strip():
        return
    if re.search(r"(?m)^writer:\s*$", text):
        text = re.sub(r"(?m)^writer:\s*$", f"writer:\n  email_rules: {rel}", text, count=1)
    else:
        text = text.rstrip("\n") + f"\nwriter:\n  email_rules: {rel}\n"
    if (yaml.safe_load(text) or {}).get("writer", {}).get("email_rules") != rel:
        raise ValueError("Couldn't set writer.email_rules safely; add it to the config by hand.")
    path.write_text(text, encoding="utf-8")


@app.get("/api/email-template")
async def get_email_template():
    """The template Harvey writes cold emails from, as the operator wrote it."""
    from harvey.config import MAX_EMAIL_RULES_CHARS, load_config
    try:
        config = load_config()
        rel, path = _email_rules_file(config)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    from harvey import email_template
    active = bool(str(getattr(config.writer, "email_rules", "") or "").strip())
    try:
        content = path.read_text(encoding="utf-8") if path.is_file() else ""
        saved = _editor_file(path)
        editor = json.loads(saved.read_text(encoding="utf-8")) if saved.is_file() else None
        editor = email_template.clean(editor) if editor else None
    except (OSError, ValueError) as e:
        logger.warning("Email editor data unusable, starting from the default: %s", e)
        editor = None
    in_sync = bool(editor) and email_template.render(editor).strip() == content.strip()
    return {"path": rel, "content": content, "active": active, "limit": MAX_EMAIL_RULES_CHARS,
            "editor": editor or email_template.DEFAULT, "editor_in_sync": in_sync,
            "placeholders": list(email_template.PLACEHOLDERS),
            "sender": {"name": config.persona.name, "company": config.persona.company}}


def _editor_file(rules_path: Path) -> Path:
    """Where the editor's emails are kept: beside the rules they render to."""
    return rules_path.with_name("email_template.json")


def _atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _rules_from(data: dict) -> tuple[str, dict | None]:
    """(rules text, editor emails or None) from what the page sent: the
    editor's emails, rendered into rules, or the raw rules (Advanced)."""
    from harvey import email_template
    if data.get("editor") is not None:
        editor = email_template.clean(data["editor"])
        return _clean_rules(email_template.render(editor)), editor
    return _clean_rules(data.get("content")), None


@app.post("/api/email-template")
async def save_email_template(request: Request):
    """Save the template; the Writer uses it for every email it writes next.
    Drafts already in the Outbox keep their text."""
    from harvey import email_template
    from harvey.agents.writer import EMAIL_RULES_PROBLEM_KEY
    from harvey.config import load_config
    try:
        data = await request.json()
        text, editor = _rules_from(data if isinstance(data, dict) else {})
        config = load_config()
        rel, path = _email_rules_file(config)
        _atomic_write(path, text + "\n")
        if editor is not None:
            _atomic_write(_editor_file(path), email_template.dumps(editor))
        _point_writer_at(rel)
    except ValueError as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=400)
    except Exception as e:
        logger.warning("Saving the email template failed: %s", e)
        return JSONResponse({"success": False, "message": "Could not save the template."}, status_code=500)
    state = _state()
    await state.init_db()
    await state.delete_setting(EMAIL_RULES_PROBLEM_KEY)  # a fixed template clears the warning
    return {"success": True, "path": rel}


@app.post("/api/email-template/preview")
async def preview_email_template(request: Request):
    """Draft one first email for one lead from the template on screen (saved
    or not). Nothing goes to the Outbox and nothing is sent; it costs one
    model call."""
    from harvey.agents.writer import Writer
    from harvey.brain import Brain
    from harvey.config import load_config, load_email_rules, load_env
    try:
        data = await request.json()
    except Exception:
        data = {}
    data = data if isinstance(data, dict) else {}
    try:
        config = load_config()
        if data.get("editor") is not None or data.get("content"):
            rules = _rules_from(data)[0]
        else:
            rules = load_email_rules(config)
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=400)
    state = _state()
    await state.init_db()
    pid = str(data.get("prospect_id") or "")
    if not pid:
        # The best-scored named person at a company, else anyone.
        rows = await query_db(
            "SELECT id FROM prospects WHERE company_id IS NOT NULL AND company_id != '' "
            "AND status NOT IN ('opted_out', 'bounced') "
            "ORDER BY (first_name != '') DESC, score DESC, created_at DESC LIMIT 1")
        pid = rows[0]["id"] if rows else ""
    prospect = await state.get_prospect(pid) if pid else None
    if not prospect:
        return JSONResponse({"success": False, "message": "No lead to preview with yet. Find some leads first."},
                            status_code=400)
    writer = Writer(Brain(state, config.brain), state, config, load_env())
    writer.skills = writer.brain.load_skills_for_agent("writer")
    writer.email_rules = rules
    draft = await writer._write_personal_email(prospect)
    if not draft:
        return JSONResponse({"success": False, "message": "Claude didn't return a draft. Try again."}, status_code=502)
    return {"success": True, "subject": draft["subject"], "body": draft["body"],
            "to": {"name": prospect.full_name().strip() or "General inbox", "company": prospect.company,
                   "email_status": prospect.email_status or ""}}


@app.post("/api/profile/run")
async def start_profile():
    """Read the websites of everything discovered but not yet looked at.

    Free and model-free, so there is nothing to estimate and no cap to set.
    """
    global _discovery_task, _discovery_report

    if _discovery_task and not _discovery_task.done():
        return JSONResponse({"success": False, "message": "a run is already going"},
                            status_code=409)
    try:
        from harvey.pipeline import run_profile_stage

        state = _state()
        await state.init_db()
        pending = await state.count_companies_needing_profile()
        if not pending:
            return {"success": True, "pending": 0}

        async def _go():
            global _discovery_report
            try:
                try:
                    from harvey.config import load_config
                    config = load_config()
                except Exception:
                    config = None   # still profiles; no role is singled out
                companies, observations, _ = await run_profile_stage(
                    state, limit=200, config=config)
                _discovery_report = {
                    "profiled_companies": companies,
                    "profile_observations": observations,
                }
            except Exception as exc:
                logger.exception("profile run failed")
                _discovery_report = {"errors": [str(exc)], "stopped": "failed"}

        global _discovery_kind
        _discovery_kind = "profile"
        _discovery_report = None
        _discovery_task = asyncio.create_task(_go())
        return {"success": True, "pending": pending}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/discover/stop")
async def stop_discovery():
    """Kill switch. Read between batches, so an in-flight run stops cleanly."""
    try:
        state = _state()
        await state.init_db()
        await state.set_setting("discovery_paused", "stopped from dashboard")
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


def _parse_ts(value) -> datetime | None:
    """SQLite CURRENT_TIMESTAMP / isoformat strings -> naive UTC datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "").split("+")[0])
    except ValueError:
        return None


async def _scalar(sql: str, params: tuple = ()) -> int:
    rows = await query_db(sql, params)
    return int(next(iter(rows[0].values())) or 0) if rows else 0


async def _first(sql: str, params: tuple = (), key: str = "t"):
    """First row's `key`, or None — query_db returns [] on any DB problem."""
    rows = await query_db(sql, params)
    return rows[0].get(key) if rows else None


# A run row still says 'running' after its process was killed (closed window,
# reboot). No new facts for this long means it is not coming back.
STALE_RUN_SECONDS = 900
# Contact-finding has no run row; a recent prospects_checked_at means it is active.
CONTACT_ACTIVE_SECONDS = 180
# Moved on from "waiting to be emailed" (opted_out too: never offered again).
SENT_STATUSES = ("contacted", "replied", "meeting", "closed", "lost", "opted_out")
REPLY_STATUSES = ("replied", "meeting", "closed")


def _eta_text(minutes) -> str:
    if minutes is None:
        return ""
    return "less than a minute left" if minutes < 1 else f"about {round(minutes)} min left"


@app.get("/api/live-status")
async def get_live_status():
    """Plain-language progress: each pipeline step, what is running, how long is left.

    Built for someone who would otherwise keep asking "what's the status?".
    Everything is derived from the database, so it is true whatever started
    the work — `harvey run`, a CLI command, or a one-off script. Every total
    counts only what the step can act on (a business with no website is never
    read), so a finished step always reaches 100% instead of sticking.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # DB stores naive UTC
    has_site = "COALESCE(domain, '') != ''"
    companies = await _scalar("SELECT COUNT(*) FROM companies")
    with_site = await _scalar(f"SELECT COUNT(*) FROM companies WHERE {has_site}")
    no_site = companies - with_site
    profiled = await _scalar(
        f"SELECT COUNT(*) FROM companies WHERE {has_site} AND profile_checked_at IS NOT NULL")
    checked = await _scalar(
        f"SELECT COUNT(*) FROM companies WHERE {has_site} "
        "AND profile_checked_at IS NOT NULL AND prospects_checked_at IS NOT NULL")

    prospects = await query_db("SELECT id, email, email_status, status FROM prospects")
    with_email = [p for p in prospects if p.get("email")]
    by_status: dict[str, int] = {}
    for p in with_email:
        k = (p.get("email_status") or "guess").lower()
        by_status[k] = by_status.get(k, 0) + 1
    deliverable = {p["id"] for p in with_email
                   if (p.get("email_status") or "") in ("verified", "risky")}
    unconfirmed = sum(n for k, n in by_status.items()
                      if k not in ("verified", "risky", "invalid"))

    # Drafts are counted per person, not per campaign: one campaign can cover many.
    drafted: set[str] = set()
    for row in await query_db("SELECT prospect_ids_json FROM campaigns"):
        try:
            ids = json.loads(row.get("prospect_ids_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            ids = []
        if isinstance(ids, list):
            drafted.update(i for i in ids if isinstance(i, str))
    sent = {p["id"] for p in prospects if p.get("status") in SENT_STATUSES}
    # Who is left for the user to send by hand: only when Harvey can't send
    # itself, and never someone Harvey has queued, sent to, or can't reach.
    harvey_sends = _harvey_can_send()
    awaiting_review = await _scalar("SELECT COUNT(*) FROM outbox WHERE status = 'pending_review'")
    by_hand: set[str] = set()
    if not harvey_sends:
        skip = await _harvey_committed_ids() | await _hand_sent_ids()
        by_hand = {p["id"] for p in prospects
                   if p["id"] in drafted and p["id"] not in skip
                   and (p.get("status") or "new") in HAND_SENDABLE_STATUSES
                   and (p.get("email_status") or "guess") in HAND_SENDABLE_EMAIL}
    replies = sum(1 for p in prospects if p.get("status") in REPLY_STATUSES)
    cost = float(await _first(
        "SELECT COALESCE(SUM(cost_usd), 0) AS c FROM runs", key="c") or 0)

    running, stopped = [], []
    for run in await query_db(
            "SELECT * FROM runs WHERE status = 'running' ORDER BY started_at"):
        started = _parse_ts(run.get("started_at"))
        elapsed = max(0.0, (now - started).total_seconds()) if started else 0.0
        last = _parse_ts(await _first(
            "SELECT MAX(observed_at) AS t FROM observations WHERE run_id = ?",
            (run["id"],))) or started
        item = {"stage": run.get("stage"), "provider": run.get("provider"),
                "elapsed_min": round(elapsed / 60, 1), "eta_min": None}
        if last and (now - last).total_seconds() > STALE_RUN_SECONDS:
            stopped.append(item)
            continue
        if run.get("stage") == "profile" and started:
            done_in_run = await _scalar(
                f"SELECT COUNT(*) FROM companies WHERE {has_site} AND profile_checked_at >= ?",
                (run["started_at"],))
            left = max(0, with_site - profiled)
            if left and done_in_run and elapsed > 0:
                item["eta_min"] = left / (done_in_run / elapsed) / 60
        running.append(item)

    last_contact = _parse_ts(await _first(
        "SELECT MAX(prospects_checked_at) AS t FROM companies"))
    finding_emails = bool(
        last_contact and (now - last_contact).total_seconds() < CONTACT_ACTIVE_SECONDS)
    reading = any(r["stage"] == "profile" for r in running)
    finding = any(r["stage"] == "discover" for r in running)

    def step(label, done, total, active, detail, info=False):
        if active:
            state_ = "running"
        elif info:
            state_ = "done" if done else "waiting"
        elif total and done >= total:
            state_ = "done"
        elif done:
            state_ = "partial"
        else:
            state_ = "waiting"
        return {"label": label, "done": done, "total": total,
                "state": state_, "detail": detail}

    profile_eta = next((r["eta_min"] for r in running if r["stage"] == "profile"), None)
    read_detail = f"{profiled} of {with_site} read"
    if reading and profile_eta is not None:
        read_detail += " · " + _eta_text(profile_eta)
    if no_site:
        read_detail += f" · {no_site} have no website (skipped)"

    steps = [
        step("Find businesses", companies, 0, finding,
             f"{companies} businesses found", info=True),
        step("Read their websites", profiled, with_site, reading, read_detail),
        step("Find emails", checked, profiled, finding_emails,
             f"{checked} of {profiled} checked · {len(with_email)} emails found"),
        step("Check emails are real", len(with_email) - unconfirmed, len(with_email), False,
             f"{by_status.get('verified', 0)} verified · {by_status.get('risky', 0)} risky · "
             f"{unconfirmed} not confirmed"
             + (f" · {by_status['invalid']} bad" if by_status.get("invalid") else "")),
        step("Write first emails", len(drafted), len(deliverable | drafted), False,
             f"{len(drafted)} written · see Campaigns"),
        step("Sent", len(sent & drafted), len(drafted), False,
             f"{len(sent & drafted)} of {len(drafted)} sent"),
        step("Replies", replies, 0, False,
             f"{replies} replies" if replies else "No replies yet", info=True),
    ]

    next_tab = ""   # where the next step is done, when it's done in the dashboard
    if reading or finding:
        next_up = "Wait — Harvey is working. This page updates by itself."
    elif finding_emails:
        next_up = "Wait — emails are being found."
    elif stopped:
        next_up, next_tab = "A job stopped before finishing. Run it again.", "discover"
    elif harvey_sends and awaiting_review:
        next_up, next_tab = f"Review the {awaiting_review} draft(s) waiting in the Outbox.", "outbox"
    elif by_hand:
        next_up, next_tab = f"Open Campaigns and send the {len(by_hand)} unsent email(s).", "campaigns"
    elif profiled > checked:
        next_up, next_tab = f"Find emails for the {profiled - checked} businesses not checked yet.", "controls"
    elif deliverable - drafted:
        next_up, next_tab = f"Write first emails for {len(deliverable - drafted)} more contact(s).", "controls"
    elif not companies:
        next_up, next_tab = "Search for businesses to get started.", "discover"
    else:
        next_up, next_tab = "Search more cities or industries to get more leads.", "discover"

    return {
        "steps": steps,
        "running": [dict(r, eta_text=_eta_text(r["eta_min"])) for r in running],
        "stopped": stopped,
        "next": next_up,
        "next_tab": next_tab,
        "cost_usd": round(cost, 4),
        "updated_at": now.isoformat() + "Z",
    }


def _require_approval_now() -> bool:
    try:
        from harvey.config import load_config
        return bool(load_config(str(_resolve_config_file())).channels.email.require_approval)
    except Exception:
        return True


@app.get("/api/today")
async def get_today():
    """What needs a human, right now.

    The dashboard opens here rather than on a setup checklist: the question a
    returning user actually has is "is anything waiting on me?", and the
    answer is usually a short list or nothing at all.
    """
    items: list[dict] = []
    stats: dict = {}
    try:
        from harvey.signals import seed_signal_catalog

        state = _state()
        await state.init_db()
        await seed_signal_catalog(state)

        codes = await state.get_signal_codes()
        proposed = [c for c in codes if c.get("status") == "proposed"]
        confirmed = [c for c in codes if c.get("status") == "confirmed"]
        pending = await state.get_outbox(status="pending_review", limit=200)
        approved = await state.get_outbox(status="approved", limit=200)
        paused = await state.get_setting("sending_paused")
        counts = await state.count_prospects_by_status()

        convos = await query_db(
            "SELECT COUNT(*) AS n FROM conversations WHERE status = 'open'"
        )
        open_convos = convos[0]["n"] if convos else 0
        handed = await query_db(
            "SELECT COUNT(*) AS n FROM conversations WHERE status = 'needs_human'"
        )
        needs_human = handed[0]["n"] if handed else 0
        unanswered = await _unanswered_conversation_ids()
        unseen = await _unseen_conversation_ids()
        companies = await query_db("SELECT COUNT(*) AS n FROM companies")
        n_companies = companies[0]["n"] if companies else 0

        unprofiled_count = await state.count_companies_needing_profile()
        leads_pending = await query_db(
            "SELECT COUNT(*) AS n FROM prospects WHERE lead_approval = 'pending' AND status = 'new'")
        stats = {
            "leads_pending": leads_pending[0]["n"] if leads_pending else 0,
            "companies": n_companies,
            "prospects": sum(counts.values()),
            "signals_confirmed": len(confirmed),
            "outbox_pending": len(pending),
            "outbox_approved": len(approved),
            "open_conversations": open_convos,
            "needs_human": needs_human,
            "conversations_new": len(unanswered),
            "conversations_unseen": len(unseen),
            "signals_proposed": len(proposed),
            "email_provider": _current_provider(),
            "unprofiled": unprofiled_count,
        }

        # Ordered by how much it blocks Harvey from doing anything at all.
        brain = await _brain_status()
        if brain.get("status") == "paused":
            who = "Codex" if brain.get("provider") == "codex" else "Harvey's model"
            items.append({
                "key": "brain-paused", "tone": "bad",
                "title": f"{who} is paused, so Harvey is waiting",
                "detail": (str(brain.get("reason") or "It can't be used right now.")[:300] +
                           " Replies and queued emails are untouched; Harvey resumes "
                           "once it's fixed. harvey status --check says what's wrong."),
                "action": "Open the log", "tab": "controls",
            })

        from harvey.agents.writer import EMAIL_RULES_PROBLEM_KEY
        rules_problem = await state.get_setting(EMAIL_RULES_PROBLEM_KEY)
        if rules_problem:
            items.append({
                "key": "email-rules", "tone": "bad",
                "title": "Harvey isn't writing emails: your email rules can't be used",
                "detail": (f"{rules_problem}. Fix the file, or clear writer.email_rules "
                           "to use Harvey's built-in rules."),
                "action": "Open the log", "tab": "controls",
            })

        hold = await state.current_sends_held()
        if hold:
            from harvey.sending_mode import held_message
            title, detail = held_message(hold, waiting=len(approved))
            items.append({
                "key": "sends-held", "tone": "bad", "title": title, "detail": detail,
                "action": "Open the log", "tab": "controls",
            })

        if paused:
            items.append({
                "key": "paused", "tone": "bad",
                "title": "Sending is paused",
                "detail": str(paused) + ". Nothing will go out until you resume it.",
                "action": "Review and resume", "tab": "outbox",
            })

        # Cold email waiting on the optional Precleared check: its free
        # checks ran out, the key was rejected, or it couldn't be reached.
        from harvey.integrations.precleared import current_hold, hold_message
        try:
            from harvey.config import load_config
            loaded = load_config(str(_resolve_config_file()))
        except Exception:
            loaded = None  # can't judge: show the hold rather than hide it
        precleared_hold = await current_hold(state, loaded)
        if precleared_hold:
            message = hold_message(precleared_hold)
            items.append({
                "key": "precleared-hold", "tone": message["tone"],
                "title": message["title"], "detail": message["detail"],
                "action": "Open the log", "tab": "controls",
                "url": message["url"], "url_label": message["url_label"],
            })

        setup = await get_setup_status()
        if isinstance(setup, dict) and setup.get("percent", 100) < 100:
            missing = [c["label"] for c in setup.get("checks", [])
                       if c.get("required") and not c.get("done")]
            items.append({
                "key": "setup", "tone": "warn",
                "title": "Finish setting Harvey up",
                "detail": ", ".join(missing[:3]) or "Some required steps are incomplete.",
                "action": "Open setup", "tab": "settings",
            })

        if proposed:
            items.append({
                "key": "signals", "tone": "warn",
                "title": (f"{len(proposed)} signal waiting for your confirmation"
                          if len(proposed) == 1
                          else f"{len(proposed)} signals waiting for your confirmation"),
                "detail": ("Harvey won't collect anything you haven't approved. "
                           "Confirm which signals define a good prospect for you."),
                "action": "Review signals", "tab": "signals",
            })
        elif not confirmed:
            items.append({
                "key": "signals-none", "tone": "warn",
                "title": "No signals confirmed",
                "detail": "Every signal is rejected, so prospecting has nothing to collect.",
                "action": "Review signals", "tab": "signals",
            })

        if not n_companies and confirmed:
            items.append({
                "key": "discover", "tone": "good",
                "title": "No companies yet",
                "detail": ("Signals are confirmed but nothing has been collected. "
                           "Discovery is free to try — no account needed."),
                "action": "Find businesses", "tab": "discover",
            })

        unprofiled = unprofiled_count
        if unprofiled:
            items.append({
                "key": "profile", "tone": "good",
                "title": f"{unprofiled} companies not looked at yet",
                "detail": ("Reading their websites is free and it is what makes "
                           "an email specific — who their agency is, what they "
                           "are missing, whether they are spending on ads."),
                "action": "Read their sites", "tab": "discover",
            })

        if needs_human:
            items.append({
                "key": "needs-human", "tone": "bad",
                "title": (f"{needs_human} conversation needs you" if needs_human == 1
                          else f"{needs_human} conversations need you"),
                "detail": ("Harvey stopped and handed these over: an escalation, or a "
                           "reply it had no safe answer for. Reply yourself, then close "
                           "it or hand it back."),
                "action": "Read them", "tab": "conversations",
            })

        if pending:
            items.append({
                "key": "outbox", "tone": "warn",
                "title": (f"{len(pending)} email waiting for approval" if len(pending) == 1
                          else f"{len(pending)} emails waiting for approval"),
                "detail": ("Nothing sends until you approve it. Read them one at a time."
                           if _require_approval_now() else
                           "Autopilot sends the rest on its own; these wait for you. "
                           "Read them one at a time."),
                "action": "Open the decisions desk", "tab": "outbox",
            })

        if stats["leads_pending"]:
            n = stats["leads_pending"]
            items.append({
                "key": "leads", "tone": "warn",
                "title": f"{n} lead{'s' if n != 1 else ''} waiting for approval",
                "detail": "Harvey only writes to leads you approve. Approve or reject them by business type.",
                "action": "Review leads", "tab": "found",
            })

        if open_convos:
            items.append({
                "key": "replies", "tone": "good",
                "title": (f"{open_convos} live conversation" if open_convos == 1
                          else f"{open_convos} live conversations"),
                "detail": "People replied. Check how Harvey is handling them.",
                "action": "Read conversations", "tab": "conversations",
            })

        return {"items": items, "stats": stats}
    except Exception as e:
        logger.exception("today load failed")
        return {"items": [], "stats": stats, "error": str(e)}


# ── Signals: Harvey proposes, the user confirms ──
#
# Nothing is collected until a human has said yes to it. This is the gate the
# whole prospecting pipeline hangs off: collectors ask `state.confirmed_signal_codes()`
# and skip anything that isn't in the set.

CATEGORY_META = {
    "discovery": {
        "label": "Discovery — who exists",
        "blurb": "How Harvey finds businesses at all, and how visible they are. "
                 "This is the only stage that costs money.",
    },
    "profile": {
        "label": "Profile — what they are",
        "blurb": "Read from the pages a business already publishes. Free, no AI "
                 "tokens, three HTTP requests per company. These are the signals "
                 "that make an email specific.",
    },
    "people": {
        "label": "People — who decides",
        "blurb": "Named humans and whether they're the one who can say yes.",
    },
    "verification": {
        "label": "Contactability — can you reach them",
        "blurb": "Whether the address will actually deliver, and what to do when "
                 "it won't.",
    },
}
CATEGORY_ORDER = ["discovery", "profile", "people", "verification"]


@app.get("/api/signals")
async def get_signals():
    """The signal vocabulary, grouped for review, with live cohort sizes."""
    try:
        from harvey.signals import seed_signal_catalog

        state = _state()
        await state.init_db()
        # Seeding is idempotent and never overrides a decision the user made,
        # so it is safe to run on every load — new signals shipped in an
        # upgrade show up as `proposed` without any migration step.
        await seed_signal_catalog(state)

        codes = await state.get_signal_codes()
        counts = {c["signal_code"]: c for c in await state.signal_counts()}

        groups, summary = [], {"proposed": 0, "confirmed": 0, "rejected": 0}
        for cat in CATEGORY_ORDER:
            rows = []
            for sig in codes:
                if sig.get("category") != cat:
                    continue
                seen = counts.get(sig["code"], {})
                rows.append({
                    **sig,
                    "companies": seen.get("companies", 0),
                    "observations": seen.get("observations", 0),
                })
            if rows:
                meta = CATEGORY_META.get(cat, {})
                groups.append({
                    "key": cat,
                    "label": meta.get("label", cat.title()),
                    "blurb": meta.get("blurb", ""),
                    "signals": rows,
                })
        for sig in codes:
            summary[sig.get("status", "proposed")] = (
                summary.get(sig.get("status", "proposed"), 0) + 1
            )

        return {"summary": summary, "groups": groups, "total": len(codes)}
    except Exception as e:
        logger.exception("signals load failed")
        return {"error": str(e), "groups": [], "summary": {}}


@app.post("/api/signals/status")
async def set_signals_status(request: Request):
    """Confirm or reject one signal, or a whole category at once."""
    try:
        body = await request.json()
        status = (body.get("status") or "").strip()
        codes = body.get("codes") or ([body["code"]] if body.get("code") else [])
        if status not in ("proposed", "confirmed", "rejected"):
            return JSONResponse(
                {"success": False, "message": f"invalid status: {status!r}"},
                status_code=400,
            )
        if not codes:
            return JSONResponse(
                {"success": False, "message": "no signal codes given"}, status_code=400
            )

        from harvey.signals import seed_signal_catalog

        state = _state()
        await state.init_db()
        # Seed first: a confirm that arrives before anything has loaded the
        # catalog would otherwise report success while changing nothing.
        await seed_signal_catalog(state)

        changed, unknown = 0, []
        for code in codes:
            if await state.set_signal_status(code, status):
                changed += 1
            else:
                unknown.append(code)
        return {
            "success": True, "changed": changed,
            "status": status, "unknown": unknown,
        }
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


COHORT_PAGE_SIZE = 50
COHORT_MAX_PAGE_SIZE = 200


async def _cohort_companies(require: list[str], exclude: list[str],
                            offset: int = 0, limit: int | None = None,
                            ranges: list[dict] | None = None):
    state = _state()
    await state.init_db()
    return await state.cohort_companies(require, exclude, offset, limit,
                                        ranges=ranges)


def _codes(raw) -> list[str]:
    """Accept lists or CSV strings, with bounded SQL parameter counts."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, list) or any(not isinstance(c, str) for c in raw):
        raise ValueError("signals must be a list of strings or comma-separated string")
    codes = list(dict.fromkeys(c.strip() for c in raw if c.strip()))
    if len(codes) > 200 or any(len(c) > 128 for c in codes):
        raise ValueError("too many or oversized signal codes")
    return codes


COHORT_MAX_RANGES = 20


def _range_bound(raw):
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ValueError("range bounds must be numbers")
    value = float(raw)
    if value != value or abs(value) > 1e12:
        raise ValueError("range bound is out of bounds")
    return value


def _ranges(raw) -> list[dict]:
    """Numeric cohort filters: a list of {code, min, max}, or the CSV query
    form "SERP_RANK:11:30,MAPS_RANK::3". At least one bound per range."""
    if raw is None or raw == "" or raw == []:
        return []
    if isinstance(raw, str):
        items = []
        for part in raw.split(","):
            if not part.strip():
                continue
            pieces = part.split(":")
            if len(pieces) != 3:
                raise ValueError("ranges look like CODE:min:max")
            items.append({"code": pieces[0], "min": pieces[1], "max": pieces[2]})
        raw = items
    if not isinstance(raw, list) or any(not isinstance(r, dict) for r in raw):
        raise ValueError("ranges must be a list of {code, min, max}")
    if len(raw) > COHORT_MAX_RANGES:
        raise ValueError("too many ranges")
    out = []
    for r in raw:
        code = r.get("code")
        if not isinstance(code, str) or not code.strip() or len(code) > 128:
            raise ValueError("each range needs a signal code")
        low, high = _range_bound(r.get("min")), _range_bound(r.get("max"))
        if low is None and high is None:
            raise ValueError(f"range on {code} needs a min or a max")
        if low is not None and high is not None and low > high:
            raise ValueError(f"range on {code}: min is above max")
        out.append({"code": code.strip(), "min": low, "max": high})
    return out


def _cohort_integer(raw, default):
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise ValueError("offset and limit must be integers")
    value = int(raw)
    if abs(value) > 9223372036854775807:
        raise ValueError("offset or limit is too large")
    return value


@app.post("/api/cohort")
async def preview_cohort(request: Request):
    """How many companies carry ALL these signals and none of those.

    The payoff for confirming signals: a cohort is a query, not a list. Set
    intersection happens in SQL — intersecting in JS over a capped fetch
    silently returns the wrong answer. ``offset``/``limit`` page through the
    matches; ``size`` is always the whole cohort. ``ranges`` adds numeric
    filters — ``[{"code": "SERP_RANK", "min": 11, "max": 30}]``.
    """
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        require = _codes(body.get("require"))
        exclude = _codes(body.get("exclude"))
        ranges = _ranges(body.get("ranges"))
        offset = max(0, _cohort_integer(body.get("offset"), 0))
        limit = min(max(1, _cohort_integer(body.get("limit"), COHORT_PAGE_SIZE)),
                    COHORT_MAX_PAGE_SIZE)
        if not require and not ranges:
            return {"size": 0, "offset": 0, "limit": limit, "companies": []}

        size, rows = await _cohort_companies(require, exclude, offset, limit,
                                             ranges=ranges)
        return {"size": size, "offset": offset, "limit": limit,
                "companies": rows}
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"size": 0, "companies": [], "error": str(e)}, status_code=500)


def _csv_cell(value):
    text = str(value or "")
    # Spreadsheet software may ignore leading whitespace before formulas.
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


@app.get("/api/cohort.csv")
async def export_cohort(require: str = "", exclude: str = "", ranges: str = ""):
    """The whole cohort as a CSV download — every match, not just one page."""
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["name", "domain", "website", "industry", "location"])
    try:
        codes = _codes(require)
        excluded = _codes(exclude)
        bounds = _ranges(ranges)
        if codes or bounds:
            _, rows = await _cohort_companies(codes, excluded, ranges=bounds)
            for r in rows:
                writer.writerow([_csv_cell(r.get(k)) for k in
                                 ("name", "domain", "website", "industry", "location")])
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.warning("Cohort export failed: %s", e)
        return JSONResponse({"error": "Cohort export failed"}, status_code=500)
    return PlainTextResponse(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="cohort.csv"'},
    )


@app.get("/api/runs")
async def get_runs_api():
    """The collector run log — what ran, when, what it produced and cost."""
    try:
        state = _state()
        await state.init_db()
        await state.sweep_stale_runs()
        runs = await state.get_runs(limit=25)
        # Same rule as the live panel: a "running" run that has recorded
        # nothing for STALE_RUN_SECONDS has stopped, whatever its row says.
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for run in runs:
            if run.get("status") != "running":
                continue
            last = _parse_ts(await _first(
                "SELECT MAX(observed_at) AS t FROM observations WHERE run_id = ?",
                (run["id"],))) or _parse_ts(run.get("started_at"))
            if last and (now - last).total_seconds() > STALE_RUN_SECONDS:
                run["status"] = "stalled"
        return runs
    except Exception as e:
        return {"error": str(e)}


WEB_DIR = (Path(__file__).resolve().parent / "web")


TEXT_TYPES = {".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml"}
BINARY_TYPES = {".woff2": "font/woff2", ".woff": "font/woff", ".png": "image/png"}


@app.get("/static/{path:path}")
async def static_file(path: str):
    """Serve the dashboard's own assets from disk.

    Read per-request rather than cached at import: editing app.css and hitting
    reload is the whole point of having them as real files. Fonts are vendored
    rather than fetched from a CDN — this is a local tool and it should work
    with the network off.
    """
    target = (WEB_DIR / path).resolve()
    root = WEB_DIR.resolve()
    if not target.is_file() or not target.is_relative_to(root):
        return PlainTextResponse("not found", status_code=404)

    if target.suffix in BINARY_TYPES:
        return Response(
            target.read_bytes(),
            media_type=BINARY_TYPES[target.suffix],
            headers={"Cache-Control": "public, max-age=604800"},
        )
    return PlainTextResponse(
        target.read_text(encoding="utf-8"),
        media_type=TEXT_TYPES.get(target.suffix, "text/plain"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


# ── Sign-in page ──
# The guard lets these through without a token; a correct sign-in returns
# the dashboard token, which the page keeps for this tab like the link's.

async def _credentials(request: Request) -> tuple[str, str] | None:
    try:
        body = await request.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    username, password = body.get("username"), body.get("password")
    if not isinstance(username, str) or not isinstance(password, str) or len(password) > 1024:
        return None
    return username, password


@app.get("/auth/status")
async def auth_status():
    try:
        has_users = await asyncio.to_thread(user_store.has_users)
    except auth.AccountError as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=503)
    return {"has_users": has_users, "signup_open": auth.signup_open() and not has_users,
            "brand": _brand_name()}


def _brand_name() -> str:
    """The name the dashboard shows instead of "Harvey" (HARVEY_BRAND_NAME in
    .env, e.g. a company's own product name); "" keeps "Harvey". Read from
    the .env on disk so a change shows on the next page load."""
    try:
        name = (_read_env_file().get("HARVEY_BRAND_NAME") or "").strip()
    except Exception:
        name = ""
    name = name or os.getenv("HARVEY_BRAND_NAME", "").strip()
    # Plain text only, short enough for the sidebar
    return "".join(ch for ch in name if ch.isprintable() and ch not in "<>\"'`")[:40]


@app.post("/auth/signup")
async def auth_signup(request: Request):
    creds = await _credentials(request)
    if creds is None:
        return JSONResponse({"success": False, "message": "Invalid request body."}, status_code=400)
    if not auth.signup_open():
        return JSONResponse({"success": False, "message": "Sign-up is closed. Ask the owner for an account."},
                            status_code=403)
    try:
        await asyncio.to_thread(user_store.signup, *creds)
        token = token_store.get()
    except (auth.AccountError, auth.TokenError) as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=400)
    return {"success": True, "token": token, "username": creds[0].strip()}


@app.post("/auth/login")
async def auth_login(request: Request):
    creds = await _credentials(request)
    if creds is None:
        return JSONResponse({"success": False, "message": "Invalid request body."}, status_code=400)
    try:
        ok = await asyncio.to_thread(user_store.check, *creds)
        token = token_store.get() if ok else ""
    except (auth.AccountError, auth.TokenError) as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=503)
    if not ok:
        await asyncio.sleep(1)  # slows guessing
        return JSONResponse({"success": False, "message": "Wrong username or password."},
                            status_code=401)
    return {"success": True, "token": token, "username": creds[0].strip()}


class DashboardSetupError(Exception):
    """The dashboard can't start safely; the message says what to do."""


def _check_loopback_bind(host: str) -> str:
    """The host to bind, which must be a loopback address. The dashboard has
    no remote-access mode: from another machine, use an SSH tunnel."""
    name = (host or "").strip().lower()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    if not auth.is_loopback(name):
        raise DashboardSetupError(
            f"Refusing to listen on {host!r}: the dashboard only listens on this machine "
            "(127.0.0.1, ::1 or localhost). To reach it from elsewhere, tunnel instead: "
            "ssh -L 5555:127.0.0.1:5555 <this-host>")
    return name


def dashboard_url(host: str = "127.0.0.1", port: int = 5555) -> str:
    """The unlock link. The token rides in the #fragment, which browsers
    never send to the server (so it stays out of logs and Referer); the
    page moves it to sessionStorage and strips it from the address bar."""
    name = _check_loopback_bind(host)
    try:
        if ipaddress.ip_address(name).version == 6:
            name = f"[{name}]"
    except ValueError:
        pass
    return f"http://{name}:{port}/#token={token_store.get()}"


def start_dashboard(host: str = "127.0.0.1", port: int = 5555):
    """Start the dashboard server (loopback only, token required)."""
    import uvicorn

    try:
        bind = _check_loopback_bind(host)
        url = dashboard_url(host, port)
    except (DashboardSetupError, auth.TokenError) as e:
        raise DashboardSetupError(str(e)) from e

    if auth.no_token_mode():
        print(f"\n  Harvey Dashboard running, NO TOKEN (HARVEY_DASHBOARD_NO_TOKEN=1):\n\n"
              f"    http://{url.split('://', 1)[1].split('/', 1)[0]}/\n")
        print("  Any program or user on this computer can use it. Unset the variable")
        print("  and restart to lock it again. Press Ctrl+C to stop.\n")
    else:
        print(f"\n  Harvey Dashboard running. Open http://{url.split('://', 1)[1].split('/', 1)[0]}/")
        print(f"  and sign in (or create an account), or unlock with this link:\n\n    {url}\n")
        print(f"  The token is stored in {token_store.path} (owner-only). Keep the link")
        print("  private; anyone with it can use the dashboard. Press Ctrl+C to stop.\n")
    if os.name == "nt" and not auth.no_token_mode():
        print("  Windows: the token file is protected by your folder's permissions,")
        print("  not POSIX file modes.\n")
    uvicorn.run(app, host=bind, port=port, log_level="warning",
                proxy_headers=False, server_header=False, date_header=False)
