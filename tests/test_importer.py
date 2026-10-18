import json
import os
import time
from dataclasses import replace

import pytest
from pdf_factory import make_pdf
from test_catalog import Source, counting, make_catalog, report
from test_crossref import from_fixtures, make_source

from politica_monetaria_banrep_rag.catalog import CatalogError
from politica_monetaria_banrep_rag.cli import main
from politica_monetaria_banrep_rag.importer import ImportFileError, identify
from politica_monetaria_banrep_rag.isolation import IsolationError, run_isolated

TITLE = "Informe de Política Monetaria, enero de 2025"
COVER = [["01/ 2025 ISSN - 2711 - 1164", "INFORME DE POLÍTICA MONETARIA", "Enero de 2025"]]


def write(tmp_path, name="ipm.pdf", pages=COVER, title=TITLE, raw=None):
    path = tmp_path / name
    path.write_bytes(make_pdf(pages, title=title, raw_content=raw))
    return path


def test_real_like_cover_needs_two_independent_evidences(tmp_path):
    found = identify(write(tmp_path))
    assert found.report_id == "ipm-2025-01"
    assert found.evidence == ("ISSN 2711-1164", "metadato /Title", "portada")
    cover_only_title = identify(write(tmp_path, pages=[COVER[0][1:]]))
    assert cover_only_title.evidence == ("metadato /Title", "portada")


def test_title_page_can_be_the_second_page(tmp_path):
    pages = [["04/ 2025 ISSN - 2711 - 1164"], ["Abril de 2025", "INFORME DE POLÍTICA MONETARIA"]]
    found = identify(
        write(tmp_path, pages=pages, title="Informe de Política Monetaria, abril de 2025")
    )
    assert found.report_id == "ipm-2025-04"


@pytest.mark.parametrize(
    "pages,title,error",
    [
        # presentación con portada del IPM
        ([["Presentación", "INFORME DE POLÍTICA MONETARIA", "Enero de 2025"]],
         "Presentación de PowerPoint", "presentación"),
        ([["INFORME DE POLÍTICA MONETARIA", "Enero de 2025"]],
         "Presentación del Informe de Política Monetaria, enero de 2025", "presentación"),
        ([["Anexo estadístico", "INFORME DE POLÍTICA MONETARIA", "Enero de 2025"]], TITLE, "anexo"),
        ([["Comunicado de prensa", "INFORME DE POLÍTICA MONETARIA", "octubre de 2025"]], None,
         "comunicado"),
        ([["MONETARY POLICY REPORT", "January 2025"]], TITLE, "presentación|serie"),
        # REF que cita el IPM lejos del periodo
        ([["Reporte de Estabilidad Financiera", "Septiembre de 2025", "x " * 60,
           "como señaló el Informe de Política Monetaria"]], None, "serie junto a su periodo"),
        # solo portada, sin título ni ISSN
        ([["INFORME DE POLÍTICA MONETARIA", "Enero de 2025"]], "Documento", "una evidencia"),
        # título y portada contradictorios
        (COVER, "Informe de Política Monetaria, abril de 2025", "contradictorios"),
    ],
)  # fmt: skip
def test_other_documents_and_weak_evidence_are_rejected(tmp_path, pages, title, error):
    with pytest.raises(ImportFileError, match=error):
        identify(write(tmp_path, pages=pages, title=title))


def test_update_date_does_not_hide_the_period_next_to_the_title(tmp_path):
    pages = [["01/ 2026 ISSN - 2711 - 1164", "INFORME DE POLÍTICA MONETARIA", "Enero de 2026",
              "Actualización 5 de febrero de 2026"]]  # fmt: skip
    found = identify(
        write(tmp_path, pages=pages, title="Informe de Política Monetaria, enero de 2026")
    )
    assert found.report_id == "ipm-2026-01"


def test_expected_period_only_confirms(tmp_path):
    assert "periodo indicado" in identify(write(tmp_path), "2025-01").evidence
    with pytest.raises(ImportFileError, match="contradictorios"):
        identify(write(tmp_path), "2025-04")
    with pytest.raises(ImportFileError, match="AAAA-MM"):
        identify(write(tmp_path), "2025-13")


def test_hostile_pdf_is_contained(tmp_path):
    nested = b"[" * 200_000 + b"]" * 200_000
    path = write(tmp_path, "hostil.pdf", raw=nested)
    with pytest.raises(ImportFileError, match="No se pudo leer"):
        identify(path)


def sleeper(seconds):
    time.sleep(seconds)


def test_isolation_stops_slow_work():
    started = time.monotonic()
    with pytest.raises(IsolationError, match="tiempo máximo"):
        run_isolated(sleeper, 30, timeout=1)
    assert time.monotonic() - started < 15


