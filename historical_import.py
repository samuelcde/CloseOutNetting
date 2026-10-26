from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata

from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from psycopg import sql
from psycopg.types.json import Jsonb

from app.postgres_db import (
    finalize_ingestion,
    get_connection,
    json_safe,
    register_ingestion_attempt,
    set_ingestion_status,
)

from historical_reader import (
    InventorySheet,
    group_by_file,
    iter_sheet_rows,
    load_report_passwords,
    read_inventory,
)


FINAL_STATUSES = {"stored", "stored_warning"}


class DuplicateLogicalContent(RuntimeError):
    def __init__(self, ingestion_id: int, content_sha256: str):
        super().__init__(
            f"Contenido lógico ya almacenado en ingestion_id={ingestion_id}."
        )
        self.ingestion_id = ingestion_id
        self.content_sha256 = content_sha256


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Carga histórica CloseNetting 2018-2026 a PostgreSQL. "
            "Por defecto opera en DRY-RUN."
        )
    )
    parser.add_argument("root", type=Path)
    parser.add_argument("inventory_csv", type=Path)
    parser.add_argument(
        "--map",
        dest="map_path",
        type=Path,
        default=Path("historical_schema_map.json"),
    )
    parser.add_argument("--min-year", type=int, default=2018)
    parser.add_argument("--max-year", type=int, default=2026)
    parser.add_argument(
        "--entity",
        action="append",
        help="Filtra entidad final. Puede repetirse.",
    )
    parser.add_argument(
        "--limit-files",
        type=int,
        default=0,
        help="0 = sin límite.",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Realiza escrituras. Sin esta opción solo simula.",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help=(
            "Reintenta una combinación entity/source_year/"
            "source_filename/file_sha256 ya registrada pero no finalizada."
        ),
    )
    parser.add_argument(
        "--retry-partial",
        action="store_true",
        help=(
            "Reintenta ?nicamente ingestas con status=partial "
            "reutilizando el mismo ingestion_id."
        ),
    )

    return parser.parse_args()


