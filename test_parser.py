from __future__ import annotations

import argparse

from pathlib import Path

from app.config import load_operational_config
from app.exceptions import ReportParseError
from app.parser import parse_report


def main() -> None:

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "file",
        help="Archivo XLSX/XLS/CSV a analizar.",
    )
    parser.add_argument(
        "--entity",
        default=None,
        help="Nombre de la entidad (opcional). Si no se pasa, se infiere de la carpeta.",
    )

    args = parser.parse_args()

    path = Path(
        args.file
    )

    # Si no especificas la entidad, toma el nombre de la carpeta contenedora (ej. GS, MORGAN_STANLEY)
    entity_name = args.entity or path.parent.name.upper()

    config = load_operational_config()

    try:
        report = parse_report(
            path,
            config.parser,
            entity=entity_name,
        )

    except ReportParseError as exc:
        print()
        print(
            f"[ERROR PARSER] {exc}"
        )
        return

    print()
    print("=" * 70)
    print(
        f"Archivo : {report.source}"
    )
    print(
        f"Formato : {report.extension}"
    )
    print(
        f"Tablas  : {report.total_tables}"
    )
    print(
        f"Filas   : {report.total_rows}"
    )
    print("=" * 70)

    for table_name, table in (
        report.tables.items()
    ):

        print()
        print(
            f"Tabla/Hoja: {table_name}"
        )

        print(
            f"Dimensión: "
            f"{table.row_count} filas x "
            f"{table.column_count} columnas"
        )

        print()

        print(
            table.dataframe.head(
                10
            ).to_string()
        )

        print()
        print(
            "Columnas:"
        )

        for column in (
            table.dataframe.columns
        ):
            print(
                f"  - {column}"
            )


if __name__ == "__main__":
    main()