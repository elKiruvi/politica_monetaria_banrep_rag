"""Comparaciones entre informes calculadas en Python, solo con celdas atribuidas.

El LLM no interviene en los números. Dos cifras se comparan solo si comparten cuadro (mismo
título normalizado), fila, columna (horizonte) y unidad; se distingue proyección de dato
observado y se cita cada valor con su informe y página. Los cuadros sin estructura
(``table_raw``) y los gráficos nunca aportan cifras.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from itertools import product

from politica_monetaria_banrep_rag.retrieval import IndexedChunk, ReportMeta, fold
from politica_monetaria_banrep_rag.tables import AttributedCell

MAX_ROWS = 40
_MONTHS = {"enero": "01", "abril": "04", "julio": "07", "octubre": "10"}
# Llamadas de nota pegadas o separadas: «IPC totald/», «Balanza de pagos i/j/», «a/, b/».
_MARKERS = re.compile(r"(?:\s*,?\s*[a-z]/)+$")
_CAPTION_NOISE = re.compile(
    r"\((?:años|continuación|parte \d+ de \d+)\)|\bcontinuación\b", re.IGNORECASE
)
_VALUE = re.compile(r"^([-−–]?)(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d+))?(?:\s*(?:[a-z]/)+)?$")
_PERIOD_MENTION = re.compile(r"\b(enero|abril|julio|octubre)(?:\s+(?:de|del)\s+(\d{4}))?\b", re.I)
# Opciones por término, de la más específica a la más general; se usa la primera combinación
# que coincide con alguna fila de los cuadros elegidos.
_SYNONYMS = {
    "inflacion": ("ipc total", "ipc", "inflacion"),
    "crecimiento": ("producto interno bruto", "crecimiento"),
    "pib": ("producto interno bruto", "pib"),
    "producto": ("producto interno bruto", "producto"),
    "tpm": ("tasa de interes de politica", "tpm"),
    "petroleo": ("petroleo", "brent"),
    "desempleo": ("tasa de desempleo", "desempleo"),
}
_IGNORED = frozenset(
    """a al como comparar compara comparacion cambiaron cambio cambios cual cuales de del
    diferencia diferencias el en entre informe informes ipm la las lo los para por proyeccion
    proyecciones que se segun sobre su sus un una y o evolucion""".split()
)


def _clean_label(text: str) -> str:
    return re.sub(r"\s+", " ", _MARKERS.sub("", text.strip())).strip()


def table_key(chunk: IndexedChunk) -> str:
    """Título del cuadro sin «(continuación)», «(Años)», partes ni llamadas de nota."""
    caption = chunk.text.split("\n", 1)[0]
    return _clean_label(re.sub(r"\s+", " ", _CAPTION_NOISE.sub(" ", caption)))


def parse_value(text: str) -> Decimal | None:
    """«4,7» → 4.7; «-13.451» → -13451; «4,80 c/» → 4.80; «n. d.» → None."""
    match = _VALUE.match(text.strip())
    if not match:
        return None
    sign, whole, decimals = match.groups()
    try:
        number = Decimal(whole.replace(".", "") + (f".{decimals}" if decimals else ""))
    except InvalidOperation:
        return None
    return -number if sign else number


@dataclass(frozen=True)
class CellValue:
    report_id: str
    reference_period: str
    report_title: str
    source_url: str
    table: str
    row: tuple[str, ...]
    column: str
    unit: str | None
    value_text: str
    value: Decimal
    projection: bool
    page_start: int
    page_end: int
    page_labels: tuple[str, ...]
    chunk_id: str
    table_id: str | None


@dataclass(frozen=True)
class Difference:
    from_report: str
    to_report: str
    value: Decimal  # posterior − anterior, con la precisión de la cifra más precisa
    # revision_de_proyeccion | proyeccion_vs_observado | observado_vs_proyeccion | observados
    kind: str


def _kind(before: CellValue, after: CellValue) -> str:
    return {
        (True, True): "revision_de_proyeccion",
        (True, False): "proyeccion_vs_observado",
        (False, True): "observado_vs_proyeccion",
        (False, False): "observados",
    }[(before.projection, after.projection)]


def _places(cell: CellValue) -> int:
    exponent = cell.value.as_tuple().exponent
    return -exponent if isinstance(exponent, int) else 0


@dataclass(frozen=True)
class ComparisonRow:
    table: str
    indicator: str
    horizon: str
    values: tuple[CellValue | None, ...]  # en el orden de los informes comparados
    comparable: bool
    unit: str | None
    differences: tuple["Difference", ...] = ()
    difference_unit: str = ""
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Comparison:
    reports: tuple[ReportMeta, ...]
    indicator: str
    horizon: str | None
    rows: tuple[ComparisonRow, ...]
    interpretation: tuple[str, ...] = ()  # términos con los que se buscaron las filas
    truncated: bool = False
    warnings: tuple[str, ...] = field(default=())


def detect_periods(
    question: str, available: dict[str, str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Informes nombrados por mes de publicación y año: «entre enero y julio de 2025» → dos.

    Un mes sin año toma el siguiente año explícito. Devuelve (``report_id`` indexados,
    periodos AAAA-MM mencionados que no están en el índice)."""
    mentions = [(m.group(1).lower(), m.group(2)) for m in _PERIOD_MENTION.finditer(question)]
    found: list[str] = []
    missing: list[str] = []
    for index, (month, year) in enumerate(mentions):
        year = year or next((y for _, y in mentions[index + 1 :] if y), None)
        if not year:
            continue
        period = f"{year}-{_MONTHS[month]}"
        (found if period in available else missing).append(available.get(period, period))
    return tuple(dict.fromkeys(found)), tuple(dict.fromkeys(missing))


