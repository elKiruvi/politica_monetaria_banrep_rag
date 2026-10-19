"""De PDF a unidades citables: párrafos, cuadros, leyendas de gráficos y notas al pie.

Cada unidad conserva la página del PDF (1-indexada, la que sirve para enlazar ``#page=N``), la
numeración impresa cuando existe y la ruta de secciones. Lo que se excluye (índices, cabeceras
repetidas, interior de gráficos) se cuenta en el informe de calidad; nada se completa con
información que no esté en el documento.
"""

import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from politica_monetaria_banrep_rag.layout import PARSER_VERSION, Block, PageLayout, extract_layout
from politica_monetaria_banrep_rag.tables import Table, TableError, build_tables

PROCESSING_VERSION = f"{PARSER_VERSION}+unidades-3"
_EXCLUDED = {"toc", "running_header", "page_number", "chart_residue", "small_text"}
_CHART_NOTE = "Los datos del gráfico no se extraen (fuera de alcance); solo su título y fuente."
CRITICAL_SECTIONS = {"1": "Resumen", "2": "Pronósticos", "3": "Situación económica"}


@dataclass(frozen=True)
class Unit:
    kind: str  # paragraph | table | table_raw | chart_caption | caption | source_note | footnote
    text: str
    page: int
    page_label: str | None
    section: tuple[str, ...]
    table: Table | None = None
    page_end: int | None = None  # si el párrafo continúa en otra página
    page_end_label: str | None = None
    evidence_quality: str = field(init=False)

    def __post_init__(self) -> None:
        default = (
            self.table.evidence_quality
            if self.kind == "table" and self.table
            else "unattributed_values"
        )
        quality = {
            "paragraph": "narrative_only",
            "footnote": "note_only",
            "chart_caption": "caption_only",
            "caption": "caption_only",
            "table_raw": "unattributed_values",
            "source_note": "note_only",
        }.get(self.kind, default)
        object.__setattr__(self, "evidence_quality", quality)


@dataclass(frozen=True)
class QualityReport:
    pages: int
    pages_with_text: int
    characters: int
    units: dict[str, int]
    excluded_blocks: dict[str, int]
    tables_reconstructed: int
    tables_failed: tuple[tuple[int, str, str], ...]
    ligatures_repaired: int
    ligatures_unresolved: int
    degraded: bool
    warnings: tuple[str, ...] = field(default_factory=tuple)
    critical_sections: dict[str, dict[str, int | bool]] = field(default_factory=dict)


@dataclass(frozen=True)
class ProcessedDocument:
    report_id: str
    sha256: str
    version: str
    units: tuple[Unit, ...]
    quality: QualityReport

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def quality_warnings(
    pages: int,
    pages_with_text: int,
    characters: int,
    ligatures_unresolved: int,
    tables_reconstructed: int,
    tables_failed: int,
) -> tuple[str, ...]:
    """Deduce las advertencias de los contadores, también al releer un artefacto."""
    warnings = []
    if pages_with_text < 0.8 * pages:
        warnings.append("Menos del 80 % de las páginas tienen texto extraíble (¿PDF escaneado?).")
    if characters < 20_000:
        warnings.append("El documento tiene muy poco texto para un Informe de Política Monetaria.")
    if ligatures_unresolved > 50:
        warnings.append("Hay muchas ligaduras «fi»/«fl» que no se pudieron reparar.")
    total_tables = tables_reconstructed + tables_failed
    if total_tables and tables_failed > total_tables / 2:
        warnings.append("Más de la mitad de los cuadros no se pudieron reconstruir.")
    return tuple(warnings)


