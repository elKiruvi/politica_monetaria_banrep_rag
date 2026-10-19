"""Vía semántica (FAISS), proveedor Ollama simulado y fusión RRF, sin red ni modelos."""

import hashlib
import json
import re

import httpx
import numpy as np
import pytest
import test_index_store
from test_index_store import build, corpus, run_cli
from test_lexical_index import CHUNKS, chunk

from politica_monetaria_banrep_rag import index_store
from politica_monetaria_banrep_rag.embeddings import (
    EmbeddingError,
    EmbeddingIdentity,
    OllamaEmbeddings,
    is_local_url,
    normalize,
)
from politica_monetaria_banrep_rag.fusion import FusionConfig, reciprocal_rank_fusion
from politica_monetaria_banrep_rag.index_store import IndexManager, IndexStoreError, build_key
from politica_monetaria_banrep_rag.retrieval import RetrievalHit, SearchFilters, fold
from politica_monetaria_banrep_rag.vector import VectorIndex, VectorIndexError, save_vectors

catalog_root = test_index_store.catalog_root  # fixture compartida


class FakeEmbedder:
    """Bolsa de palabras con hash: determinista y suficiente para probar la recuperación."""

    def __init__(self, dimension: int = 64, digest: str = "sha256:uno", model: str = "falso"):
        self.dimension = dimension
        self.digest = digest
        self.model = model
        self.document_calls = 0
        self.fail = False

    def close(self) -> None:
        pass

    def identity(self) -> EmbeddingIdentity:
        return EmbeddingIdentity("ollama", self.model, self.digest, self.dimension, "{text}", "{q}")

    def _vector(self, text: str) -> np.ndarray:
        vector = np.zeros(self.dimension, dtype=np.float32)
        for word in re.findall(r"\w+", fold(text)):
            vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % self.dimension] += 1
        vector[0] += 0.01  # nunca nulo
        return vector

    def embed_documents(self, items, progress=None):
        self.document_calls += 1
        if self.fail:
            raise EmbeddingError("Ollama no responde")
        return normalize(np.array([self._vector(text) for _, text in items]))

    def embed_query(self, query):
        return normalize(self._vector(query)[None, :])[0]


def semantic_build(manager, embedder=None, value=None, **kwargs):
    embedder = embedder or FakeEmbedder()
    value = value or corpus()
    return manager.build(lambda: value, lambda: value.snapshot_id, embedder=embedder, **kwargs)


# --- normalización y proveedor Ollama -----------------------------------------------------------


@pytest.mark.parametrize(
    "vectors,error",
    [
        (np.zeros((2, 3)), "nulo"),
        (np.array([[1.0, np.nan, 0.0]]), "no finitos"),
        (np.ones((2,)), "forma"),
        (np.ones((1, 4)), "Dimensión"),
    ],
)
def test_normalize_rejects_invalid_vectors(vectors, error):
    with pytest.raises(EmbeddingError, match=error):
        normalize(vectors, expected_dimension=3)


def test_normalize_produces_unit_float32_rows():
    result = normalize(np.array([[3.0, 4.0], [0.0, 2.0]]))
    assert result.dtype == np.float32
    assert np.allclose(np.linalg.norm(result, axis=1), 1.0)


def ollama(handler, model="embeddinggemma", batch_size=2):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OllamaEmbeddings(model, batch_size=batch_size, client=client)


def fake_server(dimension=3, digest="sha256:abc", models=("embeddinggemma:latest",), seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": name, "digest": digest} for name in models]}
            )
        body = json.loads(request.content)
        if seen is not None:
            seen.append(body)
        if body["model"] + ":latest" not in models and body["model"] not in models:
            return httpx.Response(404, json={"error": "model not found"})
        vectors = [[float(len(text))] + [1.0] * (dimension - 1) for text in body["input"]]
        return httpx.Response(200, json={"embeddings": vectors})

    return handler


