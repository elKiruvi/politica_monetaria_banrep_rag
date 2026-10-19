import pytest
from pdf_factory import make_layout_pdf

from politica_monetaria_banrep_rag.layout import extract_layout
from politica_monetaria_banrep_rag.tables import build_tables

BODY = [
    "En 2025 la demanda externa de Colombia crecería un 2,1 % anual",
    "y se moderaría en 2026 (2 %), una revisión al alza frente al",
    "Informe anterior. El déficit se ubicaría en -0,4 % del PIB y la incer-",
    "tidumbre seguiría elevada por las tensiones comerciales",
]


def page_one():
    items = [
        (367, 800, 8, "Informe de Política Monetaria | Octubre de 2025", "R"),
        (57, 760, 20, "2. Pronósticos macroeconómicos", "B"),
        (291, 700, 10, "2.1 Contexto externo", "B"),
    ]
    items += [(291, 680 - 12 * i, 10, text, "R") for i, text in enumerate(BODY)]
    # Gráfico en la barra lateral: leyenda, ejes (letra pequeña) y fuente.
    items += [
        (57, 700, 8, "Gráfico 2.1", "R"),
        (57, 691, 8, "PIB real de los socios comerciales", "R"),
        (57, 670, 5.5, "4,0", "R"), (57, 655, 5.5, "3,0", "R"), (57, 640, 5.5, "2,0", "R"),
        (75, 628, 5.5, "2024", "R"), (120, 628, 5.5, "2025", "R"),
        (57, 612, 6, "Fuente: Banco de la República.", "R"),
    ]  # fmt: skip
    # Cuadro en la barra lateral.
    items += [
        (57, 560, 9, "Cuadro 2.1", "R"),
        (57, 550, 9, "Proyecciones de crecimiento", "R"),
        (130, 528, 8, "2024", "R"), (170, 528, 8, "2025", "R"),
        (131, 519, 8, "(pr)", "R"), (168, 519, 8, "(proy)", "R"),
        (46, 528, 8, "Socios", "R"),
        (46, 500, 7.5, "Estados Unidos", "R"), (133, 500, 7.5, "2,8", "R"),
        (173, 500, 7.5, "1,7", "R"),
        (46, 486, 7.5, "Perú", "R"), (131, 486, 7.5, "-0,4", "R"), (173, 486, 7.5, "3,3", "R"),
        (46, 465, 6, "Fuente: Bloomberg.", "R"),
    ]  # fmt: skip
    items += [
        (291, 120, 8, "1", "R"),
        (304, 120, 8, "Las proyecciones usan datos al 10 de octubre.", "R"),
        (561, 30, 10, "13", "R"),
    ]
    return items


def page_two():
    return [
        (291, 700, 10, "comerciales entre las grandes economías.", "R"),
        (291, 670, 10, "2.2 Precios", "B"),
        (
            291,
            650,
            10,
            b"La in\xb1aci\xf3n se reduce; ver el gr\xe1\xb0co de precios y la forma",
            "L",
        ),
        (291, 638, 10, b"de la fuerza de fe y fuerza en la forma de los precios", "L"),
        (561, 30, 10, "14", "R"),
    ]


@pytest.fixture
def report_pdf(tmp_path):
    path = tmp_path / "informe.pdf"
    path.write_bytes(make_layout_pdf([page_one(), page_two()]))
    return path


def test_layout_classifies_blocks_and_page_labels(report_pdf):
    layout = extract_layout(report_pdf)
    first = layout.pages[0]

    def kind(prefix):
        return next(b.kind for b in first.blocks if b.text.startswith(prefix))

    assert first.label == "13" and layout.pages[1].label == "14"
    assert kind("Informe de Política Monetaria |") == "running_header"
    assert kind("2. Pronósticos") == "heading"
    assert kind("4,0") == "chart_residue"
    assert kind("Fuente: Banco") == "source_note"
    assert kind("2025 (proy)") == "table_cell"
    assert kind("1 Las proyecciones") == "footnote"  # marcador unido a su texto
    assert {b.column for b in first.blocks if b.text.startswith(("En 2025", "Gráfico"))} == {0, 1}
    heading = next(b for b in first.blocks if b.text.startswith("2.1"))
    assert heading.level == 2


def test_layout_excludes_off_page_lines_before_classification(tmp_path):
    path = tmp_path / "clipped.pdf"
    path.write_bytes(make_layout_pdf([[
        (60, 700, 10, "Visible narrative", "R"),
        (700, 600, 10, "Invisible right 9,9", "R"),
        (-120, 500, 10, "Invisible left 8,8", "R"),
        (605, 400, 10, "Partly outside 7,7", "R"),
        (60, 900, 10, "Invisible above 6,6", "R"),
    ]]))  # fmt: skip
    page = extract_layout(path).pages[0]
    assert [block.text for block in page.blocks] == ["Visible narrative"]
    assert all(0 <= line.bbox[0] < line.bbox[2] <= page.width
               for block in page.blocks for line in block.lines)  # fmt: skip


def annex_page():
    years = [2023, 2024, 2025, 2026]
    items = [(57, 780, 16, "Anexo 2 Principales variables del pronóstico", "B")]
    items += [(320 + 40 * i, 740, 7, str(year), "R") for i, year in enumerate(years)]
    items += [(57, 725, 5, "Internas", "R")]
    values = {"IPC total": ["9,3", "5,2", "5,1", "3,6"], "PIB": ["0,7", "1,6", "2,6", "2,9"],
              "Desempleo": ["10,2", "9,1", "8,9", "8,6"], "TPM": ["13,0", "9,5", "9,25", "8,25"],
              "Tasa de cambio": ["4.325", "4.071", "4.000", "4.100"],
              "Brent": ["82", "80", "68", "58"]}  # fmt: skip
    for row, (label, cells) in enumerate(values.items()):
        y = 712 - 9 * row
        items.append((60, y, 5, label, "R"))
        for i, value in enumerate(cells):
            items.append((322 + 40 * i, y, 5, value, "B" if i >= 2 else "R"))
    items.append((57, 600, 5, "Nota: los valores en negrilla corresponden a una proyección o "
                  "supuesto.", "R"))  # fmt: skip
    return items


