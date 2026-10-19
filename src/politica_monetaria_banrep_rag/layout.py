"""Extracción de bloques con posición y tipo desde un PDF con capa de texto.

Se usa el análisis de diseño de pdfminer.six: cada caja de texto conserva página (1-indexada),
coordenadas, tamaño de letra y negrita. Las reglas de clasificación son conservadoras: nada se
borra; lo que no se indexa (interior de gráficos, cabeceras repetidas) queda marcado y contado.
"""

import logging
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from pdfminer.high_level import extract_pages
from pdfminer.layout import LAParams, LTChar, LTPage, LTTextBoxHorizontal, LTTextLineHorizontal
from pdfminer.pdfexceptions import PDFException
from pdfminer.psexceptions import PSException

PARSER_VERSION = "pdfminer-layout-1"

_CAPTION = re.compile(r"^(Gráfico|Cuadro|Recuadro|Diagrama|Mapa|Figura)\s+[A-Z]?\d+(\.\d+)*\b")
_SOURCE = re.compile(
    r"^(Fuentes?|Notas?|Nota metodológica)\s*:|^\([a-z]{2,4}\):|^[a-z]/\s|^n\.\s?[ad]\.\s?:"
    r"|^\*+\s|^Significancia\b"
)
_NUMBERED_HEADING = re.compile(r"^(\d+(?:\.\d+){0,3})\.?\s+\S")
_RUNNING_HEADER = re.compile(r"^Informe de Política Monetaria\s*\|", re.IGNORECASE)
_PAGE_NUMBER = re.compile(r"^\d{1,3}$")


class LayoutError(Exception):
    """El PDF no tiene una capa de texto utilizable."""


_BOLD_MARKS = ("Bold", "Heavy", "Black", "SemiBold")


@dataclass(frozen=True)
class Line:
    text: str
    bbox: tuple[float, float, float, float]
    size: float
    bold: bool


@dataclass(frozen=True)
class Block:
    page: int
    kind: str
    text: str
    bbox: tuple[float, float, float, float]
    size: float
    bold: bool
    column: int = 0
    level: int = 0  # nivel de título (1 = capítulo)
    region: int | None = None  # gráfico o cuadro al que pertenece
    lines: tuple[Line, ...] = ()


@dataclass(frozen=True)
class LigatureRepair:
    """Glifos «fi»/«fl» que el PDF declara como «f» (ToUnicode incompleto)."""

    repaired: int = 0
    unresolved: int = 0


@dataclass(frozen=True)
class PageLayout:
    number: int
    label: str | None
    width: float
    height: float
    blocks: tuple[Block, ...] = field(default_factory=tuple)


_UNICODE_LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"})


def _join_lines(lines: Iterable[str]) -> str:
    """Une líneas; solo elimina el guion de corte si la línea siguiente sigue en minúscula."""
    text = ""
    for raw in lines:
        line = " ".join(raw.translate(_UNICODE_LIGATURES).split())
        if not line:
            continue
        if text.endswith("-") and not text.endswith(" -") and line[:1].islower():
            text = text[:-1] + line
        else:
            text = f"{text} {line}" if text else line
    return text


def _dominant(chars: list[LTChar]) -> tuple[str, float]:
    fonts: Counter[tuple[str, float]] = Counter()
    for char in chars:
        if not char.get_text().isspace():
            fonts[(char.fontname.split("+")[-1], round(char.size, 1))] += 1
    return fonts.most_common(1)[0][0] if fonts else ("", 0.0)


_Ligatures = dict[str, dict[float, str]]


def _font(char: LTChar) -> str:
    return char.fontname.split("+")[-1]


