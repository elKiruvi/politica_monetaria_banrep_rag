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
_PERIOD_MENTION = re.compile(
    r"\b(enero|abril|julio|octubre)(?:\s+(?:(?:de|del)\s+)?(\d{4}))?\b", re.IGNORECASE
)
_ISO_PERIOD = re.compile(r"\b(\d{4})-(01|04|07|10)\b")
MAX_INDICATOR_CHARS = 500
_MAX_TERMS = 6
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
# Palabras de la pregunta que no describen el indicador (no se aplican a --indicador).
_IGNORED = frozenset(
    """a al como cómo comparar compara comparacion cambiaron cambio cambios cambió cual cuales
    de del desde hasta frente versus vs con contra diferencia diferencias el en entre informe
    informes ipm la las lo los para por pronostico pronosticos proyeccion proyecciones proyectado
    proyectada que se segun sobre su sus un una y o evolucion evoluciono paso ocurrio vario
    variacion banco republica""".split()
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
    """Informes nombrados por mes de publicación y año («entre enero y julio de 2025», «enero
    2025», «2025-01»). Un mes sin año toma el siguiente año explícito. Devuelve (``report_id``
    indexados, periodos AAAA-MM mencionados que no están en el índice)."""
    mentions = [
        (m.start(), _MONTHS[m.group(1).lower()], m.group(2))
        for m in _PERIOD_MENTION.finditer(question)
    ]
    mentions += [(m.start(), m.group(2), m.group(1)) for m in _ISO_PERIOD.finditer(question)]
    mentions.sort()
    found: list[str] = []
    missing: list[str] = []
    for index, (_, month, year) in enumerate(mentions):
        year = year or next((y for _, _, y in mentions[index + 1 :] if y), None)
        if not year:
            continue
        period = f"{year}-{month}"
        (found if period in available else missing).append(available.get(period, period))
    return tuple(dict.fromkeys(found)), tuple(dict.fromkeys(missing))


def indicator_terms(text: str, *, from_question: bool = True) -> list[tuple[str, ...]]:
    """Términos del indicador con sinónimos del dominio. Las palabras de función de una pregunta
    se ignoran; un ``--indicador`` explícito se respeta completo."""
    if len(text) > MAX_INDICATOR_CHARS:
        raise ValueError(f"El indicador o la pregunta supera {MAX_INDICATOR_CHARS} caracteres.")
    stripped = _ISO_PERIOD.sub(" ", _PERIOD_MENTION.sub(" ", text)) if from_question else text
    words = re.findall(r"\w+", fold(stripped))
    stop = {fold(word) for word in _IGNORED} if from_question else {"de", "del", "la", "el", "y"}
    terms = [
        _SYNONYMS.get(word, (word,))
        for word in dict.fromkeys(words)
        if word not in stop and not word.isdigit() and len(word) > 1
    ]
    return terms[:_MAX_TERMS]


def choose_terms(rows: Iterable[str], terms: list[tuple[str, ...]]) -> tuple[str, ...] | None:
    """Primera combinación de opciones (por especificidad) que coincide con alguna fila."""
    folded = [fold(row) for row in rows]
    # Como mucho 6 términos de 3 opciones: 729 combinaciones.
    combos = sorted(product(*(range(len(options)) for options in terms)), key=sum)
    for combo in combos[:256]:
        chosen = tuple(options[i] for options, i in zip(terms, combo, strict=True))
        if any(all(option in row for option in chosen) for row in folded):
            return chosen
    return None


def _difference_unit(unit: str | None) -> str:
    if unit is None:
        return "unidad no declarada en la celda"
    if unit == "%" or "porcentaje" in unit or "porcentual" in unit:
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
    explicit_indicator: bool = False,
) -> Comparison:
    """Compara un indicador entre informes; con ``only_projections`` (por defecto) omite las
    filas en las que ningún informe marca proyección, como la historia observada."""
    if len(report_ids) < 2:
        raise ValueError("Se necesitan al menos dos informes para comparar.")
    missing = [report_id for report_id in report_ids if report_id not in reports]
    if missing:
        raise ValueError(f"Informes no indexados: {', '.join(missing)}.")
    terms = indicator_terms(indicator, from_question=not explicit_indicator)
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
    ambiguous: set[tuple[str, str, str]] = set()
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
        previous = slot[position[chunk.report_id]]
        if previous is not None:
            # Misma clave en el mismo informe: repetición idéntica o filas distintas con la misma
            # ruta (p. ej. un grupo padre perdido); en el segundo caso la fila es ambigua.
            if previous.value != value:
                ambiguous.add(key)
            continue
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
            # Negrilla según la nota del cuadro, o «(proy)» en el encabezado de la columna.
            cell.projection_or_assumption or "proy" in fold(cell.column),
            chunk.page_start,
            chunk.page_end,
            chunk.page_labels,
            chunk.chunk_id,
            chunk.table_id,
        )
    rows = []
    hidden = 0
    for key, values in grouped.items():
        present = [v for v in values if v is not None]
        if len(present) < 2:
            continue  # sin contraparte en otro informe
        if only_projections and not any(v.projection for v in present):
            hidden += 1
            continue
        caption, row_text = labels[key]
        units = {v.unit for v in present}
        notes = []
        comparable = len(units) == 1 and key not in ambiguous
        if key in ambiguous:
            notes.append(
                "Fila ambigua: el mismo informe trae valores distintos para esta ruta del cuadro "
                "(posible grupo no identificado); no se compara."
            )
        if len(units) > 1:
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
    if hidden:
        warnings.append(
            f"Se omitieron {hidden} fila(s) sin cifras marcadas como proyección (negrilla o "
            "«proy»); inclúyalas con la opción de filas sin proyecciones (--incluir-observados)."
        )
    if not chosen:
        warnings.append(
            "Ninguna fila coincide con los términos "
            + " + ".join("/".join(options) for options in terms)
            + "; precise el indicador con --indicador."
        )
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
        interpretation=tuple(dict.fromkeys(chosen)) or tuple("/".join(o) for o in terms),
        truncated=len(rows) > MAX_ROWS,
        warnings=tuple(warnings),
    )
