"""Punto de entrada de los comandos del proyecto."""

import argparse
import json
import os
import sys
from contextlib import ExitStack
from pathlib import Path

from politica_monetaria_banrep_rag import diagnostics
from politica_monetaria_banrep_rag.catalog import Catalog, CatalogError
from politica_monetaria_banrep_rag.crossref import CrossrefReportSource
from politica_monetaria_banrep_rag.download import DownloadError, PdfDownloader
from politica_monetaria_banrep_rag.importer import ImportFileError
from politica_monetaria_banrep_rag.layout import LayoutError
from politica_monetaria_banrep_rag.processed_store import ProcessedStore
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
    args = parser.parse_args()
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
        print("La sincronización del catálogo está disponible; las consultas aún no.")
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
