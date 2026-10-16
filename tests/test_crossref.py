import json
import os
from datetime import date
from pathlib import Path

import httpx
import pytest

from politica_monetaria_banrep_rag.crossref import CrossrefReportSource
from politica_monetaria_banrep_rag.source import SourceError

FIXTURES = Path(__file__).parent / "fixtures" / "crossref"
EXPECTED_12 = [
    "2026-07", "2026-04", "2026-01", "2025-10", "2025-07", "2025-04",
    "2025-01", "2024-10", "2024-07", "2024-04", "2024-01", "2023-10",
]  # fmt: skip


def year_of(request: httpx.Request) -> int:
    return int(request.url.params["filter"].split("from-pub-date:")[1][:4])


def payload(year: int) -> dict:
    path = FIXTURES / f"anio-{year}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "status": "ok",
        "message-type": "work-list",
        "message": {"total-results": 0, "items": []},
    }


def make_source(respond, **kwargs):
    requests = []

    def handler(request):
        requests.append(request)
        return respond(request)

    pauses = []
    client = httpx.Client(transport=httpx.MockTransport(handler))
    source = CrossrefReportSource(client, today=date(2026, 10, 16), pause=pauses.append, **kwargs)
    return source, requests, pauses


def from_fixtures(request):
    return httpx.Response(200, json=payload(year_of(request)))


def test_discovers_the_twelve_latest_spanish_main_reports():
    source, requests, pauses = make_source(from_fixtures)
    with source.client:
        reports = source.discover(12)
    assert [r.reference_period for r in reports] == EXPECTED_12
    assert [year_of(r) for r in requests] == [2027, 2026, 2025, 2024, 2023, 2022]
    assert pauses == [0.5] * 5
    assert all(r.document_type == "main_report" and r.language == "es" for r in reports)
    assert all("-spa." in (r.doi or "") for r in reports)
    july = reports[0]
    assert july.title == "Informe de Política Monetaria - julio de 2026"
    assert july.published_at == "2026-08-04"
    assert july.publication_date_origin == "crossref:published"
    assert july.source_url == "https://doi.org/10.32468/inf-pol-mont-spa.tr3-2026"
    assert july.download_url == (
        "https://repositorio.banrep.gov.co/bitstream/handle/20.500.12134/11411/IPM-julio-2026.pdf"
    )
    assert july.discovery_source == "crossref"
    april_2024 = next(r for r in reports if r.reference_period == "2024-04")
    assert april_2024.source_updates == ("correction:2024-05-08",)
    assert "mailto" not in requests[0].url.params


@pytest.mark.parametrize(
    "count,years",
    [(2, [2027, 2026, 2025]), (3, [2027, 2026, 2025]), (4, [2027, 2026, 2025, 2024])],
)
def test_stops_as_soon_as_the_window_is_provably_complete(count, years):
    source, requests, _ = make_source(from_fixtures)
    with source.client:
        reports = source.discover(count)
    assert [r.reference_period for r in reports] == EXPECTED_12[:count]
    assert [year_of(r) for r in requests] == years


def test_insufficient_reports_fail_without_partial_window():
    def respond(request):
        return httpx.Response(
            200, json=payload(year_of(request) if year_of(request) == 2026 else 1)
        )

    source, requests, _ = make_source(respond)
    with source.client, pytest.raises(SourceError, match="Solo se verificaron 3 de 12"):
        source.discover(12)
    assert year_of(requests[-1]) == 2000


def mutate(year, change):
    def respond(request):
        data = payload(year_of(request))
        if year_of(request) == year:
            change(data["message"])
        return httpx.Response(200, json=data)

    return respond


def append(message, item):
    message["items"].append(item)
    message["total-results"] += 1


def july_item(message):
    return next(i for i in message["items"] if i["DOI"].endswith("spa.tr3-2026"))


