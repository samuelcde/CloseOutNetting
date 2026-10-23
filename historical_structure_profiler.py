from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable


# ============================================================
# CONSTANTES
# ============================================================

DEFAULT_MIN_YEAR = 2018
DEFAULT_MAX_YEAR = 2026
DEFAULT_SAMPLE_ROWS = 1000

FINALIZED_TYPES = {
    "TEXT",
    "BIGINT",
    "NUMERIC",
    "DATE",
    "TIMESTAMP",
    "TIME",
    "BOOLEAN",
}

IDENTIFIER_HINTS = (
    "id",
    "_id",
    "code",
    "_code",
    "ref",
    "_ref",
    "reference",
    "account",
    "acct",
    "ticket",
    "order",
    "usi",
    "uti",
    "bic",
    "isin",
    "cusip",
    "sedol",
    "currency",
    "ccy",
    "book",
    "trader",
    "counterparty",
    "party",
    "entity",
    "market",
)

DATE_HINTS = (
    "date",
    "fecha",
    "_dt",
    "maturity",
    "termination",
    "effective",
    "expiry",
    "expire",
    "settle",
)

TIME_HINTS = (
    "time",
    "hora",
    "timestamp",
)

NUMERIC_HINTS = (
    "amount",
    "amt",
    "monto",
    "notional",
    "quantity",
    "qty",
    "rate",
    "tasa",
    "price",
    "precio",
    "strike",
    "spread",
    "margin",
    "margen",
    "volatility",
    "volatilidad",
)

BOOLEAN_TEXTS = {
    "true",
    "false",
    "yes",
    "no",
    "y",
    "n",
    "si",
    "sí",
}


# ============================================================
# MODELOS
# ============================================================


@dataclass(slots=True)
class InventoryRecord:
    entity_hint: str
    market_hint: str
    structure_hash: str
    source_year: int | None
    relative_path: str
    filename: str
    extension: str
    sheet_name: str
    header_row: int
    data_rows_estimate: int
    column_count: int
    open_method: str
    headers: list[str]
    normalized_headers: list[str]


@dataclass(slots=True)
class ColumnStats:
    position: int
    original_header: str
    normalized_header: str
    sampled_rows: int = 0
    blank_count: int = 0
    non_empty_count: int = 0
    integer_count: int = 0
    numeric_count: int = 0
    date_count: int = 0
    datetime_count: int = 0
    time_count: int = 0
    boolean_count: int = 0
    text_count: int = 0
    max_text_length: int = 0
    distinct_tokens: set[str] | None = None

    def __post_init__(self) -> None:
        if self.distinct_tokens is None:
            self.distinct_tokens = set()


# ============================================================
# UTILIDADES
# ============================================================


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or str(value).strip() == "":
            return default
        return int(float(str(value).strip()))
    except Exception:
        return default


def parse_year(value: Any) -> int | None:
    parsed = safe_int(value, 0)
    return parsed if parsed > 0 else None


def normalize_header(value: Any) -> str:
    if value is None:
        return ""

    text = str(value).strip().lower()
    text = (
        text.replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
        .replace("ü", "u")
        .replace("ñ", "n")
    )
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^a-z0-9_]+", "", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text


def parse_json_list(value: str) -> list[str]:
    if not value:
        return []

    try:
        parsed = json.loads(value)
    except Exception:
        return []

    if not isinstance(parsed, list):
        return []

    return ["" if item is None else str(item) for item in parsed]


def choose_pg_identifier(value: str, fallback: str) -> str:
    normalized = normalize_header(value)

    if not normalized:
        normalized = fallback

    if normalized[0].isdigit():
        normalized = f"col_{normalized}"

    # PostgreSQL limita identificadores a 63 bytes.
    return normalized[:60]


def stable_token(value: Any) -> str:
    """
    Token no reversible para contar distintos sin escribir valores reales
    en los archivos de salida.
    """

    import hashlib

    raw = repr(value).encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:16]


def is_blank(value: Any) -> bool:
    if value is None:
        return True

    if isinstance(value, str) and value.strip() == "":
        return True

    return False


def is_identifier_like(header: str) -> bool:
    h = f"_{normalize_header(header)}_"

    # No forzar TEXT para campos inequívocamente cuantitativos/temporales.
    if any(token in h for token in NUMERIC_HINTS):
        return False

    if any(token in h for token in DATE_HINTS):
        return False

    if any(token in h for token in TIME_HINTS):
        return False

    for token in IDENTIFIER_HINTS:
        t = normalize_header(token)
        if (
            h == f"_{t}_"
            or h.startswith(f"_{t}_")
            or h.endswith(f"_{t}_")
            or f"_{t}_" in h
        ):
            return True

    return False


# ============================================================
# PASSWORDS
# ============================================================


def load_report_passwords() -> dict[str, tuple[str, ...]]:
    """
    Lee REPORT_PASSWORDS del .env.

    Ejemplo:
        REPORT_PASSWORDS={"GS":["clave1"],"CITI":["clave2"]}

    Las contraseñas nunca se escriben en los resultados.
    """

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    raw = os.getenv("REPORT_PASSWORDS", "").strip()

    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
    except Exception:
        return {}

    if not isinstance(parsed, dict):
        return {}

    result: dict[str, tuple[str, ...]] = {}

    for key, values in parsed.items():
        entity = str(key).strip().upper()

        if not isinstance(values, list):
            continue

        clean: list[str] = []

        for item in values:
            password = str(item).strip()

            if password and password not in clean:
                clean.append(password)

        result[entity] = tuple(clean)

    return result


