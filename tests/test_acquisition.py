"""Adquisición asistida: freno tras un bloqueo antibot, ventana sin descargas e importación."""

import json

import httpx
import pytest
from pdf_factory import make_pdf
from test_catalog import Clock, Source, counting, make_catalog, report
from test_index_store import run_cli

from politica_monetaria_banrep_rag.catalog import CatalogError
from politica_monetaria_banrep_rag.download import DownloadBlockedError

MONTHS = {1: "Enero", 4: "Abril", 7: "Julio", 10: "Octubre"}


@pytest.fixture
def pdf_bytes():
    return make_pdf([["Página"]])


def ipm(tmp_path, month, name=None):
    """PDF con portada, título e ISSN del informe principal de 2026."""
    path = tmp_path / (name or f"ipm-{month}.pdf")
    title = f"Informe de Política Monetaria, {MONTHS[month].lower()} de 2026"
    pages = [["ISSN - 2711 - 1164", "INFORME DE POLÍTICA MONETARIA", f"{MONTHS[month]} de 2026"]]
    path.write_bytes(make_pdf(pages, title=title))
    return path


def test_block_stops_every_later_download_and_lists_what_is_missing(tmp_path, pdf_bytes):
    calls = []
    source = Source([report(7), report(4), report(1)])
    catalog, client = make_catalog(tmp_path, source, counting(pdf_bytes, calls, 302))
    with client, pytest.raises(DownloadBlockedError) as error:
        catalog.sync(3)
    assert len(calls) == 1  # un solo reto y ninguna solicitud más
    message = str(error.value)
    assert "antibot" in message and "Faltan 3 informe(s)" in message
    assert all(f"https://www.banrep.gov.co/es/informe/{m}" in message for m in (7, 4, 1))
    assert catalog.current() is None


def test_block_falls_back_to_imports_for_the_rest_of_the_window(tmp_path, pdf_bytes):
    calls, messages = [], []
    source = Source([report(7), report(4)])
    catalog, client = make_catalog(
        tmp_path, source, counting(pdf_bytes, calls, 302), progress=messages.append
    )
    with client:
        for month in (7, 4):
            catalog.import_pdf(ipm(tmp_path, month))
        catalog.sync(2, use_imports=True)
    assert len(calls) == 1
    snapshot = catalog.current()[1]
    assert snapshot["selection"]["mode"] == "latest_n"
    assert [r["acquisition"]["method"] for r in snapshot["reports"]] == ["local_import"] * 2
    assert any("no se solicitarán más PDF" in m for m in messages)


def test_window_without_downloads_never_touches_the_network(tmp_path, pdf_bytes):
    calls = []
    source = Source([report(7), report(4)])
    catalog, client = make_catalog(tmp_path, source, counting(pdf_bytes, calls))
    with client:
        catalog.import_pdf(ipm(tmp_path, 7))
        assert [row["status"] for row in catalog.plan(2, allow_downloads=False)["window"]] == [
            "importado",
            "falta",
        ]
        with pytest.raises(CatalogError, match="Faltan 1 informe") as error:
            catalog.sync(2, allow_downloads=False)
        assert "2026-04" in str(error.value) and "2026-07" not in str(error.value)
        catalog.import_pdf(ipm(tmp_path, 4))
        snapshot_id = catalog.sync(2, allow_downloads=False)
    assert calls == [] and catalog.current()[0] == snapshot_id
    assert catalog.revalidations() == {}  # no se registra un intento que no ocurrió


def test_cooldown_after_a_block_is_respected_and_can_be_overridden(tmp_path, pdf_bytes):
    calls, clock, status = [], Clock(), [302]

    def respond(request):
        return counting(pdf_bytes, calls, status[0])(request)

    catalog, client = make_catalog(tmp_path, Source([report(7)]), respond, wall_clock=clock)
    with client:
        with pytest.raises(DownloadBlockedError):
            catalog.sync(1)
        status[0] = 200
        clock.now += 3600
        plan = catalog.plan(1)
        assert plan["blocked_until"] and plan["window"][0]["status"] == "falta"
        with pytest.raises(CatalogError, match="Faltan 1"):
            catalog.sync(1)
        assert len(calls) == 1  # durante la espera no se pidió nada
        catalog.sync(1, ignore_cooldown=True)
        assert len(calls) == 2
    assert catalog.blocked_until() is None  # la descarga verificada reemplaza el bloqueo


