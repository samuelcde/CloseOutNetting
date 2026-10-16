from __future__ import annotations



import hashlib

import json

import re

import unicodedata



from dataclasses import dataclass

from typing import Any



from .parser import ParsedTable





# ============================================================

# MODELOS

# ============================================================





@dataclass(frozen=True, slots=True)

class ReportProfile:

    """

    Perfil conocido de un tipo de reporte.



    identifying_columns:

        Columnas mínimas que deben existir para confirmar

        que la estructura corresponde al producto.



    expected_columns:

        Estructura conocida actualmente.



        Si aparecen columnas nuevas o desaparecen columnas

        no identificadoras, el reporte puede seguir siendo

        clasificado, pero genera advertencias.

    """



    entity: str

    report_type: str



    sheet_aliases: frozenset[str]



    identifying_columns: frozenset[str]



    expected_columns: frozenset[str]





@dataclass(frozen=True, slots=True)

class ReportClassification:

    """

    Resultado de clasificar una hoja.

    """



    recognized: bool



    entity: str

    source_sheet_name: str



    report_type: str | None



    schema_hash: str



    columns: tuple[dict[str, Any], ...]



    issues: tuple[dict[str, Any], ...]





# ============================================================

# PERFILES CONOCIDOS

# ============================================================





SMBC_FORWARD = ReportProfile(

    entity="SMBC",



    report_type="FORWARD",



    sheet_aliases=frozenset(

        {

            "forward",

        }

    ),



    # Estas son las columnas que usamos para confirmar

    # que realmente estamos viendo un reporte FORWARD

    # conocido de SMBC.

    #

    # No exigimos las 15 para poder clasificar.

    identifying_columns=frozenset(

        {

            "mercado",

            "fecha_negociacion",

            "comprador_compania",

            "vendedor_compania",

            "par_moneda",

            "monto",

            "id",

        }

    ),



    # Estructura conocida actualmente del archivo real SMBC.

    expected_columns=frozenset(

        {

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

            "id",

        }

    ),

)



GS_FORWARD = ReportProfile(

    entity="GS",



    report_type="FORWARD",



    sheet_aliases=frozenset({

        "forward",

    }),



    identifying_columns=frozenset({

        "tipo",

        "fecha_negociacion",

        "comprador_compania",

        "vendedor_compania",

        "par_moneda",

        "monto",

        "id",

    }),



    expected_columns=frozenset({

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

        "fecha_pago",

        "id",

    }),

)







GS_IRS = ReportProfile(
    entity="GS",
    report_type="IRS",
    sheet_aliases=frozenset({"irs"}),

    identifying_columns=frozenset({
        "tipo",
        "fecha_negociacion",
        "comprador_compania",
        "vendedor_compania",
        "par_moneda",
        "monto",
        "id",
    }),

    expected_columns=frozenset({
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
        "plazo",
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
        "id",
    }),
)


GS_OPCIONES = ReportProfile(
    entity="GS",
    report_type="OPCIONES",
    sheet_aliases=frozenset({"opciones"}),

    identifying_columns=frozenset({
        "tipo",
        "fecha_negociacion",
        "comprador_compania",
        "vendedor_compania",
        "par_moneda",
        "monto",
        "id",
    }),

    expected_columns=frozenset({
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
        "plazo",
        "fecha_asignacion_tasa",
        "fecha_pago",
        "strike_tasa",
        "volatilidad",
        "tipo_operacion",
        "id",
    }),
)



# ============================================================
# MORGAN STANLEY
# ============================================================

# FX, Cash y FX Cash son variantes estructurales de la familia
# de operaciones FX / NDF / Forward. Se mantienen perfiles
# separados para no generar advertencias falsas por diferencias
# de columnas entre formatos.

MORGAN_STANLEY_FORWARD_FX = ReportProfile(
    entity="MORGAN_STANLEY",
    report_type="FORWARD",
    sheet_aliases=frozenset({"fx"}),

    identifying_columns=frozenset({
        "trade_type",
        "producttype",
        "trade_date",
        "buy_sell",
        "ms_buy",
        "ms_buy_amt",
        "ms_sell",
        "ms_sell_amt",
        "rate",
        "order",
    }),

    expected_columns=frozenset({
        "order",
        "type",
        "trade_type",
        "producttype",
        "trade_date",
        "buy_sell",
        "customer",
        "order_placer",
        "principal_party_name",
        "account",
        "entity",
        "vista_fx_scope_primary_bic",
        "ms_buy",
        "ms_buy_amt",
        "ms_sell",
        "ms_sell_amt",
        "rate",
        "fixing_date",
        "value_date",
        "entered_by",
        "entry_time",
        "executeduser",
        "app",
    }),
)


