"""Índices versionados con manifiesto, activación atómica y reversión.

Estructura bajo ``<data>/indexes/``:

- ``<index_id>/manifest.json`` y los artefactos que enumera con su SHA-256. ``index_id`` es el
  SHA-256 del manifiesto: un directorio publicado nunca se modifica.
- ``current.json``: puntero al índice activo y a los anteriores conservados para revertir.

Un índice se construye en un directorio temporal, se valida, se renombra y solo entonces se cambia
el puntero. Un fallo en cualquier paso previo deja intacto el índice activo; los lectores resuelven
el puntero una vez y abren un directorio inmutable, así que nunca combinan artefactos de dos
construcciones.
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BufferedRandom
from pathlib import Path

from politica_monetaria_banrep_rag.catalog import (
    _atomic_write,
    _json_bytes,
    _lock_exclusive,
    _sync_directory,
)
from politica_monetaria_banrep_rag.corpus import Corpus
from politica_monetaria_banrep_rag.lexical import (
    LEXICAL_FORMAT,
    TOKENIZER,
    LexicalIndex,
    LexicalIndexError,
    build_lexical_index,
)
from politica_monetaria_banrep_rag.retrieval import IndexedChunk, RetrievalHit, SearchFilters

MANIFEST_SCHEMA = 1
INDEX_FORMAT = "banrep-index-1"
_ID = re.compile(r"[0-9a-f]{64}")
_LEXICAL_FILE = "lexical.sqlite"


class IndexStoreError(Exception):
    """No hay un índice válido que usar o no se pudo publicar uno nuevo."""


def _sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def build_key(corpus: Corpus) -> str:
    """Todo lo que, si cambia, exige reconstruir: corpus, procesamiento y formatos."""
    material = {
        "index_format": INDEX_FORMAT,
        "lexical": {"format": LEXICAL_FORMAT, "tokenizer": TOKENIZER},
        "catalog_snapshot": corpus.snapshot_id,
        "processing_signature": corpus.processing_signature,
        "chunk_ids_sha256": corpus.chunk_ids_sha256,
        "reports": [[r.report_id, r.pdf_sha256] for r in corpus.reports],
    }
    return hashlib.sha256(_json_bytes(material)).hexdigest()


@dataclass
class ActiveIndex:
    index_id: str
    manifest: dict[str, object]
    lexical: LexicalIndex = field(repr=False)

    def close(self) -> None:
        self.lexical.close()

    def search(
        self, query: str, filters: SearchFilters | None = None, *, top_k: int = 5
    ) -> list[RetrievalHit]:
        return self.lexical.search(query, filters or SearchFilters(), top_k)

    def table_notes(self, chunk: IndexedChunk) -> list[IndexedChunk]:
        return self.lexical.table_notes(chunk)


@dataclass(frozen=True)
class BuildResult:
    index_id: str
    created: bool  # False si el índice activo ya correspondía al corpus
    manifest: dict[str, object]


class IndexManager:
    def __init__(self, root: Path, *, keep: int = 3) -> None:
        if keep < 2:
            raise ValueError("Se deben conservar al menos dos índices para poder revertir.")
        self.root = root
        self.directory = root / "indexes"
        self.keep = keep

    # --- lectura -----------------------------------------------------------------------------

    def _pointer(self) -> dict[str, object] | None:
        path = self.directory / "current.json"
        if not path.exists():
            return None
        try:
            pointer = json.loads(path.read_bytes())
            history = pointer["history"]
            if (
                set(pointer) != {"version", "index", "history"}
                or pointer["version"] != 1
                or not isinstance(pointer["index"], str)
                or not _ID.fullmatch(pointer["index"])
                or not isinstance(history, list)
                or not all(isinstance(item, str) and _ID.fullmatch(item) for item in history)
            ):
                raise ValueError("puntero inválido")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise IndexStoreError("El puntero del índice activo está dañado.") from exc
        return pointer

    def active_id(self) -> str | None:
        pointer = self._pointer()
        return None if pointer is None else str(pointer["index"])

    def open(self, index_id: str) -> ActiveIndex:
        """Abre un índice publicado tras validar manifiesto y artefactos; nunca lo modifica."""
        if not _ID.fullmatch(index_id):
            raise IndexStoreError("Identificador de índice inválido.")
        folder = self.directory / index_id
        try:
            if folder.is_symlink() or not folder.is_dir():
                raise IndexStoreError(f"Falta el índice {index_id[:12]}.")
            data = (folder / "manifest.json").read_bytes()
            if hashlib.sha256(data).hexdigest() != index_id:
                raise IndexStoreError(f"El manifiesto del índice {index_id[:12]} fue alterado.")
            manifest = json.loads(data)
            lexical = manifest["lexical"]
            if (
                manifest["schema_version"] != MANIFEST_SCHEMA
                or manifest["index_format"] != INDEX_FORMAT
                or lexical["format"] != LEXICAL_FORMAT
                or lexical["tokenizer"] != TOKENIZER
                or lexical["file"] != _LEXICAL_FILE
            ):
                raise IndexStoreError(
                    f"El índice {index_id[:12]} usa un formato incompatible; reconstrúyalo."
                )
            path = folder / _LEXICAL_FILE
            if path.is_symlink() or _sha256_file(path) != lexical["sha256"]:
                raise IndexStoreError(f"El índice léxico {index_id[:12]} fue alterado.")
            index = LexicalIndex(
                path,
                chunk_count=int(manifest["chunk_count"]),
                chunk_ids_sha256=str(manifest["chunk_ids_sha256"]),
            )
        except IndexStoreError:
            raise
        except (OSError, ValueError, KeyError, TypeError, LexicalIndexError) as exc:
            raise IndexStoreError(f"El índice {index_id[:12]} no es válido: {exc}") from exc
        return ActiveIndex(index_id, manifest, index)

    def open_active(self) -> ActiveIndex:
        """Resuelve el puntero una vez; si una publicación simultánea podó ese directorio, vuelve
        a leer el puntero (pocas veces) en lugar de mezclar estados."""
        for _ in range(3):
            index_id = self.active_id()
            if index_id is None:
                raise IndexStoreError("No hay índice activo; ejecute «banrep-rag indexar».")
            try:
                return self.open(index_id)
            except IndexStoreError:
                if self.active_id() == index_id:
                    raise
        raise IndexStoreError("El índice activo cambió repetidamente durante la lectura.")

    # --- escritura ---------------------------------------------------------------------------

    def _lock(self) -> BufferedRandom:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = (self.directory / ".index.lock").open("a+b")
        _lock_exclusive(handle)
        return handle

    def build(
        self,
        load: Callable[[], Corpus],
        current_snapshot: Callable[[], str | None],
        *,
        force: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> BuildResult:
        """Construye y activa un índice para el corpus de ``load``.

        ``current_snapshot`` se consulta justo antes de activar: si el catálogo cambió durante la
        construcción, no se publica nada (el índice anterior sigue activo).
        """
        with self._lock():
            for leftover in self.directory.glob(".tmp-*"):
                shutil.rmtree(leftover, ignore_errors=True)  # construcciones interrumpidas
            corpus = load()
            key = build_key(corpus)
            pointer = self._pointer()
            if pointer is not None and not force:
                try:
                    active = self.open(str(pointer["index"]))
                    active.close()
                    if active.manifest.get("build_key") == key:
                        return BuildResult(active.index_id, False, active.manifest)
                except IndexStoreError:
                    pass  # un índice activo inválido se reemplaza
            tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=self.directory))
            try:
                lexical_path = tmp / _LEXICAL_FILE
                build_lexical_index(
                    lexical_path, corpus.reports, corpus.chunks, corpus.chunk_ids_sha256
                )
                with lexical_path.open("rb+") as handle:
                    os.fsync(handle.fileno())
                manifest = {
                    "schema_version": MANIFEST_SCHEMA,
                    "index_format": INDEX_FORMAT,
                    "build_key": key,
                    "created_at": clock().strftime("%Y-%m-%dT%H:%M:%S.%f+00:00"),
                    "catalog_snapshot": corpus.snapshot_id,
                    "selection_mode": corpus.selection_mode,
                    "reports": [
                        {
                            "report_id": r.report_id,
                            "reference_period": r.reference_period,
                            "pdf_sha256": r.pdf_sha256,
                        }
                        for r in corpus.reports
                    ],
                    "processing": {
                        "signature": corpus.processing_signature,
                        "versions": corpus.processing_versions,
                    },
                    "chunk_count": len(corpus.chunks),
                    "chunk_ids_sha256": corpus.chunk_ids_sha256,
                    "lexical": {
                        "format": LEXICAL_FORMAT,
                        "tokenizer": TOKENIZER,
                        "file": _LEXICAL_FILE,
                        "sha256": _sha256_file(lexical_path),
                        "bytes": lexical_path.stat().st_size,
                    },
                    "vector": None,
                }
                data = _json_bytes(manifest)
                index_id = hashlib.sha256(data).hexdigest()
                _atomic_write(tmp / "manifest.json", data)
                final = self.directory / index_id
                if final.exists():
                    shutil.rmtree(tmp)  # mismo contenido ya publicado
                else:
                    tmp.rename(final)
                    _sync_directory(self.directory)
            except BaseException:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
            # Validación del artefacto publicado, tal como lo leerán las consultas.
            self.open(index_id).close()
            if current_snapshot() != corpus.snapshot_id:
                raise IndexStoreError(
                    "El catálogo activo cambió durante la indexación; no se activó el índice "
                    "nuevo. Vuelva a ejecutar «banrep-rag indexar»."
                )
            self._activate(index_id, pointer)
            return BuildResult(index_id, True, manifest)

    def _activate(self, index_id: str, pointer: dict[str, object] | None) -> None:
        history: list[str] = []
        if pointer is not None and pointer["index"] != index_id:
            previous = pointer["history"]
            assert isinstance(previous, list)
            history = [str(pointer["index"]), *previous]
        history = [item for item in dict.fromkeys(history) if item != index_id][: self.keep - 1]
        _atomic_write(
            self.directory / "current.json",
            _json_bytes({"version": 1, "index": index_id, "history": history}),
        )
        self._prune({index_id, *history})

    def _prune(self, keep: set[str]) -> None:
        for folder in self.directory.iterdir():
            if folder.is_dir() and _ID.fullmatch(folder.name) and folder.name not in keep:
                shutil.rmtree(folder, ignore_errors=True)

    def rollback(self) -> str:
        """Activa el índice anterior válido; el actual queda fuera del historial."""
        with self._lock():
            pointer = self._pointer()
            if pointer is None:
                raise IndexStoreError("No hay índice activo que revertir.")
            history = pointer["history"]
            assert isinstance(history, list)
            for position, candidate in enumerate(history):
                try:
                    self.open(candidate).close()
                except IndexStoreError:
                    continue
                remaining = history[position + 1 :]
                _atomic_write(
                    self.directory / "current.json",
                    _json_bytes({"version": 1, "index": candidate, "history": remaining}),
                )
                return str(candidate)
            raise IndexStoreError("No se conserva ningún índice anterior válido.")


def corpus_status(
    manifest: dict[str, object],
    active_snapshot: str | None,
    verification: list[dict[str, object]],
) -> dict[str, object]:
    """Separa la integridad del índice de la vigencia de sus fuentes; no modifica nada.

    ``verification`` es el estado efectivo del catálogo activo (``Catalog.verification``). Un
    índice íntegro puede corresponder a un catálogo anterior o a PDF pendientes de revalidación.
    """
    states = {str(item["report_id"]): str(item["status"]) for item in verification}
    reports = manifest["reports"]
    assert isinstance(reports, list)
    warnings = []
    matches = active_snapshot == manifest["catalog_snapshot"]
    if not matches:
        warnings.append(
            "El índice no corresponde al catálogo activo; ejecute «banrep-rag indexar»."
        )
    if manifest["selection_mode"] == "local_import":
        warnings.append(
            "Corpus de importación local: no equivale a los últimos informes publicados."
        )
    pending = sorted(
        report_id
        for report_id, state in states.items()
        if state not in {"verified", "local_import"}
    )
    if pending:
        warnings.append("Vigencia en la fuente sin confirmar: " + ", ".join(pending) + ".")
    return {
        "index_matches_catalog": matches,
        "selection_mode": manifest["selection_mode"],
        "reports": [
            {
                **item,
                "verification_status": states.get(str(item["report_id"]), "fuera_del_catalogo"),
            }
            for item in reports
            if isinstance(item, dict)
        ],
        "warnings": warnings,
    }