PASSWORD_ALIASES = {
    "MORGAN_STANLEY": (
        "MORGAN_STANLEY",
        "MORGAN",
        "MS",
    ),
    "GS": (
        "GS",
        "GOLDMAN_SACHS",
        "GOLDMAN",
    ),
    "STANDARD": (
        "STANDARD",
        "STANDARD_CHARTERED",
        "SCB",
    ),
    "FICC": (
        "FICC",
        "BAML",
        "BANK_OF_AMERICA",
        "BOFA",
    ),
    "CITI": (
        "CITI",
        "CITIBANK",
    ),
    "SMBC": (
        "SMBC",
        "SUMITOMO",
    ),
    "BBVA": (
        "BBVA",
    ),
    "SET-ICAP": (
        "SET-ICAP",
        "SET_ICAP",
    ),
}


def passwords_for_entity(
    passwords: dict[str, tuple[str, ...]],
    entity: str,
) -> tuple[str, ...]:
    keys = PASSWORD_ALIASES.get(
        entity.strip().upper(),
        (entity.strip().upper(),),
    )

    result: list[str] = []

    for key in keys:
        for password in passwords.get(key, ()):
            if password not in result:
                result.append(password)

    return tuple(result)


def decrypt_office(
    path: Path,
    password_candidates: tuple[str, ...],
) -> bytes:
    try:
        import msoffcrypto
    except ImportError as exc:
        raise RuntimeError(
            "Falta msoffcrypto-tool. "
            "Ejecute: python -m pip install msoffcrypto-tool"
        ) from exc

    if not password_candidates:
        raise RuntimeError(
            "El archivo requiere contraseña y no hay una "
            "configurada para esta entidad en REPORT_PASSWORDS."
        )

    last_error: Exception | None = None

    for password in password_candidates:
        try:
            with path.open("rb") as source:
                office = msoffcrypto.OfficeFile(source)
                office.load_key(password=password)

                output = io.BytesIO()
                office.decrypt(output)

                return output.getvalue()

        except Exception as exc:
            last_error = exc

    raise RuntimeError(
        "Ninguna contraseña configurada pudo abrir el archivo."
    ) from last_error


# ============================================================
# INVENTARIO / MUESTRAS
# ============================================================


def read_inventory(
    inventory_csv: Path,
    *,
    min_year: int,
    max_year: int,
    include_undated: bool,
) -> list[InventoryRecord]:
    records: list[InventoryRecord] = []

    with inventory_csv.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        reader = csv.DictReader(handle)

        required = {
            "source_year",
            "relative_path",
            "filename",
            "extension",
            "entity_hint",
            "market_hint",
            "sheet_name",
            "header_row",
            "data_rows_estimate",
            "column_count",
            "structure_hash",
            "headers",
            "normalized_headers",
            "scan_status",
            "open_method",
        }

        missing = required - set(reader.fieldnames or [])

        if missing:
            raise RuntimeError(
                "El inventario no tiene las columnas requeridas: "
                + ", ".join(sorted(missing))
            )

        for row in reader:
            if row.get("scan_status") != "OK":
                continue

            structure = str(
                row.get("structure_hash") or ""
            ).strip()

            if not structure:
                continue

            source_year = parse_year(row.get("source_year"))

            if source_year is None and not include_undated:
                continue

            if (
                source_year is not None
                and not (min_year <= source_year <= max_year)
            ):
                continue

            header_row = safe_int(row.get("header_row"), 0)

            if header_row <= 0:
                continue

            records.append(
                InventoryRecord(
                    entity_hint=str(
                        row.get("entity_hint") or "DESCONOCIDA"
                    ).strip(),
                    market_hint=str(
                        row.get("market_hint") or "DESCONOCIDO"
                    ).strip(),
                    structure_hash=structure,
                    source_year=source_year,
                    relative_path=str(
                        row.get("relative_path") or ""
                    ).strip(),
                    filename=str(
                        row.get("filename") or ""
                    ).strip(),
                    extension=str(
                        row.get("extension") or ""
                    ).strip().lower(),
                    sheet_name=str(
                        row.get("sheet_name") or ""
                    ),
                    header_row=header_row,
                    data_rows_estimate=safe_int(
                        row.get("data_rows_estimate"),
                        0,
                    ),
                    column_count=safe_int(
                        row.get("column_count"),
                        0,
                    ),
                    open_method=str(
                        row.get("open_method") or ""
                    ).strip(),
                    headers=parse_json_list(
                        str(row.get("headers") or "")
                    ),
                    normalized_headers=parse_json_list(
                        str(row.get("normalized_headers") or "")
                    ),
                )
            )

    return records


