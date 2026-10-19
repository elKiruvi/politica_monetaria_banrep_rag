"""Detector de publicaciones y actualización local, sin red."""

import json
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from test_index_store import run_cli

from politica_monetaria_banrep_rag import detector
from politica_monetaria_banrep_rag.source import ReportRecord, SourceBlockedError, SourceError

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def record(period: str, **changes) -> ReportRecord:
    base = ReportRecord(
        report_id=f"ipm-{period}",
        title=f"Informe de Política Monetaria {period}",
        document_type="main_report",
        language="es",
        reference_period=period,
        published_at=f"{period}-28",
        publication_date_origin="crossref:published",
        discovered_at="2026-10-19T00:00:00+00:00",
        file_updated_at=None,
        source_url=f"https://doi.org/10.32468/{period}",
        download_url=f"https://repositorio.banrep.gov.co/bitstream/handle/1/{period}.pdf",
        doi=f"10.32468/{period}",
        discovery_source="crossref",
    )
    return replace(base, **changes)


class Source:
    def __init__(self, records=None, error=None):
        self.records = records or []
        self.error = error
        self.client = httpx.Client()

    def discover(self, max_reports=12):
        if self.error:
            raise self.error
        return self.records[:max_reports]


@pytest.fixture
def reference(tmp_path):
    path = tmp_path / "informes.json"
    detector.write_reference(path, [record("2026-07"), record("2026-04")], now=NOW)
    return path


def test_unchanged_window_ignores_volatile_fields(reference):
    known = detector.load_reference(reference)
    current = [record("2026-07", discovered_at="2027-01-01T00:00:00+00:00",
                      metadata_deposited_at="2027-01-01"), record("2026-04")]  # fmt: skip
    result, _ = detector.detect(Source(current), known, now=NOW)
    assert result.status == "sin_cambios" and result.changes == ()
    assert result.window == ("ipm-2026-07", "ipm-2026-04")


def test_new_report_document_update_and_metadata_correction(reference):
    known = detector.load_reference(reference)
    current = [
        record("2026-10"),
        record(
            "2026-07", download_url="https://repositorio.banrep.gov.co/bitstream/handle/1/v2.pdf"
        ),
        record("2026-04", title="Informe de Política Monetaria - abril de 2026 (corregido)"),
    ]
    result, _ = detector.detect(Source(current), known, now=NOW)
    assert result.status == "cambios"
    kinds = {change.report_id: (change.kind, change.fields) for change in result.changes}
    assert kinds == {
        "ipm-2026-10": ("nuevo_informe", ()),
        "ipm-2026-07": ("documento_actualizado", ("download_url",)),
        "ipm-2026-04": ("correccion_metadatos", ("title",)),
    }


def test_change_keys_are_stable_and_specific(reference):
    known = detector.load_reference(reference)
    first, _ = detector.detect(Source([record("2026-10")]), known, now=NOW)
    again, _ = detector.detect(Source([record("2026-10")]), known, now=NOW)
    other, _ = detector.detect(Source([record("2026-10", title="otro")]), known, now=NOW)
    assert first.changes[0].key == again.changes[0].key  # mismo cambio: mismo aviso
    assert first.changes[0].key != other.changes[0].key
    assert first.changes[0].key.startswith("ipm-2026-10:nuevo_informe:")


@pytest.mark.parametrize(
    "error,status",
    [
        (SourceBlockedError("reto antibot"), "fuente_no_disponible"),
        (SourceError("Crossref respondió HTTP 503."), "fuente_no_disponible"),
        (SourceError("Crossref devolvió un listado con formato inesperado."), "respuesta_ambigua"),
        (httpx.ConnectError("sin red"), "error_red"),
    ],
)
def test_access_failures_are_never_reported_as_no_news(reference, error, status):
    result, records = detector.detect(Source(error=error), {}, now=NOW)
    assert result.status == status and records == [] and result.changes == ()
    assert detector.EXIT_CODES[status] not in {0, 10}


def test_network_cause_inside_source_error_is_a_network_error():
    try:
        raise httpx.ReadTimeout("lento")
    except httpx.ReadTimeout as cause:
        error = SourceError("No se pudo consultar Crossref.")
        error.__cause__ = cause
    result, _ = detector.detect(Source(error=error), {}, now=NOW)
    assert result.status == "error_red"


def test_reference_format_is_validated(tmp_path):
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"version": 9, "reports": []}))
    with pytest.raises(ValueError):
        detector.load_reference(path)
    path.write_text(json.dumps({"version": 1, "reports": [{"title": "sin id"}]}))
    with pytest.raises(ValueError):
        detector.load_reference(path)


def test_reference_contains_metadata_only(reference):
    data = json.loads(reference.read_text())
    assert set(data["reports"][0]) == set(detector._TRACKED)
    assert reference.stat().st_size < 10_000  # solo metadatos, nunca el contenido de un PDF
    assert "%PDF" not in reference.read_text()


def test_cli_exit_codes_and_reference_update(monkeypatch, capsys, tmp_path, reference):
    from politica_monetaria_banrep_rag import cli

    source = Source([record("2026-07"), record("2026-04")])
    monkeypatch.setattr(cli, "CrossrefReportSource", lambda contact_email=None: source)
    code, out, _ = run_cli(monkeypatch, capsys, "detectar", "--referencia", str(reference))
    assert code == 0 and "Sin cambios" in out
    source.records = [record("2026-10"), *source.records]
    code, out, _ = run_cli(monkeypatch, capsys, "detectar", "--referencia", str(reference),
                           "--json")  # fmt: skip
    assert code == 10 and json.loads(out)["changes"][0]["kind"] == "nuevo_informe"
    code, _, err = run_cli(monkeypatch, capsys, "detectar", "--referencia", str(reference),
                           "--actualizar-referencia")  # fmt: skip
    assert code == 0 and "Referencia actualizada" in err
    code, _, _ = run_cli(monkeypatch, capsys, "detectar", "--referencia", str(reference))
    assert code == 0
    source.error = SourceBlockedError("reto antibot")
    code, out, _ = run_cli(monkeypatch, capsys, "detectar", "--referencia", str(reference))
    assert code == 20 and "no se sabe si hay novedades" in out
    code, _, err = run_cli(monkeypatch, capsys, "detectar", "--referencia", str(reference),
                           "--actualizar-referencia")  # fmt: skip
    assert code == 1 and "no se actualiza la referencia" in err


def test_update_continues_with_active_catalog_only_when_asked(monkeypatch, capsys, tmp_path):
    from politica_monetaria_banrep_rag import cli

    calls = []

    def failing_sync(parser, args):
        calls.append("sync")
        parser.exit(1, "Error de sincronización: reto antibot\n")

    monkeypatch.setattr(cli, "_run_sync", failing_sync)
    monkeypatch.setattr(cli, "_process", lambda parser, args: calls.append("process"))
    monkeypatch.setattr(cli, "_index", lambda parser, args: calls.append("index"))
    code, _, err = run_cli(monkeypatch, capsys, "actualizar", "--data-dir", str(tmp_path))
    assert code == 1 and calls == ["sync"] and "reto antibot" in err
    calls.clear()
    code, out, err = run_cli(monkeypatch, capsys, "actualizar", "--continuar-si-falla",
                             "--sin-embeddings", "--data-dir", str(tmp_path))  # fmt: skip
    assert code == 0 and calls == ["sync", "process", "index"]
    assert "no equivale a los últimos informes" in err and "procesamiento, indexación" in out