def indicator_terms(text: str) -> list[tuple[str, ...]]:
    """Términos del indicador con sinónimos del dominio; se ignoran palabras de la pregunta."""
    words = re.findall(r"\w+", fold(_PERIOD_MENTION.sub(" ", text)))
    return [
        _SYNONYMS.get(word, (word,))
        for word in words
        if word not in _IGNORED and not word.isdigit() and len(word) > 1
    ]


def choose_terms(rows: Iterable[str], terms: list[tuple[str, ...]]) -> tuple[str, ...] | None:
    """Primera combinación de opciones (por especificidad) que coincide con alguna fila."""
    folded = [fold(row) for row in rows]
    combos = sorted(product(*(range(len(options)) for options in terms)), key=sum)
    for combo in combos[:256]:
        chosen = tuple(options[i] for options, i in zip(terms, combo, strict=True))
        if any(all(option in row for option in chosen) for row in folded):
            return chosen
    return None


def _difference_unit(unit: str | None) -> str:
    if unit is None:
        return "unidad no declarada en la celda"
    if unit in {"porcentaje", "%"} or "porcentaje" in unit:
        return "puntos porcentuales"
    return unit


def _round(value: Decimal, *operands: CellValue) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-max(_places(o) for o in operands)))


def compare(
    chunks: Iterable[IndexedChunk],
    reports: dict[str, ReportMeta],
    report_ids: tuple[str, ...],
    indicator: str,
    *,
    horizon: str | None = None,
    table: str | None = None,
    only_projections: bool = True,
) -> Comparison:
    """Compara un indicador entre informes; con ``only_projections`` (por defecto) omite las
    filas en las que ningún informe marca proyección, como la historia observada."""
    if len(report_ids) < 2:
        raise ValueError("Se necesitan al menos dos informes para comparar.")
    missing = [report_id for report_id in report_ids if report_id not in reports]
    if missing:
        raise ValueError(f"Informes no indexados: {', '.join(missing)}.")
    terms = indicator_terms(indicator)
    if not terms:
        raise ValueError("Indique el indicador a comparar (por ejemplo, «IPC total»).")
    ordered = tuple(sorted((reports[r] for r in report_ids), key=lambda r: r.reference_period))
    position = {report.report_id: i for i, report in enumerate(ordered)}
    candidates: list[tuple[IndexedChunk, str, str, AttributedCell]] = []
    for chunk in chunks:
        if (
            chunk.report_id not in position
            or chunk.kind != "table"
            or chunk.evidence_quality != "structured_cells"
        ):
            continue
        caption = table_key(chunk)
        if table and fold(table) not in fold(caption):
            continue
        for cell in chunk.attributed_cells:
            if horizon and fold(cell.column) != fold(horizon):
                continue
            row_text = " > ".join(_clean_label(label) for label in cell.row)
            candidates.append((chunk, caption, row_text, cell))
    chosen = choose_terms({row for _, _, row, _ in candidates}, terms) or ()
    grouped: dict[tuple[str, str, str], list[CellValue | None]] = {}
    labels: dict[tuple[str, str, str], tuple[str, str]] = {}
    for chunk, caption, row_text, cell in candidates if chosen else ():
        if not all(option in fold(row_text) for option in chosen):
            continue
        value = parse_value(cell.value)
        if value is None:
            continue
        meta = reports[chunk.report_id]
        key = (fold(caption), fold(row_text), fold(cell.column))
        labels.setdefault(key, (caption, row_text))
        slot = grouped.setdefault(key, [None] * len(ordered))
        if slot[position[chunk.report_id]] is not None:
            continue  # la misma celda repetida en otro fragmento del mismo cuadro
        slot[position[chunk.report_id]] = CellValue(
            meta.report_id,
            meta.reference_period,
            meta.title,
            meta.source_url,
            caption,
            tuple(_clean_label(label) for label in cell.row),
            cell.column,
            cell.unit,
            cell.value,
            value,
            cell.projection_or_assumption,
            chunk.page_start,
            chunk.page_end,
            chunk.page_labels,
            chunk.chunk_id,
            chunk.table_id,
        )
    rows = []
    for key, values in grouped.items():
        present = [v for v in values if v is not None]
        if len(present) < 2:
            continue  # sin contraparte en otro informe
        if only_projections and not any(v.projection for v in present):
            continue
        caption, row_text = labels[key]
        units = {v.unit for v in present}
        notes = []
        comparable = len(units) == 1
        if not comparable:
            notes.append(
                f"Unidades distintas ({', '.join(sorted(map(str, units)))}): no se comparan."
            )
        kinds = {v.projection for v in present}
        if len(kinds) > 1:
            notes.append(
                "Mezcla proyecciones y datos observados: la diferencia es una revisión frente "
                "al dato observado, no entre proyecciones."
            )
        elif kinds == {True}:
            notes.append("Revisión entre proyecciones (o supuestos) del mismo horizonte.")
        else:
            notes.append(
                "Ninguna celda está marcada como proyección: puede tratarse de datos observados "
                "o de un cuadro que no marca proyecciones."
            )
        if len(present) < len(values):
            notes.append("Falta el valor en algún informe.")
        if len({_places(v) for v in present}) > 1:
            notes.append(
                "Los informes publican la cifra con distinta precisión: parte de la diferencia "
                "puede deberse al redondeo."
            )
        differences: list[Difference] = []
        if comparable:
            for before, after in zip(present, present[1:], strict=False):
                differences.append(
                    Difference(
                        before.report_id,
                        after.report_id,
                        _round(after.value - before.value, before, after),
                        _kind(before, after),
                    )
                )
        rows.append(
            ComparisonRow(
                caption,
                row_text,
                present[0].column,
                tuple(values),
                comparable,
                present[0].unit if comparable else None,
                tuple(differences),
                _difference_unit(present[0].unit) if comparable else "",
                tuple(notes),
            )
        )
    rows.sort(key=lambda r: (r.table, r.indicator, r.horizon))
    warnings = []
    if not rows:
        warnings.append(
            "No hay celdas atribuidas comparables para ese indicador en los informes elegidos. "
            "Los cuadros sin estructura y los gráficos no aportan cifras."
        )
    return Comparison(
        ordered,
        indicator,
        horizon,
        tuple(rows[:MAX_ROWS]),
        interpretation=tuple(dict.fromkeys(chosen)),
        truncated=len(rows) > MAX_ROWS,
        warnings=tuple(warnings),
    )
