"""HARVEY_BRAND_NAME: the dashboard can show another name instead of
"Harvey". Fictional names only."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_dashboard_sitewatch import page, yaml_file  # noqa: F401  (fixtures)
from tests.test_dashboard_signals import client  # noqa: F401  (fixture)

WEB = Path(__file__).resolve().parent.parent / "harvey" / "web"


def test_no_brand_by_default(page):  # noqa: F811
    assert page.get("/auth/status").json()["brand"] == ""


def test_brand_from_env_file(page):  # noqa: F811
    page.env_file.write_text("HARVEY_BRAND_NAME=ExampleAudit\n", encoding="utf-8")
    assert page.get("/auth/status").json()["brand"] == "ExampleAudit"


def test_brand_is_plain_text_and_short(page):  # noqa: F811
    page.env_file.write_text('HARVEY_BRAND_NAME=<b>Exam"ple</b>' + "x" * 60 + "\n", encoding="utf-8")
    brand = page.get("/auth/status").json()["brand"]
    assert "<" not in brand and '"' not in brand and len(brand) <= 40


def _brand_text(brand: str, text: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    source = (WEB / "app.js").read_text(encoding="utf-8")
    fn = re.search(r"^function brandText\(.*?^}\n", source, re.S | re.M).group(0)
    script = f"let _brand = {json.dumps(brand)};\n{fn}\nprocess.stdout.write(brandText({json.dumps(text)}));"
    return subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8",
                          check=True, timeout=30).stdout


def test_brand_replaces_the_word_only():
    assert _brand_text("ExampleAudit", "Start Harvey") == "Start ExampleAudit"
    assert _brand_text("ExampleAudit", "Harvey's check-ins") == "ExampleAudit's check-ins"
    assert _brand_text("ExampleAudit", "harvey run") == "harvey run"          # commands stay
    assert _brand_text("ExampleAudit", "Harveyish") == "Harveyish"          # whole word only
    assert _brand_text("", "Start Harvey") == "Start Harvey"                # off by default
