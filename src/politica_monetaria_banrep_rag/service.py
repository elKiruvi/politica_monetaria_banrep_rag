"""Servicio de consulta compartido por la CLI, la API y la interfaz web.

Orquesta índice, recuperación con degradación, respuestas verificadas, comparaciones y resúmenes.
No imprime: devuelve resultados con avisos y lanza ``ServiceError`` con un tipo (entrada inválida,
no encontrado o servicio no disponible) para que cada interfaz elija su código de salida o HTTP.
"""

import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

import httpx

from politica_monetaria_banrep_rag.answering import Answer, answer_question, detect_reports
from politica_monetaria_banrep_rag.catalog import Catalog, CatalogError
from politica_monetaria_banrep_rag.comparison import Comparison, compare, detect_periods
from politica_monetaria_banrep_rag.download import PdfDownloader
from politica_monetaria_banrep_rag.embeddings import (
    DEFAULT_OLLAMA_URL,
    EmbeddingError,
    EmbeddingProvider,
    OllamaEmbeddings,
    is_local_url,
)
from politica_monetaria_banrep_rag.generation import (
    DEFAULT_GENERATION_MODEL,
    GenerationError,
    GenerationProvider,
    OllamaGenerator,
)
from politica_monetaria_banrep_rag.index_store import (
    ActiveIndex,
    IndexManager,
    IndexStoreError,
    corpus_status,
)
from politica_monetaria_banrep_rag.retrieval import (
    ReportMeta,
    RetrievalHit,
    SearchFilters,
)
from politica_monetaria_banrep_rag.source import BanrepSource

SEARCH_MODES = ("auto", "lexico", "semantico", "hibrido")
DISCLAIMER = (
    "Proyecto no oficial: no representa al Banco de la República ni es asesoría financiera."
)