def select_representatives(
    records: list[InventoryRecord],
    files_per_structure: int,
) -> list[InventoryRecord]:
    groups: dict[
        tuple[str, str, str],
        list[InventoryRecord],
    ] = defaultdict(list)

    for record in records:
        key = (
            record.entity_hint,
            record.market_hint,
            record.structure_hash,
        )
        groups[key].append(record)

    selected: list[InventoryRecord] = []

    for group in groups.values():
        # Preferir archivos con más filas y, en empate, los más recientes.
        group.sort(
            key=lambda item: (
                item.data_rows_estimate,
                item.source_year or 0,
                item.relative_path,
            ),
            reverse=True,
        )

        selected.extend(
            group[: max(1, files_per_structure)]
        )

    selected.sort(
        key=lambda item: (
            item.entity_hint,
            item.market_hint,
            item.structure_hash,
            -(item.source_year or 0),
            item.relative_path,
        )
    )

    return selected


# ============================================================
# LECTURA DE ARCHIVOS
# ============================================================


class HTMLTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._in_cell = False

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        tag = tag.lower()

        if tag == "table":
            self._table = []

        elif tag == "tr" and self._table is not None:
            self._row = []

        elif (
            tag in {"td", "th"}
            and self._row is not None
        ):
            self._cell = []
            self._in_cell = True

    def handle_data(self, data: str) -> None:
        if self._in_cell and self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        if (
            tag in {"td", "th"}
            and self._row is not None
        ):
            value = "".join(self._cell or [])
            value = re.sub(r"\s+", " ", value).strip()
            self._row.append(value)
            self._cell = None
            self._in_cell = False

        elif tag == "tr" and self._table is not None:
            if self._row is not None and any(
                str(value).strip()
                for value in self._row
            ):
                self._table.append(self._row)

            self._row = None

        elif tag == "table":
            if self._table:
                self.tables.append(self._table)

            self._table = None


def decode_html(data: bytes) -> str:
    for encoding in (
        "utf-8-sig",
        "utf-8",
        "cp1252",
        "latin-1",
    ):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue

    raise RuntimeError(
        "No fue posible decodificar el archivo HTML."
    )


def read_html_rows(
    data: bytes,
    record: InventoryRecord,
    sample_rows: int,
) -> list[list[Any]]:
    parser = HTMLTableParser()
    parser.feed(decode_html(data))

    match = re.fullmatch(
        r"HTML_TABLE_(\d+)",
        record.sheet_name.strip(),
        re.IGNORECASE,
    )

    table_index = (
        int(match.group(1)) - 1
        if match
        else 0
    )

    if (
        table_index < 0
        or table_index >= len(parser.tables)
    ):
        raise RuntimeError(
            f"No se encontró la tabla HTML "
            f"'{record.sheet_name}'."
        )

    table = parser.tables[table_index]

    # header_row es 1-based; la primera fila de datos tiene
    # índice header_row en la lista Python.
    start = record.header_row
    end = start + sample_rows

    return table[start:end]


def read_openpyxl_rows(
    source: Path | io.BytesIO,
    record: InventoryRecord,
    sample_rows: int,
) -> list[list[Any]]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise RuntimeError(
            "Falta openpyxl."
        ) from exc

    workbook = load_workbook(
        source,
        read_only=True,
        data_only=True,
    )

    try:
        if record.sheet_name not in workbook.sheetnames:
            raise RuntimeError(
                f"No existe la hoja '{record.sheet_name}'."
            )

        worksheet = workbook[record.sheet_name]
        rows: list[list[Any]] = []

        for row in worksheet.iter_rows(
            min_row=record.header_row + 1,
            max_row=record.header_row + sample_rows,
            values_only=True,
        ):
            rows.append(list(row))

        return rows

    finally:
        workbook.close()


def xlrd_cell_value(
    workbook: Any,
    cell: Any,
) -> Any:
    try:
        import xlrd
    except ImportError:
        return cell.value

    ctype = cell.ctype

    if ctype in {
        xlrd.XL_CELL_EMPTY,
        xlrd.XL_CELL_BLANK,
    }:
        return None

    if ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(cell.value)

    if ctype == xlrd.XL_CELL_DATE:
        try:
            return xlrd.xldate_as_datetime(
                cell.value,
                workbook.datemode,
            )
        except Exception:
            return cell.value

    if ctype == xlrd.XL_CELL_ERROR:
        return None

    return cell.value


def read_xlrd_rows(
    source: Path | bytes,
    record: InventoryRecord,
    sample_rows: int,
) -> list[list[Any]]:
    try:
        import xlrd
    except ImportError as exc:
        raise RuntimeError(
            "Falta xlrd. "
            "Ejecute: python -m pip install xlrd"
        ) from exc

    if isinstance(source, Path):
        workbook = xlrd.open_workbook(
            source,
            on_demand=True,
        )
    else:
        workbook = xlrd.open_workbook(
            file_contents=source,
            on_demand=True,
        )

    try:
        if record.sheet_name not in workbook.sheet_names():
            raise RuntimeError(
                f"No existe la hoja '{record.sheet_name}'."
            )

        sheet = workbook.sheet_by_name(
            record.sheet_name
        )

        start = record.header_row
        end = min(
            sheet.nrows,
            start + sample_rows,
        )

        rows: list[list[Any]] = []

        for row_index in range(start, end):
            rows.append(
                [
                    xlrd_cell_value(
                        workbook,
                        sheet.cell(
                            row_index,
                            column_index,
                        ),
                    )
                    for column_index in range(
                        sheet.ncols
                    )
                ]
            )

        return rows

    finally:
        workbook.release_resources()


