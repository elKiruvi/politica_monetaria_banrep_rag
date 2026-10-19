"""Manifiesto, activación atómica, reversión y lectura concurrente de índices."""

import json
import threading

import pytest
from test_importer import COVER, write
from test_lexical_index import CHUNKS, REPORTS

from politica_monetaria_banrep_rag import index_store
from politica_monetaria_banrep_rag.catalog import Catalog
from politica_monetaria_banrep_rag.corpus import Corpus, CorpusError, load_corpus
from politica_monetaria_banrep_rag.index_store import (
    IndexManager,
    IndexStoreError,
    build_key,
    corpus_status,
)
from politica_monetaria_banrep_rag.processed_store import ProcessedStore

SNAPSHOT = "a" * 64


def corpus(snapshot: str = SNAPSHOT, chunks=CHUNKS, signature: str = "proc-1") -> Corpus:
    return Corpus(
        snapshot_id=snapshot,
        selection_mode="local_import",
        processing_signature=signature,
        processing_versions={"processing": "x"},
        reports=REPORTS,
        chunks=chunks,
    )


def build(manager: IndexManager, value: Corpus | None = None, **kwargs):
    value = value or corpus()
    return manager.build(lambda: value, lambda: value.snapshot_id, **kwargs)


def test_no_index_is_an_explicit_error(tmp_path):
    with pytest.raises(IndexStoreError, match="indexar"):
        IndexManager(tmp_path).open_active()


def test_build_activates_and_manifest_describes_exactly_what_was_built(tmp_path):
    manager = IndexManager(tmp_path)
    result = build(manager)
    assert result.created
    manifest = result.manifest
    assert manifest["catalog_snapshot"] == SNAPSHOT
    assert manifest["selection_mode"] == "local_import"
    assert manifest["chunk_count"] == len(CHUNKS)
    assert [r["report_id"] for r in manifest["reports"]] == ["ipm-2025-07", "ipm-2025-10"]
    assert manifest["vector"] is None
    active = manager.open_active()
    try:
        assert active.index_id == result.index_id
        assert active.search("petróleo", top_k=1)[0].report.report_id == "ipm-2025-10"
    finally:
        active.close()


def test_unchanged_corpus_reuses_active_index(tmp_path):
    manager = IndexManager(tmp_path)
    first = build(manager)
    second = build(manager)
    assert not second.created and second.index_id == first.index_id


@pytest.mark.parametrize(
    "changed",
    [
        corpus(snapshot="b" * 64),
        corpus(signature="proc-2"),
        corpus(chunks=CHUNKS[:-1]),
    ],
)
def test_corpus_or_processing_change_requires_new_index(tmp_path, changed):
    manager = IndexManager(tmp_path)
    first = build(manager)
    second = build(manager, changed)
    assert second.created and second.index_id != first.index_id
    assert build_key(changed) != first.manifest["build_key"]


def test_rollback_restores_previous_index_and_prunes_old_ones(tmp_path):
    manager = IndexManager(tmp_path, keep=3)
    ids = [build(manager, force=True).index_id for _ in range(4)]
    assert len(set(ids)) == 4
    kept = {p.name for p in (tmp_path / "indexes").iterdir() if p.is_dir()}
    assert kept == set(ids[1:])
    assert manager.rollback() == ids[2]
    assert manager.active_id() == ids[2]
    assert manager.rollback() == ids[1]
    with pytest.raises(IndexStoreError, match="anterior"):
        manager.rollback()
    assert manager.active_id() == ids[1]


def test_rollback_skips_damaged_previous_index(tmp_path):
    manager = IndexManager(tmp_path)
    ids = [build(manager, force=True).index_id for _ in range(3)]
    (tmp_path / "indexes" / ids[1] / "lexical.sqlite").write_bytes(b"alterado")
    assert manager.rollback() == ids[0]


@pytest.mark.parametrize("target", ["manifest.json", "lexical.sqlite"])
def test_tampered_artifacts_are_rejected(tmp_path, target):
    manager = IndexManager(tmp_path)
    result = build(manager)
    path = tmp_path / "indexes" / result.index_id / target
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0x01
    path.write_bytes(bytes(data))
    with pytest.raises(IndexStoreError, match="alterado"):
        manager.open_active()


