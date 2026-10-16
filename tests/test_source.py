import httpx
import pytest

from politica_monetaria_banrep_rag.source import BanrepSource, SourceError, parse_history

ROOT = "https://www.banrep.gov.co"


def history(rows: str, heading: str = "2026 - julio") -> str:
    return f'<div class="view-publicaciones-buscador"><h3>{heading}</h3>{rows}</div>'


def row(title: str, url: str, download: str) -> str:
    return (
        '<div class="views-row"><div class="titulo"><h2>'
        f'<a href="{url}">{title}</a></h2></div>'
        f'<div class="enlaces"><a href="{download}">Descargar</a>'
        '<a href="https://doi.org/extra">Gráficos</a></div></div>'
    )


def july() -> str:
    return row(
        "Informe de Política Monetaria - julio de 2026", "/es/informe/julio", "https://doi.org/rh7c"
    )


def april() -> str:
    return row(
        "Informe de Política Monetaria - abril de 2026", "/es/informe/abril", "https://doi.org/q5kx"
    )


def test_only_main_report_and_separate_dates():
    html = history(
        row("Presentación del Informe de Política Monetaria - julio de 2026", "/es/p", "/p.pdf")
        + july()
        + row("Recuadro 1: Informe de Política Monetaria - julio de 2026", "/es/r", "/r.pdf")
    )
    reports = parse_history(html, "2026-10-15T00:00:00+00:00")
    assert [r.report_id for r in reports] == ["ipm-2026-07"]
    assert reports[0].published_at is None
    assert reports[0].discovered_at == "2026-10-15T00:00:00+00:00"
    assert reports[0].download_url == "https://doi.org/rh7c"


@pytest.mark.parametrize(
    "html",
    [
        "<head><title>Radware Bot Manager Block</title></head>",
        "<html><body>Sin resultados</body></html>",
        history(july(), "2025 - julio"),
        history(july() + july()),
        history(
            row("Informe de Política Monetaria - julio de 2026", "/es/x", "http://127.0.0.1/x")
        ),
        history(row("Informe de Política Monetaria - julio de 2026", "/es/x", "file:///x")),
        history(row("Informe de Política Monetaria - julio de 2026", "/es/x", "")),
    ],
)
def test_reject_ambiguous_or_blocked_html(html):
    with pytest.raises(SourceError):
        parse_history(html, "2026-10-15T00:00:00+00:00")


def test_latest_n_across_pages_without_complementary_material():
    pages = [history(july()), history(april(), "2026 - abril")]

    def respond(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 0))
        return httpx.Response(200, text=pages[page])

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        reports = BanrepSource(client).discover(2)
    assert [r.report_id for r in reports] == ["ipm-2026-07", "ipm-2026-04"]


def test_blocked_next_page_does_not_return_partial_window():
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=history(july()) if not request.url.params else "Radware Bot Manager Block"
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(SourceError, match="bloqueó"):
            BanrepSource(client).discover(12)


def test_unverified_redirect_is_rejected():
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(302, headers={"location": ROOT}))
    ) as client:
        with pytest.raises(SourceError, match="redirección"):
            BanrepSource(client).discover(1)
