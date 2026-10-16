import json
from datetime import date

import pytest
from pdf_factory import make_pdf
from test_catalog import Source, counting, make_catalog, report
from test_crossref import from_fixtures, make_source

from politica_monetaria_banrep_rag.catalog import CatalogError
from politica_monetaria_banrep_rag.cli import main
from politica_monetaria_banrep_rag.importer import ImportFileError, identify

TITLE = "Informe de Política Monetaria, enero de 2025"
COVER = [["01/ 2025", "INFORME DE POLÍTICA MONETARIA", "Enero de 2025", "Banco de la República"]]


def write(tmp_path, name="ipm.pdf", pages=COVER, title=TITLE):
    path = tmp_path / name
    path.write_bytes(make_pdf(pages, title=title))
    return path


def test_identify_title_and_cover_agree(tmp_path):
    found = identify(write(tmp_path))
    assert found.report_id == "ipm-2025-01"
    assert found.evidence == ("metadato /Title", "portada")


@pytest.mark.parametrize(
    "pages,title,period,error",
    [
        (COVER, "Informe de Política Monetaria, abril de 2025", None, "contradictorios"),
        ([["Reporte de Estabilidad Financiera"]], "Otro documento", None, "no parece"),
        ([["Reporte de Estabilidad Financiera"]], None, "2025-01", "no parece"),
        (COVER, TITLE, "2025-04", "contradictorios"),
        (COVER, TITLE, "2025-13", "AAAA-MM"),
    ],
)
def test_identify_rejects_ambiguous_files(tmp_path, pages, title, period, error):
    with pytest.raises(ImportFileError, match=error):
        identify(write(tmp_path, pages=pages, title=title), period)


def test_identify_accepts_confirming_period_and_rejects_non_pdf(tmp_path):
    assert identify(write(tmp_path), "2025-01").evidence == (
        "metadato /Title",
        "periodo indicado",
        "portada",
    )
    bogus = tmp_path / "x.pdf"
    bogus.write_text("<html>no</html>")
    with pytest.raises(ImportFileError, match="No se pudo leer"):
        identify(bogus)


def test_import_registers_and_activates_honest_local_catalog(tmp_path, pdf_bytes):
    catalog, client = make_catalog(tmp_path, Source([]), lambda _: pytest.fail())
    path = write(tmp_path)
    with client:
        entry = catalog.import_pdf(path, identify(path), None)
        snapshot_id = catalog.activate_imports()
    assert (catalog.root / str(entry["path"])).read_bytes() == path.read_bytes()
    active_id, snapshot = catalog.current()
    assert active_id == snapshot_id
    assert snapshot["selection"] == {"mode": "local_import"}
    record = snapshot["reports"][0]
    assert record["report"]["report_id"] == "ipm-2025-01"
    assert record["report"]["discovery_source"] == "local_import"
    assert record["acquisition"]["method"] == "local_import"
    assert record["acquisition"]["verified_against_source"] is False
    assert record["acquisition"]["metadata_verified"] is False
    assert record["acquisition"]["source_name"] == "ipm.pdf"
    assert str(tmp_path) not in json.dumps(snapshot)  # Nunca se guarda la ruta personal.


def test_import_rejects_invalid_pdf_and_mismatched_metadata(tmp_path):
    catalog, client = make_catalog(tmp_path, Source([]), lambda _: pytest.fail())
    path = write(tmp_path)
    broken = tmp_path / "roto.pdf"
    broken.write_bytes(b"%PDF-1.4 truncado")
    with client:
        with pytest.raises(CatalogError, match="no es un PDF válido"):
            catalog.import_pdf(broken, identify(path), None)
        with pytest.raises(CatalogError, match="otro periodo"):
            catalog.import_pdf(path, identify(path), report(7))
        with pytest.raises(CatalogError, match="No hay PDF importados"):
            catalog.activate_imports()
    assert list((catalog.root / "raw").iterdir()) == []


def test_sync_uses_import_only_when_allowed_and_download_fails(tmp_path, pdf_bytes):
    path = write(tmp_path, pages=[["INFORME DE POLÍTICA MONETARIA", "Julio de 2026"]],
                 title="Informe de Política Monetaria, julio de 2026")  # fmt: skip
    calls = []
    catalog, client = make_catalog(tmp_path, Source([report(7)]), counting(pdf_bytes, calls, 302))
    with client:
        catalog.import_pdf(path, identify(path), None)
        with pytest.raises(Exception, match="antibot"):
            catalog.sync(1)
        assert catalog.current() is None
        catalog.sync(1, use_imports=True)
    record = catalog.current()[1]["reports"][0]
    assert record["acquisition"]["method"] == "local_import"
    assert record["report"]["download_url"] == report(7).download_url  # metadatos verificados
    assert catalog.current()[1]["selection"]["mode"] == "latest_n"


def test_crossref_find_returns_the_requested_period():
    source, requests, _ = make_source(from_fixtures)
    with source.client:
        found = source.find("ipm-2024-04")
        missing = source.find("ipm-2024-05")
    assert found.doi == "10.32468/inf-pol-mont-spa.tr2-2024"
    assert missing is None
    assert len(requests) == 4  # 2024 y 2025 para cada búsqueda


def test_crossref_find_skips_future_years():
    source, requests, _ = make_source(from_fixtures)
    with source.client:
        assert source.find("ipm-2026-07").published_at == "2026-08-04"
    assert len(requests) == 1
    assert date(2026, 10, 16).year == 2026


def test_cli_import_offline_and_status(tmp_path, capsys, monkeypatch):
    path = write(tmp_path)
    data = tmp_path / "datos"
    monkeypatch.setattr(
        "sys.argv",
        ["banrep-rag", "importar", str(path), "--data-dir", str(data), "--sin-red", "--activar"],
    )
    main()
    out = capsys.readouterr().out
    assert "Importado ipm-2025-01" in out and "metadatos sin verificar" in out
    monkeypatch.setattr("sys.argv", ["banrep-rag", "estado", "--data-dir", str(data)])
    main()
    out = capsys.readouterr().out
    assert "importación local (no equivale a los últimos N informes)" in out
    assert "2025-01" in out and "importado" in out


def test_cli_status_without_catalog(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["banrep-rag", "estado", "--data-dir", str(tmp_path)])
    main()
    assert "No hay catálogo activo" in capsys.readouterr().out


def test_cli_import_rejects_period_with_many_files(tmp_path, capsys, monkeypatch):
    path = write(tmp_path)
    monkeypatch.setattr(
        "sys.argv", ["banrep-rag", "importar", str(path), str(path), "--periodo", "2025-01"]
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


@pytest.fixture
def pdf_bytes():
    return make_pdf([["Documento"]])