def _ligature_table(pages: list[LTPage]) -> tuple[_Ligatures, int]:
    """Por fuente, anchuras de «f» que en realidad son ligaduras «fi» (estrecha) y «fl» (ancha).

    Se compara la anchura normalizada por tamaño con la «f» más frecuente de la misma fuente;
    si solo hay un grupo de ligaduras no se puede saber cuál es y no se repara.
    """
    widths: dict[str, list[float]] = {}
    for page in pages:
        for element in page:
            if isinstance(element, LTTextBoxHorizontal):
                for line in element:
                    if isinstance(line, LTTextLineHorizontal):
                        for char in line:
                            if isinstance(char, LTChar) and char.get_text() == "f" and char.size:
                                widths.setdefault(_font(char), []).append(
                                    round(char.width / char.size, 3)
                                )
    table: _Ligatures = {}
    unresolved = 0
    for font, values in widths.items():
        # La «f» sola es el glifo más estrecho; las ligaduras pueden ser más frecuentes que ella.
        regular = _clusters_1d(values, 0.006)[0]
        wide = [w for w in values if w >= 1.5 * regular]
        groups = _clusters_1d(wide, 0.006)
        if len(groups) == 2:
            table[font] = {round(g, 3): name for g, name in zip(groups, ("fi", "fl"), strict=True)}
        else:
            unresolved += len(wide)  # sin dos grupos no se distingue «fi» de «fl»
    return table, unresolved


def _clusters_1d(values: list[float], tolerance: float) -> list[float]:
    groups: list[list[float]] = []
    for value in sorted(values):
        if groups and value - groups[-1][-1] <= tolerance:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [sum(group) / len(group) for group in groups]


def _char_text(char: LTChar, ligatures: _Ligatures, counts: Counter[str]) -> str:
    text = char.get_text()
    if text != "f" or not char.size:
        return text
    table = ligatures.get(_font(char))
    width = round(char.width / char.size, 3)
    if table:
        nearest = min(table, key=lambda w: abs(w - width))
        if abs(nearest - width) <= 0.006:
            counts["repaired"] += 1
            return table[nearest]
    return text


def _box(
    element: LTTextBoxHorizontal,
    page: int,
    width: float,
    height: float,
    ligatures: _Ligatures,
    counts: Counter[str],
) -> Block | None:
    lines: list[Line] = []
    chars: list[LTChar] = []
    for line in element:
        if not isinstance(line, LTTextLineHorizontal):
            continue
        x0, y0, x1, y1 = line.bbox
        if x0 < 0 or y0 < 0 or x1 > width or y1 > height:
            # Pdfminer can expose text positioned beyond the visible page (wide annexes).
            # A citation to this page must not inherit cells from its invisible half.
            continue
        line_chars = [c for c in line if isinstance(c, LTChar)]
        chars += line_chars
        raw = "".join(
            _char_text(obj, ligatures, counts) if isinstance(obj, LTChar) else obj.get_text()
            for obj in line
        )
        text = " ".join(raw.translate(_UNICODE_LIGATURES).split())
        if text:
            font, size = _dominant(line_chars)
            x0, y0, x1, y1 = (round(v, 1) for v in line.bbox)
            lines.append(Line(text, (x0, y0, x1, y1), size, any(m in font for m in _BOLD_MARKS)))
    text = _join_lines(line.text for line in lines)
    font, size = _dominant(chars)
    if not text or not font:
        return None
    bold = any(mark in font for mark in _BOLD_MARKS)
    x0 = min(line.bbox[0] for line in lines)
    y0 = min(line.bbox[1] for line in lines)
    x1 = max(line.bbox[2] for line in lines)
    y1 = max(line.bbox[3] for line in lines)
    return Block(page, "paragraph", text, (x0, y0, x1, y1), size, bold, lines=tuple(lines))


