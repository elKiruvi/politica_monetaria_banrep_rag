"""Descubrimiento conservador de informes principales en el histórico oficial."""

import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup, Tag

HISTORY_URL = (
    "https://www.banrep.gov.co/es/publicaciones-investigaciones/"
    "informe-politica-monetaria/historico-informes"
)
MONTHS = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}
MAIN_TITLE = re.compile(
    r"^Informe de Política Monetaria\s*[-–—:]\s*"
    r"(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)"
    r"\s+(?:de\s+)?(\d{4})$",
    re.IGNORECASE,
)
PERIOD_TITLE = re.compile(r"^(\d{4})\s*[-–]\s*([a-záéíóú]+)$", re.IGNORECASE)


class SourceError(Exception):
    """La fuente no permite verificar con certeza la ventana solicitada."""


def _safe_text(value: str) -> str:
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in value):
        raise SourceError("La fuente incluyó caracteres de control en un título o enlace.")
    return value


@dataclass(frozen=True)
class ReportRecord:
    report_id: str
    title: str
    document_type: str
    language: str
    reference_period: str
    published_at: str | None
    publication_date_origin: str | None
    discovered_at: str
    file_updated_at: str | None
    source_url: str
    download_url: str

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


def _official_url(href: str, *, download: bool = False) -> str:
    _safe_text(href)
    raw = urlsplit(href)
    if not raw.path or any(part in {".", ".."} for part in unquote(raw.path).split("/")):
        raise SourceError("La fuente proporcionó una ruta vacía o ambigua.")
    url = urljoin(HISTORY_URL, href)
    parts = urlsplit(url)
    allowed = {"www.banrep.gov.co", "banrep.gov.co"}
    if download:
        allowed |= {"doi.org", "repositorio.banrep.gov.co"}
    if parts.scheme != "https" or parts.hostname not in allowed or parts.username or parts.password:
        raise SourceError("La fuente proporcionó una URL fuera de los dominios autorizados.")
    try:
        invalid_port = parts.port is not None
    except ValueError as exc:
        raise SourceError("La fuente proporcionó un puerto no válido.") from exc
    if not parts.path or invalid_port:
        raise SourceError("La fuente proporcionó una URL no válida.")
    if download and not (
        (parts.hostname == "doi.org" and parts.path.strip("/"))
        or (parts.hostname != "doi.org" and parts.path.lower().endswith(".pdf"))
    ):
        raise SourceError("La descarga principal no apunta a un DOI ni a un archivo PDF.")
    return url


def parse_history(html: str, checked_at: str) -> list[ReportRecord]:
    """Acepta solo filas de informe principal con periodo y descarga inequívocos."""
    if "Radware Bot Manager Block" in html:
        raise SourceError("Banrep bloqueó la consulta; no se puede verificar el histórico.")
    soup = BeautifulSoup(html, "html.parser")
    view = soup.select_one(".view-publicaciones-buscador")
    if view is None:
        raise SourceError("Cambió el formato del histórico oficial; no se actualizará el catálogo.")
    result: list[ReportRecord] = []
    period: tuple[int, int] | None = None
    for element in view.descendants:
        if not isinstance(element, Tag):
            continue
        if element.name == "h3":
            match = PERIOD_TITLE.fullmatch(element.get_text(" ", strip=True))
            period = (
                (int(match[1]), MONTHS[match[2].lower()])
                if match and match[2].lower() in MONTHS
                else None
            )
        classes = element.get("class")
        if not isinstance(classes, list) or "views-row" not in classes:
            continue
        link = element.select_one(".titulo h2 a")
        if link is None:
            raise SourceError("Una fila del histórico carece de título clasificable.")
        title = _safe_text(link.get_text(" ", strip=True))
        match = MAIN_TITLE.fullmatch(title)
        if not match:
            if title.casefold().startswith("informe de política monetaria"):
                raise SourceError(f"No se puede clasificar el informe principal «{title}».")
            continue
        month, year = MONTHS[match[1].lower()], int(match[2])
        if period != (year, month):
            raise SourceError(f"El periodo del informe «{title}» no coincide con su sección.")
        downloads = [
            anchor
            for anchor in element.select(".descargables a, .enlaces a")
            if anchor.get_text(" ", strip=True).casefold() == "descargar" and anchor.get("href")
        ]
        if len(downloads) != 1:
            raise SourceError(f"El informe «{title}» no tiene una descarga principal inequívoca.")
        result.append(
            ReportRecord(
                report_id=f"ipm-{year:04d}-{month:02d}",
                title=title,
                document_type="main_report",
                language="es",
                reference_period=f"{year:04d}-{month:02d}",
                published_at=None,
                publication_date_origin=None,
                discovered_at=checked_at,
                file_updated_at=None,
                source_url=_official_url(str(link["href"])),
                download_url=_official_url(str(downloads[0]["href"]), download=True),
            )
        )
    if not result and not view.select(".views-row"):
        raise SourceError("No se encontró ningún informe principal verificable.")
    ids = [report.report_id for report in result]
    if len(ids) != len(set(ids)):
        raise SourceError("El histórico contiene periodos principales duplicados.")
    return result


def _page_signature(reports: list[ReportRecord]) -> list[tuple[str, str, str, str, str]]:
    return [
        (r.report_id, r.title, r.reference_period, r.source_url, r.download_url) for r in reports
    ]


class BanrepSource:
    """Consulta únicamente páginas públicas, sin eludir controles de acceso."""

    def __init__(self, client: httpx.Client | None = None):
        if client is not None and client.follow_redirects:
            raise ValueError("El cliente de la fuente no debe seguir redirecciones.")
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(20, connect=5),
            follow_redirects=False,
            headers={
                "User-Agent": "banrep-rag/0.1 (+https://github.com/elKiruvi/politica_monetaria_banrep_rag)"
            },
        )

    def _fetch_page(self, page: int) -> list[ReportRecord]:
        try:
            response = self.client.get(
                HISTORY_URL,
                params={"page": page} if page else None,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise SourceError("No se pudo consultar el histórico oficial.") from exc
        if (
            response.url.scheme != "https"
            or response.url.host not in {"www.banrep.gov.co", "banrep.gov.co"}
            or response.is_redirect
            or response.history
        ):
            raise SourceError("La fuente respondió con una redirección no verificada.")
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SourceError("No se pudo consultar el histórico oficial.") from exc
        return parse_history(response.text, datetime.now(UTC).isoformat())

    def discover(self, max_reports: int = 12) -> list[ReportRecord]:
        if max_reports < 1 or max_reports > 100:
            raise ValueError("max_reports debe estar entre 1 y 100.")
        selected: dict[str, ReportRecord] = {}
        first_page: list[tuple[str, str, str, str, str]] | None = None
        for page in range(41):
            reports = self._fetch_page(page)
            if page == 0:
                first_page = _page_signature(reports)
            for report in reports:
                previous = selected.get(report.report_id)
                if previous is not None and previous.download_url != report.download_url:
                    raise SourceError("Un periodo tiene descargas contradictorias entre páginas.")
                selected[report.report_id] = report
            if len(selected) >= max_reports:
                if page > 0 and first_page != _page_signature(self._fetch_page(0)):
                    raise SourceError(
                        "El histórico cambió durante la paginación; repita la consulta."
                    )
                return sorted(
                    selected.values(), key=lambda item: item.reference_period, reverse=True
                )[:max_reports]
        raise SourceError(
            f"Solo se verificaron {len(selected)} de {max_reports} informes; "
            "se conserva el catálogo anterior."
        )
