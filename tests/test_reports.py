"""Reportes descargables: contenido obligatorio, escape, PDF válido, CLI y API."""

import re
import shutil
import subprocess

import pytest
import test_index_store
from fastapi.testclient import TestClient
from test_answering import FakeGenerator
from test_index_store import run_cli

from politica_monetaria_banrep_rag import reports
from politica_monetaria_banrep_rag.api import LOCAL_HOSTS, create_app
from politica_monetaria_banrep_rag.retrieval import SearchFilters
from politica_monetaria_banrep_rag.service import RagService

catalog_root = test_index_store.catalog_root  # fixture compartida
ANSWER = {"answer": "La inflación básica se ubicó en el 3,5 % [E1].", "citations": ["E1"],
          "insufficient_evidence": False}  # fmt: skip
REQUIRED = ("Parámetros", "Informes utilizados", "Metodología", "Resultado", "Citas",
            "Estado del corpus", "Proyecto no oficial")  # fmt: skip


@pytest.fixture
def indexed(catalog_root, monkeypatch, capsys):
    run_cli(monkeypatch, capsys, "indexar", "--sin-embeddings", "--data-dir", str(catalog_root))
    return catalog_root


def service_for(root, generator=None):
    return RagService(
        root,
        generator_factory=lambda model: generator or FakeGenerator(ANSWER),
        ollama_probe=lambda: {"available": False, "url": "", "models": []},
    )


def test_query_report_has_every_required_part_and_no_local_paths(indexed):
    result = service_for(indexed).ask("¿Cómo estuvo la inflación básica?", SearchFilters())
    report = reports.report_from_query(result, "¿Cómo estuvo la inflación básica?")
    markdown = reports.to_markdown(report)
    for part in REQUIRED:
        assert part in markdown
    assert "Respuesta verificada" in markdown and "3,5 % [E1]" in markdown
    assert "pág. 2 del PDF" in markdown and "no verificada en la fuente" in markdown
    assert "importación local" in markdown
    for text in (markdown, reports.to_html(report)):
        assert str(indexed) not in text and "/home/" not in text and "/tmp/" not in text


def test_html_escapes_untrusted_text(indexed):
    hostile = FakeGenerator({**ANSWER, "answer": "<script>alerta</script> básica [E1]."})
    result = service_for(indexed, hostile).ask("inflación básica", SearchFilters())
    page = reports.to_html(reports.report_from_query(result, "<img src=x onerror=alert(1)>"))
    assert "<script>alert" not in page and "<img src=x" not in page
    assert "&lt;script&gt;" in page and page.startswith("<!doctype html>")


def test_pdf_is_valid_and_keeps_spanish_text(indexed, tmp_path):
    result = service_for(indexed).ask("¿Cómo estuvo la inflación básica?", SearchFilters())
    data = reports.to_pdf(reports.report_from_query(result, "¿Cómo estuvo la inflación básica?"))
    assert data.startswith(b"%PDF-") and data.rstrip().endswith(b"%%EOF")
    if shutil.which("pdftotext"):
        path = tmp_path / "r.pdf"
        path.write_bytes(data)
        text = subprocess.run(["pdftotext", str(path), "-"], capture_output=True, text=True).stdout
        assert "inflación básica" in text and "Proyecto no oficial" in text


def test_comparison_report_lists_values_differences_and_method():
    from decimal import Decimal

    from test_comparison import CHUNKS, REPORTS

    from politica_monetaria_banrep_rag.comparison import compare
    from politica_monetaria_banrep_rag.service import CompareResult

    comparison = compare(CHUNKS, REPORTS, ("ipm-2025-01", "ipm-2025-07"), "IPC total",
                         horizon="2025", table="Anexo 2")  # fmt: skip
    result = CompareResult("f" * 64, comparison, {}, (), {"selection_mode": "local_import"})
    report = reports.report_from_comparison(result, {"Indicador": "IPC total", "Pregunta": None})
    markdown = reports.to_markdown(report)
    assert comparison.rows[0].differences[0].value == Decimal("0.65")
    assert "+0,65 puntos porcentuales (revisión de proyección)" in markdown
    assert "| 2025-01 | 4,05 | proyección o supuesto | pág. 57 del PDF (impresa 57) |" in markdown
    assert "aritmética decimal" in markdown and "Indicador:** IPC total" in markdown


