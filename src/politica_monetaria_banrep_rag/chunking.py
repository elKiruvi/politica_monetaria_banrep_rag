"""Fragmentos para recuperación: nunca mezclan informes ni secciones y conservan sus páginas.

Los párrafos de una misma sección se agrupan hasta ``max_chars``; un párrafo más largo se corta
en límites de oración sin separar cifras de su unidad. Cada cuadro se fragmenta por filas y
cada fragmento repite su título y columnas, para que una cifra nunca quede sin su encabezado.
"""

import hashlib
import re
from dataclasses import asdict, dataclass

from politica_monetaria_banrep_rag.processing import ProcessedDocument, Unit

CHUNKER_VERSION = "secciones-2"
_NOTES_IN_TABLE = 400
# Corte de oración: fin de oración y mayúscula; no corta «p. ej.», «EE. UU.» ni «3,5 %».
_SENTENCE = re.compile(r"(?<=[^\s.]{3}[.!?])\s+(?=[A-ZÁÉÍÓÚÑ¿¡(«])")


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
    # Una sola oración enorme se corta por palabras como último recurso.
    result = []
    for piece in pieces:
        while len(piece) > max_chars:
            cut = piece.rfind(" ", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            result.append(piece[:cut])
            piece = piece[cut:].strip()
        result.append(piece)
    return result


def _table_parts(unit: Unit, max_chars: int) -> list[str]:
    table = unit.table
    assert table is not None
    header = table.header_text()
    notes = " ".join(table.notes)
    if len(notes) > _NOTES_IN_TABLE:
        notes = notes[:_NOTES_IN_TABLE].rsplit(" ", 1)[0] + " […] (notas completas aparte)"
    parts: list[str] = []
    rows: list[str] = []
    for row in table.rows:
        line = table.row_text(row)
        candidate = "\n".join([header, *rows, line])
        if rows and len(candidate) + len(notes) > max_chars:
            parts.append("\n".join([header, *rows, notes]).strip())
            rows = []
        rows.append(line)
    parts.append("\n".join([header, *rows, notes]).strip())
    return parts


def chunk_document(document: ProcessedDocument, *, max_chars: int = 1400) -> list[Chunk]:
    """Divide el documento; ``chunk_id`` es estable para el mismo PDF y las mismas versiones."""
    if not 300 <= max_chars <= 8000:
        raise ValueError("max_chars debe estar entre 300 y 8000.")
    drafts: list[tuple[str, tuple[str, ...], list[Unit], str]] = []
    buffer: list[Unit] = []

    def flush() -> None:
        if buffer:
            text = "\n\n".join(unit.text for unit in buffer)
            drafts.append(("paragraph", buffer[0].section, list(buffer), text))
            buffer.clear()

    for unit in document.units:
        if unit.kind == "paragraph":
            if buffer and (
                buffer[0].section != unit.section
                or sum(len(u.text) + 2 for u in buffer) + len(unit.text) > max_chars
            ):
                flush()
            if len(unit.text) > max_chars:
                flush()
                for piece in _split_long(unit.text, max_chars):
                    drafts.append(("paragraph", unit.section, [unit], piece))
            else:
                buffer.append(unit)
            continue
        flush()
        if unit.kind == "table":
            for part in _table_parts(unit, max_chars):
                drafts.append(("table", unit.section, [unit], part))
            assert unit.table is not None
            notes = " ".join(unit.table.notes)
            if len(notes) > _NOTES_IN_TABLE:
                # Las notas completas (signos, definiciones) no caben en cada fragmento del cuadro.
                for piece in _split_long(f"{unit.table.caption} — notas: {notes}", max_chars):
                    drafts.append(("table_notes", unit.section, [unit], piece))
        else:
            for piece in _split_long(unit.text, max_chars):
                drafts.append((unit.kind, unit.section, [unit], piece))
    flush()
    chunks = []
    for index, (kind, section, units, text) in enumerate(drafts):
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
            )
        )
    return chunks