def test_import_identifies_the_private_copy_and_keeps_provenance(tmp_path, monkeypatch):
    seen = []
    from politica_monetaria_banrep_rag import catalog as module

    real = module.identify
    monkeypatch.setattr(
        module, "identify", lambda path, period, **kw: seen.append(path) or real(path, period, **kw)
    )
    catalog, client = make_catalog(tmp_path, Source([]), lambda _: pytest.fail())
    path = write(tmp_path)
    with client:
        entry = catalog.import_pdf(path)
        snapshot_id = catalog.activate_imports()
    assert seen[0].parent == catalog.root / "raw" and seen[0].name.startswith(".importacion-")
    assert (catalog.root / str(entry["path"])).read_bytes() == path.read_bytes()
    active_id, snapshot = catalog.current()
    assert active_id == snapshot_id and snapshot["selection"] == {"mode": "local_import"}
    acquisition = snapshot["reports"][0]["acquisition"]
    assert acquisition["method"] == "local_import"
    assert acquisition["verified_against_source"] is False
    assert acquisition["metadata_verified"] is False
    assert (
        str(tmp_path) not in json.dumps(snapshot) + (catalog.root / "acquisitions.json").read_text()
    )


def test_import_rejects_missing_special_large_and_invalid_files(tmp_path):
    catalog, client = make_catalog(tmp_path, Source([]), lambda _: pytest.fail())
    broken = tmp_path / "roto.pdf"
    broken.write_bytes(b"%PDF-1.4 truncado")
    big = tmp_path / "grande.pdf"
    with big.open("wb") as handle:
        handle.truncate(31_000_000)
    fifo = tmp_path / "tubo.pdf"
    os.mkfifo(fifo)
    with client:
        with pytest.raises(CatalogError, match="no existe"):
            catalog.import_pdf(tmp_path / "nada.pdf")
        with pytest.raises(CatalogError, match="no es un archivo regular"):
            catalog.import_pdf(fifo)
        with pytest.raises(CatalogError, match="excede 30 MB"):
            catalog.import_pdf(big)
        with pytest.raises(CatalogError, match="no es un PDF válido"):
            catalog.import_pdf(broken)
    assert [p.name for p in (catalog.root / "raw").iterdir()] == []


def test_unusual_month_requires_verification_and_lookup_must_confirm(tmp_path):
    pages = [["ISSN - 2711 - 1164", "INFORME DE POLÍTICA MONETARIA", "Septiembre de 2025"]]
    odd = write(tmp_path, "sep.pdf", pages, "Informe de Política Monetaria, septiembre de 2025")
    catalog, client = make_catalog(tmp_path, Source([]), lambda _: pytest.fail())
    with client:
        with pytest.raises(ImportFileError, match="mes habitual"):
            catalog.import_pdf(odd)
        with pytest.raises(ImportFileError, match="no es el informe principal"):
            catalog.import_pdf(write(tmp_path), lookup=lambda _: None)
        verified = replace(report(1), report_id="ipm-2025-01", reference_period="2025-01")
        entry = catalog.import_pdf(write(tmp_path), lookup=lambda _: verified)
    assert entry["metadata_verified"] is True


def test_different_pdf_for_same_period_needs_replace(tmp_path):
    catalog, client = make_catalog(tmp_path, Source([]), lambda _: pytest.fail())
    first = write(tmp_path, "a.pdf")
    second = write(tmp_path, "b.pdf", pages=[COVER[0] + ["versión corregida"]])
    with client:
        catalog.import_pdf(first)
        with pytest.raises(CatalogError, match="--reemplazar"):
            catalog.import_pdf(second)
        catalog.import_pdf(second, replace=True)
        catalog.import_pdf(second)  # el mismo archivo otra vez no es un reemplazo
    entries = json.loads((catalog.root / "acquisitions.json").read_text())["entries"]
    assert [e["source_name"] for e in entries["ipm-2025-01"]] == ["b.pdf"]


@pytest.mark.parametrize(
    "field,value",
    [("report", None), ("report", "x"), ("source_name", None), ("metadata_verified", "si"),
     ("identified_by", [1])],
)  # fmt: skip
def test_damaged_import_entries_are_rejected(tmp_path, field, value):
    catalog, client = make_catalog(tmp_path, Source([]), lambda _: pytest.fail())
    with client:
        catalog.import_pdf(write(tmp_path))
        path = catalog.root / "acquisitions.json"
        data = json.loads(path.read_text())
        entry = data["entries"]["ipm-2025-01"][0]
        if value is None:
            entry.pop(field)
        else:
            entry[field] = value
        path.write_text(json.dumps(data))
        with pytest.raises(CatalogError, match="registro local"):
            catalog.activate_imports()


def test_import_entry_with_foreign_report_id_is_rejected(tmp_path):
    catalog, client = make_catalog(tmp_path, Source([]), lambda _: pytest.fail())
    with client:
        catalog.import_pdf(write(tmp_path))
        path = catalog.root / "acquisitions.json"
        data = json.loads(path.read_text())
        data["entries"]["ipm-2025-01"][0]["report"]["report_id"] = "ipm-1999-01"
        path.write_text(json.dumps(data))
        with pytest.raises(CatalogError, match="registro local"):
            catalog.activate_imports()