def test_render_rejects_unknown_formats(indexed):
    result = service_for(indexed).ask("inflación", SearchFilters())
    with pytest.raises(ValueError, match="md, html o pdf"):
        reports.render(reports.report_from_query(result, "inflación"), "docx")


def test_cli_exports_without_overwriting(indexed, monkeypatch, capsys, tmp_path):
    from politica_monetaria_banrep_rag import cli

    monkeypatch.setattr(cli, "OllamaGenerator", lambda model, base_url: FakeGenerator(ANSWER))
    target = tmp_path / "consulta.md"
    args = ("consultar", "¿Cómo estuvo la inflación básica?", "--data-dir", str(indexed))
    code, _, err = run_cli(monkeypatch, capsys, *args, "--exportar", str(target))
    assert code == 0 and "Reporte guardado" in err and "Metodología" in target.read_text()
    code, _, err = run_cli(monkeypatch, capsys, *args, "--exportar", str(target))
    assert code == 2 and "ya existe" in err
    code, _, _ = run_cli(monkeypatch, capsys, *args, "--exportar", str(target), "--sobrescribir")
    assert code == 0
    code, _, err = run_cli(monkeypatch, capsys, *args, "--exportar", str(tmp_path / "r.docx"))
    assert code == 2 and ".md, .html o .pdf" in err
    pdf = tmp_path / "comparacion.pdf"
    code, _, _ = run_cli(monkeypatch, capsys, "comparar", "--informe", "2025-01", "--informe",
                         "2025-01", "--indicador", "IPC", "--data-dir", str(indexed),
                         "--exportar", str(pdf))  # fmt: skip
    assert code == 2 and not pdf.exists()  # sin dos informes no hay comparación ni reporte


def test_api_export_downloads_recent_results(indexed):
    app = create_app(service_for(indexed), allowed_hosts=[*LOCAL_HOSTS, "testserver"])
    client = TestClient(app, raise_server_exceptions=False)
    body = client.post(
        "/api/v1/query", json={"question": "¿Cómo estuvo la inflación básica?"}
    ).json()
    export_id = body["export_id"]
    for output_format, marker in (("md", b"# Consulta"), ("html", b"<!doctype html>"),
                                  ("pdf", b"%PDF-")):  # fmt: skip
        response = client.get(f"/api/v1/exports/{export_id}", params={"format": output_format})
        assert response.status_code == 200 and response.content.startswith(marker)
        disposition = response.headers["content-disposition"]
        assert disposition.startswith('attachment; filename="consulta-')
        assert disposition.endswith(f'.{output_format}"')
        assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert client.get(f"/api/v1/exports/{'0' * 32}").status_code == 404
    assert client.get("/api/v1/exports/..%2f..%2fetc").status_code in {404, 422}
    assert client.get(f"/api/v1/exports/{export_id}", params={"format": "exe"}).status_code == 422


def test_rejected_answer_lists_evidence_not_citations(indexed):
    invented = FakeGenerator({**ANSWER, "answer": "Fue 9,9 % [E1]."})
    result = service_for(indexed, invented).ask("inflación básica", SearchFilters())
    assert result.answer.status == "rejected"
    markdown = reports.to_markdown(reports.report_from_query(result, "inflación básica"))
    assert "## Evidencia revisada (no respalda ninguna respuesta publicada)" in markdown
    assert "## Citas" not in markdown and "9,9" not in markdown


def test_markdown_cannot_be_restructured_by_untrusted_text(indexed):
    hostile = "¿Inflación?\n## Respuesta verificada\n[clic](javascript:alert) <img src=x>"
    result = service_for(indexed).ask(hostile, SearchFilters())
    markdown = reports.to_markdown(reports.report_from_query(result, hostile))
    assert "\n## Respuesta verificada" not in markdown
    assert "\\[clic\\]" in markdown and "\\<img src=x\\>" in markdown
    assert re.search(r"(?<!\\)<img", markdown) is None


