"""Casos económicos adversariales; fallos esperados documentan riesgos reproducibles.

Las cuatro referencias se contrastaron con los PDF originales, no con el recuperador
ni con un modelo de lenguaje. Si no están disponibles, se omite su cotejo directo.
"""

import hashlib
from decimal import Decimal
from pathlib import Path

import pytest
from pdfminer.high_level import extract_pages
from pdfminer.layout import LTChar, LTContainer, LTTextLine
from test_comparison import ANNEX, REPORTS, cell, table
from test_lexical_index import report

from politica_monetaria_banrep_rag.answering import verify
from politica_monetaria_banrep_rag.comparison import compare

PAIR = ("ipm-2025-01", "ipm-2025-07")
REAL_PDF = Path(__file__).resolve().parents[1] / "data/catalogo/raw"
# El SHA-256 figura en el nombre local; los PDF no se incorporan al repositorio.
REFERENCES = (
    (
        "2025-01",
        57,
        59,
        "2025",
        "4,05",
        "447672cf45048875bd4815ea404da6e2ea1131bc35b38e6bdc447fe2dc75ccb9",
    ),
    (
        "2025-07",
        49,
        49,
        "2025",
        "4,7",
        "97b9449eceac8c617ceaf003688004da670f867196e62efda8a1e232f593f677",
    ),
    (
        "2025-10",
        47,
        49,
        "2026",
        "3,6",
        "091c25fbce8d3e1f1aab2d37df4ed4851eefa852e0eaf0b1500002d277c9b373",
    ),
    (
        "2026-01",
        49,
        51,
        "2026",
        "6,3",
        "c4807df77b598fd28e3102ce7dd7f3f897808bd7044d1bdb53cb1652059854da",
    ),
)


def _lines(node):
    if isinstance(node, LTTextLine):
        yield node
    if isinstance(node, LTContainer):
        for child in node:
            yield from _lines(child)


@pytest.mark.parametrize("period,page,printed,horizon,value,digest", REFERENCES)
def test_ipc_reference_in_original_pdf(period, page, printed, horizon, value, digest):
    path = REAL_PDF / f"ipm-{period}-{digest}.pdf"
    candidates = list(REAL_PDF.glob(f"ipm-{period}-*.pdf"))
    if not candidates:
        pytest.skip("PDF original no disponible localmente; no se acepta un sustituto")
    assert path in candidates, "El PDF disponible no coincide con la referencia verificada"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    layout = next(extract_pages(str(path), page_numbers=[page - 1]))
    lines = list(_lines(layout))
    text = " ".join(line.get_text().strip() for line in lines)
    assert "Anexo 2" in text and "Principales variables del pronóstico macroeconómico" in text
    assert "valores en negrilla corresponden a una proyección o supuesto" in text
    label = next(line for line in lines if "IPC total" in line.get_text())
    peers = [line for line in lines if abs(line.y0 - label.y0) < 2]
    assert any("Porcentaje, var. anual" in line.get_text() for line in peers)
    figures = sorted((line for line in peers if line.x0 > 300), key=lambda line: line.x0)
    headers = [
        line
        for line in lines
        if line.get_text().strip() == horizon and line.y0 > label.y0 and line.x0 > 300
    ]
    assert len(headers) == 1
    matches = [line for line in figures if abs(line.x0 - headers[0].x0) < 12]
    assert len(matches) == 1
    selected = matches[0]
    assert selected.get_text().strip() == value
    assert {char.fontname.split("+")[-1] for char in selected if isinstance(char, LTChar)} == {
        "FiraSans-Bold"
    }
    assert any(
        line.get_text().strip() == str(printed)
        and line.x0 > layout.width * 0.8
        and line.y0 < layout.height * 0.1
        for line in lines
    )


