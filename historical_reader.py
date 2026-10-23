from __future__ import annotations

import csv
import io
import json
import os
import re

from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterator


@dataclass(frozen=True, slots=True)
class InventorySheet:
    source_year: int | None
    relative_path: str
    filename: str
    extension: str
    size_bytes: int | None
    file_sha256: str
    entity_hint: str
    market_hint: str
    sheet_name: str
    header_row: int | None
    structure_hash: str
    headers: tuple[str, ...]
    normalized_headers: tuple[str, ...]
    scan_status: str
    open_method: str
    error: str


def _safe_int(value: Any) -> int | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(float(str(value).strip()))
    except Exception:
        return None


def _json_list(value: Any) -> tuple[str, ...]:
    try:
        parsed = json.loads(str(value))
        if isinstance(parsed, list):
            return tuple("" if x is None else str(x) for x in parsed)
    except Exception:
        pass
    return ()


def read_inventory(path: Path) -> list[InventorySheet]:
    rows: list[InventorySheet] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows.append(
                InventorySheet(
                    source_year=_safe_int(row.get("source_year")),
                    relative_path=str(row.get("relative_path") or "").strip(),
                    filename=str(row.get("filename") or "").strip(),
                    extension=str(row.get("extension") or "").strip().lower(),
                    size_bytes=_safe_int(row.get("size_bytes")),
                    file_sha256=str(row.get("file_sha256") or "").strip(),
                    entity_hint=str(row.get("entity_hint") or "DESCONOCIDA").strip(),
                    market_hint=str(row.get("market_hint") or "DESCONOCIDO").strip(),
                    sheet_name=str(row.get("sheet_name") or ""),
                    header_row=_safe_int(row.get("header_row")),
                    structure_hash=str(row.get("structure_hash") or "").strip(),
                    headers=_json_list(row.get("headers") or "[]"),
                    normalized_headers=_json_list(row.get("normalized_headers") or "[]"),
                    scan_status=str(row.get("scan_status") or "").strip(),
                    open_method=str(row.get("open_method") or "").strip(),
                    error=str(row.get("error") or "").strip(),
                )
            )
    return rows


def group_by_file(rows: list[InventorySheet]) -> dict[str, list[InventorySheet]]:
    result: dict[str, list[InventorySheet]] = {}
    for row in rows:
        result.setdefault(row.relative_path, []).append(row)
    return result


def load_report_passwords() -> dict[str, tuple[str, ...]]:
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

    result: dict[str, tuple[str, ...]] = {}
    if not isinstance(parsed, dict):
        return result

    for key, values in parsed.items():
        if not isinstance(values, list):
            continue
        result[str(key).strip().upper()] = tuple(
            str(value).strip()
            for value in values
            if str(value).strip()
        )
    return result


PASSWORD_ALIASES = {
    "MORGAN_STANLEY": ("MORGAN_STANLEY", "MORGAN", "MS"),
    "GS": ("GS", "GOLDMAN_SACHS", "GOLDMAN"),
    "STANDARD": ("STANDARD", "STANDARD_CHARTERED", "SCB"),
    "FICC": ("FICC", "BAML", "BANK_OF_AMERICA", "BOFA"),
    "CITI": ("CITI", "CITIBANK"),
    "SMBC": ("SMBC", "SUMITOMO"),
    "BBVA": ("BBVA",),
    "BLADEX": ("BLADEX",),
    "SET-ICAP": ("SET-ICAP", "SET_ICAP"),
}


def passwords_for_entity(
    passwords: dict[str, tuple[str, ...]],
    entity: str,
) -> tuple[str, ...]:
    found: list[str] = []
    for key in PASSWORD_ALIASES.get(entity, (entity,)):
        for value in passwords.get(key, ()):
            if value not in found:
                found.append(value)
    return tuple(found)


def decrypt_office(path: Path, passwords: tuple[str, ...]) -> bytes:
    try:
        import msoffcrypto
    except ImportError as exc:
        raise RuntimeError(
            "Falta msoffcrypto-tool. Ejecute: "
            "python -m pip install msoffcrypto-tool"
        ) from exc

    if not passwords:
        raise RuntimeError("PASSWORD_REQUIRED")

    last_error: Exception | None = None
    for password in passwords:
        try:
            with path.open("rb") as source:
                office = msoffcrypto.OfficeFile(source)
                office.load_key(password=password)
                target = io.BytesIO()
                office.decrypt(target)
                return target.getvalue()
        except Exception as exc:
            last_error = exc

    raise RuntimeError("PASSWORD_FAILED") from last_error


class _HTMLTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self.table: list[list[str]] | None = None
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.in_cell = False

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag == "table":
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []
            self.in_cell = True

    def handle_data(self, data: str) -> None:
        if self.in_cell and self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self.row is not None:
            value = re.sub(r"\s+", " ", "".join(self.cell or [])).strip()
            self.row.append(value)
            self.cell = None
            self.in_cell = False
        elif tag == "tr" and self.table is not None:
            if self.row is not None and any(str(x).strip() for x in self.row):
                self.table.append(self.row)
            self.row = None
        elif tag == "table":
            if self.table:
                self.tables.append(self.table)
            self.table = None


