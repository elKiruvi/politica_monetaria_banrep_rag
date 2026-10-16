"""Catálogo local: snapshots inmutables y activación atómica tras descargar la ventana."""

import hashlib
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from politica_monetaria_banrep_rag.download import (
    DownloadBlockedError,
    DownloadError,
    DownloadTransientError,
    PdfDownloader,
    inspect_pdf,
)
from politica_monetaria_banrep_rag.importer import Identification
from politica_monetaria_banrep_rag.source import (
    MONTHS,
    ReportRecord,
    ReportSource,
    SourceError,
    _official_url,
)

_PDF_FIELDS = ("path", "sha256", "bytes", "pages", "final_url", "mime_type")
_REGISTRY_FIELDS = (
    *_PDF_FIELDS,
    "download_url",
    "metadata_fingerprint",
    "verified_at",
    "verified_ts",
)


_MAX_IMPORT_BYTES = 30_000_000


class CatalogError(Exception):
    """La ventana o el estado local no permiten activar un snapshot."""


_FINGERPRINT_EXCLUDE = ("metadata_deposited_at", "discovery_source")
_ISO_SECONDS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00")


def _fingerprint(metadata: dict[str, object]) -> str:
    """Huella de los metadatos que afectan al documento (no de la procedencia de la consulta)."""
    relevant = {k: v for k, v in metadata.items() if k not in _FINGERPRINT_EXCLUDE}
    return hashlib.sha256(_json_bytes(relevant)).hexdigest()


def _reject_constant(value: str) -> float:
    raise ValueError(f"Valor numérico no permitido: {value}")


def _check_entry(report_id: str, item: dict[str, object]) -> None:
    """Valida tipos y coherencia de una entrada del registro; ``ValueError`` si no encaja."""
    match = re.fullmatch(rf"raw/{report_id}-([0-9a-f]{{64}})\.pdf", str(item["path"]))
    numbers = (item["bytes"], item["pages"], item["verified_ts"])
    if (
        not match
        or match[1] != item["sha256"]
        or any(isinstance(n, bool) or not isinstance(n, int | float) for n in numbers)
        or not isinstance(item["bytes"], int)
        or not isinstance(item["pages"], int)
        or int(item["bytes"]) < 8
        or not 1 <= int(item["pages"]) <= 400
        or not all(isinstance(item[k], str) for k in ("final_url", "mime_type", "download_url"))
        or not isinstance(item["verified_at"], str)
        or not _ISO_SECONDS.fullmatch(item["verified_at"])
    ):
        raise ValueError("Entrada inválida")


def _is_import(entry: dict[str, object]) -> bool:
    return entry.get("method") == "local_import"


def _latest_import(entries: list[dict[str, object]]) -> dict[str, object] | None:
    imports = [entry for entry in entries if _is_import(entry)]
    return max(imports, key=_timestamp) if imports else None


def _inferred_metadata(report_id: str) -> dict[str, object]:
    """Metadatos mínimos cuando no se pudieron verificar en Crossref (sin red)."""
    year, month = int(report_id[4:8]), int(report_id[9:11])
    name = next(key for key, value in MONTHS.items() if value == month)
    return ReportRecord(
        report_id=report_id,
        title=f"Informe de Política Monetaria - {name} de {year}",
        document_type="main_report",
        language="es",
        reference_period=report_id[4:],
        published_at=None,
        publication_date_origin=None,
        discovered_at="",
        file_updated_at=None,
        source_url="",
        download_url="",
        discovery_source="local_import",
    ).to_dict()