def test_ollama_identity_batches_templates_and_no_truncation():
    seen = []
    provider = ollama(fake_server(seen=seen))
    identity = provider.identity()
    assert identity == EmbeddingIdentity(
        "ollama",
        "embeddinggemma",
        "sha256:abc",
        3,
        "title: {section} | text: {text}",
        "task: search result | query: {query}",
    )
    vectors = provider.embed_documents([("2.1", "a"), ("2.2", "b"), ("2.3", "c {x}")])
    assert vectors.shape == (3, 3) and np.allclose(np.linalg.norm(vectors, axis=1), 1)
    batches = [body["input"] for body in seen[1:]]
    assert batches == [
        ["title: 2.1 | text: a", "title: 2.2 | text: b"],
        ["title: 2.3 | text: c {x}"],  # las llaves del texto no se interpretan
    ]
    assert all(body["truncate"] is False for body in seen)
    provider.embed_query("inflación")
    assert seen[-1]["input"] == ["task: search result | query: inflación"]


def test_ollama_errors_are_explicit_in_spanish():
    def down(request):
        raise httpx.ConnectError("rechazada")

    with pytest.raises(EmbeddingError, match="ollama serve"):
        ollama(down).identity()
    with pytest.raises(EmbeddingError, match="ollama pull bge-m3"):
        ollama(fake_server(), model="bge-m3").identity()

    def too_long(request):
        if request.url.path == "/api/tags":
            return fake_server()(request)
        return httpx.Response(400, json={"error": "the input length exceeds the context length"})

    with pytest.raises(EmbeddingError, match="exceeds the context length"):
        ollama(too_long).identity()

    def wrong_count(request):
        if request.url.path == "/api/tags":
            return fake_server()(request)
        return httpx.Response(200, json={"embeddings": []})

    with pytest.raises(EmbeddingError, match="cantidad"):
        ollama(wrong_count).identity()


def test_ollama_dimension_change_between_batches_is_an_error():
    state = {"dimension": 3}

    def handler(request):
        if request.url.path == "/api/tags":
            return fake_server()(request)
        body = json.loads(request.content)
        response = httpx.Response(
            200, json={"embeddings": [[1.0] * state["dimension"] for _ in body["input"]]}
        )
        state["dimension"] = 4
        return response

    provider = ollama(handler)
    provider.identity()
    with pytest.raises(EmbeddingError, match="Dimensión incompatible"):
        provider.embed_documents([("s", "a")])


@pytest.mark.parametrize(
    "url,local",
    [
        ("http://127.0.0.1:11434", True),
        ("http://localhost:11434", True),
        ("http://[::1]:11434", True),
        ("http://192.168.1.5:11434", False),
        ("https://ollama.example.com", False),
    ],
)
def test_local_url_detection(url, local):
    assert is_local_url(url) is local


def test_provider_configuration_is_validated():
    with pytest.raises(ValueError):
        OllamaEmbeddings("modelo con espacios")
    with pytest.raises(ValueError):
        OllamaEmbeddings("bge-m3", base_url="file:///etc/passwd")


# --- índice vectorial -----------------------------------------------------------------------------


def unit_rows(count: int, dimension: int) -> np.ndarray:
    rows = np.random.default_rng(7).normal(size=(count, dimension)).astype(np.float32)
    return rows / np.linalg.norm(rows, axis=1, keepdims=True)


def test_vector_index_validates_file_and_query(tmp_path):
    path = tmp_path / "v.npy"
    save_vectors(path, unit_rows(5, 4))
    with pytest.raises(VectorIndexError, match="forma"):
        VectorIndex(path, count=6, dimension=4)
    index = VectorIndex(path, count=5, dimension=4)
    with pytest.raises(VectorIndexError, match="dimensión"):
        index.search(np.ones(3), 2)
    assert index.search(unit_rows(5, 4)[2], 1)[0][0] == 3  # rowid = fila + 1
    assert [rowid for rowid, _ in index.search(unit_rows(5, 4)[2], 5, [1, 5])] in ([1, 5], [5, 1])
    assert index.search(unit_rows(5, 4)[2], 5, []) == []
    with pytest.raises(VectorIndexError, match="ya existe"):
        save_vectors(path, unit_rows(5, 4))