def test_cooldown_expires(tmp_path, pdf_bytes):
    calls, clock, status = [], Clock(), [302]

    def respond(request):
        return counting(pdf_bytes, calls, status[0])(request)

    catalog, client = make_catalog(tmp_path, Source([report(7)]), respond, wall_clock=clock)
    with client:
        with pytest.raises(DownloadBlockedError):
            catalog.sync(1)
        status[0] = 200
        clock.now += 12 * 3600 + 1
        catalog.sync(1)
    assert len(calls) == 2


def test_expired_download_is_kept_without_asking_the_source(tmp_path, pdf_bytes):
    calls, clock = [], Clock()
    catalog, client = make_catalog(
        tmp_path, Source([report(7)]), counting(pdf_bytes, calls), wall_clock=clock
    )
    with client:
        first = catalog.sync(1)
        clock.now += 40 * 86400
        assert catalog.plan(1, allow_downloads=False)["window"][0]["status"] == "pdf_anterior"
        assert catalog.sync(1, allow_downloads=False) == first
    assert len(calls) == 1


class NetSource(Source):
    def __init__(self, reports):
        super().__init__(reports)
        self.client = httpx.Client()


def test_cli_missing_lists_official_links(monkeypatch, capsys, tmp_path):
    from politica_monetaria_banrep_rag import catalog as catalog_module
    from politica_monetaria_banrep_rag import cli

    source = NetSource([report(7), report(4)])
    monkeypatch.setattr(cli, "_make_source", lambda name: source)
    data = str(tmp_path / "datos")
    catalog = catalog_module.Catalog(tmp_path / "datos", source, cli.PdfDownloader())
    catalog.import_pdf(ipm(tmp_path, 7))
    code, out, _ = run_cli(monkeypatch, capsys, "faltantes", "--max-reports", "2",
                           "--data-dir", data)  # fmt: skip
    assert code == 0 and "importado" in out and "FALTA" in out
    assert "https://www.banrep.gov.co/es/informe/4" in out and "Faltan 1 de 2" in out
    code, out, _ = run_cli(monkeypatch, capsys, "faltantes", "--max-reports", "2", "--json",
                           "--data-dir", data)  # fmt: skip
    assert json.loads(out)["missing"] == 1
    code, out, err = run_cli(monkeypatch, capsys, "sincronizar", "--sin-descargar",
                             "--max-reports", "2", "--data-dir", data)  # fmt: skip
    assert code == 1 and "Faltan 1 informe" in err


def test_cli_imports_a_folder_and_skips_other_documents(monkeypatch, capsys, tmp_path):
    folder = tmp_path / "Descargas"
    folder.mkdir()
    ipm(folder, 7)
    (folder / "presentacion.pdf").write_bytes(
        make_pdf(
            [["Presentación", "INFORME DE POLÍTICA MONETARIA", "Julio de 2026"]],
            title="Presentación de PowerPoint",
        )  # fmt: skip
    )
    (folder / "roto.pdf").write_bytes(b"no es un pdf")
    (folder / "notas.txt").write_text("x")
    data = str(tmp_path / "datos")
    code, out, err = run_cli(monkeypatch, capsys, "importar", str(folder), "--sin-red",
                             "--data-dir", data)  # fmt: skip
    assert code == 0 and "Importado ipm-2026-07" in out
    assert "Omitido «presentacion.pdf»" in err and "Omitido «roto.pdf»" in err
    assert "1 importado(s), 2 omitido(s)" in err
    empty = tmp_path / "vacia"
    empty.mkdir()
    code, _, err = run_cli(monkeypatch, capsys, "importar", str(empty), "--sin-red",
                           "--data-dir", data)  # fmt: skip
    assert code == 1 and "ningún informe" in err
    code, _, err = run_cli(monkeypatch, capsys, "importar", str(folder), "--periodo", "2026-07",
                           "--sin-red", "--data-dir", data)  # fmt: skip
    assert code == 2


def test_cli_single_file_errors_still_fail(monkeypatch, capsys, tmp_path):
    bad = tmp_path / "roto.pdf"
    bad.write_bytes(b"no es un pdf")
    code, _, err = run_cli(monkeypatch, capsys, "importar", str(bad), "--sin-red",
                           "--data-dir", str(tmp_path / "datos"))  # fmt: skip
    assert code == 1 and "no es un PDF" in err
