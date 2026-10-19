"""Regresiones offline del caché antes de construir cualquier índice."""

import hashlib
import json
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from importlib import metadata

import pytest
from pdf_factory import make_layout_pdf
from test_importer import write
from test_layout import annex_page, page_one, page_two

from politica_monetaria_banrep_rag.catalog import Catalog
from politica_monetaria_banrep_rag.chunking import CHUNKER_VERSION
from politica_monetaria_banrep_rag.processed_store import ProcessedStore, _payload_digest
from politica_monetaria_banrep_rag.processing import PROCESSING_VERSION, process_pdf


def _ensure_in_process(args):
    root, records = args
    summaries, failures = ProcessedStore(root).ensure(records)
    return [summary.cached for summary in summaries], failures


@pytest.fixture
def prepared(tmp_path):
    root = tmp_path / "datos"
    catalog = Catalog(root, None, None)  # type: ignore[arg-type]
    catalog.import_pdf(write(tmp_path))
    catalog.activate_imports()
    records = catalog.current()[1]["reports"]
    store = ProcessedStore(root)
    summaries, failures = store.ensure(records)
    assert not failures and len(summaries) == 1
    return store, records, summaries[0].path


@pytest.fixture
def prepared_evidence(tmp_path):
    root = tmp_path / "data"
    raw = root / "raw"
    raw.mkdir(parents=True)
    pdf = make_layout_pdf([page_one(), page_two(), annex_page()])
    sha = hashlib.sha256(pdf).hexdigest()
    source = raw / f"ipm-2025-01-{sha}.pdf"
    source.write_bytes(pdf)
    records = [{"report": {"report_id": "ipm-2025-01"},
                "pdf": {"path": f"raw/{source.name}", "sha256": sha}}]  # fmt: skip
    store = ProcessedStore(root)
    summaries, failures = store.ensure(records)
    assert not failures and len(summaries) == 1
    return store, records, summaries[0].path


@pytest.mark.parametrize("change", ["missing", "altered"])
def test_cached_report_requires_verified_source(prepared, change):
    store, records, target = prepared
    source = store.root / records[0]["pdf"]["path"]
    if change == "missing":
        source.unlink()
    else:
        source.write_bytes(b"%PDF-archivo alterado")
    summaries, failures = store.ensure(records)
    assert not summaries and len(failures) == 1
    assert "PDF" in failures[0][1]
    assert target.exists()  # El caché anterior no se borra por un fallo.


@pytest.mark.parametrize(
    "change",
    [
        "report_id",
        "pdf_hash",
        "chunk_id",
        "page",
        "unit_kind",
        "chunk_kind",
        "units",
        "quality",
        "versions",
        "checksum",
        "truncated",
    ],
)
def test_tampered_or_partial_cache_is_rebuilt(prepared, change):
    store, records, target = prepared
    content = json.loads(target.read_text())
    if change == "truncated":
        target.write_text('{"document":')
    else:
        if change == "report_id":
            content["document"]["report_id"] = "ipm-2024-01"
        elif change == "pdf_hash":
            content["document"]["sha256"] = "f" * 64
        elif change == "chunk_id":
            content["chunks"][0]["chunk_id"] = "0" * 32
        elif change == "page":
            content["chunks"][0]["page_end"] = 999
        elif change == "unit_kind":
            content["document"]["units"][0]["kind"] = ["table"]
        elif change == "units":
            content["document"]["units"] = []
        elif change == "quality":
            content["document"]["quality"]["characters"] = -1
        elif change == "versions":
            content["versions"]["chunker"] = "otra-version"
        elif change == "checksum":
            content["chunks"][0]["text"] = "Texto alterado sin actualizar el identificador."
        else:
            content["chunks"][0]["kind"] = ["table_raw"]
        if change != "checksum":
            # Incluso con la huella recalculada, la validación estructural debe fallar.
            content["payload_sha256"] = _payload_digest(content)
        target.write_text(json.dumps(content))
    summaries, failures = store.ensure(records)
    assert not failures and not summaries[0].cached
    assert summaries[0].path == target
    assert json.loads(target.read_text())["document"]["report_id"] == "ipm-2025-01"


def test_reuse_versions_and_legacy_file_survives(prepared):
    store, records, target = prepared
    summaries, failures = store.ensure(records)
    assert not failures and summaries[0].cached
    changed = ProcessedStore(store.root, max_chars=1500)
    assert changed.path_for("ipm-2025-01", records[0]["pdf"]["sha256"]) != target
    summaries, failures = changed.ensure(records)
    assert not failures and not summaries[0].cached and target.exists()


