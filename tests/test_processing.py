import json

import pytest
from pdf_factory import make_layout_pdf
from test_layout import annex_page, page_one, page_two, toc_page

from politica_monetaria_banrep_rag.chunking import chunk_document
from politica_monetaria_banrep_rag.cli import main
from politica_monetaria_banrep_rag.processing import ProcessedDocument, Unit, process_pdf
from politica_monetaria_banrep_rag.tables import Cell, Row, Table


@pytest.fixture
def report_pdf(tmp_path):
    path = tmp_path / "informe.pdf"
    path.write_bytes(make_layout_pdf([page_one(), page_two()]))
    return path


def test_processing_keeps_numbers_units_sections_and_pages(report_pdf):
    document = process_pdf(report_pdf, "ipm-2025-10", "0" * 64)
    paragraphs = [u for u in document.units if u.kind == "paragraph"]
    first = paragraphs[0]
    assert first.section == ("2. Pronósticos macroeconómicos", "2.1 Contexto externo")
    assert "2,1 % anual" in first.text and "-0,4 % del PIB" in first.text
    assert "incertidumbre" in first.text  # guion de corte unido
    # El párrafo continúa en la página siguiente: misma unidad, rango de páginas.
    assert first.text.endswith("comerciales entre las grandes economías.")
    assert (first.page, first.page_end, first.page_label, first.page_end_label) == (
        1,
        2,
        "13",
        "14",
    )
    second = paragraphs[1]
    assert second.section[-1] == "2.2 Precios"
    assert "inflación" in second.text and "gráfico" in second.text  # ligaduras reparadas
    chart = next(u for u in document.units if u.kind == "chart_caption")
    assert chart.text.startswith("Gráfico 2.1 PIB real de los socios comerciales")
    assert "Fuente: Banco de la República." in chart.text and "fuera de alcance" in chart.text
    assert not any("4,0" in u.text for u in document.units)  # ejes del gráfico excluidos
    table = next(u for u in document.units if u.kind == "table").table
    assert table.columns == ("2024 (pr)", "2025 (proy)")
    assert table.row_text(table.rows[1]) == "Perú — 2024 (pr): -0,4; 2025 (proy): 3,3"
    footnote = next(u for u in document.units if u.kind == "footnote")
    assert footnote.text.startswith("1 Las proyecciones")
    quality = document.quality
    assert quality.tables_reconstructed == 1 and quality.tables_failed == ()
    assert quality.ligatures_repaired >= 2
    assert quality.excluded_blocks["running_header"] == 1
    assert quality.excluded_blocks["chart_residue"] >= 5
    assert quality.degraded  # documento sintético muy corto: se declara degradado
    assert any("poco texto" in w for w in quality.warnings)


def test_scanned_like_pdf_is_degraded(tmp_path):
    path = tmp_path / "vacio.pdf"
    path.write_bytes(make_layout_pdf([[(300, 400, 10, "x", "R")], [], []]))
    quality = process_pdf(path, "ipm-2025-10", "0" * 64).quality
    assert quality.degraded and any("80 %" in w for w in quality.warnings)


def make_document(units):
    return ProcessedDocument("ipm-2025-10", "a" * 64, "prueba", tuple(units), None)  # type: ignore[arg-type]


def test_chunks_respect_sections_size_and_are_stable():
    sentence = "La inflación anual se ubicó en 5,1 % y bajaría a 3,6 % en 2026. "
    units = [
        Unit("paragraph", "Párrafo corto de contexto.", 3, "4", ("1. Resumen",)),
        Unit("paragraph", sentence * 40, 12, "13", ("2. Pronósticos",), page_end=13,
             page_end_label="14"),
        Unit("chart_caption", "Gráfico 2.1. Fuente: Banco.", 12, "13", ("2. Pronósticos",)),
        Unit("footnote", "1 Nota al pie.", 12, "13", ("2. Pronósticos",)),
    ]  # fmt: skip
    chunks = chunk_document(make_document(units), max_chars=500)
    assert chunks[0].section == ("1. Resumen",) and chunks[0].text == "Párrafo corto de contexto."
    long = [c for c in chunks if c.section == ("2. Pronósticos",) and c.kind == "paragraph"]
    assert len(long) > 4 and all(len(c.text) <= 500 for c in long)
    assert all(c.text.rstrip().endswith("2026.") for c in long)  # cortes en fin de oración
    assert all("5,1 %" in c.text for c in long)
    assert long[0].page_start == 12 and long[0].page_end == 13
    assert long[0].page_labels == ("13", "14")
    assert [c.kind for c in chunks[-2:]] == ["chart_caption", "footnote"]
    again = chunk_document(make_document(units), max_chars=500)
    assert [c.chunk_id for c in again] == [c.chunk_id for c in chunks]
    assert len({c.chunk_id for c in chunks}) == len(chunks)


