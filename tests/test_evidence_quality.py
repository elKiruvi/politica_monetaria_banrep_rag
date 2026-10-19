"""Evidence labels are conservative even if document-level extraction appears healthy."""

import hashlib
import os
from dataclasses import asdict
from pathlib import Path

import pytest

from politica_monetaria_banrep_rag.chunking import chunk_document
from politica_monetaria_banrep_rag.processing import (
    ProcessedDocument,
    Unit,
    critical_section_coverage,
    process_pdf,
)
from politica_monetaria_banrep_rag.tables import Cell, Row, Table


def _document(*units):
    return ProcessedDocument("ipm-2025-04", "a" * 64, "fixture", units, None)  # type: ignore[arg-type]


def test_quality_labels_do_not_promote_raw_or_chart_numbers():
    section = ("2. Forecasts",)
    table = Table(
        "Table 2.1 (porcentaje)", 14, "", ("Country",), ("2025 (proy)",),
        (Row(("Country A",), (Cell("1.2", False),)),), (), False,
    )  # fmt: skip
    units = (
        Unit("paragraph", "Growth is forecast at 1.2 percent.", 14, "12", section),
        Unit("table", table.to_text(), 14, "12", section, table),
        Unit("table_raw", "2025 | Q1 | 1.2", 53, "51", section),
        Unit("chart_caption", "Chart 2.1: Growth 1.2; image not extracted.", 15, "13", section),
        Unit("footnote", "Note: 1.2 is an estimate.", 14, "12", section),
    )
    assert table.evidence_quality == "structured_cells"
    assert [unit.evidence_quality for unit in units] == [
        "narrative_only",
        "structured_cells",
        "unattributed_values",
        "caption_only",
        "note_only",
    ]
    assert asdict(units[1])["table"]["evidence_quality"] == "structured_cells"
    chunks = chunk_document(_document(*units))
    assert [chunk.evidence_quality for chunk in chunks] == [
        "narrative_only",
        "structured_cells",
        "unattributed_values",
        "caption_only",
        "note_only",
    ]
    assert all(chunk.to_dict()["evidence_quality"] == chunk.evidence_quality for chunk in chunks)
    assert chunks[1].attributed_cells[0].row == ("Country A",)
    assert chunks[1].attributed_cells[0].column == "2025 (proy)"
    assert chunks[1].attributed_cells[0].horizon == "2025 (proy)"
    assert chunks[1].attributed_cells[0].unit == "porcentaje"
    assert all(not chunk.attributed_cells for chunk in chunks if chunk.kind != "table")


def test_incomplete_structure_never_claims_attributed_values():
    table = Table(
        "Table 1", 1, "", ("Concept",), ("2025",),
        (Row(("",), (Cell("1.2", False),)),), (), False,
    )  # fmt: skip
    assert table.evidence_quality == "unattributed_values"
    assert Unit("table", table.to_text(), 1, None, (), table).evidence_quality == (
        "unattributed_values"
    )
    assert table.attributed_cells((table.rows[0],)) == ()


def test_missing_values_are_not_numeric_evidence():
    table = Table(
        "Rates", 1, "", ("Concept",), ("2024", "2025", "2026"),
        (Row(("Value",), (Cell("n. d.", False), Cell("1,2", False), Cell("n. a.", False))),),
        (), False,
    )  # fmt: skip
    (chunk,) = chunk_document(_document(Unit("table", table.to_text(), 1, None, (), table)))
    assert [(cell.column, cell.value) for cell in chunk.attributed_cells] == [("2025", "1,2")]


def test_horizon_and_unit_are_not_inferred_from_an_unlabeled_table():
    table = Table(
        "Index", 1, "", ("Item",), ("Scenario A",),
        (Row(("One",), (Cell("1,2", False),)),), (), False,
    )  # fmt: skip
    unit = Unit("table", table.to_text(), 1, None, (), table)
    (chunk,) = chunk_document(_document(unit))
    (cell,) = chunk.attributed_cells
    assert (cell.column, cell.value, cell.horizon, cell.unit) == ("Scenario A", "1,2", None, None)