def _split_columns(blocks: list[Block], width: float) -> float | None:
    """Busca un canal vertical sin texto entre el 30 % y el 70 % del ancho de la página."""
    narrow = [b for b in blocks if b.bbox[2] - b.bbox[0] < 0.6 * width]
    if len(narrow) < 2:
        return None
    covered = sorted((b.bbox[0], b.bbox[2]) for b in narrow)
    best: tuple[float, float] | None = None
    reach = covered[0][1]
    for start, end in covered[1:]:
        if start > reach and 0.3 * width <= (start + reach) / 2 <= 0.7 * width:
            gap = (reach, start)
            if best is None or gap[1] - gap[0] > best[1] - best[0]:
                best = gap
        reach = max(reach, end)
    return (best[0] + best[1]) / 2 if best and best[1] - best[0] >= 8 else None


def _with(block: Block, **changes: object) -> Block:
    data = {**block.__dict__, **changes}
    return Block(**data)  # type: ignore[arg-type]


def _classify(blocks: list[Block], width: float, height: float) -> tuple[list[Block], str | None]:
    label = None
    result = []
    for block in blocks:
        x0, y0, x1, y1 = block.bbox
        kind = block.kind
        level = 0
        if _RUNNING_HEADER.match(block.text) or (y0 > height - 70 and block.size <= 8.5):
            kind = "running_header"
        elif _PAGE_NUMBER.match(block.text) and (y1 < 55 or y0 > height - 55):
            kind, label = "page_number", block.text
        elif _CAPTION.match(block.text):
            kind = "caption"
        elif _SOURCE.match(block.text) and block.size <= 8.5:
            kind = "source_note"
        elif block.size >= 13 and block.bold and len(block.text) < 160:
            kind, level = "heading", 1
        elif (match := _NUMBERED_HEADING.match(block.text)) and (
            len(block.text) < 120 and block.size >= 9.5 and len(block.lines) <= 2
        ):
            kind, level = "heading", match[1].count(".") + 1
        result.append(_with(block, kind=kind, level=level))
    return result, label


def _mark_regions(blocks: list[Block], split: float | None, start: int) -> tuple[list[Block], int]:
    """Asigna a cada «Gráfico»/«Cuadro» las cajas entre su leyenda y su nota de fuentes."""

    def column(block: Block) -> int:
        if split is None:
            return 0
        return 0 if block.bbox[2] <= split else 1 if block.bbox[0] >= split else -1

    blocks = [_with(b, column=column(b)) for b in blocks]
    region = start
    for caption in [b for b in blocks if b.kind == "caption"]:
        if caption.text.startswith("Recuadro"):
            continue
        same = [b for b in blocks if b.column == caption.column and b.bbox[3] < caption.bbox[1] + 1]
        notes = [
            b
            for b in same
            if b.kind == "source_note"
            and b.bbox[0] <= caption.bbox[2]
            and b.bbox[2] >= caption.bbox[0]  # misma franja horizontal que la leyenda
        ]
        bottom = max((n.bbox[3] for n in notes), default=None)
        if bottom is None:
            continue
        others = [b for b in same if b.kind == "caption" and b is not caption]
        stop = max([bottom] + [o.bbox[3] for o in others if o.bbox[3] > bottom])
        # Ancho del gráfico o cuadro: de la leyenda y sus notas (por si no hay canal de columnas).
        left = min([caption.bbox[0]] + [n.bbox[0] for n in notes]) - 15
        right = max([caption.bbox[2]] + [n.bbox[2] for n in notes if n.bbox[3] <= bottom + 1]) + 15
        region += 1
        is_table = caption.text.startswith("Cuadro")
        updated = []
        for block in blocks:
            # Con canal de columnas basta la columna; sin canal, solo letra pequeña (contenido de
            # gráficos y cuadros) o lo que quepa en el ancho de la leyenda y sus notas.
            within = (
                split is not None
                or block.size < 9.5
                or (left <= block.bbox[0] and block.bbox[2] <= right)
            )
            inside = (
                block.column == caption.column
                and within
                and block.kind in {"paragraph", "heading"}
                and stop <= block.bbox[1]
                and block.bbox[3] <= caption.bbox[1] + 1
            )
            if block is caption:
                block = _with(block, region=region)
            elif inside:
                kind = "table_cell" if is_table else "chart_residue"
                if not is_table and block.size >= 7.5:
                    kind = "chart_panel"  # subtítulos «A. Inflación total»
                block = _with(block, kind=kind, region=region, level=0)
            elif (
                block.kind == "source_note"
                and block.column == caption.column
                and (stop - 1 <= block.bbox[3] <= bottom + 1)
            ):
                block = _with(block, region=region)
            updated.append(block)
        blocks = updated
    return blocks, region