@pytest.mark.parametrize(
    "change", ["unit", "table", "chunk", "attributed_cells", "table_id", "coverage"]
)
def test_recalculated_checksum_cannot_promote_unattributed_evidence(prepared_evidence, change):
    store, records, target = prepared_evidence
    content = json.loads(target.read_text())
    document = content["document"]
    units = document["units"]
    chunks = content["chunks"]
    if change == "unit":
        next(unit for unit in units if unit["kind"] == "paragraph")["evidence_quality"] = (
            "structured_cells"
        )
    elif change == "table":
        next(unit for unit in units if unit["kind"] == "table")["table"]["evidence_quality"] = (
            "unattributed_values"
        )
    elif change == "chunk":
        next(chunk for chunk in chunks if chunk["kind"] == "chart_caption")["evidence_quality"] = (
            "structured_cells"
        )
    elif change == "attributed_cells":
        next(chunk for chunk in chunks if chunk["kind"] == "chart_caption")["attributed_cells"] = [
            {
                "row": ["Invented"],
                "column": "2026",
                "value": "9,9",
                "horizon": "2026",
                "unit": "porcentaje",
                "projection_or_assumption": False,
            }
        ]
    elif change == "table_id":
        next(chunk for chunk in chunks if chunk["kind"] == "table")["table_id"] = "0" * 32
    else:
        document["quality"]["critical_sections"]["2"]["covered"] = False
    content["payload_sha256"] = _payload_digest(content)
    target.write_text(json.dumps(content))
    summaries, failures = store.ensure(records)
    assert not failures and not summaries[0].cached


@pytest.mark.parametrize("version", [1, 2])
def test_old_cache_signature_is_not_reused(prepared, version):
    store, records, target = prepared
    signature = (
        f"cache={version}|{PROCESSING_VERSION}|{CHUNKER_VERSION}|{store.max_chars}|"
        f"pdfminer={metadata.version('pdfminer.six')}"
    )
    old_version = hashlib.sha256(signature.encode()).hexdigest()[:12]
    old_target = target.with_name(target.name.replace(store.version, old_version))
    target.rename(old_target)
    summaries, failures = store.ensure(records)
    assert not failures and not summaries[0].cached
    assert target.exists() and old_target.exists()


@pytest.mark.parametrize("change", ["text", "page_labels", "page_end"])
def test_recalculated_checksum_cannot_forge_chunk_citation(prepared, change):
    store, records, target = prepared
    content = json.loads(target.read_text())
    chunk = content["chunks"][0]
    if change == "text":
        chunk["text"] = "Una cifra inventada sin respaldo en las unidades."
        identity = "|".join(
            (
                records[0]["report"]["report_id"],
                records[0]["pdf"]["sha256"],
                PROCESSING_VERSION,
                CHUNKER_VERSION,
                "0",
                chunk["text"],
            )
        )
        chunk["chunk_id"] = hashlib.sha256(identity.encode()).hexdigest()[:32]
    elif change == "page_labels":
        chunk["page_labels"] = ["otra página"]
    else:
        chunk["page_end"] = chunk["page_start"] + 1
        content["document"]["quality"]["pages"] += 1
    content["payload_sha256"] = _payload_digest(content)
    target.write_text(json.dumps(content))
    summaries, failures = store.ensure(records)
    assert not failures and not summaries[0].cached
    assert json.loads(target.read_text())["chunks"][0]["text"] != (
        "Una cifra inventada sin respaldo en las unidades."
    )


@pytest.mark.parametrize(
    "change",
    ["degraded", "warnings", "unresolved", "excluded", "tables_failed", "pages_with_text"],
)
def test_recalculated_checksum_cannot_hide_quality_warning(prepared, change):
    store, records, target = prepared
    original = target.read_bytes()
    content = json.loads(original)
    quality = content["document"]["quality"]
    if change == "degraded":
        quality["degraded"] = False
    elif change == "warnings":
        quality["warnings"] = []
        quality["degraded"] = False
    elif change == "unresolved":
        quality["ligatures_unresolved"] = -1
    elif change == "excluded":
        quality["excluded_blocks"]["paragraph"] = -1
    elif change == "tables_failed":
        quality["tables_failed"].append([1, "Cuadro inventado", "sin columnas"])
    else:
        quality["pages_with_text"] = 0
    content["payload_sha256"] = _payload_digest(content)
    target.write_text(json.dumps(content))
    summaries, failures = store.ensure(records)
    assert not failures and not summaries[0].cached
    assert target.read_bytes() == original


def test_paragraph_spanning_an_empty_page_remains_processable(prepared):
    store, records, _ = prepared
    pdf = make_layout_pdf(
        [
            [(291, 700, 10, "La demanda crecería por", "R")],
            [],
            [(291, 700, 10, "nuevos acuerdos comerciales.", "R")],
        ]
    )
    sha256 = hashlib.sha256(pdf).hexdigest()
    report_id = records[0]["report"]["report_id"]
    path = f"raw/{report_id}-{sha256}.pdf"
    (store.root / path).write_bytes(pdf)
    records[0]["pdf"].update({"path": path, "sha256": sha256, "pages": 3})
    document = process_pdf(store.root / path, report_id, sha256)
    assert document.quality.pages_with_text == 2
    assert any(unit.page == 1 and unit.page_end == 3 for unit in document.units)
    summaries, failures = store.ensure(records)
    assert not failures and len(summaries) == 1 and not summaries[0].cached