class ServiceError(Exception):
    """Error presentable al usuario; ``kind`` es invalid, not_found o unavailable.

    ``warnings`` conserva los avisos reunidos antes del fallo (por ejemplo, el de privacidad)."""

    def __init__(self, kind: str, message: str, warnings: list[str] | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.warnings = list(warnings or [])


@dataclass(frozen=True)
class SearchResult:
    index_id: str
    mode: str
    hits: list[RetrievalHit]
    notes: dict[str, list[str]]  # chunk_id → textos de notas del mismo cuadro
    corpus: dict[str, object]
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class QueryResult:
    index_id: str
    mode: str
    model: str
    detected_reports: tuple[str, ...]
    answer: Answer
    corpus: dict[str, object]
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CompareResult:
    index_id: str
    comparison: Comparison
    table_notes: dict[str, str]
    missing_reports: tuple[str, ...]
    corpus: dict[str, object]
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SummaryResult:
    index_id: str
    report: ReportMeta
    topic: str
    model: str
    answer: Answer
    corpus: dict[str, object]
    warnings: list[str] = field(default_factory=list)
    mode: str = "auto"  # vía de recuperación efectivamente usada


def ollama_url() -> str:
    return os.getenv("BANREP_OLLAMA_URL") or DEFAULT_OLLAMA_URL


def probe_ollama() -> dict[str, object]:
    """Modelos instalados en Ollama (``/api/tags``); no descarga nada."""
    try:
        response = httpx.get(f"{ollama_url()}/api/tags", timeout=httpx.Timeout(3.0))
        models = [m.get("name") for m in response.json().get("models", [])]
    except (httpx.HTTPError, ValueError, AttributeError):
        return {"available": False, "url": ollama_url(), "models": []}
    return {"available": response.status_code == 200, "url": ollama_url(), "models": models}


class RagService:
    def __init__(
        self,
        root: Path,
        *,
        embedder_factory: Callable[[str], EmbeddingProvider] | None = None,
        generator_factory: Callable[[str], GenerationProvider] | None = None,
        generation_model: str | None = None,
        ollama_probe: Callable[[], dict[str, object]] | None = None,
    ) -> None:
        self.root = root
        self.ollama_probe = ollama_probe or probe_ollama
        self.embedder_factory = embedder_factory or (
            lambda model: OllamaEmbeddings(model, base_url=ollama_url())
        )
        self.generator_factory = generator_factory or (
            lambda model: OllamaGenerator(model, base_url=ollama_url())
        )
        self.generation_model = (
            generation_model or os.getenv("BANREP_GENERATION_MODEL") or DEFAULT_GENERATION_MODEL
        )

    # --- índice y estado -------------------------------------------------------------------

    def privacy_warnings(self) -> list[str]:
        url = ollama_url()
        if is_local_url(url):
            return []
        return [f"Ollama no es local ({url}); las consultas y la evidencia saldrán del equipo."]

    def _note_privacy(self, warnings: list[str]) -> None:
        """Solo cuando se va a contactar a Ollama, y una vez por operación."""
        for warning in self.privacy_warnings():
            if warning not in warnings:
                warnings.append(warning)

    def _redact(self, message: str) -> str:
        """Los mensajes al usuario no exponen rutas locales del equipo."""
        # Solo rutas absolutas (la más larga primero): un nombre relativo corto como «data»
        # no debe sustituirse dentro de otras palabras.
        candidates = {str(self.root.resolve()): "<datos>", str(Path.home()): "~"}
        for path in sorted(candidates, key=len, reverse=True):
            if Path(path).is_absolute() and len(path) > 1:
                message = message.replace(path, candidates[path])
        return message

    @contextmanager
    def _guard(self, warnings: list[str]) -> Iterator[None]:
        """Traduce errores a ``ServiceError`` sin rutas y con los avisos reunidos."""
        try:
            yield
        except ServiceError as exc:
            raise ServiceError(
                exc.kind, self._redact(str(exc)), [*warnings, *exc.warnings]
            ) from exc
        except (IndexStoreError, sqlite3.Error, OSError) as exc:
            raise ServiceError(
                "unavailable",
                self._redact(f"No se pudo leer el índice activo ({exc})."),
                warnings,
            ) from exc

    @contextmanager
    def open_index(self) -> Iterator[ActiveIndex]:
        try:
            active = IndexManager(self.root).open_active()
        except IndexStoreError as exc:
            raise ServiceError("unavailable", self._redact(str(exc))) from exc
        try:
            yield active
        finally:
            active.close()

    def _catalog(self) -> Catalog:
        return Catalog(self.root, BanrepSource(), PdfDownloader())

    def corpus(self, active: ActiveIndex) -> dict[str, object]:
        """Correspondencia del índice con el catálogo activo y vigencia de sus fuentes."""
        catalog = self._catalog()
        try:
            with catalog.source.client, catalog.downloader.client:
                current = catalog.current()
                records = current[1]["reports"] if current else []
                assert isinstance(records, list)
                verification = catalog.verification(records) if current else []
        except CatalogError as exc:
            return {"index_matches_catalog": False, "warnings": [str(exc)], "reports": []}
        return corpus_status(active.manifest, current[0] if current else None, verification)

    def status(self) -> dict[str, object]:
        try:
            with self.open_index() as active:
                index: dict[str, object] = {
                    "available": True,
                    "index_id": active.index_id,
                    "chunk_count": active.manifest["chunk_count"],
                    "embedding_model": (active.embedding or {}).get("model"),
                    "created_at": active.manifest["created_at"],
                }
                corpus = self.corpus(active)
        except ServiceError as exc:
            index, corpus = {"available": False, "error": str(exc)}, {}
        ollama = self.ollama_probe()
        models = ollama["models"]
        assert isinstance(models, list)
        generation = self.generation_model
        return {
            "index": index,
            "corpus": corpus,
            "ollama": ollama,
            "generation_model": generation,
            "generation_model_installed": generation in models or f"{generation}:latest" in models,
            "warnings": self.privacy_warnings(),  # avisa aunque aún no se haya consultado
            "disclaimer": DISCLAIMER,
        }

    def reports(self) -> list[dict[str, object]]:
        with self.open_index() as active:
            listed = self.corpus(active).get("reports", [])
            states = {
                str(item["report_id"]): item.get("verification_status")
                for item in (listed if isinstance(listed, list) else [])
                if isinstance(item, dict)
            }
            return [
                {**report.to_dict(), "verification_status": states.get(report.report_id)}
                for report in sorted(
                    active.lexical.reports.values(),
                    key=lambda r: r.reference_period,
                    reverse=True,
                )
            ]

    # --- recuperación --------------------------------------------------------------------

    @staticmethod
    def resolve_mode(active: ActiveIndex, mode: str) -> str:
        if mode not in SEARCH_MODES:
            raise ServiceError("invalid", f"Modo desconocido: {mode}.")
        if mode != "auto":
            return mode
        return "hibrido" if active.embedding is not None else "lexico"

    def _embedder(self, active: ActiveIndex, mode: str) -> EmbeddingProvider | None:
        if mode == "lexico":
            return None
        embedding = active.embedding
        if embedding is None:
            raise IndexStoreError(
                "El índice activo no tiene vectores; ejecute «banrep-rag indexar "
                "--modelo-embeddings <modelo>»."
            )
        if embedding["provider"] != "ollama":
            raise IndexStoreError(f"Proveedor de embeddings no soportado: {embedding['provider']}.")
        return self.embedder_factory(str(embedding["model"]))

    def retrieve(
        self,
        active: ActiveIndex,
        query: str,
        filters: SearchFilters,
        top_k: int,
        requested: str,
        warnings: list[str],
    ) -> tuple[list[RetrievalHit], str]:
        """Recupera con el modo pedido; «auto» degrada a la vía léxica si Ollama falla."""
        mode = self.resolve_mode(active, requested)
        embedder = None
        try:
            try:
                if mode != "lexico":
                    self._note_privacy(warnings)
                embedder = self._embedder(active, mode)
                hits = active.search(query, filters, top_k=top_k, mode=mode, embedder=embedder)
                return hits, mode
            except (IndexStoreError, EmbeddingError) as exc:
                if requested != "auto" or mode == "lexico":
                    raise ServiceError(
                        "unavailable",
                        f"{exc} La vía léxica no necesita Ollama: «--modo lexico».",
                    ) from exc
                warnings.append(f"Vía semántica no disponible ({exc}); se usa la léxica.")
                return active.search(query, filters, top_k=top_k, mode="lexico"), "lexico"
        except ValueError as exc:
            raise ServiceError("invalid", str(exc)) from exc
        finally:
            close = getattr(embedder, "close", None)
            if close is not None:
                close()

    def search(
        self, query: str, filters: SearchFilters, *, top_k: int = 5, mode: str = "auto"
    ) -> SearchResult:
        warnings: list[str] = []
        with self._guard(warnings), self.open_index() as active:
            hits, used = self.retrieve(active, query, filters, top_k, mode, warnings)
            notes = {
                hit.chunk.chunk_id: [note.text for note in active.table_notes(hit.chunk)]
                for hit in hits
            }
            return SearchResult(active.index_id, used, hits, notes, self.corpus(active), warnings)

    # --- respuestas, comparaciones y resúmenes ----------------------------------------------

    def _generate(
        self,
        active: ActiveIndex,
        question: str,
        hits: list[RetrievalHit],
        mode: str,
        model: str,
        scope: str,
    ) -> Answer:
        embedding = active.embedding if mode != "lexico" else None
        generator = self.generator_factory(model)
        try:
            return answer_question(
                question,
                hits,
                active.table_notes,
                generator,
                embedding_model=str(embedding["model"]) if embedding else None,
                scope=scope,
            )
        except GenerationError as exc:
            raise ServiceError(
                "unavailable", f"{exc} Puede consultar la evidencia con la búsqueda."
            ) from exc
        except ValueError as exc:
            raise ServiceError("invalid", str(exc)) from exc
        finally:
            close = getattr(generator, "close", None)
            if close is not None:
                close()

    def ask(
        self,
        question: str,
        filters: SearchFilters,
        *,
        top_k: int = 6,
        mode: str = "auto",
        model: str | None = None,
    ) -> QueryResult:
        warnings: list[str] = []
        model = model or self.generation_model
        with self._guard(warnings), self.open_index() as active:
            detected: tuple[str, ...] = ()
            if not (filters.report_ids or filters.period_from or filters.period_to):
                periods = {r.reference_period: r.report_id for r in active.lexical.reports.values()}
                detected, absent = detect_reports(question, periods)
                if absent:
                    warnings.append(
                        f"Informes nombrados que no están indexados: {', '.join(absent)}."
                    )
                if detected:
                    filters = replace(filters, report_ids=detected)
            hits, used = self.retrieve(active, question, filters, top_k, mode, warnings)
            self._note_privacy(warnings)  # la pregunta y la evidencia van al generador
            answer = self._generate(active, question, hits, used, model, "cited")
            return QueryResult(
                active.index_id, used, model, detected, answer, self.corpus(active), warnings
            )

    def compare(
        self,
        *,
        question: str | None = None,
        reports: tuple[str, ...] = (),
        indicator: str | None = None,
        horizon: str | None = None,
        table: str | None = None,
        include_observed: bool = False,
    ) -> CompareResult:
        warnings: list[str] = []
        with self._guard(warnings), self.open_index() as active:
            indexed = active.lexical.reports
            periods = {r.reference_period: r.report_id for r in indexed.values()}
            missing: tuple[str, ...] = ()
            if reports:
                ids = tuple(dict.fromkeys(periods.get(item, item) for item in reports))
            else:
                ids, missing = detect_periods(question or "", periods)
            absent = f" No están indexados: {', '.join(missing)}." if missing else ""
            if len(ids) < 2:
                raise ServiceError(
                    "invalid",
                    "Indique al menos dos informes indexados (por ejemplo «2025-01» y "
                    f"«2025-07») o nómbrelos en la pregunta.{absent}",
                )
            if missing:
                warnings.append(
                    f"Informes mencionados que no están indexados: {', '.join(missing)}."
                )
            chunks = active.lexical.all_chunks()
            try:
                comparison = compare(
                    chunks,
                    indexed,
                    ids,
                    indicator or question or "",
                    horizon=horizon,
                    table=table,
                    only_projections=not include_observed,
                    explicit_indicator=bool(indicator),
                )
            except ValueError as exc:
                raise ServiceError("invalid", str(exc)) from exc
            tables = {c.chunk_id: c for c in chunks if c.kind == "table"}
            notes: dict[str, str] = {}
            for row in comparison.rows:
                for value in row.values:
                    if value is not None and value.table_id and value.table_id not in notes:
                        table_notes = active.table_notes(tables[value.chunk_id])
                        if table_notes:
                            notes[value.table_id] = " ".join(n.text for n in table_notes)
            return CompareResult(
                active.index_id, comparison, notes, missing, self.corpus(active), warnings
            )

    def summarize(
        self,
        report: str,
        *,
        topic: str | None = None,
        top_k: int = 8,
        mode: str = "auto",
        model: str | None = None,
    ) -> SummaryResult:
        warnings: list[str] = []
        model = model or self.generation_model
        with self._guard(warnings), self.open_index() as active:
            periods = {r.reference_period: r.report_id for r in active.lexical.reports.values()}
            report_id = periods.get(report, report)
            if report_id not in active.lexical.reports:
                raise ServiceError("not_found", f"El informe {report} no está indexado.")
            meta = active.lexical.reports[report_id]
            subject = (
                topic or "mensajes principales sobre inflación, crecimiento y política monetaria"
            )
            question = (
                f"Resume lo que dice el Informe de Política Monetaria de {meta.reference_period} "
                f"sobre: {subject}."
            )
            filters = SearchFilters(report_ids=(report_id,), section=None if topic else "Resumen")
            hits, used = self.retrieve(active, subject, filters, top_k, mode, warnings)
            self._note_privacy(warnings)
            answer = self._generate(active, question, hits, used, model, "all")
            return SummaryResult(
                active.index_id, meta, subject, model, answer, self.corpus(active), warnings, used
            )