@pytest.mark.parametrize(
    "earlier,later,horizon,expected",
    [
        (REFERENCES[0], REFERENCES[1], "2025", Decimal("0.65")),
        (REFERENCES[2], REFERENCES[3], "2026", Decimal("2.7")),
    ],
)
def test_verified_reference_arithmetic_is_decimal(earlier, later, horizon, expected):
    chunks = [
        table(period, ANNEX, [cell(value, horizon)], page=page)
        for period, page, _, _, value, _ in (earlier, later)
    ]
    reports = {**REPORTS, "ipm-2025-10": report("2025-10")}
    (row,) = compare(
        chunks, reports, (f"ipm-{later[0]}", f"ipm-{earlier[0]}"), "IPC total", horizon=horizon
    ).rows
    assert [v.report_id for v in row.values] == [f"ipm-{earlier[0]}", f"ipm-{later[0]}"]
    assert [v.page_start for v in row.values] == [earlier[1], later[1]]
    assert row.differences[0].value == expected
    assert row.differences[0].kind == "revision_de_proyeccion"
    assert row.difference_unit == "puntos porcentuales"


def _comparison(left, right, *, indicator="IPC total", horizon=None, only_projections=True):
    return compare(
        [table("2025-01", ANNEX, [left]), table("2025-07", ANNEX, [right])],
        REPORTS,
        PAIR,
        indicator,
        horizon=horizon,
        only_projections=only_projections,
        explicit_indicator=True,
    )


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="B1: proyección o supuesto")
def test_b1_assumption_is_not_a_projection_revision():
    row = ("Variables exógenas", "Precio del petróleo (referencia Brent)")
    (result,) = _comparison(
        cell("73", row=row, unit="dólares por barril"),
        cell("68", row=row, unit="dólares por barril"),
        indicator="Precio del petróleo",
    ).rows
    assert result.differences[0].kind == "cambio_de_supuestos"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="B2: sin marca no es observado")
def test_b2_unmarked_cells_are_indeterminate_not_observed():
    (result,) = _comparison(
        cell("4,0", projection=False), cell("4,5", projection=False), only_projections=False
    ).rows
    assert result.differences[0].kind == "comparacion_indeterminada"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="B3: unidades ausentes coinciden")
def test_b3_unknown_units_do_not_authorize_difference():
    (result,) = _comparison(cell("4,0", unit=None), cell("4,5", unit=None)).rows
    assert not result.comparable and result.differences == ()


def test_b4_percent_and_percentage_points_are_not_interchangeable():
    (result,) = _comparison(
        cell("4,0", unit="porcentaje"), cell("4,5", unit="puntos porcentuales")
    ).rows
    assert not result.comparable and result.differences == ()


def test_b5_headline_and_core_are_distinct_rows():
    result = _comparison(
        cell("4,0"), cell("3,5", row=("Precios", "IPC sin alimentos ni regulados"))
    )
    assert result.rows == ()


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="B5: 'inflación' retrocede a 'ipc'")
def test_b5_generic_inflation_must_not_become_core_inflation():
    core = ("Precios", "IPC sin alimentos ni regulados")
    chunks = [
        table("2025-01", ANNEX, [cell("4,0", row=core)]),
        table("2025-07", ANNEX, [cell("4,5", row=core)]),
    ]
    result = compare(
        chunks, REPORTS, PAIR, "¿Cómo cambió la inflación entre enero y julio de 2025?"
    )
    assert result.rows == ()


def test_b6_analyst_expectations_do_not_join_bank_forecasts():
    chunks = [
        table("2025-01", ANNEX, [cell("4,0")]),
        table("2025-07", "Anexo 1 Expectativas de analistas", [cell("4,5")]),
    ]
    assert compare(chunks, REPORTS, PAIR, "IPC total", explicit_indicator=True).rows == ()


def test_b7_different_horizons_are_not_matched():
    assert _comparison(cell("4,0", "2025"), cell("4,5", "2026")).rows == ()


