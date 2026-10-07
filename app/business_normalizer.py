from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

from psycopg.types.json import Jsonb

from .postgres_db import (
    get_connection,
    update_ingestion_status,
)

from .report_classifier import (
    normalize_name,
)


# ============================================================
# MAPEO DE CELDAS
# ============================================================


def _cells_to_dict(
    source_cells: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Convierte:

        [
            {
                "header": "FECHA PAGO",
                "value": "2026-09-24"
            }
        ]

    en:

        {
            "fecha_pago": "2026-09-24"
        }

    Los encabezados duplicados reciben sufijos.
    """

    result: dict[str, Any] = {}

    counters: dict[str, int] = {}

    for cell in source_cells:

        header = cell.get(
            "header"
        )

        name = normalize_name(
            header
        )

        if not name:
            continue

        count = counters.get(
            name,
            0,
        )

        counters[name] = (
            count + 1
        )

        if count:
            name = (
                f"{name}_{count + 1}"
            )

        result[name] = cell.get(
            "value"
        )

    return result


# ============================================================
# VALORES VACÍOS
# ============================================================


def _is_blank(
    value: Any,
) -> bool:

    if value is None:
        return True

    if isinstance(
        value,
        str,
    ):
        return not value.strip()

    return False


# ============================================================
# TEXTO
# ============================================================


def _to_text(
    value: Any,
) -> str | None:

    if _is_blank(
        value
    ):
        return None

    return str(
        value
    ).strip()


# ============================================================
# FECHAS
# ============================================================


def _to_date(
    value: Any,
    *,
    field: str,
    issues: list[dict[str, Any]],
) -> date | None:

    if _is_blank(
        value
    ):
        return None

    if isinstance(
        value,
        datetime,
    ):
        return value.date()

    if isinstance(
        value,
        date,
    ):
        return value

    text = str(
        value
    ).strip()

    try:
        return date.fromisoformat(
            text
        )

    except ValueError:
        pass

    try:
        return datetime.fromisoformat(
            text
        ).date()

    except ValueError:
        issues.append(
            {
                "source": "normalizer",
                "code": "INVALID_DATE",
                "field": field,
                "value": text,
            }
        )

        return None


# ============================================================
# HORAS
# ============================================================


def _to_time(
    value: Any,
    *,
    field: str,
    issues: list[dict[str, Any]],
) -> time | None:

    if _is_blank(
        value
    ):
        return None

    if isinstance(
        value,
        datetime,
    ):
        return (
            value.time()
            .replace(
                tzinfo=None
            )
        )

    if isinstance(
        value,
        time,
    ):
        return value.replace(
            tzinfo=None
        )

    text = str(
        value
    ).strip()

    try:

        parsed = time.fromisoformat(
            text
        )

        return parsed.replace(
            tzinfo=None
        )

    except ValueError:

        issues.append(
            {
                "source": "normalizer",
                "code": "INVALID_TIME",
                "field": field,
                "value": text,
            }
        )

        return None


# ============================================================
# NUMÉRICOS
# ============================================================


def _to_decimal(
    value: Any,
    *,
    field: str,
    issues: list[dict[str, Any]],
) -> Decimal | None:

    if _is_blank(
        value
    ):
        return None

    if isinstance(
        value,
        bool,
    ):

        issues.append(
            {
                "source": "normalizer",
                "code": "INVALID_NUMERIC",
                "field": field,
                "value": value,
            }
        )

        return None

    if isinstance(
        value,
        (
            int,
            float,
            Decimal,
        ),
    ):

        try:
            return Decimal(
                str(value)
            )

        except InvalidOperation:
            pass

    text = str(
        value
    ).strip()

    text = text.replace(
        " ",
        "",
    )

    # 1.234,56
    if (
        "." in text
        and "," in text
    ):

        if (
            text.rfind(",")
            > text.rfind(".")
        ):

            text = (
                text
                .replace(
                    ".",
                    "",
                )
                .replace(
                    ",",
                    ".",
                )
            )

        else:

            # 1,234.56
            text = text.replace(
                ",",
                "",
            )

    elif (
        "," in text
    ):

        # Si hay varias comas se interpretan
        # como separadores de miles.
        if (
            text.count(",")
            > 1
        ):

            text = text.replace(
                ",",
                "",
            )

        else:

            # Caso decimal:
            # 3157,924
            text = text.replace(
                ",",
                ".",
            )

    try:

        return Decimal(
            text
        )

    except InvalidOperation:

        issues.append(
            {
                "source": "normalizer",
                "code": "INVALID_NUMERIC",
                "field": field,
                "value": str(value),
            }
        )

        return None


# ============================================================
# WARNINGS
# ============================================================


def _merge_issues(
    existing: Any,
    normalization_issues: list[
        dict[str, Any]
    ],
) -> list[dict[str, Any]]:

    if not isinstance(
        existing,
        list,
    ):
        existing = []

    result: list[
        dict[str, Any]
    ] = []

    # Conservamos parser/classifier,
    # eliminamos normalizaciones anteriores.
    for issue in existing:

        if not isinstance(
            issue,
            dict,
        ):
            continue

        if (
            issue.get("source")
            == "normalizer"
        ):
            continue

        result.append(
            issue
        )

    result.extend(
        normalization_issues
    )

    return result


# ============================================================
# NORMALIZACIÓN SMBC FORWARD
# ============================================================


def normalize_smbc_forward(
    ingestion_id: int,
) -> tuple[int, int]:
    """
    Normaliza todas las filas SMBC/FORWARD
    correspondientes a una ingesta.

    Retorna:

        (normalized_rows, rows_with_warnings)
    """

    normalized_rows = 0
    rows_with_warnings = 0

    with get_connection() as connection:

        # ----------------------------------------------------
        # VALIDAR INGESTA
        # ----------------------------------------------------

        with connection.cursor() as cursor:

            cursor.execute(
                """
                SELECT entity
                FROM closenetting.ingestion_files
                WHERE id = %s
                """,
                (
                    ingestion_id,
                ),
            )

            ingestion = (
                cursor.fetchone()
            )

        if ingestion is None:

            raise RuntimeError(
                f"No existe ingestion_id="
                f"{ingestion_id}."
            )

        entity = str(
            ingestion[0]
        ).upper()

        if entity != "SMBC":

            raise RuntimeError(
                f"El ingestion_id="
                f"{ingestion_id} pertenece "
                f"a '{entity}', no a SMBC."
            )

        # ----------------------------------------------------
        # OBTENER RAW
        # ----------------------------------------------------

        with connection.cursor() as cursor:

            cursor.execute(
                """
                SELECT
                    id,
                    source_cells,
                    validation_issues
                FROM closenetting.raw_report_rows
                WHERE ingestion_id = %s
                  AND report_type = 'FORWARD'
                  AND report_schema_id IS NOT NULL
                ORDER BY source_row_number
                """,
                (
                    ingestion_id,
                ),
            )

            raw_rows = (
                cursor.fetchall()
            )

        if not raw_rows:

            raise RuntimeError(
                f"No existen registros "
                f"SMBC/FORWARD clasificados "
                f"para ingestion_id="
                f"{ingestion_id}."
            )

        # ----------------------------------------------------
        # NORMALIZAR FILAS
        # ----------------------------------------------------

        for (
            raw_row_id,
            source_cells,
            existing_issues,
        ) in raw_rows:

            fields = (
                _cells_to_dict(
                    source_cells
                )
            )

            issues: list[
                dict[str, Any]
            ] = []

            values = {
                "mercado": _to_text(
                    fields.get(
                        "mercado"
                    )
                ),

                "hora_negociacion": _to_time(
                    fields.get(
                        "hora_negociacion"
                    ),
                    field="hora_negociacion",
                    issues=issues,
                ),

                "fecha_negociacion": _to_date(
                    fields.get(
                        "fecha_negociacion"
                    ),
                    field="fecha_negociacion",
                    issues=issues,
                ),

                "comprador_compania": _to_text(
                    fields.get(
                        "comprador_compania"
                    )
                ),

                "vendedor_compania": _to_text(
                    fields.get(
                        "vendedor_compania"
                    )
                ),

                "nombre_trader_comp": _to_text(
                    fields.get(
                        "nombre_trader_comp"
                    )
                ),

                "nombre_trader_vend": _to_text(
                    fields.get(
                        "nombre_trader_vend"
                    )
                ),

                "par_moneda": _to_text(
                    fields.get(
                        "par_moneda"
                    )
                ),

                "monto": _to_decimal(
                    fields.get(
                        "monto"
                    ),
                    field="monto",
                    issues=issues,
                ),

                "precio_spot": _to_decimal(
                    fields.get(
                        "precio_spot"
                    ),
                    field="precio_spot",
                    issues=issues,
                ),

                "precio_final": _to_decimal(
                    fields.get(
                        "precio_final"
                    ),
                    field="precio_final",
                    issues=issues,
                ),

                "plazo": _to_text(
                    fields.get(
                        "plazo"
                    )
                ),

                "fecha_asignacion_tasa": _to_date(
                    fields.get(
                        "fecha_asignacion_tasa"
                    ),
                    field="fecha_asignacion_tasa",
                    issues=issues,
                ),

                "fecha_pago": _to_date(
                    fields.get(
                        "fecha_pago"
                    ),
                    field="fecha_pago",
                    issues=issues,
                ),

                "source_id": _to_text(
                    fields.get(
                        "id"
                    )
                ),
            }

            # ------------------------------------------------
            # INSERT / UPSERT NEGOCIO
            # ------------------------------------------------

            with connection.cursor() as cursor:

                cursor.execute(
                    """
                    INSERT INTO closenetting.smbc_forward (
                        raw_row_id,
                        mercado,
                        hora_negociacion,
                        fecha_negociacion,
                        comprador_compania,
                        vendedor_compania,
                        nombre_trader_comp,
                        nombre_trader_vend,
                        par_moneda,
                        monto,
                        precio_spot,
                        precio_final,
                        plazo,
                        fecha_asignacion_tasa,
                        fecha_pago,
                        source_id
                    )
                    VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s
                    )
                    ON CONFLICT (raw_row_id)
                    DO UPDATE SET
                        mercado = EXCLUDED.mercado,
                        hora_negociacion =
                            EXCLUDED.hora_negociacion,
                        fecha_negociacion =
                            EXCLUDED.fecha_negociacion,
                        comprador_compania =
                            EXCLUDED.comprador_compania,
                        vendedor_compania =
                            EXCLUDED.vendedor_compania,
                        nombre_trader_comp =
                            EXCLUDED.nombre_trader_comp,
                        nombre_trader_vend =
                            EXCLUDED.nombre_trader_vend,
                        par_moneda =
                            EXCLUDED.par_moneda,
                        monto =
                            EXCLUDED.monto,
                        precio_spot =
                            EXCLUDED.precio_spot,
                        precio_final =
                            EXCLUDED.precio_final,
                        plazo =
                            EXCLUDED.plazo,
                        fecha_asignacion_tasa =
                            EXCLUDED.fecha_asignacion_tasa,
                        fecha_pago =
                            EXCLUDED.fecha_pago,
                        source_id =
                            EXCLUDED.source_id,
                        updated_at =
                            CURRENT_TIMESTAMP
                    """,
                    (
                        raw_row_id,
                        values[
                            "mercado"
                        ],
                        values[
                            "hora_negociacion"
                        ],
                        values[
                            "fecha_negociacion"
                        ],
                        values[
                            "comprador_compania"
                        ],
                        values[
                            "vendedor_compania"
                        ],
                        values[
                            "nombre_trader_comp"
                        ],
                        values[
                            "nombre_trader_vend"
                        ],
                        values[
                            "par_moneda"
                        ],
                        values[
                            "monto"
                        ],
                        values[
                            "precio_spot"
                        ],
                        values[
                            "precio_final"
                        ],
                        values[
                            "plazo"
                        ],
                        values[
                            "fecha_asignacion_tasa"
                        ],
                        values[
                            "fecha_pago"
                        ],
                        values[
                            "source_id"
                        ],
                    ),
                )

            merged_issues = (
                _merge_issues(
                    existing_issues,
                    issues,
                )
            )

            raw_status = (
                "WARNING"
                if merged_issues
                else "NORMALIZED"
            )

            with connection.cursor() as cursor:

                cursor.execute(
                    """
                    UPDATE closenetting.raw_report_rows
                    SET
                        status = %s,
                        validation_issues = %s,
                        updated_at =
                            CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (
                        raw_status,
                        Jsonb(
                            merged_issues
                        ),
                        raw_row_id,
                    ),
                )

            normalized_rows += 1

            if merged_issues:
                rows_with_warnings += 1

        # ----------------------------------------------------
        # ESTADO GLOBAL DE LA INGESTA
        # ----------------------------------------------------

        with connection.cursor() as cursor:

            cursor.execute(
                """
                SELECT
                    COUNT(*) FILTER (
                        WHERE status = 'UNCLASSIFIED'
                    ),
                    COUNT(*) FILTER (
                        WHERE status = 'PENDING'
                    ),
                    COUNT(*) FILTER (
                        WHERE status = 'WARNING'
                    )
                FROM closenetting.raw_report_rows
                WHERE ingestion_id = %s
                """,
                (
                    ingestion_id,
                ),
            )

            (
                unclassified_count,
                pending_count,
                warning_count,
            ) = cursor.fetchone()

        if (
            unclassified_count
            or pending_count
        ):

            ingestion_status = (
                "normalization_pending"
            )

        elif warning_count:

            ingestion_status = (
                "normalized_warning"
            )

        else:

            ingestion_status = (
                "normalized"
            )

        update_ingestion_status(
            connection,
            ingestion_id=ingestion_id,
            status=ingestion_status,
        )

    return (
        normalized_rows,
        rows_with_warnings,
    )