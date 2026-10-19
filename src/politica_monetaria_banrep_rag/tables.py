"""Reconstrucción de cuadros a partir de líneas posicionadas (pdfminer).

Las filas se anclan en las líneas numéricas y las columnas en sus centros horizontales. Las
etiquetas de varias líneas se unen a la fila más cercana solo si están a menos de media
separación entre filas; si no, son filas de grupo («Variables exógenas»). Ante cualquier
ambigüedad (dos cifras en una celda, columna sin encabezado, fila sin etiqueta) el cuadro no se
reconstruye: se conserva su texto sin atribuir cifras a columnas.
"""

import re
import statistics
from dataclasses import dataclass, field

from politica_monetaria_banrep_rag.layout import Line

_NUMBER = re.compile(
    r"^[-−–]?\d{1,3}(?:\.\d{3})*(?:,\d+)?\s*%?(?:\s?[a-z]/)*$|^n\.?\s?[ad]\.?$|^\.$|^-$|^…$"
)
_PROJECTION_NOTE = re.compile(r"negrilla.{0,40}proyecci", re.IGNORECASE | re.DOTALL)
_MISSING_VALUE = re.compile(r"^n\.?\s*[ad]\.?$", re.I)
_HORIZON = re.compile(
    r"^(?:(?:19|20)\d{2}(?:\s*\((?:pr|proy)\)|\s+T[1-4])?|"
    r"(?:IV|III|II|I)-(?:19|20)\d{2}|"
    r"(?:ene|feb|mar|abr|may|jun|jul|ago|sep|sept|oct|nov|dic)[- ]\d{2})$",
    re.I,
)
_EXPLICIT_UNIT = re.compile(
    r"\((?:promedio mensual,\s*)?(porcentaje|puntos porcentuales|billones de pesos|"
    r"miles de millones de pesos)\)",
    re.I,
)
_ROW_UNIT = re.compile(
    r"\b(pesos por dólar|dólares por barril|puntos porcentuales|porcentaje del PIB|"
    r"miles de millones de (?:dólares|pesos)|millones de (?:dólares|pesos)|"
    r"billones de pesos|porcentaje|pesos|dólares)\b(?=\s*(?:[,;.)]|$))|(?<!\w)%(?!\w)",
    re.I,
)
_UNIT_HINT = re.compile(r"\b(puntos?|porcent\w*|billones|millones|pesos|dólares|índice)\b|%", re.I)


def _attributable_value(text: str) -> bool:
    return text not in {".", "-", "…"} and not _MISSING_VALUE.fullmatch(text)


class TableError(Exception):
    """El cuadro no se puede reconstruir sin ambigüedad; el mensaje indica el motivo."""


@dataclass(frozen=True)
class Cell:
    text: str
    bold: bool


@dataclass(frozen=True)
class Row:
    labels: tuple[str, ...]
    values: tuple[Cell | None, ...]
    group: tuple[str, ...] = ()


def _row_unit(row: Row, default: str | None) -> str | None:
    for labels in (row.labels, row.group):
        text = " ".join(labels)
        explicit = _ROW_UNIT.search(text)
        if explicit:
            if re.search(r"\b(?:miles|millones|billones)\s+de\s*$", text[: explicit.start()], re.I):
                return None  # Escala no reconocida: no atribuir solo el sufijo monetario.
            return explicit[0].lower()
        if _UNIT_HINT.search(text):
            return None
    return default


@dataclass(frozen=True)
class AttributedCell:
    row: tuple[str, ...]
    column: str
    value: str
    horizon: str | None
    unit: str | None
    projection_or_assumption: bool