def test_table_chunks_repeat_caption_and_columns():
    rows = tuple(
        Row((f"Variable {i}", "Porcentaje"), (Cell("1,0", False), Cell("2,0", True)))
        for i in range(40)
    )
    table = Table(
        "Anexo 2 Principales variables", 47, "", ("Concepto",), ("2025", "2026"), rows,
        ("Nota: los valores en negrilla corresponden a una proyección o supuesto.",), True,
    )  # fmt: skip
    unit = Unit("table", table.to_text(), 47, "49", ("Anexo 2",), table)
    chunks = chunk_document(make_document([unit]), max_chars=600)
    assert len(chunks) > 3
    for chunk in chunks:
        assert chunk.text.startswith("Anexo 2 Principales variables\nColumnas: Concepto | 2025")
        assert "negrilla" in chunk.text
        assert "2026: 2,0 (proyección o supuesto)" in chunk.text
    assert sum(chunk.text.count("Variable ") for chunk in chunks) == 40


def test_invalid_chunk_size():
    with pytest.raises(ValueError, match="max_chars"):
        chunk_document(make_document([]), max_chars=100)


def test_cli_procesar_uses_active_catalog_and_cache(tmp_path, report_pdf, capsys, monkeypatch):
    from test_importer import write

    data = tmp_path / "datos"
    monkeypatch.setattr(
        "sys.argv",
        ["banrep-rag", "importar", str(write(tmp_path)), "--data-dir", str(data), "--sin-red",
         "--activar"],
    )  # fmt: skip
    main()
    capsys.readouterr()
    monkeypatch.setattr("sys.argv", ["banrep-rag", "procesar", "--data-dir", str(data), "--json"])
    main()
    first = json.loads(capsys.readouterr().out)
    assert first[0]["report_id"] == "ipm-2025-01" and first[0]["cached"] is False
    assert first[0]["degraded"] is True  # PDF sintético mínimo
    main()
    assert json.loads(capsys.readouterr().out)[0]["cached"] is True
    stored = json.loads(next((data / "processed").glob("*.json")).read_text())
    assert stored["report"]["report_id"] == "ipm-2025-01"
    assert stored["versions"]["chunker"] and stored["chunks"]


def test_cli_procesar_without_catalog(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["banrep-rag", "procesar", "--data-dir", str(tmp_path)])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert "no hay catálogo activo" in capsys.readouterr().err


def test_dense_annex_table_and_table_of_contents(tmp_path):
    path = tmp_path / "anexo.pdf"
    path.write_bytes(make_layout_pdf([toc_page(), annex_page()]))
    document = process_pdf(path, "ipm-2025-10", "0" * 64)
    assert document.quality.excluded_blocks.get("toc", 0) >= 1
    assert not any(u.page == 1 for u in document.units)  # el índice no se indexa
    table = next(u.table for u in document.units if u.kind == "table")
    assert table.caption.startswith("Anexo 2 Principales variables")
    assert table.columns == ("2023", "2024", "2025", "2026")
    assert table.bold_is_projection
    ipc = next(row for row in table.rows if row.labels[0] == "IPC total")
    assert table.row_text(ipc) == (
        "Internas > IPC total — 2023: 9,3; 2024: 5,2; 2025: 5,1 (proyección o supuesto); "
        "2026: 3,6 (proyección o supuesto)"
    )
    fx = next(row for row in table.rows if row.labels[0] == "Tasa de cambio")
    assert "2023: 4.325" in table.row_text(fx)


def test_annex_page_without_title_is_kept_as_continuation(tmp_path):
    path = tmp_path / "anexo-continuacion.pdf"
    path.write_bytes(make_layout_pdf([annex_page(), annex_page()[1:]]))
    document = process_pdf(path, "ipm-2025-10", "0" * 64)
    tables = [u for u in document.units if u.kind in {"table", "table_raw"}]
    assert [u.page for u in tables] == [1, 2]
    assert tables[1].text.startswith("Anexo 2 Principales variables del pronóstico (continuación)")
    assert tables[1].section == ("Anexo 2 Principales variables del pronóstico",)
    assert "5,1 (proyección o supuesto)" in tables[1].text


def test_isolated_dense_table_without_any_title_is_not_lost(tmp_path):
    path = tmp_path / "sin-titulo.pdf"
    path.write_bytes(make_layout_pdf([annex_page()[1:]]))
    document = process_pdf(path, "ipm-2025-10", "0" * 64)
    (table,) = [u for u in document.units if u.kind in {"table", "table_raw"}]
    assert table.text.startswith("Cuadro sin título (página 1)")