def _timestamp(entry: dict[str, object]) -> float:
    value = entry["verified_ts"]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise CatalogError("El registro local de descargas tiene una fecha no válida.")
    return float(value)


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
        source: ReportSource,
        downloader: PdfDownloader,
        *,
        progress: Callable[[str], None] | None = None,
        max_seconds: float = 1800,
        clock: Callable[[], float] = time.monotonic,
        revalidate_seconds: float = 30 * 86400,
        download_pause: float = 10.0,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not 0 < max_seconds < float("inf"):
            raise ValueError("El plazo total de sincronización debe ser positivo.")
        if not 0 <= revalidate_seconds < float("inf"):
            raise ValueError("El plazo de revalidación debe ser un número finito no negativo.")
        if not 0 <= download_pause <= 600:
            raise ValueError("La pausa entre descargas debe estar entre 0 y 600 segundos.")
        self.root = root
        self.source = source
        self.downloader = downloader
        self.progress = progress or (lambda _: None)
        self.max_seconds = max_seconds
        self.clock = clock
        self.revalidate_seconds = revalidate_seconds
        self.download_pause = download_pause
        self.wall_clock = wall_clock
        self.sleep = sleep

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
            if snapshot.get("version") not in {1, 2} or not isinstance(
                snapshot.get("reports"), list
            ):
                raise ValueError("Formato de snapshot inválido")
            return snapshot_id, snapshot
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise CatalogError("El catálogo activo está dañado; no se actualizará.") from exc

    def sync(self, max_reports: int = 12, *, use_imports: bool = False) -> str:
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
            self.progress(
                f"Consultando la fuente: ventana de {max_reports} informe(s) principal(es)…"
            )
            reports = self.source.discover(max_reports)
            self._validate_window(reports, max_reports)
            records: list[dict[str, object]] = []
            raw_dir = self.root / "raw"
            registry = self._load_registry()
            downloaded = False
            for position, report in enumerate(reports, start=1):
                if self.clock() >= deadline:
                    raise CatalogError(
                        "La sincronización superó el plazo total; se conserva el catálogo anterior."
                    )
                metadata = report.to_dict()
                metadata.pop("discovered_at")  # No cambiar la revisión solo por una nueva consulta.
                fingerprint = _fingerprint(metadata)
                entries = registry.setdefault(report.report_id, [])
                previous = self._reusable(entries, report.download_url, fingerprint, raw_dir)
                fresh = previous is not None and self._is_fresh(previous)
                label = f"{position}/{len(reports)}: {report.report_id}"
                if fresh:
                    assert previous is not None
                    self.progress(f"Sin cambios {label} (verificado el {previous['verified_at']}).")
                    pdf = previous
                else:
                    if downloaded and self.download_pause:
                        self.sleep(self.download_pause)  # Ritmo respetuoso con la fuente.
                        deadline += self.download_pause  # La espera no consume el plazo.
                    self.progress(f"Descargando {label}…")
                    downloaded = True
                    try:
                        pdf = self._download(report, raw_dir, fingerprint)
                    except DownloadError as exc:
                        transient = isinstance(exc, DownloadBlockedError | DownloadTransientError)
                        imported = _latest_import(entries) if use_imports else None
                        if previous is None and imported is not None:
                            self.progress(
                                f"Aviso: no se pudo descargar {report.report_id}; se usa el PDF "
                                f"importado localmente el {imported['verified_at']}."
                            )
                            records.append(self._import_record(imported, metadata))
                            continue
                        if previous is None or not transient:
                            raise
                        self.progress(
                            f"Aviso: no se pudo revalidar {report.report_id}; se conserva el PDF "
                            f"verificado el {previous['verified_at']}."
                        )
                        pdf = previous
                    else:
                        # Una revalidación con los mismos bytes renueva la entrada existente.
                        entries[:] = [
                            entry
                            for entry in entries
                            if (entry["sha256"], entry["download_url"])
                            != (pdf["sha256"], pdf["download_url"])
                        ] + [pdf]
                        self._save_registry(registry)  # Lo verificado sobrevive a un fallo.
                records.append(
                    {
                        "report": metadata,
                        "pdf": {key: pdf[key] for key in _PDF_FIELDS},
                        "acquisition": {"method": "download"},
                    }
                )
            # Los nombres de los PDF deben ser duraderos antes del puntero.
            _sync_directory(raw_dir)
            self._save_registry(registry)
            return self._publish(
                {"mode": "latest_n", "max_reports": max_reports}, records, deadline
            )

    def _publish(
        self, selection: dict[str, object], records: list[dict[str, object]], deadline: float
    ) -> str:
        """Escribe el snapshot inmutable y, si cambió, reemplaza el puntero de forma atómica."""
        data = _json_bytes({"version": 2, "selection": selection, "reports": records})
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

    def import_pdf(
        self, source_path: Path, identification: Identification, report: ReportRecord | None
    ) -> dict[str, object]:
        """Copia un PDF local validado a ``raw/`` y lo registra con procedencia ``local_import``."""
        report_id = identification.report_id
        if not re.fullmatch(r"ipm-\d{4}-(0[1-9]|1[0-2])", report_id):
            raise CatalogError("El identificador del informe importado no es válido.")
        if report is not None and report.report_id != report_id:
            raise CatalogError("Los metadatos verificados corresponden a otro periodo.")
        if not source_path.is_file() or source_path.stat().st_size > _MAX_IMPORT_BYTES:
            raise CatalogError("El archivo a importar no existe o excede 30 MB.")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        raw_dir = self.root / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / ".sync.lock").open("a+b") as lock:
            _lock_exclusive(lock)
            fd, tmp_name = tempfile.mkstemp(prefix=".importacion-", suffix=".pdf", dir=raw_dir)
            tmp = Path(tmp_name)
            try:
                digest = hashlib.sha256()
                with source_path.open("rb") as source, os.fdopen(fd, "wb") as handle:
                    while block := source.read(65_536):
                        digest.update(block)
                        handle.write(block)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    pages = inspect_pdf(tmp)
                except DownloadError as exc:
                    raise CatalogError(f"El archivo no es un PDF válido: {exc}") from exc
                sha256 = digest.hexdigest()
                target = raw_dir / f"{report_id}-{sha256}.pdf"
                os.replace(tmp, target)
                _sync_directory(raw_dir)
            finally:
                tmp.unlink(missing_ok=True)
            now = self.wall_clock()
            metadata = report.to_dict() if report is not None else _inferred_metadata(report_id)
            metadata.pop("discovered_at", None)
            entry: dict[str, object] = {
                "path": f"raw/{target.name}",
                "sha256": sha256,
                "bytes": target.stat().st_size,
                "pages": pages,
                "final_url": "",
                "mime_type": "application/pdf",
                "download_url": report.download_url if report is not None else "",
                "metadata_fingerprint": "",
                "verified_at": datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds"),
                "verified_ts": now,
                "method": "local_import",
                "source_name": source_path.name,
                "identified_by": list(identification.evidence),
                "metadata_verified": report is not None,
                "report": metadata,
            }
            registry = self._load_registry()
            entries = registry.setdefault(report_id, [])
            entries[:] = [e for e in entries if e.get("sha256") != sha256 or not _is_import(e)]
            entries.append(entry)
            self._save_registry(registry)
            return entry

    def activate_imports(self) -> str:
        """Activa un catálogo con los PDF importados; no equivale a «los últimos N informes»."""
        with (self.root / ".sync.lock").open("a+b") as lock:
            _lock_exclusive(lock)
            self.current()
            registry = self._load_registry()
            records = []
            for report_id in sorted(registry, reverse=True):
                imported = _latest_import(registry[report_id])
                if imported is not None:
                    report = imported["report"]
                    assert isinstance(report, dict)
                    records.append(self._import_record(imported, report))
            if not records:
                raise CatalogError("No hay PDF importados para activar.")
            return self._publish({"mode": "local_import"}, records, self.clock() + self.max_seconds)

    def _import_record(
        self, entry: dict[str, object], metadata: dict[str, object]
    ) -> dict[str, object]:
        self._verify_local(
            self.root / str(entry["path"]), str(entry["sha256"]), int(str(entry["bytes"]))
        )
        return {
            "report": metadata,
            "pdf": {key: entry[key] for key in _PDF_FIELDS},
            "acquisition": {
                "method": "local_import",
                "source_name": entry["source_name"],
                "imported_at": entry["verified_at"],
                "identified_by": entry["identified_by"],
                "metadata_verified": entry["metadata_verified"],
                "verified_against_source": False,
            },
        }

    def plan(self, max_reports: int = 12) -> dict[str, object]:
        """Describe qué haría ``sync`` sin descargar ni escribir nada (``--dry-run``)."""
        reports = self.source.discover(max_reports)
        self._validate_window(reports, max_reports)
        exists = self.root.exists()
        registry = self._load_registry() if exists else {}
        active = self.current() if exists else None
        raw_dir = self.root / "raw"
        rows = []
        for report in reports:
            metadata = report.to_dict()
            metadata.pop("discovered_at")
            fingerprint = _fingerprint(metadata)
            entries = registry.get(report.report_id, [])
            previous = self._reusable(entries, report.download_url, fingerprint, raw_dir)
            if previous is not None:
                status = "sin_cambios" if self._is_fresh(previous) else "revalidar"
            else:
                status = "metadatos_o_archivo_cambiados" if entries else "nuevo"
            rows.append({"report_id": report.report_id, "title": report.title, "status": status})
        window = {report.report_id for report in reports}
        leaving = []
        active_records = active[1]["reports"] if active is not None else []
        assert isinstance(active_records, list)
        for record in active_records:
            report_id = record["report"]["report_id"]
            if report_id not in window:
                leaving.append(report_id)
        return {"window": rows, "leaving": leaving}

    def _download(self, report: ReportRecord, raw_dir: Path, fingerprint: str) -> dict[str, object]:
        result = self.downloader.download(report, raw_dir)
        expected_name = f"{report.report_id}-{result.sha256}.pdf"
        if (
            not re.fullmatch(r"[0-9a-f]{64}", result.sha256)
            or result.path.name != expected_name
            or result.path.parent.resolve() != raw_dir.resolve()
        ):
            raise CatalogError("La descarga no produjo un PDF dentro del almacén local.")
        self._verify_local(result.path, result.sha256, result.bytes_count)
        now = self.wall_clock()
        return {
            "path": f"raw/{expected_name}",
            "sha256": result.sha256,
            "bytes": result.bytes_count,
            "pages": result.pages_count,
            "final_url": result.final_url,
            "mime_type": result.mime_type,
            "download_url": report.download_url,
            "metadata_fingerprint": fingerprint,
            "verified_at": datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds"),
            "verified_ts": now,
        }

    @staticmethod
    def _verify_local(path: Path, sha256: str, size: int) -> None:
        if path.is_symlink() or not path.is_file():
            raise CatalogError("La descarga no produjo un PDF dentro del almacén local.")
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if digest != sha256 or path.stat().st_size != size:
            raise CatalogError("El PDF cambió antes de registrar el snapshot.")
        with path.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                raise CatalogError("El archivo local no es un PDF.")

    def _is_fresh(self, entry: dict[str, object]) -> bool:
        """Vigente si se verificó hace menos del plazo; una fecha futura nunca es vigente."""
        age = self.wall_clock() - _timestamp(entry)
        return 0 <= age < self.revalidate_seconds

    def _reusable(
        self, entries: list[dict[str, object]], url: str, fingerprint: str, raw_dir: Path
    ) -> dict[str, object] | None:
        """Última descarga verificada de la misma URL y metadatos cuyo archivo sigue intacto."""
        for entry in reversed(entries):
            if _is_import(entry):
                continue
            if entry.get("download_url") != url or entry.get("metadata_fingerprint") != fingerprint:
                continue
            path = self.root / str(entry["path"])
            if path.parent.resolve() != raw_dir.resolve():
                return None
            try:
                self._verify_local(path, str(entry["sha256"]), int(str(entry["bytes"])))
            except (CatalogError, OSError):
                return None  # Archivo ausente o alterado: hay que descargarlo de nuevo.
            return entry
        return None

    def _load_registry(self) -> dict[str, list[dict[str, object]]]:
        path = self.root / "acquisitions.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_bytes(), parse_constant=_reject_constant)
            entries = data["entries"]
            if data.get("version") != 1 or not isinstance(entries, dict):
                raise ValueError("Formato inválido")
            for report_id, items in entries.items():
                if not re.fullmatch(r"ipm-\d{4}-\d{2}", report_id) or not isinstance(items, list):
                    raise ValueError("Formato inválido")
                for item in items:
                    if not isinstance(item, dict) or set(_REGISTRY_FIELDS) - set(item):
                        raise ValueError("Formato inválido")
                    _check_entry(report_id, item)
            return entries
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CatalogError(
                "El registro local de descargas está dañado; revíselo o elimínelo para "
                "volver a descargar todo."
            ) from exc

    def _save_registry(self, registry: dict[str, list[dict[str, object]]]) -> None:
        _atomic_write(
            self.root / "acquisitions.json", _json_bytes({"version": 1, "entries": registry})
        )

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