def test_textless_pdf_keeps_degraded_report_without_chunks(prepared):
    store, records, _ = prepared
    pdf = make_layout_pdf([[]])
    sha256 = hashlib.sha256(pdf).hexdigest()
    report_id = records[0]["report"]["report_id"]
    path = f"raw/{report_id}-{sha256}.pdf"
    (store.root / path).write_bytes(pdf)
    records[0]["pdf"].update({"path": path, "sha256": sha256, "pages": 1})
    summaries, failures = store.ensure(records)
    assert not failures and len(summaries) == 1
    assert summaries[0].chunks == 0 and summaries[0].degraded
    cached, failures = store.ensure(records)
    assert not failures and cached[0].cached and cached[0].chunks == 0


@pytest.mark.parametrize("malformed", ["surrogate", "deeply_nested"])
def test_malformed_cache_rebuilds_without_aborting(prepared, malformed):
    store, records, target = prepared
    if malformed == "surrogate":
        content = json.loads(target.read_text())
        content["chunks"][0]["text"] = "\ud800"
        target.write_text(json.dumps(content))
    else:
        target.write_text("[" * 10000 + "]" * 10000)
    summaries, failures = store.ensure(records)
    assert not failures and not summaries[0].cached


@pytest.mark.parametrize("path", ["../outside.pdf", "raw/../outside.pdf"])
def test_pdf_path_cannot_leave_raw_even_on_cache_hit(prepared, path):
    store, records, target = prepared
    records[0]["pdf"]["path"] = path
    summaries, failures = store.ensure(records)
    assert not summaries and failures == [
        ("ipm-2025-01", "falta el PDF seguro en el almacén local")
    ]
    assert target.exists()


def test_symlinked_raw_directory_is_not_read(prepared, tmp_path):
    store, records, _ = prepared
    raw = store.root / "raw"
    elsewhere = tmp_path / "elsewhere"
    raw.rename(elsewhere)
    try:
        raw.symlink_to(elsewhere, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Esta plataforma no permite crear enlaces simbólicos.")
    summaries, failures = store.ensure(records)
    assert not summaries and failures[0][1] == "falta el PDF seguro en el almacén local"


@pytest.mark.parametrize("result", [b"{broken", b"[]"])
def test_invalid_isolated_result_is_reported_per_record(prepared, monkeypatch, result):
    store, records, target = prepared
    previous = target.read_bytes()
    monkeypatch.setattr(
        "politica_monetaria_banrep_rag.processed_store.run_isolated", lambda *a, **kw: result
    )
    summaries, failures = store.ensure(records, force=True)
    assert not summaries and failures == [
        ("ipm-2025-01", "el procesamiento produjo un artefacto inválido")
    ]
    assert target.read_bytes() == previous


def test_pdf_disappearing_during_processing_is_reported(prepared, monkeypatch):
    from politica_monetaria_banrep_rag.processed_store import _work

    store, records, target = prepared
    previous = target.read_bytes()

    def disappear(work, job, **kwargs):
        result = _work(job)
        (store.root / records[0]["pdf"]["path"]).unlink()
        return result

    monkeypatch.setattr("politica_monetaria_banrep_rag.processed_store.run_isolated", disappear)
    summaries, failures = store.ensure(records, force=True)
    assert not summaries and len(failures) == 1
    assert "PDF" in failures[0][1] and target.read_bytes() == previous


def test_two_writers_share_one_complete_cache(prepared):
    store, records, target = prepared
    # Una reconstrucción forzada compite con el lector; el segundo espera el bloqueo.
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(store.ensure, records, force=True), pool.submit(store.ensure, records)]
        results = [task.result() for task in tasks]
    assert all(not failures and len(summaries) == 1 for summaries, failures in results)
    assert store._load(target, "ipm-2025-01", records[0]["pdf"]["sha256"]) is not None


def test_two_processes_build_one_missing_cache(prepared):
    store, records, target = prepared
    target.unlink()
    with ProcessPoolExecutor(
        max_workers=2, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        results = list(pool.map(_ensure_in_process, [(store.root, records)] * 2))
    assert sorted(cached[0] for cached, failures in results if not failures) == [False, True]
    assert store._load(target, "ipm-2025-01", records[0]["pdf"]["sha256"]) is not None


def test_failed_forced_processing_keeps_previous_artifact(prepared, monkeypatch):
    store, records, target = prepared
    previous = target.read_bytes()
    payload = json.loads(previous)
    payload.pop("payload_sha256")
    payload.pop("versions")
    payload.pop("report")
    payload.pop("pdf")
    payload["chunks"] = payload["chunks"][1:]
    monkeypatch.setattr(
        "politica_monetaria_banrep_rag.processed_store.run_isolated",
        lambda *args, **kwargs: json.dumps(payload).encode(),
    )
    summaries, failures = store.ensure(records, force=True)
    assert not summaries and failures == [
        ("ipm-2025-01", "el procesamiento produjo un artefacto inválido")
    ]
    assert target.read_bytes() == previous
