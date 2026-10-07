from __future__ import annotations

import hashlib
import math
import os

from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import psycopg

from dotenv import load_dotenv
from psycopg import Connection
from psycopg.types.json import Jsonb

from .parser import ParsedReport

from .report_classifier import (
    ReportClassification,
    classify_table,
)


load_dotenv()


# ============================================================
# CONFIGURACIÓN
# ============================================================


def _required_env(name: str) -> str:
    value = os.getenv(name)

    if value is None or not value.strip():
        raise RuntimeError(
            f"Falta configurar la variable "
            f"de entorno {name}."
        )

    return value.strip()


def get_connection() -> Connection:
    """
    Abre una conexión nueva a PostgreSQL.

    Las credenciales se obtienen únicamente desde .env.
    """

    return psycopg.connect(
        host=_required_env(
            "DATABASE_HOST"
        ),
        port=int(
            os.getenv(
                "DATABASE_PORT",
                "5432",
            )
        ),
        dbname=_required_env(
            "DATABASE_NAME"
        ),
        user=_required_env(
            "DATABASE_USER"
        ),
        password=_required_env(
            "DATABASE_PASSWORD"
        ),
        connect_timeout=10,
        application_name=(
            "auto-closeoutnetting"
        ),
    )


# ============================================================
# HASH FÍSICO DEL ARCHIVO
# ============================================================


def calculate_file_sha256(
    path: Path,
) -> str:
    digest = hashlib.sha256()

    with path.open(
        "rb"
    ) as file_handle:

        while True:
            block = file_handle.read(
                1024 * 1024
            )

            if not block:
                break

            digest.update(
                block
            )

    return digest.hexdigest()


# ============================================================
# NORMALIZACIÓN JSON
# ============================================================


def _json_safe(
    value: Any,
) -> Any:
    """
    Convierte valores Python/Pandas/OpenPyXL
    a representaciones compatibles con JSONB.

    Importante:
    esto NO pretende interpretar financieramente
    el valor. Solo permitir conservarlo en PostgreSQL.
    """

    if value is None:
        return None

    try:
        if pd.isna(value):
            return None
    except (
        TypeError,
        ValueError,
    ):
        pass

    if hasattr(
        value,
        "item",
    ) and not isinstance(
        value,
        (
            str,
            bytes,
            bytearray,
        ),
    ):
        try:
            value = value.item()
        except (
            TypeError,
            ValueError,
        ):
            pass

    if isinstance(
        value,
        datetime,
    ):
        return value.isoformat()

    if isinstance(
        value,
        date,
    ):
        return value.isoformat()

    if isinstance(
        value,
        time,
    ):
        return value.isoformat()

    if isinstance(
        value,
        Decimal,
    ):
        return str(value)

    if isinstance(
        value,
        float,
    ):
        if not math.isfinite(
            value
        ):
            return str(value)

        return value

    if isinstance(
        value,
        (
            str,
            int,
            bool,
        ),
    ):
        return value

    if isinstance(
        value,
        bytes,
    ):
        return value.hex()

    if isinstance(
        value,
        dict,
    ):
        return {
            str(key): _json_safe(
                item
            )
            for key, item
            in value.items()
        }

    if isinstance(
        value,
        (
            list,
            tuple,
            set,
        ),
    ):
        return [
            _json_safe(
                item
            )
            for item in value
        ]

    return str(
        value
    )


# ============================================================
# INGESTION_FILES
# ============================================================


def create_ingestion(
    connection: Connection,
    *,
    entity: str,
    source_filename: str,
    source_path: str | None,
    message_id: str | None = None,
    attachment_id: str | None = None,
    file_sha256: str | None = None,
    content_sha256: str | None = None,
    size_bytes: int | None = None,
    status: str = "raw_pending",
) -> int:
    """
    Registra el archivo recibido y devuelve ingestion_id.
    """

    with connection.cursor() as cursor:

        cursor.execute(
            """
            INSERT INTO closenetting.ingestion_files (
                entity,
                source_filename,
                source_path,
                message_id,
                attachment_id,
                file_sha256,
                content_sha256,
                size_bytes,
                status
            )
            VALUES (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s
            )
            RETURNING id
            """,
            (
                entity,
                source_filename,
                source_path,
                message_id,
                attachment_id,
                file_sha256,
                content_sha256,
                size_bytes,
                status,
            ),
        )

        row = cursor.fetchone()

        if row is None:
            raise RuntimeError(
                "PostgreSQL no devolvió "
                "el ingestion_id."
            )

        return int(
            row[0]
        )


