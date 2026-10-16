"""Punto de entrada de los comandos del proyecto."""

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="banrep-rag", description="Consulta local de informes de política monetaria."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("diagnostico", help="Muestra las capacidades disponibles")
    args = parser.parse_args()
    if args.command == "diagnostico":
        print("Base instalada. La sincronización y las consultas aún no están disponibles.")


if __name__ == "__main__":
    main()