def toc_page():
    return [
        (57, 760 - 14 * i, 10, f"Gráfico 2.{i} Título del gráfico número {i}  {10 + i}", "R")
        for i in range(1, 12)
    ]


def test_broken_ligatures_are_repaired_by_glyph_width(tmp_path):
    path = tmp_path / "ligaduras.pdf"
    items = [
        (300, 700, 10, b"La forma de fe y la fuerza fueron firmes", "L"),
        (300, 686, 10, b"Gr\xe1\xb0co de la in\xb1aci\xf3n y la forma fuerte", "L"),
        (300, 672, 10, b"La forma refuerza fe y fuerza; se re\xb1eja en el gr\xe1\xb0co", "L"),
    ]
    path.write_bytes(make_layout_pdf([items]))
    layout = extract_layout(path)
    text = layout.pages[0].blocks[0].text
    assert "Gráfico de la inflación" in text and "se refleja en el gráfico" in text
    assert "La forma de fe y la fuerza" in text  # la «f» normal no cambia
    assert layout.ligatures.repaired == 4 and layout.ligatures.unresolved == 0


def test_dense_table_and_toc_are_detected(tmp_path):
    path = tmp_path / "anexo.pdf"
    path.write_bytes(make_layout_pdf([toc_page(), annex_page()]))
    layout = extract_layout(path)
    assert {b.kind for b in layout.pages[0].blocks} == {"toc"}
    blocks = layout.pages[1].blocks
    caption = next(b for b in blocks if b.kind == "caption")
    assert caption.text.startswith("Anexo 2") and caption.region is not None
    cells = [line for b in blocks if b.kind == "table_cell" for line in b.lines]
    notes = [b.text for b in blocks if b.kind == "source_note"]
    (table,) = build_tables(caption.text, 2, cells, notes)
    assert table.columns == ("2023", "2024", "2025", "2026") and table.bold_is_projection


def test_ligatures_more_frequent_than_plain_f_are_still_repaired(tmp_path):
    path = tmp_path / "ligaduras2.pdf"
    items = [
        (300, 700, 8, b"La in\xb1aci\xf3n y el gr\xe1\xb0co; la in\xb1aci\xf3n \xb0nal", "L"),
        (300, 688, 8, b"d\xe9\xb0cit \xb0scal y la in\xb1aci\xf3n con forma", "L"),
    ]
    path.write_bytes(make_layout_pdf([items]))
    text = " ".join(b.text for b in extract_layout(path).pages[0].blocks)
    assert "inflación" in text and "gráfico" in text and "déficit fiscal" in text
    assert "forma" in text


def test_annex_continuation_without_title_and_orphan_small_text(tmp_path):
    path = tmp_path / "continuacion.pdf"
    page = annex_page()[1:] + [(400, 300, 5.5, "dic-22", "R"), (450, 300, 5.5, "4 6", "R")]
    path.write_bytes(make_layout_pdf([page]))
    blocks = extract_layout(path).pages[0].blocks
    cells = [b for b in blocks if b.kind == "table_cell"]
    assert cells and all(b.region is not None for b in cells)
    assert not any(b.kind == "caption" for b in blocks)  # sin título: continuación
    assert {b.text for b in blocks if b.kind == "small_text"} == {"dic-22", "4 6"}


def test_chapter_title_in_sidebar_comes_first_and_note_calls_are_removed(tmp_path):
    path = tmp_path / "capitulo.pdf"
    items = [
        (57, 780, 16, "1. Resumen", "B"),
        (291, 760, 10, "1.1 Resumen macroeconómico6, 7", "B"),
        (291, 740, 10, "La inflación bajaría al 3 % en 2026 según el pronóstico.", "R"),
        (291, 728, 10, "El crecimiento económico seguiría recuperándose este año.", "R"),
        (57, 600, 8, "Gráfico 1.1 Inflación", "R"),
        (57, 560, 6, "Fuente: DANE.", "R"),
        (200, 760, 10, "x", "R"),
    ]
    path.write_bytes(make_layout_pdf([items]))
    blocks = extract_layout(path).pages[0].blocks
    headings = [b.text for b in blocks if b.kind == "heading"]
    assert headings == ["1. Resumen", "1.1 Resumen macroeconómico"]


def test_unicode_ligatures_are_expanded():
    from politica_monetaria_banrep_rag.layout import _join_lines

    assert _join_lines(["La in\ufb02ación y el gr\u00e1\ufb01co"]) == "La inflación y el gráfico"


def test_small_notes_are_notes_not_chart_labels(tmp_path):
    path = tmp_path / "notas.pdf"
    items = [
        (57, 700, 6, "Significancia estadística: * p < 0,10; ** p < 0,05.", "R"),
        (57, 680, 6, "* Para el consumo de los hogares. Fuente: DANE.", "R"),
        (57, 660, 5, "dic-22", "R"),
    ]
    path.write_bytes(make_layout_pdf([items]))
    kinds = {b.text[:12]: b.kind for b in extract_layout(path).pages[0].blocks}
    assert kinds["Significanci"] == "source_note" and kinds["* Para el co"] == "source_note"
    assert kinds["dic-22"] == "small_text"
