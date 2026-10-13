from __future__ import annotations

import argparse

from pathlib import Path

from app.business_normalizer import (
    save_report_data,
)

from app.config import (
    load_operational_config,
)

from app.parser import (
    parse_report,
)


def main() -> None:

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "file",
    )

    parser.add_argument(
        "ingestion_id",
        type=int,
    )

    parser.add_argument(
        "--entity",
        required=True,
    )

    args = parser.parse_args()

    path = Path(
        args.file
    ).resolve()

    config = (
        load_operational_config()
    )

    report = parse_report(
        path,
        config.parser,
        entity=args.entity,
    )

    result = save_report_data(
        ingestion_id=(
            args.ingestion_id
        ),
        report=report,
        entity=args.entity,
    )

    print()
    print("=" * 70)
    print("NUEVO MODELO DE PERSISTENCIA")
    print("=" * 70)

    print(
        f"Completo             : "
        f"{result.complete}"
    )

    print(
        f"Filas guardadas      : "
        f"{result.rows_saved}"
    )

    print(
        f"Filas con warnings   : "
        f"{result.rows_with_warnings}"
    )

    print(
        f"No soportados        : "
        f"{result.unsupported_reports}"
    )

    print(
        f"No clasificados      : "
        f"{result.unclassified_sheets}"
    )


if __name__ == "__main__":
    main()