@dataclass(frozen=True)
class Table:
    caption: str
    page: int
    context: str
    label_headers: tuple[str, ...]
    columns: tuple[str, ...]
    rows: tuple[Row, ...]
    notes: tuple[str, ...]
    bold_is_projection: bool

    evidence_quality: str = field(init=False)

    def __post_init__(self) -> None:
        """La relación de columnas es estructural; no verifica las cifras contra el PDF."""
        structured = (
            bool(self.columns and self.rows and all(self.columns))
            and len(set(self.columns)) == len(self.columns)
            and all(
                row.labels and any(row.labels) and len(row.values) == len(self.columns)
                for row in self.rows
            )
            and any(
                cell is not None and _attributable_value(cell.text)
                for row in self.rows
                for cell in row.values
            )
        )
        object.__setattr__(
            self, "evidence_quality", "structured_cells" if structured else "unattributed_values"
        )

    def row_text(self, row: Row) -> str:
        """Una fila con cada cifra junto a su columna; marca proyecciones si la nota lo indica."""
        name = " > ".join((*row.group, *(label for label in row.labels if label)))
        cells = []
        for column, cell in zip(self.columns, row.values, strict=True):
            if cell is None or cell.text in {".", "-", "…"}:
                continue
            mark = " (proyección o supuesto)" if self.bold_is_projection and cell.bold else ""
            cells.append(f"{column}: {cell.text}{mark}")
        return f"{name} — " + "; ".join(cells) if cells else name

    def attributed_cells(self, rows: tuple[Row, ...]) -> tuple[AttributedCell, ...]:
        """Solo celdas de las filas que el fragmentador incluyó, nunca texto de notas."""
        if self.evidence_quality != "structured_cells":
            return ()
        explicit = _EXPLICIT_UNIT.search(f"{self.caption} {self.context}")
        return tuple(
            AttributedCell(
                (*row.group, *row.labels),
                column,
                cell.text,
                column if _HORIZON.fullmatch(column) else None,
                _row_unit(row, explicit[1].lower() if explicit else None),
                self.bold_is_projection and cell.bold,
            )
            for row in rows
            for column, cell in zip(self.columns, row.values, strict=True)
            if cell is not None and _attributable_value(cell.text)
        )

    def header_text(self) -> str:
        context = f" ({self.context})" if self.context else ""
        return f"{self.caption}{context}\nColumnas: " + " | ".join(
            (*self.label_headers, *self.columns)
        )

    def to_text(self) -> str:
        lines = [self.header_text()]
        lines += [self.row_text(row) for row in self.rows]
        lines += list(self.notes)
        return "\n".join(lines)


def _cx(line: Line) -> float:
    return (line.bbox[0] + line.bbox[2]) / 2


def _cy(line: Line) -> float:
    return (line.bbox[1] + line.bbox[3]) / 2


def _clusters(values: list[float], tolerance: float) -> list[float]:
    groups: list[list[float]] = []
    for value in sorted(values):
        if groups and value - groups[-1][-1] <= tolerance:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [sum(group) / len(group) for group in groups]


def _nearest(values: list[float], target: float) -> int:
    return min(range(len(values)), key=lambda i: abs(values[i] - target))


def _split_merged_headers(cells: list[Line], column_x: list[float]) -> list[Line]:
    """«II-2026 III-2026 IV-2026» en una sola línea: se divide si cubre tantas columnas como
    palabras tiene; si no, se deja intacta (y el cuadro fallará si queda ambiguo)."""
    result = []
    for cell in cells:
        covered = [x for x in column_x if cell.bbox[0] - 2 <= x <= cell.bbox[2] + 2]
        words = cell.text.split()
        if len(covered) >= 2 and words and len(words) % len(covered) == 0:
            size = len(words) // len(covered)  # «dic 24 mar 25» → «dic 24», «mar 25»
            parts = [" ".join(words[i : i + size]) for i in range(0, len(words), size)]
            result += [Line(w, (x - 1, cell.bbox[1], x + 1, cell.bbox[3]), cell.size, cell.bold)
                       for w, x in zip(parts, covered, strict=True)]  # fmt: skip
        else:
            result.append(cell)
    return result


def _assign_spans(column_x: list[float], span_x: list[float]) -> list[int]:
    """Reparte columnas contiguas entre encabezados que abarcan varias (años sobre trimestres).

    Cada encabezado recibe un bloque contiguo cuyo centro medio queda lo más cerca posible
    del centro del encabezado; evita los empates del criterio «el más cercano».
    """
    order = sorted(range(len(span_x)), key=lambda i: span_x[i])
    centers = [span_x[i] for i in order]
    n, k = len(column_x), len(centers)
    if k > n:
        raise TableError("más encabezados agrupadores que columnas")
    inf = float("inf")
    cost = [[inf] * (k + 1) for _ in range(n + 1)]
    back = [[0] * (k + 1) for _ in range(n + 1)]
    cost[0][0] = 0.0
    for j in range(1, k + 1):
        for i in range(j, n + 1):
            for start in range(j - 1, i):
                group = column_x[start:i]
                value = cost[start][j - 1] + abs(sum(group) / len(group) - centers[j - 1])
                if value < cost[i][j]:
                    cost[i][j], back[i][j] = value, start
    owners = [0] * n
    i = n
    for j in range(k, 0, -1):
        start = back[i][j]
        for index in range(start, i):
            owners[index] = order[j - 1]
        i = start
    return owners


