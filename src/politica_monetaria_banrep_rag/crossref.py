"""Descubrimiento de informes principales con los metadatos que el Banco deposita en Crossref.

Se consulta la API pública de Crossref por ventanas anuales de fecha de publicación del prefijo
DOI del Banco (``10.32468``). No se usa búsqueda por relevancia: cada ventana debe llegar
completa. El periodo de referencia se toma del título depositado; el DOI es solo identificador.
"""

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

import httpx

from politica_monetaria_banrep_rag.source import (
    MAIN_TITLE,
    MONTHS,
    ReportRecord,
    SourceError,
    _safe_text,
)
from politica_monetaria_banrep_rag.urls import UnsafeUrlError, download_start_url

CROSSREF_WORKS_URL = "https://api.crossref.org/prefixes/10.32468/works"
_FIELDS = "DOI,title,type,publisher,published,deposited,resource,updated-by"
_ROWS = 1000
_MAX_BYTES = 8_000_000
_DOI = re.compile(r"10\.32468/[a-z0-9._()-]{1,120}")
_FIRST_YEAR = 2000


@dataclass(frozen=True)
class _Candidate:
    record: ReportRecord
    published: date | None


def _date(value: object) -> date | None:
    """Convierte ``{"date-parts": [[a, m, d]]}``; solo acepta fechas completas."""
    if not isinstance(value, dict):
        return None
    parts = value.get("date-parts")
    if not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], list):
        return None
    numbers = parts[0]
    if len(numbers) != 3 or not all(type(n) is int for n in numbers):
        return None
    try:
        return date(numbers[0], numbers[1], numbers[2])
    except ValueError:
        return None


def _parse_item(item: object, checked_at: str) -> _Candidate | None:
    """Devuelve un candidato si el ítem es un informe principal en español; si no, ``None``."""
    if not isinstance(item, dict):
        raise SourceError("Crossref devolvió un registro con formato inesperado.")
    titles = item.get("title")
    title = titles[0] if isinstance(titles, list) and titles and isinstance(titles[0], str) else ""
    title = _safe_text(" ".join(title.split()))
    match = MAIN_TITLE.fullmatch(title)
    if not match:
        if title.casefold().startswith("informe de política monetaria"):
            raise SourceError(f"No se puede clasificar el registro de Crossref «{title}».")
        return None
    doi = item.get("DOI")
    if not isinstance(doi, str) or not _DOI.fullmatch(doi.lower()):
        raise SourceError(f"El informe «{title}» no tiene un DOI del Banco válido.")
    doi = doi.lower()
    publisher = item.get("publisher")
    if item.get("type") != "report" or not (
        isinstance(publisher, str) and publisher.startswith("Banco de la República")
    ):
        raise SourceError(f"El informe «{title}» no figura como reporte del Banco en Crossref.")
    resource = item.get("resource")
    primary = resource.get("primary") if isinstance(resource, dict) else None
    url = primary.get("URL") if isinstance(primary, dict) else None
    try:
        download_url = download_start_url(url) if isinstance(url, str) else ""
    except UnsafeUrlError as exc:
        raise SourceError(f"La URL registrada para «{title}» no es institucional: {exc}") from exc
    if not download_url:
        raise SourceError(f"El informe «{title}» no tiene URL registrada en Crossref.")
    month, year = MONTHS[match[1].lower()], int(match[2])
    published = _date(item.get("published"))
    period_start = date(year, month, 1)
    if published is not None and not period_start <= published <= date(year + 1, month, 1):
        raise SourceError(f"La fecha de publicación de «{title}» no concuerda con su periodo.")
    deposited = _date(item.get("deposited"))
    updates = []
    for update in item.get("updated-by") or []:
        if isinstance(update, dict) and update.get("DOI", "").lower() == doi:
            when = _date(update.get("updated"))
            kind_value = update.get("type")
            kind = kind_value if isinstance(kind_value, str) else "update"
            updates.append(f"{_safe_text(kind)}:{when.isoformat() if when else 'sin-fecha'}")
    record = ReportRecord(
        report_id=f"ipm-{year:04d}-{month:02d}",
        title=title,
        document_type="main_report",
        language="es",
        reference_period=f"{year:04d}-{month:02d}",
        published_at=published.isoformat() if published else None,
        publication_date_origin="crossref:published" if published else None,
        discovered_at=checked_at,
        file_updated_at=None,
        source_url=f"https://doi.org/{doi}",
        download_url=download_url,
        doi=doi,
        discovery_source="crossref",
        metadata_deposited_at=deposited.isoformat() if deposited else None,
        source_updates=tuple(sorted(updates)),
    )
    return _Candidate(record, published)