@pytest.mark.parametrize(
    "change,error",
    [
        (lambda m: append(m, {**july_item(m), "DOI": "10.32468/otro"}), "más de un"),
        (lambda m: m.update({"total-results": m["total-results"] + 1}), "completo"),
        (lambda m: july_item(m).update(title=["Informe de Política Monetaria (IPM) jul 2026"]),
         "clasificar"),
        (lambda m: july_item(m).update(resource={"primary": {"URL": "https://evil.example/x.pdf"}}),
         "no es institucional"),
        (lambda m: july_item(m).update(resource={"primary": {"URL": "https://repositorio.banrep.gov.co/server/api/core/bitstreams/7a119fee-bead-43c1-9671-78e3f45ef54a/content"}}),
         "no es institucional"),
        (lambda m: july_item(m).update(resource={}), "no tiene URL"),
        (lambda m: july_item(m).update(published={"date-parts": [[2026, 6, 30]]}), "no concuerda"),
        (lambda m: july_item(m).update(publisher="Otra entidad"), "no figura"),
        (lambda m: july_item(m).update(DOI="10.9999/ajeno"), "DOI del Banco"),
        (lambda m: july_item(m).update(title=["Informe de Política Monetaria - julio‮ de 2026"]),
         "caracteres de control"),
        (lambda m: append(m, "no es un objeto"), "formato inesperado"),
    ],
)  # fmt: skip
def test_ambiguous_or_unsafe_metadata_fails_closed(change, error):
    source, _, _ = make_source(mutate(2026, change))
    with source.client, pytest.raises(SourceError, match=error):
        source.discover(2)


@pytest.mark.parametrize(
    "response,error",
    [
        (httpx.Response(429, headers={"retry-after": "5"}), "HTTP 429"),
        (httpx.Response(302, headers={"location": "https://evil.example/"}), "HTTP 302"),
        (httpx.Response(200, text="<html>no</html>"), "no es JSON"),
        (httpx.Response(200, json={"status": "failed", "message": []}), "formato inesperado"),
        (httpx.Response(200, content=b"[" * 9_000_001), "excede"),
    ],
)
def test_transport_failures_are_explicit(response, error):
    source, _, _ = make_source(lambda _: response)
    with source.client, pytest.raises(SourceError, match=error):
        source.discover(1)


def test_english_reports_and_unrelated_records_are_ignored():
    source, _, _ = make_source(from_fixtures)
    with source.client:
        reports = source.discover(12)
    assert not any("Monetary" in r.title or "-eng." in (r.doi or "") for r in reports)


def test_contact_email_is_opt_in_and_validated():
    source, requests, _ = make_source(from_fixtures, contact_email="persona@example.org")
    with source.client:
        source.discover(1)
    assert requests[0].url.params["mailto"] == "persona@example.org"
    with pytest.raises(ValueError, match="correo"):
        CrossrefReportSource(contact_email="no-es-correo")
    with pytest.raises(ValueError, match="redirecciones"):
        CrossrefReportSource(httpx.Client(follow_redirects=True))


@pytest.mark.parametrize("count", [0, 101, True])
def test_invalid_window_size(count):
    source, requests, _ = make_source(from_fixtures)
    with source.client, pytest.raises(ValueError):
        source.discover(count)
    assert requests == []


@pytest.mark.live
@pytest.mark.skipif(
    os.getenv("BANREP_LIVE") != "1", reason="Prueba en vivo opcional: BANREP_LIVE=1"
)
def test_live_crossref_window_of_twelve():
    source = CrossrefReportSource()
    with source.client:
        reports = source.discover(12)
    periods = [r.reference_period for r in reports]
    assert len(set(periods)) == 12
    assert periods == sorted(periods, reverse=True)
    assert all(r.published_at and r.doi and "-spa." in r.doi for r in reports)
    print("\n".join(f"{r.reference_period} {r.doi} {r.published_at}" for r in reports))


def edit_july(field, value):
    return mutate(2026, lambda m: july_item(m).update({field: value}))


@pytest.mark.parametrize(
    "title",
    [
        "Informe de Poli\u0301tica Monetaria - julio de 2026",  # NFD: se normaliza y se acepta
        "Informe de Política Monetaria – julio de 2026",
    ],
)
def test_equivalent_unicode_titles_are_normalized(title):
    source, _, _ = make_source(edit_july("title", [title]))
    with source.client:
        assert [r.reference_period for r in source.discover(2)] == ["2026-07", "2026-04"]


