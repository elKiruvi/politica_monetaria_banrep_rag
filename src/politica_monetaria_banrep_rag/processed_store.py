"""Almacén de documentos procesados, con caché por hash del PDF y versiones del procesamiento.

Cada archivo de ``processed/`` depende solo de los bytes del PDF y de las versiones del parser y
del fragmentador: volver a procesar el mismo PDF con las mismas versiones no cambia nada. Los
metadatos del informe (título, DOI, URL) se copian del snapshot activo para poder citar.
"""

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from politica_monetaria_banrep_rag.catalog import (
    CatalogError,
    _atomic_write,
    _json_bytes,
    _lock_exclusive,
)
from politica_monetaria_banrep_rag.chunking import CHUNKER_VERSION, chunk_document
from politica_monetaria_banrep_rag.isolation import IsolationError, run_isolated
from politica_monetaria_banrep_rag.processing import (
    PROCESSING_VERSION,
    ProcessedDocument,
    QualityReport,
    Unit,
    process_pdf,
    quality_warnings,
)
from politica_monetaria_banrep_rag.tables import Cell, Row, Table

_SHA = re.compile(r"[0-9a-f]{64}")
_UNIT_KINDS = {"paragraph", "table", "table_raw", "chart_caption", "footnote"}


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("lista de texto inválida")
    return tuple(value)


def _table(value: object) -> Table:
    if not isinstance(value, dict) or not isinstance(value.get("rows"), list):
        raise ValueError("cuadro inválido")
    rows = []
    for entry in value["rows"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("values"), list):
            raise ValueError("fila inválida")
        cells: list[Cell | None] = []
        for cell in entry["values"]:
            if cell is None:
                cells.append(None)
            elif (
                isinstance(cell, dict)
                and isinstance(cell.get("text"), str)
                and type(cell.get("bold")) is bool
            ):
                cells.append(Cell(cell["text"], cell["bold"]))
            else:
                raise ValueError("celda inválida")
        rows.append(Row(_strings(entry.get("labels")), tuple(cells), _strings(entry.get("group"))))
    if (
        not isinstance(value.get("caption"), str)
        or type(value.get("page")) is not int
        or not isinstance(value.get("context"), str)
        or type(value.get("bold_is_projection")) is not bool
    ):
        raise ValueError("cabecera de cuadro inválida")
    table = Table(
        value["caption"],
        value["page"],
        value["context"],
        _strings(value.get("label_headers")),
        _strings(value.get("columns")),
        tuple(rows),
        _strings(value.get("notes")),
        value["bold_is_projection"],
    )
    if any(len(row.values) != len(table.columns) for row in rows):
        raise ValueError("columnas de cuadro inválidas")
    return table


def _replay_chunks(
    document: dict[str, object], report_id: str, sha256: str, max_chars: int
) -> list[dict[str, object]]:
    """Reconstruye las citas desde las unidades, no desde el texto del fragmento guardado."""
    serialized = document["units"]
    assert isinstance(serialized, list)
    units = []
    for entry in serialized:
        assert isinstance(entry, dict)
        table = _table(entry["table"]) if entry.get("kind") == "table" else None
        if entry.get("table") is not None and entry.get("kind") != "table":
            raise ValueError("cuadro fuera de una unidad de cuadro")
        if (
            entry.get("page_label") is not None
            and not isinstance(entry["page_label"], str)
            or entry.get("page_end_label") is not None
            and not isinstance(entry["page_end_label"], str)
        ):
            raise ValueError("etiqueta de página inválida")
        unit = Unit(
            entry["kind"],
            entry["text"],
            entry["page"],
            entry.get("page_label"),
            _strings(entry["section"]),
            table,
            entry.get("page_end"),
            entry.get("page_end_label"),
        )
        if table is not None and (table.page != unit.page or table.to_text() != unit.text):
            raise ValueError("texto del cuadro no coincide con sus celdas")
        units.append(unit)
    quality = document["quality"]
    assert isinstance(quality, dict)
    replayed = ProcessedDocument(
        report_id, sha256, PROCESSING_VERSION, tuple(units), QualityReport(**quality)
    )
    return [chunk.to_dict() for chunk in chunk_document(replayed, max_chars=max_chars)]