def test_vector_index_rejects_pickles_and_unnormalized_rows(tmp_path):
    pickled = tmp_path / "p.npy"
    np.save(pickled, np.array([{"a": 1}], dtype=object), allow_pickle=True)
    with pytest.raises(VectorIndexError, match="legibles"):
        VectorIndex(pickled, count=1, dimension=1)
    raw = tmp_path / "r.npy"
    np.save(raw, np.full((2, 3), 2.0, dtype=np.float32))
    with pytest.raises(VectorIndexError, match="normalizados"):
        VectorIndex(raw, count=2, dimension=3)


# --- índice completo ------------------------------------------------------------------------------


def test_semantic_build_records_model_in_manifest(tmp_path):
    manager = IndexManager(tmp_path)
    result = semantic_build(manager)
    vector = result.manifest["vector"]
    assert vector["model"] == "falso" and vector["dimension"] == 64
    assert vector["normalization"] == "l2" and vector["distance_metric"] == "inner_product"
    assert vector["index_type"] == "faiss.IndexFlatIP"
    active = manager.open_active()
    try:
        hits = active.search(
            "inflación básica regulados", mode="semantico", embedder=FakeEmbedder(), top_k=1
        )
        assert hits[0].chunk.text.startswith("La inflación básica")
        assert hits[0].semantic_similarity > 0 and hits[0].lexical_bm25 is None
    finally:
        active.close()


def test_unchanged_semantic_index_does_not_recompute_embeddings(tmp_path):
    manager = IndexManager(tmp_path)
    embedder = FakeEmbedder()
    first = semantic_build(manager, embedder)
    second = semantic_build(manager, embedder)
    assert not second.created and second.index_id == first.index_id
    assert embedder.document_calls == 1


@pytest.mark.parametrize(
    "other",
    [FakeEmbedder(digest="sha256:dos"), FakeEmbedder(dimension=32), FakeEmbedder(model="otro")],
)
def test_model_change_requires_new_index_and_is_rejected_at_query_time(tmp_path, other):
    manager = IndexManager(tmp_path)
    first = semantic_build(manager)
    assert build_key(corpus(), other.identity()) != first.manifest["build_key"]
    active = manager.open_active()
    try:
        with pytest.raises(IndexStoreError, match="reconstruya"):
            active.search("inflación", mode="semantico", embedder=other)
    finally:
        active.close()
    assert semantic_build(manager, other).created


def test_lexical_only_index_cannot_answer_semantic_queries(tmp_path):
    manager = IndexManager(tmp_path)
    build(manager)
    active = manager.open_active()
    try:
        with pytest.raises(IndexStoreError, match="no tiene vectores"):
            active.search("inflación", mode="semantico", embedder=FakeEmbedder())
        with pytest.raises(IndexStoreError, match="no tiene vectores"):
            active.search("inflación", mode="hibrido", embedder=FakeEmbedder())
    finally:
        active.close()


def test_embedding_failure_keeps_previous_index(tmp_path):
    manager = IndexManager(tmp_path)
    first = build(manager)
    failing = FakeEmbedder()
    failing.fail = True
    with pytest.raises(EmbeddingError):
        semantic_build(manager, failing)
    assert manager.active_id() == first.index_id
    assert not list((tmp_path / "indexes").glob(".tmp-*"))


def test_wrong_vector_count_from_provider_is_rejected(tmp_path, monkeypatch):
    manager = IndexManager(tmp_path)
    embedder = FakeEmbedder()
    original = embedder.embed_documents
    monkeypatch.setattr(
        embedder, "embed_documents", lambda items, progress=None: original(items)[1:]
    )
    with pytest.raises(EmbeddingError, match="se esperaban"):
        semantic_build(manager, embedder)
    assert manager.active_id() is None


