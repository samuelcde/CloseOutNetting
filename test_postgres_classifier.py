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

from app.postgres_db import (
    classify_saved_report,
)


def main() -> None:

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "file",
        help="Archivo original asociado al ingestion_id.",
    )

    parser.add_argument(
        "ingestion_id",
        type=int,
        help="ID existente en ingestion_files.",
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
    print("CLASIFICACIÓN POSTGRESQL")
    print("=" * 70)

    print(
        f"Archivo      : {path.name}"
    )

    print(
        f"Entidad      : {entity}"
    )

    print(
        f"Ingestion ID : "
        f"{args.ingestion_id}"
    )

    try:

        classifications = (
            classify_saved_report(
                ingestion_id=(
                    args.ingestion_id
                ),
                report=report,
                entity=entity,
            )
        )

    except Exception as exc:

        print()
        print(
            "[ERROR CLASIFICANDO]"
        )

        print(
            f"{type(exc).__name__}: "
            f"{exc}"
        )

        return

    for (
        sheet_name,
        classification,
    ) in classifications.items():

        print()
        print("-" * 70)

        print(
            f"Hoja         : "
            f"{sheet_name}"
        )

        print(
            f"Reconocida   : "
            f"{classification.recognized}"
        )

        print(
            f"Report type  : "
            f"{classification.report_type}"
        )

        print(
            f"Schema hash  : "
            f"{classification.schema_hash}"
        )

        print(
            f"Advertencias : "
            f"{len(classification.issues)}"
        )

    print()
    print(
        "[OK] Clasificación almacenada "
        "en PostgreSQL."
    )


if __name__ == "__main__":
    main()