def _payload_digest(data: dict[str, object]) -> str:
    """Detecta corrupción; no autentica un archivo local manipulado con su hash recalculado."""
    content = {key: data[key] for key in ("document", "chunks", "versions")}
    return hashlib.sha256(_json_bytes(content)).hexdigest()


def _valid_payload(
    data: dict[str, object], report_id: str, sha256: str, versions: dict[str, object]
) -> bool:
    if data.get("versions") != versions or not isinstance(data.get("document"), dict):
        return False
    document = data["document"]
    assert isinstance(document, dict)
    quality = document.get("quality")
    units = document.get("units")
    chunks = data.get("chunks")
    if (
        document.get("report_id") != report_id
        or document.get("sha256") != sha256
        or document.get("version") != PROCESSING_VERSION
        or not isinstance(quality, dict)
        or not isinstance(chunks, list)
        or not isinstance(units, list)
    ):
        return False
    pages = quality.get("pages")
    assert isinstance(units, list) and isinstance(chunks, list)
    if (
        type(pages) is not int
        or not 1 <= pages <= 400
        or any(
            type(quality.get(key)) is not int or quality[key] < 0
            for key in ("tables_reconstructed", "ligatures_repaired")
        )
        or not isinstance(quality.get("tables_failed"), list)
        or type(quality.get("degraded")) is not bool
        or not isinstance(quality.get("warnings"), list)
        or not all(isinstance(w, str) for w in quality["warnings"])
        or type(quality.get("pages_with_text")) is not int
        or not 0 <= quality["pages_with_text"] <= pages
        or type(quality.get("characters")) is not int
        or type(quality.get("ligatures_unresolved")) is not int
        or quality["ligatures_unresolved"] < 0
        or not isinstance(quality.get("units"), dict)
        or not isinstance(quality.get("excluded_blocks"), dict)
        or any(
            not isinstance(key, str) or type(value) is not int or value < 0
            for key, value in quality["excluded_blocks"].items()
        )
    ):
        return False
    for unit in units:
        if not isinstance(unit, dict):
            return False
        page, end = unit.get("page"), unit.get("page_end")
        if (
            not isinstance(unit.get("kind"), str)
            or unit["kind"] not in _UNIT_KINDS
            or not isinstance(unit.get("text"), str)
            or not unit["text"]
            or type(page) is not int
            or not 1 <= page <= pages
            or (end is not None and (type(end) is not int or not page <= end <= pages))
            or not isinstance(unit.get("section"), list)
            or not all(isinstance(s, str) for s in unit["section"])
            or (unit["kind"] == "table" and not isinstance(unit.get("table"), dict))
        ):
            return False
    if (
        quality["characters"] != sum(len(unit["text"]) for unit in units)
        or quality["units"] != dict(Counter(unit["kind"] for unit in units))
        or quality["tables_reconstructed"] != sum(unit["kind"] == "table" for unit in units)
        or len(quality["tables_failed"]) != sum(unit["kind"] == "table_raw" for unit in units)
        or quality["pages_with_text"]
        < len({page for unit in units for page in (unit["page"], unit["page_end"] or unit["page"])})
        or any(
            not isinstance(item, list)
            or len(item) != 3
            or type(item[0]) is not int
            or not 1 <= item[0] <= pages
            or not all(isinstance(value, str) for value in item[1:])
            for item in quality["tables_failed"]
        )
    ):
        return False
    raw_units = (unit for unit in units if unit["kind"] == "table_raw")
    if any(
        entry[0] != unit["page"] or not unit["text"].startswith(entry[1])
        for entry, unit in zip(quality["tables_failed"], raw_units, strict=True)
    ):
        return False
    expected_warnings = quality_warnings(
        pages,
        quality["pages_with_text"],
        quality["characters"],
        quality["ligatures_unresolved"],
        quality["tables_reconstructed"],
        len(quality["tables_failed"]),
    )
    if quality["warnings"] != list(expected_warnings) or quality["degraded"] != bool(
        expected_warnings
    ):
        return False
    max_chars = versions.get("max_chars")
    if type(max_chars) is not int:
        return False
    return _json_bytes(chunks) == _json_bytes(
        _replay_chunks(document, report_id, sha256, max_chars)
    )