MORGAN_STANLEY_FORWARD_CASH = ReportProfile(
    entity="MORGAN_STANLEY",
    report_type="FORWARD",
    sheet_aliases=frozenset({"cash"}),

    identifying_columns=frozenset({
        "trade_type",
        "producttype",
        "trade_date",
        "buy_sell",
        "ms_buy",
        "ms_buy_amt",
        "ms_sell",
        "ms_sell_amt",
        "rate",
        "order",
    }),

    expected_columns=frozenset({
        "trade_type",
        "producttype",
        "trade_date",
        "buy_sell",
        "customer",
        "trading_name",
        "account",
        "ms_buy",
        "ms_buy_amt",
        "ms_sell",
        "ms_sell_amt",
        "rate",
        "fixing_date",
        "value_date",
        "order",
        "entered_by",
        "entry_time",
        "executeduser",
    }),
)


MORGAN_STANLEY_FORWARD_FX_CASH = ReportProfile(
    entity="MORGAN_STANLEY",
    report_type="FORWARD",
    sheet_aliases=frozenset({"fx_cash"}),

    identifying_columns=frozenset({
        "trade_type",
        "product_type",
        "trade_calendar_date",
        "ms_buy_sell",
        "buy_currency",
        "buy_amount",
        "sell_currency",
        "sell_amount",
        "rate",
        "order_number",
    }),

    expected_columns=frozenset({
        "trade_type",
        "product_type",
        "trade_calendar_date",
        "ms_buy_sell",
        "short_name",
        "op_party_name",
        "taps_account_id",
        "buy_currency",
        "buy_amount",
        "sell_currency",
        "sell_amount",
        "rate",
        "fixing_dt",
        "value_calendar_date",
        "order_number",
    }),
)


MORGAN_STANLEY_IRS = ReportProfile(
    entity="MORGAN_STANLEY",
    report_type="IRS",
    sheet_aliases=frozenset({"irs"}),

    identifying_columns=frozenset({
        "trade_nm",
        "account_name",
        "orig_notional_amt",
        "currency_cd",
        "trade_date",
        "effective_dt",
        "termination_dt",
        "economics_type_cd",
    }),

    expected_columns=frozenset({
        "trade_nm",
        "account_name",
        "orig_notional_amt",
        "currency_cd",
        "d_pay_direction",
        "fixed_rate",
        "trade_date",
        "effective_dt",
        "termination_dt",
        "economics_type_cd",
        "usi_prefix",
        "usi_value",
        "uti_prefix",
        "uti_value",
        "legal_entity_cd",
    }),
)


MORGAN_STANLEY_LISTED_LD = ReportProfile(
    entity="MORGAN_STANLEY",
    report_type="LISTED",
    sheet_aliases=frozenset({"ld"}),

    identifying_columns=frozenset({
        "eventdate",
        "traderef",
        "tradedate",
        "transactiontype",
        "buysell",
        "quantity",
        "ccpclearedprice",
        "issuetype",
        "contractdate",
    }),

    expected_columns=frozenset({
        "eventdate",
        "traderef",
        "tradedate",
        "transactiontype",
        "buysell",
        "quantity",
        "ccpclearedprice",
        "destinationaccount",
        "companycode",
        "miccode",
        "currency",
        "asset_dsc",
        "issuetype",
        "excontractcode",
        "contractdate",
    }),
)


MORGAN_STANLEY_LISTED = ReportProfile(
    entity="MORGAN_STANLEY",
    report_type="LISTED",
    sheet_aliases=frozenset({"listed"}),

    identifying_columns=frozenset({
        "eventdate",
        "traderef",
        "tradedate",
        "transactiontype",
        "buysell",
        "quantity",
        "ccpclearedprice",
        "issuetype",
        "contractdate",
    }),

    expected_columns=frozenset({
        "eventdate",
        "traderef",
        "tradedate",
        "transactiontype",
        "buysell",
        "quantity",
        "ccpclearedprice",
        "destinationaccount",
        "companycode",
        "miccode",
        "currency",
        "asset_dsc",
        "issuetype",
        "excontractcode",
        "contractdate",
        "putorcall",
        "strikeprice",
    }),
)


MORGAN_STANLEY_OPTIONS = ReportProfile(
    entity="MORGAN_STANLEY",
    report_type="OPTIONS",
    sheet_aliases=frozenset({
        "option",
        "options",
    }),

    identifying_columns=frozenset({
        "trade_id",
        "trade_calendar_date",
        "expiry_calendar_date",
        "trade_status",
        "taps_account_id",
        "ccy1",
        "ccy1_amt",
        "ccy2",
        "ccy2_amt",
    }),

    expected_columns=frozenset({
        "trade_id",
        "trade_calendar_date",
        "expiry_calendar_date",
        "trade_status",
        "taps_account_id",
        "ccy1",
        "ccy1_amt",
        "ccy2",
        "ccy2_amt",
        "uti_prefix",
        "uti_value",
        "usi_prefix",
        "usi_value",
        "b2b_trade_role",
    }),
)


