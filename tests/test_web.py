"""Interfaz web local: archivos propios, sin CDN, compatible con la CSP y sin HTML inyectable."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from politica_monetaria_banrep_rag.api import LOCAL_HOSTS, WEB_DIR, create_app
from politica_monetaria_banrep_rag.service import RagService

JS = (WEB_DIR / "app.js").read_text(encoding="utf-8")
HTML = (WEB_DIR / "index.html").read_text(encoding="utf-8")
CSS = (WEB_DIR / "app.css").read_text(encoding="utf-8")


@pytest.fixture
def client(tmp_path):
    service = RagService(
        tmp_path, ollama_probe=lambda: {"available": False, "url": "", "models": []}
    )
    app = create_app(service, allowed_hosts=[*LOCAL_HOSTS, "testserver"])
    return TestClient(app, raise_server_exceptions=False)


def test_index_and_assets_are_served_with_security_headers(client):
    page = client.get("/")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "Proyecto no oficial" in page.text and 'lang="es"' in page.text
    assert "script-src 'self'" in page.headers["Content-Security-Policy"]
    for asset, kind in (("/static/app.js", "javascript"), ("/static/app.css", "css")):
        response = client.get(asset)
        assert response.status_code == 200 and kind in response.headers["content-type"]


@pytest.mark.parametrize("path", ["/static/../api.py", "/static/%2e%2e/api.py",
                                  "/static/..%2fservice.py", "/static/nada.js"])  # fmt: skip
def test_static_files_cannot_escape_their_directory(client, path):
    assert client.get(path).status_code == 404


def test_no_inline_scripts_styles_or_external_resources():
    assert re.search(r"<script(?![^>]*\bsrc=)", HTML) is None  # sin JS en línea (CSP)
    assert " style=" not in HTML and "<style" not in HTML
    assert re.search(r"\son[a-z]+=", HTML) is None  # sin manejadores de eventos en línea
    for text in (HTML, CSS, JS):
        assert re.search(r"(?:src|href)=[\"']https?://", text) is None
        assert "@import" not in text and "cdn" not in text.lower()


def test_untrusted_text_is_never_inserted_as_html():
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert forbidden not in JS
    assert "textContent" in JS or "createTextNode" in JS
    assert "safeUrl" in JS and 'protocol === "https:"' in JS


def test_every_api_route_used_by_the_page_exists(client):
    used = set(re.findall(r'call\("(/[a-z]+)"', JS))
    assert used == {"/status", "/reports", "/query", "/compare", "/summarize", "/search"}
    schema = client.get("/api/v1/openapi.json").json()["paths"]
    assert {f"/api/v1{path}" for path in used} <= set(schema)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js no disponible")
def test_javascript_syntax_is_valid():
    result = subprocess.run(["node", "--check", str(WEB_DIR / "app.js")], capture_output=True)
    assert result.returncode == 0, result.stderr


def test_assets_are_packaged_inside_the_module():
    assert WEB_DIR.is_relative_to(Path(__file__).parents[1] / "src")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js no disponible")
def test_render_never_inserts_null_or_html_from_data():
    script = Path(__file__).parent / "web_dom_check.mjs"
    result = subprocess.run(
        ["node", str(script), str(WEB_DIR / "app.js")], capture_output=True, text=True
    )
    assert result.returncode == 0 and result.stdout.strip() == "ok", result.stderr


def test_forms_ignore_stale_responses_and_tabs_support_keyboard():
    assert "current === sequence" in JS and "button.disabled = true" in JS
    for key in ("ArrowRight", "ArrowLeft", "Home", "End"):
        assert key in JS
    assert 'placeholder="AAAA-MM"' in HTML
