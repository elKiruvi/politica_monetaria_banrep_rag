"""Métricas de recuperación con juicios por documento, página y evidencia."""

import json
import math
from pathlib import Path

import pytest
from test_lexical_index import CHUNKS, chunk

from politica_monetaria_banrep_rag.evaluation import (
    EvalQuery,
    Judgment,
    evaluate,
    load_queries,
    matches,
    score,
    unresolved,
)
from politica_monetaria_banrep_rag.retrieval import RetrievalHit, SearchFilters

QRELS = Path(__file__).parents[1] / "evaluacion" / "consultas_recuperacion.json"


def test_relevance_levels_are_progressively_stricter():
    hit = chunk("x", "2025-07", "La inflación total terminaría en el 4,7 %.", page=18)
    assert matches(hit, Judgment("ipm-2025-07", (9,), "4,7 %", 2), "document")
    assert not matches(hit, Judgment("ipm-2025-07", (9,), "4,7 %", 2), "page")
    assert matches(hit, Judgment("ipm-2025-07", (18,), "otra cifra", 2), "page")
    assert not matches(hit, Judgment("ipm-2025-07", (18,), "otra cifra", 2), "evidence")
    # Tildes, mayúsculas y espacios no cambian la evidencia; el informe sí.
    assert matches(hit, Judgment("ipm-2025-07", (18,), "INFLACION  total", 2), "evidence")
    assert not matches(hit, Judgment("ipm-2025-10", (18,), "4,7 %", 2), "document")


def test_scores_match_hand_computation():
    good = chunk("g", "2025-07", "texto A", page=5)
    support = chunk("s", "2025-07", "texto B", page=6)
    noise = chunk("n", "2025-07", "ruido", page=40)
    judgments = (
        Judgment("ipm-2025-07", (5,), "texto A", 2),
        Judgment("ipm-2025-07", (6,), "texto B", 1),
    )
    result = score([noise, good, good, support], judgments, 4, "evidence")
    assert result["precision"] == 3 / 4  # el duplicado cuenta para precisión…
    assert result["recall"] == 1.0  # …pero cada juicio se acredita una vez
    assert result["mrr"] == 1 / 2
    dcg = 3 / math.log2(3) + 1 / math.log2(5)
    idcg = 3 / math.log2(2) + 1 / math.log2(3)
    assert result["ndcg"] == pytest.approx(dcg / idcg)
    # Varios juicios del mismo informe: una lista perfecta vale 1 en nDCG de documento.
    perfect = score([good, support], judgments, 2, "document")
    assert perfect == {"precision": 1.0, "recall": 1.0, "mrr": 1.0, "ndcg": 1.0}
    single = score([good], judgments, 2, "document")
    assert single["recall"] == 1.0 and single["ndcg"] < 1.0
    empty = score([], judgments, 4, "evidence")
    assert empty == {"precision": 0.0, "recall": 0.0, "mrr": 0.0, "ndcg": 0.0}


def test_evaluate_skips_queries_for_missing_reports_and_reports_latency(tmp_path):
    path = tmp_path / "q.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "queries": [
                    {"id": "a", "query": "inflación básica",
                     "relevant": [{"report_id": "ipm-2025-07", "pages": [12],
                                   "contains": "inflación básica", "grade": 2}]},
                    {"id": "b", "query": "x",
                     "relevant": [{"report_id": "ipm-2019-01", "pages": [1], "grade": 2}]},
                    {"id": "c", "query": "bitcoin", "relevant": []},
                ],
            }
        )
    )  # fmt: skip
    queries = load_queries(path)

    def search(text, filters, k):
        found = [c for c in CHUNKS if "inflación básica" in c.text] if "básica" in text else []
        return [RetrievalHit(i, c, None, -1.0) for i, c in enumerate(found[:k], 1)]  # type: ignore[arg-type]

    ticks = iter([0.0, 0.002, 0.0, 0.001])
    result = evaluate(
        search, queries, indexed_reports={"ipm-2025-07"}, k=3, clock=lambda: next(ticks)
    )
    assert result["skipped_missing_reports"] == ["b"]
    assert result["answerable"] == 1 and result["unanswerable_without_results"] == 1
    assert result["metrics"]["evidence"]["mrr"] == 1.0
    assert result["latency_ms"]["samples"] == 2


def test_unresolved_detects_judgments_without_indexed_evidence():
    good = chunk("g", "2025-07", "texto A", page=5)
    query = EvalQuery(
        "q", "x", "general", SearchFilters(),
        (Judgment("ipm-2025-07", (5,), "texto A", 2), Judgment("ipm-2025-07", (5,), "no está", 1),
         Judgment("ipm-2019-01", (1,), "otro informe", 2)),
    )  # fmt: skip
    assert unresolved([query], [good]) == ["q: ipm-2025-07 (5,)"]


def test_repository_queries_are_well_formed():
    queries = load_queries(QRELS)
    assert len(queries) >= 15
    assert {q.query_id for q in queries if not q.judgments} >= {"n01", "n02", "n03"}
    for query in queries:
        for judgment in query.judgments:
            assert judgment.contains, f"{query.query_id} necesita evidencia literal"
            assert all(page >= 1 for page in judgment.pages)
            if query.filters.report_ids:
                assert judgment.report_id in query.filters.report_ids


@pytest.mark.parametrize(
    "item",
    [
        {"id": "a", "query": "x", "relevant": [{"report_id": "ipm-2025-07", "pages": []}]},
        {
            "id": "a",
            "query": "x",
            "relevant": [{"report_id": "ipm-2025-07", "pages": [1], "grade": 3}],
        },  # fmt: skip
        {"id": "a", "query": "x", "filters": {"periodo": "2025"}, "relevant": []},
        {"id": "a", "query": "x", "filters": {"report_ids": "ipm-2025-07"}, "relevant": []},
        {
            "id": "a",
            "query": "x",
            "relevant": [{"report_id": "ipm-2025-07", "pages": [1], "contains": 5}],
        },  # fmt: skip
    ],
)
def test_invalid_query_files_are_rejected(tmp_path, item):
    path = tmp_path / "q.json"
    path.write_text(json.dumps({"version": 1, "queries": [item]}))
    with pytest.raises(ValueError):
        load_queries(path)