def test_tampered_vectors_are_rejected(tmp_path):
    manager = IndexManager(tmp_path)
    result = semantic_build(manager)
    path = tmp_path / "indexes" / result.index_id / "vectors.npy"
    data = bytearray(path.read_bytes())
    data[-3] ^= 0x01
    path.write_bytes(bytes(data))
    with pytest.raises(IndexStoreError, match="alterados"):
        manager.open_active()


def test_vector_order_must_match_lexical_rows(tmp_path, monkeypatch):
    manager = IndexManager(tmp_path)
    monkeypatch.setattr(index_store, "ordered_ids_digest", lambda ids: "0" * 64)
    result = semantic_build(manager)
    monkeypatch.undo()
    with pytest.raises(IndexStoreError, match="no corresponden"):
        manager.open(result.index_id)


def test_filters_apply_identically_to_both_paths(tmp_path):
    manager = IndexManager(tmp_path)
    semantic_build(manager)
    active = manager.open_active()
    embedder = FakeEmbedder()
    try:
        for filters in (
            SearchFilters(report_ids=("ipm-2025-10",)),
            SearchFilters(period_to="2025-07", kinds=("table", "table_notes")),
            SearchFilters(evidence_qualities=("unattributed_values",)),
            SearchFilters(section="anexo 2"),
            SearchFilters(period_from="2026-01"),
        ):
            reports = active.lexical.reports
            allowed = {c.chunk_id for c in CHUNKS if filters.matches(c, reports[c.report_id])}
            semantic = active.search("cuadro petróleo inflación", filters, top_k=50,
                                     mode="semantico", embedder=embedder)  # fmt: skip
            assert {hit.chunk.chunk_id for hit in semantic} == allowed
            hybrid = active.search("cuadro petróleo inflación", filters, top_k=50,
                                   mode="hibrido", embedder=embedder)  # fmt: skip
            assert {hit.chunk.chunk_id for hit in hybrid} <= allowed
    finally:
        active.close()


def test_hybrid_keeps_evidence_fields_and_table_notes(tmp_path):
    manager = IndexManager(tmp_path)
    semantic_build(manager)
    active = manager.open_active()
    try:
        hits = active.search("IPC total dic-25", mode="hibrido", embedder=FakeEmbedder(), top_k=3)
        table = next(hit for hit in hits if hit.chunk.kind == "table")
        assert table.chunk.attributed_cells[0].value == "4,79"
        assert table.fused_score > 0
        assert table.lexical_rank is not None and table.semantic_rank is not None
        notes = active.table_notes(table.chunk)
        assert [n.table_id for n in notes] == [table.chunk.table_id]
        raw = active.search(
            "petróleo dólares por barril",
            SearchFilters(kinds=("table_raw",)),
            mode="hibrido",
            embedder=FakeEmbedder(),
            top_k=1,
        )[0]
        assert raw.chunk.evidence_quality == "unattributed_values"
        assert raw.chunk.attributed_cells == ()
        with pytest.raises(ValueError, match="candidate_k"):
            active.search("x", mode="hibrido", embedder=FakeEmbedder(), top_k=60)
    finally:
        active.close()


# --- fusión -------------------------------------------------------------------------------------


def hit(key: str, rank: int, *, lexical: bool) -> RetrievalHit:
    item = chunk(key, "2025-07", f"texto {key}")
    report = None
    if lexical:
        return RetrievalHit(rank, item, report, -float(10 - rank), lexical_rank=rank)  # type: ignore[arg-type]
    return RetrievalHit(rank, item, report, semantic_similarity=1 - rank / 10, semantic_rank=rank)  # type: ignore[arg-type]


