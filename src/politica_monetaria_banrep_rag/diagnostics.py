"""Diagnóstico de acceso a las fuentes: pocas solicitudes, sin redirecciones ni cuerpos guardados.

Sirve para comparar el acceso desde un equipo y desde un runner de GitHub Actions. Nunca registra
la URL completa de un reto antibot (su consulta incluye la IP pública codificada).
"""

import ssl
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from politica_monetaria_banrep_rag.crossref import CROSSREF_WORKS_URL, CrossrefReportSource
from politica_monetaria_banrep_rag.download import (
    DownloadBlockedError,
    DownloadError,
    PdfDownloader,
)
from politica_monetaria_banrep_rag.source import HISTORY_URL, ReportRecord, SourceError
from politica_monetaria_banrep_rag.urls import is_challenge_url

USER_AGENT = "banrep-rag/0.1 (+https://github.com/elKiruvi/politica_monetaria_banrep_rag)"
_PEEK_BYTES = 65_536


@dataclass(frozen=True)
class ProbeResult:
    name: str
    url: str
    outcome: str
    status: int | None = None
    content_type: str | None = None
    location: str | None = None
    detail: str | None = None


def _safe_location(value: str | None) -> str | None:
    """Conserva esquema, host y ruta; descarta la consulta (puede contener datos del cliente)."""
    if not value:
        return None
    parts = urlsplit(value)
    if is_challenge_url(value):
        return f"{parts.scheme}://{parts.netloc}/"  # La ruta del reto también puede llevar datos.
    return f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.netloc else parts.path


def _classify_error(exc: Exception) -> str:
    text = str(exc).lower()
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        if "name resolution" in text or "nodename" in text or "name or service" in text:
            return "dns"
        if isinstance(exc.__context__, ssl.SSLError) or "ssl" in text or "certificate" in text:
            return "tls"
        return "conexion"
    return "error_http"


def probe(client: httpx.Client, name: str, url: str) -> ProbeResult:
    """Una solicitud GET; lee como máximo 64 KB para clasificar la respuesta."""
    try:
        with client.stream("GET", url, follow_redirects=False) as response:
            peek = b""
            for block in response.iter_bytes():
                peek += block
                if len(peek) >= _PEEK_BYTES:
                    break
            mime = response.headers.get("content-type", "").split(";", 1)[0].strip() or None
            location = response.headers.get("location")
            status = response.status_code
    except httpx.HTTPError as exc:
        return ProbeResult(name, url, _classify_error(exc), detail=type(exc).__name__)
    if response.is_redirect:
        outcome = "reto_antibot" if is_challenge_url(location or "") else "redireccion"
    elif b"Radware Bot Manager" in peek:
        outcome = "reto_antibot"
    elif status == 429:
        outcome = "limite_de_tasa"
    elif 400 <= status < 500:
        outcome = "http_4xx"
    elif status >= 500:
        outcome = "http_5xx"
    elif status == 200 and peek.startswith(b"%PDF-"):
        outcome = "pdf"
    elif status == 200:
        outcome = "ok"
    else:
        outcome = "otro"
    return ProbeResult(name, url, outcome, status, mime, _safe_location(location))


def run(
    *,
    download_one: bool = False,
    client: httpx.Client | None = None,
    pause: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Ejecuta el diagnóstico completo y devuelve un informe serializable."""
    started = datetime.now(UTC).isoformat()
    own_client = client is None
    client = client or httpx.Client(
        timeout=httpx.Timeout(25, connect=8),
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
    )
    results: list[ProbeResult] = []
    latest: ReportRecord | None = None
    download: dict[str, object] | None = None
    try:
        results.append(probe(client, "crossref", f"{CROSSREF_WORKS_URL}?rows=0&filter=type:report"))
        try:
            latest = CrossrefReportSource(client, pause=pause).discover(1)[0]
            results.append(
                ProbeResult("crossref_ultimo_informe", CROSSREF_WORKS_URL, "ok", 200,
                            detail=f"{latest.reference_period} {latest.doi}")
            )  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - el diagnóstico debe informar, no abortar.
            detail = str(exc) if isinstance(exc, SourceError) else type(exc).__name__
            results.append(ProbeResult("crossref_ultimo_informe", CROSSREF_WORKS_URL, "error",
                                       detail=detail))  # fmt: skip
        targets = [
            ("historico_portada", HISTORY_URL),
            ("repositorio_robots", "https://repositorio.banrep.gov.co/robots.txt"),
        ]
        if latest is not None:
            targets += [
                ("doi_ultimo_informe", latest.source_url),
                ("pdf_ultimo_informe", latest.download_url),
            ]
        for name, url in targets:
            pause(2.0)
            results.append(probe(client, name, url))
        if download_one and latest is not None:
            pause(2.0)
            download = _download(latest)
        # Al final, porque la paginación suele activar el antibot y contaminaría el resto.
        pause(2.0)
        results.append(probe(client, "historico_pagina_2", f"{HISTORY_URL}?page=1"))
    finally:
        if own_client:
            client.close()
    return {
        "version": 1,
        "iniciado": started,
        "terminado": datetime.now(UTC).isoformat(),
        "sondas": [asdict(result) for result in results],
        "descarga": download,
    }


def _download(report: ReportRecord) -> dict[str, object]:
    """Descarga un PDF en un directorio temporal, informa hash y páginas y lo elimina."""
    with tempfile.TemporaryDirectory(prefix="banrep-diagnostico-") as folder:
        downloader = PdfDownloader()
        try:
            with downloader.client:
                result = downloader.download(report, Path(folder))
        except DownloadBlockedError as exc:
            return {"informe": report.report_id, "resultado": "reto_antibot", "detalle": str(exc)}
        except DownloadError as exc:
            return {"informe": report.report_id, "resultado": "error", "detalle": str(exc)}
        return {
            "informe": report.report_id,
            "resultado": "pdf_verificado",
            "sha256": result.sha256,
            "bytes": result.bytes_count,
            "paginas": result.pages_count,
            "url_final": _safe_location(result.final_url),
            "mime": result.mime_type,
        }


def _cell(value: object) -> str:
    """Escapa texto externo para que no altere la tabla Markdown del resumen."""
    text = " ".join(str(value or "").split())
    for char, escaped in (
        ("\\", "\\\\"),
        ("|", "\\|"),
        ("<", "&lt;"),
        (">", "&gt;"),
        ("[", "\\["),
        ("]", "\\]"),
        ("`", "\\`"),
        ("*", "\\*"),
        ("_", "\\_"),
    ):
        text = text.replace(char, escaped)
    return text


def render(report: dict[str, object]) -> str:
    """Tabla Markdown para la salida humana y el resumen de GitHub Actions."""
    lines = [
        f"Diagnóstico de fuentes — {report['iniciado']}",
        "",
        "| Sonda | Resultado | HTTP | Tipo | Destino / detalle |",
        "|---|---|---|---|---|",
    ]
    rows = report["sondas"]
    assert isinstance(rows, list)
    for row in rows:
        assert isinstance(row, dict)
        detail = row.get("location") or row.get("detail")
        cells = [row["name"], row["outcome"], row.get("status"), row.get("content_type"), detail]
        lines.append("| " + " | ".join(_cell(cell) for cell in cells) + " |")
    download = report.get("descarga")
    if isinstance(download, dict):
        lines += [
            "",
            "Descarga de prueba: " + ", ".join(f"{k}={_cell(v)}" for k, v in download.items()),
        ]
    return "\n".join(lines)
