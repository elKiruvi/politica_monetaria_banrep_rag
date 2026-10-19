"""Proveedores de embeddings. Por defecto, Ollama local; ningún texto sale del equipo.

Un índice vectorial queda ligado al proveedor, al modelo, a su digest en Ollama, a las plantillas
de texto y a la dimensión: si cualquiera cambia, el índice no se usa y hay que reconstruirlo.
"""

import ipaddress
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx
import numpy as np

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
# Elegido con `banrep-rag evaluar` sobre el corpus local (ADR 0008): mejor recuperación híbrida y
# unas 6 veces más rápido que bge-m3 al indexar en una GTX 1660 SUPER.
DEFAULT_EMBEDDING_MODEL = "embeddinggemma"
# Plantillas recomendadas por cada modelo; forman parte de la identidad del índice.
_TEMPLATES = {
    "embeddinggemma": ("title: {section} | text: {text}", "task: search result | query: {query}"),
    "bge-m3": ("{section}\n{text}", "{query}"),
}
_GENERIC_TEMPLATE = ("{section}\n{text}", "{query}")


class EmbeddingError(Exception):
    """El proveedor no está disponible, rechazó un texto o devolvió vectores inválidos."""


@dataclass(frozen=True)
class EmbeddingIdentity:
    provider: str
    model: str
    model_digest: str
    dimension: int
    document_template: str
    query_template: str

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "model_digest": self.model_digest,
            "dimension": self.dimension,
            "document_template": self.document_template,
            "query_template": self.query_template,
        }


class EmbeddingProvider(Protocol):
    def identity(self) -> EmbeddingIdentity: ...

    def embed_documents(
        self, items: Sequence[tuple[str, str]], progress: Callable[[int], None] | None = None
    ) -> np.ndarray:
        """``items`` son pares (sección, texto); devuelve vectores L2-normalizados float32."""
        ...

    def embed_query(self, query: str) -> np.ndarray: ...


def normalize(vectors: np.ndarray, expected_dimension: int | None = None) -> np.ndarray:
    """Valida forma y valores y normaliza por L2; un vector nulo o no finito es un error."""
    array = np.asarray(vectors, dtype=np.float32)
    if array.ndim != 2 or array.shape[1] == 0:
        raise EmbeddingError("El proveedor devolvió vectores con forma inválida.")
    if expected_dimension is not None and array.shape[1] != expected_dimension:
        raise EmbeddingError(
            f"Dimensión incompatible: {array.shape[1]} (se esperaba {expected_dimension})."
        )
    if not np.isfinite(array).all():
        raise EmbeddingError("El proveedor devolvió valores no finitos.")
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    if (norms < 1e-6).any():
        raise EmbeddingError("El proveedor devolvió un vector nulo.")
    return np.ascontiguousarray(array / norms, dtype=np.float32)


def is_local_url(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class OllamaEmbeddings:
    """Cliente de ``/api/embed`` con ``truncate: false``: un texto demasiado largo es un error,
    nunca se recorta en silencio."""

    def __init__(
        self,
        model: str,
        *,
        base_url: str = DEFAULT_OLLAMA_URL,
        batch_size: int = 16,
        timeout: float = 120.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not model or len(model) > 200 or any(c.isspace() for c in model):
            raise ValueError("Nombre de modelo de embeddings inválido.")
        if urlsplit(base_url).scheme not in {"http", "https"}:
            raise ValueError("La URL de Ollama debe ser http o https.")
        if not 1 <= batch_size <= 256:
            raise ValueError("El tamaño de lote debe estar entre 1 y 256.")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.batch_size = batch_size
        self.client = client or httpx.Client(timeout=timeout, trust_env=False)
        family = model.split(":")[0].split("/")[-1]
        self.document_template, self.query_template = _TEMPLATES.get(family, _GENERIC_TEMPLATE)
        self._identity: EmbeddingIdentity | None = None

    def close(self) -> None:
        self.client.close()

    def _missing(self) -> str:
        return (
            f"Ollama no tiene el modelo «{self.model}»; instálelo con «ollama pull {self.model}»."
        )

    def _post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        try:
            response = self.client.post(f"{self.base_url}{path}", json=payload)
        except httpx.HTTPError as exc:
            raise EmbeddingError(
                f"No se pudo contactar a Ollama en {self.base_url} ({type(exc).__name__}). "
                "¿Está en ejecución («ollama serve»)?"
            ) from exc
        if response.status_code == 404:
            raise EmbeddingError(self._missing())
        if response.status_code != 200:
            detail = response.text[:200].replace("\n", " ")
            raise EmbeddingError(f"Ollama respondió {response.status_code}: {detail}")
        try:
            data = response.json()
        except ValueError as exc:
            raise EmbeddingError("Ollama devolvió una respuesta que no es JSON.") from exc
        if not isinstance(data, dict):
            raise EmbeddingError("Ollama devolvió una respuesta inesperada.")
        return data

    def _digest(self) -> str:
        try:
            response = self.client.get(f"{self.base_url}/api/tags")
            models = response.json().get("models", []) if response.status_code == 200 else []
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise EmbeddingError(
                f"No se pudo contactar a Ollama en {self.base_url}. ¿Está en ejecución "
                "(«ollama serve»)?"
            ) from exc
        wanted = {self.model, f"{self.model}:latest"}
        for entry in models if isinstance(models, list) else []:
            if isinstance(entry, dict) and entry.get("name") in wanted:
                digest = entry.get("digest")
                if isinstance(digest, str) and digest:
                    return digest
        raise EmbeddingError(self._missing())

    def _embed(self, texts: list[str], dimension: int | None) -> np.ndarray:
        data = self._post("/api/embed", {"model": self.model, "input": texts, "truncate": False})
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise EmbeddingError("Ollama devolvió una cantidad de vectores distinta a la pedida.")
        try:
            return normalize(np.array(vectors, dtype=np.float32), dimension)
        except (ValueError, TypeError) as exc:
            raise EmbeddingError("Ollama devolvió vectores no numéricos.") from exc

    def identity(self) -> EmbeddingIdentity:
        """Consulta el digest del modelo y su dimensión real (una sola vez por instancia)."""
        if self._identity is None:
            digest = self._digest()
            probe = self._embed([self.query_template.format(query="inflación")], None)
            self._identity = EmbeddingIdentity(
                provider="ollama",
                model=self.model,
                model_digest=digest,
                dimension=int(probe.shape[1]),
                document_template=self.document_template,
                query_template=self.query_template,
            )
        return self._identity

    def embed_documents(
        self, items: Sequence[tuple[str, str]], progress: Callable[[int], None] | None = None
    ) -> np.ndarray:
        dimension = self.identity().dimension
        texts = [self.document_template.format(section=s, text=t) for s, t in items]
        parts = []
        for start in range(0, len(texts), self.batch_size):
            parts.append(self._embed(texts[start : start + self.batch_size], dimension))
            if progress:
                progress(min(start + self.batch_size, len(texts)))
        if not parts:
            return np.zeros((0, dimension), dtype=np.float32)
        return np.vstack(parts)

    def embed_query(self, query: str) -> np.ndarray:
        identity = self.identity()
        return self._embed([self.query_template.format(query=query)], identity.dimension)[0]
