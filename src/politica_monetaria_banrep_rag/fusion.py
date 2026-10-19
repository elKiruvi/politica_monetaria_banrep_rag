"""Fusión de rankings por Reciprocal Rank Fusion (RRF).

Solo se usan posiciones: ``peso / (rrf_k + posición)`` sumado por vía. Las puntuaciones BM25 y
los cosenos no son comparables y nunca se suman. Los empates se resuelven por la mejor posición
en cualquier vía y después por ``chunk_id``, de modo que el orden es determinista.
"""

from dataclasses import dataclass, replace

from politica_monetaria_banrep_rag.retrieval import RetrievalHit


@dataclass(frozen=True)
class FusionConfig:
    rrf_k: int = 60
    lexical_weight: float = 1.0
    semantic_weight: float = 1.0
    candidate_k: int = 50

    def __post_init__(self) -> None:
        if not 1 <= self.rrf_k <= 1000:
            raise ValueError("rrf_k debe estar entre 1 y 1000.")
        if not 1 <= self.candidate_k <= 200:
            raise ValueError("candidate_k debe estar entre 1 y 200.")
        weights = (self.lexical_weight, self.semantic_weight)
        if any(not 0 <= weight <= 10 for weight in weights) or not any(weights):
            raise ValueError("Los pesos deben estar entre 0 y 10 y no ser ambos cero.")


@dataclass
class _Entry:
    hit: RetrievalHit
    score: float = 0.0
    best: int = 0
    lexical: RetrievalHit | None = None
    semantic: RetrievalHit | None = None


def reciprocal_rank_fusion(
    lexical: list[RetrievalHit],
    semantic: list[RetrievalHit],
    config: FusionConfig,
    top_k: int,
) -> list[RetrievalHit]:
    merged: dict[str, _Entry] = {}
    for position, hit in enumerate(lexical, start=1):
        entry = merged.setdefault(hit.chunk.chunk_id, _Entry(hit, best=position))
        entry.score += config.lexical_weight / (config.rrf_k + position)
        entry.best = min(entry.best, position)
        entry.lexical = hit
    for position, hit in enumerate(semantic, start=1):
        entry = merged.setdefault(hit.chunk.chunk_id, _Entry(hit, best=position))
        entry.score += config.semantic_weight / (config.rrf_k + position)
        entry.best = min(entry.best, position)
        entry.semantic = hit
    ordered = sorted(merged.values(), key=lambda e: (-e.score, e.best, e.hit.chunk.chunk_id))
    return [
        replace(
            entry.hit,
            rank=rank,
            lexical_bm25=entry.lexical.lexical_bm25 if entry.lexical else None,
            lexical_rank=entry.lexical.lexical_rank if entry.lexical else None,
            semantic_similarity=entry.semantic.semantic_similarity if entry.semantic else None,
            semantic_rank=entry.semantic.semantic_rank if entry.semantic else None,
            fused_score=entry.score,
        )
        for rank, entry in enumerate(ordered[:top_k], start=1)
    ]
