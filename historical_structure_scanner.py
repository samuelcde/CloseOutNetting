from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import os
import re
import sys

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable


SUPPORTED_EXTENSIONS = {
    ".xlsx",
    ".xlsm",
    ".xls",
    ".csv",
}

YEAR_RE = re.compile(r"^(19|20|21)\d{2}$")


@dataclass(slots=True)
class SheetScan:
    sheet_name: str
    header_row: int | None
    headers: list[str]
    normalized_headers: list[str]
    structure_hash: str | None
    data_rows_estimate: int | None
    scan_status: str
    open_method: str = ""
    error: str = ""


# ============================================================
# NORMALIZACIÓN
# ============================================================


def normalize_header(value: Any) -> str:
    if value is None:
        return ""

    text = str(value).strip().lower()
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^a-z0-9_]+", "", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text


def calculate_sha256(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        while chunk := handle.read(128 * 1024):
            digest.update(chunk)

    return digest.hexdigest()


def detect_source_year(relative_path: Path) -> int | None:
    for part in relative_path.parts:
        if YEAR_RE.fullmatch(part):
            return int(part)

    return None


def entity_hint(path: Path) -> str:
    text = " ".join(path.parts).lower()
    compact = re.sub(r"[^a-z0-9]+", "", text)

    aliases = (
        ("MORGAN_STANLEY", ("morganstanley", "morgan", "stanley")),
        ("SMBC", ("smbc", "sumitomo")),
        ("CITI", ("citibank", "citi")),
        ("GS", ("goldmansachs", "goldman", "gsotc", "gsreport")),
        ("STANDARD", ("standardchartered",)),
        ("BBVA", ("bbva",)),
        ("FICC", ("baml", "merrilllynch", "bankofamerica")),
        ("SET-ICAP", ("seticap", "investorssolutions")),
    )

    for entity, values in aliases:
        if any(alias in compact for alias in values):
            return entity

    upper_parts = {
        re.sub(r"[^A-Z0-9]+", "_", part.upper()).strip("_")
        for part in path.parts
    }

    for entity in (
        "GS",
        "SMBC",
        "CITI",
        "BBVA",
        "FICC",
        "STANDARD",
        "MORGAN_STANLEY",
        "SET_ICAP",
    ):
        if entity in upper_parts:
            return entity.replace("SET_ICAP", "SET-ICAP")

    return "DESCONOCIDA"


def market_hint(sheet_name: str, headers: Iterable[str]) -> str:
    sheet = normalize_header(sheet_name)
    header_set = set(headers)

    if "irs" in sheet or "ois" in sheet:
        return "IRS"

    if "ccs" in sheet:
        return "CCS"

    if "commod" in sheet or sheet == "comm":
        return "COMMODITIES"

    if "listed" in sheet or sheet == "ld":
        return "LISTED"

    if "option" in sheet or "opcion" in sheet:
        return "OPTIONS/OPCIONES"

    if sheet in {
        "fx",
        "cash",
        "fx_cash",
        "cash_fx",
        "forward",
        "forwards",
        "fxcash",
    }:
        return "FORWARD"

    if {
        "strikeprice",
        "putorcall",
    }.intersection(header_set):
        return "LISTED/OPTIONS"

    if {
        "fixed_rate",
        "termination_dt",
        "economics_type_cd",
    }.intersection(header_set):
        return "IRS"

    if {
        "buy_currency",
        "sell_currency",
        "fixing_date",
        "rate",
    }.intersection(header_set):
        return "FORWARD"

    return "DESCONOCIDO"


def _cell_kind(value: Any) -> str:
    if value is None or str(value).strip() == "":
        return "blank"

    if isinstance(value, (date, datetime)):
        return "date"

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "number"

    return "text"


def choose_header_row(
    rows: list[list[Any]],
) -> tuple[int | None, list[str]]:
    best: tuple[float, int, list[str]] | None = None

    for index, row in enumerate(rows, start=1):
        nonempty_values = [
            value
            for value in row
            if value is not None and str(value).strip() != ""
        ]

        if len(nonempty_values) < 2:
            continue

        kinds = [_cell_kind(value) for value in nonempty_values]
        text_count = kinds.count("text")
        numeric_count = kinds.count("number") + kinds.count("date")

        rendered = [str(value).strip() for value in nonempty_values]
        unique_count = len(set(rendered))

        score = (
            len(nonempty_values) * 2.0
            + text_count * 2.5
            + unique_count * 0.5
            - numeric_count * 1.5
            - (index - 1) * 0.15
        )

        headers = [
            "" if value is None else str(value).strip()
            for value in row
        ]

        while headers and headers[-1] == "":
            headers.pop()

        candidate = (score, index, headers)

        if best is None or candidate[0] > best[0]:
            best = candidate

    if best is None:
        return None, []

    return best[1], best[2]


def structure_hash(headers: list[str]) -> tuple[list[str], str | None]:
    normalized = [normalize_header(value) for value in headers]

    while normalized and normalized[-1] == "":
        normalized.pop()

    if not any(normalized):
        return normalized, None

    digest = hashlib.sha256(
        "|".join(normalized).encode("utf-8")
    ).hexdigest()

    return normalized, digest[:20]


# ============================================================
# PASSWORDS
# ============================================================


def load_report_passwords() -> dict[str, tuple[str, ...]]:
    """
    Lee REPORT_PASSWORDS desde .env sin mostrar nunca las claves.

    Formato esperado:
        REPORT_PASSWORDS={"GS":["clave"],"MORGAN_STANLEY":["clave2"]}
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

    for entity, values in parsed.items():
        key = str(entity).strip().upper()

        if values is None:
            result[key] = ()
            continue

        if not isinstance(values, list):
            continue

        clean: list[str] = []

        for value in values:
            password = str(value).strip()
            if password and password not in clean:
                clean.append(password)

        result[key] = tuple(clean)

    return result


def _looks_like_html_bytes(data: bytes) -> bool:
    sample = data[:8192].lstrip().lower()

    return (
        sample.startswith(b"<html")
        or sample.startswith(b"<!doctype html")
        or b"<html" in sample
        or b"<table" in sample
    )


def _looks_like_zip_bytes(data: bytes) -> bool:
    return data.startswith(b"PK\x03\x04")


def _looks_like_ole_bytes(data: bytes) -> bool:
    return data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")


def try_decrypt_office(
    path: Path,
    passwords: tuple[str, ...],
) -> tuple[bytes | None, str, str]:
    """
    Retorna:
        decrypted_bytes, status, error

    status:
        NOT_ENCRYPTED
        PASSWORD_REQUIRED
        PASSWORD_FAILED
        DECRYPTED
        DECRYPT_ERROR
    """

    try:
        import msoffcrypto
    except ImportError:
        return (
            None,
            "DEPENDENCY_MISSING_MSOFFCRYPTO",
            "Instale msoffcrypto-tool: python -m pip install msoffcrypto-tool",
        )

    try:
        with path.open("rb") as handle:
            office = msoffcrypto.OfficeFile(handle)

            try:
                encrypted = office.is_encrypted()
            except Exception:
                encrypted = True

        if not encrypted:
            return None, "NOT_ENCRYPTED", ""

    except Exception as exc:
        return None, "DECRYPT_ERROR", f"{type(exc).__name__}: {exc}"

    if not passwords:
        return None, "PASSWORD_REQUIRED", "Documento Office cifrado sin contraseña configurada para la entidad."

    last_error = ""

    for password in passwords:
        try:
            with path.open("rb") as handle:
                office = msoffcrypto.OfficeFile(handle)
                office.load_key(password=password)
                output = io.BytesIO()
                office.decrypt(output)
                return output.getvalue(), "DECRYPTED", ""
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

    return None, "PASSWORD_FAILED", last_error or "Ninguna contraseña configurada pudo abrir el archivo."


# ============================================================
# HTML DISFRAZADO DE XLS
# ============================================================


class _HTMLTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self.current_table: list[list[str]] | None = None
        self.current_row: list[str] | None = None
        self.current_cell: list[str] | None = None
        self.in_cell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()

        if tag == "table":
            self.current_table = []
            return

        if tag == "tr" and self.current_table is not None:
            self.current_row = []
            return

        if tag in {"td", "th"} and self.current_row is not None:
            self.current_cell = []
            self.in_cell = True

    def handle_data(self, data: str) -> None:
        if self.in_cell and self.current_cell is not None:
            self.current_cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        if tag in {"td", "th"} and self.current_row is not None:
            value = "".join(self.current_cell or [])
            value = html.unescape(value)
            value = re.sub(r"\s+", " ", value).strip()
            self.current_row.append(value)
            self.current_cell = None
            self.in_cell = False
            return

        if tag == "tr" and self.current_table is not None:
            if self.current_row is not None and any(
                str(value).strip() for value in self.current_row
            ):
                self.current_table.append(self.current_row)
            self.current_row = None
            return

        if tag == "table":
            if self.current_table:
                self.tables.append(self.current_table)
            self.current_table = None


def scan_html_bytes(
    data: bytes,
    max_header_scan: int,
    *,
    method: str,
) -> list[SheetScan]:
    text: str | None = None

    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue

    if text is None:
        return [
            SheetScan(
                sheet_name="",
                header_row=None,
                headers=[],
                normalized_headers=[],
                structure_hash=None,
                data_rows_estimate=None,
                scan_status="OPEN_ERROR",
                open_method=method,
                error="No fue posible decodificar el contenido HTML.",
            )
        ]

    parser = _HTMLTableParser()

    try:
        parser.feed(text)
    except Exception as exc:
        return [
            SheetScan(
                sheet_name="",
                header_row=None,
                headers=[],
                normalized_headers=[],
                structure_hash=None,
                data_rows_estimate=None,
                scan_status="OPEN_ERROR",
                open_method=method,
                error=f"{type(exc).__name__}: {exc}",
            )
        ]

    if not parser.tables:
        return [
            SheetScan(
                sheet_name="",
                header_row=None,
                headers=[],
                normalized_headers=[],
                structure_hash=None,
                data_rows_estimate=None,
                scan_status="EMPTY_OR_NO_HEADER",
                open_method=method,
                error="Se detectó HTML pero no se encontraron tablas.",
            )
        ]

    scans: list[SheetScan] = []

    for index, rows in enumerate(parser.tables, start=1):
        sample_rows = rows[:max_header_scan]
        header_row, headers = choose_header_row(sample_rows)
        normalized, digest = structure_hash(headers)

        scans.append(
            SheetScan(
                sheet_name=f"HTML_TABLE_{index}",
                header_row=header_row,
                headers=headers,
                normalized_headers=normalized,
                structure_hash=digest,
                data_rows_estimate=(
                    max(0, len(rows) - header_row)
                    if header_row is not None
                    else None
                ),
                scan_status="OK" if digest else "EMPTY_OR_NO_HEADER",
                open_method=method,
            )
        )

    return scans


# ============================================================
# XLSX / XLSM
# ============================================================


def _scan_openpyxl_workbook(
    workbook: Any,
    max_header_scan: int,
    *,
    method: str,
) -> list[SheetScan]:
    scans: list[SheetScan] = []

    try:
        for worksheet in workbook.worksheets:
            sample_rows: list[list[Any]] = []

            for row in worksheet.iter_rows(
                min_row=1,
                max_row=max_header_scan,
                values_only=True,
            ):
                sample_rows.append(list(row))

            header_row, headers = choose_header_row(sample_rows)
            normalized, digest = structure_hash(headers)

            max_row = worksheet.max_row
            data_rows = None

            if header_row is not None and max_row is not None:
                data_rows = max(0, int(max_row) - header_row)

            scans.append(
                SheetScan(
                    sheet_name=worksheet.title,
                    header_row=header_row,
                    headers=headers,
                    normalized_headers=normalized,
                    structure_hash=digest,
                    data_rows_estimate=data_rows,
                    scan_status="OK" if digest else "EMPTY_OR_NO_HEADER",
                    open_method=method,
                )
            )
    finally:
        workbook.close()

    return scans


def scan_xlsx(
    path: Path,
    max_header_scan: int,
    passwords: tuple[str, ...],
) -> list[SheetScan]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        return [
            SheetScan(
                sheet_name="",
                header_row=None,
                headers=[],
                normalized_headers=[],
                structure_hash=None,
                data_rows_estimate=None,
                scan_status="DEPENDENCY_MISSING_OPENPYXL",
                error=str(exc),
            )
        ]

    prefix = path.read_bytes()[:8192]

    if _looks_like_html_bytes(prefix):
        return scan_html_bytes(
            path.read_bytes(),
            max_header_scan,
            method="HTML_XLSX",
        )

    try:
        workbook = load_workbook(
            path,
            read_only=True,
            data_only=True,
        )
        return _scan_openpyxl_workbook(
            workbook,
            max_header_scan,
            method="OPENPYXL",
        )
    except Exception as original_exc:
        decrypted, decrypt_status, decrypt_error = try_decrypt_office(
            path,
            passwords,
        )

        if decrypted is not None:
            try:
                if _looks_like_html_bytes(decrypted):
                    return scan_html_bytes(
                        decrypted,
                        max_header_scan,
                        method="MSOFFCRYPTO_HTML",
                    )

                workbook = load_workbook(
                    io.BytesIO(decrypted),
                    read_only=True,
                    data_only=True,
                )

                return _scan_openpyxl_workbook(
                    workbook,
                    max_header_scan,
                    method="MSOFFCRYPTO_OPENPYXL",
                )
            except Exception as exc:
                return [
                    SheetScan(
                        sheet_name="",
                        header_row=None,
                        headers=[],
                        normalized_headers=[],
                        structure_hash=None,
                        data_rows_estimate=None,
                        scan_status="OPEN_ERROR_AFTER_DECRYPT",
                        open_method="MSOFFCRYPTO",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                ]

        if decrypt_status in {
            "PASSWORD_REQUIRED",
            "PASSWORD_FAILED",
            "DEPENDENCY_MISSING_MSOFFCRYPTO",
        }:
            return [
                SheetScan(
                    sheet_name="",
                    header_row=None,
                    headers=[],
                    normalized_headers=[],
                    structure_hash=None,
                    data_rows_estimate=None,
                    scan_status=decrypt_status,
                    open_method="MSOFFCRYPTO",
                    error=decrypt_error,
                )
            ]

        return [
            SheetScan(
                sheet_name="",
                header_row=None,
                headers=[],
                normalized_headers=[],
                structure_hash=None,
                data_rows_estimate=None,
                scan_status="OPEN_ERROR",
                open_method="OPENPYXL",
                error=f"{type(original_exc).__name__}: {original_exc}",
            )
        ]


# ============================================================
# XLS
# ============================================================


def _scan_xlrd_workbook(
    workbook: Any,
    max_header_scan: int,
    *,
    method: str,
) -> list[SheetScan]:
    scans: list[SheetScan] = []

    try:
        for sheet_name in workbook.sheet_names():
            sheet = workbook.sheet_by_name(sheet_name)
            rows: list[list[Any]] = []

            for row_index in range(
                min(sheet.nrows, max_header_scan)
            ):
                rows.append(sheet.row_values(row_index))

            header_row, headers = choose_header_row(rows)
            normalized, digest = structure_hash(headers)

            data_rows = (
                max(0, sheet.nrows - header_row)
                if header_row is not None
                else None
            )

            scans.append(
                SheetScan(
                    sheet_name=sheet_name,
                    header_row=header_row,
                    headers=headers,
                    normalized_headers=normalized,
                    structure_hash=digest,
                    data_rows_estimate=data_rows,
                    scan_status="OK" if digest else "EMPTY_OR_NO_HEADER",
                    open_method=method,
                )
            )
    finally:
        workbook.release_resources()

    return scans


def scan_xls(
    path: Path,
    max_header_scan: int,
    passwords: tuple[str, ...],
) -> list[SheetScan]:
    raw_prefix = path.read_bytes()[:8192]

    # Muchos históricos de GS llevan extensión .xls pero internamente son HTML.
    if _looks_like_html_bytes(raw_prefix):
        return scan_html_bytes(
            path.read_bytes(),
            max_header_scan,
            method="HTML_XLS",
        )

    try:
        import xlrd
    except ImportError as exc:
        return [
            SheetScan(
                sheet_name="",
                header_row=None,
                headers=[],
                normalized_headers=[],
                structure_hash=None,
                data_rows_estimate=None,
                scan_status="XLS_REQUIRES_XLRD",
                error=(
                    f"{exc}. Instale xlrd: "
                    "python -m pip install xlrd"
                ),
            )
        ]

    try:
        workbook = xlrd.open_workbook(
            path,
            on_demand=True,
        )

        return _scan_xlrd_workbook(
            workbook,
            max_header_scan,
            method="XLRD",
        )

    except Exception as original_exc:
        decrypted, decrypt_status, decrypt_error = try_decrypt_office(
            path,
            passwords,
        )

        if decrypted is not None:
            try:
                if _looks_like_html_bytes(decrypted):
                    return scan_html_bytes(
                        decrypted,
                        max_header_scan,
                        method="MSOFFCRYPTO_HTML_XLS",
                    )

                if _looks_like_zip_bytes(decrypted):
                    try:
                        from openpyxl import load_workbook
                    except ImportError as exc:
                        return [
                            SheetScan(
                                sheet_name="",
                                header_row=None,
                                headers=[],
                                normalized_headers=[],
                                structure_hash=None,
                                data_rows_estimate=None,
                                scan_status="DEPENDENCY_MISSING_OPENPYXL",
                                error=str(exc),
                            )
                        ]

                    workbook = load_workbook(
                        io.BytesIO(decrypted),
                        read_only=True,
                        data_only=True,
                    )

                    return _scan_openpyxl_workbook(
                        workbook,
                        max_header_scan,
                        method="MSOFFCRYPTO_OPENPYXL_XLS",
                    )

                workbook = xlrd.open_workbook(
                    file_contents=decrypted,
                    on_demand=True,
                )

                return _scan_xlrd_workbook(
                    workbook,
                    max_header_scan,
                    method="MSOFFCRYPTO_XLRD",
                )

            except Exception as exc:
                return [
                    SheetScan(
                        sheet_name="",
                        header_row=None,
                        headers=[],
                        normalized_headers=[],
                        structure_hash=None,
                        data_rows_estimate=None,
                        scan_status="OPEN_ERROR_AFTER_DECRYPT",
                        open_method="MSOFFCRYPTO",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                ]

        if decrypt_status in {
            "PASSWORD_REQUIRED",
            "PASSWORD_FAILED",
            "DEPENDENCY_MISSING_MSOFFCRYPTO",
        }:
            return [
                SheetScan(
                    sheet_name="",
                    header_row=None,
                    headers=[],
                    normalized_headers=[],
                    structure_hash=None,
                    data_rows_estimate=None,
                    scan_status=decrypt_status,
                    open_method="MSOFFCRYPTO",
                    error=decrypt_error,
                )
            ]

        return [
            SheetScan(
                sheet_name="",
                header_row=None,
                headers=[],
                normalized_headers=[],
                structure_hash=None,
                data_rows_estimate=None,
                scan_status="OPEN_ERROR",
                open_method="XLRD",
                error=f"{type(original_exc).__name__}: {original_exc}",
            )
        ]


# ============================================================
# CSV
# ============================================================


def scan_csv(path: Path, max_header_scan: int) -> list[SheetScan]:
    encodings = ("utf-8-sig", "utf-8", "cp1252", "latin-1")
    last_error: Exception | None = None

    for encoding in encodings:
        try:
            with path.open(
                "r",
                encoding=encoding,
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

                reader = csv.reader(handle, dialect)
                rows: list[list[Any]] = []
                total_rows = 0

                for row in reader:
                    total_rows += 1

                    if len(rows) < max_header_scan:
                        rows.append(row)

                header_row, headers = choose_header_row(rows)
                normalized, digest = structure_hash(headers)

                return [
                    SheetScan(
                        sheet_name="CSV",
                        header_row=header_row,
                        headers=headers,
                        normalized_headers=normalized,
                        structure_hash=digest,
                        data_rows_estimate=(
                            max(0, total_rows - header_row)
                            if header_row is not None
                            else None
                        ),
                        scan_status="OK" if digest else "EMPTY_OR_NO_HEADER",
                        open_method=f"CSV_{encoding}",
                    )
                ]
        except Exception as exc:
            last_error = exc

    return [
        SheetScan(
            sheet_name="CSV",
            header_row=None,
            headers=[],
            normalized_headers=[],
            structure_hash=None,
            data_rows_estimate=None,
            scan_status="OPEN_ERROR",
            open_method="CSV",
            error=(
                f"{type(last_error).__name__}: {last_error}"
                if last_error
                else "No fue posible abrir el CSV."
            ),
        )
    ]




def _detect_drm_protection(path: Path) -> list[SheetScan] | None:
    """Reconoce Office DRM/IRM por estructuras OLE; no intenta descifrar."""
    try:
        with path.open("rb") as handle:
            if handle.read(8) != bytes.fromhex("d0cf11e0a1b11ae1"):
                return None
        import olefile
        with olefile.OleFileIO(str(path)) as ole:
            streams = {"/".join(parts) for parts in ole.listdir(streams=True, storages=False)}
        if ("EncryptedPackage" in streams
            and any("DRMEncryptedDataSpace" in name for name in streams)
            and any("DRMEncryptedTransform" in name for name in streams)):
            return [SheetScan(
                sheet_name="", header_row=None, headers=[], normalized_headers=[],
                structure_hash=None, data_rows_estimate=None,
                scan_status="DRM_PROTECTED", open_method="OLE_DRM",
                error="Documento Office protegido por DRM/IRM; requiere acceso autorizado."
            )]
    except ImportError:
        return None  # La auditoría separada sí exige olefile.
    except Exception:
        return None  # Mantener el manejo preexistente del escáner.
    return None

def scan_file(
    path: Path,
    max_header_scan: int,
    *,
    passwords: tuple[str, ...],
) -> list[SheetScan]:
    if path.suffix.lower() in {".xlsx", ".xlsm", ".xls"}:
        drm_result = _detect_drm_protection(path)
        if drm_result is not None:
            return drm_result

    suffix = path.suffix.lower()

    if suffix in {".xlsx", ".xlsm"}:
        return scan_xlsx(
            path,
            max_header_scan,
            passwords,
        )

    if suffix == ".xls":
        return scan_xls(
            path,
            max_header_scan,
            passwords,
        )

    if suffix == ".csv":
        return scan_csv(path, max_header_scan)

    return []


# ============================================================
# CLI
# ============================================================


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Inventaría estructuras históricas de CloseNetting sin "
            "modificar PostgreSQL ni los archivos originales. "
            "Versión 2: soporta Office cifrado y .xls con contenido HTML."
        )
    )

    parser.add_argument(
        "root",
        type=Path,
        help="Carpeta raíz que contiene 2018, 2019, ..., 2026, etc.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("historical_scan_output_v2"),
        help="Carpeta de salida. Por defecto: historical_scan_output_v2",
    )
    parser.add_argument(
        "--max-header-scan",
        type=int,
        default=30,
        help="Cantidad máxima de filas iniciales examinadas por hoja.",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()

    if not root.exists() or not root.is_dir():
        print(
            f"ERROR: la carpeta no existe o no es directorio: {root}",
            file=sys.stderr,
        )
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)

    inventory_path = output_dir / "historical_structure_inventory.csv"
    structures_path = output_dir / "historical_structure_summary.csv"
    summary_json_path = output_dir / "historical_scan_summary.json"

    report_passwords = load_report_passwords()

    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in SUPPORTED_EXTENSIONS
        and not path.name.startswith("~$")
    )

    inventory_rows: list[dict[str, Any]] = []
    structures: dict[tuple[str, str, str], dict[str, Any]] = {}

    print(f"Carpeta raíz: {root}")
    print(f"Archivos candidatos: {len(files)}")
    print(
        "Entidades con contraseña configurada: "
        + ", ".join(
            sorted(
                entity
                for entity, values in report_passwords.items()
                if values
            )
        )
    )

    for position, path in enumerate(files, start=1):
        relative = path.relative_to(root)
        year = detect_source_year(relative)
        hint = entity_hint(relative)
        passwords = report_passwords.get(hint, ())

        try:
            physical_hash = calculate_sha256(path)
            hash_error = ""
        except Exception as exc:
            physical_hash = ""
            hash_error = f"{type(exc).__name__}: {exc}"

        try:
            scans = scan_file(
                path,
                args.max_header_scan,
                passwords=passwords,
            )
        except Exception as exc:
            scans = [
                SheetScan(
                    sheet_name="",
                    header_row=None,
                    headers=[],
                    normalized_headers=[],
                    structure_hash=None,
                    data_rows_estimate=None,
                    scan_status="SCAN_ERROR",
                    open_method="UNKNOWN",
                    error=f"{type(exc).__name__}: {exc}",
                )
            ]

        for scan in scans:
            detected_market = market_hint(
                scan.sheet_name,
                scan.normalized_headers,
            )

            error = " | ".join(
                part
                for part in (hash_error, scan.error)
                if part
            )

            row = {
                "source_year": year or "",
                "relative_path": relative.as_posix(),
                "filename": path.name,
                "extension": path.suffix.lower(),
                "size_bytes": path.stat().st_size,
                "file_sha256": physical_hash,
                "entity_hint": hint,
                "market_hint": detected_market,
                "sheet_name": scan.sheet_name,
                "header_row": scan.header_row or "",
                "data_rows_estimate": (
                    scan.data_rows_estimate
                    if scan.data_rows_estimate is not None
                    else ""
                ),
                "column_count": len(scan.normalized_headers),
                "structure_hash": scan.structure_hash or "",
                "headers": json.dumps(
                    scan.headers,
                    ensure_ascii=False,
                ),
                "normalized_headers": json.dumps(
                    scan.normalized_headers,
                    ensure_ascii=False,
                ),
                "scan_status": scan.scan_status,
                "open_method": scan.open_method,
                "error": error,
            }

            inventory_rows.append(row)

            if scan.structure_hash:
                key = (
                    hint,
                    detected_market,
                    scan.structure_hash,
                )

                if key not in structures:
                    structures[key] = {
                        "entity_hint": hint,
                        "market_hint": detected_market,
                        "structure_hash": scan.structure_hash,
                        "file_count": 0,
                        "years": set(),
                        "sheet_names": set(),
                        "open_methods": set(),
                        "example_relative_path": relative.as_posix(),
                        "column_count": len(scan.normalized_headers),
                        "normalized_headers": json.dumps(
                            scan.normalized_headers,
                            ensure_ascii=False,
                        ),
                    }

                item = structures[key]
                item["file_count"] += 1

                if year:
                    item["years"].add(year)

                if scan.sheet_name:
                    item["sheet_names"].add(scan.sheet_name)

                if scan.open_method:
                    item["open_methods"].add(scan.open_method)

        if position % 25 == 0 or position == len(files):
            print(
                f"Analizados {position}/{len(files)} archivos..."
            )

    inventory_fields = [
        "source_year",
        "relative_path",
        "filename",
        "extension",
        "size_bytes",
        "file_sha256",
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
        "error",
    ]

    with inventory_path.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=inventory_fields,
        )
        writer.writeheader()
        writer.writerows(inventory_rows)

    structure_fields = [
        "entity_hint",
        "market_hint",
        "structure_hash",
        "file_count",
        "years",
        "sheet_names",
        "open_methods",
        "example_relative_path",
        "column_count",
        "normalized_headers",
    ]

    summary_rows: list[dict[str, Any]] = []

    for item in sorted(
        structures.values(),
        key=lambda value: (
            value["entity_hint"],
            value["market_hint"],
            value["structure_hash"],
        ),
    ):
        summary_rows.append(
            {
                **item,
                "years": ", ".join(
                    str(value)
                    for value in sorted(item["years"])
                ),
                "sheet_names": ", ".join(
                    sorted(item["sheet_names"])
                ),
                "open_methods": ", ".join(
                    sorted(item["open_methods"])
                ),
            }
        )

    with structures_path.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=structure_fields,
        )
        writer.writeheader()
        writer.writerows(summary_rows)

    status_counts: dict[str, int] = defaultdict(int)
    method_counts: dict[str, int] = defaultdict(int)

    for row in inventory_rows:
        status_counts[str(row["scan_status"])] += 1

        if row["open_method"]:
            method_counts[str(row["open_method"])] += 1

    summary = {
        "root": str(root),
        "files_found": len(files),
        "sheet_records": len(inventory_rows),
        "unique_structures": len(summary_rows),
        "status_counts": dict(sorted(status_counts.items())),
        "open_method_counts": dict(sorted(method_counts.items())),
        "inventory_csv": str(inventory_path),
        "structures_csv": str(structures_path),
    }

    summary_json_path.write_text(
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print()
    print("Escaneo finalizado.")
    print(f"Inventario: {inventory_path}")
    print(f"Estructuras únicas: {structures_path}")
    print(f"Resumen: {summary_json_path}")
    print(f"Estructuras únicas detectadas: {len(summary_rows)}")
    print("Estados:")
    for key, value in sorted(status_counts.items()):
        print(f"  {key}: {value}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