@dataclass(frozen=True)
class ProcessedSummary:
    report_id: str
    path: Path
    pages: int
    chunks: int
    tables: int
    tables_failed: int
    ligatures_repaired: int
    degraded: bool
    warnings: tuple[str, ...]
    cached: bool


def _work(args: tuple[str, str, str, int]) -> bytes:
    """Procesa un PDF en un proceso aparte y devuelve el JSON serializado (sin metadatos)."""
    pdf, report_id, sha256, max_chars = args
    document = process_pdf(Path(pdf), report_id, sha256)
    chunks = chunk_document(document, max_chars=max_chars)
    return _json_bytes(
        {"document": document.to_dict(), "chunks": [chunk.to_dict() for chunk in chunks]}
    )


def job_id(record: dict[str, object]) -> str:
    report = record["report"]
    assert isinstance(report, dict)
    return str(report["report_id"])


class ProcessedStore:
    def __init__(self, root: Path, *, max_chars: int = 1400) -> None:
        self.root = root
        self.max_chars = max_chars
        signature = (
            f"cache=3|{PROCESSING_VERSION}|{CHUNKER_VERSION}|{max_chars}|"
            f"pdfminer={metadata.version('pdfminer.six')}"
        )
        self.version = hashlib.sha256(signature.encode()).hexdigest()[:12]

    def path_for(self, report_id: str, sha256: str) -> Path:
        if not re.fullmatch(r"ipm-\d{4}-\d{2}", report_id) or not _SHA.fullmatch(sha256):
            raise CatalogError("Identificador o hash no válido para un documento procesado.")
        return self.root / "processed" / f"{report_id}-{sha256[:16]}-{self.version}.json"

    def _load(self, target: Path, report_id: str, sha256: str) -> dict[str, object] | None:
        """Lee un resultado en caché; un archivo dañado se trata como ausente."""
        try:
            data = json.loads(target.read_bytes())
            if not isinstance(data, dict) or not {"document", "chunks", "report", "pdf"} <= set(
                data
            ):
                return None
            if not _valid_payload(data, report_id, sha256, self._versions()):
                return None
            if data.get("payload_sha256") != _payload_digest(data):
                return None
        except (OSError, ValueError, UnicodeError, TypeError, KeyError, RecursionError):
            return None
        return data

    def _versions(self) -> dict[str, object]:
        return {
            "processing": PROCESSING_VERSION,
            "chunker": CHUNKER_VERSION,
            "max_chars": self.max_chars,
            "pdfminer": metadata.version("pdfminer.six"),
        }

    def ensure(
        self,
        records: list[dict[str, object]],
        *,
        force: bool = False,
        workers: int = 1,
        progress: Callable[[str], None] | None = None,
    ) -> tuple[list[ProcessedSummary], list[tuple[str, str]]]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / ".processing.lock").open("a+b") as lock:
            _lock_exclusive(lock)
            return self._ensure_locked(records, force=force, workers=workers, progress=progress)

    def _ensure_locked(
        self,
        records: list[dict[str, object]],
        *,
        force: bool,
        workers: int,
        progress: Callable[[str], None] | None,
    ) -> tuple[list[ProcessedSummary], list[tuple[str, str]]]:
        """Procesa lo que falte y devuelve (resúmenes, fallos por informe).

        Cada resultado se guarda en cuanto termina; un informe que falla no impide procesar ni
        conservar los demás. Si cambian los metadatos del snapshot, se actualizan sin reprocesar.
        """
        notify = progress or (lambda _: None)
        pending: list[tuple[dict[str, object], Path, tuple[str, str, str, int]]] = []
        failures: list[tuple[str, str]] = []
        for record in records:
            report, pdf = record["report"], record["pdf"]
            assert isinstance(report, dict) and isinstance(pdf, dict)
            report_id, sha256 = str(report["report_id"]), str(pdf["sha256"])
            target = self.path_for(report_id, sha256)
            expected = f"raw/{report_id}-{sha256}.pdf"
            source = self.root / expected
            if (
                pdf.get("path") != expected
                or (self.root / "raw").is_symlink()
                or not source.is_file()
                or source.is_symlink()
            ):
                failures.append((report_id, "falta el PDF seguro en el almacén local"))
                continue
            try:
                with source.open("rb") as handle:
                    if hashlib.file_digest(handle, "sha256").hexdigest() != sha256:
                        failures.append((report_id, "el PDF local no coincide con su hash"))
                        continue
            except OSError as exc:
                failures.append((report_id, f"no se pudo leer el PDF local: {exc}"))
                continue
            cached = None if force else self._load(target, report_id, sha256)
            if cached is not None:
                if cached["report"] != report or cached["pdf"] != pdf:
                    cached["report"], cached["pdf"] = report, pdf
                    _atomic_write(target, _json_bytes(cached))
                continue
            if target.exists():
                notify(f"Caché de {report_id} incompatible o dañada; se reconstruirá.")
            pending.append((record, target, (str(source), report_id, sha256, self.max_chars)))
        if pending:
            notify(f"Procesando {len(pending)} informe(s); cada uno puede tardar unos segundos…")

        def run(item: tuple[dict[str, object], Path, tuple[str, str, str, int]]) -> None:
            record, target, job = item
            notify(f"Procesando {job_id(record)}…")
            try:
                # Proceso aislado: un PDF hostil no puede colgar ni agotar la memoria.
                data = run_isolated(_work, job, timeout=900, memory_mb=3072)
            except IsolationError as exc:
                failures.append((job_id(record), str(exc)))
                notify(f"No se pudo procesar {job_id(record)}: {exc}")
                return
            try:
                payload = json.loads(data)
                if not isinstance(payload, dict):
                    raise ValueError("resultado no es un objeto")
                payload["report"] = record["report"]
                payload["pdf"] = record["pdf"]
                payload["versions"] = self._versions()
                valid = _valid_payload(payload, job[1], job[2], self._versions())
            except (ValueError, UnicodeError, TypeError, KeyError, RecursionError):
                valid = False
            if not valid:
                failures.append((job[1], "el procesamiento produjo un artefacto inválido"))
                return
            try:
                with Path(job[0]).open("rb") as handle:
                    if hashlib.file_digest(handle, "sha256").hexdigest() != job[2]:
                        failures.append((job[1], "el PDF cambió durante el procesamiento"))
                        return
            except OSError as exc:
                failures.append((job[1], f"no se pudo releer el PDF local: {exc}"))
                return
            payload["payload_sha256"] = _payload_digest(payload)
            _atomic_write(target, _json_bytes(payload))
            notify(f"Procesado {job_id(record)}.")

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            list(pool.map(run, pending))
        fresh = {target for _, target, _ in pending}
        summaries = []
        failed = {report_id for report_id, _ in failures}
        for record in records:
            report, pdf = record["report"], record["pdf"]
            assert isinstance(report, dict) and isinstance(pdf, dict)
            if str(report["report_id"]) in failed:
                continue
            target = self.path_for(str(report["report_id"]), str(pdf["sha256"]))
            data = self._load(target, str(report["report_id"]), str(pdf["sha256"]))
            if data is None:
                failures.append((str(report["report_id"]), "el resultado guardado no es legible"))
                continue
            document = data["document"]
            assert isinstance(document, dict)
            quality = document["quality"]
            chunks = data["chunks"]
            assert isinstance(quality, dict) and isinstance(chunks, list)
            summaries.append(
                ProcessedSummary(
                    report_id=str(report["report_id"]),
                    path=target,
                    pages=quality["pages"],
                    chunks=len(chunks),
                    tables=quality["tables_reconstructed"],
                    tables_failed=len(quality["tables_failed"]),
                    ligatures_repaired=quality["ligatures_repaired"],
                    degraded=quality["degraded"],
                    warnings=tuple(quality["warnings"]),
                    cached=target not in fresh,
                )
            )
        return summaries, failures
