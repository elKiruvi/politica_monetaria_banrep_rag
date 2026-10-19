"""API FastAPI local: rutas reales sobre el catálogo de prueba, seguridad y errores."""

import json
import threading

import pytest
import test_index_store
from fastapi.testclient import TestClient
from test_answering import FakeGenerator
from test_index_store import run_cli
from test_semantic_index import FakeEmbedder

from politica_monetaria_banrep_rag.api import create_app
from politica_monetaria_banrep_rag.service import RagService

catalog_root = test_index_store.catalog_root  # fixture compartida
ANSWER = {"answer": "La inflación básica se ubicó en el 3,5 % [E1].", "citations": ["E1"],
          "insufficient_evidence": False}  # fmt: skip


@pytest.fixture
def indexed(catalog_root, monkeypatch, capsys):
    run_cli(monkeypatch, capsys, "indexar", "--sin-embeddings", "--data-dir", str(catalog_root))
    return catalog_root


def client_for(root, generator=None, **kwargs):
    service = RagService(
        root,
        embedder_factory=lambda model: FakeEmbedder(model=model),
        generator_factory=lambda model: generator or FakeGenerator(ANSWER),
        ollama_probe=lambda: {
            "available": True,
            "url": "http://127.0.0.1:11434",
            "models": ["qwen3:4b-instruct"],
        },  # fmt: skip
    )
    return TestClient(create_app(service, **kwargs), raise_server_exceptions=False)


def test_health_and_readiness_without_index(tmp_path):
    client = client_for(tmp_path)
    assert client.get("/api/v1/health").json() == {"status": "ok"}
    response = client.get("/api/v1/ready")
    assert response.status_code == 503 and response.json()["ready"] is False


def test_ready_status_and_reports_with_index(indexed):
    client = client_for(indexed)
    ready = client.get("/api/v1/ready")
    assert ready.status_code == 200 and ready.json()["index"]["chunk_count"] > 0
    assert ready.json()["generation_model_installed"] is True
    status = client.get("/api/v1/status").json()
    assert status["index"]["available"] and "no oficial" in status["disclaimer"]
    assert "importación local" in " ".join(status["corpus"]["warnings"])
    reports = client.get("/api/v1/reports").json()["reports"]
    assert reports[0]["report_id"] == "ipm-2025-01"
    assert reports[0]["verification_status"] == "local_import"


def test_search_returns_citable_hits(indexed):
    response = client_for(indexed).post(
        "/api/v1/search", json={"query": "petróleo", "mode": "lexico", "top_k": 3}
    )
    assert response.status_code == 200
    hit = response.json()["results"][0]
    assert hit["page_start"] == 3 and hit["evidence_quality"] == "narrative_only"
    assert hit["report"]["reference_period"] == "2025-01" and hit["table_notes"] == []


def test_query_returns_verified_answer(indexed):
    response = client_for(indexed).post(
        "/api/v1/query", json={"question": "¿Cómo estuvo la inflación básica?"}
    )
    body = response.json()
    assert response.status_code == 200 and body["status"] == "answered"
    assert body["citations"][0]["page_start"] == 2 and "no oficial" in body["disclaimer"]


def test_query_rejection_and_generation_failure(indexed):
    invented = FakeGenerator({**ANSWER, "answer": "Fue 9,9 % [E1]."})
    body = (
        client_for(indexed, invented)
        .post("/api/v1/query", json={"question": "inflación básica"})
        .json()
    )
    assert body["status"] == "rejected" and "9.9" in body["reason"]
    from politica_monetaria_banrep_rag.generation import GenerationError

    down = FakeGenerator(error=GenerationError("Ollama no responde"))
    response = client_for(indexed, down).post("/api/v1/query", json={"question": "inflación"})
    assert response.status_code == 503 and "Ollama no responde" in response.json()["detail"]


def test_compare_and_summarize_errors_are_typed(indexed):
    client = client_for(indexed)
    response = client.post("/api/v1/compare", json={"question": "inflación entre enero y julio"})
    assert response.status_code == 422 and "dos informes" in response.json()["detail"]
    response = client.post("/api/v1/summarize", json={"report": "2019-01"})
    assert response.status_code == 404 and "no está indexado" in response.json()["detail"]


@pytest.mark.parametrize(
    "path,payload,field",
    [
        ("/api/v1/search", {"query": ""}, "query"),
        ("/api/v1/search", {"query": "x" * 501}, "query"),
        ("/api/v1/search", {"query": "x", "top_k": 500}, "top_k"),
        ("/api/v1/search", {"query": "x", "mode": "otro"}, "mode"),
        ("/api/v1/search", {"query": "x", "inesperado": 1}, "inesperado"),
        (
            "/api/v1/search",
            {"query": "x", "filters": {"period_from": "2025-13"}},
            "filters.period_from",
        ),  # fmt: skip
        ("/api/v1/summarize", {"report": "../../etc"}, "report"),
    ],
)
def test_invalid_requests_are_rejected_in_spanish(indexed, path, payload, field):
    response = client_for(indexed).post(path, json=payload)
    assert response.status_code == 422
    assert response.json()["detail"].startswith("Solicitud inválida en:")
    assert field in response.json()["detail"]


def test_unknown_filter_values_are_invalid(indexed):
    response = client_for(indexed).post(
        "/api/v1/search", json={"query": "x", "filters": {"kinds": ["imagen"]}}
    )
    assert response.status_code == 422 and "Tipos válidos" in response.json()["detail"]


def test_security_headers_hosts_and_body_limit(indexed):
    client = client_for(indexed)
    response = client.get("/api/v1/health")
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert "access-control-allow-origin" not in response.headers
    rebinding = client.get("/api/v1/health", headers={"Host": "atacante.example"})
    assert rebinding.status_code == 400
    big = client.post(
        "/api/v1/search",
        content=json.dumps({"query": "x" * 20_000}),
        headers={"Content-Type": "application/json"},
    )
    assert big.status_code == 413


def test_concurrent_generation_is_serialized(indexed):
    started, release = threading.Event(), threading.Event()

    class Slow(FakeGenerator):
        def generate_json(self, system, user, schema):
            started.set()
            release.wait(5)
            return ANSWER

    client = client_for(indexed, Slow())
    results = []
    worker = threading.Thread(
        target=lambda: results.append(client.post("/api/v1/query", json={"question": "inflación"}))
    )
    worker.start()
    assert started.wait(5)
    busy = client.post("/api/v1/query", json={"question": "inflación"})
    release.set()
    worker.join()
    assert busy.status_code == 503 and "otra solicitud" in busy.json()["detail"]
    assert results[0].status_code == 200


def test_unexpected_errors_do_not_leak_internals(indexed, monkeypatch):
    from politica_monetaria_banrep_rag import service

    def boom(self, *args, **kwargs):
        raise RuntimeError("/home/secreto/ruta")

    monkeypatch.setattr(service.RagService, "reports", boom)
    response = client_for(indexed).get("/api/v1/reports")
    assert response.status_code == 500 and "secreto" not in response.text


def test_servir_refuses_network_exposure_without_flag(monkeypatch, capsys, tmp_path):
    code, _, err = run_cli(monkeypatch, capsys, "servir", "--host", "0.0.0.0", "--data-dir",
                           str(tmp_path))  # fmt: skip
    assert code == 2 and "--permitir-red" in err
