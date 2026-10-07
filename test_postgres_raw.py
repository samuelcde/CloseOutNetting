from __future__ import annotations

import argparse

from pathlib import Path

import psycopg

from app.config import load_operational_config
from app.exceptions import ReportParseError
from app.parser import parse_report
from app.postgres_db import save_parsed_report_raw


def main() -> None:

    parser = argparse.ArgumentParser(
        description=(
            "Prueba el flujo Parser -> PostgreSQL RAW "
            "sin usar correo ni Microsoft Graph."
        )
    )

    parser.add_argument(
        "file",
        help="Archivo XLSX o CSV a procesar.",
    )

    parser.add_argument(
        "--entity",
        default=None,
        help=(
            "Entidad del reporte. "
            "Si no se indica, se toma del nombre "
            "de la carpeta contenedora."
        ),
    )

    args = parser.parse_args()

    path = Path(
        args.file
    ).resolve()

    if not path.exists():

        print()
        print(
            f"[ERROR] El archivo no existe:"
        )
        print(
            path
        )

        return

    entity = (
        args.entity
        or path.parent.name.upper()
    )

    print()
    print("=" * 70)
    print("PRUEBA PARSER -> POSTGRESQL RAW")
    print("=" * 70)

    print(
        f"Archivo : {path}"
    )

    print(
        f"Entidad : {entity}"
    )

    # ========================================================
    # CONFIGURACIÓN
    # ========================================================

    config = (
        load_operational_config()
    )

    # ========================================================
    # PARSER
    # ========================================================

    print()
    print(
        "1. Ejecutando parser..."
    )

    try:

        report = parse_report(
            path,
            config.parser,
            entity=entity,
        )

    except ReportParseError as exc:

        print()
        print(
            f"[ERROR PARSER] {exc}"
        )

        return

    print(
        "   Parser correcto."
    )

    print(
        f"   Tablas útiles : "
        f"{report.total_tables}"
    )

    print(
        f"   Filas reales  : "
        f"{report.total_rows}"
    )

    # ========================================================
    # DETALLE PREVIO A POSTGRESQL
    # ========================================================

    print()
    print(
        "2. Registros que serán enviados a PostgreSQL:"
    )

    for (
        sheet_name,
        table,
    ) in report.tables.items():

        print()
        print(
            f"   Hoja: {sheet_name}"
        )

        print(
            f"   Registros: "
            f"{table.row_count}"
        )

        print(
            f"   Filas Excel: "
            f"{table.source_row_numbers}"
        )

        for raw_record in (
            table.raw_records()
        ):

            print(
                "      -> fila Excel "
                f"{raw_record['source_row_number']}"
            )

    # ========================================================
    # POSTGRESQL
    # ========================================================

    print()
    print(
        "3. Guardando en PostgreSQL..."
    )

    try:

        (
            ingestion_id,
            raw_rows_inserted,
        ) = save_parsed_report_raw(
            report=report,
            entity=entity,
        )

    except psycopg.Error as exc:

        print()
        print(
            "[ERROR POSTGRESQL]"
        )

        print(
            exc
        )

        return

    except Exception as exc:

        print()
        print(
            "[ERROR GENERAL]"
        )

        print(
            f"{type(exc).__name__}: {exc}"
        )

        return

    # ========================================================
    # RESULTADO
    # ========================================================

    print()
    print("=" * 70)
    print("GUARDADO CORRECTAMENTE")
    print("=" * 70)

    print(
        f"ingestion_id : "
        f"{ingestion_id}"
    )

    print(
        f"filas parser : "
        f"{report.total_rows}"
    )

    print(
        f"filas RAW    : "
        f"{raw_rows_inserted}"
    )

    if (
        report.total_rows
        == raw_rows_inserted
    ):

        print()
        print(
            "[OK] La cantidad de filas del parser "
            "coincide con PostgreSQL RAW."
        )

    else:

        print()
        print(
            "[ADVERTENCIA] La cantidad de filas "
            "del parser NO coincide con las "
            "filas insertadas."
        )


if __name__ == "__main__":
    main()