def test_coherent_manifest_rewrite_still_fails_against_pointer(tmp_path):
    manager = IndexManager(tmp_path)
    result = build(manager)
    path = tmp_path / "indexes" / result.index_id / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["chunk_count"] = 1
    path.write_text(json.dumps(manifest))
    with pytest.raises(IndexStoreError):
        manager.open_active()


@pytest.mark.parametrize(
    "content", [b"{", b'{"version": 1, "index": "../x", "history": []}', b"[]"]
)
def test_damaged_pointer_is_rejected(tmp_path, content):
    manager = IndexManager(tmp_path)
    build(manager)
    (tmp_path / "indexes" / "current.json").write_bytes(content)
    with pytest.raises(IndexStoreError, match="puntero"):
        manager.open_active()


def test_damaged_pointer_is_replaced_only_when_forced(tmp_path):
    manager = IndexManager(tmp_path)
    build(manager)
    (tmp_path / "indexes" / "current.json").write_bytes(b"{")
    with pytest.raises(IndexStoreError, match="--forzar"):
        build(manager)
    result = build(manager, force=True)
    assert manager.active_id() == result.index_id
    manager.open_active().close()


def test_incompatible_format_is_rejected(tmp_path, monkeypatch):
    manager = IndexManager(tmp_path)
    build(manager)
    monkeypatch.setattr(index_store, "INDEX_FORMAT", "banrep-index-2")
    with pytest.raises(IndexStoreError, match="incompatible"):
        manager.open_active()


def test_failed_build_keeps_previous_index_and_leaves_no_temporary_files(tmp_path, monkeypatch):
    manager = IndexManager(tmp_path)
    first = build(manager)

    def broken(path, *args):
        path.write_bytes(b"parcial")
        raise OSError("disco lleno")

    monkeypatch.setattr(index_store, "build_lexical_index", broken)
    with pytest.raises(OSError, match="disco lleno"):
        build(manager, corpus(snapshot="b" * 64))
    assert manager.active_id() == first.index_id
    assert not list((tmp_path / "indexes").glob(".tmp-*"))
    manager.open_active().close()


def test_corpus_error_keeps_previous_index(tmp_path):
    manager = IndexManager(tmp_path)
    first = build(manager)

    def failing():
        raise CorpusError("caché dañada")

    with pytest.raises(CorpusError):
        manager.build(failing, lambda: SNAPSHOT)
    assert manager.active_id() == first.index_id


def test_catalog_change_during_build_does_not_activate(tmp_path):
    manager = IndexManager(tmp_path)
    first = build(manager)
    newer = corpus(snapshot="b" * 64)
    with pytest.raises(IndexStoreError, match="cambió durante"):
        manager.build(lambda: newer, lambda: "c" * 64)
    assert manager.active_id() == first.index_id


def test_interrupted_build_leftovers_are_cleaned(tmp_path):
    manager = IndexManager(tmp_path)
    leftover = tmp_path / "indexes" / ".tmp-interrumpido"
    leftover.mkdir(parents=True)
    (leftover / "lexical.sqlite").write_bytes(b"a medias")
    build(manager)
    assert not leftover.exists()


def test_readers_never_see_a_mixed_or_missing_index_during_publications(tmp_path):
    manager = IndexManager(tmp_path, keep=2)
    build(manager)
    errors: list[BaseException] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                active = manager.open_active()
                try:
                    hits = active.search("petróleo", top_k=3)
                    assert hits and active.manifest["chunk_count"] == len(CHUNKS)
                finally:
                    active.close()
            except BaseException as exc:  # noqa: BLE001 - se informa al final
                errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(3)]
    for thread in threads:
        thread.start()
    for _ in range(8):
        build(manager, force=True)
    stop.set()
    for thread in threads:
        thread.join()
    assert errors == []


