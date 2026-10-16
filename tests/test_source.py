import httpx
import pytest

from politica_monetaria_banrep_rag.source import (
    BanrepSource,
    SourceBlockedError,
    SourceError,
    parse_history,
)


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
        history(row("Informe de Política Monetaria - julio de 2026", "/es/x", "#")),
        history(row("Informe de Política Monetaria - julio de 2026", "/es/x", "/")),
        history(
            row("Informe de Política Monetaria - julio de 2026", "/es/x", "https://doi.org/../")
        ),
        history(
            row("Informe de Política Monetaria - julio de 2026", "/es/x", "https://doi.org/%2e%2e/")
        ),
        history(
            row("Informe de Política Monetaria - julio de 2026", "/es/x", "https://doi.org:bad/x")
        ),
        history(row("Informe de Política Monetaria - julio de 2026", "#", "https://doi.org/x")),
        history(
            row(
                "Informe de Política Monetaria - julio de 2026",
                "/es/x",
                "https://doi.org/x\x1b[31m",
            )
        ),
        history(
            row(
                "Informe de Política Monetaria - julio de 2026\x1b[31m",
                "/es/x",
                "https://doi.org/x",
            )
        ),
        history(
            '<div class="views-row"><div class="titulo">'
            "<p>Informe de Política Monetaria - julio de 2026</p></div></div>"
        ),
        history(
            row("Informe de Política Monetaria (IPM) julio de 2026", "/es/x", "https://doi.org/x")
        ),
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


def test_latest_n_sorts_and_caps_more_than_n_on_a_page():
    html = (
        history(april(), "2026 - abril")
        + history(july())
        + history(
            row(
                "Informe de Política Monetaria - enero de 2026",
                "/es/informe/enero",
                "https://doi.org/enero",
            ),
            "2026 - enero",
        )
    )
    html = html.replace('</div><div class="view-publicaciones-buscador">', "")

    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        reports = BanrepSource(client).discover(2)
    assert [r.report_id for r in reports] == ["ipm-2026-07", "ipm-2026-04"]


def test_page_with_only_complements_continues():
    complementary = history(
        row("Presentación del Informe de Política Monetaria - julio de 2026", "/es/p", "/p.pdf")
    )

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=complementary if not request.url.params else history(july())
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert BanrepSource(client).discover(1)[0].report_id == "ipm-2026-07"


def test_archive_changes_mid_pagination_fail_closed():
    first_page_calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal first_page_calls
        if request.url.params:
            return httpx.Response(200, text=history(april(), "2026 - abril"))
        first_page_calls += 1
        if first_page_calls == 1:
            return httpx.Response(200, text=history(july()))
        newer = row(
            "Informe de Política Monetaria - octubre de 2026",
            "/es/informe/octubre",
            "https://doi.org/octubre",
        )
        return httpx.Response(200, text=history(newer, "2026 - octubre"))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(SourceError, match="cambió durante"):
            BanrepSource(client).discover(2)
    assert first_page_calls == 2


def test_source_page_changes_with_same_doi_fail_closed():
    first_page_calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal first_page_calls
        if request.url.params:
            return httpx.Response(200, text=history(april(), "2026 - abril"))
        first_page_calls += 1
        entry = (
            july() if first_page_calls == 1 else july().replace("/es/informe/julio", "/es/nuevo")
        )
        return httpx.Response(200, text=history(entry))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(SourceError, match="cambió durante"):
            BanrepSource(client).discover(2)


def test_blocked_next_page_does_not_return_partial_window():
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=history(july()) if not request.url.params else "Radware Bot Manager Block"
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(SourceError, match="bloqueó"):
            BanrepSource(client).discover(12)


@pytest.mark.parametrize(
    "location,error,message",
    [
        ("https://validate.perfdrive.com/?ssa=1", SourceBlockedError, "antibot"),
        ("https://www.banrep.gov.co/es/otra", SourceError, "redirección"),
    ],
)
def test_unverified_redirect_is_rejected(location, error, message):
    visited = []

    def respond(request: httpx.Request) -> httpx.Response:
        visited.append(str(request.url))
        return httpx.Response(302, headers={"location": location})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(error, match=message):
            BanrepSource(client).discover(1)
    assert len(visited) == 1


def test_report_with_em_dash_is_not_silently_skipped():
    reports = parse_history(
        history(row("Informe de Política Monetaria — julio de 2026", "/es/x", "https://doi.org/x")),
        "2026-10-15T00:00:00+00:00",
    )
    assert reports[0].report_id == "ipm-2026-07"


def test_client_cannot_follow_redirects():
    with httpx.Client(follow_redirects=True) as client:
        with pytest.raises(ValueError, match="redirecciones"):
            BanrepSource(client)


def test_repeated_period_with_different_title_fails_closed():
    variant = july().replace("julio de 2026", "julio 2026")

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=history(variant if request.url.params else july()))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(SourceError, match="metadatos contradictorios"):
            BanrepSource(client).discover(2)
