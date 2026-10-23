from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from .parser import ParsedReport, ParsedTable
from .postgres_db import (
    finalize_ingestion,
    get_connection,
    json_safe,
    update_ingestion_status,
)
from .report_classifier import (
    classify_table,
    normalize_name,
)


# ============================================================
# RESULTADO
# ============================================================


@dataclass(frozen=True, slots=True)
class StructureChange:
    entity: str
    report_type: str
    sheet_name: str
    code: str
    column: str


@dataclass(frozen=True, slots=True)
class ReportPersistenceResult:
    complete: bool
    rows_saved: int
    rows_with_warnings: int
    ingestion_id: int | None = None
    unsupported_reports: tuple[str, ...] = ()
    unclassified_sheets: tuple[str, ...] = ()
    structure_changes: tuple[StructureChange, ...] = ()


@dataclass(frozen=True, slots=True)
class FieldRule:
    aliases: tuple[str, ...]
    kind: str = "text"


# ============================================================
# MAPEO DE CELDAS
# ============================================================


def _cells_to_dict(
    source_cells: list[dict[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    counters: dict[str, int] = {}

    for cell in source_cells:
        name = normalize_name(cell.get("header"))

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
        pass

    for date_format in (
        "%m/%d/%Y",
        "%m/%d/%y",
    ):
        try:
            return datetime.strptime(
                text,
                date_format,
            ).date()
        except ValueError:
            continue

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

    if "." in text and "," in text:
        if text.rfind(",") > text.rfind("."):
            text = (
                text
                .replace(".", "")
                .replace(",", ".")
            )
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


def _convert(
    value: Any,
    *,
    kind: str,
    field: str,
    issues: list[dict[str, Any]],
) -> Any:
    if kind == "text":
        return _to_text(value)

    if kind == "date":
        return _to_date(
            value,
            field=field,
            issues=issues,
        )

    if kind == "time":
        return _to_time(
            value,
            field=field,
            issues=issues,
        )

    if kind == "decimal":
        return _to_decimal(
            value,
            field=field,
            issues=issues,
        )

    raise RuntimeError(
        f"Tipo de conversión no soportado: {kind}"
    )


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


def _structure_changes(
    *,
    entity: str,
    report_type: str,
    sheet_name: str,
    issues: tuple[dict[str, Any], ...],
) -> list[StructureChange]:
    changes: list[StructureChange] = []

    for issue in issues:
        code = str(issue.get("code") or "")
        column = str(issue.get("column") or "")

        if code not in {
            "UNEXPECTED_COLUMN",
            "MISSING_EXPECTED_COLUMN",
        }:
            continue

        if not column:
            continue

        changes.append(
            StructureChange(
                entity=entity,
                report_type=report_type,
                sheet_name=sheet_name,
                code=code,
                column=column,
            )
        )

    return changes


# ============================================================
# VALIDACIÓN DE INGESTA
# ============================================================


def _validate_ingestion(
    connection: Connection,
    *,
    ingestion_id: int,
    entity: str,
) -> None:
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
        raise RuntimeError(
            f"No existe ingestion_id={ingestion_id}."
        )

    stored_entity = str(row[0]).strip().upper()
    entity_key = str(entity).strip().upper()

    if stored_entity != entity_key:
        raise RuntimeError(
            f"El ingestion_id={ingestion_id} pertenece a la entidad "
            f"'{stored_entity}', no a '{entity_key}'."
        )


# ============================================================
# ESQUEMA POR ENTIDAD
#
# Una única tabla física por entidad.
# Cada mercado ocupa la misma tabla y market_type identifica
# el producto.
#
# IMPORTANTE:
# Los nombres de tabla y columnas son constantes internas.
# Nunca se construyen desde valores no confiables del Excel.
# ============================================================


ENTITY_TABLES: dict[str, str] = {
    "SMBC": "closenetting.raw_report_smbc",
    "GS": "closenetting.raw_report_gs",
    "MORGAN_STANLEY": (
        "closenetting.raw_report_morgan_stanley"
    ),
}


SMBC_COLUMNS = (
    "mercado",
    "hora_negociacion",
    "fecha_negociacion",
    "comprador_compania",
    "vendedor_compania",
    "nombre_trader_comp",
    "nombre_trader_vend",
    "par_moneda",
    "monto",
    "precio_spot",
    "precio_final",
    "plazo",
    "fecha_asignacion_tasa",
    "fecha_pago",
    "source_id",
)


GS_COLUMNS = (
    "tipo",
    "mercado",
    "hora_negociacion",
    "fecha_negociacion",
    "comprador_compania",
    "vendedor_compania",
    "nombre_trader_comp",
    "nombre_trader_vend",
    "par_moneda",
    "monto",
    "precio_spot",
    "precio_final",
    "plazo",
    "fecha_asignacion_tasa",
    "fecha_inicio_primer_flujo_fra",
    "fecha_pago",
    "tasa_ref_de_interes_m1",
    "tasa_ref_de_interes_m2",
    "periocidad_m1",
    "periocidad_m2",
    "tipo_de_interes_m1",
    "margen_m1",
    "conteo_dias_m1",
    "tipo_de_interes_m2",
    "margen_m2",
    "conteo_dias_m2",
    "strike_tasa",
    "volatilidad",
    "tipo_operacion",
    "source_id",
)


MORGAN_STANLEY_COLUMNS = (
    "record_type",
    "trade_type",
    "product_type",
    "trade_date",
    "buy_sell",
    "counterparty_code",
    "counterparty_name",
    "order_placer",
    "account_id",
    "legal_entity_code",
    "bic",
    "buy_currency",
    "buy_amount",
    "sell_currency",
    "sell_amount",
    "rate",
    "fixing_date",
    "value_date",
    "order_number",
    "entered_by",
    "entry_time",
    "executed_user",
    "app",

    "trade_nm",
    "account_name",
    "orig_notional_amt",
    "currency_cd",
    "pay_direction",
    "fixed_rate",
    "effective_date",
    "termination_date",
    "economics_type_cd",
    "usi_prefix",
    "usi_value",
    "uti_prefix",
    "uti_value",
    "legal_entity_cd",

    "event_date",
    "trade_ref",
    "transaction_type",
    "quantity",
    "ccp_cleared_price",
    "destination_account",
    "company_code",
    "mic_code",
    "currency",
    "asset_description",
    "issue_type",
    "ex_contract_code",
    "contract_date",
    "put_or_call",
    "strike_price",

    "trade_id",
    "trade_calendar_date",
    "expiry_calendar_date",
    "trade_status",
    "taps_account_id",
    "ccy1",
    "ccy1_amt",
    "ccy2",
    "ccy2_amt",
    "b2b_trade_role",

    "trade_num",
    "source_system",
    "transaction_date",
    "exposure_account",
    "co_legal_name",
    "short_name",
    "settlement_account",
    "party_id",
    "commodity_codes",
    "product_class",
    "subproduct_name",
    "transaction_term_start_date",
    "transaction_term_end_date",
    "internal_company_name",
    "usi",
    "uti",
    "broker_short_name",
    "fixed_price",
    "fixed_price_currency",
    "strike_price_currency",
)


ENTITY_COLUMNS: dict[str, tuple[str, ...]] = {
    "SMBC": SMBC_COLUMNS,
    "GS": GS_COLUMNS,
    "MORGAN_STANLEY": MORGAN_STANLEY_COLUMNS,
}


# ============================================================
# REGLAS POR MERCADO
# ============================================================


def _rule(
    *aliases: str,
    kind: str = "text",
) -> FieldRule:
    return FieldRule(
        aliases=tuple(aliases),
        kind=kind,
    )


MARKET_RULES: dict[
    tuple[str, str],
    dict[str, FieldRule],
] = {
    # --------------------------------------------------------
    # SMBC / FORWARD
    # --------------------------------------------------------
    ("SMBC", "FORWARD"): {
        "mercado": _rule("mercado"),
        "hora_negociacion": _rule(
            "hora_negociacion",
            kind="time",
        ),
        "fecha_negociacion": _rule(
            "fecha_negociacion",
            kind="date",
        ),
        "comprador_compania": _rule(
            "comprador_compania"
        ),
        "vendedor_compania": _rule(
            "vendedor_compania"
        ),
        "nombre_trader_comp": _rule(
            "nombre_trader_comp"
        ),
        "nombre_trader_vend": _rule(
            "nombre_trader_vend"
        ),
        "par_moneda": _rule("par_moneda"),
        "monto": _rule(
            "monto",
            kind="decimal",
        ),
        "precio_spot": _rule(
            "precio_spot",
            kind="decimal",
        ),
        "precio_final": _rule(
            "precio_final",
            kind="decimal",
        ),
        "plazo": _rule("plazo"),
        "fecha_asignacion_tasa": _rule(
            "fecha_asignacion_tasa",
            kind="date",
        ),
        "fecha_pago": _rule(
            "fecha_pago",
            kind="date",
        ),
        "source_id": _rule("id"),
    },

    # --------------------------------------------------------
    # GS / FORWARD
    # --------------------------------------------------------
    ("GS", "FORWARD"): {
        "tipo": _rule("tipo"),
        "mercado": _rule("mercado"),
        "hora_negociacion": _rule(
            "hora_negociacion",
            kind="time",
        ),
        "fecha_negociacion": _rule(
            "fecha_negociacion",
            kind="date",
        ),
        "comprador_compania": _rule(
            "comprador_compania"
        ),
        "vendedor_compania": _rule(
            "vendedor_compania"
        ),
        "nombre_trader_comp": _rule(
            "nombre_trader_comp"
        ),
        "nombre_trader_vend": _rule(
            "nombre_trader_vend"
        ),
        "par_moneda": _rule("par_moneda"),
        "monto": _rule(
            "monto",
            kind="decimal",
        ),
        "precio_spot": _rule(
            "precio_spot",
            kind="decimal",
        ),
        "precio_final": _rule(
            "precio_final",
            kind="decimal",
        ),
        "plazo": _rule("plazo"),
        "fecha_asignacion_tasa": _rule(
            "fecha_asignacion_tasa",
            kind="date",
        ),
        "fecha_pago": _rule(
            "fecha_pago",
            kind="date",
        ),
        "source_id": _rule("id"),
    },

    # --------------------------------------------------------
    # GS / IRS
    # --------------------------------------------------------
    ("GS", "IRS"): {
        "tipo": _rule("tipo"),
        "mercado": _rule("mercado"),
        "hora_negociacion": _rule(
            "hora_negociacion",
            kind="time",
        ),
        "fecha_negociacion": _rule(
            "fecha_negociacion",
            kind="date",
        ),
        "comprador_compania": _rule(
            "comprador_compania"
        ),
        "vendedor_compania": _rule(
            "vendedor_compania"
        ),
        "nombre_trader_comp": _rule(
            "nombre_trader_comp"
        ),
        "nombre_trader_vend": _rule(
            "nombre_trader_vend"
        ),
        "par_moneda": _rule("par_moneda"),
        "monto": _rule(
            "monto",
            kind="decimal",
        ),
        "plazo": _rule("plazo"),
        "fecha_inicio_primer_flujo_fra": _rule(
            "fecha_inicio_primer_flujo_fra",
            kind="date",
        ),
        "fecha_pago": _rule(
            "fecha_pago",
            kind="date",
        ),
        "tasa_ref_de_interes_m1": _rule(
            "tasa_ref_de_interes_m1"
        ),
        "tasa_ref_de_interes_m2": _rule(
            "tasa_ref_de_interes_m2"
        ),
        "periocidad_m1": _rule(
            "periocidad_m1"
        ),
        "periocidad_m2": _rule(
            "periocidad_m2"
        ),
        "tipo_de_interes_m1": _rule(
            "tipo_de_interes_m1"
        ),
        "margen_m1": _rule("margen_m1"),
        "conteo_dias_m1": _rule(
            "conteo_dias_m1"
        ),
        "tipo_de_interes_m2": _rule(
            "tipo_de_interes_m2"
        ),
        "margen_m2": _rule("margen_m2"),
        "conteo_dias_m2": _rule(
            "conteo_dias_m2"
        ),
        "source_id": _rule("id"),
    },

    # --------------------------------------------------------
    # GS / OPCIONES
    # --------------------------------------------------------
    ("GS", "OPCIONES"): {
        "tipo": _rule("tipo"),
        "mercado": _rule("mercado"),
        "hora_negociacion": _rule(
            "hora_negociacion",
            kind="time",
        ),
        "fecha_negociacion": _rule(
            "fecha_negociacion",
            kind="date",
        ),
        "comprador_compania": _rule(
            "comprador_compania"
        ),
        "vendedor_compania": _rule(
            "vendedor_compania"
        ),
        "nombre_trader_comp": _rule(
            "nombre_trader_comp"
        ),
        "nombre_trader_vend": _rule(
            "nombre_trader_vend"
        ),
        "par_moneda": _rule("par_moneda"),
        "monto": _rule(
            "monto",
            kind="decimal",
        ),
        "precio_spot": _rule(
            "precio_spot",
            kind="decimal",
        ),
        "plazo": _rule("plazo"),
        "fecha_asignacion_tasa": _rule(
            "fecha_asignacion_tasa",
            kind="date",
        ),
        "fecha_pago": _rule(
            "fecha_pago",
            kind="date",
        ),
        "strike_tasa": _rule(
            "strike_tasa",
            kind="decimal",
        ),
        "volatilidad": _rule(
            "volatilidad",
            kind="decimal",
        ),
        "tipo_operacion": _rule(
            "tipo_operacion"
        ),
        "source_id": _rule("id"),
    },

    # --------------------------------------------------------
    # MORGAN STANLEY / FORWARD
    # Las tres variantes FX, Cash y FX Cash llegan al mismo
    # mercado y a la misma tabla.
    # --------------------------------------------------------
    ("MORGAN_STANLEY", "FORWARD"): {
        "record_type": _rule("type"),
        "trade_type": _rule("trade_type"),
        "product_type": _rule(
            "producttype",
            "product_type",
        ),
        "trade_date": _rule(
            "trade_date",
            "trade_calendar_date",
            kind="date",
        ),
        "buy_sell": _rule(
            "buy_sell",
            "ms_buy_sell",
        ),
        "counterparty_code": _rule(
            "customer",
            "short_name",
        ),
        "counterparty_name": _rule(
            "principal_party_name",
            "trading_name",
            "op_party_name",
        ),
        "order_placer": _rule(
            "order_placer"
        ),
        "account_id": _rule(
            "account",
            "taps_account_id",
        ),
        "legal_entity_code": _rule(
            "entity"
        ),
        "bic": _rule(
            "vista_fx_scope_primary_bic"
        ),
        "buy_currency": _rule(
            "ms_buy",
            "buy_currency",
        ),
        "buy_amount": _rule(
            "ms_buy_amt",
            "buy_amount",
            kind="decimal",
        ),
        "sell_currency": _rule(
            "ms_sell",
            "sell_currency",
        ),
        "sell_amount": _rule(
            "ms_sell_amt",
            "sell_amount",
            kind="decimal",
        ),
        "rate": _rule(
            "rate",
            kind="decimal",
        ),
        "fixing_date": _rule(
            "fixing_date",
            "fixing_dt",
            kind="date",
        ),
        "value_date": _rule(
            "value_date",
            "value_calendar_date",
            kind="date",
        ),
        "order_number": _rule(
            "order",
            "order_number",
        ),
        "entered_by": _rule(
            "entered_by"
        ),
        "entry_time": _rule(
            "entry_time"
        ),
        "executed_user": _rule(
            "executeduser"
        ),
        "app": _rule("app"),
    },

    # --------------------------------------------------------
    # MORGAN STANLEY / IRS
    # --------------------------------------------------------
    ("MORGAN_STANLEY", "IRS"): {
        "trade_nm": _rule("trade_nm"),
        "account_name": _rule(
            "account_name"
        ),
        "orig_notional_amt": _rule(
            "orig_notional_amt",
            kind="decimal",
        ),
        "currency_cd": _rule(
            "currency_cd"
        ),
        "pay_direction": _rule(
            "d_pay_direction"
        ),
        "fixed_rate": _rule(
            "fixed_rate",
            kind="decimal",
        ),
        "trade_date": _rule(
            "trade_date",
            kind="date",
        ),
        "effective_date": _rule(
            "effective_dt",
            kind="date",
        ),
        "termination_date": _rule(
            "termination_dt",
            kind="date",
        ),
        "economics_type_cd": _rule(
            "economics_type_cd"
        ),
        "usi_prefix": _rule("usi_prefix"),
        "usi_value": _rule("usi_value"),
        "uti_prefix": _rule("uti_prefix"),
        "uti_value": _rule("uti_value"),
        "legal_entity_cd": _rule(
            "legal_entity_cd"
        ),
    },

    # --------------------------------------------------------
    # MORGAN STANLEY / LISTED
    # --------------------------------------------------------
    ("MORGAN_STANLEY", "LISTED"): {
        "event_date": _rule(
            "eventdate",
            kind="date",
        ),
        "trade_ref": _rule("traderef"),
        "trade_date": _rule(
            "tradedate",
            kind="date",
        ),
        "transaction_type": _rule(
            "transactiontype"
        ),
        "buy_sell": _rule("buysell"),
        "quantity": _rule("quantity"),
        "ccp_cleared_price": _rule(
            "ccpclearedprice",
            kind="decimal",
        ),
        "destination_account": _rule(
            "destinationaccount"
        ),
        "company_code": _rule(
            "companycode"
        ),
        "mic_code": _rule("miccode"),
        "currency": _rule("currency"),
        "asset_description": _rule(
            "asset_dsc"
        ),
        "issue_type": _rule("issuetype"),
        "ex_contract_code": _rule(
            "excontractcode"
        ),
        "contract_date": _rule(
            "contractdate",
            kind="date",
        ),
        "put_or_call": _rule("putorcall"),
        "strike_price": _rule(
            "strikeprice",
            kind="decimal",
        ),
    },

    # --------------------------------------------------------
    # MORGAN STANLEY / OPTIONS
    # --------------------------------------------------------
    ("MORGAN_STANLEY", "OPTIONS"): {
        "trade_id": _rule("trade_id"),
        "trade_calendar_date": _rule(
            "trade_calendar_date",
            kind="date",
        ),
        "expiry_calendar_date": _rule(
            "expiry_calendar_date",
            kind="date",
        ),
        "trade_status": _rule(
            "trade_status"
        ),
        "taps_account_id": _rule(
            "taps_account_id"
        ),
        "ccy1": _rule("ccy1"),
        "ccy1_amt": _rule(
            "ccy1_amt",
            kind="decimal",
        ),
        "ccy2": _rule("ccy2"),
        "ccy2_amt": _rule(
            "ccy2_amt",
            kind="decimal",
        ),
        "uti_prefix": _rule("uti_prefix"),
        "uti_value": _rule("uti_value"),
        "usi_prefix": _rule("usi_prefix"),
        "usi_value": _rule("usi_value"),
        "b2b_trade_role": _rule(
            "b2b_trade_role"
        ),
    },

    # --------------------------------------------------------
    # MORGAN STANLEY / COMMODITIES
    # --------------------------------------------------------
    ("MORGAN_STANLEY", "COMMODITIES"): {
        "trade_ref": _rule("trade_ref"),
        "trade_num": _rule("trade_num"),
        "source_system": _rule(
            "source_system"
        ),
        "buy_sell": _rule("bs"),
        "transaction_date": _rule(
            "transaction_date",
            kind="date",
        ),
        "exposure_account": _rule(
            "exposure_acct"
        ),
        "co_legal_name": _rule(
            "co_legal_name"
        ),
        "short_name": _rule("short_name"),
        "settlement_account": _rule(
            "settlement_acct"
        ),
        "party_id": _rule("party_id"),
        "quantity": _rule("quantity"),
        "commodity_codes": _rule(
            "commod_codes"
        ),
        "product_class": _rule(
            "product_class"
        ),
        "subproduct_name": _rule(
            "subproduct_name"
        ),
        "transaction_term_start_date": _rule(
            "transaction_term_start_dt",
            kind="date",
        ),
        "transaction_term_end_date": _rule(
            "transaction_term_end_dt",
            kind="date",
        ),
        "internal_company_name": _rule(
            "internal_company_name"
        ),
        "usi": _rule("usi"),
        "uti": _rule("uti"),
        "broker_short_name": _rule(
            "broker_short_name"
        ),
        "fixed_price": _rule(
            "fixed_price",
            kind="decimal",
        ),
        "fixed_price_currency": _rule(
            "fixed_price_currency"
        ),
        "strike_price": _rule(
            "strike_price",
            kind="decimal",
        ),
        "strike_price_currency": _rule(
            "strike_price_currency"
        ),
    },
}


# ============================================================
# CONSTRUCCIÓN DE VALORES
# ============================================================


def _find_source_value(
    fields: dict[str, Any],
    aliases: tuple[str, ...],
) -> tuple[bool, Any]:
    """
    Devuelve (True, valor) cuando alguno de los encabezados existe,
    incluso si el valor viene vacío.
    """

    for alias in aliases:
        if alias in fields:
            return True, fields[alias]

    return False, None


def _build_values(
    *,
    entity: str,
    report_type: str,
    fields: dict[str, Any],
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Genera todas las columnas físicas de la entidad.

    Si un campo no aplica al mercado, no viene en el formato o viene vacío,
    se almacena NULL. El mensaje/fila original permanece íntegro en
    original_message y las anomalías técnicas se conservan en
    validation_issues.
    """

    entity_columns = ENTITY_COLUMNS[entity]
    rules = MARKET_RULES[(entity, report_type)]

    values: dict[str, Any] = {}

    for column in entity_columns:
        rule = rules.get(column)

        if rule is None:
            values[column] = None
            continue

        exists, raw_value = _find_source_value(
            fields,
            rule.aliases,
        )

        if not exists or _is_blank(raw_value):
            values[column] = None
            continue

        values[column] = _convert(
            raw_value,
            kind=rule.kind,
            field=column,
            issues=issues,
        )

    return values


# ============================================================
# VALIDACIÓN DE INGESTION
# ============================================================


def _validate_ingestion(
    connection: Connection,
    *,
    ingestion_id: int,
    entity: str,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT entity, processed_at
            FROM closenetting.ingestion_files
            WHERE id = %s
            """,
            (ingestion_id,),
        )

        row = cursor.fetchone()

    if row is None:
        raise RuntimeError(
            f"No existe ingestion_id={ingestion_id}."
        )

    stored_entity = str(row[0]).strip().upper()

    if stored_entity != entity:
        raise RuntimeError(
            f"El ingestion_id={ingestion_id} pertenece a "
            f"'{stored_entity}', no a '{entity}'."
        )

    if row[1] is not None:
        raise RuntimeError(
            f"El ingestion_id={ingestion_id} ya fue finalizado."
        )


def _clear_existing_rows(
    connection: Connection,
    *,
    ingestion_id: int,
    entity: str,
) -> None:
    table_name = ENTITY_TABLES[entity]

    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            DELETE FROM {table_name}
            WHERE ingestion_id = %s
            """,
            (ingestion_id,),
        )


# ============================================================
# INSERT GENÉRICO EN LA TABLA ÚNICA DE LA ENTIDAD
# ============================================================


def _save_table(
    connection: Connection,
    *,
    ingestion_id: int,
    entity: str,
    report_type: str,
    table: ParsedTable,
    classification_issues: tuple[dict[str, Any], ...],
) -> tuple[int, int]:
    table_name = ENTITY_TABLES[entity]
    entity_columns = ENTITY_COLUMNS[entity]

    rows_saved = 0
    rows_with_warnings = 0

    insert_columns = (
        "ingestion_id",
        "market_type",
        "source_sheet_name",
        *entity_columns,
        "original_message",
        "validation_issues",
    )

    column_sql = ", ".join(insert_columns)
    placeholder_sql = ", ".join(
        ["%s"] * len(insert_columns)
    )

    query = f"""
        INSERT INTO {table_name} (
            {column_sql}
        )
        VALUES (
            {placeholder_sql}
        )
    """

    for raw_record in table.raw_records():
        source_cells = raw_record["source_cells"]
        fields = _cells_to_dict(source_cells)

        issues = (
            _parser_issues(raw_record)
            + _classifier_issues(
                classification_issues
            )
        )

        values = _build_values(
            entity=entity,
            report_type=report_type,
            fields=fields,
            issues=issues,
        )

        params = (
            ingestion_id,
            report_type,
            table.name,
            *(
                values[column]
                for column in entity_columns
            ),
            Jsonb(json_safe(source_cells)),
            Jsonb(json_safe(issues)),
        )

        with connection.cursor() as cursor:
            cursor.execute(
                query,
                params,
            )

        rows_saved += 1

        if issues:
            rows_with_warnings += 1

    return rows_saved, rows_with_warnings


# ============================================================
# DESPACHADOR
# ============================================================


def save_report_data(
    *,
    ingestion_id: int,
    report: ParsedReport,
    entity: str,
    content_sha256: str | None,
) -> ReportPersistenceResult:
    """
    Persiste las operaciones de un ingestion_id ya registrado.

    processed_at:
    - se fija solo si TODO el archivo termina correctamente;
    - permanece NULL para partial, no_database_rows y errores.
    """

    entity_key = str(entity).strip().upper()

    rows_saved = 0
    rows_with_warnings = 0
    unsupported_reports: list[str] = []
    unclassified_sheets: list[str] = []
    structure_changes: list[StructureChange] = []

    plans: list[
        tuple[
            str,
            ParsedTable,
            str,
            tuple[dict[str, Any], ...],
        ]
    ] = []

    for sheet_name, table in report.tables.items():
        classification = classify_table(
            entity=entity_key,
            table=table,
        )

        if (
            not classification.recognized
            or not classification.report_type
        ):
            unclassified_sheets.append(
                sheet_name
            )
            continue

        report_type = (
            classification.report_type
            .strip()
            .upper()
        )

        structure_changes.extend(
            _structure_changes(
                entity=entity_key,
                report_type=report_type,
                sheet_name=sheet_name,
                issues=classification.issues,
            )
        )

        route = (
            entity_key,
            report_type,
        )

        if (
            entity_key not in ENTITY_TABLES
            or route not in MARKET_RULES
        ):
            unsupported_reports.append(
                f"{entity_key}/{report_type}"
            )
            continue

        plans.append(
            (
                sheet_name,
                table,
                report_type,
                classification.issues,
            )
        )

    unsupported_reports = list(dict.fromkeys(unsupported_reports))
    unclassified_sheets = list(dict.fromkeys(unclassified_sheets))

    unique_changes: dict[
        tuple[str, str, str, str, str],
        StructureChange,
    ] = {}

    for change in structure_changes:
        key = (
            change.entity,
            change.report_type,
            change.sheet_name,
            change.code,
            change.column,
        )
        unique_changes[key] = change

    structure_changes = list(unique_changes.values())

    complete = (
        not unsupported_reports
        and not unclassified_sheets
    )

    with get_connection() as connection:
        _validate_ingestion(
            connection,
            ingestion_id=ingestion_id,
            entity=entity_key,
        )

        _clear_existing_rows(
            connection,
            ingestion_id=ingestion_id,
            entity=entity_key,
        )

        if not plans:
            update_ingestion_status(
                connection,
                ingestion_id=ingestion_id,
                status="no_database_rows",
                content_sha256=content_sha256,
            )

            return ReportPersistenceResult(
                complete=complete,
                rows_saved=0,
                rows_with_warnings=0,
                ingestion_id=ingestion_id,
                unsupported_reports=tuple(unsupported_reports),
                unclassified_sheets=tuple(unclassified_sheets),
                structure_changes=tuple(structure_changes),
            )

        for (
            _sheet_name,
            table,
            report_type,
            classification_issues,
        ) in plans:
            saved, warnings = _save_table(
                connection,
                ingestion_id=ingestion_id,
                entity=entity_key,
                report_type=report_type,
                table=table,
                classification_issues=classification_issues,
            )

            rows_saved += saved
            rows_with_warnings += warnings

        if rows_saved <= 0:
            update_ingestion_status(
                connection,
                ingestion_id=ingestion_id,
                status="no_database_rows",
                content_sha256=content_sha256,
            )

        elif not complete:
            # Puede haber filas parciales, pero el archivo no terminó.
            update_ingestion_status(
                connection,
                ingestion_id=ingestion_id,
                status="partial",
                content_sha256=content_sha256,
            )

        else:
            ingestion_status = (
                "stored_warning"
                if rows_with_warnings
                else "stored"
            )

            finalize_ingestion(
                connection,
                ingestion_id=ingestion_id,
                status=ingestion_status,
                content_sha256=content_sha256,
            )

    return ReportPersistenceResult(
        complete=complete,
        rows_saved=rows_saved,
        rows_with_warnings=rows_with_warnings,
        ingestion_id=ingestion_id,
        unsupported_reports=tuple(unsupported_reports),
        unclassified_sheets=tuple(unclassified_sheets),
        structure_changes=tuple(structure_changes),
    )

