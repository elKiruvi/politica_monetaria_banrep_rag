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
}


def _non_negative(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("debe ser un número") from None
    if not 0 <= number <= 3650:
        raise argparse.ArgumentTypeError("debe estar entre 0 y 3650")
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
        "--revalidar-dias",
        type=_non_negative,
        default=30.0,
        help="Días tras los cuales se vuelve a descargar un PDF sin cambios (por defecto: 30)",
    )
    sync.add_argument(
        "--pausa",
        type=_non_negative,
        default=10.0,
        help="Segundos de espera entre descargas reales (por defecto: 10)",
    )
    args = parser.parse_args()
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
            data_dir = args.data_dir or os.getenv("BANREP_DATA_DIR")
            root = (
                Path(data_dir).expanduser() if data_dir else Path.home() / ".local/share/banrep-rag"
            )
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
                        _print_plan(catalog.plan(max_reports))
                        return
                    snapshot_id = catalog.sync(max_reports)
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
