"""Punto de entrada de los comandos del proyecto."""

import argparse
import ipaddress
import json
import os
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path

from politica_monetaria_banrep_rag import diagnostics, reports
from politica_monetaria_banrep_rag.answering import Answer
from politica_monetaria_banrep_rag.catalog import Catalog, CatalogError
from politica_monetaria_banrep_rag.comparison import (
    Comparison,
)
from politica_monetaria_banrep_rag.corpus import CorpusError, load_corpus
from politica_monetaria_banrep_rag.crossref import CrossrefReportSource
from politica_monetaria_banrep_rag.download import DownloadError, PdfDownloader
from politica_monetaria_banrep_rag.embeddings import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_OLLAMA_URL,
    EmbeddingError,
    OllamaEmbeddings,
)
from politica_monetaria_banrep_rag.evaluation import evaluate, load_queries, unresolved
from politica_monetaria_banrep_rag.generation import (
    DEFAULT_GENERATION_MODEL,
    OllamaGenerator,
)
from politica_monetaria_banrep_rag.importer import ImportFileError
from politica_monetaria_banrep_rag.index_store import (
    ActiveIndex,
    IndexManager,
    IndexStoreError,
    corpus_status,
)
from politica_monetaria_banrep_rag.layout import LayoutError
from politica_monetaria_banrep_rag.processed_store import ProcessedStore
from politica_monetaria_banrep_rag.retrieval import (
    CHUNK_KINDS,
    EVIDENCE_QUALITIES,
    RetrievalHit,
    SearchFilters,
)
from politica_monetaria_banrep_rag.service import (
    DISCLAIMER,
    SEARCH_MODES,
    RagService,
    ServiceError,
)
from politica_monetaria_banrep_rag.source import BanrepSource, ReportSource, SourceError

SOURCES = ("crossref", "historico")


def _make_source(name: str) -> ReportSource:
    if name == "historico":
        return BanrepSource()
    return CrossrefReportSource(contact_email=os.getenv("BANREP_CONTACT_EMAIL") or None)


def _local_failure(exc: OSError) -> str:
    target = f" en {exc.filename}" if exc.filename else ""
    return f"fallo del sistema local{target} ({type(exc).__name__})."


_PLAN_LABELS = {
    "nuevo": "nuevo, se descargará",
    "sin_cambios": "sin cambios, no se descargará",
    "revalidar": "verificación vencida, se volverá a descargar",
    "metadatos_o_archivo_cambiados": "metadatos o archivo local cambiados, se descargará",
    "importado_si_falla": "se intentará descargar; si falla, se usará el PDF importado",
}