def update_ingestion_status(
    connection: Connection,
    *,
    ingestion_id: int,
    status: str,
    content_sha256: str | None = None,
) -> None:

    with connection.cursor() as cursor:

        if content_sha256 is None:

            cursor.execute(
                """
                UPDATE closenetting.ingestion_files
                SET
                    status = %s,
                    processed_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (
                    status,
                    ingestion_id,
                ),
            )

        else:

            cursor.execute(
                """
                UPDATE closenetting.ingestion_files
                SET
                    status = %s,
                    content_sha256 = %s,
                    processed_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (
                    status,
                    content_sha256,
                    ingestion_id,
                ),
            )


# ============================================================
# RAW_REPORT_ROWS
# ============================================================


def insert_raw_row(
    connection: Connection,
    *,
    ingestion_id: int,
    source_sheet_name: str,
    source_row_number: int,
    source_cells: list[dict[str, Any]],
    report_schema_id: int | None = None,
    report_type: str | None = None,
    status: str = "PENDING",
    validation_issues: list[
        dict[str, Any]
    ] | None = None,
) -> int:
    """
    Inserta una fila legible del reporte.

    No interpreta aún el producto financiero.
    """

    safe_cells = _json_safe(
        source_cells
    )

    safe_issues = _json_safe(
        validation_issues or []
    )

    with connection.cursor() as cursor:

        cursor.execute(
            """
            INSERT INTO closenetting.raw_report_rows (
                ingestion_id,
                report_schema_id,
                report_type,
                source_sheet_name,
                source_row_number,
                source_cells,
                status,
                validation_issues
            )
            VALUES (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s
            )
            RETURNING id
            """,
            (
                ingestion_id,
                report_schema_id,
                report_type,
                source_sheet_name,
                source_row_number,
                Jsonb(
                    safe_cells
                ),
                status,
                Jsonb(
                    safe_issues
                ),
            ),
        )

        row = cursor.fetchone()

        if row is None:
            raise RuntimeError(
                "PostgreSQL no devolvió "
                "el raw_report_row id."
            )

        return int(
            row[0]
        )


# ============================================================
# REPORT_SCHEMAS
# ============================================================


def upsert_report_schema(
    connection: Connection,
    *,
    classification: ReportClassification,
) -> int:
    """
    Registra una estructura conocida.

    Si ya existe:
        entity + report_type + schema_hash

    no crea otra fila y solamente actualiza last_seen_at.

    Retorna report_schema_id.
    """

    if (
        not classification.recognized
        or classification.report_type is None
    ):
        raise ValueError(
            "No se puede registrar un esquema "
            "de un reporte no clasificado."
        )

    columns_json = _json_safe(
        list(
            classification.columns
        )
    )

    with connection.cursor() as cursor:

        cursor.execute(
            """
            INSERT INTO closenetting.report_schemas (
                entity,
                report_type,
                schema_hash,
                columns_json
            )
            VALUES (
                %s,
                %s,
                %s,
                %s
            )
            ON CONFLICT (
                entity,
                report_type,
                schema_hash
            )
            DO UPDATE SET
                last_seen_at = CURRENT_TIMESTAMP
            RETURNING id
            """,
            (
                classification.entity,
                classification.report_type,
                classification.schema_hash,
                Jsonb(
                    columns_json
                ),
            ),
        )

        row = cursor.fetchone()

        if row is None:
            raise RuntimeError(
                "PostgreSQL no devolvió "
                "report_schema_id."
            )

        return int(
            row[0]
        )