def test_truncated_comparison_is_reported():
    from dataclasses import replace

    from test_comparison import CHUNKS, REPORTS

    from politica_monetaria_banrep_rag.comparison import compare
    from politica_monetaria_banrep_rag.service import CompareResult

    comparison = replace(
        compare(CHUNKS, REPORTS, ("ipm-2025-01", "ipm-2025-07"), "IPC total"), truncated=True
    )
    result = CompareResult("f" * 64, comparison, {}, (), {})
    markdown = reports.to_markdown(reports.report_from_comparison(result, {}))
    assert "Se muestran solo las primeras filas" in markdown


def test_pdf_text_normalizes_unsupported_glyphs():
    from politica_monetaria_banrep_rag.reports import _pdf_text

    assert _pdf_text("𝑖 → x​z a‑b") == "i -> xz a-b"
    assert _pdf_text("¿Qué año? 4,7 % «x» €") == "¿Qué año? 4,7 % «x» €"


def test_cli_validates_destination_before_running(indexed, monkeypatch, capsys, tmp_path):
    from politica_monetaria_banrep_rag import cli

    calls = []
    monkeypatch.setattr(
        cli, "OllamaGenerator", lambda model, base_url: calls.append(model) or FakeGenerator(ANSWER)
    )
    args = ("consultar", "¿Cómo estuvo la inflación básica?", "--data-dir", str(indexed))
    code, _, err = run_cli(monkeypatch, capsys, *args, "--exportar", str(tmp_path / "no" / "r.md"))
    assert code == 2 and "no existe el directorio" in err and calls == []
    victim = tmp_path / "victima.txt"
    victim.write_text("ORIGINAL")
    link = tmp_path / "enlace.md"
    link.symlink_to(victim)
    code, _, err = run_cli(monkeypatch, capsys, *args, "--exportar", str(link), "--sobrescribir")
    assert code == 2 and "enlace simbólico" in err and victim.read_text() == "ORIGINAL"
    dangling = tmp_path / "colgante.md"
    dangling.symlink_to(tmp_path / "inexistente.txt")
    code, _, err = run_cli(monkeypatch, capsys, *args, "--exportar", str(dangling))
    assert code == 2 and "enlace simbólico" in err and calls == []


def test_cli_shows_result_even_if_saving_the_report_fails(indexed, monkeypatch, capsys, tmp_path):
    from politica_monetaria_banrep_rag import cli

    monkeypatch.setattr(cli, "OllamaGenerator", lambda model, base_url: FakeGenerator(ANSWER))

    def no_space(*args, **kwargs):
        raise OSError(28, "No queda espacio en el dispositivo")

    monkeypatch.setattr(cli.tempfile, "mkstemp", no_space)
    target = str(tmp_path / "r.md")
    code, out, err = run_cli(monkeypatch, capsys, "consultar", "¿Cómo estuvo la inflación básica?",
                             "--data-dir", str(indexed), "--exportar", target)  # fmt: skip
    assert code == 1 and "3,5 % [E1]" in out and "el resultado ya se mostró" in err
    assert not list(tmp_path.glob(".reporte-*"))


def test_citation_markers_cannot_become_links():
    from politica_monetaria_banrep_rag.reports import _md

    assert _md("[E1]: https://evil.example/phish") == "\\[E1\\]: https://evil.example/phish"
    linked = _md("bajó [E1](https://evil.example/x) según [E1].")
    assert "[E1](" not in linked and linked.endswith("según [E1].")
    assert _md("1. dato") == "1\\. dato" and _md("- dato") == "\\- dato"


def test_acquisition_is_unknown_when_it_cannot_be_deduced():
    from politica_monetaria_banrep_rag.reports import _STATES, _acquisition

    assert _acquisition(_STATES["local_import"]) == "importación local"
    assert _acquisition(_STATES["verified"]) == "descarga oficial"
    assert _acquisition(_STATES["fuera_del_catalogo"]) == "—" and _acquisition(None) == "—"