def test_box_subsections_hang_from_the_box(tmp_path):
    path = tmp_path / "recuadro.pdf"
    items = [
        (57, 760, 20, "3. Situación económica actual", "B"),
        (291, 720, 9, "Recuadro 1 Un indicador de demanda", "R"),
        (291, 700, 10, "1. Introducción", "B"),
        (291, 680, 10, "El indicador resume la demanda interna de corto plazo.", "R"),
        (291, 650, 10, "2. Metodología", "B"),
        (291, 630, 10, "Se estima con un modelo de factores dinámicos.", "R"),
        (57, 300, 20, "4. Decisiones de política", "B"),
        (291, 260, 10, "La Junta redujo la tasa de interés.", "R"),
    ]
    path.write_bytes(make_layout_pdf([items]))
    sections = {u.text[:15]: u.section for u in process_pdf(path, "ipm-2025-10", "0" * 64).units}
    assert sections["El indicador re"] == ("Recuadro 1 Un indicador de demanda", "1. Introducción")
    assert sections["Se estima con u"] == ("Recuadro 1 Un indicador de demanda", "2. Metodología")
    assert sections["La Junta redujo"] == ("4. Decisiones de política",)


def test_long_table_notes_get_their_own_chunk():
    notes = ("d/ Los valores positivos representan déficit y los negativos superávit. " * 12,)
    rows = (Row(("Déficit fiscal",), (Cell("5,1", False),)),)
    table = Table("Anexo 1 Proyecciones", 46, "", ("Concepto",), ("2025",), rows, notes, False)
    unit = Unit("table", table.to_text(), 46, "47", ("Anexo 1",), table)
    chunks = chunk_document(make_document([unit]), max_chars=600)
    assert "notas completas aparte" in chunks[0].text
    notes_chunks = [c for c in chunks if c.kind == "table_notes"]
    assert notes_chunks and notes_chunks[0].text.startswith("Anexo 1 Proyecciones — notas:")
    joined = " ".join(c.text for c in notes_chunks)
    assert joined.count("superávit") == 12


def test_sentences_are_not_cut_after_abbreviations():
    from politica_monetaria_banrep_rag.chunking import _split_long

    text = "Hay choques, p. ej. La sequía. " + "Otra oración larga sobre la inflación. " * 30
    pieces = _split_long(text, 200)
    assert pieces[0].startswith("Hay choques, p. ej. La sequía.")


def test_store_keeps_good_reports_when_one_fails_and_refreshes_metadata(tmp_path):
    from test_importer import write

    from politica_monetaria_banrep_rag.catalog import Catalog
    from politica_monetaria_banrep_rag.processed_store import ProcessedStore

    data = tmp_path / "datos"
    monkey_catalog = Catalog(data, None, None)  # type: ignore[arg-type]
    monkey_catalog.import_pdf(write(tmp_path))
    monkey_catalog.activate_imports()
    records = monkey_catalog.current()[1]["reports"]
    broken = json.loads(json.dumps(records[0]))
    broken["report"]["report_id"] = "ipm-2025-04"
    broken["pdf"]["sha256"] = "f" * 64  # el archivo no coincide con este hash
    store = ProcessedStore(data)
    summaries, failures = store.ensure(records + [broken])
    assert [s.report_id for s in summaries] == ["ipm-2025-01"]
    assert failures == [("ipm-2025-04", "el PDF local no coincide con su hash")]
    renamed = json.loads(json.dumps(records))
    renamed[0]["report"]["title"] = "Título corregido"
    summaries, failures = store.ensure(renamed)
    assert summaries[0].cached and not failures
    stored = json.loads(summaries[0].path.read_text())
    assert stored["report"]["title"] == "Título corregido"
    summaries[0].path.write_text("{truncado")
    summaries, failures = store.ensure(records)
    assert not summaries[0].cached and not failures


def test_box_with_dotted_subsections_and_main_text_after_it(tmp_path):
    path = tmp_path / "recuadro2.pdf"
    items = [
        (57, 780, 20, "3. Situación económica actual", "B"),
        (291, 750, 9, "Recuadro 1 Un indicador de demanda", "R"),
        (291, 730, 10, "2. Metodología", "B"),
        (291, 710, 10, "2.1 Desbalance del crédito", "B"),
        (291, 690, 10, "El desbalance se mide frente a su tendencia de largo plazo.", "R"),
        (291, 660, 10, "3. Construcción del indicador agregado", "B"),
        (291, 640, 10, "Se agregan los componentes con pesos estimados por factores.", "R"),
        (291, 610, 10, "3.5 Mercado laboral", "B"),
        (291, 590, 10, "La tasa de desempleo se mantuvo estable en el trimestre.", "R"),
    ]
    path.write_bytes(make_layout_pdf([items]))
    sections = {u.text[:12]: u.section for u in process_pdf(path, "ipm-2025-10", "0" * 64).units}
    box = "Recuadro 1 Un indicador de demanda"
    assert sections["El desbalanc"] == (box, "2. Metodología", "2.1 Desbalance del crédito")
    assert sections["Se agregan l"] == (box, "3. Construcción del indicador agregado")
    assert sections["La tasa de d"] == ("3. Situación económica actual", "3.5 Mercado laboral")