def read_csv_rows(
    path: Path,
    record: InventoryRecord,
    sample_rows: int,
) -> list[list[Any]]:
    method = record.open_method.upper()

    if method.startswith("CSV_"):
        encoding = record.open_method[4:]
    else:
        encoding = "utf-8-sig"

    encodings = [
        encoding,
        "utf-8-sig",
        "utf-8",
        "cp1252",
        "latin-1",
    ]

    last_error: Exception | None = None

    for current_encoding in dict.fromkeys(encodings):
        try:
            with path.open(
                "r",
                encoding=current_encoding,
                newline="",
            ) as handle:
                sample = handle.read(64 * 1024)
                handle.seek(0)

                try:
                    dialect = csv.Sniffer().sniff(
                        sample,
                        delimiters=",;\t|",
                    )
                except csv.Error:
                    dialect = csv.excel

                reader = csv.reader(
                    handle,
                    dialect,
                )

                rows: list[list[Any]] = []

                for index, row in enumerate(
                    reader,
                    start=1,
                ):
                    if index <= record.header_row:
                        continue

                    rows.append(row)

                    if len(rows) >= sample_rows:
                        break

                return rows

        except Exception as exc:
            last_error = exc

    raise RuntimeError(
        f"No fue posible leer el CSV: {last_error}"
    )


def load_sample_rows(
    root: Path,
    record: InventoryRecord,
    sample_rows: int,
    password_map: dict[str, tuple[str, ...]],
) -> list[list[Any]]:
    path = root / Path(record.relative_path)

    if not path.exists():
        raise FileNotFoundError(
            f"No existe: {path}"
        )

    method = record.open_method.upper()
    password_candidates = passwords_for_entity(
        password_map,
        record.entity_hint,
    )

    if method in {"HTML_XLS", "HTML_XLSX"}:
        return read_html_rows(
            path.read_bytes(),
            record,
            sample_rows,
        )

    if method.startswith("CSV"):
        return read_csv_rows(
            path,
            record,
            sample_rows,
        )

    if method == "OPENPYXL":
        return read_openpyxl_rows(
            path,
            record,
            sample_rows,
        )

    if method == "XLRD":
        return read_xlrd_rows(
            path,
            record,
            sample_rows,
        )

    if method.startswith("MSOFFCRYPTO"):
        decrypted = decrypt_office(
            path,
            password_candidates,
        )

        if "OPENPYXL" in method:
            return read_openpyxl_rows(
                io.BytesIO(decrypted),
                record,
                sample_rows,
            )

        if "XLRD" in method:
            return read_xlrd_rows(
                decrypted,
                record,
                sample_rows,
            )

        # Fallback según firma interna.
        if decrypted.startswith(b"PK\x03\x04"):
            return read_openpyxl_rows(
                io.BytesIO(decrypted),
                record,
                sample_rows,
            )

        if (
            decrypted
            .lstrip()
            .lower()
            .startswith((b"<html", b"<!doctype"))
        ):
            return read_html_rows(
                decrypted,
                record,
                sample_rows,
            )

        return read_xlrd_rows(
            decrypted,
            record,
            sample_rows,
        )

    # Fallback por extensión.
    suffix = path.suffix.lower()

    if suffix in {".xlsx", ".xlsm"}:
        return read_openpyxl_rows(
            path,
            record,
            sample_rows,
        )

    if suffix == ".xls":
        return read_xlrd_rows(
            path,
            record,
            sample_rows,
        )

    if suffix == ".csv":
        return read_csv_rows(
            path,
            record,
            sample_rows,
        )

    raise RuntimeError(
        f"Método de apertura no soportado: "
        f"{record.open_method}"
    )


# ============================================================
# TIPADO DE VALORES
# ============================================================


DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%m-%d-%Y",
    "%d.%m.%Y",
    "%m.%d.%Y",
    "%d/%m/%y",
    "%m/%d/%y",
    "%d-%m-%y",
    "%m-%d-%y",
    "%d-%b-%Y",
    "%d-%b-%y",
    "%d %b %Y",
    "%d %b %y",
    "%Y%m%d",
)

DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%m-%d-%Y %H:%M:%S",
    "%m-%d-%Y %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
)

TIME_FORMATS = (
    "%H:%M",
    "%H:%M:%S",
    "%H:%M:%S.%f",
    "%I:%M %p",
    "%I:%M:%S %p",
)


def parse_numeric_text(value: str) -> Decimal | None:
    text = value.strip()

    if not text:
        return None

    # No tratar fechas como números.
    if re.search(r"[/:\-]", text):
        return None

    negative = (
        text.startswith("(")
        and text.endswith(")")
    )

    if negative:
        text = text[1:-1].strip()

    text = re.sub(
        r"[$€£COPUSD% ]",
        "",
        text,
        flags=re.IGNORECASE,
    )

    if not text:
        return None

    # Si hay coma y punto, usar el último como decimal.
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "")
            text = text.replace(",", ".")
        else:
            text = text.replace(",", "")

    elif "," in text:
        parts = text.split(",")

        # 1,234 -> miles; 1,23 -> decimal
        if (
            len(parts) == 2
            and len(parts[1]) not in {3}
        ):
            text = text.replace(",", ".")
        else:
            text = text.replace(",", "")

    try:
        number = Decimal(text)

        if negative:
            number = -number

        return number

    except InvalidOperation:
        return None


