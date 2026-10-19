"""Fragmentos para recuperación: nunca mezclan informes ni secciones y conservan sus páginas.

Los párrafos de una misma sección y página se agrupan hasta ``max_chars``; los largos se cortan
por oraciones o palabras completas. Cada cuadro se fragmenta por filas con título y columnas;
si una fila no cabe, el procesamiento falla antes de publicar fragmentos sin atribución.
"""

import hashlib
import re
from dataclasses import asdict, dataclass

from politica_monetaria_banrep_rag.processing import ProcessedDocument, Unit
from politica_monetaria_banrep_rag.tables import AttributedCell, Row

CHUNKER_VERSION = "secciones-4"
_NOTES_IN_TABLE = 400
# Corte de oración: fin de oración y mayúscula; no corta «p. ej.», «EE. UU.» ni «3,5 %».
_SENTENCE = re.compile(r"(?<=[^\s.]{3}[.!?])\s+(?=[A-ZÁÉÍÓÚÑ¿¡(«])")
_NUMBER_UNIT = re.compile(
    r"(?<!\w)[-−–]?\d[\d.,]*\s+(?:%(?:\s+del\s+PIB)?|"
    r"(?:miles\s+de\s+)?millones\s+de\s+(?:dólares|pesos)\b|"
    r"billones\s+de\s+pesos\b|puntos\s+porcentuales\b|"
    r"porcentaje(?:\s+del\s+PIB)?\b|pesos(?:\s+por\s+dólar)?\b|"
    r"dólares(?:\s+por\s+barril)?\b)",
    re.I,
)