class CrossrefReportSource:
    """Consulta Crossref por años de publicación, del más reciente al más antiguo."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        contact_email: str | None = None,
        today: date | None = None,
        pause: Callable[[float], None] = time.sleep,
    ) -> None:
        if client is not None and client.follow_redirects:
            raise ValueError("El cliente de Crossref no debe seguir redirecciones.")
        if contact_email is not None and not re.fullmatch(
            r"[^@\s]+@[^@\s]+\.[^@\s]+", contact_email
        ):
            raise ValueError("El correo de contacto para Crossref no es válido.")
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(30, connect=8),
            follow_redirects=False,
            headers={
                "User-Agent": (
                    "banrep-rag/0.1 (+https://github.com/elKiruvi/politica_monetaria_banrep_rag)"
                )
            },
        )
        self.contact_email = contact_email
        self.today = today
        self.pause = pause

    def _fetch_year(self, year: int) -> list[object]:
        params = {
            "filter": f"type:report,from-pub-date:{year}-01-01,until-pub-date:{year}-12-31",
            "rows": str(_ROWS),
            "select": _FIELDS,
        }
        if self.contact_email:
            params["mailto"] = self.contact_email
        try:
            with self.client.stream(
                "GET", CROSSREF_WORKS_URL, params=params, follow_redirects=False
            ) as response:
                if response.status_code != 200:
                    raise SourceError(f"Crossref respondió HTTP {response.status_code}.")
                if response.url.host != "api.crossref.org":
                    raise SourceError("Crossref respondió desde un host inesperado.")
                body = bytearray()
                for block in response.iter_bytes():
                    body += block
                    if len(body) > _MAX_BYTES:
                        raise SourceError("La respuesta de Crossref excede el tamaño permitido.")
        except httpx.HTTPError as exc:
            raise SourceError("No se pudo consultar Crossref.") from exc
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise SourceError("Crossref devolvió una respuesta que no es JSON válido.") from exc
        message = data.get("message") if isinstance(data, dict) else None
        if (
            not isinstance(data, dict)
            or data.get("status") != "ok"
            or data.get("message-type") != "work-list"
            or not isinstance(message, dict)
            or not isinstance(message.get("items"), list)
            or type(message.get("total-results")) is not int
        ):
            raise SourceError("Crossref devolvió un listado con formato inesperado.")
        items = message["items"]
        if message["total-results"] != len(items):
            # Con más de mil reportes por año habría que paginar; no se acepta un año parcial.
            raise SourceError(f"Crossref no devolvió completo el año {year}; no se continúa.")
        return items

    def discover(self, max_reports: int = 12) -> list[ReportRecord]:
        if (
            not isinstance(max_reports, int)
            or isinstance(max_reports, bool)
            or not 1 <= max_reports <= 100
        ):
            raise ValueError("max_reports debe estar entre 1 y 100.")
        checked_at = datetime.now(UTC).isoformat()
        current_year = (self.today or datetime.now(UTC).date()).year
        selected: dict[str, ReportRecord] = {}
        for year in range(current_year, _FIRST_YEAR - 1, -1):
            if year != current_year:
                self.pause(0.5)  # Crossref limita la concurrencia; las consultas van en serie.
            for item in self._fetch_year(year):
                candidate = _parse_item(item, checked_at)
                if candidate is None:
                    continue
                record = candidate.record
                if record.report_id in selected:
                    raise SourceError(
                        "Crossref registra más de un informe principal para "
                        f"{record.reference_period}."
                    )
                selected[record.report_id] = record
            window = sorted(selected.values(), key=lambda r: r.reference_period, reverse=True)
            # Todo informe publicado antes de este año tiene periodo anterior al 1 de enero
            # (la fecha de publicación nunca precede al periodo), así que la ventana es final.
            if len(window) >= max_reports and window[max_reports - 1].reference_period >= (
                f"{year:04d}-01"
            ):
                return window[:max_reports]
        raise SourceError(
            f"Solo se verificaron {len(selected)} de {max_reports} informes en Crossref; "
            "se conserva el catálogo anterior."
        )