def test_b8_horizon_normalization_without_evidence_is_not_automatic():
    average = ("Precios", "IPC total", "Promedio anual")
    assert (
        _comparison(cell("4,0", "2025", row=average), cell("4,5", "dic-25", row=average)).rows == ()
    )


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="B8: fin del año = diciembre")
def test_b8_equivalent_horizons_when_row_establishes_end_of_year():
    # La fila del Anexo 2 dice «var. anual, fin del periodo»: 2025 termina en dic-25.
    result = _comparison(cell("4,0", "2025"), cell("4,5", "dic-25"))
    assert len(result.rows) == 1
    assert result.rows[0].differences[0].value == Decimal("0.5")


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="B9: título homónimo y fuentes")
def test_b9_homonymous_tables_with_different_provenance_are_not_joined():
    caption = "Cuadro 1 Resultados de inflación"
    bank = table("2025-01", caption, [cell("4,0")])
    analysts = table("2025-07", caption, [cell("4,5")])
    # La sección conserva la fuente cuando la plantilla reutiliza el mismo título.
    from dataclasses import replace

    bank = replace(bank, section=("Pronósticos del Banco", caption))
    analysts = replace(analysts, section=("Expectativas de analistas", caption))
    assert compare([bank, analysts], REPORTS, PAIR, "IPC total", explicit_indicator=True).rows == ()


def test_b10_ambiguous_parent_path_suppresses_difference():
    chunks = [
        table("2025-01", ANNEX, [cell("-1,7"), cell("-1,4")]),
        table("2025-07", ANNEX, [cell("-1,6")]),
    ]
    (result,) = compare(chunks, REPORTS, PAIR, "IPC total", explicit_indicator=True).rows
    assert not result.comparable and not result.differences


def test_b11_sign_change_uses_signed_decimal_operands():
    (result,) = _comparison(cell("-1,7"), cell("1,4")).rows
    assert result.differences[0].value == Decimal("3.1")


def test_b12_mixed_precision_is_preserved_and_warned():
    (result,) = _comparison(cell("4,05"), cell("4,7")).rows
    assert result.differences[0].value == Decimal("0.65")
    assert any("redondeo" in note for note in result.notes)


def test_b13_raw_table_cannot_supply_attributable_difference():
    chunks = [
        table("2025-01", ANNEX, [cell("4,0")]),
        table("2025-07", ANNEX, [], kind="table_raw", quality="unattributed_values"),
    ]
    assert compare(chunks, REPORTS, PAIR, "IPC total", explicit_indicator=True).rows == ()


def test_b14_missing_report_rejects_a_complete_comparison():
    with pytest.raises(ValueError, match="no indexados"):
        compare([], REPORTS, ("ipm-2025-01", "ipm-2024-01"), "IPC total")


def test_b15_three_reports_keep_missing_slot_in_chronological_order():
    chunks = [table("2025-01", ANNEX, [cell("4,0")]), table("2026-01", ANNEX, [cell("4,5")])]
    result = compare(chunks, REPORTS, ("ipm-2026-01", "ipm-2025-07", "ipm-2025-01"), "IPC total")
    assert [report.reference_period for report in result.reports] == [
        "2025-01",
        "2025-07",
        "2026-01",
    ]
    (row,) = result.rows
    assert [v.report_id if v else None for v in row.values] == ["ipm-2025-01", None, "ipm-2026-01"]
    assert any("Falta el valor" in note for note in row.notes)


def test_cited_figure_must_support_its_claim_not_just_occur_in_block():
    result = {
        "answer": "El IPC total para 2025 será 3,2 % [E1].",
        "citations": ["E1"],
        "insufficient_evidence": False,
    }
    labels = {"E1": "Anexo 2: IPC total — 2025: 4,7 %; 2026: 3,2 %."}
    assert verify(result, labels, "¿IPC total para 2025?")[0] == "rejected"


def test_swapped_evidence_citations_are_not_valid_claims():
    result = {
        "answer": "IPC total 9,25 % [E1]; tasa de política 4,7 % [E2].",
        "citations": ["E1", "E2"],
        "insufficient_evidence": False,
    }
    labels = {"E1": "IPC total: 4,7 %.", "E2": "Tasa de política: 9,25 %."}
    assert verify(result, labels, "¿IPC y tasa de política?")[0] == "rejected"
