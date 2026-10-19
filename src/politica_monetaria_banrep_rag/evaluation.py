"""Evaluación de la recuperación con juicios de relevancia verificados a mano.

Cada juicio indica un informe, sus páginas físicas y, opcionalmente, un fragmento literal que debe
aparecer en el texto recuperado. La relevancia se mide en tres niveles:

- ``document``: el resultado es del informe esperado.
- ``page``: además, su rango de páginas incluye alguna de las páginas juzgadas.
- ``evidence``: además, contiene el texto literal del juicio (sin distinguir tildes ni espacios).

Un juicio se acredita una sola vez (recall y ganancia de nDCG); la precisión cuenta todos los
resultados que satisfacen algún juicio. Los juicios no dependen de ``chunk_id``, que cambia con
las versiones del fragmentador.
"""

import json
import math
import re
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from politica_monetaria_banrep_rag.retrieval import IndexedChunk, RetrievalHit, SearchFilters, fold

LEVELS = ("document", "page", "evidence")


@dataclass(frozen=True)
class Judgment:
    report_id: str
    pages: tuple[int, ...]
    contains: str | None
    grade: int  # 2: responde directamente; 1: evidencia de apoyo


@dataclass(frozen=True)
class EvalQuery:
    query_id: str
    query: str
    category: str
    filters: SearchFilters
    judgments: tuple[Judgment, ...]  # vacío: la colección no contiene la respuesta


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", fold(text)).strip()


def _filters(raw: dict[str, object]) -> SearchFilters:
    allowed = {"report_ids", "period_from", "period_to", "section", "kinds", "evidence_qualities"}
    if not set(raw) <= allowed:
        raise ValueError(f"Filtros desconocidos: {sorted(set(raw) - allowed)}.")
    lists: dict[str, tuple[str, ...]] = {}
    for key in ("report_ids", "kinds", "evidence_qualities"):
        value = raw.get(key) or []
        if not isinstance(value, list):
            raise ValueError(f"El filtro {key} debe ser una lista.")
        lists[key] = tuple(str(item) for item in value)
    texts = {key: None if raw.get(key) is None else str(raw[key]) for key in
             ("period_from", "period_to", "section")}  # fmt: skip
    return SearchFilters(
        report_ids=lists["report_ids"],
        period_from=texts["period_from"],
        period_to=texts["period_to"],
        section=texts["section"],
        kinds=lists["kinds"],
        evidence_qualities=lists["evidence_qualities"],
    )


def load_queries(path: Path) -> list[EvalQuery]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("queries"), list):
        raise ValueError("Formato de consultas de evaluación no reconocido.")
    queries = []
    for item in data["queries"]:
        judgments = tuple(
            Judgment(
                report_id=str(entry["report_id"]),
                pages=tuple(int(page) for page in entry["pages"]),
                contains=entry.get("contains"),
                grade=int(entry.get("grade", 1)),
            )
            for entry in item.get("relevant", [])
        )
        if any(not j.pages or j.grade not in {1, 2} for j in judgments):
            raise ValueError(f"Juicio inválido en {item['id']}.")
        queries.append(
            EvalQuery(
                query_id=str(item["id"]),
                query=str(item["query"]),
                category=str(item.get("category", "general")),
                filters=_filters(item.get("filters", {})),
                judgments=judgments,
            )
        )
    if len({q.query_id for q in queries}) != len(queries):
        raise ValueError("Identificadores de consulta repetidos.")
    return queries


def matches(chunk: IndexedChunk, judgment: Judgment, level: str) -> bool:
    if chunk.report_id != judgment.report_id:
        return False
    if level == "document":
        return True
    if not any(chunk.page_start <= page <= chunk.page_end for page in judgment.pages):
        return False
    if level == "page" or judgment.contains is None:
        return True
    return _normalized(judgment.contains) in _normalized(chunk.text)


def score(chunks: list[IndexedChunk], judgments: tuple[Judgment, ...], k: int, level: str):
    """Precision@k, Recall@k, MRR y nDCG@k de una lista ordenada."""
    top = chunks[:k]
    credited: set[int] = set()
    relevant_hits = 0
    first: int | None = None
    dcg = 0.0
    for position, chunk in enumerate(top, start=1):
        matched = [i for i, j in enumerate(judgments) if matches(chunk, j, level)]
        if not matched:
            continue
        relevant_hits += 1
        first = first or position
        fresh = [i for i in matched if i not in credited]
        if fresh:
            gain = max(judgments[i].grade for i in fresh)
            credited.update(fresh)
            dcg += (2**gain - 1) / math.log2(position + 1)
    ideal = sorted((j.grade for j in judgments), reverse=True)[:k]
    idcg = sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))
    return {
        "precision": relevant_hits / k,
        "recall": len(credited) / len(judgments),
        "mrr": 1 / first if first else 0.0,
        "ndcg": dcg / idcg if idcg else 0.0,
    }


def unresolved(queries: list[EvalQuery], chunks: list[IndexedChunk]) -> list[str]:
    """Juicios de evidencia que ningún fragmento indexado satisface (consultas mal escritas o
    procesamiento cambiado); los informes ausentes del índice no se consideran aquí."""
    reports = {chunk.report_id for chunk in chunks}
    problems = []
    for query in queries:
        for judgment in query.judgments:
            if judgment.report_id in reports and not any(
                matches(chunk, judgment, "evidence") for chunk in chunks
            ):
                problems.append(f"{query.query_id}: {judgment.report_id} {judgment.pages}")
    return problems


def evaluate(
    search: Callable[[str, SearchFilters, int], list[RetrievalHit]],
    queries: list[EvalQuery],
    *,
    indexed_reports: set[str],
    k: int = 5,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, object]:
    if not 1 <= k <= 50:
        raise ValueError("k debe estar entre 1 y 50.")
    rows = []
    latencies = []
    skipped = []
    for query in queries:
        if any(j.report_id not in indexed_reports for j in query.judgments):
            skipped.append(query.query_id)  # el índice no contiene todos los informes juzgados
            continue
        start = clock()
        hits = search(query.query, query.filters, k)
        latencies.append((clock() - start) * 1000)
        chunks = [hit.chunk for hit in hits]
        row: dict[str, object] = {
            "id": query.query_id,
            "category": query.category,
            "answerable": bool(query.judgments),
            "results": len(hits),
            "top": [
                f"{c.report_id} p{c.page_start}"
                + (f"-{c.page_end}" if c.page_end != c.page_start else "")
                for c in chunks
            ],
        }
        if query.judgments:
            row["metrics"] = {level: score(chunks, query.judgments, k, level) for level in LEVELS}
        rows.append(row)
    answerable = [row for row in rows if row["answerable"]]
    summary: dict[str, dict[str, float]] = {}
    for level in LEVELS:
        summary[level] = {
            name: round(
                statistics.fmean(row["metrics"][level][name] for row in answerable),  # type: ignore[index]
                4,
            )
            if answerable
            else 0.0
            for name in ("precision", "recall", "mrr", "ndcg")
        }
    ordered = sorted(latencies)
    return {
        "k": k,
        "queries": len(rows),
        "answerable": len(answerable),
        "unanswerable_without_results": sum(
            1 for row in rows if not row["answerable"] and row["results"] == 0
        ),
        "skipped_missing_reports": skipped,
        "metrics": summary,
        "latency_ms": {
            "p50": round(statistics.median(ordered), 2) if ordered else None,
            "p95": round(ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)], 2)
            if ordered
            else None,
            "samples": len(ordered),
        },
        "per_query": rows,
    }
