from __future__ import annotations

import argparse

from pathlib import Path

from app.config import load_operational_config
from app.exceptions import ReportParseError
from app.parser import parse_report
from openpyxl import load_workbook


def main() -> None:

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "file",
        help="Archivo XLSX/XLS/CSV a analizar.",
    )

    parser.add_argument(
        "--entity",
        default=None,
        help=(
            "Nombre de la entidad (opcional). "
            "Si no se pasa, se infiere de la carpeta."
        ),
    )

    args = parser.parse_args()

    path = Path(
        args.file
    )

    # Si no especificas la entidad,
    # toma el nombre de la carpeta contenedora.
    entity_name = (
        args.entity
        or path.parent.name.upper()
    )

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

    # ========================================================
    # RESUMEN GENERAL
    # ========================================================

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

    # ========================================================
    # TODAS LAS HOJAS DEL ARCHIVO
    # ========================================================

    if report.extension == ".xlsx":

        print()
        print("=" * 70)
        print(
            "HOJAS ENCONTRADAS EN EL EXCEL"
        )
        print("=" * 70)

        workbook = load_workbook(
            filename=path,
            read_only=True,
            data_only=True,
        )

        try:
            for sheet_name in (
                workbook.sheetnames
            ):

                parsed_table = (
                    report.tables.get(
                        sheet_name
                    )
                )

                if parsed_table is None:

                    print(
                        f"{sheet_name:<20} "
                        "→ sin registros / omitida"
                    )

                    continue

                print(
                    f"{sheet_name:<20} "
                    f"→ "
                    f"{parsed_table.row_count} "
                    "registro(s)"
                )

        finally:
            workbook.close()

    # ========================================================
    # DETALLE DE TABLAS PROCESADAS
    # ========================================================

    for (
        table_name,
        parsed_table,
    ) in report.tables.items():

        print()
        print("=" * 70)

        print(
            f"Tabla/Hoja: {table_name}"
        )

        print(
            f"Dimensión: "
            f"{parsed_table.row_count} filas x "
            f"{parsed_table.column_count} columnas"
        )

        print(
            f"Fila de encabezado: "
            f"{parsed_table.source_header_row_number}"
        )

        print(
            f"Filas físicas de datos: "
            f"{parsed_table.source_row_numbers}"
        )

        print(
            f"Filas estructurales: "
            f"{len(parsed_table.structural_rows)}"
        )

        print(
            f"Advertencias: "
            f"{parsed_table.warnings}"
        )

        # ----------------------------------------------------
        # Filas estructurales detectadas
        # ----------------------------------------------------

        if parsed_table.structural_rows:

            print()
            print(
                "Filas estructurales detectadas:"
            )

            for structural_row in (
                parsed_table.structural_rows
            ):

                print(
                    "  - "
                    f"{structural_row}"
                )

        # ----------------------------------------------------
        # DataFrame
        # ----------------------------------------------------

        print()
        print(
            "Datos interpretados:"
        )
        print()

        print(
            parsed_table.dataframe.head(
                10
            ).to_string()
        )

        # ----------------------------------------------------
        # Columnas
        # ----------------------------------------------------

        print()
        print(
            "Columnas:"
        )

        for column in (
            parsed_table.dataframe.columns
        ):

            print(
                f"  - {column}"
            )

        # ----------------------------------------------------
        # Registros RAW
        # ----------------------------------------------------

        print()
        print(
            "Registros RAW:"
        )

        for raw_record in (
            parsed_table.raw_records()
        ):

            print(
                f"  - Fila Excel "
                f"{raw_record['source_row_number']}"
            )

            if raw_record["warnings"]:

                print(
                    "    Advertencias: "
                    f"{raw_record['warnings']}"
                )


if __name__ == "__main__":
    main()