def parse_temporal_text(
    value: str,
) -> str | None:
    text = value.strip()

    if not text:
        return None

    # Evitar parsear IDs numéricos como fechas.
    if not re.search(
        r"[-/:T ]",
        text,
    ):
        return None

    for fmt in DATETIME_FORMATS:
        try:
            datetime.strptime(
                text,
                fmt,
            )
            return "datetime"
        except ValueError:
            pass

    for fmt in DATE_FORMATS:
        try:
            datetime.strptime(
                text,
                fmt,
            )
            return "date"
        except ValueError:
            pass

    for fmt in TIME_FORMATS:
        try:
            datetime.strptime(
                text.upper(),
                fmt,
            )
            return "time"
        except ValueError:
            pass

    return None


def classify_value(
    value: Any,
) -> tuple[str, int]:
    """
    Retorna (tipo, longitud_textual).
    """

    if is_blank(value):
        return "blank", 0

    if isinstance(value, bool):
        return "boolean", len(str(value))

    if isinstance(value, datetime):
        if (
            value.hour
            or value.minute
            or value.second
            or value.microsecond
        ):
            return "datetime", len(str(value))

        return "date", len(str(value.date()))

    if isinstance(value, date):
        return "date", len(str(value))

    if isinstance(value, time):
        return "time", len(str(value))

    if isinstance(value, int):
        return "integer", len(str(value))

    if isinstance(value, float):
        if value != value:
            return "blank", 0

        if value.is_integer():
            return "integer", len(str(int(value)))

        return "numeric", len(str(value))

    if isinstance(value, Decimal):
        if value == value.to_integral():
            return "integer", len(str(value))

        return "numeric", len(str(value))

    text = str(value).strip()

    if text.lower() in BOOLEAN_TEXTS:
        return "boolean", len(text)

    temporal = parse_temporal_text(text)

    if temporal:
        return temporal, len(text)

    numeric = parse_numeric_text(text)

    if numeric is not None:
        if numeric == numeric.to_integral():
            return "integer", len(text)

        return "numeric", len(text)

    return "text", len(text)


def update_column_stats(
    stats: ColumnStats,
    value: Any,
) -> None:
    stats.sampled_rows += 1

    kind, text_length = classify_value(value)

    if kind == "blank":
        stats.blank_count += 1
        return

    stats.non_empty_count += 1
    stats.max_text_length = max(
        stats.max_text_length,
        text_length,
    )

    if len(stats.distinct_tokens or ()) < 10000:
        assert stats.distinct_tokens is not None
        stats.distinct_tokens.add(
            stable_token(value)
        )

    if kind == "integer":
        stats.integer_count += 1
    elif kind == "numeric":
        stats.numeric_count += 1
    elif kind == "date":
        stats.date_count += 1
    elif kind == "datetime":
        stats.datetime_count += 1
    elif kind == "time":
        stats.time_count += 1
    elif kind == "boolean":
        stats.boolean_count += 1
    else:
        stats.text_count += 1


def infer_pg_type(
    stats: ColumnStats,
) -> tuple[str, float, str]:
    n = stats.non_empty_count

    if n <= 0:
        return "TEXT", 0.0, "EMPTY_SAMPLE"

    identifier_override = is_identifier_like(
        stats.normalized_header
    )

    temporal_total = (
        stats.date_count
        + stats.datetime_count
    )

    numeric_total = (
        stats.integer_count
        + stats.numeric_count
    )

    counts = {
        "BOOLEAN": stats.boolean_count,
        "TIME": stats.time_count,
        "TIMESTAMP": stats.datetime_count,
        "DATE": stats.date_count,
        "NUMERIC": numeric_total,
        "TEXT": stats.text_count,
    }

    dominant_type, dominant_count = max(
        counts.items(),
        key=lambda item: item[1],
    )

    confidence = dominant_count / n

    if identifier_override:
        return (
            "TEXT",
            max(
                confidence,
                numeric_total / n,
            ),
            "IDENTIFIER_SEMANTIC_OVERRIDE",
        )

    if temporal_total / n >= 0.95:
        if stats.datetime_count > 0:
            return (
                "TIMESTAMP",
                temporal_total / n,
                "TEMPORAL_DOMINANT",
            )

        return (
            "DATE",
            temporal_total / n,
            "TEMPORAL_DOMINANT",
        )

    if stats.time_count / n >= 0.95:
        return (
            "TIME",
            stats.time_count / n,
            "TIME_DOMINANT",
        )

    if stats.boolean_count / n >= 0.95:
        return (
            "BOOLEAN",
            stats.boolean_count / n,
            "BOOLEAN_DOMINANT",
        )

    if numeric_total / n >= 0.95:
        if stats.numeric_count == 0:
            return (
                "BIGINT",
                numeric_total / n,
                "INTEGER_DOMINANT",
            )

        return (
            "NUMERIC",
            numeric_total / n,
            "NUMERIC_DOMINANT",
        )

    # Hints semánticos se usan solo si los datos los apoyan de forma razonable.
    h = stats.normalized_header

    if (
        any(token in h for token in NUMERIC_HINTS)
        and numeric_total / n >= 0.80
    ):
        return (
            "NUMERIC",
            numeric_total / n,
            "NUMERIC_SEMANTIC_HINT",
        )

    if (
        any(token in h for token in DATE_HINTS)
        and temporal_total / n >= 0.80
    ):
        return (
            "TIMESTAMP"
            if stats.datetime_count > 0
            else "DATE",
            temporal_total / n,
            "DATE_SEMANTIC_HINT",
        )

    if (
        any(token in h for token in TIME_HINTS)
        and stats.time_count / n >= 0.80
    ):
        return (
            "TIME",
            stats.time_count / n,
            "TIME_SEMANTIC_HINT",
        )

    return (
        "TEXT",
        max(
            confidence,
            stats.text_count / n,
        ),
        "MIXED_OR_TEXT",
    )


