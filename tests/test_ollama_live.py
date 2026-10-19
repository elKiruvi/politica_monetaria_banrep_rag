"""Prueba opcional con Ollama real: ``BANREP_OLLAMA_LIVE=1 uv run --frozen pytest -q -k ollama``.

No descarga modelos; se omite si el servidor o ``embeddinggemma`` no están disponibles.
"""

import os

import numpy as np
import pytest

from politica_monetaria_banrep_rag.embeddings import (
    DEFAULT_EMBEDDING_MODEL,
    EmbeddingError,
    OllamaEmbeddings,
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.getenv("BANREP_OLLAMA_LIVE"), reason="BANREP_OLLAMA_LIVE no definida"
    ),
]


def test_ollama_embeddings_are_normalized_and_untruncated():
    provider = OllamaEmbeddings(DEFAULT_EMBEDDING_MODEL)
    try:
        try:
            identity = provider.identity()
        except EmbeddingError as exc:
            pytest.skip(str(exc))
        vectors = provider.embed_documents(
            [("2.2.1 Inflación", "La inflación total anual se ubicaría en un 4,7 %."),
             ("2.1 Contexto externo", "El precio del petróleo Brent cayó.")]
        )  # fmt: skip
        assert vectors.shape == (2, identity.dimension)
        assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-4)
        query = provider.embed_query("proyección de inflación")
        assert float(vectors[0] @ query) > float(vectors[1] @ query)
        with pytest.raises(EmbeddingError, match="context length"):
            provider.embed_documents([("s", "palabra " * 20000)])
    finally:
        provider.close()
