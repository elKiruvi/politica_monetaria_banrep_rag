"""Proveedores de generación. Por defecto, Ollama local con ``qwen3:4b-instruct``.

El generador es independiente del índice: cambiarlo no exige reindexar. Nunca descarga modelos;
si falta, indica el comando de instalación.
"""

import json
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from politica_monetaria_banrep_rag.embeddings import DEFAULT_OLLAMA_URL

DEFAULT_GENERATION_MODEL = "qwen3:4b-instruct"


class GenerationError(Exception):
    """El modelo de generación no está disponible o devolvió una respuesta inválida."""


class _InvalidJson(Exception):
    """La salida del modelo no es un objeto JSON."""


@dataclass(frozen=True)
class GeneratorInfo:
    provider: str
    model: str
    model_digest: str


class GenerationProvider(Protocol):
    def info(self) -> GeneratorInfo: ...

    def generate_json(self, system: str, user: str, schema: dict[str, object]) -> dict[str, object]:
        """Devuelve el objeto JSON producido por el modelo, ajustado a ``schema``."""
        ...


class OllamaGenerator:
    """Cliente de ``/api/chat`` con salida JSON y temperatura 0.

    Primero ``format: "json"``; si la salida no es JSON, un reintento con la gramática del
    esquema. Con ``qwen3:4b-instruct`` en Ollama 0.33.2 cada modo falló en casos que el otro
    resolvía (salida vacía hasta agotar los tokens; ADR 0009). El esquema se comprueba siempre
    después, en ``answering.verify``.
    """

    def __init__(
        self,
        model: str = DEFAULT_GENERATION_MODEL,
        *,
        base_url: str = DEFAULT_OLLAMA_URL,
        timeout: float = 180.0,
        context_tokens: int = 8192,
        max_output_tokens: int = 600,
        client: httpx.Client | None = None,
    ) -> None:
        if not model or len(model) > 200 or any(c.isspace() for c in model):
            raise ValueError("Nombre de modelo de generación inválido.")
        if urlsplit(base_url).scheme not in {"http", "https"}:
            raise ValueError("La URL de Ollama debe ser http o https.")
        if not 2048 <= context_tokens <= 32768:
            raise ValueError("El contexto debe estar entre 2048 y 32768 tokens.")
        self.model = model if ":" in model.rsplit("/", 1)[-1] else f"{model}:latest"
        self.base_url = base_url.rstrip("/")
        self.context_tokens = context_tokens
        # Límite de salida: la respuesta pedida es breve y un JSON que no termina no sirve.
        self.max_output_tokens = max_output_tokens
        self.timeout = timeout
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(timeout, connect=5.0), trust_env=False
        )
        self._info: GeneratorInfo | None = None

    def close(self) -> None:
        self.client.close()

    def _unavailable(self, exc: Exception | None = None) -> GenerationError:
        detail = f" ({type(exc).__name__})" if exc else ""
        return GenerationError(
            f"No se pudo contactar a Ollama en {self.base_url}{detail}. ¿Está en ejecución "
            "(«ollama serve»)?"
        )

    def info(self) -> GeneratorInfo:
        if self._info is None:
            try:
                response = self.client.get(f"{self.base_url}/api/tags")
            except httpx.HTTPError as exc:
                raise self._unavailable(exc) from exc
            if response.status_code != 200:
                raise GenerationError(
                    f"Ollama respondió {response.status_code} al listar los modelos instalados."
                )
            try:
                models = response.json().get("models", [])
            except (ValueError, AttributeError) as exc:
                raise GenerationError("Ollama devolvió una lista de modelos inválida.") from exc
            for entry in models if isinstance(models, list) else []:
                if isinstance(entry, dict) and entry.get("name") == self.model:
                    self._info = GeneratorInfo("ollama", self.model, str(entry.get("digest", "")))
                    break
            else:
                raise GenerationError(
                    f"Ollama no tiene el modelo «{self.model}»; instálelo con "
                    f"«ollama pull {self.model}» (varios GB; no se descarga automáticamente)."
                )
        return self._info

    def generate_json(self, system: str, user: str, schema: dict[str, object]) -> dict[str, object]:
        """Pide JSON libre y, si no es válido, reintenta una vez con la gramática del esquema."""
        self.info()
        try:
            return self._chat(system, user, "json")
        except _InvalidJson:
            try:
                return self._chat(system, user, schema)
            except _InvalidJson as exc:
                raise GenerationError(
                    "El modelo no devolvió un JSON válido (¿respuesta truncada por el límite de "
                    f"{self.max_output_tokens} tokens?)."
                ) from exc

    def _chat(self, system: str, user: str, output_format: object) -> dict[str, object]:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "format": output_format,
            "options": {
                "temperature": 0,
                "seed": 0,
                "num_ctx": self.context_tokens,
                "num_predict": self.max_output_tokens,
            },
        }
        try:
            response = self.client.post(f"{self.base_url}/api/chat", json=payload)
        except httpx.TimeoutException as exc:
            if isinstance(exc, httpx.ConnectTimeout):
                raise self._unavailable(exc) from exc
            raise GenerationError(
                f"El modelo no terminó de responder en {self.timeout:.0f} s."
            ) from exc
        except httpx.HTTPError as exc:
            raise self._unavailable(exc) from exc
        if response.status_code != 200:
            detail = response.text[:200].replace("\n", " ")
            raise GenerationError(f"Ollama respondió {response.status_code}: {detail}")
        try:
            result = json.loads(response.json()["message"]["content"])
        except (ValueError, KeyError, TypeError) as exc:
            raise _InvalidJson from exc
        if not isinstance(result, dict):
            raise _InvalidJson
        return result
