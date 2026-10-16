"""Descarga PDF oficial con redirecciones verificadas y almacenamiento atómico."""

import hashlib
import io
import math
import os
import random
import re
import tempfile
import time
import zlib
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


class _TransientDownloadError(Exception):
    def __init__(self, retry_after: float | None = None) -> None:
        self.retry_after = retry_after


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
        return max(0.0, min(seconds, 10.0)) if math.isfinite(seconds) else None
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            seconds = date.timestamp() - time.time()
            return max(0.0, min(seconds, 10.0)) if math.isfinite(seconds) else None
        except (TypeError, ValueError, OverflowError):
            return None


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
        max_seconds: float = 180,
        attempts: int = 3,
    ) -> None:
        if (
            min(max_bytes, max_pages, max_seconds, attempts) < 1
            or not math.isfinite(max_seconds)
            or attempts > 5
        ):
            raise ValueError("Los límites de descarga deben ser positivos.")
        if client is not None:
            self._reject_client_credentials(client)
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
        self.max_seconds = max_seconds
        self.attempts = attempts

    @staticmethod
    def _reject_client_credentials(client: httpx.Client) -> None:
        public_headers = {"accept", "accept-encoding", "connection", "user-agent"}
        if (
            client.auth is not None
            or client.params
            or any(key.lower() not in public_headers for key in client.headers)
            or client.cookies
            or any(client.event_hooks.values())
        ):
            raise ValueError("La descarga pública no admite clientes con credenciales ni hooks.")

    def download(self, report: ReportRecord, raw_dir: Path) -> DownloadResult:
        if not re.fullmatch(r"ipm-\d{4}-\d{2}", report.report_id):
            raise DownloadError("El identificador del informe no es seguro.")
        try:
            url = _official_url(report.download_url, download=True)
        except SourceError as exc:
            raise DownloadError("La URL de descarga no es oficial o válida.") from exc
        deadline = time.monotonic() + self.max_seconds
        for attempt in range(self.attempts):
            self._check_deadline(deadline)
            try:
                return self._download_once(report.report_id, url, raw_dir, deadline)
            except _TransientDownloadError as exc:
                if attempt == self.attempts - 1:
                    raise DownloadError("La descarga falló después de los reintentos.") from exc
                delay = exc.retry_after
                if delay is None:
                    delay = min(2**attempt + random.random(), 10.0)
                if time.monotonic() + delay >= deadline:
                    raise DownloadError("La descarga superó el tiempo máximo.") from exc
                time.sleep(delay)
        raise DownloadError("No se completó la descarga.")

    @staticmethod
    def _check_deadline(deadline: float) -> None:
        if time.monotonic() >= deadline:
            raise DownloadError("La descarga superó el tiempo máximo.")

    def _download_once(
        self, report_id: str, url: str, raw_dir: Path, deadline: float
    ) -> DownloadResult:
        for _ in range(6):
            self._check_deadline(deadline)
            self._reject_client_credentials(self.client)
            try:
                with self.client.stream("GET", url, follow_redirects=False) as response:
                    # La fuente pública no requiere ni conserva cookies entre saltos.
                    self.client.cookies.clear()
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
                    if response.status_code in {408, 429} or response.status_code >= 500:
                        raise _TransientDownloadError(
                            _retry_after(response.headers.get("retry-after"))
                        )
                    if response.status_code != 200:
                        raise DownloadError(f"La fuente devolvió HTTP {response.status_code}.")
                    return self._save(response, report_id, raw_dir, url, deadline)
            except httpx.HTTPError as exc:
                raise _TransientDownloadError() from exc
        raise DownloadError("Se superó el número de redirecciones autorizadas.")

    def _save(
        self, response: httpx.Response, report_id: str, raw_dir: Path, url: str, deadline: float
    ) -> DownloadResult:
        mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        encoding = response.headers.get("content-encoding", "identity").lower()
        if encoding not in {"identity", "gzip"}:
            raise DownloadError("La compresión HTTP de la respuesta no está admitida.")
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
        raw_total = 0
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
        try:
            with os.fdopen(fd, "wb") as handle:
                for raw_block in response.iter_raw(chunk_size=65_536):
                    self._check_deadline(deadline)
                    raw_total += len(raw_block)
                    if raw_total > self.max_bytes:
                        raise DownloadError("El archivo excede el límite de tamaño.")
                    try:
                        block = (
                            decoder.decompress(raw_block, self.max_bytes - total + 1)
                            if decoder is not None
                            else raw_block
                        )
                    except zlib.error as exc:
                        raise DownloadError("La compresión HTTP está dañada.") from exc
                    total += len(block)
                    if total > self.max_bytes:
                        raise DownloadError("El archivo excede el límite de tamaño.")
                    digest.update(block)
                    handle.write(block)
                handle.flush()
                os.fsync(handle.fileno())
            if decoder is not None and not decoder.eof:
                raise DownloadError("La respuesta comprimida está incompleta o excede el límite.")
            if decoder is not None and decoder.unused_data:
                raise DownloadError("La respuesta comprimida contiene datos sobrantes.")
            if declared and raw_total != declared:
                raise DownloadError("La descarga está truncada o tiene un tamaño contradictorio.")
            if total < 8:
                raise DownloadError("La descarga PDF está vacía o incompleta.")
            self._check_deadline(deadline)
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
            self._check_deadline(deadline)
            target = raw_dir / f"{report_id}-{digest.hexdigest()}.pdf"
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise DownloadError("El destino del PDF no es un archivo seguro.")
            if target.is_file() and target.stat().st_size == total:
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
