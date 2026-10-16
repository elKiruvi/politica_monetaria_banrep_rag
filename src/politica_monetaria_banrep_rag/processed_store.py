"""Almacén de documentos procesados, con caché por hash del PDF y versiones del procesamiento.

Cada archivo de ``processed/`` depende solo de los bytes del PDF y de las versiones del parser y
del fragmentador: volver a procesar el mismo PDF con las mismas versiones no cambia nada. Los
metadatos del informe (título, DOI, URL) se copian del snapshot activo para poder citar.
"""

import hashlib
import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from politica_monetaria_banrep_rag.catalog import CatalogError, _atomic_write, _json_bytes
from politica_monetaria_banrep_rag.chunking import CHUNKER_VERSION, chunk_document
from politica_monetaria_banrep_rag.isolation import IsolationError, run_isolated
from politica_monetaria_banrep_rag.processing import PROCESSING_VERSION, process_pdf

_SHA = re.compile(r"[0-9a-f]{64}")


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
            f"{PROCESSING_VERSION}|{CHUNKER_VERSION}|{max_chars}|"
            f"pdfminer={metadata.version('pdfminer.six')}"
        )
        self.version = hashlib.sha256(signature.encode()).hexdigest()[:12]

    def path_for(self, report_id: str, sha256: str) -> Path:
        if not re.fullmatch(r"ipm-\d{4}-\d{2}", report_id) or not _SHA.fullmatch(sha256):
            raise CatalogError("Identificador o hash no válido para un documento procesado.")
        return self.root / "processed" / f"{report_id}-{sha256[:16]}-{self.version}.json"

    def _load(self, target: Path) -> dict[str, object] | None:
        """Lee un resultado en caché; un archivo dañado se trata como ausente."""
        try:
            data = json.loads(target.read_bytes())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not {"document", "chunks", "report", "pdf"} <= set(data):
            return None
        return data

    def ensure(
        self,
        records: list[dict[str, object]],
        *,
        force: bool = False,
        workers: int = 1,
        progress: Callable[[str], None] | None = None,
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
            cached = None if force else self._load(target)
            if cached is not None:
                if cached["report"] != report or cached["pdf"] != pdf:
                    cached["report"], cached["pdf"] = report, pdf
                    _atomic_write(target, _json_bytes(cached))
                continue
            source = self.root / str(pdf["path"])
            if not source.is_file():
                failures.append((report_id, "falta el PDF en el almacén local"))
                continue
            with source.open("rb") as handle:
                if hashlib.file_digest(handle, "sha256").hexdigest() != sha256:
                    failures.append((report_id, "el PDF local no coincide con su hash"))
                    continue
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
            payload = json.loads(data)
            payload["report"] = record["report"]
            payload["pdf"] = record["pdf"]
            payload["versions"] = {
                "processing": PROCESSING_VERSION,
                "chunker": CHUNKER_VERSION,
                "max_chars": self.max_chars,
                "pdfminer": metadata.version("pdfminer.six"),
            }
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
            data = self._load(target)
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