def test_rrf_combines_ranks_not_scores():
    lexical = [hit("a", 1, lexical=True), hit("b", 2, lexical=True)]
    semantic = [hit("b", 1, lexical=False), hit("c", 2, lexical=False)]
    fused = reciprocal_rank_fusion(lexical, semantic, FusionConfig(rrf_k=60), 10)
    assert [h.chunk.text for h in fused] == ["texto b", "texto a", "texto c"]
    b = fused[0]
    assert b.fused_score == pytest.approx(1 / 62 + 1 / 61)
    assert (b.lexical_rank, b.semantic_rank) == (2, 1)
    assert fused[2].lexical_rank is None and fused[2].lexical_bm25 is None
    assert [h.rank for h in fused] == [1, 2, 3]  # sin duplicados


def test_rrf_ties_are_deterministic_and_weights_matter():
    lexical = [hit("a", 1, lexical=True)]
    semantic = [hit("z", 1, lexical=False)]
    tie = reciprocal_rank_fusion(lexical, semantic, FusionConfig(), 2)
    swapped = reciprocal_rank_fusion(lexical, semantic, FusionConfig(), 2)
    assert [h.chunk.chunk_id for h in tie] == [h.chunk.chunk_id for h in swapped]
    assert [h.chunk.chunk_id for h in tie] == sorted(h.chunk.chunk_id for h in tie)
    semantic_first = reciprocal_rank_fusion(
        lexical, semantic, FusionConfig(lexical_weight=0.5, semantic_weight=1.0), 2
    )
    assert semantic_first[0].chunk.text == "texto z"
    assert reciprocal_rank_fusion([], [], FusionConfig(), 5) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rrf_k": 0},
        {"candidate_k": 0},
        {"lexical_weight": -1},
        {"lexical_weight": 0, "semantic_weight": 0},
    ],
)
def test_fusion_config_is_validated(kwargs):
    with pytest.raises(ValueError):
        FusionConfig(**kwargs)


# --- CLI -----------------------------------------------------------------------------------------


def test_cli_semantic_and_hybrid_with_fake_provider(catalog_root, monkeypatch, capsys):
    from politica_monetaria_banrep_rag import cli

    monkeypatch.setattr(cli, "_ollama", lambda model: FakeEmbedder(model=model))
    data = str(catalog_root)
    code, _, err = run_cli(monkeypatch, capsys, "buscar", "inflación", "--modo", "semantico",
                           "--data-dir", data)  # fmt: skip
    assert code == 1 and "indexar" in err
    code, out, _ = run_cli(monkeypatch, capsys, "indexar", "--modelo-embeddings", "falso",
                           "--data-dir", data)  # fmt: skip
    assert code == 0 and "Vectores: falso (64 dimensiones" in out
    code, out, _ = run_cli(monkeypatch, capsys, "buscar", "precio del petróleo", "--json",
                           "--data-dir", data)  # fmt: skip
    payload = json.loads(out)
    assert payload["mode"] == "hibrido"
    assert payload["results"][0]["page_start"] == 3
    assert payload["results"][0]["fused_score"] > 0
    code, out, _ = run_cli(monkeypatch, capsys, "evaluar", "--modo", "semantico", "--consultas",
                           str(catalog_root.parent / "nada.json"), "--data-dir", data)  # fmt: skip
    assert code == 2


def test_cli_default_index_reports_ollama_down_and_suggests_lexical(
    catalog_root, monkeypatch, capsys
):
    from politica_monetaria_banrep_rag import cli

    def down(request):
        raise httpx.ConnectError("rechazada")

    client = httpx.Client(transport=httpx.MockTransport(down))
    monkeypatch.setattr(cli, "_ollama", lambda model: OllamaEmbeddings(model, client=client))
    code, _, err = run_cli(monkeypatch, capsys, "indexar", "--data-dir", str(catalog_root))
    assert code == 1 and "--sin-embeddings" in err and "no cambió" in err
    assert IndexManager(catalog_root).active_id() is None