def test_sync_uses_import_only_when_allowed_and_download_fails(tmp_path, pdf_bytes):
    pages = [["ISSN - 2711 - 1164", "INFORME DE POLÍTICA MONETARIA", "Julio de 2026"]]
    path = write(tmp_path, pages=pages, title="Informe de Política Monetaria, julio de 2026")
    calls = []
    catalog, client = make_catalog(tmp_path, Source([report(7)]), counting(pdf_bytes, calls, 302))
    with client:
        catalog.import_pdf(path)
        assert catalog.plan(1, use_imports=True)["window"][0]["status"] == "importado_si_falla"
        assert catalog.plan(1)["window"][0]["status"] == "nuevo"
        with pytest.raises(Exception, match="antibot"):
            catalog.sync(1)
        assert catalog.current() is None
        catalog.sync(1, use_imports=True)
    record = catalog.current()[1]["reports"][0]
    assert record["acquisition"]["method"] == "local_import"
    assert record["acquisition"]["metadata_verified"] is True  # metadatos del descubrimiento
    assert record["report"]["download_url"] == report(7).download_url
    assert catalog.current()[1]["selection"]["mode"] == "latest_n"


def test_crossref_find_reuses_yearly_listings():
    source, requests, pauses = make_source(from_fixtures)
    with source.client:
        found = source.find("ipm-2024-04")
        missing = source.find("ipm-2024-05")
        again = source.find("ipm-2025-01")
    assert found.doi == "10.32468/inf-pol-mont-spa.tr2-2024"
    assert missing is None and again.reference_period == "2025-01"
    assert len(requests) == 3  # 2024, 2025 y 2026, cada año una sola vez
    assert pauses == [0.5, 0.5]


def test_crossref_find_skips_future_years():
    source, requests, _ = make_source(from_fixtures)
    with source.client:
        assert source.find("ipm-2026-07").published_at == "2026-08-04"
    assert len(requests) == 1


def test_cli_import_offline_and_status_marks_unverified(tmp_path, capsys, monkeypatch):
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
    assert "importado, metadatos sin verificar" in out
    assert "PDF no verificado en la fuente" in out
    monkeypatch.setattr("sys.argv", ["banrep-rag", "estado", "--data-dir", str(data), "--json"])
    main()
    verification = json.loads(capsys.readouterr().out)["verification"][0]
    assert verification["status"] == "local_import"
    assert verification["last_successful_verification_at"] is None


def test_cli_import_reports_missing_file_in_spanish(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        [
            "banrep-rag",
            "importar",
            str(tmp_path / "x.pdf"),
            "--data-dir",
            str(tmp_path),
            "--sin-red",
        ],
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert "no existe" in capsys.readouterr().err


def test_cli_status_without_catalog_and_without_permissions(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["banrep-rag", "estado", "--data-dir", str(tmp_path)])
    main()
    assert "No hay catálogo activo" in capsys.readouterr().out


def test_cli_import_rejects_period_with_many_files(tmp_path, monkeypatch):
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


def test_reimporting_same_pdf_offline_keeps_verified_metadata(tmp_path):
    catalog, client = make_catalog(tmp_path, Source([]), lambda _: pytest.fail())
    verified = replace(report(1), report_id="ipm-2025-01", reference_period="2025-01")
    path = write(tmp_path)
    with client:
        first = catalog.import_pdf(path, lookup=lambda _: verified)
        again = catalog.import_pdf(path)
    assert again == first and again["metadata_verified"] is True


def test_repeated_cover_text_is_linear(tmp_path):
    from politica_monetaria_banrep_rag.importer import _cover_period

    hostile = "Informe de Política Monetaria Enero de 2025 " * 20_000
    started = time.monotonic()
    assert _cover_period(hostile) == "2025-01"
    assert time.monotonic() - started < 2


def test_errors_name_the_persons_file_not_the_temporary_copy(tmp_path):
    catalog, client = make_catalog(tmp_path, Source([]), lambda _: pytest.fail())
    pages = [["Presentación", "INFORME DE POLÍTICA MONETARIA", "Enero de 2025"]]
    path = write(tmp_path, "mi-presentacion.pdf", pages=pages)
    with client, pytest.raises(ImportFileError, match="«mi-presentacion.pdf»"):
        catalog.import_pdf(path)


def test_stale_temporary_copies_are_removed(tmp_path):
    catalog, client = make_catalog(tmp_path, Source([]), lambda _: pytest.fail())
    stale = catalog.root / "raw" / ".importacion-abandonada.pdf"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"%PDF-resto")
    with client:
        catalog.import_pdf(write(tmp_path))
    assert not stale.exists()