def profile_record(
    root: Path,
    record: InventoryRecord,
    sample_rows: int,
    password_map: dict[str, tuple[str, ...]],
) -> tuple[
    list[dict[str, Any]],
    dict[str, Any],
]:
    rows = load_sample_rows(
        root,
        record,
        sample_rows,
        password_map,
    )

    headers = record.headers[:]

    if not headers:
        headers = [
            f"column_{index}"
            for index in range(
                1,
                record.column_count + 1,
            )
        ]

    normalized = record.normalized_headers[:]

    while len(normalized) < len(headers):
        normalized.append(
            normalize_header(
                headers[len(normalized)]
            )
        )

    columns: list[ColumnStats] = []

    for index, header in enumerate(
        headers,
        start=1,
    ):
        norm = (
            normalized[index - 1]
            if index - 1 < len(normalized)
            else normalize_header(header)
        )

        columns.append(
            ColumnStats(
                position=index,
                original_header=header,
                normalized_header=choose_pg_identifier(
                    norm or header,
                    f"column_{index}",
                ),
            )
        )

    for row in rows:
        for index, stats in enumerate(columns):
            value = (
                row[index]
                if index < len(row)
                else None
            )

            update_column_stats(
                stats,
                value,
            )

    profile_rows: list[dict[str, Any]] = []
    type_counter: Counter[str] = Counter()

    for stats in columns:
        pg_type, confidence, reason = infer_pg_type(
            stats
        )

        type_counter[pg_type] += 1

        profile_rows.append(
            {
                "entity_hint": record.entity_hint,
                "market_hint": record.market_hint,
                "structure_hash": record.structure_hash,
                "source_year": record.source_year or "",
                "relative_path": record.relative_path,
                "sheet_name": record.sheet_name,
                "open_method": record.open_method,
                "header_row": record.header_row,
                "data_rows_estimate": record.data_rows_estimate,
                "sampled_rows": stats.sampled_rows,
                "column_position": stats.position,
                "original_header": stats.original_header,
                "normalized_header": stats.normalized_header,
                "blank_count": stats.blank_count,
                "non_empty_count": stats.non_empty_count,
                "integer_count": stats.integer_count,
                "numeric_count": stats.numeric_count,
                "date_count": stats.date_count,
                "datetime_count": stats.datetime_count,
                "time_count": stats.time_count,
                "boolean_count": stats.boolean_count,
                "text_count": stats.text_count,
                "sample_distinct_count": len(
                    stats.distinct_tokens or ()
                ),
                "max_text_length": stats.max_text_length,
                "suggested_pg_type": pg_type,
                "type_confidence": round(
                    confidence,
                    6,
                ),
                "type_reason": reason,
            }
        )

    summary = {
        "entity_hint": record.entity_hint,
        "market_hint": record.market_hint,
        "structure_hash": record.structure_hash,
        "source_year": record.source_year or "",
        "relative_path": record.relative_path,
        "sheet_name": record.sheet_name,
        "open_method": record.open_method,
        "header_row": record.header_row,
        "data_rows_estimate": record.data_rows_estimate,
        "sampled_rows": len(rows),
        "column_count": len(columns),
        "text_columns": type_counter["TEXT"],
        "bigint_columns": type_counter["BIGINT"],
        "numeric_columns": type_counter["NUMERIC"],
        "date_columns": type_counter["DATE"],
        "timestamp_columns": type_counter["TIMESTAMP"],
        "time_columns": type_counter["TIME"],
        "boolean_columns": type_counter["BOOLEAN"],
        "profile_status": "OK",
        "error": "",
    }

    return profile_rows, summary


# ============================================================
# UNIÓN DE CAMPOS POR ENTIDAD
# ============================================================


def merge_recommended_types(
    observed: set[str],
) -> str:
    observed = {
        item
        for item in observed
        if item in FINALIZED_TYPES
    }

    if not observed:
        return "TEXT"

    if len(observed) == 1:
        return next(iter(observed))

    if observed <= {"BIGINT", "NUMERIC"}:
        return "NUMERIC"

    if observed <= {"DATE", "TIMESTAMP"}:
        return "TIMESTAMP"

    # Cualquier mezcla incompatible se conserva como TEXT,
    # priorizando seguridad de la carga histórica.
    return "TEXT"