def _footnotes(blocks: list[Block], main_column: int) -> list[Block]:
    """Notas al pie: letra pequeña en la columna principal fuera de gráficos y cuadros."""
    result = []
    for block in blocks:
        if (
            block.kind == "paragraph"
            and block.size <= 8.5
            and block.column in {main_column, -1}
            and block.region is None
        ):
            block = _with(block, kind="footnote")
        result.append(block)
    return result


_NUMERIC = re.compile(r"^[-−–]?\d{1,3}(?:\.\d{3})*(?:,\d+)?\s*%?$")
_PAGE_REF = re.compile(r"\s\d{1,3}$")


def _mark_toc(blocks: list[Block]) -> list[Block]:
    """Índices: la mayoría de las líneas terminan en un número de página."""
    content = [b for b in blocks if b.kind not in {"running_header", "page_number"}]
    lines = [line for b in content for line in b.lines]
    with_ref = [line for line in lines if _PAGE_REF.search(line.text)]
    if len(with_ref) < 8 or len(with_ref) < 0.6 * len(lines):
        return blocks
    return [_with(b, kind="toc", level=0) if b in content else b for b in blocks]


def _mark_dense_tables(blocks: list[Block], region: int) -> tuple[list[Block], int]:
    """Tablas sin leyenda «Cuadro» (p. ej. anexos): muchas cifras en letra pequeña."""
    free = [b for b in blocks if b.region is None and b.kind in {"paragraph", "footnote"}]
    numeric = [
        line for b in free for line in b.lines if line.size <= 7.5 and _NUMERIC.match(line.text)
    ]
    if len(numeric) < 20:
        return blocks, region
    top = max(line.bbox[3] for line in numeric) + 60
    bottom = min(line.bbox[1] for line in numeric) - 12
    region += 1
    first_row = max(line.bbox[3] for line in numeric)
    headings = [
        b
        for b in blocks
        if b.kind == "heading"
        and b.bbox[1] >= first_row
        and re.match(r"(Anexo|Cuadro|Tabla)\b", b.text, re.IGNORECASE)
    ]
    # El título «Anexo/Cuadro» más cercano por encima; sin él, la región queda sin leyenda y
    # el procesamiento la trata como continuación del cuadro anterior.
    caption = min(headings, key=lambda b: b.bbox[1] - first_row, default=None)
    result = []
    for block in blocks:
        small = block.size <= 7.5 or (block.size <= 8.5 and len(block.text) <= 60)
        if block in free and small and bottom <= block.bbox[1] and block.bbox[3] <= top:
            block = _with(block, kind="table_cell", region=region)
        elif block is caption:
            block = _with(block, kind="caption", region=region, level=0)
        elif block.kind == "source_note" and block.region is None and block.bbox[3] <= top:
            block = _with(block, region=region)
        result.append(block)
    return result, region


def _mark_small_orphans(blocks: list[Block]) -> list[Block]:
    """Letra de menos de 6,5 pt fuera de gráficos y cuadros: rótulos de gráficos sin leyenda
    reconocible («dic-22», «4 6»). No es texto de lectura; se excluye y se cuenta."""
    return [
        _with(b, kind="small_text")
        if b.region is None and b.kind in {"paragraph", "footnote"} and b.size < 6.5
        else b
        for b in blocks
    ]


