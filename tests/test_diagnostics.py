import io
import json

import httpx
import pytest
from pypdf import PdfWriter

from politica_monetaria_banrep_rag import diagnostics
from politica_monetaria_banrep_rag.cli import main

CHALLENGE = "https://validate.perfdrive.com/?ssa=1&ssr=MjA2Ljg0LjgxLjIyMA==&ssc=https%3A%2F%2Fx"
LEGACY = "https://repositorio.banrep.gov.co/bitstream/handle/20.500.12134/11411/IPM-julio-2026.pdf"


def crossref_payload():
    item = {
        "DOI": "10.32468/inf-pol-mont-spa.tr3-2026",
        "title": ["Informe de Política Monetaria - julio de 2026"],
        "type": "report",
        "publisher": "Banco de la República",
        "published": {"date-parts": [[2026, 8, 4]]},
        "resource": {"primary": {"URL": LEGACY}},
    }
    return {"status": "ok", "message-type": "work-list",
            "message": {"total-results": 1, "items": [item]}}  # fmt: skip


def respond(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if request.url.host == "api.crossref.org":
        return httpx.Response(200, json=crossref_payload())
    if "page=1" in url:
        return httpx.Response(200, text="<html>Radware Bot Manager Block</html>")
    if request.url.host == "www.banrep.gov.co":
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html></html>")
    if url.endswith("robots.txt"):
        return httpx.Response(200, text="Disallow: /server")
    if request.url.host == "doi.org":
        return httpx.Response(302, headers={"location": LEGACY})
    if request.url.host == "repositorio.banrep.gov.co":
        return httpx.Response(302, headers={"location": CHALLENGE})
    raise AssertionError(url)


def test_run_classifies_probes_and_hides_challenge_query():
    pauses = []
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        report = diagnostics.run(client=client, pause=pauses.append)
    outcomes = {row["name"]: row["outcome"] for row in report["sondas"]}
    assert outcomes == {
        "crossref": "ok",
        "crossref_ultimo_informe": "ok",
        "historico_portada": "ok",
        "repositorio_robots": "ok",
        "doi_ultimo_informe": "redireccion",
        "pdf_ultimo_informe": "reto_antibot",
        "historico_pagina_2": "reto_antibot",
    }
    assert [row["name"] for row in report["sondas"]][-1] == "historico_pagina_2"
    text = json.dumps(report)
    assert "MjA2" not in text and "ssa=" not in text
    assert "https://validate.perfdrive.com/" in text
    assert all(p == 2.0 for p in pauses)
    assert report["descarga"] is None
    assert "| pdf_ultimo_informe | reto_antibot | 302 |" in diagnostics.render(report)


@pytest.mark.parametrize(
    "error,outcome",
    [
        (httpx.ConnectError("[Errno -3] Temporary failure in name resolution"), "dns"),
        (httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]"), "tls"),
        (httpx.ConnectError("Connection refused"), "conexion"),
        (httpx.ReadTimeout("lento"), "timeout"),
    ],
)
def test_network_failures_are_distinguished(error, outcome):
    def fail(_):
        raise error

    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        assert diagnostics.probe(client, "x", "https://doi.org/x").outcome == outcome


@pytest.mark.parametrize(
    "response,outcome",
    [
        (httpx.Response(429), "limite_de_tasa"),
        (httpx.Response(403), "http_4xx"),
        (httpx.Response(503), "http_5xx"),
        (httpx.Response(200, content=b"%PDF-1.7"), "pdf"),
        (httpx.Response(301, headers={"location": "/bitstreams/x/download"}), "redireccion"),
    ],
)
def test_http_outcomes(response, outcome):
    with httpx.Client(transport=httpx.MockTransport(lambda _: response)) as client:
        assert diagnostics.probe(client, "x", "https://doi.org/x").outcome == outcome


def test_download_one_reports_hash_and_pages(monkeypatch):
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    output = io.BytesIO()
    writer.write(output)
    pdf = output.getvalue()

    def with_pdf(request):
        if request.url.host == "repositorio.banrep.gov.co":
            return httpx.Response(
                200, headers={"content-type": "application/pdf"}, stream=httpx.ByteStream(pdf)
            )
        return respond(request)

    real_downloader = diagnostics.PdfDownloader
    monkeypatch.setattr(
        diagnostics,
        "PdfDownloader",
        lambda: real_downloader(httpx.Client(transport=httpx.MockTransport(with_pdf))),
    )
    with httpx.Client(transport=httpx.MockTransport(with_pdf)) as client:
        report = diagnostics.run(client=client, download_one=True, pause=lambda _: None)
    assert report["descarga"]["resultado"] == "pdf_verificado"
    assert report["descarga"]["paginas"] == 1
    assert len(report["descarga"]["sha256"]) == 64


def test_cli_prints_table(capsys, monkeypatch):
    monkeypatch.setattr(diagnostics, "run", lambda download_one: {
        "iniciado": "2026-10-16T00:00:00+00:00", "sondas": [
            {"name": "crossref", "outcome": "ok", "status": 200, "content_type": None,
             "location": None, "detail": None}], "descarga": None})  # fmt: skip
    monkeypatch.setattr("sys.argv", ["banrep-rag", "diagnostico-fuentes"])
    main()
    assert "| crossref | ok | 200 |" in capsys.readouterr().out