def build_field_union(
    profile_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[
        tuple[str, str],
        dict[str, Any],
    ] = {}

    for row in profile_rows:
        key = (
            str(row["entity_hint"]),
            str(row["normalized_header"]),
        )

        if key not in groups:
            groups[key] = {
                "entity_hint": row["entity_hint"],
                "normalized_header": row["normalized_header"],
                "original_headers": set(),
                "markets": set(),
                "years": set(),
                "structures": set(),
                "observed_pg_types": set(),
                "total_non_empty_samples": 0,
                "max_text_length": 0,
            }

        item = groups[key]

        item["original_headers"].add(
            str(row["original_header"])
        )
        item["markets"].add(
            str(row["market_hint"])
        )

        if str(row["source_year"]).strip():
            item["years"].add(
                str(row["source_year"])
            )

        item["structures"].add(
            str(row["structure_hash"])
        )
        item["observed_pg_types"].add(
            str(row["suggested_pg_type"])
        )
        item["total_non_empty_samples"] += safe_int(
            row["non_empty_count"],
            0,
        )
        item["max_text_length"] = max(
            item["max_text_length"],
            safe_int(
                row["max_text_length"],
                0,
            ),
        )

    result: list[dict[str, Any]] = []

    for item in groups.values():
        observed = set(item["observed_pg_types"])

        result.append(
            {
                "entity_hint": item["entity_hint"],
                "normalized_header": item["normalized_header"],
                "original_headers": " | ".join(
                    sorted(item["original_headers"])
                ),
                "structures_count": len(
                    item["structures"]
                ),
                "markets": " | ".join(
                    sorted(item["markets"])
                ),
                "years": " | ".join(
                    sorted(
                        item["years"],
                        key=lambda value: safe_int(
                            value,
                            0,
                        ),
                    )
                ),
                "observed_pg_types": " | ".join(
                    sorted(observed)
                ),
                "recommended_pg_type": (
                    merge_recommended_types(
                        observed
                    )
                ),
                "total_non_empty_samples": (
                    item[
                        "total_non_empty_samples"
                    ]
                ),
                "max_text_length": item[
                    "max_text_length"
                ],
            }
        )

    result.sort(
        key=lambda row: (
            row["entity_hint"],
            row["normalized_header"],
        )
    )

    return result


# ============================================================
# SALIDA
# ============================================================


def write_csv(
    path: Path,
    rows: list[dict[str, Any]],
    fieldnames: list[str],
) -> None:
    with path.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Perfila una muestra representativa de cada estructura "
            "histórica de CloseNetting. No modifica PostgreSQL ni "
            "los archivos originales."
        )
    )

    parser.add_argument(
        "root",
        type=Path,
        help=(
            "Carpeta raíz que contiene los históricos "
            "2018, 2019, ..., 2026."
        ),
    )

    parser.add_argument(
        "inventory_csv",
        type=Path,
        help=(
            "historical_structure_inventory.csv generado "
            "por historical_structure_scanner_v2.py"
        ),
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(
            "historical_profile_output"
        ),
    )

    parser.add_argument(
        "--sample-rows",
        type=int,
        default=DEFAULT_SAMPLE_ROWS,
        help=(
            "Máximo de filas de datos por archivo muestra. "
            f"Por defecto: {DEFAULT_SAMPLE_ROWS}."
        ),
    )

    parser.add_argument(
        "--files-per-structure",
        type=int,
        default=1,
        help=(
            "Cantidad de archivos representativos por estructura. "
            "Por defecto: 1."
        ),
    )

    parser.add_argument(
        "--min-year",
        type=int,
        default=DEFAULT_MIN_YEAR,
    )

    parser.add_argument(
        "--max-year",
        type=int,
        default=DEFAULT_MAX_YEAR,
    )

    parser.add_argument(
        "--include-undated",
        action="store_true",
        help=(
            "Incluye registros sin source_year, por ejemplo "
            "closeoutnetting_pruebas. Por defecto se excluyen."
        ),
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    root = args.root.expanduser().resolve()
    inventory_csv = (
        args.inventory_csv
        .expanduser()
        .resolve()
    )
    output_dir = (
        args.output_dir
        .expanduser()
        .resolve()
    )

    if not root.is_dir():
        print(
            f"ERROR: no existe la carpeta raíz: {root}",
            file=sys.stderr,
        )
        return 2

    if not inventory_csv.is_file():
        print(
            f"ERROR: no existe el inventario: "
            f"{inventory_csv}",
            file=sys.stderr,
        )
        return 2

    if args.sample_rows <= 0:
        print(
            "ERROR: --sample-rows debe ser mayor que cero.",
            file=sys.stderr,
        )
        return 2

    if args.files_per_structure <= 0:
        print(
            "ERROR: --files-per-structure debe ser mayor que cero.",
            file=sys.stderr,
        )
        return 2

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    records = read_inventory(
        inventory_csv,
        min_year=args.min_year,
        max_year=args.max_year,
        include_undated=args.include_undated,
    )

    representatives = select_representatives(
        records,
        files_per_structure=args.files_per_structure,
    )

    password_map = load_report_passwords()

    print("=" * 70)
    print("AUTO CLOSENETTING - PERFILADOR HISTÓRICO")
    print("=" * 70)
    print(f"Raíz: {root}")
    print(f"Inventario: {inventory_csv}")
    print(
        f"Registros OK elegibles: {len(records)}"
    )
    print(
        "Muestras seleccionadas: "
        f"{len(representatives)}"
    )
    print(
        "Rango de años: "
        f"{args.min_year}-{args.max_year}"
    )
    print(
        "Filas máximas por muestra: "
        f"{args.sample_rows}"
    )
    print(
        "Archivos por estructura: "
        f"{args.files_per_structure}"
    )
    print(
        "Registros sin año: "
        + (
            "INCLUIDOS"
            if args.include_undated
            else "EXCLUIDOS"
        )
    )
    print()

    profile_rows: list[dict[str, Any]] = []
    structure_rows: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()

    for index, record in enumerate(
        representatives,
        start=1,
    ):
        try:
            rows, summary = profile_record(
                root,
                record,
                args.sample_rows,
                password_map,
            )
            profile_rows.extend(rows)
            structure_rows.append(summary)
            status_counts["OK"] += 1

        except Exception as exc:
            structure_rows.append(
                {
                    "entity_hint": record.entity_hint,
                    "market_hint": record.market_hint,
                    "structure_hash": record.structure_hash,
                    "source_year": record.source_year or "",
                    "relative_path": record.relative_path,
                    "sheet_name": record.sheet_name,
                    "open_method": record.open_method,
                    "header_row": record.header_row,
                    "data_rows_estimate": record.data_rows_estimate,
                    "sampled_rows": 0,
                    "column_count": record.column_count,
                    "text_columns": 0,
                    "bigint_columns": 0,
                    "numeric_columns": 0,
                    "date_columns": 0,
                    "timestamp_columns": 0,
                    "time_columns": 0,
                    "boolean_columns": 0,
                    "profile_status": "ERROR",
                    "error": (
                        f"{type(exc).__name__}: {exc}"
                    ),
                }
            )
            status_counts["ERROR"] += 1

        if (
            index % 10 == 0
            or index == len(representatives)
        ):
            print(
                f"Perfiladas {index}/"
                f"{len(representatives)} muestras..."
            )

    field_union_rows = build_field_union(
        profile_rows
    )

    columns_path = (
        output_dir
        / "historical_profile_columns.csv"
    )
    structures_path = (
        output_dir
        / "historical_profile_structures.csv"
    )
    union_path = (
        output_dir
        / "historical_field_union.csv"
    )
    summary_path = (
        output_dir
        / "historical_profile_summary.json"
    )

    write_csv(
        columns_path,
        profile_rows,
        [
            "entity_hint",
            "market_hint",
            "structure_hash",
            "source_year",
            "relative_path",
            "sheet_name",
            "open_method",
            "header_row",
            "data_rows_estimate",
            "sampled_rows",
            "column_position",
            "original_header",
            "normalized_header",
            "blank_count",
            "non_empty_count",
            "integer_count",
            "numeric_count",
            "date_count",
            "datetime_count",
            "time_count",
            "boolean_count",
            "text_count",
            "sample_distinct_count",
            "max_text_length",
            "suggested_pg_type",
            "type_confidence",
            "type_reason",
        ],
    )

    write_csv(
        structures_path,
        structure_rows,
        [
            "entity_hint",
            "market_hint",
            "structure_hash",
            "source_year",
            "relative_path",
            "sheet_name",
            "open_method",
            "header_row",
            "data_rows_estimate",
            "sampled_rows",
            "column_count",
            "text_columns",
            "bigint_columns",
            "numeric_columns",
            "date_columns",
            "timestamp_columns",
            "time_columns",
            "boolean_columns",
            "profile_status",
            "error",
        ],
    )

    write_csv(
        union_path,
        field_union_rows,
        [
            "entity_hint",
            "normalized_header",
            "original_headers",
            "structures_count",
            "markets",
            "years",
            "observed_pg_types",
            "recommended_pg_type",
            "total_non_empty_samples",
            "max_text_length",
        ],
    )

    summary = {
        "root": str(root),
        "inventory_csv": str(
            inventory_csv
        ),
        "year_range": {
            "min": args.min_year,
            "max": args.max_year,
        },
        "include_undated": (
            args.include_undated
        ),
        "sample_rows_per_file": (
            args.sample_rows
        ),
        "files_per_structure": (
            args.files_per_structure
        ),
        "eligible_inventory_records": len(
            records
        ),
        "selected_samples": len(
            representatives
        ),
        "profiled_structures_ok": (
            status_counts["OK"]
        ),
        "profile_errors": (
            status_counts["ERROR"]
        ),
        "profiled_columns": len(
            profile_rows
        ),
        "union_fields": len(
            field_union_rows
        ),
        "entities_profiled": sorted(
            {
                str(row["entity_hint"])
                for row in profile_rows
            }
        ),
        "outputs": {
            "columns_csv": str(
                columns_path
            ),
            "structures_csv": str(
                structures_path
            ),
            "field_union_csv": str(
                union_path
            ),
        },
    }

    summary_path.write_text(
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print()
    print("=" * 70)
    print("PERFILADO FINALIZADO")
    print("=" * 70)
    print(
        "Muestras OK: "
        f"{status_counts['OK']}"
    )
    print(
        "Errores de perfilado: "
        f"{status_counts['ERROR']}"
    )
    print(
        "Columnas perfiladas: "
        f"{len(profile_rows)}"
    )
    print(
        "Campos unificados por entidad: "
        f"{len(field_union_rows)}"
    )
    print()
    print(f"Columnas: {columns_path}")
    print(f"Estructuras: {structures_path}")
    print(f"Unión de campos: {union_path}")
    print(f"Resumen: {summary_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