def _merge_validation_issues(
    existing: Any,
    classification_issues: tuple[
        dict[str, Any],
        ...,
    ],
) -> list[dict[str, Any]]:
    """
    Conserva advertencias anteriores del parser y sustituye
    únicamente las advertencias generadas previamente por
    el clasificador.

    Esto permite ejecutar la clasificación varias veces
    sin duplicar validation_issues.
    """

    if not isinstance(
        existing,
        list,
    ):
        existing = []

    result: list[
        dict[str, Any]
    ] = []

    for issue in existing:

        if not isinstance(
            issue,
            dict,
        ):
            continue

        if (
            issue.get("source")
            == "classifier"
        ):
            continue

        result.append(
            issue
        )

    for issue in (
        classification_issues
    ):

        safe_issue = dict(
            issue
        )

        safe_issue[
            "source"
        ] = "classifier"

        result.append(
            safe_issue
        )

    return result

def validate_ingestion_source(
    connection: Connection,
    *,
    ingestion_id: int,
    report: ParsedReport,
    entity: str,
) -> None:
    """
    Impide clasificar una ingesta utilizando un archivo
    diferente al que originó esa ingesta.
    """

    source_path = Path(
        report.source
    )

    current_sha256 = (
        calculate_file_sha256(
            source_path
        )
    )

    entity_key = (
        str(entity)
        .strip()
        .upper()
    )

    with connection.cursor() as cursor:

        cursor.execute(
            """
            SELECT
                entity,
                source_filename,
                file_sha256
            FROM closenetting.ingestion_files
            WHERE id = %s
            """,
            (
                ingestion_id,
            ),
        )

        row = cursor.fetchone()

    if row is None:
        raise RuntimeError(
            f"No existe ingestion_id="
            f"{ingestion_id}."
        )

    (
        stored_entity,
        stored_filename,
        stored_sha256,
    ) = row

    if (
        str(stored_entity).upper()
        != entity_key
    ):
        raise RuntimeError(
            f"El ingestion_id={ingestion_id} "
            f"pertenece a la entidad "
            f"'{stored_entity}', no a "
            f"'{entity_key}'."
        )

    if (
        stored_sha256
        and stored_sha256
        != current_sha256
    ):
        raise RuntimeError(
            f"El ingestion_id={ingestion_id} "
            "pertenece a otro archivo. "
            f"Registrado='{stored_filename}', "
            f"actual='{source_path.name}'."
        )

def classify_saved_report(
    *,
    ingestion_id: int,
    report: ParsedReport,
    entity: str,
) -> dict[str, ReportClassification]:
    """
    Clasifica las hojas de un reporte RAW ya almacenado.

    Esta operación ocurre en una transacción distinta
    al guardado RAW.

    Resultado:

        reconocido sin advertencias:
            report_type asignado
            report_schema_id asignado
            status sigue PENDING

        reconocido con diferencias:
            report_type asignado
            report_schema_id asignado
            status WARNING

        no reconocido:
            report_type NULL
            report_schema_id NULL
            status UNCLASSIFIED
    """

    classifications: dict[
        str,
        ReportClassification,
    ] = {}

    entity_key = (
        str(entity)
        .strip()
        .upper()
    )

    with get_connection() as connection:

        validate_ingestion_source(
            connection,
            ingestion_id=ingestion_id,
            report=report,
            entity=entity_key,
        )

        for (
            sheet_name,
            table,
        ) in report.tables.items():

            classification = (
                classify_table(
                    entity=entity_key,
                    table=table,
                )
            )

            classifications[
                sheet_name
            ] = classification

            # =================================================
            # REPORTE RECONOCIDO
            # =================================================

            if (
                classification.recognized
                and classification.report_type
            ):

                report_schema_id = (
                    upsert_report_schema(
                        connection,
                        classification=(
                            classification
                        ),
                    )
                )

                row_status = (
                    "WARNING"
                    if classification.issues
                    else "PENDING"
                )

            # =================================================
            # REPORTE NO RECONOCIDO
            # =================================================

            else:

                report_schema_id = None

                row_status = (
                    "UNCLASSIFIED"
                )

            # =================================================
            # ACTUALIZAR LAS FILAS RAW DE ESA HOJA
            # =================================================

            with connection.cursor() as cursor:

                cursor.execute(
                    """
                    SELECT
                        id,
                        validation_issues
                    FROM closenetting.raw_report_rows
                    WHERE ingestion_id = %s
                      AND source_sheet_name = %s
                    ORDER BY source_row_number
                    """,
                    (
                        ingestion_id,
                        sheet_name,
                    ),
                )

                raw_rows = (
                    cursor.fetchall()
                )

            if not raw_rows:
                raise RuntimeError(
                    f"No existen filas RAW para "
                    f"ingestion_id={ingestion_id}, "
                    f"hoja='{sheet_name}'."
                )

            for (
                raw_row_id,
                existing_issues,
            ) in raw_rows:

                merged_issues = (
                    _merge_validation_issues(
                        existing_issues,
                        classification.issues,
                    )
                )

                with connection.cursor() as cursor:

                    cursor.execute(
                        """
                        UPDATE closenetting.raw_report_rows
                        SET
                            report_schema_id = %s,
                            report_type = %s,
                            status = %s,
                            validation_issues = %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                        """,
                        (
                            report_schema_id,
                            classification.report_type,
                            row_status,
                            Jsonb(
                                _json_safe(
                                    merged_issues
                                )
                            ),
                            raw_row_id,
                        ),
                    )

        # =====================================================
        # ESTADO GENERAL DE LA INGESTA
        # =====================================================

        has_unclassified = any(
            not classification.recognized
            for classification
            in classifications.values()
        )

        has_warnings = any(
            classification.issues
            for classification
            in classifications.values()
        )

        if has_unclassified:

            ingestion_status = (
                "classification_pending"
            )

        elif has_warnings:

            ingestion_status = (
                "classified_warning"
            )

        else:

            ingestion_status = (
                "classified"
            )

        update_ingestion_status(
            connection,
            ingestion_id=ingestion_id,
            status=ingestion_status,
        )

    return classifications



