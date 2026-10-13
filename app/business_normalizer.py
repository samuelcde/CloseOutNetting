from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from .parser import ParsedReport, ParsedTable
from .postgres_db import get_connection, json_safe, update_ingestion_status
from .report_classifier import classify_table, normalize_name


# ============================================================
# RESULTADO DE PERSISTENCIA
# ============================================================


@dataclass(frozen=True, slots=True)
class ReportPersistenceResult:
    """Resultado global del guardado de un archivo en PostgreSQL."""

    complete: bool
    rows_saved: int
    rows_with_warnings: int
    unsupported_reports: tuple[str, ...] = ()
    unclassified_sheets: tuple[str, ...] = ()


# ============================================================
# MAPEO DE CELDAS
# ============================================================


def _cells_to_dict(
    source_cells: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Convierte las celdas RAW ordenadas del parser en un diccionario
    accesible por nombre normalizado de columna.

    Los encabezados duplicados reciben sufijos _2, _3, etc.
    """

    result: dict[str, Any] = {}
    counters: dict[str, int] = {}

    for cell in source_cells:
        header = cell.get("header")
        name = normalize_name(header)

        if not name:
            continue

        count = counters.get(name, 0)
        counters[name] = count + 1

        if count:
            name = f"{name}_{count + 1}"

        result[name] = cell.get("value")

    return result


# ============================================================
# CONVERSIÓN DE TIPOS
# ============================================================


def _is_blank(value: Any) -> bool:
    if value is None:
        return True

    if isinstance(value, str):
        return not value.strip()

    return False


def _to_text(value: Any) -> str | None:
    if _is_blank(value):
        return None

    return str(value).strip()


def _to_date(
    value: Any,
    *,
    field: str,
    issues: list[dict[str, Any]],
) -> date | None:
    if _is_blank(value):
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    text = str(value).strip()

    try:
        return date.fromisoformat(text)
    except ValueError:
        pass

    try:
        return datetime.fromisoformat(text).date()
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


def _to_time(
    value: Any,
    *,
    field: str,
    issues: list[dict[str, Any]],
) -> time | None:
    if _is_blank(value):
        return None

    if isinstance(value, datetime):
        return value.time().replace(tzinfo=None)

    if isinstance(value, time):
        return value.replace(tzinfo=None)

    text = str(value).strip()

    try:
        parsed = time.fromisoformat(text)
        return parsed.replace(tzinfo=None)
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


def _to_decimal(
    value: Any,
    *,
    field: str,
    issues: list[dict[str, Any]],
) -> Decimal | None:
    if _is_blank(value):
        return None

    if isinstance(value, bool):
        issues.append(
            {
                "source": "normalizer",
                "code": "INVALID_NUMERIC",
                "field": field,
                "value": value,
            }
        )
        return None

    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            pass

    text = str(value).strip().replace(" ", "")

    # 1.234,56 / 1,234.56
    if "." in text and "," in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")

    elif "," in text:
        if text.count(",") > 1:
            text = text.replace(",", "")
        else:
            text = text.replace(",", ".")

    try:
        return Decimal(text)
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
# ADVERTENCIAS
# ============================================================


def _parser_issues(
    raw_record: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "source": "parser",
            "code": str(code),
        }
        for code in raw_record.get("warnings", [])
    ]


def _classifier_issues(
    issues: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    for issue in issues:
        safe_issue = dict(issue)
        safe_issue["source"] = "classifier"
        result.append(safe_issue)

    return result


# ============================================================
# VALIDACIÓN DE INGESTA
# ============================================================


def _validate_ingestion(
    connection: Connection,
    *,
    ingestion_id: int,
    entity: str,
) -> None:
    """Evita guardar operaciones en una ingesta de otra entidad."""

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT entity
            FROM closenetting.ingestion_files
            WHERE id = %s
            """,
            (ingestion_id,),
        )
        row = cursor.fetchone()

    if row is None:
        raise RuntimeError(f"No existe ingestion_id={ingestion_id}.")

    stored_entity = str(row[0]).strip().upper()
    entity_key = str(entity).strip().upper()

    if stored_entity != entity_key:
        raise RuntimeError(
            f"El ingestion_id={ingestion_id} pertenece a la entidad "
            f"'{stored_entity}', no a '{entity_key}'."
        )


# ============================================================
# SMBC / FORWARD
# ============================================================


