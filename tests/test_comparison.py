"""Comparaciones deterministas entre informes con celdas atribuidas sintéticas."""

import hashlib
import json
from decimal import Decimal

import pytest
import test_index_store
from test_index_store import run_cli
from test_lexical_index import report

from politica_monetaria_banrep_rag.comparison import (
    compare,
    detect_periods,
    indicator_terms,
    parse_value,
)
from politica_monetaria_banrep_rag.retrieval import IndexedChunk
from politica_monetaria_banrep_rag.tables import AttributedCell

catalog_root = test_index_store.catalog_root  # fixture compartida
REPORTS = {r.report_id: r for r in (report("2025-01"), report("2025-07"), report("2026-01"))}
IPC = ("Internas", "Precios", "IPC total c/", "Porcentaje, var. anual, fin del periodo")


def table(period, caption, cells, *, page=49, kind="table", quality="structured_cells"):
    key = f"{period}{caption}{cells}{kind}"
    return IndexedChunk(
        chunk_id=hashlib.sha256(key.encode()).hexdigest()[:32],
        report_id=f"ipm-{period}",
        kind=kind,
        text=f"{caption}\nColumnas: Concepto | 2025 | 2026\n…",
        section=(caption,),
        page_start=page,
        page_end=page,
        page_labels=(str(page),),
        evidence_quality=quality,
        attributed_cells=tuple(cells) if kind == "table" else (),
        table_id=hashlib.sha256(key.encode()).hexdigest()[:32] if kind == "table" else None,
    )


def cell(value, column="2025", row=IPC, unit="porcentaje", projection=True):
    return AttributedCell(row, column, value, column, unit, projection)


ANNEX = "Anexo 2 Principales variables del pronóstico macroeconómico (Años)"
CHUNKS = [
    table("2025-01", ANNEX, [cell("4,05"), cell("3,05", "2026")], page=57),
    table("2025-07", ANNEX, [cell("4,7"), cell("3,2", "2026")]),
    # Enero de 2026: 2025 ya es dato observado; la llamada de nota cambia («d/» pegada).
    table(
        "2026-01",
        ANNEX,
        [
            cell(
                "5,1",
                row=(
                    "Internas",
                    "Precios",
                    "IPC totald/",
                    "Porcentaje, var. anual, fin del periodo",
                ),
                projection=False,
            ),
            cell("6,3", "2026"),
        ],
    ),  # fmt: skip
    # Otro cuadro con una fila parecida (analistas): nunca se mezcla con el Anexo 2.
    table(
        "2025-07",
        "Anexo 1 Proyecciones de analistas",
        [cell("4,79", "dic-25", row=("IPC total", "Variación anual"), unit=None, projection=False)],
    ),  # fmt: skip
    table(
        "2025-01",
        "Anexo 1 Proyecciones de analistas",
        [cell("4,21", "dic-25", row=("IPC total", "Variación anual"), unit=None, projection=False)],
    ),  # fmt: skip
]


def test_parse_value_handles_spanish_numbers_and_markers():
    assert parse_value("4,7") == Decimal("4.7")
    assert parse_value("-0,4") == Decimal("-0.4")
    assert parse_value("−13.451") == Decimal("-13451")
    assert parse_value("4,80 c/") == Decimal("4.80")
    for text in ("n. d.", "n. a.", "", "4,7 %", "abc"):
        assert parse_value(text) is None


def test_projection_revision_between_two_reports():
    result = compare(CHUNKS, REPORTS, ("ipm-2025-07", "ipm-2025-01"), "IPC total", horizon="2025")
    assert [r.reference_period for r in result.reports] == ["2025-01", "2025-07"]  # cronológico
    (row,) = [r for r in result.rows if r.table.startswith("Anexo 2")]
    assert row.table == "Anexo 2 Principales variables del pronóstico macroeconómico"
    assert row.comparable and row.unit == "porcentaje"
    assert [v.value_text for v in row.values] == ["4,05", "4,7"]
    (difference,) = row.differences
    assert difference.value == Decimal("0.65") and difference.kind == "revision_de_proyeccion"
    assert row.difference_unit == "puntos porcentuales"
    assert any("redondeo" in note for note in row.notes)
    assert row.values[0].page_start == 57 and row.values[0].source_url.startswith("https://")


def test_projection_against_observed_value_is_labelled():
    result = compare(CHUNKS, REPORTS, tuple(REPORTS), "IPC total", horizon="2025", table="Anexo 2")
    (row,) = result.rows
    assert [d.kind for d in row.differences] == [
        "revision_de_proyeccion",
        "proyeccion_vs_observado",
    ]
    assert row.differences[1].value == Decimal("0.4")
    assert any("observados" in note for note in row.notes)


def test_tables_with_similar_rows_are_never_mixed():
    result = compare(CHUNKS, REPORTS, ("ipm-2025-01", "ipm-2025-07"), "IPC total",
                     only_projections=False)  # fmt: skip
    tables = {row.table for row in result.rows}
    assert tables == {"Anexo 1 Proyecciones de analistas",
                      "Anexo 2 Principales variables del pronóstico macroeconómico"}  # fmt: skip
    analysts = next(r for r in result.rows if r.table.startswith("Anexo 1"))
    assert analysts.horizon == "dic-25" and analysts.difference_unit.startswith("unidad no")
    # Solo celdas sin proyección: se omiten por defecto.
    default = compare(CHUNKS, REPORTS, ("ipm-2025-01", "ipm-2025-07"), "IPC total")
    assert all(r.table.startswith("Anexo 2") for r in default.rows)


