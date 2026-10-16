from __future__ import annotations

import argparse
import re
from pathlib import Path

from app.config import load_operational_config
from app.parser import parse_report
from app.report_classifier import classify_table


SUPPORTED_EXTENSIONS = {
    ".xlsx",
    ".csv",
}


# Las claves de este diccionario ya están normalizadas con
# normalize_folder_key(): mayúsculas y solo letras/números.
ENTITY_FOLDER_ALIASES = {
    "GS": "GS",
    "GOLDMANSACHS": "GS",
    "GOLDMANSACHSCO": "GS",

    "SMBC": "SMBC",

    "MORGANSTANLEY": "MORGAN_STANLEY",

    "STANDARD": "STANDARD",

    "CITI": "CITI",
}


def normalize_folder_key(
    value: str,
) -> str:
    """
    Convierte variantes físicas del nombre de carpeta
    a una clave comparable.

    Ejemplos:
        MorganStanley
        MORGAN STANLEY
        MORGAN_STANLEY
        Morgan-Stanley

    todas producen:
        MORGANSTANLEY
    """

    return re.sub(
        r"[^A-Z0-9]+",
        "",
        str(value).strip().upper(),
    )


def resolve_entity(
    root: Path,
    file_path: Path,
) -> str:
    """
    Determina la entidad canónica a partir de la primera
    carpeta ubicada debajo de root.
    """

    try:
        relative = file_path.relative_to(root)

        if len(relative.parts) > 1:
            folder_name = relative.parts[0]
        else:
            folder_name = file_path.parent.name

    except ValueError:
        folder_name = file_path.parent.name

    folder_key = normalize_folder_key(
        folder_name
    )

    return ENTITY_FOLDER_ALIASES.get(
        folder_key,
        folder_key,
    )


def main() -> None:

    parser = argparse.ArgumentParser(
        description=(
            "Busca productos con operaciones reales "
            "en todos los reportes descargados."
        )
    )

    parser.add_argument(
        "root",
        help=(
            "Carpeta raíz donde están almacenados "
            "los reportes descargados."
        ),
    )

    args = parser.parse_args()

    root = Path(
        args.root
    ).resolve()

    if not root.exists():
        raise RuntimeError(
            f"No existe la ruta: {root}"
        )

    config = load_operational_config()

    files = sorted(
        path
        for path in root.rglob("*")
        if (
            path.is_file()
            and path.suffix.lower()
            in SUPPORTED_EXTENSIONS
            and not path.name.startswith(".cln-")
        )
    )

    print()
    print("=" * 100)
    print("INVENTARIO DE PRODUCTOS - AUTO CLOSENETTING")
    print("=" * 100)
    print()
    print(
        f"Archivos encontrados: {len(files)}"
    )

    total_operations = 0

    products_found: dict[
        tuple[str, str],
        dict[str, int],
    ] = {}

    errors: list[
        tuple[str, str, str],
    ] = []

    for index, file_path in enumerate(
        files,
        start=1,
    ):

        entity = resolve_entity(
            root,
            file_path,
        )

        print()
        print("-" * 100)

        print(
            f"[{index}/{len(files)}] "
            f"{entity} | {file_path.name}"
        )

        print(
            f"  Entidad resuelta: {entity}"
        )

        try:
            report = parse_report(
                file_path,
                config.parser,
                entity=entity,
            )

        except Exception as exc:

            print(
                "  ERROR: "
                f"{type(exc).__name__}: {exc}"
            )

            errors.append(
                (
                    entity,
                    file_path.name,
                    f"{type(exc).__name__}: {exc}",
                )
            )

            continue

        if not report.tables:

            print(
                "  Sin operaciones interpretables."
            )

            continue

        for (
            sheet_name,
            table,
        ) in report.tables.items():

            row_count = len(
                table.raw_records()
            )

            if row_count <= 0:
                continue

            total_operations += row_count

            classification = classify_table(
                entity=entity,
                table=table,
            )

            if (
                classification.recognized
                and classification.report_type
            ):
                report_type = (
                    classification.report_type
                )
                state = "RECONOCIDO"

            else:
                report_type = (
                    "NO CLASIFICADO"
                )
                state = "PENDIENTE"

            key = (
                entity,
                sheet_name,
            )

            summary = products_found.setdefault(
                key,
                {
                    "files": 0,
                    "rows": 0,
                },
            )

            summary["files"] += 1
            summary["rows"] += row_count

            print(
                f"  Hoja: {sheet_name}"
            )

            print(
                f"    Operaciones : {row_count}"
            )

            print(
                f"    Estado      : {state}"
            )

            print(
                f"    Report type : {report_type}"
            )

            if classification.issues:

                print(
                    "    Issues:"
                )

                for issue in (
                    classification.issues
                ):
                    print(
                        f"      - {issue}"
                    )

    print()
    print()
    print("=" * 100)
    print("RESUMEN DE PRODUCTOS CON OPERACIONES")
    print("=" * 100)

    if not products_found:

        print(
            "No se encontraron productos "
            "con operaciones."
        )

    else:

        for (
            entity,
            sheet_name,
        ), values in sorted(
            products_found.items()
        ):

            print(
                f"{entity:<20} "
                f"{sheet_name:<25} "
                f"archivos={values['files']:<5} "
                f"operaciones={values['rows']}"
            )

    print()

    print(
        f"Total operaciones encontradas: "
        f"{total_operations}"
    )

    print(
        f"Archivos con error: "
        f"{len(errors)}"
    )

    if errors:

        print()
        print("=" * 100)
        print("ARCHIVOS QUE NO PUDIERON LEERSE")
        print("=" * 100)

        for (
            entity,
            filename,
            error,
        ) in errors:

            print(
                f"{entity} | "
                f"{filename} | "
                f"{error}"
            )


if __name__ == "__main__":
    main()