def _header_bands(lines: list[Line]) -> list[float]:
    """Alturas donde empieza una subtabla: filas de encabezados debajo de filas con cifras."""
    numbers = [line for line in lines if _NUMBER.match(line.text)]
    first_x = min(n.bbox[0] for n in numbers)
    top = max(n.bbox[3] for n in numbers)
    candidates = [
        line
        for line in lines
        if not _NUMBER.match(line.text) and line.bbox[0] >= first_x - 4 and line.bbox[3] < top
    ]
    bands = []
    for level in _clusters([_cy(c) for c in candidates], 1.6):
        row = [c for c in candidates if abs(_cy(c) - level) <= 1.6]
        if len(row) >= 2 or sum(len(c.text.split()) for c in row) >= 2:
            bands.append(max(c.bbox[3] for c in row))
    return sorted(bands, reverse=True)


def _merge_contained(lines: list[Line]) -> list[Line]:
    """«abr 25» con un «-» dibujado aparte dentro de su caja: se recompone «abr-25»."""
    result = list(lines)
    for piece in lines:
        if len(piece.text) > 2 or any(ch.isalnum() for ch in piece.text):
            continue
        hosts = [
            other
            for other in result
            if other is not piece
            and abs(_cy(other) - _cy(piece)) <= 1.6
            and other.bbox[0] <= piece.bbox[0]
            and piece.bbox[2] <= other.bbox[2]
        ]
        if len(hosts) == 1:
            host = hosts[0]
            text = host.text.replace(" ", piece.text, 1) if host.text.count(" ") == 1 else host.text
            result[result.index(host)] = Line(text, host.bbox, host.size, host.bold)
            result.remove(piece)
    return result


def build_tables(caption: str, page: int, lines: list[Line], notes: list[str]) -> list[Table]:
    """Divide en subtablas apiladas con encabezados propios y reconstruye cada una."""
    lines = _merge_contained(lines)
    numbers = [line for line in lines if _NUMBER.match(line.text)]
    if len(numbers) < 2:
        raise TableError("menos de dos cifras")
    parts: list[list[Line]] = []
    remaining = list(lines)
    for band in _header_bands(lines):
        upper = [line for line in remaining if _cy(line) > band + 0.5]
        if any(_NUMBER.match(line.text) for line in upper):
            parts.append(upper)
            remaining = [line for line in remaining if line not in upper]
    parts.append(remaining)
    tables = []
    for index, part in enumerate(parts, start=1):
        title = caption if len(parts) == 1 else f"{caption} (parte {index} de {len(parts)})"
        tables.append(build_table(title, page, part, notes))
    return tables