def test_comparative_column_is_not_a_single_horizon():
    table = Table(
        "Growth", 1, "", ("Country",), ("2025 vs. 2024",),
        (Row(("A",), (Cell("1,2", False),)),), (), False,
    )  # fmt: skip
    (chunk,) = chunk_document(_document(Unit("table", table.to_text(), 1, None, (), table)))
    assert chunk.attributed_cells[0].horizon is None


def test_quarter_and_month_headers_keep_their_exact_horizons():
    table = Table(
        "Series", 1, "", ("Concept",), ("I-2025", "dic 24"),
        (Row(("PIB",), (Cell("2,4", False), Cell("2,1", False))),), (), False,
    )  # fmt: skip
    (chunk,) = chunk_document(_document(Unit("table", table.to_text(), 1, None, (), table)))
    assert [cell.horizon for cell in chunk.attributed_cells] == ["I-2025", "dic 24"]


def test_row_unit_overrides_a_mixed_table_title():
    table = Table(
        "Inflation (porcentaje)", 1, "", ("Measure",), ("2025",),
        (Row(("Monthly change (puntos porcentuales)",), (Cell("1,2", False),)),),
        (), False,
    )  # fmt: skip
    (chunk,) = chunk_document(_document(Unit("table", table.to_text(), 1, None, (), table)))
    assert chunk.attributed_cells[0].unit == "puntos porcentuales"


def test_row_unit_precedes_group_unit():
    table = Table(
        "Inflation", 1, "", ("Measure",), ("2025",),
        (Row(("Change (puntos porcentuales)",), (Cell("1,2", False),),
             ("Series (porcentaje)",)),), (), False,
    )  # fmt: skip
    (chunk,) = chunk_document(_document(Unit("table", table.to_text(), 1, None, (), table)))
    assert chunk.attributed_cells[0].unit == "puntos porcentuales"


@pytest.mark.parametrize(
    "row_label,expected",
    [
        ("Pesos por dólar, end of period", "pesos por dólar"),
        ("Dólares por barril, average", "dólares por barril"),
        ("Millones de dólares", "millones de dólares"),
        ("Millions: millones de dólares, average", "millones de dólares"),
        ("Porcentaje del PIB", "porcentaje del pib"),
        ("Millones de dólares por person", None),
        ("Miles de pesos por dólar", None),
        ("Millones de dólares por barril", None),
        ("Pesos por person, average", None),
    ],
)
def test_compound_unit_is_never_truncated(row_label, expected):
    table = Table(
        "Forecast (porcentaje)", 1, "", ("Measure",), ("2025",),
        (Row((row_label,), (Cell("1,2", False),)),), (), False,
    )  # fmt: skip
    (chunk,) = chunk_document(_document(Unit("table", table.to_text(), 1, None, (), table)))
    assert chunk.attributed_cells[0].unit == expected


def test_chunk_without_attributable_values_is_not_structured():
    rows = (
        Row(("Unknown" * 28,), (Cell("n. d.", False),)),
        Row(("Known" * 28,), (Cell("1,2", False),)),
    )
    table = Table("Data", 1, "", ("Item",), ("2025",), rows, (), False)
    chunks = chunk_document(_document(Unit("table", table.to_text(), 1, None, (), table)),
                            max_chars=300)  # fmt: skip
    assert table.evidence_quality == "structured_cells" and len(chunks) == 2
    assert chunks[0].evidence_quality == "unattributed_values" and not chunks[0].attributed_cells
    assert chunks[1].evidence_quality == "structured_cells"
    assert chunks[1].attributed_cells[0].value == "1,2"


def test_repeated_row_text_in_notes_does_not_create_extra_attribution():
    rows = (
        Row(("A" * 105,), (Cell("1,2", False),)),
        Row(("B" * 105,), (Cell("2,3", False),)),
    )
    first = Table("Table", 1, "", ("Concept",), ("2025",), rows, (), False)
    table = Table("Table", 1, "", ("Concept",), ("2025",), rows,
                  (first.row_text(rows[0]),), False)  # fmt: skip
    chunks = chunk_document(_document(Unit("table", table.to_text(), 1, None, (), table)),
                            max_chars=300)  # fmt: skip
    assert len(chunks) == 2
    assert first.row_text(rows[0]) in chunks[1].text  # The note repeats the first row.
    assert [(cell.row[0], cell.value) for cell in chunks[1].attributed_cells] == [
        ("B" * 105, "2,3")
    ]


