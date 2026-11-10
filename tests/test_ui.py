"""UI tests: the page is served safely, and the JavaScript matches the HTML.

The browser behaviour itself is checked by the demo script (tools/record_demo.py); these tests
guard the things that are easy to break silently.
"""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codelens.main import STATIC_DIR, app
from codelens.review.diff import parse_diff

ROOT = Path(__file__).resolve().parent.parent
HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


# ---- serving --------------------------------------------------------------------------


def test_the_home_page_is_the_ui_with_security_headers(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<title>CodeLens</title>" in response.text
    policy = response.headers["content-security-policy"]
    assert "default-src 'self'" in policy and "frame-ancestors 'none'" in policy
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("path", ["/static/app.js", "/static/style.css", "/static/examples.json"])
def test_the_assets_are_served_with_the_same_headers(client, path):
    response = client.get(path)

    assert response.status_code == 200
    assert "content-security-policy" in response.headers


def test_the_api_docs_page_is_not_restricted_by_the_page_policy(client):
    assert "content-security-policy" not in client.get("/docs").headers


def test_the_api_is_unchanged_next_to_the_ui(client):
    assert client.get("/health").json() == {"status": "ok"}


# ---- the page is safe by construction -------------------------------------------------


@pytest.mark.parametrize(
    "sink",
    ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"],
)
def test_the_script_never_uses_an_html_injection_sink(sink):
    """Model answers and diff text are untrusted. They may only be added as text."""
    assert sink not in JS


def test_the_page_has_no_inline_scripts_styles_or_event_handlers():
    assert not re.search(r"<script(?![^>]*\bsrc=)", HTML)
    assert not re.search(r"\sstyle=", HTML)
    assert not re.search(r"\son[a-z]+=", HTML)


def test_links_are_restricted_to_http_urls():
    assert "safeUrl" in JS and r"/^https?:\/\//i" in JS


# ---- the script and the page agree ----------------------------------------------------


def test_every_element_id_the_script_uses_exists_in_the_page():
    html_ids = set(re.findall(r'\bid="([^"]+)"', HTML))
    used = set(re.findall(r'\$\("([^"$]+)"\)', JS))
    used |= {f"{kind}-{name}" for kind in ("tab", "panel") for name in ("repos", "ask", "review")}
    used |= {"ask-repo", "review-repo"}  # used through a loop in refreshRepos

    assert used - html_ids == set()


def test_the_tab_panels_in_the_page_match_the_script():
    assert re.search(r'const TABS = \["repos", "ask", "review"\]', JS)
    for name in ("repos", "ask", "review"):
        assert f'id="tab-{name}"' in HTML and f'id="panel-{name}"' in HTML


# ---- the examples ---------------------------------------------------------------------


def test_the_examples_are_valid_and_match_the_evaluation_cases():
    examples = json.loads((STATIC_DIR / "examples.json").read_text(encoding="utf-8"))
    dataset = json.loads((ROOT / "eval" / "questions.json").read_text(encoding="utf-8"))
    cases = {
        c["id"]: c for c in json.loads((ROOT / "eval" / "review_cases.json").read_text())["cases"]
    }

    assert set(examples["questions"]) <= set(dataset["repos"])
    ids = [item["id"] for item in examples["diffs"]]
    assert len(ids) == len(set(ids)) >= 3
    for item in examples["diffs"]:
        assert item["repo"] in dataset["repos"]
        assert item["diff"] == cases[item["id"]]["diff"], (
            "examples must not drift from the eval cases"
        )
        files, _ = parse_diff(item["diff"], max_added_lines=300)
        assert files, f"{item['id']}: nothing reviewable"