def build_table(caption: str, page: int, lines: list[Line], notes: list[str]) -> Table:
    """Devuelve el cuadro o lanza ``TableError`` si alguna cifra no se puede asignar."""
    numbers = [line for line in lines if _NUMBER.match(line.text)]
    if len(numbers) < 2:
        raise TableError("menos de dos cifras")
    column_x = _clusters([_cx(n) for n in numbers], 6.0)
    row_y = sorted(_clusters([_cy(n) for n in numbers], 1.6), reverse=True)
    first_x = min(n.bbox[0] for n in numbers)
    top = max(n.bbox[3] for n in numbers)
    others = [line for line in lines if line not in numbers]
    headers = [line for line in others if line.bbox[1] >= top - 0.5]
    labels = [line for line in others if line not in headers and line.bbox[2] <= first_x + 2]
    if [line for line in others if line not in headers and line not in labels]:
        raise TableError("texto suelto entre las cifras")
    grid: list[list[Cell | None]] = [[None] * len(column_x) for _ in row_y]
    for line in numbers:
        row, col = _nearest(row_y, _cy(line)), _nearest(column_x, _cx(line))
        if abs(row_y[row] - _cy(line)) > 1.6 or abs(column_x[col] - _cx(line)) > 9:
            raise TableError("cifra fuera de la cuadrícula")
        if grid[row][col] is not None:
            raise TableError("dos cifras en una misma celda")
        text = re.sub(
            r"(?<=[\d%])\s?(?=[a-z]/)", " ", line.text.replace("−", "-").replace("–", "-")
        )
        grid[row][col] = Cell(text, line.bold)
    # Encabezados por niveles de altura. Un nivel con casi todas las columnas las nombra; uno
    # con varias celdas las agrupa (años sobre trimestres); una sola celda es un título.
    direct = [""] * len(column_x)
    spans: list[tuple[float, str]] = []
    context: list[str] = []
    band_bottom = float("inf")
    for level in sorted(_clusters([_cy(h) for h in headers], 1.6), reverse=True):
        aligned = sorted(
            (h for h in headers if abs(_cy(h) - level) <= 1.6 and _cx(h) >= first_x - 4),
            key=lambda h: h.bbox[0],
        )
        if not aligned:
            continue
        aligned = _split_merged_headers(aligned, column_x)
        band_bottom = min(band_bottom, min(h.bbox[1] for h in aligned))
        if len(aligned) >= len(column_x) / 2:
            for header in aligned:
                col = _nearest(column_x, _cx(header))
                direct[col] = f"{direct[col]} {header.text}".strip()
        elif len(aligned) == 1:
            context.append(aligned[0].text)
        else:
            spans += [(_cx(h), h.text) for h in aligned]
    if band_bottom == float("inf"):
        band_bottom = top
    owners = _assign_spans(column_x, [sx for sx, _ in spans]) if spans else []
    columns = [
        f"{spans[owners[index]][1] if spans else ''} {direct[index]}".strip()
        for index in range(len(column_x))
    ]
    if not all(columns):
        raise TableError(f"{columns.count('')} columnas sin encabezado")
    if len(set(columns)) != len(columns):
        repeated = sorted({c for c in columns if columns.count(c) > 1})
        raise TableError("columnas repetidas: " + ", ".join(repeated[:4]))
    label_headers: dict[float, str] = {}
    for header in headers:
        if _cx(header) < first_x - 4:
            if _cy(header) >= band_bottom - 1:
                label_headers[header.bbox[0]] = header.text
            else:
                labels.append(header)  # bajo la banda de encabezados: es una fila de grupo
    # Etiquetas: cada línea va a su fila más cercana. Se acepta primero la más próxima y luego
    # las que no se alejen más de 0,75 alturas de línea de las ya aceptadas (etiquetas de
    # varias líneas); las demás son filas de grupo («Variables exógenas», «Internas»).
    label_x = _clusters([line.bbox[0] for line in labels], 12.0)
    height = statistics.median(line.bbox[3] - line.bbox[1] for line in labels) if labels else 7.0
    by_row: dict[int, list[tuple[float, Line]]] = {}
    for line in labels:
        row = _nearest(row_y, _cy(line))
        by_row.setdefault(row, []).append((abs(row_y[row] - _cy(line)), line))
    row_labels: list[list[list[Line]]] = [[[] for _ in label_x] for _ in row_y]
    groups: list[tuple[float, float, str]] = []
    for row, candidates in by_row.items():
        accepted: list[float] = []
        for distance, line in sorted(candidates, key=lambda item: item[0]):
            limit = (max(accepted) if accepted else 0.25 * height) + 0.75 * height
            if distance <= limit:
                accepted.append(distance)
                row_labels[row][_nearest(label_x, line.bbox[0])].append(line)
            else:
                groups.append((_cy(line), line.bbox[0], line.text))
    rows: list[Row] = []
    for index, y in enumerate(row_y):
        names = tuple(
            " ".join(part.text for part in sorted(slot, key=lambda line: -_cy(line)))
            for slot in row_labels[index]
        )
        if not names or not names[0]:
            raise TableError("fila sin etiqueta")
        path: list[tuple[float, str]] = []
        for gy, gx, text in sorted(groups, key=lambda g: -g[0]):
            if gy > y:
                path = [(px, pt) for px, pt in path if px < gx] + [(gx, text)]
        rows.append(Row(names, tuple(grid[index]), tuple(text for _, text in path)))
    return Table(
        caption=caption,
        page=page,
        context=" ".join(context),
        label_headers=tuple(label_headers[x] for x in sorted(label_headers)) or ("Concepto",),
        columns=tuple(columns),
        rows=tuple(rows),
        notes=tuple(notes),
        bold_is_projection=any(_PROJECTION_NOTE.search(note) for note in notes),
    )