def test_each_table_part_attributes_only_its_own_cells():
    table = Table(
        "Interest rates (promedio mensual, porcentaje)", 2, "", ("Rate",),
        ("dic-24", "mar-25"),
        tuple(Row((f"Rate {index}",), (Cell(f"{index},1", False),
                                         Cell(f"{index},2", True))) for index in range(30)),
        ("Bold type indicates projections.",), True,
    )  # fmt: skip
    unit = Unit("table", table.to_text(), 2, "1", ("2. Forecasts",), table)
    chunks = chunk_document(_document(unit), max_chars=350)
    assert len(chunks) > 1
    seen = []
    for chunk in chunks:
        for cell in chunk.attributed_cells:
            assert any(line.startswith(f"{cell.row[-1]} — ") for line in chunk.text.splitlines())
            assert cell.horizon == cell.column and cell.unit == "porcentaje"
            if cell.column == "mar-25":
                assert cell.projection_or_assumption is True
            seen.append((cell.row[-1], cell.column, cell.value))
    assert len(seen) == 60 and len(set(seen)) == 60


def test_critical_coverage_does_not_equate_table_or_chart_to_prose():
    units = (
        Unit("paragraph", "Summary text", 1, None, ("1. Summary",)),
        Unit("table_raw", "orphan values", 2, None, ("2. Forecasts",)),
        Unit("chart_caption", "Chart 1", 3, None, ("3. Current situation",)),
        Unit("paragraph", "Box", 4, None, ("Box 2",)),
    )
    coverage = critical_section_coverage(units)
    assert coverage["1"]["covered"] is True
    assert coverage["2"]["covered"] is False and coverage["2"]["raw_tables"] == 1
    assert coverage["3"]["covered"] is False and coverage["3"]["charts"] == 1


def test_unattributed_table_is_not_counted_as_structured():
    table = Table(
        "No values", 1, "", ("Concept",), ("2025",),
        (Row(("Inflation",), (Cell("n. d.", False),)),), (), False,
    )  # fmt: skip
    unit = Unit("table", table.to_text(), 1, None, ("2. Forecasts",), table)
    coverage = critical_section_coverage((unit,))
    assert coverage["2"]["structured_tables"] == 0
    assert coverage["2"]["unattributed_tables"] == 1
    assert coverage["2"]["covered"] is False


def test_orphan_source_note_does_not_cover_a_critical_section(monkeypatch, tmp_path):
    from politica_monetaria_banrep_rag import processing
    from politica_monetaria_banrep_rag.layout import (
        Block,
        DocumentLayout,
        LigatureRepair,
        PageLayout,
    )

    def block(kind, text, size=10):
        return Block(1, kind, text, (0, 0, 100, 10), size, False)

    page = PageLayout(1, "1", 600, 800, (
        block("heading", "2. Forecasts", 20), block("source_note", "Source: Report"),
        block("caption", "Chart 1"),
    ))  # fmt: skip
    monkeypatch.setattr(
        processing, "extract_layout", lambda _: DocumentLayout((page,), LigatureRepair())
    )
    document = process_pdf(tmp_path / "unused.pdf", "ipm-2025-04", "a" * 64)
    assert [unit.kind for unit in document.units] == ["source_note", "caption"]
    assert document.quality.critical_sections["2"]["covered"] is False
    assert [chunk.evidence_quality for chunk in chunk_document(document)] == [
        "note_only",
        "caption_only",
    ]


