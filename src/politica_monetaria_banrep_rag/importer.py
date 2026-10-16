"""Identificación de PDF oficiales que la persona obtuvo legítimamente y quiere importar.

La importación es una vía de continuidad cuando la descarga automática está bloqueada. No prueba
que el archivo sea idéntico al publicado hoy. Exige dos evidencias independientes de que es el
informe principal de un periodo: la portada (la frase de la serie junto al periodo) y, además,
el metadato ``/Title`` anclado o el ISSN de la serie. Presentaciones, anexos, recuadros,
comunicados o versiones en inglés se rechazan.
"""

import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from politica_monetaria_banrep_rag.isolation import IsolationError, run_isolated
from politica_monetaria_banrep_rag.source import MONTHS

_MONTH = "|".join(MONTHS)
_SERIES = r"informe de pol[ií]tica monetaria"
_TITLE = re.compile(rf"^{_SERIES}[\s,:–—-]+({_MONTH})\s+(?:de\s+)?([0-9]{{4}})$", re.IGNORECASE)
_MONTH_YEAR = re.compile(rf"\b({_MONTH})\s+(?:de\s+)?([0-9]{{4}})\b", re.IGNORECASE)
_SERIES_RE = re.compile(_SERIES, re.IGNORECASE)
_ISSN = re.compile(r"ISSN\s*-?\s*2711\s*-?\s*1164")
_OTHER_DOCUMENT = re.compile(
    r"\b(presentaci[oó]n|anexo|recuadro|comunicado|minutas?|monetary policy report)\b",
    re.IGNORECASE,
)
_PERIOD = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
_USUAL_MONTHS = {1, 4, 7, 10}
_ADJACENCY = 80  # caracteres entre la frase de la serie y el periodo en la portada


class ImportFileError(Exception):
    """El archivo no se puede identificar con certeza como un informe principal."""


@dataclass(frozen=True)
class Identification:
    report_id: str
    evidence: tuple[str, ...]
    usual_month: bool = True


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    return " ".join(
        "".join(" " if unicodedata.category(c) in {"Cc", "Cf"} else c for c in text).split()
    )


def _period(match: re.Match[str]) -> str:
    return f"{int(match[2]):04d}-{MONTHS[match[1].lower()]:02d}"


_COVER_CHARS = 5_000  # las portadas reales caben de sobra; acota el trabajo con PDF hostiles


def _cover_period(cover: str) -> str | None:
    """El periodo más cercano a la frase de la serie, si está a menos de 80 caracteres.

    Lineal: cada aparición de la serie solo mira la ventana de 80 caracteres a su alrededor.
    """
    cover = cover[:_COVER_CHARS]
    best: tuple[int, str] | None = None
    for series in _SERIES_RE.finditer(cover):
        start = max(0, series.start() - _ADJACENCY - 20)
        window = cover[start : series.end() + _ADJACENCY + 20]
        for match in _MONTH_YEAR.finditer(window):
            if match[1].lower() not in MONTHS:
                continue
            m_start, m_end = start + match.start(), start + match.end()
            gap = m_start - series.end() if m_start >= series.end() else series.start() - m_end
            if 0 <= gap <= _ADJACENCY and (best is None or gap < best[0]):
                best = (gap, _period(match))
    return best[1] if best else None


def _read(path: str) -> tuple[str, str, str, str | None, str | None]:
    """Se ejecuta en un proceso aislado: título, inicio de las páginas 1 y 2, y su periodo."""
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    reader = PdfReader(path, strict=True)
    info = reader.metadata
    title = _normalize(str(info.title or "") if info is not None else "")[:500]
    pages = [_normalize(page.extract_text() or "")[:_COVER_CHARS] for page in reader.pages[:2]]
    first, second = (pages + ["", ""])[:2]
    return title, first, second, _cover_period(first), _cover_period(second)


def identify(
    path: Path, expected_period: str | None = None, *, display_name: str | None = None
) -> Identification:
    """Deduce el periodo del informe principal o lanza ``ImportFileError`` con el motivo.

    ``display_name`` es el nombre que verá la persona (la ruta puede ser una copia temporal).
    """
    if expected_period is not None and not _PERIOD.fullmatch(expected_period):
        raise ImportFileError("El periodo debe tener la forma AAAA-MM, p. ej. 2025-01.")
    try:
        title, first, second, cover_first, cover_second = run_isolated(
            _read, str(path), timeout=60, memory_mb=1024
        )
    except IsolationError as exc:
        raise ImportFileError(f"No se pudo leer el PDF para identificarlo: {exc}") from exc
    except (PyPdfError, ValueError, TypeError, OSError) as exc:  # por si falla el arranque
        raise ImportFileError("No se pudo leer el PDF para identificarlo.") from exc
    name = display_name or path.name
    if any(_OTHER_DOCUMENT.search(text) for text in (title, first[:300], second[:300])):
        raise ImportFileError(
            f"«{name}» parece una presentación, anexo, recuadro, comunicado o versión en "
            "inglés, no el informe principal."
        )
    cover = cover_first or cover_second  # a veces la p. 1 es solo imagen
    if cover is None:
        raise ImportFileError(
            f"«{name}» no parece un Informe de Política Monetaria: la portada no muestra la "
            "serie junto a su periodo."
        )
    found = {"portada": cover}
    if title_match := _TITLE.fullmatch(title):
        if title_match[1].lower() in MONTHS:
            found["metadato /Title"] = _period(title_match)
    if _ISSN.search(f"{first} {second}"):
        found["ISSN 2711-1164"] = cover
    if len(found) < 2:
        raise ImportFileError(
            f"«{name}» solo tiene una evidencia del periodo (la portada); falta el título "
            "interno del documento o el ISSN de la serie."
        )
    if expected_period is not None:
        found["periodo indicado"] = expected_period
    periods = set(found.values())
    if len(periods) != 1:
        detail = ", ".join(f"{origin}: {period}" for origin, period in found.items())
        raise ImportFileError(f"«{name}» tiene periodos contradictorios ({detail}).")
    period = periods.pop()
    usual = int(period[5:]) in _USUAL_MONTHS
    return Identification(f"ipm-{period}", tuple(sorted(found)), usual)