MORGAN_STANLEY_COMMODITIES = ReportProfile(
    entity="MORGAN_STANLEY",
    report_type="COMMODITIES",
    sheet_aliases=frozenset({"commodities"}),

    identifying_columns=frozenset({
        "trade_ref",
        "trade_num",
        "source_system",
        "transaction_date",
        "product_class",
        "subproduct_name",
        "strike_price",
    }),

    expected_columns=frozenset({
        "trade_ref",
        "trade_num",
        "source_system",
        "bs",
        "transaction_date",
        "exposure_acct",
        "co_legal_name",
        "short_name",
        "settlement_acct",
        "party_id",
        "quantity",
        "commod_codes",
        "product_class",
        "subproduct_name",
        "transaction_term_start_dt",
        "transaction_term_end_dt",
        "internal_company_name",
        "usi",
        "uti",
        "broker_short_name",
        "fixed_price",
        "fixed_price_currency",
        "strike_price",
        "strike_price_currency",
    }),
)

REPORT_PROFILES: tuple[
    ReportProfile,
    ...,
] = (
    SMBC_FORWARD,
    GS_FORWARD,
    GS_IRS,
    GS_OPCIONES,

    MORGAN_STANLEY_FORWARD_FX,
    MORGAN_STANLEY_FORWARD_CASH,
    MORGAN_STANLEY_FORWARD_FX_CASH,
    MORGAN_STANLEY_IRS,
    MORGAN_STANLEY_LISTED_LD,
    MORGAN_STANLEY_LISTED,
    MORGAN_STANLEY_OPTIONS,
    MORGAN_STANLEY_COMMODITIES,
)





# ============================================================

# NORMALIZACIÓN

# ============================================================





def normalize_name(

    value: Any,

) -> str:

    """

    Normaliza nombres de hojas y encabezados.



    Ejemplo:



        FECHA ASIGNACIÓN TASA

            ↓

        fecha_asignacion_tasa

    """



    if value is None:

        return ""



    text = unicodedata.normalize(

        "NFKD",

        str(value).strip(),

    )



    text = "".join(

        character

        for character in text

        if not unicodedata.combining(

            character

        )

    )



    text = text.lower()



    text = re.sub(

        r"[^a-z0-9]+",

        "_",

        text,

    )



    return text.strip(

        "_"

    )





def _normalize_column_name(

    value: Any,

    ordinal: int,

) -> str:

    """

    Genera un nombre estable incluso para columnas

    que no tienen encabezado.



    Una columna sin nombre NO se elimina.

    """



    normalized = normalize_name(

        value

    )



    if not normalized:

        normalized = (

            f"column_{ordinal}"

        )



    if normalized[0].isdigit():

        normalized = (

            f"col_{normalized}"

        )



    return normalized





# ============================================================

# DESCRIPCIÓN DE ESTRUCTURA

# ============================================================





def build_columns_descriptor(

    table: ParsedTable,

) -> tuple[dict[str, Any], ...]:

    """

    Convierte los encabezados originales del parser

    en una representación estable para report_schemas.



    También conserva encabezados duplicados.

    """



    result: list[

        dict[str, Any]

    ] = []



    counters: dict[

        str,

        int,

    ] = {}



    for ordinal, source_header in enumerate(

        table.source_headers,

        start=1,

    ):



        base_name = (

            _normalize_column_name(

                source_header,

                ordinal,

            )

        )



        count = counters.get(

            base_name,

            0,

        )



        counters[

            base_name

        ] = count + 1



        normalized_name = (

            base_name

            if count == 0

            else f"{base_name}_{count + 1}"

        )



        result.append(

            {

                "ordinal": ordinal,

                "source_header": (

                    None

                    if source_header is None

                    else str(source_header)

                ),

                "normalized_name": (

                    normalized_name

                ),

            }

        )



    return tuple(

        result

    )





# ============================================================

# HASH DEL ESQUEMA

# ============================================================