def _non_negative(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("debe ser un número") from None
    if not 0 <= number <= 3650:
        raise argparse.ArgumentTypeError("debe estar entre 0 y 3650")
    return number


def _pause(value: str) -> float:
    number = _non_negative(value)
    if number > 600:
        raise argparse.ArgumentTypeError("debe estar entre 0 y 600 segundos")
    return number


def _print_plan(plan: dict[str, object]) -> None:
    window = plan["window"]
    leaving = plan["leaving"]
    assert isinstance(window, list) and isinstance(leaving, list)
    print("Simulación (no se descargó ni modificó nada):")
    for row in window:
        print(f"  {row['report_id']} — {_PLAN_LABELS[row['status']]}")
    for report_id in leaving:
        print(f"  {report_id} — sale de la ventana activa (el PDF se conserva)")


def _data_dir(args: argparse.Namespace) -> Path:
    data_dir = args.data_dir or os.getenv("BANREP_DATA_DIR")
    return Path(data_dir).expanduser() if data_dir else Path.home() / ".local/share/banrep-rag"


def _offline_catalog(root: Path) -> Catalog:
    """Catálogo para operaciones locales: sus clientes HTTP no se usan."""
    return Catalog(root, BanrepSource(), PdfDownloader())


def _import(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.periodo and len(args.archivos) != 1:
        parser.exit(2, "Error: --periodo solo se admite con un único archivo.\n")
    root = _data_dir(args)
    try:
        with ExitStack() as stack:
            catalog = _offline_catalog(root)
            stack.callback(catalog.source.client.close)
            stack.callback(catalog.downloader.client.close)
            lookup = None
            if not args.sin_red:
                lookup = CrossrefReportSource(
                    contact_email=os.getenv("BANREP_CONTACT_EMAIL") or None
                )
                stack.callback(lookup.client.close)
            for path in args.archivos:
                entry = catalog.import_pdf(
                    path,
                    expected_period=args.periodo,
                    lookup=lookup.find if lookup is not None else None,
                    replace=args.reemplazar,
                )
                origin = (
                    "metadatos de Crossref"
                    if entry["metadata_verified"]
                    else ("metadatos sin verificar")
                )
                evidence = ", ".join(str(e) for e in entry["identified_by"])  # type: ignore[attr-defined]
                print(
                    f"Importado {str(entry['path'])[4:15]} desde «{path.name}» "
                    f"({evidence}; {origin})."
                )
            if args.activar:
                active = catalog.current()
                selection = active[1].get("selection") if active is not None else None
                mode = selection.get("mode") if isinstance(selection, dict) else "latest_n"
                if active is not None and mode != "local_import":
                    print(
                        "Aviso: se reemplaza el catálogo activo de los últimos informes por uno "
                        "de importación local; el anterior se conserva en snapshots/.",
                        file=sys.stderr,
                    )
                print(f"Catálogo activo (importación local): {catalog.activate_imports()}")
    except OSError as exc:
        parser.exit(1, f"Error de importación: {_local_failure(exc)}\n")
    except (ImportFileError, CatalogError, SourceError, ValueError) as exc:
        parser.exit(1, f"Error de importación: {exc}\n")


def _process(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    root = _data_dir(args)
    try:
        catalog = _offline_catalog(root)
        with catalog.source.client, catalog.downloader.client:
            active = catalog.current()
        if active is None:
            parser.exit(1, "Error: no hay catálogo activo; sincronice o importe informes.\n")
        records = active[1]["reports"]
        assert isinstance(records, list)
        store = ProcessedStore(root)
        summaries, failures = store.ensure(
            records,
            force=args.forzar,
            workers=args.procesos,
            progress=lambda text: print(text, file=sys.stderr),
        )
    except LayoutError as exc:
        parser.exit(1, f"Error de procesamiento: {exc}\n")
    except OSError as exc:
        parser.exit(1, f"Error de procesamiento: {_local_failure(exc)}\n")
    except CatalogError as exc:
        parser.exit(1, f"Error de procesamiento: {exc}\n")
    if args.json:
        rows = [{**s.__dict__, "path": str(s.path)} for s in summaries]
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        if failures:
            parser.exit(1, "".join(f"Error: {r}: {why}\n" for r, why in failures))
        return
    for s in summaries:
        state = "degradado: " + " ".join(s.warnings) if s.degraded else "calidad aceptable"
        origin = "caché" if s.cached else "procesado ahora"
        print(
            f"{s.report_id} — {s.pages} págs., {s.chunks} fragmentos, {s.tables} cuadros "
            f"({s.tables_failed} sin reconstruir), {s.ligatures_repaired} ligaduras reparadas; "
            f"{state} ({origin})"
        )
    if failures:
        parser.exit(
            1,
            "".join(f"Error de procesamiento en {r}: {why}\n" for r, why in failures)
            + ("Los demás informes se procesaron y quedaron guardados.\n" if summaries else ""),
        )


def _status(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    root = _data_dir(args)
    try:
        catalog = _offline_catalog(root)
        with catalog.source.client, catalog.downloader.client:
            active = catalog.current()
            attempts = catalog.revalidations()
            records = active[1]["reports"] if active else []
            assert isinstance(records, list)
            verification = catalog.verification(records, attempts=attempts) if active else []
    except OSError as exc:
        parser.exit(1, f"Error: {_local_failure(exc)}\n")
    except CatalogError as exc:
        parser.exit(1, f"Error: {exc}\n")
    if active is None:
        if args.json:
            print(json.dumps({"snapshot": None, "verification": [], "revalidations": attempts},
                             ensure_ascii=False, indent=2))  # fmt: skip
            return
        print(f"No hay catálogo activo en {root}. Ejecute «banrep-rag sincronizar» o «importar».")
        for report_id, attempt in sorted(attempts.items()):
            if attempt["status"] != "verified":
                print(
                    f"  {report_id}: descarga no verificada el {attempt['last_attempt_at']} "
                    f"({attempt['error_type']}); sin PDF activo."
                )
        return
    snapshot_id, snapshot = active
    if args.json:
        print(json.dumps({"snapshot": snapshot_id, **snapshot, "verification": verification,
                          "revalidations": attempts}, ensure_ascii=False, indent=2))  # fmt: skip
        return
    selection = snapshot.get("selection") or {"mode": "latest_n"}
    records = snapshot["reports"]
    assert isinstance(selection, dict) and isinstance(records, list)
    mode = (
        "importación local (no equivale a los últimos N informes)"
        if selection.get("mode") == "local_import"
        else f"últimos {selection.get('max_reports', len(records))} informes"
    )
    print(f"Catálogo activo {snapshot_id[:12]} — {mode}")
    active_ids = {record["report"]["report_id"] for record in records}
    for record, state in zip(records, verification, strict=True):
        report, pdf = record["report"], record["pdf"]
        acquisition = record.get("acquisition") or {"method": "download"}
        origin = "descargado"
        if acquisition["method"] == "local_import":
            origin = (
                "importado"
                if acquisition.get("metadata_verified")
                else ("importado, metadatos sin verificar")
            )
        print(
            f"  {report['reference_period']}  {report['title']} — {pdf['pages']} págs., "
            f"{origin}, sha256 {str(pdf['sha256'])[:12]}"
        )
        if state["status"] == "local_import":
            detail = "importación local; PDF no verificado en la fuente"
        elif state["status"] == "verified":
            detail = f"verificado en la fuente el {state['last_successful_verification_at']}"
        elif state["status"] == "snapshot_outdated":
            detail = "el último intento verificó otro PDF; el catálogo activo está desactualizado"
        elif state["status"] == "revalidation_pending":
            detail = "revalidación pendiente; vigencia en la fuente no confirmada"
        else:
            detail = "PDF no disponible para verificar; vigencia en la fuente no confirmada"
        if state["last_attempt_at"]:
            detail += f"; último intento {state['last_attempt_at']}"
        if state["error_type"]:
            detail += f" ({state['error_type']})"
        if not state["local_pdf_valid"]:
            detail += "; PDF local ausente o alterado"
        print(f"    Verificación: {detail}")
    for report_id, attempt in sorted(attempts.items()):
        if report_id not in active_ids and attempt["status"] != "verified":
            print(
                f"  {report_id}: intento fallido el {attempt['last_attempt_at']} "
                f"({attempt['error_type']}); informe fuera del catálogo activo."
            )


_KIND_LABELS = {
    "paragraph": "párrafo",
    "table": "fila de cuadro",
    "table_notes": "notas de cuadro",
    "table_raw": "cuadro sin columnas verificables",
    "chart_caption": "título o fuente de gráfico",
    "caption": "título",
    "source_note": "fuente",
    "footnote": "nota al pie",
}


def _index_status(root: Path, active: ActiveIndex) -> dict[str, object]:
    """Estado del índice frente al catálogo activo y la vigencia efectiva de sus fuentes."""
    catalog = _offline_catalog(root)
    with catalog.source.client, catalog.downloader.client:
        current = catalog.current()
        records = current[1]["reports"] if current else []
        assert isinstance(records, list)
        verification = catalog.verification(records) if current else []
    return corpus_status(active.manifest, current[0] if current else None, verification)


def _ollama(model: str) -> OllamaEmbeddings:
    """Proveedor de embeddings; el aviso de privacidad lo emite quien lo usa, una sola vez."""
    return OllamaEmbeddings(model, base_url=os.getenv("BANREP_OLLAMA_URL") or DEFAULT_OLLAMA_URL)


def _resolve_mode(active: ActiveIndex, mode: str) -> str:
    """``auto``: híbrida si el índice tiene vectores; si no, léxica."""
    if mode != "auto":
        return mode
    return "hibrido" if active.embedding is not None else "lexico"


def _embedder_for(active: ActiveIndex, mode: str) -> OllamaEmbeddings | None:
    """Proveedor con el que se construyó el índice; solo si la vía lo necesita."""
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
    return _ollama(str(embedding["model"]))


def _index(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    root = _data_dir(args)
    manager = IndexManager(root)
    embedder = None
    try:
        if args.revertir:
            index_id = manager.rollback()
            print(f"Índice activo revertido a {index_id[:12]}.")
            return
        catalog = _offline_catalog(root)
        with catalog.source.client, catalog.downloader.client:
            store = ProcessedStore(root)

            def snapshot() -> str | None:
                current = catalog.current()
                return current[0] if current else None

            model = (
                args.modelo_embeddings
                or os.getenv("BANREP_EMBEDDING_MODEL")
                or DEFAULT_EMBEDDING_MODEL
            )
            embedder = None if args.sin_embeddings else _ollama(model)
            if embedder is not None:
                _print_warnings(RagService(root).privacy_warnings())
            result = manager.build(
                lambda: load_corpus(catalog, store),
                snapshot,
                force=args.forzar,
                embedder=embedder,
                progress=lambda text: print(text, file=sys.stderr),
            )
    except EmbeddingError as exc:
        parser.exit(
            1,
            f"Error de indexación: {exc}\nEl índice activo anterior no cambió. Para indexar sin "
            "Ollama (solo búsqueda léxica) use «--sin-embeddings».\n",
        )
    except (CorpusError, IndexStoreError, CatalogError) as exc:
        parser.exit(1, f"Error de indexación: {exc}\nEl índice activo anterior no cambió.\n")
    except OSError as exc:
        parser.exit(
            1, f"Error de indexación: {_local_failure(exc)}\nEl índice activo anterior no cambió.\n"
        )
    except ValueError as exc:
        parser.exit(2, f"Error: {exc}\n")
    finally:
        if embedder is not None:
            embedder.close()
    manifest = result.manifest
    if args.json:
        print(json.dumps({"index_id": result.index_id, "created": result.created, **manifest},
                         ensure_ascii=False, indent=2))  # fmt: skip
        return
    verb = "Índice creado y activado" if result.created else "Sin cambios: el índice activo"
    reports = manifest["reports"]
    assert isinstance(reports, list)
    print(
        f"{verb} {result.index_id[:12]} — {manifest['chunk_count']} fragmentos de "
        f"{len(reports)} informes (snapshot {str(manifest['catalog_snapshot'])[:12]})."
    )
    vector = manifest.get("vector")
    if isinstance(vector, dict):
        print(f"Vectores: {vector['model']} ({vector['dimension']} dimensiones, FAISS exacto).")
    else:
        print("Sin vectores: solo búsqueda léxica (use --modelo-embeddings para la semántica).")
    if manifest["selection_mode"] == "local_import":
        print("Aviso: corpus de importación local; no equivale a los últimos informes publicados.")


def _validate_index(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    root = _data_dir(args)
    try:
        active = IndexManager(root).open_active()
    except IndexStoreError as exc:
        parser.exit(1, f"Índice no válido: {exc}\n")
    model_ok = True
    extra: list[str] = []
    try:
        status = _index_status(root, active)
    except CatalogError as exc:
        status = {"index_matches_catalog": False, "warnings": [str(exc)], "reports": []}
    try:
        embedder = _embedder_for(active, "semantico") if active.embedding else None
        if embedder is not None:
            try:
                active.check_embedder(embedder)
            finally:
                embedder.close()
    except IndexStoreError as exc:
        model_ok = False
        extra.append(str(exc))
    except EmbeddingError as exc:
        extra.append(f"No se pudo comprobar el modelo de embeddings: {exc}")
    finally:
        active.close()
    previous = status["warnings"]
    assert isinstance(previous, list)
    status["warnings"] = [*previous, *extra]
    status["embedding_model_matches"] = model_ok
    if args.json:
        print(json.dumps({"index_id": active.index_id, "integrity": "ok", **status},
                         ensure_ascii=False, indent=2))  # fmt: skip
    else:
        print(
            f"Índice {active.index_id[:12]} íntegro: manifiesto, huellas de archivos y "
            f"{active.manifest['chunk_count']} fragmentos verificados."
        )
        reports, warnings = status["reports"], status["warnings"]
        assert isinstance(reports, list) and isinstance(warnings, list)
        for report in reports:
            print(f"  {report['report_id']} — vigencia: {report['verification_status']}")
        for warning in warnings:
            print(f"Aviso: {warning}")
    if not status["index_matches_catalog"] or not model_ok:
        parser.exit(3, "")


def _signal(hit: RetrievalHit) -> str:
    parts = []
    if hit.fused_score is not None:
        parts.append(f"RRF {hit.fused_score:.4f}")
    if hit.lexical_rank is not None:
        parts.append(f"BM25 {hit.lexical_bm25:.3f} (n.º {hit.lexical_rank})")
    if hit.semantic_rank is not None:
        parts.append(f"similitud {hit.semantic_similarity:.3f} (n.º {hit.semantic_rank})")
    return " · ".join(parts)


def _print_hit(hit: RetrievalHit, notes: list[str]) -> None:
    chunk, report = hit.chunk, hit.report
    pages = (
        f"pág. {chunk.page_start}"
        if chunk.page_start == chunk.page_end
        else f"págs. {chunk.page_start}-{chunk.page_end}"
    )
    printed = f" (impresa {', '.join(chunk.page_labels)})" if chunk.page_labels else ""
    print(
        f"{hit.rank}. {report.title} — periodo {report.reference_period}, {pages} del PDF{printed}"
    )
    print(f"   Sección: {' > '.join(chunk.section) or '(sin sección)'}")
    print(
        f"   Tipo: {_KIND_LABELS.get(chunk.kind, chunk.kind)} · calidad: {chunk.evidence_quality}"
        f" · celdas atribuibles: {len(chunk.attributed_cells)} · {_signal(hit)}"
    )
    excerpt = " ".join(chunk.text.split())
    print(f"   {excerpt[:400]}{'…' if len(excerpt) > 400 else ''}")
    if chunk.kind == "table_raw":
        print("   Advertencia: cuadro sin estructura verificable; sus cifras no están atribuidas.")
    if notes:
        first = " ".join(notes[0].split())
        print(
            f"   Notas del cuadro ({len(notes)} fragmento(s), mismo table_id): "
            f"{first[:240]}{'…' if len(first) > 240 else ''}"
        )
    print(f"   Fuente: {report.source_url or '(sin URL verificada en Crossref)'}")


def _service(args: argparse.Namespace) -> RagService:
    """Servicio con los proveedores de este módulo (se resuelven al usarse; útil en pruebas)."""
    url = os.getenv("BANREP_OLLAMA_URL") or DEFAULT_OLLAMA_URL
    return RagService(
        _data_dir(args),
        embedder_factory=lambda model: _ollama(model),
        generator_factory=lambda model: OllamaGenerator(model, base_url=url),
        generation_model=getattr(args, "modelo", None),
    )


_EXIT_CODES = {"invalid": 2, "not_found": 2, "unavailable": 1}


def _fail(parser: argparse.ArgumentParser, prefix: str, exc: ServiceError) -> None:
    _print_warnings(exc.warnings)  # p. ej., el aviso de privacidad, aunque la operación falle
    parser.exit(_EXIT_CODES.get(exc.kind, 1), f"{prefix}: {exc}\n")


def _print_warnings(warnings: list[str]) -> None:
    for warning in warnings:
        print(f"Aviso: {warning}", file=sys.stderr)


def _search(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    try:
        filters = _filters(args)
    except ValueError as exc:
        parser.exit(2, f"Error: {exc}\n")
    try:
        result = _service(args).search(args.consulta, filters, top_k=args.top_k, mode=args.modo)
    except ServiceError as exc:
        _fail(parser, "Error de búsqueda", exc)
    _print_warnings(result.warnings)
    if args.json:
        rows = [{**hit.to_dict(), "table_notes": result.notes[hit.chunk.chunk_id]}
                for hit in result.hits]  # fmt: skip
        payload = {"index_id": result.index_id, "mode": result.mode, "query": args.consulta,
                   "results": rows, "corpus": result.corpus}  # fmt: skip
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    warnings = result.corpus.get("warnings", [])
    assert isinstance(warnings, list)
    for warning in warnings:
        print(f"Aviso: {warning}")
    print(f"Modo: {result.mode}.")
    if not result.hits:
        print("Sin resultados en el índice activo para esa consulta y esos filtros.")
        return
    for hit in result.hits:
        _print_hit(hit, result.notes[hit.chunk.chunk_id])


def _filters(args: argparse.Namespace) -> SearchFilters:
    return SearchFilters(
        report_ids=tuple(args.informe or ()),
        period_from=args.desde,
        period_to=args.hasta,
        section=args.seccion,
        kinds=tuple(args.tipo or ()),
        evidence_qualities=tuple(args.calidad or ()),
    )


def _print_answer(answer: Answer) -> None:
    labels = {
        "answered": "Respuesta",
        "insufficient_evidence": "Sin respuesta (evidencia insuficiente)",
        "rejected": "Respuesta rechazada por la verificación",
    }
    print(f"{labels[answer.status]}:\n{answer.text}")
    if answer.reason:
        print(f"Motivo: {answer.reason}")
    if answer.added_citations:
        print(
            "Fuentes añadidas por la verificación (respaldan cifras que el modelo no citó): "
            + ", ".join(answer.added_citations)
        )
    shown = answer.citations or (answer.evidence if answer.status != "answered" else ())
    if shown:
        print("\nFuentes citadas:" if answer.citations else "\nEvidencia revisada:")
    for citation in shown:
        pages = (
            f"pág. {citation.page_start}"
            if citation.page_start == citation.page_end
            else f"págs. {citation.page_start}-{citation.page_end}"
        )
        printed = f" (impresa {', '.join(citation.page_labels)})" if citation.page_labels else ""
        source = citation.source_url or "(sin URL verificada)"
        print(
            f"  [{citation.label}] {citation.report_title} ({citation.reference_period}), "
            f"{pages} del PDF{printed}; {citation.evidence_quality}. {source}"
        )
    print(f"\n{DISCLAIMER}")


def _check_export(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Valida «--exportar» antes de la operación: no se pierde una respuesta ya calculada."""
    target = getattr(args, "exportar", None)
    if target is None:
        return
    if target.suffix.lower().lstrip(".") not in reports.FORMATS:
        parser.exit(2, "Error: el archivo del reporte debe terminar en .md, .html o .pdf.\n")
    if target.is_symlink():
        parser.exit(2, f"Error: {target} es un enlace simbólico; indique un archivo normal.\n")
    if target.exists() and not args.sobrescribir:
        parser.exit(2, f"Error: {target} ya existe; use «--sobrescribir» para reemplazarlo.\n")
    if not target.parent.is_dir():
        parser.exit(2, f"Error: no existe el directorio {target.parent}.\n")


def _export(parser: argparse.ArgumentParser, args: argparse.Namespace, build) -> None:  # type: ignore[no-untyped-def]
    """Escribe el reporte (tras mostrar el resultado). El temporal se crea con O_EXCL en el mismo
    directorio, no sigue enlaces simbólicos y se elimina ante cualquier fallo o interrupción."""
    target = getattr(args, "exportar", None)
    if target is None:
        return
    data = reports.render(build(), target.suffix.lower().lstrip("."))
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".reporte-", suffix=".tmp", dir=target.parent)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if target.is_symlink():
            raise IsADirectoryError(f"{target} es un enlace simbólico")
        os.replace(tmp_name, target)
        tmp_name = None
    except OSError as exc:
        parser.exit(1, f"Error al guardar el reporte (el resultado ya se mostró): {exc}\n")
    finally:
        if tmp_name is not None:
            Path(tmp_name).unlink(missing_ok=True)
    print(f"Reporte guardado en {target}.", file=sys.stderr)


def _query(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    try:
        filters = _filters(args)
    except ValueError as exc:
        parser.exit(2, f"Error: {exc}\n")
    _check_export(parser, args)
    try:
        result = _service(args).ask(args.pregunta, filters, top_k=args.top_k, mode=args.modo)
    except ServiceError as exc:
        _fail(parser, "Error de consulta", exc)
    _print_warnings(result.warnings)
    if result.detected_reports:
        print(
            f"Informes mencionados en la pregunta: {', '.join(result.detected_reports)}.",
            file=sys.stderr if args.json else sys.stdout,
        )
    build = lambda: reports.report_from_query(result, args.pregunta)  # noqa: E731
    if args.json:
        payload = {
            "index_id": result.index_id,
            "retrieval_mode": result.mode,
            "generation_model": result.model,
            "question": args.pregunta,
            "detected_reports": list(result.detected_reports),
            **asdict(result.answer),
            "disclaimer": DISCLAIMER,
            "corpus": result.corpus,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        _export(parser, args, build)
        return
    warnings = result.corpus.get("warnings", [])
    assert isinstance(warnings, list)
    for warning in warnings:
        print(f"Aviso: {warning}")
    _print_answer(result.answer)
    _export(parser, args, build)


_DIFFERENCE_KINDS = {
    "revision_de_proyeccion": "revisión de proyección",
    "proyeccion_vs_observado": "proyección frente al dato observado",
    "observado_vs_proyeccion": "dato observado frente a proyección",
    "observados": "entre cifras no marcadas como proyección",
}


def _number(value: object, *, signed: bool = False) -> str:
    text = str(value).replace(".", ",")
    return f"+{text}" if signed and not text.startswith("-") and text.strip("0,") else text


def _print_comparison(comparison: Comparison, notes: dict[str, str]) -> None:
    names = ", ".join(f"{r.reference_period} ({r.title})" for r in comparison.reports)
    print(f"Comparación de «{comparison.indicator}» — informes: {names}")
    print(
        "Cifras tomadas solo de celdas atribuidas de cuadros; diferencias calculadas en Python, "
        "no por el modelo de lenguaje."
    )
    if comparison.interpretation:
        print(
            f"Filas buscadas con: {' + '.join(comparison.interpretation)} "
            "(precise con --indicador)."
        )
    for warning in comparison.warnings:
        print(f"Aviso: {warning}")
    current_table = None
    for row in comparison.rows:
        if row.table != current_table:
            current_table = row.table
            print(f"\n{row.table}")
        unit = f" ({row.unit})" if row.unit else ""
        print(f"  {row.indicator} — horizonte {row.horizon}{unit}")
        for value in row.values:
            if value is None:
                continue
            kind = "proyección o supuesto" if value.projection else "no marcada como proyección"
            printed = f" (impresa {', '.join(value.page_labels)})" if value.page_labels else ""
            print(
                f"    {value.reference_period}: {value.value_text} ({kind}) — pág. "
                f"{value.page_start} del PDF{printed}"
            )
        for difference in row.differences:
            print(
                f"    Diferencia {difference.from_report} → {difference.to_report}: "
                f"{_number(difference.value, signed=True)} {row.difference_unit} "
                f"({_DIFFERENCE_KINDS[difference.kind]})"
            )
        if not row.comparable:
            print("    No comparable.")
        for note in row.notes:
            print(f"    Nota: {note}")
    if comparison.truncated:
        print("\nSe muestran las primeras filas; precise --horizonte o --cuadro.")
    for table_id, text in notes.items():
        print(f"\nNotas del cuadro ({table_id[:8]}): {text[:400]}{'…' if len(text) > 400 else ''}")
    print("\nFuentes:")
    for report in comparison.reports:
        print(f"  {report.reference_period}: {report.title}. {report.source_url or '(sin URL)'}")
    print(DISCLAIMER)


def _compare(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    _check_export(parser, args)
    try:
        result = _service(args).compare(
            question=args.pregunta,
            reports=tuple(args.informe or ()),
            indicator=args.indicador,
            horizon=args.horizonte,
            table=args.cuadro,
            include_observed=args.incluir_observados,
        )
    except ServiceError as exc:
        _fail(parser, "Error", exc)

    def build() -> reports.Report:
        return reports.report_from_comparison(
            result,
            {
                "Pregunta": args.pregunta,
                "Informes": ", ".join(args.informe or ()) or None,
                "Indicador": args.indicador,
                "Horizonte": args.horizonte,
                "Cuadro": args.cuadro,
            },
        )

    if args.json:
        payload = {
            "index_id": result.index_id,
            "missing_reports": list(result.missing_reports),
            **asdict(result.comparison),
            "table_notes": result.table_notes,
            "corpus": result.corpus,
            "disclaimer": DISCLAIMER,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        _export(parser, args, build)
        return
    corpus_warnings = result.corpus.get("warnings", [])
    assert isinstance(corpus_warnings, list)
    for warning in [*result.warnings, *corpus_warnings]:
        print(f"Aviso: {warning}")
    _print_comparison(result.comparison, result.table_notes)
    _export(parser, args, build)


def _summarize(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Resumen trazable de un informe (o de un tema en él) con el flujo verificado de consultar."""
    _check_export(parser, args)
    try:
        result = _service(args).summarize(
            args.informe, topic=args.tema, top_k=args.top_k, mode=args.modo
        )
    except ServiceError as exc:
        _fail(parser, "Error de resumen", exc)
    _print_warnings(result.warnings)
    build = lambda: reports.report_from_summary(result)  # noqa: E731
    if args.json:
        payload = {"index_id": result.index_id, "report_id": result.report.report_id,
                   "topic": args.tema, "generation_model": result.model,
                   **asdict(result.answer), "corpus": result.corpus,
                   "disclaimer": DISCLAIMER}  # fmt: skip
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        _export(parser, args, build)
        return
    warnings = result.corpus.get("warnings", [])
    assert isinstance(warnings, list)
    for warning in warnings:
        print(f"Aviso: {warning}")
    print(f"Resumen de {result.report.title} ({result.report.reference_period}) — {result.topic}")
    print(
        "Cada cifra se verificó contra la evidencia de este informe; si el modelo no citó su "
        "fuente, se agrega."
    )
    _print_answer(result.answer)
    _export(parser, args, build)


def _is_local_host(host: str) -> bool:
    try:
        return host == "localhost" or ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _serve(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """API y web locales; exponerlas a la red exige una decisión explícita."""
    if not _is_local_host(args.host) and not args.permitir_red:
        parser.exit(
            2,
            f"Error: {args.host} no es una dirección local. La aplicación no tiene "
            "autenticación; use «--permitir-red» solo detrás de controles propios (ADR 0011).\n",
        )
    import uvicorn

    from politica_monetaria_banrep_rag.api import LOCAL_HOSTS, create_app

    hosts = ["*"] if args.permitir_red else [*LOCAL_HOSTS, args.host]
    app = create_app(_service(args), allowed_hosts=hosts)
    print(f"Sirviendo en http://{args.host}:{args.port} (Ctrl+C para detener). {DISCLAIMER}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


def _evaluate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    try:
        queries = load_queries(args.consultas)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"Error: consultas de evaluación no válidas ({exc}).\n")
    try:
        active = IndexManager(_data_dir(args)).open_active()
    except IndexStoreError as exc:
        parser.exit(1, f"Error de evaluación: {exc}\n")
    embedder = None
    try:
        mode = _resolve_mode(active, args.modo)
        embedder = _embedder_for(active, mode)
        chunks = active.lexical.all_chunks()
        problems = unresolved(queries, chunks)
        if problems:
            parser.exit(
                1,
                "Error: juicios sin evidencia en el índice (revise las consultas o el "
                "procesamiento): " + "; ".join(problems) + "\n",
            )
        result = evaluate(
            lambda text, filters, k: active.search(
                text, filters, top_k=k, mode=mode, embedder=embedder
            ),
            queries,
            indexed_reports={chunk.report_id for chunk in chunks},
            k=args.k,
        )
    except (IndexStoreError, EmbeddingError) as exc:
        parser.exit(
            1, f"Error de evaluación: {exc}\nLa vía léxica no necesita Ollama: «--modo lexico».\n"
        )
    finally:
        active.close()
        if embedder is not None:
            embedder.close()
    result = {"index_id": active.index_id, "mode": mode, **result}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(
        f"Evaluación {mode} en el índice {active.index_id[:12]}: {result['answerable']} "
        f"consultas con respuesta y {int(str(result['queries'])) - int(str(result['answerable']))}"
        f" sin respuesta; k={args.k}."
    )
    metrics = result["metrics"]
    assert isinstance(metrics, dict)
    for level, label in (("document", "documento"), ("page", "página"), ("evidence", "evidencia")):
        values = metrics[level]
        print(
            f"  {label:<10} P@{args.k} {values['precision']:.3f}  R@{args.k} "
            f"{values['recall']:.3f}  MRR {values['mrr']:.3f}  nDCG@{args.k} {values['ndcg']:.3f}"
        )
    unfiltered = result["metrics_without_report_filter"]
    assert isinstance(unfiltered, dict)
    print(
        f"  Sin filtro de informe ({result['queries_without_report_filter']} consultas): "
        f"documento MRR {unfiltered['document']['mrr']:.3f}, página MRR "
        f"{unfiltered['page']['mrr']:.3f}, evidencia MRR {unfiltered['evidence']['mrr']:.3f}"
    )
    latency = result["latency_ms"]
    assert isinstance(latency, dict)
    print(f"  Latencia por consulta: p50 {latency['p50']} ms, p95 {latency['p95']} ms.")
    if result["skipped_missing_reports"]:
        print(f"  Omitidas (informes no indexados): {result['skipped_missing_reports']}")


def _add_filters(command: argparse.ArgumentParser) -> None:
    command.add_argument("--informe", action="append", metavar="ipm-AAAA-MM")
    command.add_argument("--desde", metavar="AAAA-MM", help="Periodo de referencia inicial")
    command.add_argument("--hasta", metavar="AAAA-MM", help="Periodo de referencia final")
    command.add_argument("--seccion", help="Texto contenido en el nombre de la sección")
    command.add_argument("--tipo", action="append", choices=sorted(CHUNK_KINDS))
    command.add_argument("--calidad", action="append", choices=sorted(EVIDENCE_QUALITIES))


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="banrep-rag", description="Consulta local de informes de política monetaria."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("diagnostico", help="Muestra las capacidades disponibles")
    sources_check = subcommands.add_parser(
        "diagnostico-fuentes", help="Prueba el acceso a Crossref, al histórico y al repositorio"
    )
    sources_check.add_argument("--json", action="store_true", help="Emite el informe en JSON")
    sources_check.add_argument(
        "--descargar-uno",
        action="store_true",
        help="Descarga y verifica el PDF más reciente en un directorio temporal",
    )
    discover = subcommands.add_parser(
        "descubrir", help="Verifica los informes principales oficiales"
    )
    discover.add_argument("--max-reports", help="Cantidad de informes (por defecto: 12)")
    discover.add_argument("--json", action="store_true", help="Emite metadatos en JSON")
    discover.add_argument("--fuente", choices=SOURCES, help="Origen de los metadatos")
    sync = subcommands.add_parser(
        "sincronizar", help="Descarga una ventana completa y activa el catálogo local"
    )
    sync.add_argument("--max-reports", help="Cantidad de informes (por defecto: 12)")
    sync.add_argument("--data-dir", help="Directorio del catálogo y los PDF locales")
    sync.add_argument("--fuente", choices=SOURCES, help="Origen de los metadatos")
    sync.add_argument(
        "--dry-run", action="store_true", help="Muestra qué cambiaría sin descargar nada"
    )
    sync.add_argument(
        "--usar-importados",
        action="store_true",
        help="Si una descarga falla, usa el PDF importado localmente para ese informe",
    )
    sync.add_argument(
        "--revalidar-dias",
        type=_non_negative,
        default=30.0,
        help="Días tras los cuales se vuelve a descargar un PDF sin cambios (por defecto: 30)",
    )
    sync.add_argument(
        "--pausa",
        type=_pause,
        default=10.0,
        help="Segundos de espera entre descargas reales (por defecto: 10)",
    )
    importer_cmd = subcommands.add_parser(
        "importar", help="Importa PDF oficiales obtenidos legítimamente (procedencia local)"
    )
    importer_cmd.add_argument("archivos", nargs="+", type=Path, help="PDF a importar")
    importer_cmd.add_argument("--periodo", help="AAAA-MM; solo con un archivo, para confirmar")
    importer_cmd.add_argument("--data-dir", help="Directorio del catálogo y los PDF locales")
    importer_cmd.add_argument(
        "--activar", action="store_true", help="Activa un catálogo con los PDF importados"
    )
    importer_cmd.add_argument(
        "--sin-red", action="store_true", help="No consulta Crossref (metadatos sin verificar)"
    )
    importer_cmd.add_argument(
        "--reemplazar",
        action="store_true",
        help="Sustituye un PDF importado antes para el mismo periodo",
    )
    process_cmd = subcommands.add_parser(
        "procesar", help="Extrae texto, cuadros y fragmentos citables del catálogo activo"
    )
    process_cmd.add_argument("--data-dir", help="Directorio del catálogo y los PDF locales")
    process_cmd.add_argument("--forzar", action="store_true", help="Ignora la caché")
    process_cmd.add_argument(
        "--procesos", type=int, default=1, choices=range(1, 9), metavar="N",
        help="Procesos en paralelo (1 a 8; por defecto 1)",
    )  # fmt: skip
    process_cmd.add_argument("--json", action="store_true", help="Emite el resumen en JSON")
    status_cmd = subcommands.add_parser("estado", help="Muestra el catálogo local activo")
    status_cmd.add_argument("--data-dir", help="Directorio del catálogo y los PDF locales")
    status_cmd.add_argument("--json", action="store_true", help="Emite el snapshot en JSON")
    index_cmd = subcommands.add_parser(
        "indexar", help="Construye y activa el índice de búsqueda del catálogo procesado"
    )
    index_cmd.add_argument("--data-dir", help="Directorio del catálogo y los PDF locales")
    index_cmd.add_argument(
        "--forzar", action="store_true", help="Reconstruye aunque el índice activo esté al día"
    )
    index_cmd.add_argument(
        "--revertir", action="store_true", help="Reactiva el índice anterior conservado"
    )
    index_cmd.add_argument(
        "--modelo-embeddings",
        metavar="MODELO",
        help="Modelo de Ollama para la vía semántica (o BANREP_EMBEDDING_MODEL; "
        f"por defecto {DEFAULT_EMBEDDING_MODEL})",
    )
    index_cmd.add_argument(
        "--sin-embeddings", action="store_true", help="Construye solo el índice léxico (sin Ollama)"
    )
    index_cmd.add_argument("--json", action="store_true", help="Emite el manifiesto en JSON")
    search_cmd = subcommands.add_parser("buscar", help="Busca fragmentos citables en el índice")
    search_cmd.add_argument("consulta", help="Texto de la consulta")
    search_cmd.add_argument("--data-dir", help="Directorio del catálogo y los PDF locales")
    search_cmd.add_argument(
        "--modo",
        choices=SEARCH_MODES,
        default="auto",
        help="Vía de recuperación (auto: híbrida si el índice tiene vectores)",
    )
    search_cmd.add_argument(
        "--top-k", type=int, default=5, choices=range(1, 51), metavar="K",
        help="Cantidad de resultados (1 a 50; por defecto 5)",
    )  # fmt: skip
    _add_filters(search_cmd)
    search_cmd.add_argument("--json", action="store_true", help="Emite resultados en JSON")
    query_cmd = subcommands.add_parser(
        "consultar", help="Responde una pregunta con evidencia citada (Ollama local)"
    )
    query_cmd.add_argument("pregunta", help="Pregunta en lenguaje natural")
    query_cmd.add_argument("--data-dir", help="Directorio del catálogo y los PDF locales")
    query_cmd.add_argument(
        "--modo", choices=SEARCH_MODES, default="auto", help="Vía de recuperación de evidencia"
    )
    query_cmd.add_argument(
        "--top-k", type=int, default=6, choices=range(1, 13), metavar="K",
        help="Fragmentos de evidencia para el modelo (1 a 12; por defecto 6)",
    )  # fmt: skip
    query_cmd.add_argument(
        "--modelo",
        help=f"Modelo de generación de Ollama (o BANREP_GENERATION_MODEL; por defecto "
        f"{DEFAULT_GENERATION_MODEL})",
    )
    _add_filters(query_cmd)
    query_cmd.add_argument(
        "--exportar", type=Path, metavar="ARCHIVO",
        help="Guarda un reporte (.md, .html o .pdf) con citas, metodología y advertencias",
    )  # fmt: skip
    query_cmd.add_argument(
        "--sobrescribir", action="store_true", help="Reemplaza el archivo del reporte si existe"
    )
    query_cmd.add_argument("--json", action="store_true", help="Emite la respuesta en JSON")
    compare_cmd = subcommands.add_parser(
        "comparar", help="Compara cifras de cuadros entre informes (cálculo determinista)"
    )
    compare_cmd.add_argument("pregunta", nargs="?", help="Pregunta con los informes a comparar")
    compare_cmd.add_argument("--data-dir", help="Directorio del catálogo y los PDF locales")
    compare_cmd.add_argument(
        "--informe", action="append", metavar="ipm-AAAA-MM|AAAA-MM",
        help="Informe a comparar (repetible; al menos dos)",
    )  # fmt: skip
    compare_cmd.add_argument("--indicador", help="Fila del cuadro, p. ej. «IPC total»")
    compare_cmd.add_argument("--horizonte", help="Columna exacta, p. ej. «2025» o «dic-25»")
    compare_cmd.add_argument("--cuadro", help="Texto del título del cuadro, p. ej. «Anexo 2»")
    compare_cmd.add_argument(
        "--incluir-observados", action="store_true",
        help="Incluye filas sin cifras marcadas como proyección (negrilla o «proy»)",
    )  # fmt: skip
    compare_cmd.add_argument(
        "--exportar", type=Path, metavar="ARCHIVO",
        help="Guarda un reporte (.md, .html o .pdf) con citas, metodología y advertencias",
    )  # fmt: skip
    compare_cmd.add_argument(
        "--sobrescribir", action="store_true", help="Reemplaza el archivo del reporte si existe"
    )
    compare_cmd.add_argument("--json", action="store_true", help="Emite la comparación en JSON")
    summary_cmd = subcommands.add_parser(
        "resumir", help="Resume un informe o un tema con evidencia citada (Ollama local)"
    )
    summary_cmd.add_argument("informe", help="Informe: ipm-AAAA-MM o AAAA-MM")
    summary_cmd.add_argument("--tema", help="Tema del resumen (por defecto, el resumen general)")
    summary_cmd.add_argument("--data-dir", help="Directorio del catálogo y los PDF locales")
    summary_cmd.add_argument("--modo", choices=SEARCH_MODES, default="auto")
    summary_cmd.add_argument(
        "--top-k", type=int, default=8, choices=range(1, 13), metavar="K",
        help="Fragmentos de evidencia (1 a 12; por defecto 8)",
    )  # fmt: skip
    summary_cmd.add_argument("--modelo", help="Modelo de generación de Ollama")
    summary_cmd.add_argument(
        "--exportar", type=Path, metavar="ARCHIVO",
        help="Guarda un reporte (.md, .html o .pdf) con citas, metodología y advertencias",
    )  # fmt: skip
    summary_cmd.add_argument(
        "--sobrescribir", action="store_true", help="Reemplaza el archivo del reporte si existe"
    )
    summary_cmd.add_argument("--json", action="store_true", help="Emite el resumen en JSON")
    serve_cmd = subcommands.add_parser("servir", help="Inicia la API y la interfaz web locales")
    serve_cmd.add_argument("--data-dir", help="Directorio del catálogo y los PDF locales")
    serve_cmd.add_argument("--host", default="127.0.0.1", help="Dirección (por defecto 127.0.0.1)")
    serve_cmd.add_argument(
        "--port", type=int, default=8000, choices=range(1024, 65536), metavar="PUERTO",
        help="Puerto (1024 a 65535; por defecto 8000)",
    )  # fmt: skip
    serve_cmd.add_argument(
        "--permitir-red", action="store_true",
        help="Permite escuchar fuera de localhost (sin autenticación: bajo su responsabilidad)",
    )  # fmt: skip
    serve_cmd.add_argument("--modelo", help="Modelo de generación de Ollama")
    validate_cmd = subcommands.add_parser(
        "validar-indice", help="Comprueba la integridad del índice activo y su vigencia"
    )
    validate_cmd.add_argument("--data-dir", help="Directorio del catálogo y los PDF locales")
    validate_cmd.add_argument("--json", action="store_true", help="Emite el resultado en JSON")
    evaluate_cmd = subcommands.add_parser(
        "evaluar", help="Mide la recuperación con consultas de relevancia verificada"
    )
    evaluate_cmd.add_argument("--data-dir", help="Directorio del catálogo y los PDF locales")
    evaluate_cmd.add_argument(
        "--consultas", type=Path, default=Path("evaluacion/consultas_recuperacion.json"),
        help="Archivo JSON de consultas y juicios",
    )  # fmt: skip
    evaluate_cmd.add_argument(
        "--k", type=int, default=5, choices=range(1, 51), metavar="K", help="Corte (por defecto 5)"
    )
    evaluate_cmd.add_argument("--modo", choices=SEARCH_MODES, default="auto")
    evaluate_cmd.add_argument("--json", action="store_true", help="Emite el informe en JSON")
    args = parser.parse_args()
    handler = {
        "indexar": _index,
        "buscar": _search,
        "validar-indice": _validate_index,
        "evaluar": _evaluate,
        "consultar": _query,
        "comparar": _compare,
        "resumir": _summarize,
        "servir": _serve,
    }.get(args.command)
    if handler is not None:
        handler(parser, args)
        return
    if args.command == "importar":
        _import(parser, args)
        return
    if args.command == "estado":
        _status(parser, args)
        return
    if args.command == "procesar":
        _process(parser, args)
        return
    if args.command == "diagnostico":
        print(
            "Disponibles: sincronización, procesamiento, índice híbrido, búsqueda y consultas "
            "con evidencia citada y comparaciones de cuadros entre informes."
        )
    elif args.command == "diagnostico-fuentes":
        access = diagnostics.run(download_one=args.descargar_uno)
        if args.json:
            print(json.dumps(access, ensure_ascii=False, indent=2))
        else:
            print(diagnostics.render(access))
    elif args.command in {"descubrir", "sincronizar"}:
        value = (
            args.max_reports
            if args.max_reports is not None
            else os.getenv("BANREP_MAX_REPORTS", "12")
        )
        try:
            max_reports = int(value)
        except ValueError:
            parser.exit(
                2, "Error: la cantidad de informes debe ser un número entero entre 1 y 100.\n"
            )
        if not 1 <= max_reports <= 100:
            parser.exit(2, "Error: la cantidad de informes debe estar entre 1 y 100.\n")
        source_name = args.fuente or os.getenv("BANREP_SOURCE", "crossref")
        if source_name not in SOURCES:
            parser.exit(2, "Error: la fuente debe ser «crossref» o «historico».\n")
        if args.command == "sincronizar":
            root = _data_dir(args)
            try:
                with ExitStack() as stack:
                    source = _make_source(source_name)
                    stack.callback(source.client.close)
                    downloader = PdfDownloader()
                    stack.callback(downloader.client.close)
                    catalog = Catalog(
                        root,
                        source,
                        downloader,
                        progress=lambda text: print(text, file=sys.stderr),
                        revalidate_seconds=args.revalidar_dias * 86400,
                        download_pause=args.pausa,
                    )
                    if args.dry_run:
                        _print_plan(catalog.plan(max_reports, use_imports=args.usar_importados))
                        return
                    snapshot_id = catalog.sync(max_reports, use_imports=args.usar_importados)
            except OSError as exc:
                parser.exit(1, f"Error de sincronización: {_local_failure(exc)}\n")
            except (SourceError, DownloadError, CatalogError, ValueError) as exc:
                parser.exit(1, f"Error de sincronización: {exc}\n")
            print(f"Catálogo activo: {snapshot_id}")
            return
        try:
            with ExitStack() as stack:
                source = _make_source(source_name)
                stack.callback(source.client.close)
                reports = source.discover(max_reports)
        except OSError as exc:
            parser.exit(1, f"Error de descubrimiento: {_local_failure(exc)}\n")
        except (SourceError, ValueError) as exc:
            parser.exit(1, f"Error de descubrimiento: {exc}\n")
        if args.json:
            print(
                json.dumps([report.to_dict() for report in reports], ensure_ascii=False, indent=2)
            )
        else:
            for report in reports:
                print(f"{report.reference_period} — {report.title}: {report.download_url}")


if __name__ == "__main__":
    main()