@pytest.mark.parametrize(
    "title",
    [
        "Informe de Politica Monetaria - jul. 2026",
        "INFORME DE POLITICA MONETARIA julio",
        "Informe de Política Monetaria - julıo de 2026",
        "Informe de Política Monetaria - julio de ٢٠٢٦",
    ],
)
def test_lookalike_titles_fail_instead_of_shifting_the_window(title):
    source, _, _ = make_source(edit_july("title", [title]))
    with source.client, pytest.raises(SourceError, match="clasificar"):
        source.discover(2)


def test_unrelated_record_with_format_characters_is_ignored():
    def add_noise(message):
        append(message, {"DOI": "10.32468/be.9999", "title": ["Borrador\u00ad de economía"]})

    source, _, _ = make_source(mutate(2026, add_noise))
    with source.client:
        assert len(source.discover(2)) == 2


@pytest.mark.parametrize(
    "year,change",
    [
        # abril de 2026 «publicado» antes de su periodo, en un año que antes no se leía
        (2025, lambda m: append(m, {
            "DOI": "10.32468/inf-pol-mont-spa.x-2026", "type": "report",
            "publisher": "Banco de la República",
            "title": ["Informe de Política Monetaria - mayo de 2026"],
            "published": {"date-parts": [[2025, 12, 30]]},
            "resource": {"primary": {"URL": "https://repositorio.banrep.gov.co/bitstream/handle/20.500.12134/1/x.pdf"}}})),
        # errata con fecha futura
        (2027, lambda m: append(m, {
            "DOI": "10.32468/inf-pol-mont-spa.y-2026", "type": "report",
            "publisher": "Banco de la República",
            "title": ["Informe de Política Monetaria - octubre de 2026"],
            "published": {"date-parts": [[2027, 1, 4]]},
            "resource": {"primary": {"URL": "https://repositorio.banrep.gov.co/bitstream/handle/20.500.12134/2/y.pdf"}}})),
    ],
)  # fmt: skip
def test_anomalous_dates_outside_the_window_are_detected(year, change):
    source, _, _ = make_source(mutate(year, change))
    with source.client, pytest.raises(SourceError, match="no concuerda"):
        source.discover(2)


@pytest.mark.parametrize(
    "published,error",
    [
        ({"date-parts": [[2026, 6]]}, "no concuerda"),
        ({"date-parts": [[2025]]}, "no concuerda"),
        (None, "sin fecha|no tiene fecha"),
        ({"date-parts": [[2026, 13, 1]]}, "no tiene fecha"),
    ],
)
def test_partial_or_missing_dates_are_still_validated(published, error):
    source, _, _ = make_source(edit_july("published", published))
    with source.client, pytest.raises(SourceError, match=error):
        source.discover(2)


def test_partial_date_within_period_is_accepted_without_exact_day():
    source, _, _ = make_source(edit_july("published", {"date-parts": [[2026, 8]]}))
    with source.client:
        july = source.discover(1)[0]
    assert july.published_at is None and july.publication_date_origin is None


def test_future_period_is_rejected():
    def future(message):
        july_item(message).update(title=["Informe de Política Monetaria - julio de 2027"])

    source, _, _ = make_source(mutate(2026, future))
    with source.client, pytest.raises(SourceError, match="no concuerda"):
        source.discover(2)


@pytest.mark.parametrize("updates", [5, [{"DOI": None}], [7, {"DOI": 3}]])
def test_malformed_updates_are_ignored(updates):
    source, _, _ = make_source(edit_july("updated-by", updates))
    with source.client:
        assert source.discover(1)[0].source_updates == ()


def test_english_doi_with_spanish_title_is_ambiguous():
    source, _, _ = make_source(edit_july("DOI", "10.32468/inf-pol-mont-eng.tr3-2026"))
    with source.client, pytest.raises(SourceError, match="DOI en inglés"):
        source.discover(1)


def test_deeply_nested_json_is_a_source_error():
    source, _, _ = make_source(lambda _: httpx.Response(200, content=b"[" * 200_000))
    with source.client, pytest.raises(SourceError, match="JSON"):
        source.discover(1)