def critical_section_coverage(units: tuple[Unit, ...]) -> dict[str, dict[str, int | bool]]:
    """Cobertura textual por capítulo; un cuadro crudo no acredita cifras ni cubre el texto."""
    coverage: dict[str, dict[str, int | bool]] = {
        chapter: {
            "text_units": 0,
            "structured_tables": 0,
            "raw_tables": 0,
            "unattributed_tables": 0,
            "charts": 0,
            "covered": False,
        }  # fmt: skip
        for chapter in CRITICAL_SECTIONS
    }
    field = {"paragraph": "text_units", "table_raw": "raw_tables", "chart_caption": "charts"}
    for unit in units:
        if not unit.section:
            continue
        chapter = re.match(r"^([123])\.\s", unit.section[0])
        if chapter is None or unit.kind not in (*field, "table"):
            continue
        entry = coverage[chapter[1]]
        if unit.kind == "table":
            key = (
                "structured_tables"
                if unit.evidence_quality == "structured_cells"
                else "unattributed_tables"
            )
        else:
            key = field[unit.kind]
        entry[key] = int(entry[key]) + 1
    for entry in coverage.values():
        entry["covered"] = entry["text_units"] > 0
    return coverage


def _region_units(
    blocks: list[Block], page: PageLayout, section: tuple[str, ...], previous_table: str | None
) -> tuple[list[Unit], int, list[tuple[int, str, str]], str | None]:
    """Unidades de un gráfico o cuadro; nunca descarta su texto.

    Un cuadro sin leyenda en su página (p. ej. la segunda página de un anexo) se titula como
    continuación del último cuadro; si no lo hay, «Cuadro sin título (página N)».
    """
    caption_block = next((b for b in blocks if b.kind == "caption"), None)
    notes = [b.text for b in blocks if b.kind == "source_note"]
    cells = [b for b in blocks if b.kind == "table_cell"]
    if caption_block is not None:
        caption = caption_block.text
    elif previous_table:
        caption = f"{previous_table.split(' (continuación)')[0]} (continuación)"
    else:
        caption = f"Cuadro sin título (página {page.number})"
    if not cells:  # gráfico: título, subtítulos de panel y fuentes
        panels = [b.text for b in blocks if b.kind == "chart_panel"]
        text = " ".join([caption, *panels, *notes, _CHART_NOTE])
        return [Unit("chart_caption", text, page.number, page.label, section)], 0, [], None
    lines = [line for block in cells for line in block.lines]
    try:
        tables = build_tables(caption, page.number, lines, notes)
    except TableError as exc:
        rows: dict[float, list[tuple[float, str]]] = {}
        for line in lines:
            key = round((line.bbox[1] + line.bbox[3]) / 2)
            rows.setdefault(key, []).append((line.bbox[0], line.text))
        body = [" | ".join(text for _, text in sorted(rows[y])) for y in sorted(rows, reverse=True)]
        text = "\n".join(
            [f"{caption} (cuadro no reconstruido: el texto se muestra sin columnas)"] + body + notes
        )
        failure = (page.number, caption[:120], str(exc))
        return [Unit("table_raw", text, page.number, page.label, section)], 0, [failure], caption
    units = [Unit("table", t.to_text(), page.number, page.label, section, t) for t in tables]
    return units, len(tables), [], caption


def _last_paragraph(units: list[Unit], section: tuple[str, ...], page: int) -> int | None:
    """Un cuadro interpuesto en esta página corta el texto; una barra lateral previa no."""
    for index in range(len(units) - 1, -1, -1):
        if units[index].kind in {"table", "table_raw"} and units[index].page == page:
            return None
        if units[index].kind == "paragraph":
            return index if units[index].section == section else None
    return None


def _continues(previous: Unit, block: Block) -> bool:
    """Un párrafo sigue al anterior si este no cerró la oración y el nuevo empieza en minúscula."""
    return bool(block.text[:1].islower()) and not previous.text.rstrip().endswith(
        (".", ":", "?", "!", "»", ")")
    )


def _extend(previous: Unit, block: Block, page: PageLayout) -> Unit:
    joined = (
        previous.text[:-1] + block.text
        if previous.text.endswith("-") and not previous.text.endswith(" -")
        else f"{previous.text} {block.text}"
    )
    return Unit(
        previous.kind,
        joined,
        previous.page,
        previous.page_label,
        previous.section,
        previous.table,
        page.number if page.number != previous.page else previous.page_end,
        page.label if page.number != previous.page else previous.page_end_label,
    )


