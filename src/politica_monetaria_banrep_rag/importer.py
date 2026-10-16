"""Identificación de PDF oficiales que la persona obtuvo legítimamente y quiere importar.

La importación es una vía de continuidad cuando la descarga automática está bloqueada. No prueba
que el archivo sea idéntico al publicado hoy: solo que es un PDF válido cuyo metadato de título
y portada coinciden con un periodo concreto del Informe de Política Monetaria.
"""

import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from politica_monetaria_banrep_rag.source import MONTHS

_MONTH = "|".join(MONTHS)
_TITLE = re.compile(
    rf"informe de pol[ií]tica monetaria\W+({_MONTH})\s+(?:de\s+)?(\d{{4}})", re.IGNORECASE
)
_MONTH_YEAR = re.compile(rf"\b({_MONTH})\s+(?:de\s+)?(\d{{4}})\b", re.IGNORECASE)
_SERIES = re.compile(r"informe de pol[ií]tica monetaria", re.IGNORECASE)
_PERIOD = re.compile(r"\d{4}-(0[1-9]|1[0-2])")


class ImportFileError(Exception):
    """El archivo no se puede identificar con certeza como un informe principal."""


@dataclass(frozen=True)
class Identification:
    report_id: str
    evidence: tuple[str, ...]


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    return " ".join(
        "".join(" " if unicodedata.category(c) in {"Cc", "Cf"} else c for c in text).split()
    )


def _period(match: re.Match[str]) -> str:
    return f"{int(match[2]):04d}-{MONTHS[match[1].lower()]:02d}"


def identify(path: Path, expected_period: str | None = None) -> Identification:
    """Deduce el periodo con el metadato ``/Title`` y la portada; ambos deben concordar."""
    if expected_period is not None and not _PERIOD.fullmatch(expected_period):
        raise ImportFileError("El periodo debe tener la forma AAAA-MM, p. ej. 2025-01.")
    logging.getLogger("pypdf").setLevel(logging.ERROR)  # Avisos de fuentes, no errores.
    try:
        reader = PdfReader(path, strict=True)
        info = reader.metadata
        title = _normalize(str(info.title or "") if info is not None else "")
        cover = _normalize(" ".join(page.extract_text() or "" for page in reader.pages[:2]))
    except (PyPdfError, ValueError, TypeError, OSError) as exc:
        raise ImportFileError("No se pudo leer el PDF para identificarlo.") from exc
    found: dict[str, str] = {}
    title_match = _TITLE.search(title)
    if title_match:
        found["metadato /Title"] = _period(title_match)
    if _SERIES.search(cover):
        cover_match = _MONTH_YEAR.search(cover)
        if cover_match:
            found["portada"] = _period(cover_match)
    if expected_period is not None:
        found["periodo indicado"] = expected_period
    periods = set(found.values())
    if not periods or set(found) == {"periodo indicado"}:
        raise ImportFileError(
            f"«{path.name}» no parece un Informe de Política Monetaria: no se encontró el "
            "periodo en el título del documento ni en la portada."
        )
    if len(periods) != 1:
        detail = ", ".join(f"{origin}: {period}" for origin, period in found.items())
        raise ImportFileError(f"«{path.name}» tiene periodos contradictorios ({detail}).")
    period = periods.pop()
    return Identification(f"ipm-{period}", tuple(sorted(found)))