# ============================================================
# GUARDADO RAW COMPLETO
# ============================================================


def save_parsed_report_raw(
    *,
    report: ParsedReport,
    entity: str,
    message_id: str | None = None,
    attachment_id: str | None = None,
    file_sha256: str | None = None,
    content_sha256: str | None = None,
) -> tuple[int, int]:
    """
    Guarda en una única transacción:

        ingestion_files
        +
        raw_report_rows

    Si falla cualquier INSERT, PostgreSQL hace rollback
    y no queda una carga parcial.

    Retorna:

        (ingestion_id, raw_rows_inserted)
    """

    source_path = Path(
        report.source
    )

    if file_sha256 is None:
        file_sha256 = (
            calculate_file_sha256(
                source_path
            )
        )

    size_bytes = (
        source_path.stat().st_size
    )

    raw_rows_inserted = 0

    with get_connection() as connection:

        ingestion_id = create_ingestion(
            connection,
            entity=entity,
            source_filename=(
                source_path.name
            ),
            source_path=str(
                source_path
            ),
            message_id=message_id,
            attachment_id=attachment_id,
            file_sha256=file_sha256,
            content_sha256=(
                content_sha256
            ),
            size_bytes=size_bytes,
            status="raw_pending",
        )

        for (
            sheet_name,
            table,
        ) in report.tables.items():

            for raw_record in (
                table.raw_records()
            ):

                warning_codes = (
                    raw_record.get(
                        "warnings",
                        [],
                    )
                )

                issues = [
                    {
                        "code": warning_code,
                        "source": "parser",
                    }
                    for warning_code
                    in warning_codes
                ]

                status = (
                    "WARNING"
                    if issues
                    else "PENDING"
                )

                insert_raw_row(
                    connection,
                    ingestion_id=(
                        ingestion_id
                    ),
                    report_schema_id=None,
                    report_type=None,
                    source_sheet_name=(
                        sheet_name
                    ),
                    source_row_number=int(
                        raw_record[
                            "source_row_number"
                        ]
                    ),
                    source_cells=(
                        raw_record[
                            "source_cells"
                        ]
                    ),
                    status=status,
                    validation_issues=(
                        issues
                    ),
                )

                raw_rows_inserted += 1

        update_ingestion_status(
            connection,
            ingestion_id=(
                ingestion_id
            ),
            status="raw_stored",
            content_sha256=(
                content_sha256
            ),
        )

        # El context manager de psycopg hace COMMIT
        # automáticamente si todo termina bien.
        # Si ocurre una excepción, hace ROLLBACK.

    return (
        ingestion_id,
        raw_rows_inserted,
    )