def _decode_html(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise RuntimeError("No fue posible decodificar HTML.")


def _iter_html(
    data: bytes,
    sheet: InventorySheet,
) -> Iterator[list[Any]]:
    parser = _HTMLTableParser()
    parser.feed(_decode_html(data))

    match = re.fullmatch(
        r"HTML_TABLE_(\d+)",
        sheet.sheet_name.strip(),
        flags=re.IGNORECASE,
    )
    index = int(match.group(1)) - 1 if match else 0
    if index < 0 or index >= len(parser.tables):
        raise RuntimeError(f"No existe tabla HTML {sheet.sheet_name!r}.")

    start = int(sheet.header_row or 1)
    for row in parser.tables[index][start:]:
        yield list(row)


def _iter_openpyxl(
    source: Path | io.BytesIO,
    sheet: InventorySheet,
) -> Iterator[list[Any]]:
    from openpyxl import load_workbook

    workbook = load_workbook(source, read_only=True, data_only=True)
    try:
        if sheet.sheet_name not in workbook.sheetnames:
            raise RuntimeError(f"No existe hoja {sheet.sheet_name!r}.")

        worksheet = workbook[sheet.sheet_name]
        consecutive_blank = 0
        data_seen = False

        for row in worksheet.iter_rows(
            min_row=int(sheet.header_row or 1) + 1,
            values_only=True,
        ):
            values = list(row)
            blank = not any(
                value is not None and str(value).strip() != ""
                for value in values
            )

            if blank:
                if data_seen:
                    consecutive_blank += 1
                    if consecutive_blank >= 50:
                        break
                continue

            data_seen = True
            consecutive_blank = 0
            yield values
    finally:
        workbook.close()


def _xlrd_value(workbook, cell):
    import xlrd

    if cell.ctype in {xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK}:
        return None
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(cell.value)
    if cell.ctype == xlrd.XL_CELL_DATE:
        try:
            return xlrd.xldate_as_datetime(cell.value, workbook.datemode)
        except Exception:
            return cell.value
    if cell.ctype == xlrd.XL_CELL_ERROR:
        return None
    return cell.value


def _iter_xlrd(
    source: Path | bytes,
    sheet: InventorySheet,
) -> Iterator[list[Any]]:
    import xlrd

    if isinstance(source, Path):
        workbook = xlrd.open_workbook(source, on_demand=True)
    else:
        workbook = xlrd.open_workbook(file_contents=source, on_demand=True)

    try:
        if sheet.sheet_name not in workbook.sheet_names():
            raise RuntimeError(f"No existe hoja {sheet.sheet_name!r}.")

        ws = workbook.sheet_by_name(sheet.sheet_name)
        start = int(sheet.header_row or 1)
        consecutive_blank = 0
        data_seen = False

        for ridx in range(start, ws.nrows):
            values = [
                _xlrd_value(workbook, ws.cell(ridx, cidx))
                for cidx in range(ws.ncols)
            ]
            blank = not any(
                value is not None and str(value).strip() != ""
                for value in values
            )

            if blank:
                if data_seen:
                    consecutive_blank += 1
                    if consecutive_blank >= 50:
                        break
                continue

            data_seen = True
            consecutive_blank = 0
            yield values
    finally:
        workbook.release_resources()


def _iter_csv(
    path: Path,
    sheet: InventorySheet,
) -> Iterator[list[Any]]:
    method = sheet.open_method
    encoding = method[4:] if method.upper().startswith("CSV_") else "utf-8-sig"
    encodings = list(dict.fromkeys([encoding, "utf-8-sig", "utf-8", "cp1252", "latin-1"]))
    last_error = None

    for current in encodings:
        try:
            with path.open("r", encoding=current, newline="") as handle:
                sample = handle.read(65536)
                handle.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
                except csv.Error:
                    dialect = csv.excel

                reader = csv.reader(handle, dialect)
                for idx, row in enumerate(reader, start=1):
                    if idx <= int(sheet.header_row or 1):
                        continue
                    if not any(str(value).strip() for value in row):
                        continue
                    yield list(row)
            return
        except Exception as exc:
            last_error = exc

    raise RuntimeError(f"No fue posible leer CSV: {last_error}")


def iter_sheet_rows(
    root: Path,
    sheet: InventorySheet,
    entity: str,
    password_map: dict[str, tuple[str, ...]],
) -> Iterator[list[Any]]:
    path = root / Path(sheet.relative_path)
    if not path.exists():
        raise FileNotFoundError(str(path))

    method = sheet.open_method.upper()

    if method in {"HTML_XLS", "HTML_XLSX"}:
        yield from _iter_html(path.read_bytes(), sheet)
        return

    if method.startswith("CSV"):
        yield from _iter_csv(path, sheet)
        return

    if method == "OPENPYXL":
        yield from _iter_openpyxl(path, sheet)
        return

    if method == "XLRD":
        yield from _iter_xlrd(path, sheet)
        return

    if method.startswith("MSOFFCRYPTO"):
        decrypted = decrypt_office(
            path,
            passwords_for_entity(password_map, entity),
        )

        if "OPENPYXL" in method or decrypted.startswith(b"PK\x03\x04"):
            yield from _iter_openpyxl(io.BytesIO(decrypted), sheet)
            return

        if "XLRD" in method:
            yield from _iter_xlrd(decrypted, sheet)
            return

        lower = decrypted.lstrip().lower()
        if lower.startswith(b"<html") or lower.startswith(b"<!doctype"):
            yield from _iter_html(decrypted, sheet)
            return

        yield from _iter_xlrd(decrypted, sheet)
        return

    # Fallback por extensión.
    if sheet.extension in {".xlsx", ".xlsm"}:
        yield from _iter_openpyxl(path, sheet)
    elif sheet.extension == ".xls":
        raw = path.read_bytes()
        lower = raw.lstrip().lower()
        if lower.startswith(b"<html") or lower.startswith(b"<!doctype"):
            yield from _iter_html(raw, sheet)
        else:
            yield from _iter_xlrd(path, sheet)
    elif sheet.extension == ".csv":
        yield from _iter_csv(path, sheet)
    else:
        raise RuntimeError(f"Formato no soportado: {sheet.extension}")