def test_different_horizons_are_never_compared():
    result = compare(CHUNKS, REPORTS, ("ipm-2025-01", "ipm-2025-07"), "IPC total", table="Anexo 2")
    assert {r.horizon for r in result.rows} == {"2025", "2026"}
    for row in result.rows:
        assert {v.column for v in row.values if v} == {row.horizon}


def test_unit_mismatch_is_not_compared():
    chunks = [
        table("2025-01", ANNEX, [cell("4,0", unit="porcentaje")]),
        table("2025-07", ANNEX, [cell("4,5", unit="puntos básicos")]),
    ]
    (row,) = compare(chunks, REPORTS, ("ipm-2025-01", "ipm-2025-07"), "IPC total").rows
    assert not row.comparable and row.differences == ()
    assert "Unidades distintas" in row.notes[0]


def test_unattributed_tables_and_missing_counterparts_contribute_nothing():
    chunks = [
        table("2025-01", ANNEX, [cell("4,0")]),
        table("2025-07", ANNEX, [], kind="table_raw", quality="unattributed_values"),
        table("2026-01", ANNEX, [cell("9,9", "2030")]),
    ]
    result = compare(chunks, REPORTS, tuple(REPORTS), "IPC total")
    assert result.rows == ()
    assert "No hay celdas atribuidas comparables" in result.warnings[0]


def test_interpretation_picks_the_most_specific_matching_terms():
    result = compare(CHUNKS, REPORTS, ("ipm-2025-01", "ipm-2025-07"),
                     "¿Cómo cambiaron las proyecciones de inflación?")  # fmt: skip
    assert result.interpretation == ("ipc total",)
    assert indicator_terms("crecimiento del PIB") == [
        ("producto interno bruto", "crecimiento"),
        ("producto interno bruto", "pib"),
    ]


def test_input_validation():
    with pytest.raises(ValueError, match="dos informes"):
        compare(CHUNKS, REPORTS, ("ipm-2025-01",), "IPC")
    with pytest.raises(ValueError, match="no indexados"):
        compare(CHUNKS, REPORTS, ("ipm-2025-01", "ipm-2019-01"), "IPC")
    with pytest.raises(ValueError, match="indicador"):
        compare(CHUNKS, REPORTS, ("ipm-2025-01", "ipm-2025-07"), "¿cómo cambió entre?")


@pytest.mark.parametrize(
    "question,found,missing",
    [
        ("entre enero y julio de 2025", ("ipm-2025-01", "ipm-2025-07"), ()),
        ("de octubre de 2024 a enero de 2026", ("ipm-2026-01",), ("2024-10",)),
        (
            "enero de 2025, julio de 2025 y enero de 2026",
            ("ipm-2025-01", "ipm-2025-07", "ipm-2026-01"),
            (),
        ),  # fmt: skip
        ("inflación de diciembre de 2025", (), ()),
        ("julio", (), ()),
    ],
)
def test_detect_periods(question, found, missing):
    available = {r.reference_period: r.report_id for r in REPORTS.values()}
    assert detect_periods(question, available) == (found, missing)


def test_cli_comparar_requires_two_reports_and_reports_json(catalog_root, monkeypatch, capsys):
    data = str(catalog_root)
    run_cli(monkeypatch, capsys, "indexar", "--sin-embeddings", "--data-dir", data)
    code, _, err = run_cli(monkeypatch, capsys, "comparar", "inflación entre octubre de 2024 y "
                           "enero de 2025", "--data-dir", data)  # fmt: skip
    assert code == 2 and "No están indexados: 2024-10" in err
    code, _, err = run_cli(monkeypatch, capsys, "comparar", "--informe", "2025-01", "--informe",
                           "ipm-2019-01", "--indicador", "IPC", "--data-dir", data)  # fmt: skip
    assert code == 2 and "no indexados" in err


def test_cli_comparar_prints_values_pages_and_differences(monkeypatch, capsys, tmp_path):
    from politica_monetaria_banrep_rag import cli

    class Lexical:
        reports = REPORTS

        def all_chunks(self):
            return CHUNKS

    class Active:
        index_id = "f" * 64
        lexical = Lexical()

        def table_notes(self, chunk):
            return []

        def close(self):
            pass

    class Manager:
        def __init__(self, root):
            pass

        def open_active(self):
            return Active()

    monkeypatch.setattr(cli, "IndexManager", Manager)
    monkeypatch.setattr(cli, "_index_status", lambda root, active: {"warnings": []})
    code, out, _ = run_cli(monkeypatch, capsys, "comparar", "¿Cómo cambiaron las proyecciones "
                           "de inflación entre enero y julio de 2025?", "--horizonte", "2025",
                           "--data-dir", str(tmp_path))  # fmt: skip
    assert code == 0
    assert "2025-01: 4,05 (proyección o supuesto) — pág. 57 del PDF" in out
    assert "+0,65 puntos porcentuales (revisión de proyección)" in out
    assert "calculadas en Python" in out and "Proyecto no oficial" in out
    code, out, _ = run_cli(monkeypatch, capsys, "comparar", "--informe", "2025-01", "--informe",
                           "2026-01", "--indicador", "IPC total", "--json", "--data-dir",
                           str(tmp_path))  # fmt: skip
    payload = json.loads(out)
    diffs = [d for row in payload["rows"] for d in row["differences"]]
    assert {"from_report": "ipm-2025-01", "to_report": "ipm-2026-01", "value": "3.25",
            "kind": "revision_de_proyeccion"} in diffs  # fmt: skip
