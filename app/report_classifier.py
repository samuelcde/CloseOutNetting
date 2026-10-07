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


REPORT_PROFILES: tuple[
    ReportProfile,
    ...,
] = (
    SMBC_FORWARD,
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