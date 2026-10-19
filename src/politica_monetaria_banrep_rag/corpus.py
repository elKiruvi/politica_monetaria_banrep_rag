"""Corpus verificado para indexar: el snapshot activo y sus fragmentos procesados íntegros.

Antes de construir un índice se comprueba cada PDF por hash, la caché procesada con las versiones
vigentes y su correspondencia con el snapshot. Un solo informe inválido impide indexar: nunca se
publica un índice con una parte del catálogo.
"""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass

from politica_monetaria_banrep_rag.catalog import Catalog, CatalogError
from politica_monetaria_banrep_rag.processed_store import ProcessedStore
from politica_monetaria_banrep_rag.retrieval import IndexedChunk, InvalidChunkError, ReportMeta


class CorpusError(Exception):
    """El catálogo activo no puede indexarse tal como está."""


@dataclass(frozen=True)
class Corpus:
    snapshot_id: str
    selection_mode: str  # "latest_n" o "local_import"
    processing_signature: str
    processing_versions: dict[str, object]
    reports: tuple[ReportMeta, ...]
    chunks: tuple[IndexedChunk, ...]  # orden estable: informe del snapshot y orden del documento

    @property
    def chunk_ids_sha256(self) -> str:
        return chunk_ids_digest(chunk.chunk_id for chunk in self.chunks)


def chunk_ids_digest(chunk_ids: Iterable[str]) -> str:
    """Huella del conjunto de identificadores, independiente del orden."""
    return hashlib.sha256("\n".join(sorted(chunk_ids)).encode()).hexdigest()


def load_corpus(catalog: Catalog, store: ProcessedStore) -> Corpus:
    """Lee el snapshot activo y devuelve sus fragmentos verificados, o lanza ``CorpusError``."""
    try:
        active = catalog.current()
    except CatalogError as exc:
        raise CorpusError(str(exc)) from exc
    if active is None:
        raise CorpusError("No hay catálogo activo; sincronice o importe informes.")
    snapshot_id, snapshot = active
    records = snapshot["reports"]
    assert isinstance(records, list)
    if not records:
        raise CorpusError("El catálogo activo no contiene informes.")
    selection = snapshot.get("selection") or {"mode": "latest_n"}
    assert isinstance(selection, dict)
    reports: list[ReportMeta] = []
    chunks: list[IndexedChunk] = []
    problems: list[str] = []
    for record in records:
        report = record["report"]
        assert isinstance(report, dict)
        try:
            data = store.load_verified(record)
            meta = ReportMeta.from_record(record)
            raw_chunks = data["chunks"]
            assert isinstance(raw_chunks, list)
            parsed = [IndexedChunk.from_dict(item, pages=meta.pdf_pages) for item in raw_chunks]
        except CatalogError as exc:
            problems.append(str(exc))
            continue
        except (InvalidChunkError, KeyError, TypeError, ValueError) as exc:
            problems.append(f"{report.get('report_id')}: fragmentos inválidos ({exc}).")
            continue
        if not parsed:
            problems.append(f"{meta.report_id}: no tiene fragmentos.")
            continue
        if any(chunk.report_id != meta.report_id for chunk in parsed):
            problems.append(f"{meta.report_id}: contiene fragmentos de otro informe.")
            continue
        tables = {chunk.table_id for chunk in parsed if chunk.kind == "table"}
        if any(chunk.kind == "table_notes" and chunk.table_id not in tables for chunk in parsed):
            problems.append(f"{meta.report_id}: hay notas vinculadas a un cuadro inexistente.")
            continue
        reports.append(meta)
        chunks.extend(parsed)
    if problems:
        raise CorpusError("No se indexará el catálogo: " + " ".join(problems))
    if len({chunk.chunk_id for chunk in chunks}) != len(chunks):
        raise CorpusError("No se indexará el catálogo: hay identificadores de fragmento repetidos.")
    if len({report.report_id for report in reports}) != len(reports):
        raise CorpusError("No se indexará el catálogo: hay informes repetidos en el snapshot.")
    return Corpus(
        snapshot_id=snapshot_id,
        selection_mode=str(selection.get("mode", "latest_n")),
        processing_signature=store.version,
        processing_versions=store.versions(),
        reports=tuple(reports),
        chunks=tuple(chunks),
    )