class ChunkingError(ValueError):
    """A unit cannot fit within the limit without losing a word or an attributed row."""


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    report_id: str
    kind: str
    section: tuple[str, ...]
    page_start: int
    page_end: int
    page_labels: tuple[str, ...]
    text: str
    evidence_quality: str
    attributed_cells: tuple[AttributedCell, ...] = ()
    table_id: str | None = None  # Shared by the table's rows and its full notes.

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _split_long(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE.split(text):
        if current and len(current) + 1 + len(sentence) > max_chars:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    # Keep whole words: cutting inside an unbroken value could detach its unit.
    result: list[str] = []
    for piece in pieces:
        while len(piece) > max_chars:
            cut = piece.rfind(" ", 0, max_chars + 1)
            for match in _NUMBER_UNIT.finditer(piece):
                if match.start() < cut < match.end():
                    cut = piece.rfind(" ", 0, match.start())
                    break
            if cut <= 0:
                raise ChunkingError(f"Unbreakable token exceeds max_chars={max_chars}")
            result.append(piece[:cut])
            piece = piece[cut:].strip()
        result.append(piece)
    return result


def _table_parts(unit: Unit, max_chars: int) -> tuple[list[tuple[str, tuple[Row, ...]]], bool]:
    table = unit.table
    assert table is not None
    header = table.header_text()
    full_notes = " ".join(table.notes)
    # Never show a clipped note value without its scale or qualifier.
    excerpt = "(notas completas aparte)" if len(full_notes) > _NOTES_IN_TABLE else full_notes
    lines = [table.row_text(row) for row in table.rows]
    for line in lines:
        required = len(f"{header}\n{line}")
        if required > max_chars:
            raise ChunkingError(
                f"Table row on page {unit.page} requires {required} chars; max_chars={max_chars}"
            )
    excerpt_fits = not excerpt or (
        len(f"{header}\n{excerpt}") <= max_chars
        and all(len("\n".join((header, line, excerpt))) <= max_chars for line in lines)
    )
    notes = excerpt if excerpt_fits else ""
    separate_notes = bool(full_notes) and (len(full_notes) > _NOTES_IN_TABLE or not notes)
    parts: list[tuple[str, tuple[Row, ...]]] = []
    rows: list[str] = []
    included: list[Row] = []

    def content() -> str:
        return "\n".join([header, *rows, *([notes] if notes else [])])

    for row, line in zip(table.rows, lines, strict=True):
        candidate = "\n".join([header, *rows, line, *([notes] if notes else [])])
        if rows and len(candidate) > max_chars:
            parts.append((content(), tuple(included)))
            rows = []
            included = []
        rows.append(line)
        included.append(row)
    if len(content()) > max_chars:
        raise ChunkingError(f"Table header on page {unit.page} exceeds max_chars={max_chars}")
    parts.append((content(), tuple(included)))
    return parts, separate_notes


def chunk_document(document: ProcessedDocument, *, max_chars: int = 1400) -> list[Chunk]:
    """Divide el documento; ``chunk_id`` es estable para el mismo PDF y las mismas versiones."""
    if not 300 <= max_chars <= 8000:
        raise ValueError("max_chars debe estar entre 300 y 8000.")
    drafts: list[tuple[str, tuple[str, ...], list[Unit], str, tuple[Row, ...], str | None]] = []
    buffer: list[Unit] = []

    def flush() -> None:
        if buffer:
            text = "\n\n".join(unit.text for unit in buffer)
            drafts.append(("paragraph", buffer[0].section, list(buffer), text, (), None))
            buffer.clear()

    for unit_index, unit in enumerate(document.units):
        if unit.kind == "paragraph":
            if buffer and (
                buffer[0].section != unit.section
                or buffer[-1].page != unit.page
                or sum(len(u.text) + 2 for u in buffer) + len(unit.text) > max_chars
            ):
                flush()
            if len(unit.text) > max_chars:
                flush()
                for piece in _split_long(unit.text, max_chars):
                    drafts.append(("paragraph", unit.section, [unit], piece, (), None))
            else:
                buffer.append(unit)
            continue
        flush()
        if unit.kind == "table":
            table_id = hashlib.sha256(
                f"{document.report_id}|{document.sha256}|{document.version}|"
                f"{CHUNKER_VERSION}|table|{unit_index}".encode()
            ).hexdigest()[:32]
            parts, separate_notes = _table_parts(unit, max_chars)
            for part, rows in parts:
                drafts.append(("table", unit.section, [unit], part, rows, table_id))
            assert unit.table is not None
            if separate_notes:
                # Full notes carry the same table identity, including when the excerpt cannot fit.
                prefix = f"{unit.table.caption} — notas: "
                if len(prefix) >= max_chars:
                    raise ChunkingError(
                        f"Table note prefix on page {unit.page} exceeds max_chars={max_chars}"
                    )
                for piece in _split_long(" ".join(unit.table.notes), max_chars - len(prefix)):
                    drafts.append(
                        ("table_notes", unit.section, [unit], prefix + piece, (), table_id)
                    )
        else:
            for piece in _split_long(unit.text, max_chars):
                drafts.append((unit.kind, unit.section, [unit], piece, (), None))
    flush()
    chunks = []
    for index, (kind, section, units, text, rows, source_table_id) in enumerate(drafts):
        identity = "|".join(
            [
                document.report_id,
                document.sha256,
                document.version,
                CHUNKER_VERSION,
                str(index),
                text,
            ]
        )
        labels = tuple(
            dict.fromkeys(
                label for u in units for label in (u.page_label, u.page_end_label) if label
            )
        )
        cells = units[0].table.attributed_cells(rows) if kind == "table" and units[0].table else ()
        chunks.append(
            Chunk(
                chunk_id=hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32],
                report_id=document.report_id,
                kind=kind,
                section=section,
                page_start=min(u.page for u in units),
                page_end=max(u.page_end or u.page for u in units),
                page_labels=labels,
                text=text,
                evidence_quality=(
                    "note_only"
                    if kind == "table_notes"
                    else "unattributed_values"
                    if kind == "table" and not cells
                    else units[0].evidence_quality
                ),
                attributed_cells=cells,
                table_id=source_table_id,
            )
        )
    return chunks
