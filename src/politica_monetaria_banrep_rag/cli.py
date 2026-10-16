"""Punto de entrada de los comandos del proyecto."""

import argparse
import json
import os

from politica_monetaria_banrep_rag.source import BanrepSource, SourceError


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
    args = parser.parse_args()
    if args.command == "diagnostico":
        print("Base instalada. La sincronización y las consultas aún no están disponibles.")
    elif args.command == "descubrir":
        try:
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
            reports = BanrepSource().discover(max_reports)
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
