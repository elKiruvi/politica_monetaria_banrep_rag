import pytest

from politica_monetaria_banrep_rag.layout import Line
from politica_monetaria_banrep_rag.tables import (
    TableError,
    _assign_spans,
    build_table,
    build_tables,
)


def line(x, y, text, bold=False, width=None, size=7.5):
    w = width if width is not None else 4.0 * len(text)
    return Line(text, (x, y, x + w, y + 7), size, bold)


def cell(cx, y, text, bold=False):
    w = 4.0 * len(text)
    return line(cx - w / 2, y, text, bold, w)


def growth_table():
    return [
        cell(140, 120, "2024"), cell(180, 120, "2025"),
        cell(140, 111, "(pr)"), cell(180, 111, "(proy)"),
        line(40, 120, "Principales socios", width=60),
        line(40, 95, "Estados Unidos", width=55), cell(140, 95, "2,8"), cell(180, 95, "1,7", True),
        line(40, 81, "Perú", width=20), cell(140, 81, "-0,4"), cell(180, 81, "3,3 %", True),
        line(40, 70, "Total socios", width=45), line(40, 61, "comerciales a/", width=55),
        cell(140, 66, "1,8"), cell(180, 66, "2,1", True),
    ]  # fmt: skip


def test_rows_columns_units_and_multiline_labels():
    table = build_table("Cuadro 2.1 Proyecciones", 13, growth_table(), ["Fuente: Bloomberg."])
    assert table.columns == ("2024 (pr)", "2025 (proy)")
    assert table.label_headers == ("Principales socios",)
    assert [row.labels[0] for row in table.rows] == [
        "Estados Unidos",
        "Perú",
        "Total socios comerciales a/",
    ]
    assert table.row_text(table.rows[1]) == "Perú — 2024 (pr): -0,4; 2025 (proy): 3,3 %"
    assert "Columnas: Principales socios | 2024 (pr) | 2025 (proy)" in table.to_text()
    assert not table.bold_is_projection  # sin nota, la negrilla no significa nada


def test_bold_marks_projection_only_when_the_note_says_so():
    notes = ["Nota: los valores en negrilla corresponden a una proyección o supuesto."]
    table = build_table("Anexo 2", 47, growth_table(), notes)
    assert table.bold_is_projection
    text = table.row_text(table.rows[0])
    assert text == "Estados Unidos — 2024 (pr): 2,8; 2025 (proy): 1,7 (proyección o supuesto)"


def test_group_rows_build_a_path_for_following_rows():
    lines = [
        cell(150, 130, "2024"), cell(190, 130, "2025"),
        line(37, 112, "Externas", width=30),
        line(40, 100, "Petróleo", width=30), line(90, 100, "Dólares", width=30),
        cell(150, 100, "80"), cell(190, 100, "68"),
        line(37, 88, "Internas", width=30),
        line(40, 76, "IPC total", width=30), line(90, 76, "Porcentaje", width=40),
        cell(150, 76, "5,2"), cell(190, 76, "5,1"),
    ]  # fmt: skip
    table = build_table("Anexo 2", 47, lines, [])
    assert table.rows[0].group == ("Externas",)
    assert table.rows[1].group == ("Internas",)
    assert table.row_text(table.rows[1]) == (
        "Internas > IPC total > Porcentaje — 2024: 5,2; 2025: 5,1"
    )


@pytest.mark.parametrize(
    "extra,reason",
    [
        ([cell(140, 95, "9,9")], "dos cifras"),
        ([line(150, 88, "nota suelta", width=30)], "texto suelto"),
        ([cell(220, 95, "4,4")], "sin encabezado"),
    ],
)
def test_ambiguous_geometry_fails_instead_of_guessing(extra, reason):
    with pytest.raises(TableError, match=reason):
        build_table("Cuadro", 1, growth_table() + extra, [])


def test_row_without_label_fails():
    lines = [cell(140, 120, "2024"), cell(140, 95, "2,8"), cell(140, 81, "3,0")]
    lines.append(line(40, 95, "Fila", width=20))
    with pytest.raises(TableError, match="fila sin etiqueta"):
        build_table("Cuadro", 1, lines, [])


def test_merged_headers_and_contained_dashes_are_recomposed():
    lines = [
        line(120, 120, "dic 24 mar 25", width=80),  # dos encabezados fusionados
        line(210, 120, "abr 25", width=24), line(221, 120, "-", width=2),
        line(40, 95, "TPM", width=20), cell(140, 95, "9,50"), cell(180, 95, "9,50"),
        cell(222, 95, "9,25"),
    ]  # fmt: skip
    table = build_tables("Cuadro 3.1", 39, lines, [])[0]
    assert table.columns == ("dic 24", "mar 25", "abr-25")


def test_stacked_subtables_with_their_own_headers():
    lines = [
        cell(150, 200, "oct-25"), cell(190, 200, "dic-25"),
        line(40, 180, "IPC", width=20), cell(150, 180, "5,47 c/"), cell(190, 180, "5,21"),
        cell(120, 150, "I-2026"), cell(160, 150, "II-2026"), cell(200, 150, "2026"),
        line(40, 130, "PIB", width=20), cell(120, 130, "2,7"), cell(160, 130, "2,9"),
        cell(200, 130, "n. d."),
    ]  # fmt: skip
    first, second = build_tables("Anexo 1", 46, lines, [])
    assert first.caption == "Anexo 1 (parte 1 de 2)"
    assert first.row_text(first.rows[0]) == "IPC — oct-25: 5,47 c/; dic-25: 5,21"
    assert second.columns == ("I-2026", "II-2026", "2026")
    assert second.row_text(second.rows[0]) == "PIB — I-2026: 2,7; II-2026: 2,9; 2026: n. d."


def test_spanning_headers_are_partitioned_without_ties():
    # 2019 cubre T3-T4, 2020 cubre T1-T4 y 2021 T1-T2: el T4 de 2020 equidista de 2020 y 2021.
    columns = [396, 417, 438, 460, 481, 502, 523, 545]
    owners = _assign_spans(columns, [406, 470, 534])
    assert owners == [0, 0, 1, 1, 1, 1, 2, 2]


def test_quarter_columns_under_year_spans():
    lines = [cell(x, 140, str(year)) for x, year in [(406, 2019), (470, 2020)]]
    for x, quarter in zip(
        [396, 417, 438, 460, 481, 502], ["T3", "T4", "T1", "T2", "T3", "T4"], strict=True
    ):
        lines += [cell(x, 130, quarter), cell(x, 110, "1,0")]
    lines.append(line(40, 110, "PIB", width=20))
    table = build_table("Anexo 2 (continuación)", 48, lines, [])
    assert table.columns == ("2019 T3", "2019 T4", "2020 T1", "2020 T2", "2020 T3", "2020 T4")
