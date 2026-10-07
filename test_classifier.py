from __future__ import annotations

import argparse

from pathlib import Path

from app.config import (
    load_operational_config,
)

from app.exceptions import (
    ReportParseError,
)

from app.parser import (
    parse_report,
)

from app.report_classifier import (
    classify_table,
)


def main() -> None:

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "file",
        help="Archivo XLSX a analizar.",
    )

    parser.add_argument(
        "--entity",
        default=None,
    )

    args = parser.parse_args()

    path = Path(
        args.file
    ).resolve()

    entity = (
        args.entity
        or path.parent.name.upper()
    )

    config = (
        load_operational_config()
    )

    try:

        report = parse_report(
            path,
            config.parser,
            entity=entity,
        )

    except ReportParseError as exc:

        print(
            f"[ERROR PARSER] {exc}"
        )

        return

    print()
    print("=" * 70)
    print("CLASIFICACIÓN DE REPORTES")
    print("=" * 70)

    print(
        f"Archivo : {path.name}"
    )

    print(
        f"Entidad : {entity}"
    )

    for (
        sheet_name,
        table,
    ) in report.tables.items():

        classification = (
            classify_table(
                entity=entity,
                table=table,
            )
        )

        print()
        print("-" * 70)

        print(
            f"Hoja           : "
            f"{sheet_name}"
        )

        print(
            f"Reconocida     : "
            f"{classification.recognized}"
        )

        print(
            f"Report type    : "
            f"{classification.report_type}"
        )

        print(
            f"Schema hash    : "
            f"{classification.schema_hash}"
        )

        print(
            f"Columnas       : "
            f"{len(classification.columns)}"
        )

        print(
            f"Advertencias   : "
            f"{len(classification.issues)}"
        )

        print()
        print(
            "Estructura detectada:"
        )

        for column in (
            classification.columns
        ):

            print(
                f"  {column['ordinal']:02d} "
                f"{column['normalized_name']:<30} "
                f"<- "
                f"{column['source_header']!r}"
            )

        if classification.issues:

            print()
            print(
                "Advertencias:"
            )

            for issue in (
                classification.issues
            ):

                print(
                    f"  - {issue}"
                )


if __name__ == "__main__":
    main()