def process_pdf(path: Path, report_id: str, sha256: str) -> ProcessedDocument:
    """Analiza el PDF completo y devuelve sus unidades con el informe de calidad."""
    layout = extract_layout(path)
    units: list[Unit] = []
    excluded: Counter[str] = Counter()
    failed: list[tuple[int, str, str]] = []
    reconstructed = 0
    stack: list[tuple[int, str]] = []
    in_box = False  # dentro de un «Recuadro»: sus subtítulos «1.», «2.» cuelgan de él
    box_numbers: dict[str, int] = {}  # apartado «N.» del recuadro → última subsección «N.M»
    main_stack: list[tuple[int, str]] = []  # secciones del texto principal al abrir el recuadro
    previous_table: str | None = None
    pages_with_text = 0
    for page in layout.pages:
        if any(b.kind not in _EXCLUDED for b in page.blocks):
            pages_with_text += 1
        regions: dict[int, list[Block]] = {}
        for block in page.blocks:
            if block.region is not None:
                regions.setdefault(block.region, []).append(block)
        done: set[int] = set()
        for block in page.blocks:
            if block.kind in _EXCLUDED:
                excluded[block.kind] += 1
                continue
            if block.kind == "caption" and block.text.startswith("Recuadro"):
                if not in_box:
                    main_stack = stack
                stack, in_box, box_numbers = [(1, block.text)], True, {}
                continue
            if block.kind == "heading":
                level = block.level or 1
                number = re.match(r"(\d+)\.(\d+)?", block.text)
                top, sub = (number[1], int(number[2] or 0)) if number else (None, 0)
                if block.size >= 13:
                    in_box = False  # nuevo capítulo
                elif in_box and level == 1 and top:
                    box_numbers[top] = 0
                    level = 2
                elif in_box and level >= 2 and top in box_numbers and sub == box_numbers[top] + 1:
                    box_numbers[top] = sub  # «2.1», «2.2» seguidos dentro del apartado «2.»
                    level += 1
                elif in_box and level >= 2:
                    in_box = False  # subsección del texto principal («3.5») tras el recuadro
                    stack = main_stack
                stack = [(lvl, title) for lvl, title in stack if lvl < level] + [
                    (level, block.text)
                ]
                continue
            if block.region is not None:
                if block.region in done:
                    continue
                done.add(block.region)
                region = regions[block.region]
                caption = next((b.text for b in region if b.kind == "caption"), "")
                if caption.startswith("Anexo"):
                    stack, in_box = [(1, caption.split(" (continuación)")[0])], False
                section = tuple(title for _, title in stack)
                region_units, ok, failures, table_caption = _region_units(
                    region, page, section, previous_table
                )
                units += region_units
                reconstructed += ok
                failed += failures
                previous_table = table_caption or previous_table
                continue
            section = tuple(title for _, title in stack)
            if block.kind in {"paragraph", "footnote", "caption", "source_note"}:
                kind = (
                    block.kind
                    if block.kind in {"footnote", "source_note", "caption"}
                    else "paragraph"
                )
                previous = _last_paragraph(units, section, page.number)
                if (
                    kind == "paragraph"
                    and previous is not None
                    and _continues(units[previous], block)
                ):
                    units[previous] = _extend(units[previous], block, page)
                else:
                    units.append(Unit(kind, block.text, page.number, page.label, section))
    characters = sum(len(unit.text) for unit in units)
    warnings = quality_warnings(
        len(layout.pages),
        pages_with_text,
        characters,
        layout.ligatures.unresolved,
        reconstructed,
        len(failed),
    )
    quality = QualityReport(
        pages=len(layout.pages),
        pages_with_text=pages_with_text,
        characters=characters,
        units=dict(Counter(unit.kind for unit in units)),
        excluded_blocks=dict(excluded),
        tables_reconstructed=reconstructed,
        tables_failed=tuple(failed),
        ligatures_repaired=layout.ligatures.repaired,
        ligatures_unresolved=layout.ligatures.unresolved,
        degraded=bool(warnings),
        warnings=warnings,
        critical_sections=critical_section_coverage(tuple(units)),
    )
    return ProcessedDocument(report_id, sha256, PROCESSING_VERSION, tuple(units), quality)