def load_mapping(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_entity(
    sheet: InventorySheet,
    mapping: dict[str, Any],
) -> str | None:
    entity = mapping["entity_remap"].get(sheet.entity_hint)

    if entity == "BLADEX":
        # DESCONOCIDA solo se acepta como BLADEX cuando la ruta lo confirma.
        if "bladex" not in sheet.relative_path.lower():
            return None

    return entity


def is_ignored_sheet(
    entity: str,
    sheet: InventorySheet,
    mapping: dict[str, Any],
) -> bool:
    for rule in mapping.get("ignored_sheet_rules", []):
        if rule.get("entity") != entity:
            continue
        if re.search(
            rule["sheet_regex"],
            sheet.sheet_name,
            flags=re.IGNORECASE,
        ):
            return True
    return False


def resolve_market(
    entity: str,
    sheet: InventorySheet,
    mapping: dict[str, Any],
) -> str | None:
    # Overrides primero porque corrigen DESCONOCIDO y algunos casos históricos.
    for rule in mapping.get("market_overrides", []):
        if rule.get("entity") != entity:
            continue
        structure_hash = rule.get("structure_hash")
        if structure_hash and structure_hash != sheet.structure_hash:
            continue
        if re.search(
            rule["sheet_regex"],
            sheet.sheet_name,
            flags=re.IGNORECASE,
        ):
            return str(rule["market_type"]).upper()

    raw = sheet.market_hint.strip().upper()

    if raw == "DESCONOCIDO":
        return None

    if raw == "OPTIONS/OPCIONES":
        if entity == "MORGAN_STANLEY":
            return "OPTIONS"
        return "OPCIONES"

    return raw


def _attempt_exists(
    *,
    entity: str,
    table_name: str,
    source_year: int | None,
    source_filename: str,
    file_sha256: str | None,
) -> tuple[int, str, Any] | None:
    """
    Busca un intento previo perteneciente al MODELO UNIFICADO.

    ingestion_files puede conservar registros legacy que todavía apuntan
    a tablas antiguas como raw_report_smbc_forward. Un registro legacy
    stored/stored_warning sin filas en la tabla unificada NO debe bloquear
    la nueva importación histórica.
    """

    with get_connection() as connection:
        with connection.cursor() as cursor:
            query = sql.SQL(
                """
                SELECT
                    i.id,
                    i.status,
                    i.processed_at,
                    EXISTS (
                        SELECT 1
                        FROM {}.{} r
                        WHERE r.ingestion_id = i.id
                    ) AS has_unified_rows
                FROM closenetting.ingestion_files i
                WHERE i.entity = %s
                  AND i.source_year IS NOT DISTINCT FROM %s
                  AND i.source_filename = %s
                  AND i.file_sha256 IS NOT DISTINCT FROM %s
                ORDER BY i.id DESC
                """
            ).format(
                sql.Identifier("closenetting"),
                sql.Identifier(table_name),
            )

            cursor.execute(
                query,
                (
                    entity,
                    source_year,
                    source_filename,
                    file_sha256 or None,
                ),
            )

            rows = cursor.fetchall()

    non_final_attempt_statuses = {
        "historical_pending",
        "historical_db_error",
        "partial",
        "no_database_rows",
        "historical_ignored",
        "password_required",
        "password_failed",
        "open_error",
        "empty_no_header",
        "scan_error",
        "historical_unreadable",
        "duplicate_binary_database",
        "duplicate_content_database",
    }

    for row in rows:
        ingestion_id = int(row[0])
        status = str(row[1])
        processed_at = row[2]
        has_unified_rows = bool(row[3])

        if has_unified_rows:
            return (
                ingestion_id,
                status,
                processed_at,
            )

        if status in non_final_attempt_statuses:
            return (
                ingestion_id,
                status,
                processed_at,
            )

        # stored/stored_warning sin filas en la tabla unificada:
        # se considera una ingesta LEGACY y se ignora para esta búsqueda.

    return None


def _find_unified_persisted_by_file_hash(
    *,
    entity: str,
    table_name: str,
    file_sha256: str,
) -> tuple[int, str] | None:
    """
    Deduplicación física solo contra archivos realmente persistidos
    en la tabla unificada de la entidad.
    """

    with get_connection() as connection:
        with connection.cursor() as cursor:
            query = sql.SQL(
                """
                SELECT
                    i.id,
                    i.source_filename
                FROM closenetting.ingestion_files i
                WHERE i.entity = %s
                  AND i.processed_at IS NOT NULL
                  AND i.file_sha256 = %s
                  AND EXISTS (
                      SELECT 1
                      FROM {}.{} r
                      WHERE r.ingestion_id = i.id
                  )
                ORDER BY i.id
                LIMIT 1
                """
            ).format(
                sql.Identifier("closenetting"),
                sql.Identifier(table_name),
            )

            cursor.execute(
                query,
                (
                    entity,
                    file_sha256,
                ),
            )

            row = cursor.fetchone()

            if row is None:
                return None

            return int(row[0]), str(row[1])



def _table_columns(
    connection,
    table_name: str,
) -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'closenetting'
              AND table_name = %s
            """,
            (table_name,),
        )
        return {
            str(name): str(data_type)
            for name, data_type in cursor.fetchall()
        }


def _blank(value: Any) -> bool:
    return (
        value is None
        or (
            isinstance(value, str)
            and not value.strip()
        )
    )


def _normalize_header_like(value: Any) -> str:
    """
    Normaliza un texto para comparar encabezados históricos.

    Ejemplos:
        "TIME (NEGOTIATION)" -> "time_negotiation"
        "FECHA NEGOCIACIÓN"  -> "fecha_negociacion"
    """

    if value is None:
        return ""

    raw = str(value).strip().lower()
    raw = unicodedata.normalize("NFKD", raw)
    raw = "".join(
        char
        for char in raw
        if not unicodedata.combining(char)
    )
    raw = re.sub(r"[^a-z0-9]+", "_", raw)
    raw = re.sub(r"_+", "_", raw).strip("_")
    return raw


_GENERIC_HEADER_LABELS = {
    "market",
    "mercado",
    "time",
    "hora",
    "time_negotiation",
    "hora_negociacion",
    "date",
    "fecha",
    "date_negotiation",
    "fecha_negociacion",
    "buyer",
    "buyer_entity",
    "comprador",
    "comprador_compania",
    "seller",
    "seller_entity",
    "vendedor",
    "vendedor_compania",
    "buyer_traders_name",
    "seller_traders_name",
    "nombre_trader_comp",
    "nombre_trader_vend",
    "currency",
    "currency_pair",
    "par_moneda",
    "amount",
    "monto",
    "spot_price",
    "precio_spot",
    "final_price",
    "precio_final",
    "maturity",
    "plazo",
    "fixing_date",
    "fecha_asignacion_tasa",
    "value_date",
    "fecha_pago",
    "start_date",
    "fecha_inicio_primer_flujo_fra",
    "interest_rate_m1",
    "interest_rate_m2",
    "interest_rate_period_m1",
    "interest_rate_period_m2",
    "interest_rate_type_m1",
    "interest_rate_type_m2",
    "spread_rate_m1",
    "spread_rate_m2",
    "day_count_convention_m1",
    "day_count_convention_m2",
    "strike_price",
    "stike_price",
    "strike_tasa",
    "volatility",
    "volatilidad",
    "trade_type",
    "tipo_operacion",
    "id",
    "clearing_house",
    "clearing_member",
    "nombre_de_entidad",
}


def _looks_like_instruction_row(
    row: list[Any],
) -> bool:
    """
    Detecta filas hist?ricas que describen obligatoriedad
    de columnas en lugar de contener una operaci?n real.

    Ejemplo GS:
        MANDATORY | MANDATORY | MANDATORY | ...

    Se exige:
      - m?nimo 3 celdas no vac?as;
      - al menos 60% de ellas deben ser marcadores estructurales.
    """

    non_blank = [
        value
        for value in row
        if not _blank(value)
    ]

    if len(non_blank) < 3:
        return False

    structural_markers = {
        "mandatory",
        "required",
        "optional",
    }

    matches = 0

    for value in non_blank:
        normalized = str(value).strip().lower()

        if normalized in structural_markers:
            matches += 1

    return (
        matches >= 3
        and matches / len(non_blank) >= 0.60
    )


def _looks_like_repeated_header(
    *,
    entity: str,
    sheet: InventorySheet,
    row: list[Any],
    mapping: dict[str, Any],
) -> bool:
    """
    Detecta filas de encabezado secundario/bilingüe dentro del área de datos.

    No depende solo del encabezado de la hoja. También compara contra todas
    las columnas históricas conocidas de la entidad, lo que permite detectar
    casos como una primera fila en español y una segunda fila en inglés.
    """

    non_blank_values = [
        value
        for value in row
        if not _blank(value)
    ]

    if len(non_blank_values) < 3:
        return False

    # Los encabezados son eminentemente textuales. Una fila con muchos
    # números/fechas reales no debe descartarse.
    textual_values = [
        value
        for value in non_blank_values
        if isinstance(value, str)
    ]

    if len(textual_values) / len(non_blank_values) < 0.75:
        return False

    known_labels = set(_GENERIC_HEADER_LABELS)

    known_labels.update(
        _normalize_header_like(value)
        for value in sheet.headers
        if str(value).strip()
    )
    known_labels.update(
        _normalize_header_like(value)
        for value in sheet.normalized_headers
        if str(value).strip()
    )

    entity_columns = (
        mapping
        .get("columns", {})
        .get(entity, {})
    )

    known_labels.update(
        _normalize_header_like(value)
        for value in entity_columns.keys()
    )

    aliases = mapping.get(
        "common_column_aliases",
        {},
    )

    known_labels.update(
        _normalize_header_like(value)
        for value in aliases.keys()
    )
    known_labels.update(
        _normalize_header_like(value)
        for value in aliases.values()
    )

    matches = 0

    for value in non_blank_values:
        normalized = _normalize_header_like(
            value
        )

        if not normalized:
            continue

        if normalized in known_labels:
            matches += 1
            continue

        # Variantes frecuentes: "% INTEREST RATE M1",
        # "INTEREST RATE TYPE M2", etc.
        if (
            normalized.startswith(
                (
                    "interest_rate_",
                    "spread_rate_",
                    "day_count_convention_",
                    "buyer_",
                    "seller_",
                )
            )
            and any(
                token in normalized
                for token in (
                    "rate",
                    "name",
                    "entity",
                    "trader",
                    "count",
                )
            )
        ):
            matches += 1

    ratio = matches / len(
        non_blank_values
    )

    return (
        matches >= 4
        and ratio >= 0.55
    )


def _to_decimal(value: Any) -> Decimal | None:
    if _blank(value):
        return None
    if isinstance(value, bool):
        raise ValueError("boolean no es numeric")
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))

    # Algunos históricos contienen caracteres Unicode invisibles al final
    # de números visualmente válidos (p. ej. "3,428<invisible>").
    # Se eliminan únicamente caracteres de control/formato y espacios
    # Unicode; no se alteran dígitos ni separadores decimal/miles.
    text = unicodedata.normalize(
        "NFKC",
        str(value),
    )
    text = "".join(
        char
        for char in text
        if not unicodedata.category(char).startswith("C")
    )
    text = "".join(
        char
        for char in text
        if not char.isspace()
    )
    text = (
        text
        .replace("\u2212", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]

    text = re.sub(r"[$€£COPUSD%]", "", text, flags=re.IGNORECASE)

    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        parts = text.split(",")
        if len(parts) == 2 and len(parts[1]) != 3:
            text = text.replace(",", ".")
        else:
            text = text.replace(",", "")

    result = Decimal(text)
    return -result if negative else result


DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%m-%d-%Y",
    "%d/%m/%y",
    "%m/%d/%y",
    "%d-%b-%Y",
    "%d-%b-%y",
    "%Y%m%d",
)

TIME_FORMATS = (
    "%H:%M",
    "%H:%M:%S",
    "%H:%M:%S.%f",
    "%I:%M %p",
    "%I:%M:%S %p",
)

DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
)


def _to_date(value: Any) -> date | None:
    if _blank(value):
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    # --------------------------------------------------------
    # Fechas seriales de Excel
    #
    # Ejemplos hist?ricos GS:
    #   44169 -> 2020-12-04
    #   44174 -> 2020-12-09
    #
    # Excel utiliza como base pr?ctica 1899-12-30.
    # --------------------------------------------------------

    if isinstance(value, (int, float, Decimal)):
        serial = float(value)

        # Rango conservador para evitar interpretar IDs como fechas.
        # Aproximadamente a?os 1900-2173.
        if 1 <= serial <= 100000:
            return (
                datetime(1899, 12, 30)
                + timedelta(days=serial)
            ).date()

    text_value = str(value).strip()

    # Tambi?n puede venir como texto num?rico.
    if re.fullmatch(r"\d+(?:\.\d+)?", text_value):
        serial = float(text_value)

        if 1 <= serial <= 100000:
            return (
                datetime(1899, 12, 30)
                + timedelta(days=serial)
            ).date()

    try:
        return date.fromisoformat(text_value)
    except Exception:
        pass

    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(
                text_value,
                fmt
            ).date()
        except ValueError:
            pass

    raise ValueError(text_value)



def _to_datetime(value: Any) -> datetime | None:
    if _blank(value):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)

    text = str(value).strip()
    try:
        return datetime.fromisoformat(text)
    except Exception:
        pass
    for fmt in DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    # Una fecha sola también es válida como timestamp a medianoche.
    d = _to_date(text)
    return datetime.combine(d, time.min) if d else None


def _to_time(value: Any) -> time | None:
    if _blank(value):
        return None
    if isinstance(value, datetime):
        return value.time().replace(tzinfo=None)
    if isinstance(value, time):
        return value.replace(tzinfo=None)

    text = str(value).strip()
    try:
        return time.fromisoformat(text).replace(tzinfo=None)
    except Exception:
        pass
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(text.upper(), fmt).time()
        except ValueError:
            pass
    raise ValueError(text)


def _is_date_or_datetime_value(
    value: Any,
) -> bool:
    """
    True únicamente cuando el valor representa fecha/datetime,
    no una hora aislada.
    """

    if _blank(value):
        return False

    if isinstance(value, datetime):
        return True

    if isinstance(value, date) and not isinstance(
        value,
        datetime,
    ):
        return True

    if isinstance(value, time):
        return False

    text = str(value).strip()

    # Una hora aislada no se debe confundir con fecha.
    if re.fullmatch(
        r"\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
        r"(?:\s*[AP]M)?",
        text,
        flags=re.IGNORECASE,
    ):
        return False

    try:
        _to_datetime(value)
        return True
    except Exception:
        return False


def _is_time_only_value(
    value: Any,
) -> bool:
    """
    True únicamente para una hora aislada.
    """

    if _blank(value):
        return False

    if isinstance(value, time):
        return True

    if isinstance(value, (date, datetime)):
        return False

    text = str(value).strip()

    # Exigir aspecto horario para evitar interpretar IDs como hora.
    if not re.fullmatch(
        r"\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
        r"(?:\s*[AP]M)?",
        text,
        flags=re.IGNORECASE,
    ):
        return False

    try:
        _to_time(value)
        return True
    except Exception:
        return False


def _realign_date_time_pairs(
    values: dict[str, Any],
) -> int:
    """
    Corrige únicamente inversiones inequívocas fecha/hora.

    Ejemplo histórico detectado:
        HORA NEGOCIACIÓN  -> 2019-12-06T00:00:00
        FECHA NEGOCIACIÓN -> 11:41:05

    Se intercambian solo cuando:
      A) el valor destinado a hora es claramente fecha/datetime, Y
      B) el valor destinado a fecha es claramente hora aislada.

    Retorna cantidad de pares corregidos.
    """

    corrected = 0

    pairs = (
        (
            "hora_negociacion",
            "fecha_negociacion",
        ),
        (
            "time_negotiation",
            "date_negotiation",
        ),
    )

    for time_field, date_field in pairs:
        if (
            time_field not in values
            or date_field not in values
        ):
            continue

        time_value = values.get(
            time_field
        )
        date_value = values.get(
            date_field
        )

        if (
            _is_date_or_datetime_value(
                time_value
            )
            and _is_time_only_value(
                date_value
            )
        ):
            values[time_field], values[date_field] = (
                date_value,
                time_value,
            )
            corrected += 1

    return corrected


def _to_bool(value: Any) -> bool | None:
    if _blank(value):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value in (0, 1):
            return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "yes", "y", "si", "sí", "1"}:
        return True
    if text in {"false", "no", "n", "0"}:
        return False
    raise ValueError(text)


NULL_MARKERS = {
    "NA",
    "N/A",
    "NOT APPLICABLE",
}


def _is_null_marker(value: Any) -> bool:
    if not isinstance(value, str):
        return False

    normalized = " ".join(
        value.strip().upper().split()
    )

    return normalized in NULL_MARKERS


def convert_for_db(
    value: Any,
    data_type: str,
    *,
    field: str,
    issues: list[dict[str, Any]],
) -> Any:
    if _blank(value):
        return None

    dt = data_type.lower()

    # Marcadores hist?ricos de "no aplica / sin dato".
    # Solo se convierten a NULL cuando el destino NO es textual.
    #
    # Ejemplos:
    #   fecha_asignacion_tasa = "NA"  -> NULL
    #   fecha_pago            = "N/A" -> NULL
    #
    # Si una columna TEXT contiene literalmente "NA", se conserva.
    if (
        dt not in {
            "text",
            "character varying",
            "character",
        }
        and _is_null_marker(value)
    ):
        return None

    try:
        if dt in {
            "text",
            "character varying",
            "character",
        }:
            return str(value).strip()
        if dt in {
            "numeric",
            "decimal",
            "real",
            "double precision",
        }:
            return _to_decimal(value)
        if dt in {
            "bigint",
            "integer",
            "smallint",
        }:
            number = _to_decimal(value)
            if number is None:
                return None
            if number != number.to_integral_value():
                raise ValueError(f"No entero: {value}")
            return int(number)
        if dt == "date":
            return _to_date(value)
        if dt.startswith("timestamp"):
            return _to_datetime(value)
        if dt.startswith("time"):
            return _to_time(value)
        if dt == "boolean":
            return _to_bool(value)
        return str(value).strip()
    except Exception:
        issues.append(
            {
                "source": "historical_import",
                "code": "TYPE_CONVERSION_FAILED",
                "field": field,
                "target_type": data_type,
                "value": str(value),
            }
        )
        return None


def row_source_cells(
    sheet: InventorySheet,
    row: list[Any],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    max_len = max(
        len(row),
        len(sheet.headers),
        len(sheet.normalized_headers),
    )

    for index in range(max_len):
        header = (
            sheet.headers[index]
            if index < len(sheet.headers)
            else f"column_{index + 1}"
        )
        normalized = (
            sheet.normalized_headers[index]
            if index < len(sheet.normalized_headers)
            else f"column_{index + 1}"
        )
        value = (
            row[index]
            if index < len(row)
            else None
        )

        result.append(
            {
                "header": header,
                "normalized_header": normalized,
                "value": value,
            }
        )

    # Algunos .xlsx informan más columnas físicas que encabezados reales.
    # Si son columnas sintéticas (column_XX) y están vacías, no forman parte
    # de la operación ni deben alterar el content_sha256.
    while result:
        last = result[-1]
        normalized = str(
            last["normalized_header"]
        ).strip()

        if (
            normalized.startswith("column_")
            and _blank(last["value"])
        ):
            result.pop()
            continue

        break

    return result


def _canonical_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    return str(value)


def update_content_hash(
    digest,
    *,
    market_type: str,
    sheet: InventorySheet,
    source_cells: list[dict[str, Any]],
) -> None:
    payload = {
        "market_type": market_type,
        "sheet": sheet.sheet_name,
        "cells": sorted(
            (
                str(cell["normalized_header"]),
                _canonical_json_value(cell["value"]),
            )
            for cell in source_cells
        ),
    }
    digest.update(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    digest.update(b"\n")


def project_canonical(
    *,
    entity: str,
    market_type: str,
    raw_values: dict[str, Any],
    mapping: dict[str, Any],
    values: dict[str, Any],
    issues: list[dict[str, Any]],
) -> None:
    if entity != "MORGAN_STANLEY":
        return

    projections = (
        mapping
        .get("morgan_canonical_projections", {})
        .get(market_type, {})
    )

    for source, destination in projections.items():
        raw = raw_values.get(source)
        if _blank(raw):
            continue

        current = values.get(destination)
        if _blank(current):
            values[destination] = raw
        elif str(current) != str(raw):
            issues.append(
                {
                    "source": "historical_import",
                    "code": "CANONICAL_ALIAS_CONFLICT",
                    "field": destination,
                    "source_column": source,
                }
            )


def resolve_gs_row_market(
    *,
    entity: str,
    sheet: InventorySheet,
    default_market: str,
    source_cells: list[dict[str, Any]],
) -> str:
    """
    Refina market_type a nivel de FILA para hist?ricos GS.

    Sheet3 es una hoja mixta. No todas sus operaciones son FORWARD.

    Clasificaci?n validada contra los hist?ricos:

        Forward                 -> FORWARD
        Cash Settled Forward    -> FORWARD
        Bond Forward Tradable   -> FORWARD
        CT Swap                 -> IRS

    Si la fila no corresponde a GS/Sheet3 o no reconocemos
    tradableType, se conserva el mercado determinado a nivel de hoja.
    """

    if str(entity).upper() != "GS":
        return default_market

    sheet_name = str(
        sheet.sheet_name or ""
    ).strip().upper()

    if sheet_name != "SHEET3":
        return default_market

    tradable_type = ""

    for cell in source_cells:

        normalized_header = str(
            cell.get("normalized_header") or ""
        ).strip().lower()

        if normalized_header == "tradabletype":

            value = cell.get("value")

            if value is not None:
                tradable_type = " ".join(
                    str(value).strip().upper().split()
                )

            break

    if tradable_type in {
        "FORWARD",
        "CASH SETTLED FORWARD",
        "BOND FORWARD TRADABLE",
    }:
        return "FORWARD"

    if tradable_type == "CT SWAP":
        return "IRS"

    return default_market


def insert_row(
    connection,
    *,
    table_name: str,
    column_types: dict[str, str],
    ingestion_id: int,
    market_type: str,
    sheet: InventorySheet,
    row: list[Any],
    mapping: dict[str, Any],
) -> tuple[int, int, int]:
    entity_name = resolve_entity_name(
        mapping,
        table_name,
    )

    common_aliases = mapping.get(
        "common_column_aliases",
        {},
    )

    entity_aliases = (
        mapping
        .get("entity_column_aliases", {})
        .get(entity_name, {})
    )

    allowed = set(
        mapping["columns"][entity_name].keys()
    )

    source_cells = row_source_cells(sheet, row)
    raw_values: dict[str, Any] = {}
    issues: list[dict[str, Any]] = []
    semantic_alias_values_mapped = 0

    for cell in source_cells:
        src = str(cell["normalized_header"]).strip()
        if not src:
            continue
        raw_values[src] = cell["value"]

    values: dict[str, Any] = {}

    for src, raw in raw_values.items():
        # 1. Resolver normalizaciones generales (acentos/typos).
        base_destination = common_aliases.get(
            src,
            src,
        )

        # 2. Resolver equivalencia semántica específica de la entidad.
        #    Esto evita mezclar, por ejemplo, VALUE_DATE de SMBC con
        #    VALUE_DATE canónico de Morgan Stanley.
        destination = entity_aliases.get(
            base_destination,
            entity_aliases.get(
                src,
                base_destination,
            ),
        )

        if (
            destination != base_destination
            and not _blank(raw)
        ):
            semantic_alias_values_mapped += 1

        if destination not in allowed:
            # No generar warnings por columnas físicas sin encabezado
            # cuyo valor está vacío. Son residuos del ancho de la hoja.
            if _blank(raw):
                continue

            issues.append(
                {
                    "source": "historical_import",
                    "code": "UNMAPPED_HISTORICAL_COLUMN",
                    "column": src,
                }
            )
            continue

        if destination in values and not _blank(values[destination]) and not _blank(raw):
            if str(values[destination]) != str(raw):
                issues.append(
                    {
                        "source": "historical_import",
                        "code": "COLUMN_ALIAS_CONFLICT",
                        "field": destination,
                        "source_column": src,
                    }
                )
            continue

        values[destination] = raw

    project_canonical(
        entity=entity_name,
        market_type=market_type,
        raw_values=raw_values,
        mapping=mapping,
        values=values,
        issues=issues,
    )

    # Corregir inversiones inequívocas entre fecha y hora antes
    # de convertir a tipos PostgreSQL. original_message conserva
    # los valores originales tal como llegaron en el archivo.
    realigned_pairs = _realign_date_time_pairs(
        values
    )

    converted: dict[str, Any] = {}
    for name, value in values.items():
        data_type = column_types.get(name)
        if not data_type:
            issues.append(
                {
                    "source": "historical_import",
                    "code": "DESTINATION_COLUMN_MISSING",
                    "column": name,
                }
            )
            continue
        converted[name] = convert_for_db(
            value,
            data_type,
            field=name,
            issues=issues,
        )

    insert_columns = [
        "ingestion_id",
        "market_type",
        "source_sheet_name",
        *sorted(converted.keys()),
        "original_message",
        "validation_issues",
    ]

    params = [
        ingestion_id,
        market_type,
        sheet.sheet_name,
        *(converted[name] for name in sorted(converted.keys())),
        Jsonb(json_safe(source_cells)),
        Jsonb(json_safe(issues)),
    ]

    query = sql.SQL("INSERT INTO {}.{} ({}) VALUES ({})").format(
        sql.Identifier("closenetting"),
        sql.Identifier(table_name),
        sql.SQL(", ").join(sql.Identifier(name) for name in insert_columns),
        sql.SQL(", ").join(sql.Placeholder() for _ in insert_columns),
    )

    with connection.cursor() as cursor:
        cursor.execute(query, params)

    return (
        1 if issues else 0,
        realigned_pairs,
        semantic_alias_values_mapped,
    )


def resolve_entity_name(mapping: dict[str, Any], table_name: str) -> str:
    for entity, table in mapping["tables"].items():
        if table == table_name:
            return entity
    raise KeyError(table_name)


def find_logical_duplicate(
    connection,
    *,
    entity: str,
    table_name: str,
    content_sha256: str,
    current_ingestion_id: int,
) -> int | None:
    """
    Busca duplicado lógico únicamente entre ingestas finalizadas
    que realmente tengan filas en la tabla unificada destino.
    """

    with connection.cursor() as cursor:
        query = sql.SQL(
            """
            SELECT i.id
            FROM closenetting.ingestion_files i
            WHERE i.entity = %s
              AND i.id <> %s
              AND i.processed_at IS NOT NULL
              AND i.content_sha256 = %s
              AND EXISTS (
                  SELECT 1
                  FROM {}.{} r
                  WHERE r.ingestion_id = i.id
              )
            ORDER BY i.id
            LIMIT 1
            """
        ).format(
            sql.Identifier("closenetting"),
            sql.Identifier(table_name),
        )

        cursor.execute(
            query,
            (
                entity,
                current_ingestion_id,
                content_sha256,
            ),
        )

        row = cursor.fetchone()
        return int(row[0]) if row else None



def status_from_scan(scan_status: str) -> str:
    raw = scan_status.strip().upper()
    mapping = {
        "PASSWORD_REQUIRED": "password_required",
        "PASSWORD_FAILED": "password_failed",
        "OPEN_ERROR": "open_error",
        "OPEN_ERROR_AFTER_DECRYPT": "open_error",
        "EMPTY_OR_NO_HEADER": "empty_no_header",
        "SCAN_ERROR": "scan_error",
    }
    return mapping.get(raw, "historical_unreadable")


def main() -> int:
    args = parse_args()

    root = args.root.expanduser().resolve()
    inventory_path = args.inventory_csv.expanduser().resolve()
    map_path = args.map_path.expanduser().resolve()

    if not root.is_dir():
        print(f"ERROR: no existe la raíz: {root}", file=sys.stderr)
        return 2
    if not inventory_path.is_file():
        print(f"ERROR: no existe inventario: {inventory_path}", file=sys.stderr)
        return 2
    if not map_path.is_file():
        print(f"ERROR: no existe mapa: {map_path}", file=sys.stderr)
        return 2

    mapping = load_mapping(map_path)
    password_map = load_report_passwords()

    inventory = read_inventory(inventory_path)
    grouped = group_by_file(inventory)

    entity_filter = {
        value.strip().upper()
        for value in (args.entity or [])
        if value.strip()
    }

    candidates: list[tuple[str, list[InventorySheet]]] = []
    for relative_path, sheets in grouped.items():
        first = sheets[0]
        if first.source_year is None:
            continue
        if not (args.min_year <= first.source_year <= args.max_year):
            continue

        entity = resolve_entity(first, mapping)
        if not entity:
            continue
        if entity_filter and entity.upper() not in entity_filter:
            continue

        candidates.append((relative_path, sheets))

    candidates.sort(key=lambda item: item[0].lower())

    if args.limit_files > 0:
        candidates = candidates[: args.limit_files]

    print("=" * 72)
    print("AUTO CLOSENETTING - IMPORTACIÓN HISTÓRICA")
    print("=" * 72)
    print(f"Modo: {'COMMIT' if args.commit else 'DRY-RUN'}")
    print(f"Archivos candidatos: {len(candidates)}")
    print(f"Años: {args.min_year}-{args.max_year}")
    if entity_filter:
        print("Entidades: " + ", ".join(sorted(entity_filter)))
    print()

    counters = {
        "files_seen": 0,
        "already_attempted": 0,
        "stored": 0,
        "stored_warning": 0,
        "duplicate_binary": 0,
        "duplicate_content": 0,
        "failed": 0,
        "partial": 0,
        "ignored": 0,
        "rows_saved": 0,
        "repeated_header_rows_skipped": 0,
        "instruction_rows_skipped": 0,
        "date_time_pairs_realigned": 0,
        "semantic_alias_values_mapped": 0,
    }

    for relative_path, sheets in candidates:
        counters["files_seen"] += 1
        first = sheets[0]
        entity = resolve_entity(first, mapping)
        assert entity is not None
        table_name = mapping["tables"][entity]
        full_path = root / Path(relative_path)

        print(
            f"[{counters['files_seen']}/{len(candidates)}] "
            f"{entity} | {first.source_year} | {first.filename}"
        )

        previous = None
        if args.commit:
            previous = _attempt_exists(
                entity=entity,
                table_name=table_name,
                source_year=first.source_year,
                source_filename=first.filename,
                file_sha256=first.file_sha256 or None,
            )

        retry_ingestion_id = None

        if previous:

            previous_id = previous[0]
            previous_status = previous[1]

            # -----------------------------------------------
            # Reintento especial de PARTIAL:
            # reutiliza exactamente el mismo ingestion_id.
            # -----------------------------------------------

            if (
                getattr(args, "retry_partial", False)
                and previous_status == "partial"
            ):

                retry_ingestion_id = previous_id

                print(
                    f"  REINTENTO PARTIAL: "
                    f"ingestion_id={previous_id}"
                )

            # -----------------------------------------------
            # Mantener comportamiento existente de
            # --retry-failed si esa opci?n existe.
            # -----------------------------------------------

            elif getattr(args, "retry_failed", False):

                pass

            else:

                print(
                    f"  OMITIDO: intento ya registrado "
                    f"(ingestion_id={previous_id}, "
                    f"status={previous_status})."
                )

                counters["already_attempted"] += 1
                continue

        ok_sheets = [
            sheet
            for sheet in sheets
            if sheet.scan_status == "OK"
        ]

        if not ok_sheets:
            status = status_from_scan(first.scan_status)
            if args.commit:
                ingestion_id = register_ingestion_attempt(
                    entity=entity,
                    source_filename=first.filename,
                    source_year=first.source_year,
                    file_sha256=first.file_sha256 or None,
                    content_sha256=None,
                    size_bytes=first.size_bytes,
                    received_at=None,
                    status=status,
                )
                print(
                    f"  ingestion_id={ingestion_id} | "
                    f"{status} | processed_at=NULL"
                )
            else:
                print(f"  DRY-RUN: {status}")
            counters["failed"] += 1
            continue

        # Duplicado físico frente a una ingesta ya finalizada.
        persisted = None
        if args.commit and first.file_sha256:
            persisted = _find_unified_persisted_by_file_hash(
                entity=entity,
                table_name=table_name,
                file_sha256=first.file_sha256,
            )

        if persisted:
            ingestion_id = register_ingestion_attempt(
                entity=entity,
                source_filename=first.filename,
                source_year=first.source_year,
                file_sha256=first.file_sha256,
                content_sha256=None,
                size_bytes=first.size_bytes,
                received_at=None,
                status="duplicate_binary_database",
            )
            print(
                f"  DUPLICADO BINARIO de ingestion_id={persisted[0]}; "
                f"nuevo intento={ingestion_id}, processed_at=NULL"
            )
            counters["duplicate_binary"] += 1
            continue

        if not args.commit:
            supported = 0
            unsupported = 0
            ignored = 0
            for sheet in ok_sheets:
                if is_ignored_sheet(entity, sheet, mapping):
                    ignored += 1
                    continue
                market = resolve_market(entity, sheet, mapping)
                if market:
                    supported += 1
                else:
                    unsupported += 1
            print(
                f"  DRY-RUN: hojas soportadas={supported}, "
                f"sin clasificar={unsupported}, ignoradas={ignored}"
            )
            continue

        if retry_ingestion_id is not None:

            ingestion_id = retry_ingestion_id

            set_ingestion_status(
                ingestion_id=ingestion_id,
                status="historical_pending",
            )

            print(
                f"  reutilizando ingestion_id="
                f"{ingestion_id}"
            )

        else:

            ingestion_id = register_ingestion_attempt(
                entity=entity,
                source_filename=first.filename,
                source_year=first.source_year,
                file_sha256=first.file_sha256 or None,
                content_sha256=None,
                size_bytes=first.size_bytes,
                received_at=None,
                status="historical_pending",
            )

        rows_saved = 0
        rows_warning = 0
        unsupported_sheets: list[str] = []
        supported_sheets = 0
        ignored_sheets = 0
        digest = hashlib.sha256()

        try:
            with get_connection() as connection:
                column_types = _table_columns(
                    connection,
                    table_name,
                )

                # Reintento del mismo ingestion_id no duplica filas.
                with connection.cursor() as cursor:
                    cursor.execute(
                        sql.SQL("DELETE FROM {}.{} WHERE ingestion_id = %s").format(
                            sql.Identifier("closenetting"),
                            sql.Identifier(table_name),
                        ),
                        (ingestion_id,),
                    )

                for sheet in ok_sheets:
                    if is_ignored_sheet(entity, sheet, mapping):
                        ignored_sheets += 1
                        continue

                    market_type = resolve_market(
                        entity,
                        sheet,
                        mapping,
                    )

                    if not market_type:
                        unsupported_sheets.append(
                            sheet.sheet_name or "(sin nombre)"
                        )
                        continue

                    supported_sheets += 1

                    for row in iter_sheet_rows(
                        root,
                        sheet,
                        entity,
                        password_map,
                    ):
                        if not any(
                            value is not None
                            and str(value).strip() != ""
                            for value in row
                        ):
                            continue

                        if _looks_like_instruction_row(
                            row
                        ):
                            counters[
                                "instruction_rows_skipped"
                            ] += 1
                            continue

                        if _looks_like_repeated_header(
                            entity=entity,
                            sheet=sheet,
                            row=row,
                            mapping=mapping,
                        ):
                            counters[
                                "repeated_header_rows_skipped"
                            ] += 1
                            continue

                        source_cells = row_source_cells(
                            sheet,
                            row,
                        )

                        if not source_cells:
                            continue

                        # -----------------------------------------
                        # Refinar market_type a nivel de operaci?n.
                        #
                        # Especialmente importante para GS/Sheet3,
                        # que contiene FORWARD y CT Swap (IRS)
                        # dentro de la misma hoja.
                        # -----------------------------------------

                        row_market_type = resolve_gs_row_market(
                            entity=entity,
                            sheet=sheet,
                            default_market=market_type,
                            source_cells=source_cells,
                        )

                        update_content_hash(
                            digest,
                            market_type=row_market_type,
                            sheet=sheet,
                            source_cells=source_cells,
                        )

                        (
                            warning,
                            realigned_pairs,
                            semantic_alias_values,
                        ) = insert_row(
                            connection,
                            table_name=table_name,
                            column_types=column_types,
                            ingestion_id=ingestion_id,
                            market_type=row_market_type,
                            sheet=sheet,
                            row=row,
                            mapping=mapping,
                        )
                        rows_saved += 1
                        rows_warning += warning
                        counters[
                            "date_time_pairs_realigned"
                        ] += realigned_pairs
                        counters[
                            "semantic_alias_values_mapped"
                        ] += semantic_alias_values

                content_sha256 = (
                    digest.hexdigest()
                    if rows_saved > 0
                    else None
                )

                if rows_saved > 0 and content_sha256:
                    duplicate_id = find_logical_duplicate(
                        connection,
                        entity=entity,
                        table_name=table_name,
                        content_sha256=content_sha256,
                        current_ingestion_id=ingestion_id,
                    )
                    if duplicate_id is not None:
                        raise DuplicateLogicalContent(
                            duplicate_id,
                            content_sha256,
                        )

                if rows_saved <= 0:
                    from app.postgres_db import update_ingestion_status
                    update_ingestion_status(
                        connection,
                        ingestion_id=ingestion_id,
                        status=(
                            "historical_ignored"
                            if ignored_sheets and not unsupported_sheets
                            else "no_database_rows"
                        ),
                        content_sha256=content_sha256,
                    )
                    counters["ignored" if ignored_sheets else "failed"] += 1

                elif unsupported_sheets:
                    from app.postgres_db import update_ingestion_status
                    update_ingestion_status(
                        connection,
                        ingestion_id=ingestion_id,
                        status="partial",
                        content_sha256=content_sha256,
                    )
                    counters["partial"] += 1

                else:
                    status = (
                        "stored_warning"
                        if rows_warning
                        else "stored"
                    )
                    finalize_ingestion(
                        connection,
                        ingestion_id=ingestion_id,
                        status=status,
                        content_sha256=content_sha256,
                    )
                    counters[status] += 1

            counters["rows_saved"] += rows_saved

            print(
                f"  ingestion_id={ingestion_id} | filas={rows_saved} | "
                f"warnings={rows_warning} | "
                f"sin_clasificar={len(unsupported_sheets)}"
            )

        except DuplicateLogicalContent as exc:
            set_ingestion_status(
                ingestion_id=ingestion_id,
                status="duplicate_content_database",
                content_sha256=exc.content_sha256,
            )
            print(
                f"  DUPLICADO LÓGICO de ingestion_id={exc.ingestion_id}; "
                "processed_at=NULL"
            )
            counters["duplicate_content"] += 1

        except Exception as exc:
            try:
                set_ingestion_status(
                    ingestion_id=ingestion_id,
                    status="historical_db_error",
                )
            except Exception:
                pass
            print(
                f"  ERROR ingestion_id={ingestion_id}: "
                f"{type(exc).__name__}: {exc}"
            )
            counters["failed"] += 1

    print()
    print("=" * 72)
    print("RESUMEN")
    print("=" * 72)
    for key, value in counters.items():
        print(f"{key}: {value}")

    if not args.commit:
        print()
        print(
            "DRY-RUN completado. No se realizaron escrituras. "
            "Use --commit únicamente después de revisar el resultado."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