def test_corpus_status_separates_integrity_from_freshness():
    manifest = {
        "catalog_snapshot": SNAPSHOT,
        "selection_mode": "latest_n",
        "reports": [{"report_id": "ipm-2025-07"}, {"report_id": "ipm-2025-10"}],
    }
    verification = [
        {"report_id": "ipm-2025-07", "status": "verified"},
        {"report_id": "ipm-2025-10", "status": "revalidation_pending"},
    ]
    status = corpus_status(manifest, SNAPSHOT, verification)
    assert status["index_matches_catalog"]
    assert status["warnings"] == ["Vigencia en la fuente sin confirmar: ipm-2025-10."]
    stale = corpus_status(manifest, "b" * 64, verification[:1])
    assert not stale["index_matches_catalog"]
    assert stale["reports"][1]["verification_status"] == "fuera_del_catalogo"


# --- integración con el catálogo y la caché procesada reales ----------------------------------

PAGES = COVER + [
    ["La inflación básica se ubicó en el 3,5 % en diciembre.", "Las expectativas bajaron."],
    ["El precio del petróleo cayó durante el trimestre."],
]


@pytest.fixture
def catalog_root(tmp_path):
    root = tmp_path / "datos"
    catalog = Catalog(root, None, None)  # type: ignore[arg-type]
    catalog.import_pdf(write(tmp_path, pages=PAGES))
    catalog.activate_imports()
    summaries, failures = ProcessedStore(root).ensure(catalog.current()[1]["reports"])
    assert not failures and summaries
    return root


def test_load_corpus_reads_verified_processed_chunks(catalog_root):
    catalog = Catalog(catalog_root, None, None)  # type: ignore[arg-type]
    loaded = load_corpus(catalog, ProcessedStore(catalog_root))
    assert loaded.selection_mode == "local_import"
    assert [r.report_id for r in loaded.reports] == ["ipm-2025-01"]
    assert loaded.reports[0].acquisition_method == "local_import"
    assert any("3,5 %" in c.text and c.page_start == 2 for c in loaded.chunks)


def test_load_corpus_rejects_missing_or_altered_sources(catalog_root):
    catalog = Catalog(catalog_root, None, None)  # type: ignore[arg-type]
    store = ProcessedStore(catalog_root)
    for path in (catalog_root / "processed").iterdir():
        path.write_text("{}")
    with pytest.raises(CorpusError, match="procesar"):
        load_corpus(catalog, store)
    store.ensure(catalog.current()[1]["reports"])
    pdf = next((catalog_root / "raw").iterdir())
    pdf.write_bytes(b"%PDF-alterado")
    with pytest.raises(CorpusError, match="hash"):
        load_corpus(catalog, store)


def test_load_corpus_rejects_cache_for_other_metadata(catalog_root):
    catalog = Catalog(catalog_root, None, None)  # type: ignore[arg-type]
    store = ProcessedStore(catalog_root)
    record = catalog.current()[1]["reports"][0]
    changed = {**record, "report": {**record["report"], "title": "Otro título"}}
    with pytest.raises(Exception, match="no corresponde al snapshot"):
        store.load_verified(changed)


def test_load_corpus_without_catalog(tmp_path):
    with pytest.raises(CorpusError, match="No hay catálogo"):
        load_corpus(Catalog(tmp_path, None, None), ProcessedStore(tmp_path))  # type: ignore[arg-type]


def test_duplicate_chunk_ids_are_rejected(catalog_root, monkeypatch):
    catalog = Catalog(catalog_root, None, None)  # type: ignore[arg-type]
    store = ProcessedStore(catalog_root)
    original = store.load_verified

    def duplicated(record):
        data = original(record)
        return {**data, "chunks": [*data["chunks"], data["chunks"][0]]}

    monkeypatch.setattr(store, "load_verified", duplicated)
    with pytest.raises(CorpusError, match="repetidos"):
        load_corpus(catalog, store)


