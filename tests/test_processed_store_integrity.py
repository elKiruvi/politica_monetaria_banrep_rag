"""Regresiones offline del caché antes de construir cualquier índice."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_importer import write

from politica_monetaria_banrep_rag.catalog import Catalog
from politica_monetaria_banrep_rag.processed_store import ProcessedStore, _payload_digest


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


def test_two_writers_share_one_complete_cache(prepared):
    store, records, target = prepared
    # Una reconstrucción forzada compite con el lector; el segundo espera el bloqueo.
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(store.ensure, records, force=True), pool.submit(store.ensure, records)]
        results = [task.result() for task in tasks]
    assert all(not failures and len(summaries) == 1 for summaries, failures in results)
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
