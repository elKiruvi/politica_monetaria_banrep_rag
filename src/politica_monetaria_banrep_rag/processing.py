"""De PDF a unidades citables: párrafos, cuadros, leyendas de gráficos y notas al pie.

Cada unidad conserva la página del PDF (1-indexada, la que sirve para enlazar ``#page=N``), la
numeración impresa cuando existe y la ruta de secciones. Lo que se excluye (índices, cabeceras
repetidas, interior de gráficos) se cuenta en el informe de calidad; nada se completa con
información que no esté en el documento.
"""

from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from politica_monetaria_banrep_rag.layout import PARSER_VERSION, Block, PageLayout, extract_layout
from politica_monetaria_banrep_rag.tables import Table, TableError, build_tables

PROCESSING_VERSION = f"{PARSER_VERSION}+unidades-1"
_EXCLUDED = {"toc", "running_header", "page_number", "chart_residue", "small_text"}
_CHART_NOTE = "Los datos del gráfico no se extraen (fuera de alcance); solo su título y fuente."


@dataclass(frozen=True)
class Unit:
    kind: str  # paragraph | table | table_raw | chart_caption | footnote
    text: str
    page: int
    page_label: str | None
    section: tuple[str, ...]
    table: Table | None = None
    page_end: int | None = None  # si el párrafo continúa en otra página
    page_end_label: str | None = None


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


@dataclass(frozen=True)
class ProcessedDocument:
    report_id: str
    sha256: str
    version: str
    units: tuple[Unit, ...]
    quality: QualityReport

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


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


def _last_paragraph(units: list[Unit], section: tuple[str, ...]) -> int | None:
    """Índice del último párrafo; los gráficos, cuadros y notas intermedios no lo interrumpen."""
    for index in range(len(units) - 1, -1, -1):
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
                stack, in_box = [(1, block.text)], True
                continue
            if block.kind == "heading":
                level = block.level or 1
                if block.size >= 13 or (in_box and level >= 2):
                    in_box = False  # nuevo capítulo o subsección numerada del texto principal
                elif in_box:
                    level += 1
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
                kind = "footnote" if block.kind == "footnote" else "paragraph"
                previous = _last_paragraph(units, section)
                if (
                    kind == "paragraph"
                    and previous is not None
                    and _continues(units[previous], block)
                ):
                    units[previous] = _extend(units[previous], block, page)
                else:
                    units.append(Unit(kind, block.text, page.number, page.label, section))
    characters = sum(len(unit.text) for unit in units)
    warnings = []
    if pages_with_text < 0.8 * len(layout.pages):
        warnings.append("Menos del 80 % de las páginas tienen texto extraíble (¿PDF escaneado?).")
    if characters < 20_000:
        warnings.append("El documento tiene muy poco texto para un Informe de Política Monetaria.")
    if layout.ligatures.unresolved > 50:
        warnings.append("Hay muchas ligaduras «fi»/«fl» que no se pudieron reparar.")
    total_tables = reconstructed + len(failed)
    if total_tables and len(failed) > total_tables / 2:
        warnings.append("Más de la mitad de los cuadros no se pudieron reconstruir.")
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
        warnings=tuple(warnings),
    )
    return ProcessedDocument(report_id, sha256, PROCESSING_VERSION, tuple(units), quality)