def _save_smbc_forward(
    connection: Connection,
    *,
    ingestion_id: int,
    table: ParsedTable,
    classification_issues: tuple[dict[str, Any], ...],
) -> tuple[int, int]:
    """
    Guarda SMBC/FORWARD directamente en raw_report_smbc_forward.

    La misma fila contiene:
      - columnas organizadas/tipadas;
      - original_message con el registro original en JSONB;
      - validation_issues con advertencias de parser, clasificador
        o conversión de tipos.
    """

    rows_saved = 0
    rows_with_warnings = 0

    for raw_record in table.raw_records():
        source_cells = raw_record["source_cells"]
        fields = _cells_to_dict(source_cells)

        issues = (
            _parser_issues(raw_record)
            + _classifier_issues(classification_issues)
        )

        values = {
            "mercado": _to_text(fields.get("mercado")),
            "hora_negociacion": _to_time(
                fields.get("hora_negociacion"),
                field="hora_negociacion",
                issues=issues,
            ),
            "fecha_negociacion": _to_date(
                fields.get("fecha_negociacion"),
                field="fecha_negociacion",
                issues=issues,
            ),
            "comprador_compania": _to_text(
                fields.get("comprador_compania")
            ),
            "vendedor_compania": _to_text(
                fields.get("vendedor_compania")
            ),
            "nombre_trader_comp": _to_text(
                fields.get("nombre_trader_comp")
            ),
            "nombre_trader_vend": _to_text(
                fields.get("nombre_trader_vend")
            ),
            "par_moneda": _to_text(fields.get("par_moneda")),
            "monto": _to_decimal(
                fields.get("monto"),
                field="monto",
                issues=issues,
            ),
            "precio_spot": _to_decimal(
                fields.get("precio_spot"),
                field="precio_spot",
                issues=issues,
            ),
            "precio_final": _to_decimal(
                fields.get("precio_final"),
                field="precio_final",
                issues=issues,
            ),
            "plazo": _to_text(fields.get("plazo")),
            "fecha_asignacion_tasa": _to_date(
                fields.get("fecha_asignacion_tasa"),
                field="fecha_asignacion_tasa",
                issues=issues,
            ),
            "fecha_pago": _to_date(
                fields.get("fecha_pago"),
                field="fecha_pago",
                issues=issues,
            ),
            "source_id": _to_text(fields.get("id")),
        }

        original_message = json_safe(source_cells)
        safe_issues = json_safe(issues)

        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO closenetting.raw_report_smbc_forward (
                    ingestion_id,
                    source_sheet_name,
                    source_row_number,
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
                    source_id,
                    original_message,
                    validation_issues
                )
                VALUES (
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s,
                    %s, %s,
                    %s,
                    %s, %s, %s,
                    %s,
                    %s, %s,
                    %s,
                    %s, %s
                )

                ON CONFLICT (
                    ingestion_id,
                    source_sheet_name,
                    source_row_number
                )
                DO UPDATE SET
                    mercado = EXCLUDED.mercado,
                    hora_negociacion = EXCLUDED.hora_negociacion,
                    fecha_negociacion = EXCLUDED.fecha_negociacion,
                    comprador_compania = EXCLUDED.comprador_compania,
                    vendedor_compania = EXCLUDED.vendedor_compania,
                    nombre_trader_comp = EXCLUDED.nombre_trader_comp,
                    nombre_trader_vend = EXCLUDED.nombre_trader_vend,
                    par_moneda = EXCLUDED.par_moneda,
                    monto = EXCLUDED.monto,
                    precio_spot = EXCLUDED.precio_spot,
                    precio_final = EXCLUDED.precio_final,
                    plazo = EXCLUDED.plazo,
                    fecha_asignacion_tasa = EXCLUDED.fecha_asignacion_tasa,
                    fecha_pago = EXCLUDED.fecha_pago,
                    source_id = EXCLUDED.source_id,
                    original_message = EXCLUDED.original_message,
                    validation_issues = EXCLUDED.validation_issues,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    ingestion_id,
                    table.name,
                    int(raw_record["source_row_number"]),
                    values["mercado"],
                    values["hora_negociacion"],
                    values["fecha_negociacion"],
                    values["comprador_compania"],
                    values["vendedor_compania"],
                    values["nombre_trader_comp"],
                    values["nombre_trader_vend"],
                    values["par_moneda"],
                    values["monto"],
                    values["precio_spot"],
                    values["precio_final"],
                    values["plazo"],
                    values["fecha_asignacion_tasa"],
                    values["fecha_pago"],
                    values["source_id"],
                    Jsonb(original_message),
                    Jsonb(safe_issues),
                ),
            )

        rows_saved += 1

        if issues:
            rows_with_warnings += 1

    return rows_saved, rows_with_warnings


# ============================================================
# DESPACHADOR ENTIDAD / OPERACIÓN
# ============================================================


# Las rutas soportadas se mantienen explícitas. Nunca se crean tablas
# dinámicamente a partir de nombres provenientes de archivos externos.


def save_report_data(
    *,
    ingestion_id: int,
    report: ParsedReport,
    entity: str,
) -> ReportPersistenceResult:
    """
    Persiste las operaciones de un archivo según entidad + producto.

    No utiliza raw_report_rows ni report_schemas.
    La única tabla común es ingestion_files.
    """

    entity_key = str(entity).strip().upper()

    rows_saved = 0
    rows_with_warnings = 0
    unsupported_reports: list[str] = []
    unclassified_sheets: list[str] = []

    with get_connection() as connection:
        _validate_ingestion(
            connection,
            ingestion_id=ingestion_id,
            entity=entity_key,
        )

        for sheet_name, table in report.tables.items():
            classification = classify_table(
                entity=entity_key,
                table=table,
            )

            if (
                not classification.recognized
                or not classification.report_type
            ):
                unclassified_sheets.append(sheet_name)
                continue

            report_type = classification.report_type.strip().upper()
            route = (entity_key, report_type)

            if route == ("SMBC", "FORWARD"):
                saved, warnings = _save_smbc_forward(
                    connection,
                    ingestion_id=ingestion_id,
                    table=table,
                    classification_issues=classification.issues,
                )

                rows_saved += saved
                rows_with_warnings += warnings
                continue

            unsupported_reports.append(
                f"{entity_key}/{report_type}"
            )

        # Se eliminan duplicados sin alterar el orden de aparición.
        unsupported_reports = list(
            dict.fromkeys(unsupported_reports)
        )
        unclassified_sheets = list(
            dict.fromkeys(unclassified_sheets)
        )

        complete = (
            not unsupported_reports
            and not unclassified_sheets
        )

        if not complete:
            ingestion_status = "partial"
        elif rows_with_warnings:
            ingestion_status = "stored_warning"
        else:
            ingestion_status = "stored"

        update_ingestion_status(
            connection,
            ingestion_id=ingestion_id,
            status=ingestion_status,
        )

    return ReportPersistenceResult(
        complete=complete,
        rows_saved=rows_saved,
        rows_with_warnings=rows_with_warnings,
        unsupported_reports=tuple(unsupported_reports),
        unclassified_sheets=tuple(unclassified_sheets),
    )
