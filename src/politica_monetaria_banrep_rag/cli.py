"""Punto de entrada de los comandos del proyecto."""

import argparse
import json
import os
import sys
from contextlib import ExitStack
from pathlib import Path

from politica_monetaria_banrep_rag.catalog import Catalog, CatalogError
from politica_monetaria_banrep_rag.download import DownloadError, PdfDownloader
from politica_monetaria_banrep_rag.source import BanrepSource, SourceError


def _local_failure(exc: OSError) -> str:
    target = f" en {exc.filename}" if exc.filename else ""
    return f"fallo del sistema local{target} ({type(exc).__name__})."


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="banrep-rag", description="Consulta local de informes de política monetaria."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("diagnostico", help="Muestra las capacidades disponibles")
    discover = subcommands.add_parser(
        "descubrir", help="Verifica los informes principales oficiales"
    )
    discover.add_argument("--max-reports", help="Cantidad de informes (por defecto: 12)")
    discover.add_argument("--json", action="store_true", help="Emite metadatos en JSON")
    sync = subcommands.add_parser(
        "sincronizar", help="Descarga una ventana completa y activa el catálogo local"
    )
    sync.add_argument("--max-reports", help="Cantidad de informes (por defecto: 12)")
    sync.add_argument("--data-dir", help="Directorio del catálogo y los PDF locales")
    args = parser.parse_args()
    if args.command == "diagnostico":
        print("La sincronización del catálogo está disponible; las consultas aún no.")
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
        if args.command == "sincronizar":
            data_dir = args.data_dir or os.getenv("BANREP_DATA_DIR")
            root = (
                Path(data_dir).expanduser() if data_dir else Path.home() / ".local/share/banrep-rag"
            )
            try:
                with ExitStack() as stack:
                    source = BanrepSource()
                    stack.callback(source.client.close)
                    downloader = PdfDownloader()
                    stack.callback(downloader.client.close)
                    catalog = Catalog(
                        root, source, downloader, progress=lambda text: print(text, file=sys.stderr)
                    )
                    snapshot_id = catalog.sync(max_reports)
            except OSError as exc:
                parser.exit(1, f"Error de sincronización: {_local_failure(exc)}\n")
            except (SourceError, DownloadError, CatalogError, ValueError) as exc:
                parser.exit(1, f"Error de sincronización: {exc}\n")
            print(f"Catálogo activo: {snapshot_id}")
            return
        try:
            with ExitStack() as stack:
                source = BanrepSource()
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
