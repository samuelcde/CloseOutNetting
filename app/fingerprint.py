from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import pandas as pd


def _normalize_number(value: int | float | Decimal) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"

    if isinstance(value, int):
        return str(value)

    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)
        decimal_value = Decimal(str(value))
    else:
        decimal_value = value

    if decimal_value == 0:
        return "0"

    return format(decimal_value.normalize(), "f")


def _normalize_value(value: Any) -> Any:
    if value is None:
        return None

    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass

    if hasattr(value, "item") and not isinstance(value, (str, bytes, bytearray)):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass

    if isinstance(value, bool):
        return {"type": "bool", "value": value}

    if isinstance(value, (int, float, Decimal)):
        return {"type": "number", "value": _normalize_number(value)}

    if isinstance(value, datetime):
        return {"type": "datetime", "value": value.isoformat()}

    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}

    if isinstance(value, time):
        return {"type": "time", "value": value.isoformat()}

    if isinstance(value, bytes):
        return {"type": "bytes", "value": value.hex()}

    if isinstance(value, str):
        return {"type": "string", "value": value.replace("\r\n", "\n")}

    return {"type": type(value).__name__, "value": str(value)}


def _iter_report_tables(report: Any):
    tables = getattr(report, "tables", None)

    if tables is None:
        raise TypeError("El reporte parseado no contiene el atributo 'tables'.")

    if isinstance(tables, Mapping):
        yield from tables.items()
        return

    if isinstance(tables, (str, bytes, bytearray)):
        raise TypeError("'report.tables' no puede ser una cadena.")

    try:
        iterator = iter(tables)
    except TypeError as exc:
        raise TypeError("'report.tables' no es iterable.") from exc

    for index, table in enumerate(iterator):
        name = getattr(table, "name", f"table_{index + 1}")
        yield name, table


def _extract_dataframe(table: Any) -> pd.DataFrame | None:
    if isinstance(table, pd.DataFrame):
        return table

    if isinstance(table, Mapping):
        for key in ("dataframe", "frame", "data"):
            value = table.get(key)
            if isinstance(value, pd.DataFrame):
                return value

    for attribute in ("dataframe", "frame", "data"):
        value = getattr(table, attribute, None)
        if isinstance(value, pd.DataFrame):
            return value

    return None


def _extract_records(table: Any) -> tuple[list[Any], list[list[Any]]] | None:
    records_method = getattr(table, "records", None)

    if callable(records_method):
        records = records_method()
    elif isinstance(table, list):
        records = table
    elif isinstance(table, Mapping) and isinstance(table.get("records"), list):
        records = table["records"]
    else:
        return None

    if not records:
        return [], []

    if not all(isinstance(record, Mapping) for record in records):
        return None

    explicit_columns = getattr(table, "columns", None)
    if explicit_columns is None:
        explicit_columns = getattr(table, "headers", None)

    if explicit_columns is not None:
        columns = list(explicit_columns)
    else:
        columns = list(records[0].keys())

    rows = [[record.get(column) for column in columns] for record in records]
    return columns, rows


def _canonicalize_table(name: Any, table: Any) -> dict[str, Any]:
    dataframe = _extract_dataframe(table)

    if dataframe is not None:
        columns = dataframe.columns.tolist()
        rows = list(dataframe.itertuples(index=False, name=None))
    else:
        extracted = _extract_records(table)
        if extracted is None:
            raise TypeError(
                "No fue posible obtener los datos de la tabla "
                f"'{name}'. Tipo recibido: {type(table).__name__}."
            )
        columns, rows = extracted

    return {
        "name": str(name),
        "columns": [_normalize_value(column) for column in columns],
        "rows": [
            [_normalize_value(value) for value in row]
            for row in rows
        ],
    }


def build_report_content_sha256(report: Any) -> str:
    """
    Calcula una huella lógica estable del contenido parseado.

    Soporta report.tables como:
        - diccionario nombre -> tabla;
        - iterable de tablas.

    Y cada tabla puede ser:
        - pandas.DataFrame;
        - objeto con .dataframe, .frame o .data;
        - objeto con records();
        - lista de diccionarios.

    La huella incluye nombre de tabla/hoja, columnas y filas en su orden
    original, pero no metadatos internos del archivo XLS/XLSX.
    """

    canonical_tables = [
        _canonicalize_table(name, table)
        for name, table in _iter_report_tables(report)
    ]

    canonical_payload = json.dumps(
        canonical_tables,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    return hashlib.sha256(canonical_payload).hexdigest()
