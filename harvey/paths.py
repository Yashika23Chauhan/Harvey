"""Canonical filesystem locations for the Harvey checkout.

Harvey is repo-centric: config (harvey.yaml, .env), knowledge (prompts/,
skills/), and state (data/) all live next to the package. ``resolve()``
matters — when the package is reached through a symlink (some editable
installs), a bare ``Path(__file__).parent`` would point into
site-packages and Harvey would silently read/write the wrong files.
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Touched by the dashboard when you send or approve something, so a sleeping
# heartbeat wakes and sends it now instead of at the next interval.
WAKE_FILE = PROJECT_ROOT / "data" / "harvey.wake"


def wake_harvey():
    """Ask a running heartbeat to start its next cycle now."""
    try:
        WAKE_FILE.parent.mkdir(parents=True, exist_ok=True)
        WAKE_FILE.touch()
    except OSError:
        pass  # it still runs at the next interval
