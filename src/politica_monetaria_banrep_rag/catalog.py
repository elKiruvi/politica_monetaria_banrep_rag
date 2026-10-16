"""Catálogo local: snapshots inmutables y activación atómica tras descargar la ventana."""

import hashlib
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from politica_monetaria_banrep_rag.download import PdfDownloader
from politica_monetaria_banrep_rag.source import (
    BanrepSource,
    ReportRecord,
    SourceError,
    _official_url,
)


class CatalogError(Exception):
    """La ventana o el estado local no permiten activar un snapshot."""


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _sync_directory(path: Path) -> None:
    if sys.platform == "win32":
        return  # Windows no permite abrir directorios para fsync; os.replace sigue siendo atómico.
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".snapshot-", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _sync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _lock_exclusive(handle: BinaryIO) -> None:
    """Bloquea el archivo de control; en Windows `msvcrt` reintenta durante unos 10 s."""
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


class Catalog:
    """Serializa sincronizaciones; nunca publica una ventana incompleta."""

    def __init__(
        self,
        root: Path,
        source: BanrepSource,
        downloader: PdfDownloader,
        *,
        progress: Callable[[str], None] | None = None,
        max_seconds: float = 1800,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not 0 < max_seconds < float("inf"):
            raise ValueError("El plazo total de sincronización debe ser positivo.")
        self.root = root
        self.source = source
        self.downloader = downloader
        self.progress = progress or (lambda _: None)
        self.max_seconds = max_seconds
        self.clock = clock

    def current(self) -> tuple[str, dict[str, object]] | None:
        pointer = self.root / "current.json"
        if not pointer.exists():
            return None
        try:
            active = json.loads(pointer.read_bytes())
            snapshot_id = active["snapshot"]
            if (
                active != {"version": 1, "snapshot": snapshot_id}
                or not isinstance(snapshot_id, str)
                or not re.fullmatch(r"[0-9a-f]{64}", snapshot_id)
            ):
                raise ValueError("Puntero inválido")
            data = (self.root / "snapshots" / f"{snapshot_id}.json").read_bytes()
            if hashlib.sha256(data).hexdigest() != snapshot_id:
                raise ValueError("Snapshot alterado")
            snapshot = json.loads(data)
            if snapshot.get("version") != 1 or not isinstance(snapshot.get("reports"), list):
                raise ValueError("Formato de snapshot inválido")
            return snapshot_id, snapshot
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise CatalogError("El catálogo activo está dañado; no se actualizará.") from exc

    def sync(self, max_reports: int = 12) -> str:
        if (
            not isinstance(max_reports, int)
            or isinstance(max_reports, bool)
            or not 1 <= max_reports <= 100
        ):
            raise ValueError("max_reports debe estar entre 1 y 100.")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        deadline = self.clock() + self.max_seconds
        with (self.root / ".sync.lock").open("a+b") as lock:
            _lock_exclusive(lock)
            self.current()  # Un puntero anterior dañado nunca se reemplaza silenciosamente.
            self.progress(f"Verificando los {max_reports} informes principales en la fuente…")
            reports = self.source.discover(max_reports)
            self._validate_window(reports, max_reports)
            records: list[dict[str, object]] = []
            raw_dir = self.root / "raw"
            for position, report in enumerate(reports, start=1):
                if self.clock() >= deadline:
                    raise CatalogError(
                        "La sincronización superó el plazo total; se conserva el catálogo anterior."
                    )
                self.progress(f"Descargando {position}/{len(reports)}: {report.report_id}…")
                result = self.downloader.download(report, raw_dir)
                expected_name = f"{report.report_id}-{result.sha256}.pdf"
                if (
                    not re.fullmatch(r"[0-9a-f]{64}", result.sha256)
                    or result.path.name != expected_name
                    or result.path.parent.resolve() != raw_dir.resolve()
                    or result.path.is_symlink()
                    or not result.path.is_file()
                ):
                    raise CatalogError("La descarga no produjo un PDF dentro del almacén local.")
                with result.path.open("rb") as handle:
                    digest = hashlib.file_digest(handle, "sha256").hexdigest()
                if digest != result.sha256 or result.path.stat().st_size != result.bytes_count:
                    raise CatalogError("El PDF cambió antes de registrar el snapshot.")
                metadata = report.to_dict()
                metadata.pop("discovered_at")  # No cambiar la revisión solo por una nueva consulta.
                records.append(
                    {
                        "report": metadata,
                        "pdf": {
                            "path": f"raw/{expected_name}",
                            "sha256": result.sha256,
                            "bytes": result.bytes_count,
                            "pages": result.pages_count,
                            "final_url": result.final_url,
                            "mime_type": result.mime_type,
                        },
                    }
                )
            # Los nombres de los PDF deben ser duraderos antes del puntero.
            _sync_directory(raw_dir)
            data = _json_bytes({"version": 1, "reports": records})
            snapshot_id = hashlib.sha256(data).hexdigest()
            snapshot_path = self.root / "snapshots" / f"{snapshot_id}.json"
            if snapshot_path.exists():
                if snapshot_path.read_bytes() != data:
                    raise CatalogError("Un snapshot existente no coincide con su identificador.")
            else:
                _atomic_write(snapshot_path, data)
            if self.clock() >= deadline:
                raise CatalogError(
                    "La sincronización superó el plazo total; se conserva el catálogo anterior."
                )
            active = self.current()
            if active is None or active[0] != snapshot_id:
                self.progress("Activando el nuevo catálogo…")
                _atomic_write(
                    self.root / "current.json", _json_bytes({"version": 1, "snapshot": snapshot_id})
                )
            return snapshot_id

    @staticmethod
    def _validate_window(reports: list[ReportRecord], count: int) -> None:
        if len(reports) != count:
            raise CatalogError("La fuente no verificó toda la ventana solicitada.")
        ids = [report.report_id for report in reports]
        if len(set(ids)) != count or ids != sorted(ids, reverse=True):
            raise CatalogError("La ventana contiene duplicados o no está ordenada.")
        for report in reports:
            if (
                not re.fullmatch(r"ipm-\d{4}-(0[1-9]|1[0-2])", report.report_id)
                or report.reference_period != report.report_id[4:]
                or report.document_type != "main_report"
                or report.language != "es"
            ):
                raise CatalogError(
                    "La ventana contiene un documento que no es un informe principal."
                )
            try:
                _official_url(report.source_url)
                _official_url(report.download_url, download=True)
            except SourceError as exc:
                raise CatalogError("La ventana contiene una URL no autorizada.") from exc
