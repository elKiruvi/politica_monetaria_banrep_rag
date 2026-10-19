"""Índice vectorial exacto con FAISS (producto interno sobre vectores L2-normalizados = coseno).

Los vectores se guardan como ``.npy`` sin objetos de Python (``allow_pickle=False``) y el índice
FAISS se reconstruye en memoria al abrirlos: no se deserializa ningún formato ejecutable, y la
huella SHA-256 del archivo se verifica contra el manifiesto antes de leerlo. La fila *i* del
archivo corresponde al fragmento con ``rowid`` *i + 1* del índice léxico.
"""

from pathlib import Path

import faiss
import numpy as np

VECTOR_FORMAT = "npy-flatip-1"
INDEX_TYPE = "faiss.IndexFlatIP"


class VectorIndexError(Exception):
    """Los vectores faltan, están dañados o no corresponden al manifiesto."""


def save_vectors(path: Path, vectors: np.ndarray) -> None:
    if path.exists():
        raise VectorIndexError("La ruta de los vectores ya existe.")
    with path.open("xb") as handle:
        np.save(handle, np.ascontiguousarray(vectors, dtype=np.float32), allow_pickle=False)


class VectorIndex:
    def __init__(self, path: Path, *, count: int, dimension: int) -> None:
        if not path.is_file() or path.is_symlink():
            raise VectorIndexError("Faltan los vectores del índice.")
        try:
            vectors = np.load(path, allow_pickle=False)
        except (OSError, ValueError) as exc:
            raise VectorIndexError(f"Los vectores no son legibles ({exc}).") from exc
        if vectors.dtype != np.float32 or vectors.shape != (count, dimension):
            raise VectorIndexError(
                f"Vectores con forma {vectors.shape} y tipo {vectors.dtype}; "
                f"el manifiesto indica ({count}, {dimension}) float32."
            )
        norms = np.linalg.norm(vectors, axis=1) if count else np.ones(0)
        if not np.isfinite(vectors).all() or not np.allclose(norms, 1.0, atol=1e-3):
            raise VectorIndexError("Los vectores guardados no están normalizados.")
        self.dimension = dimension
        self._index = faiss.IndexFlatIP(dimension)
        self._index.add(vectors)

    def search(
        self, query: np.ndarray, limit: int, allowed_rowids: list[int] | None = None
    ) -> list[tuple[int, float]]:
        """Devuelve (rowid, similitud coseno) ordenados; ``allowed_rowids`` aplica los filtros."""
        vector = np.asarray(query, dtype=np.float32).reshape(1, -1)
        if vector.shape[1] != self.dimension:
            raise VectorIndexError(
                f"La consulta tiene dimensión {vector.shape[1]}; el índice usa {self.dimension}."
            )
        if allowed_rowids is not None and not allowed_rowids:
            return []
        params = None
        if allowed_rowids is not None:
            positions = np.array([rowid - 1 for rowid in allowed_rowids], dtype=np.int64)
            selector = faiss.IDSelectorBatch(positions)
            params = faiss.SearchParameters()
            params.sel = selector
        k = min(limit, self._index.ntotal)
        if k == 0:
            return []
        scores, positions = self._index.search(vector, k, params=params)
        return [
            (int(position) + 1, float(score))
            for score, position in zip(scores[0], positions[0], strict=True)
            if position >= 0
        ]