def calculate_schema_hash(

    columns: tuple[

        dict[str, Any],

        ...,

    ],

) -> str:

    """

    Hash estable de la estructura física.



    Se incluye:

        - ordinal

        - nombre normalizado



    Por tanto, un cambio de columnas o de orden produce

    un schema_hash distinto.



    El contenido de las operaciones NO participa.

    """



    payload = [

        {

            "ordinal": column[

                "ordinal"

            ],

            "normalized_name": column[

                "normalized_name"

            ],

        }

        for column in columns

    ]



    canonical = json.dumps(

        payload,

        ensure_ascii=False,

        sort_keys=True,

        separators=(

            ",",

            ":",

        ),

    )



    return hashlib.sha256(

        canonical.encode(

            "utf-8"

        )

    ).hexdigest()





# ============================================================

# COMPARACIÓN CON PERFIL

# ============================================================





def _match_profile(

    *,

    entity: str,

    sheet_name: str,

    columns: tuple[

        dict[str, Any],

        ...,

    ],

    profile: ReportProfile,

) -> bool:



    if entity != profile.entity:

        return False



    normalized_sheet = normalize_name(

        sheet_name

    )



    if (

        normalized_sheet

        not in profile.sheet_aliases

    ):

        return False



    actual_columns = {

        column["normalized_name"]

        for column in columns

    }



    # El nombre de la hoja NO basta.

    #

    # También deben existir las columnas que identifican

    # estructuralmente este producto.

    return (

        profile.identifying_columns

        <= actual_columns

    )





# ============================================================

# ADVERTENCIAS DE ESTRUCTURA

# ============================================================





def _build_structure_issues(

    *,

    columns: tuple[

        dict[str, Any],

        ...,

    ],

    profile: ReportProfile,

) -> tuple[

    dict[str, Any],

    ...,

]:



    issues: list[

        dict[str, Any]

    ] = []



    actual_columns = {

        column["normalized_name"]

        for column in columns

    }



    missing_columns = sorted(

        profile.expected_columns

        - actual_columns

    )



    unexpected_columns = sorted(

        actual_columns

        - profile.expected_columns

    )



    for column in missing_columns:



        issues.append(

            {

                "code": (

                    "MISSING_EXPECTED_COLUMN"

                ),

                "column": column,

            }

        )



    for column in unexpected_columns:



        issues.append(

            {

                "code": (

                    "UNEXPECTED_COLUMN"

                ),

                "column": column,

            }

        )



    return tuple(

        issues

    )





# ============================================================

# CLASIFICADOR PRINCIPAL

# ============================================================





def classify_table(

    *,

    entity: str,

    table: ParsedTable,

) -> ReportClassification:

    """

    Intenta identificar el producto correspondiente

    a una hoja ya interpretada por parser.py.



    Principio fundamental:



        No estar seguro

            !=

        inventar una clasificación



    Si no existe coincidencia suficientemente clara,

    report_type queda en None.

    """



    entity_key = (

        str(entity)

        .strip()

        .upper()

    )



    columns = (

        build_columns_descriptor(

            table

        )

    )



    schema_hash = (

        calculate_schema_hash(

            columns

        )

    )



    matching_profiles = [

        profile

        for profile in REPORT_PROFILES

        if _match_profile(

            entity=entity_key,

            sheet_name=table.name,

            columns=columns,

            profile=profile,

        )

    ]



    # --------------------------------------------------------

    # NINGÚN PERFIL

    # --------------------------------------------------------



    if not matching_profiles:



        return ReportClassification(

            recognized=False,



            entity=entity_key,



            source_sheet_name=(

                table.name

            ),



            report_type=None,



            schema_hash=schema_hash,



            columns=columns,



            issues=(

                {

                    "code": (

                        "UNCLASSIFIED_REPORT"

                    ),

                },

            ),

        )



    # --------------------------------------------------------

    # MÁS DE UN PERFIL

    #

    # Nunca escogemos uno arbitrariamente.

    # --------------------------------------------------------



    if len(

        matching_profiles

    ) > 1:



        return ReportClassification(

            recognized=False,



            entity=entity_key,



            source_sheet_name=(

                table.name

            ),



            report_type=None,



            schema_hash=schema_hash,



            columns=columns,



            issues=(

                {

                    "code": (

                        "AMBIGUOUS_REPORT_PROFILE"

                    ),

                    "candidates": [

                        profile.report_type

                        for profile

                        in matching_profiles

                    ],

                },

            ),

        )



    # --------------------------------------------------------

    # PERFIL IDENTIFICADO

    # --------------------------------------------------------



    profile = (

        matching_profiles[0]

    )



    issues = (

        _build_structure_issues(

            columns=columns,

            profile=profile,

        )

    )



    return ReportClassification(

        recognized=True,



        entity=entity_key,



        source_sheet_name=(

            table.name

        ),



        report_type=(

            profile.report_type

        ),



        schema_hash=schema_hash,



        columns=columns,



        issues=issues,

    )