def run_cli(monkeypatch, capsys, *argv):
    from politica_monetaria_banrep_rag.cli import main

    monkeypatch.setattr("sys.argv", ["banrep-rag", *argv])
    code = 0
    try:
        main()
    except SystemExit as exc:
        code = exc.code or 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_index_search_and_validate_end_to_end(catalog_root, monkeypatch, capsys):
    data = str(catalog_root)
    code, out, err = run_cli(monkeypatch, capsys, "buscar", "inflación", "--data-dir", data)
    assert code == 1 and "indexar" in err
    code, out, _ = run_cli(monkeypatch, capsys, "indexar", "--data-dir", data)
    assert code == 0 and "Índice creado y activado" in out and "importación local" in out
    code, out, _ = run_cli(monkeypatch, capsys, "indexar", "--data-dir", data)
    assert "Sin cambios" in out
    code, out, _ = run_cli(
        monkeypatch, capsys, "buscar", "inflacion basica 3,5 %", "--modo", "lexico",
        "--data-dir", data,
    )  # fmt: skip
    assert code == 0
    assert "pág. 2 del PDF" in out and "periodo 2025-01" in out and "narrative_only" in out
    assert "importación local" in out
    code, out, _ = run_cli(monkeypatch, capsys, "buscar", "petróleo", "--json", "--data-dir", data)
    payload = json.loads(out)
    hit = payload["results"][0]
    assert hit["page_start"] == 3 and hit["report"]["reference_period"] == "2025-01"
    assert hit["report"]["doi"] is None  # importación sin red: no se inventa el DOI
    assert hit["evidence_quality"] == "narrative_only" and hit["table_notes"] == []
    assert payload["corpus"]["reports"][0]["verification_status"] == "local_import"
    code, out, _ = run_cli(monkeypatch, capsys, "validar-indice", "--data-dir", data)
    assert code == 0 and "íntegro" in out
    code, out, err = run_cli(
        monkeypatch, capsys, "buscar", "x", "--desde", "2025-13", "--data-dir", data
    )
    assert code == 2 and "AAAA-MM" in err


def test_cli_validate_reports_stale_index_and_rollback(catalog_root, monkeypatch, capsys):
    data = str(catalog_root)
    run_cli(monkeypatch, capsys, "indexar", "--data-dir", data)
    first = IndexManager(catalog_root).active_id()
    run_cli(monkeypatch, capsys, "indexar", "--forzar", "--data-dir", data)
    assert IndexManager(catalog_root).active_id() != first
    code, out, _ = run_cli(monkeypatch, capsys, "indexar", "--revertir", "--data-dir", data)
    assert code == 0 and first[:12] in out
    pointer = catalog_root / "current.json"
    active = json.loads(pointer.read_text())
    pointer.write_text(json.dumps({**active, "snapshot": "f" * 64}))
    code, out, _ = run_cli(monkeypatch, capsys, "validar-indice", "--data-dir", data)
    assert code == 3 and "dañado" in out


def test_cli_index_failure_reports_previous_index_kept(catalog_root, monkeypatch, capsys):
    data = str(catalog_root)
    run_cli(monkeypatch, capsys, "indexar", "--data-dir", data)
    for path in (catalog_root / "processed").iterdir():
        path.write_text("{}")
    code, _, err = run_cli(monkeypatch, capsys, "indexar", "--forzar", "--data-dir", data)
    assert code == 1 and "no cambió" in err and "procesar" in err


def test_cli_evaluate_reports_metrics_and_rejects_unresolved_judgments(
    catalog_root, monkeypatch, capsys, tmp_path
):
    data = str(catalog_root)
    run_cli(monkeypatch, capsys, "indexar", "--data-dir", data)
    queries = tmp_path / "consultas.json"
    judgment = {"report_id": "ipm-2025-01", "pages": [3], "contains": "precio del petróleo",
                "grade": 2}  # fmt: skip
    queries.write_text(json.dumps({"version": 1, "queries": [
        {"id": "p", "query": "petróleo", "relevant": [judgment]}]}))  # fmt: skip
    code, out, _ = run_cli(
        monkeypatch, capsys, "evaluar", "--consultas", str(queries), "--data-dir", data
    )
    assert code == 0 and "MRR 1.000" in out and "evidencia" in out
    queries.write_text(json.dumps({"version": 1, "queries": [
        {"id": "p", "query": "petróleo", "relevant": [{**judgment, "pages": [2]}]}]}))  # fmt: skip
    code, _, err = run_cli(
        monkeypatch, capsys, "evaluar", "--consultas", str(queries), "--data-dir", data
    )
    assert code == 1 and "juicios sin evidencia" in err
