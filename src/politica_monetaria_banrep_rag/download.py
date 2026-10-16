"""Descarga PDF oficial con redirecciones verificadas y almacenamiento atómico."""

import hashlib
import io
import os
import random
import re
import tempfile
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin

import httpx
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from politica_monetaria_banrep_rag.source import ReportRecord, SourceError, _official_url


class DownloadError(Exception):
    """El documento remoto no pudo validarse; no debe activarse en el catálogo."""


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    sha256: str
    bytes_count: int
    pages_count: int
    final_url: str
    mime_type: str


class PdfDownloader:
    """Descarga un PDF a un directorio local sin confiar en encabezados ni redirecciones."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        max_bytes: int = 30_000_000,
        max_pages: int = 400,
        attempts: int = 3,
    ) -> None:
        if min(max_bytes, max_pages, attempts) < 1:
            raise ValueError("Los límites de descarga deben ser positivos.")
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(45, connect=5, read=30),
            follow_redirects=False,
            headers={
                "User-Agent": "banrep-rag/0.1 (+https://github.com/elKiruvi/politica_monetaria_banrep_rag)",
                "Accept-Encoding": "identity",
            },
        )
        self.max_bytes = max_bytes
        self.max_pages = max_pages
        self.attempts = attempts

    def download(self, report: ReportRecord, raw_dir: Path) -> DownloadResult:
        if not re.fullmatch(r"ipm-\d{4}-\d{2}", report.report_id):
            raise DownloadError("El identificador del informe no es seguro.")
        try:
            url = _official_url(report.download_url, download=True)
        except SourceError as exc:
            raise DownloadError("La URL de descarga no es oficial o válida.") from exc
        for attempt in range(self.attempts):
            try:
                return self._download_once(report.report_id, url, raw_dir)
            except _RetryableError as exc:
                if attempt == self.attempts - 1:
                    raise DownloadError("La descarga falló después de los reintentos.") from exc
                delay = exc.delay if exc.delay is not None else 2**attempt + random.random()
                time.sleep(min(delay, 10))
        raise DownloadError("No se completó la descarga.")

    def _download_once(self, report_id: str, url: str, raw_dir: Path) -> DownloadResult:
        for _ in range(6):
            try:
                with self.client.stream("GET", url, follow_redirects=False) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise DownloadError("Falta el destino de una redirección.")
                        try:
                            url = _official_url(urljoin(url, location), download=True)
                        except SourceError as exc:
                            raise DownloadError(
                                "La redirección apunta a un destino no autorizado."
                            ) from exc
                        continue
                    if response.status_code == 429 or response.status_code >= 500:
                        raise _RetryableError(_retry_delay(response.headers.get("retry-after")))
                    if response.status_code != 200:
                        raise DownloadError(f"La fuente devolvió HTTP {response.status_code}.")
                    return self._save(response, report_id, raw_dir, url)
            except httpx.HTTPError as exc:
                raise _RetryableError() from exc
        raise DownloadError("Se superó el número de redirecciones autorizadas.")

    def _save(
        self, response: httpx.Response, report_id: str, raw_dir: Path, url: str
    ) -> DownloadResult:
        mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        encoded = response.headers.get("content-encoding", "identity").lower() != "identity"
        if mime and mime not in {"application/pdf", "application/octet-stream"}:
            raise DownloadError("La respuesta no declara un documento PDF.")
        try:
            declared = int(response.headers.get("content-length", "0"))
            if declared > self.max_bytes:
                raise DownloadError("El archivo excede el límite de tamaño.")
        except ValueError as exc:
            raise DownloadError("El tamaño declarado no es válido.") from exc
        raw_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp_name = tempfile.mkstemp(prefix=".descarga-", suffix=".pdf", dir=raw_dir)
        tmp = Path(tmp_name)
        digest = hashlib.sha256()
        total = 0
        try:
            with os.fdopen(fd, "wb") as handle:
                for block in response.iter_bytes(chunk_size=65_536):
                    total += len(block)
                    if total > self.max_bytes:
                        raise DownloadError("El archivo excede el límite de tamaño.")
                    digest.update(block)
                    handle.write(block)
                handle.flush()
                os.fsync(handle.fileno())
            if declared and not encoded and total != declared:
                raise DownloadError("La descarga está truncada o tiene un tamaño contradictorio.")
            if total < 8:
                raise DownloadError("La descarga PDF está vacía o incompleta.")
            with tmp.open("rb") as handle:
                if handle.read(5) != b"%PDF-":
                    raise DownloadError("La firma PDF no es válida.")
                handle.seek(0)
                try:
                    reader = PdfReader(io.BytesIO(handle.read()), strict=True)
                    if reader.is_encrypted:
                        raise DownloadError("El PDF requiere contraseña.")
                    pages = len(reader.pages)
                except (PyPdfError, ValueError, TypeError) as exc:
                    raise DownloadError("El PDF no se puede abrir o está truncado.") from exc
            if pages < 1 or pages > self.max_pages:
                raise DownloadError("El número de páginas excede los límites permitidos.")
            target = raw_dir / f"{report_id}-{digest.hexdigest()}.pdf"
            if target.is_file() and not target.is_symlink() and target.stat().st_size == total:
                with target.open("rb") as existing:
                    if hashlib.file_digest(existing, "sha256").hexdigest() == digest.hexdigest():
                        return DownloadResult(
                            target, digest.hexdigest(), total, pages, url, mime or "application/pdf"
                        )
            os.replace(tmp, target)
            return DownloadResult(
                target, digest.hexdigest(), total, pages, url, mime or "application/pdf"
            )
        finally:
            tmp.unlink(missing_ok=True)


class _RetryableError(Exception):
    def __init__(self, delay: float | None = None):
        self.delay = delay


def _retry_delay(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0, min(float(value), 10))
    except ValueError:
        try:
            return max(0, min((parsedate_to_datetime(value).timestamp() - time.time()), 10))
        except (TypeError, ValueError):
            return None