def _merge_footnote_markers(blocks: list[Block]) -> list[Block]:
    """Une «1» con el texto de su nota y quita la llamada pegada al final de los títulos."""
    markers = [b for b in blocks if b.kind == "footnote" and b.text.isdigit()]
    texts = [b for b in blocks if b.kind == "footnote" and not b.text.isdigit()]
    merged: dict[int, Block] = {}
    used: set[int] = set()
    for marker in markers:
        candidates = [
            t
            for t in texts
            if abs(t.bbox[3] - marker.bbox[3]) <= 3 and 0 <= t.bbox[0] - marker.bbox[2] <= 30
        ]
        if candidates:
            target = min(candidates, key=lambda t: t.bbox[0] - marker.bbox[2])
            merged[id(target)] = _with(target, text=f"{marker.text} {target.text}")
            used.add(id(marker))
    result = []
    for block in blocks:
        if id(block) in used:
            continue
        block = merged.get(id(block), block)
        call = re.search(r"(?<=[a-záéíóúñ])\d{1,2}(?:,\s?\d{1,2})*$", block.text)
        if call and (block.kind == "heading" or block.text.startswith("Recuadro")):
            # «riesgos1», «Mercado laboral6, 7»: llamadas a nota pegadas al título.
            block = _with(block, text=block.text[: call.start()])
        result.append(block)
    return result


def _order(blocks: list[Block], main_column: int) -> list[Block]:
    """Orden de lectura por altura, intercalando columna principal y barra lateral.

    Así un gráfico o cuadro de la barra queda en la sección donde aparece visualmente. Las
    notas al pie van después del contenido y las cabeceras y números de página al final.
    """

    def key(block: Block) -> tuple[int, float, int, float]:
        if block.kind in {"running_header", "page_number"}:
            rank = 9
        elif block.kind == "footnote":
            rank = 3
        else:
            rank = 1
        return rank, -block.bbox[3], 0 if block.column == main_column else 1, block.bbox[0]

    return sorted(blocks, key=key)


@dataclass(frozen=True)
class DocumentLayout:
    pages: tuple[PageLayout, ...]
    ligatures: LigatureRepair


def extract_layout(path: Path, *, max_pages: int = 400) -> DocumentLayout:
    """Devuelve las páginas con bloques clasificados, en orden de lectura aproximado."""
    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    try:
        raw_pages = []
        for page in extract_pages(path, laparams=LAParams()):
            raw_pages.append(page)
            if len(raw_pages) > max_pages:
                raise LayoutError("El PDF excede el número de páginas permitido.")
    except (PDFException, PSException, ValueError, TypeError, KeyError) as exc:
        raise LayoutError("No se pudo analizar el diseño del PDF (¿dañado o cifrado?).") from exc
    if not raw_pages:
        raise LayoutError("El PDF no tiene páginas.")
    ligatures, unresolved = _ligature_table(raw_pages)
    counts: Counter[str] = Counter()
    pages: list[PageLayout] = []
    region = 0
    for number, page in enumerate(raw_pages, start=1):
        width, height = page.width, page.height
        raw = [
            b
            for el in page
            if isinstance(el, LTTextBoxHorizontal)
            if (b := _box(el, number, width, height, ligatures, counts))
        ]
        classified, label = _classify(raw, width, height)
        split = _split_columns(
            [b for b in classified if b.kind not in {"running_header", "heading", "page_number"}],
            width,
        )
        marked, region = _mark_regions(_mark_toc(classified), split, region)
        chars = Counter(b.column for b in marked if b.kind == "paragraph" for _ in b.text)
        main_column = chars.most_common(1)[0][0] if chars else 0
        dense, region = _mark_dense_tables(_footnotes(marked, main_column), region)
        blocks = _order(_merge_footnote_markers(_mark_small_orphans(dense)), main_column)
        pages.append(PageLayout(number, label, width, height, tuple(blocks)))
    return DocumentLayout(tuple(pages), LigatureRepair(counts["repaired"], unresolved))