def test_actual_pdf_pages_confirm_structured_and_unattributed_cases():
    """Optional local golden: use only the user's imported PDF, never ship its bytes."""
    path, sha = _golden_pdf(
        "BANREP_GOLDEN_PDF", "7d8535c2c7cbcc8a3820a593281a6243960e5e3974c6ed7ec5c165ddf42d766f"
    )
    document = process_pdf(path, "ipm-2025-04", sha)
    table = next(
        u for u in document.units if u.page == 14 and u.kind == "table"
        and u.table and u.table.caption.startswith("Cuadro 2.1")
    )  # fmt: skip
    assert table.evidence_quality == "structured_cells"
    assert table.table is not None
    row = next(row for row in table.table.rows if row.labels[0] == "Estados Unidos")
    assert table.table.columns[:4] == ("2023 (pr)", "2024 (pr)", "2025 (proy)", "2026 (proy)")
    assert [cell.text if cell else None for cell in row.values[:4]] == ["2,9", "2,8", "1,2", "1,1"]
    raw = next(u for u in document.units if u.page == 53 and u.kind == "table_raw")
    assert raw.evidence_quality == "unattributed_values" and raw.table is None
    chunks = chunk_document(document)
    assert all(c.evidence_quality == "unattributed_values" and not c.attributed_cells
               for c in chunks if c.kind == "table_raw" and c.page_start == 53)  # fmt: skip
    table_chunks = [c for c in chunks if c.kind == "table" and c.page_start == 14
                    and c.text.startswith("Cuadro 2.1")]  # fmt: skip
    assert any(
        cell.value == "1,2" and cell.horizon == "2025 (proy)" and cell.unit is None
        for c in table_chunks
        for cell in c.attributed_cells
        if cell.row == ("Estados Unidos",)
    )
    assert any(
        cell.value == "4.326" and cell.unit == "pesos por dólar" and cell.horizon == "abr-25"
        for chunk in chunks
        if chunk.kind == "table" and chunk.page_start == 49
        for cell in chunk.attributed_cells
        if cell.row[0] == "Tasa de cambio nominal"
    )
    assert any(
        cell.value == "45" and cell.unit == "dólares por barril" and cell.horizon == "2016"
        for chunk in chunks
        if chunk.kind == "table" and chunk.page_start == 51
        for cell in chunk.attributed_cells
        if cell.row[-2] == "Precio del petróleo (referencia Brent)"
    )
    assert any(
        cell.value == "-12.587" and cell.unit == "millones de dólares" and cell.horizon == "2016"
        for chunk in chunks
        if chunk.kind == "table" and chunk.page_start == 51
        for cell in chunk.attributed_cells
        if cell.row[-2] == "Cuenta corriente (A + B + C)"
    )
    assert document.quality.critical_sections["2"]["covered"] is True


def _golden_pdf(variable, expected_sha):
    configured = os.environ.get(variable)
    if configured is None:
        pytest.skip(f"{variable} local import not configured")
    path = Path(configured)
    assert path.is_file(), f"{variable} must point to an existing PDF"
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    assert sha == expected_sha, f"{variable} does not match the reviewed PDF"
    return path, sha


def test_wide_annex_only_attributes_cells_visible_on_its_page():
    """Optional local golden for July 2025: the annex spans pages 50 and 51."""
    path, sha = _golden_pdf(
        "BANREP_GOLDEN_WIDE_PDF",
        "97b9449eceac8c617ceaf003688004da670f867196e62efda8a1e232f593f677",
    )
    document = process_pdf(path, "ipm-2025-07", sha)
    first = next(u for u in document.units if u.kind == "table" and u.page == 50
                 and u.table and u.table.caption.startswith("Anexo 2"))  # fmt: skip
    assert first.table is not None
    assert first.table.columns == (
        "2019 T3", "2019 T4", "2020 T1", "2020 T2",
        "2020 T3", "2020 T4", "2021 T1", "2021 T2",
    )  # fmt: skip
    second = next(u for u in document.units if u.kind == "table_raw" and u.page == 51)
    assert second.evidence_quality == "unattributed_values"
    chunks = chunk_document(document)
    assert sum(len(c.attributed_cells) for c in chunks if c.page_start == 50) == 496
    assert all(not c.attributed_cells for c in chunks if c.page_start == 51)


@pytest.mark.parametrize("name,contents", [("missing.pdf", None), ("wrong.pdf", b"not the PDF")])
def test_configured_golden_fails_instead_of_skipping(monkeypatch, tmp_path, name, contents):
    path = tmp_path / name
    if contents is not None:
        path.write_bytes(contents)
    monkeypatch.setenv("BANREP_GOLDEN_PDF", str(path))
    with pytest.raises(AssertionError, match="BANREP_GOLDEN_PDF"):
        test_actual_pdf_pages_confirm_structured_and_unattributed_cases()
