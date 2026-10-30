from __future__ import annotations



import csv

import io

import logging

import re

import unicodedata

import zipfile



from dataclasses import dataclass, field

from datetime import date, datetime, time

from pathlib import Path, PurePosixPath

from typing import Any



import msoffcrypto

import pandas as pd

import xlrd



from msoffcrypto.exceptions import (

    DecryptionError as MsoDecryptionError,

    FileFormatError as MsoFileFormatError,

    InvalidKeyError as MsoInvalidKeyError,

)



from openpyxl import load_workbook



from .config import (

    ParserConfig,

    load_report_passwords,

)



from .exceptions import ReportParseError





logger = logging.getLogger(__name__)





# ============================================================

# CONSTANTES

# ============================================================



SUPPORTED_EXTENSIONS = {

    ".xlsx",

    ".xls",

    ".csv",

}





CSV_ENCODINGS = (

    "utf-8-sig",

    "utf-8",

    "cp1252",

    "latin-1",

)





CSV_MAX_FIELD_SIZE = (

    1 * 1024 * 1024

)





OfficeSource = (

    Path

    | io.BytesIO

)



HEADER_HINTS = frozenset(

    {

        # Español

        "tipo",

        "mercado",

        "hora",

        "fecha",

        "comprador",

        "vendedor",

        "compania",

        "entidad",

        "trader",

        "moneda",

        "monto",

        "precio",

        "plazo",

        "tasa",

        "periodicidad",

        "margen",

        "conteo",

        "dias",

        "operacion",

        "volatilidad",

        "camara",

        "id",



        # Inglés

        "type",

        "market",

        "time",

        "date",

        "buyer",

        "seller",

        "company",

        "entity",

        "trader",

        "currency",

        "amount",

        "price",

        "maturity",

        "rate",

        "periodicity",

        "margin",

        "day",

        "count",

        "operation",

        "volatility",

        "clearing",

        "strike",

        "id",

    }

)





# ============================================================

# MODELOS

# ============================================================



@dataclass(slots=True)

class PreparedOfficeSource:

    """

    Representa la fuente que consumirá el parser.



    Si encrypted=False:

        source será normalmente Path.



    Si encrypted=True:

        source será BytesIO con el contenido descifrado.

    """



    source: OfficeSource

    encrypted: bool





@dataclass(slots=True)

class ParsedTable:

    """

    Tabla obtenida desde una hoja Excel o CSV.



    dataframe:

        Representación normalizada para procesamiento.



    source_headers:

        Encabezados tal como fueron leídos del archivo.



    source_row_numbers:

        Número físico de cada fila en el Excel.



    source_rows:

        Valores originales asociados a cada registro.



    structural_rows:

        Filas detectadas como estructura y no como operaciones,

        por ejemplo encabezados secundarios o títulos.



    row_warnings:

        Advertencias asociadas a filas que se conservaron como datos

        porque no existe certeza suficiente para descartarlas.

    """



    name: str

    dataframe: pd.DataFrame



    source_header_row_number: int | None = None



    source_headers: tuple[Any, ...] = ()

    source_row_numbers: tuple[int, ...] = ()

    source_rows: tuple[tuple[Any, ...], ...] = ()



    structural_rows: tuple[dict[str, Any], ...] = ()



    warnings: tuple[str, ...] = ()



    row_warnings: dict[int, tuple[str, ...]] = field(

        default_factory=dict

    )



    @property

    def row_count(self) -> int:

        return len(self.dataframe.index)



    @property

    def column_count(self) -> int:

        return len(self.dataframe.columns)



    def records(self) -> list[dict[str, Any]]:

        """

        Mantiene compatibilidad con el código actual.

        """



        safe_df = self.dataframe.astype(object)



        safe_df = safe_df.where(

            pd.notna(safe_df),

            None,

        )



        return safe_df.to_dict(

            orient="records"

        )



    def raw_records(self) -> list[dict[str, Any]]:

        """

        Devuelve cada registro junto con su fila física y

        las celdas originales.



        Será la base para raw_report_rows.

        """



        result: list[dict[str, Any]] = []



        for row_number, row in zip(

            self.source_row_numbers,

            self.source_rows,

        ):

            cells: list[dict[str, Any]] = []



            for index, value in enumerate(

                row,

                start=1,

            ):

                header = (

                    self.source_headers[index - 1]

                    if index <= len(self.source_headers)

                    else None

                )



                cells.append(

                    {

                        "ordinal": index,

                        "header": header,

                        "value": value,

                    }

                )



            result.append(

                {

                    "source_row_number": row_number,

                    "source_cells": cells,

                    "warnings": list(

                        self.row_warnings.get(

                            row_number,

                            (),

                        )

                    ),

                }

            )



        return result





@dataclass(slots=True)

class ParsedReport:

    """

    Resultado completo de un archivo procesado.

    """



    source: Path

    extension: str

    tables: dict[str, ParsedTable]

    was_encrypted: bool = False



    @property

    def total_tables(self) -> int:

        return len(

            self.tables

        )



    @property

    def total_rows(self) -> int:

        return sum(

            table.row_count

            for table in self.tables.values()

        )





# ============================================================

# API PRINCIPAL

# ============================================================



def parse_report(

    file_path: Path,

    config: ParserConfig,

    *,

    entity: str,

    report_passwords: dict[str, tuple[str, ...]] | None = None,

) -> ParsedReport:

    """

    Punto de entrada único del parser.



    Soporta:

        .xlsx

        .xls

        .csv



    Los Excel protegidos son descifrados automáticamente

    en memoria mediante msoffcrypto-tool.



    Si report_passwords es None, se obtienen desde:

        REPORT_PASSWORDS

    """



    path = Path(

        file_path

    )



    if not path.exists():

        raise ReportParseError(

            f"El archivo no existe: {path}"

        )



    if not path.is_file():

        raise ReportParseError(

            f"La ruta no corresponde a un archivo: {path}"

        )



    extension = (

        path.suffix.lower()

    )



    if extension not in SUPPORTED_EXTENSIONS:

        raise ReportParseError(

            f"Formato no soportado: {extension}"

        )



    try:

        file_size = (

            path.stat().st_size

        )



    except OSError as exc:

        raise ReportParseError(

            f"No fue posible consultar el archivo: {path}"

        ) from exc



    if file_size <= 0:

        raise ReportParseError(

            "El archivo está vacío."

        )



    logger.info(

        "Parseando archivo: %s",

        path,

    )





    # CSV no usa cifrado Microsoft Office.

    if extension == ".csv":



        try:

            tables = _parse_csv(

                path,

                config,

            )



        except ReportParseError:

            raise



        except Exception as exc:

            raise ReportParseError(

                f"No fue posible interpretar "

                f"'{path.name}': "

                f"{type(exc).__name__}: {exc}"

            ) from exc



        if not tables:

            raise ReportParseError(

                f"'{path.name}' no contiene "

                "tablas con información."

            )



        report = ParsedReport(

            source=path,

            extension=extension,

            tables=tables,

            was_encrypted=False,

        )



        _log_success(

            report

        )



        return report





    # ========================================================

    # XLS / XLSX

    # ========================================================



    if report_passwords is None:

        report_passwords = load_report_passwords()



    entity_key = (

        str(entity)

        .strip()

        .upper()

    )



    entity_passwords = (

        report_passwords.get(

            entity_key,

            (),

        )

    )





    prepared = _preprocess_office_file(

        path=path,

        config=config,

        report_passwords=entity_passwords,

    )





    try:



        if extension == ".xlsx":



            tables = _parse_xlsx(

                source=prepared.source,

                display_name=path.name,

                config=config,
                entity=entity_key,

            )



        else:



            tables = _parse_xls(

                source=prepared.source,

                display_name=path.name,

                config=config,

            )



    except ReportParseError:

        raise



    except Exception as exc:



        raise ReportParseError(

            f"No fue posible interpretar "

            f"'{path.name}': "

            f"{type(exc).__name__}: {exc}"

        ) from exc



    finally:



        # Solo cerramos buffers creados por nosotros.

        if isinstance(

            prepared.source,

            io.BytesIO,

        ):

            prepared.source.close()





    if not tables:

        raise ReportParseError(

            f"'{path.name}' no contiene "

            "tablas con información."

        )





    report = ParsedReport(

        source=path,

        extension=extension,

        tables=tables,

        was_encrypted=prepared.encrypted,

    )



    _log_success(

        report

    )



    return report





def _log_success(

    report: ParsedReport,

) -> None:



    logger.info(

        "Parseo correcto: %s | "

        "cifrado=%s | "

        "tablas=%s | "

        "filas=%s",

        report.source.name,

        "SI"

        if report.was_encrypted

        else "NO",

        report.total_tables,

        report.total_rows,

    )





# ============================================================

# PREPROCESAMIENTO OFFICE / DESCIFRADO

# ============================================================



def _preprocess_office_file(

    *,

    path: Path,

    config: ParserConfig,

    report_passwords: tuple[str, ...],

) -> PreparedOfficeSource:

    """

    Determina si XLS/XLSX está protegido.



    Sin cifrado:

        retorna el Path original.



    Con cifrado:

        prueba REPORT_PASSWORDS en orden y devuelve BytesIO

        descifrado únicamente en memoria.



    Nunca registra las contraseñas.

    """



    encrypted = _is_office_encrypted(

        path

    )



    if not encrypted:



        return PreparedOfficeSource(

            source=path,

            encrypted=False,

        )





    logger.info(

        "Archivo Office cifrado detectado: %s",

        path.name,

    )





    if not report_passwords:



        raise ReportParseError(

            f"'{path.name}' está protegido con contraseña "

            "pero REPORT_PASSWORDS no contiene claves."

        )





    last_error: Exception | None = None





    for attempt_number, password in enumerate(

        report_passwords,

        start=1,

    ):



        decrypted = io.BytesIO()



        try:



            # Se vuelve a abrir el original en cada intento.

            # Un password incorrecto no afecta al siguiente.

            with path.open(

                "rb"

            ) as encrypted_handle:



                office_file = (

                    msoffcrypto.OfficeFile(

                        encrypted_handle

                    )

                )



                office_file.load_key(

                    password=password

                )



                office_file.decrypt(

                    decrypted

                )





            decrypted_size = (

                decrypted.tell()

            )





            if decrypted_size <= 0:



                raise MsoDecryptionError(

                    "El contenido descifrado está vacío."

                )





            # Límite adicional sobre el buffer en memoria.

            #

            # Utilizamos el límite expandido del parser para

            # impedir que un contenido descifrado anormalmente

            # grande continúe al motor Excel.

            if (

                decrypted_size

                > config.max_xlsx_uncompressed_bytes

            ):



                decrypted.close()



                raise ReportParseError(

                    f"'{path.name}' produjo un contenido "

                    "descifrado superior al límite permitido."

                )





            decrypted.seek(

                0

            )





            logger.info(

                "Archivo descifrado correctamente: %s "

                "(intento %s de %s)",

                path.name,

                attempt_number,

                len(report_passwords),

            )





            return PreparedOfficeSource(

                source=decrypted,

                encrypted=True,

            )





        except ReportParseError:



            decrypted.close()

            raise





        except (

            MsoInvalidKeyError,

            MsoDecryptionError,

        ) as exc:



            last_error = exc



            decrypted.close()



            # No se registra el password.

            logger.debug(

                "Contraseña no válida para '%s' "

                "en intento %s.",

                path.name,

                attempt_number,

            )



            continue





        except MsoFileFormatError as exc:



            decrypted.close()



            raise ReportParseError(

                f"'{path.name}' fue identificado como "

                "archivo Office protegido, pero su "

                "estructura no es compatible."

            ) from exc





        except Exception as exc:



            # Algunos métodos de cifrado antiguos pueden

            # producir errores de verificación distintos.

            # Permitimos probar la siguiente clave sin

            # exponer detalles sensibles.

            last_error = exc



            decrypted.close()



            logger.debug(

                "Falló intento de descifrado %s para '%s': %s",

                attempt_number,

                path.name,

                type(exc).__name__,

            )



            continue





    raise ReportParseError(

        f"'{path.name}' está cifrado y ninguna de las "

        f"{len(report_passwords)} contraseña(s) configuradas "

        "permitió descifrarlo."

    ) from last_error





def _is_office_encrypted(

    path: Path,

) -> bool:

    """

    Utiliza msoffcrypto para identificar documentos Office

    cifrados.



    Un XLSX protegido normalmente deja de verse como ZIP y

    aparece externamente como contenedor OLE; msoffcrypto

    resuelve esa capa antes de openpyxl.

    """



    try:



        with path.open(

            "rb"

        ) as file_handle:



            office_file = (

                msoffcrypto.OfficeFile(

                    file_handle

                )

            )



            return bool(

                office_file.is_encrypted()

            )





    except MsoFileFormatError:



        # Dejamos que el parser correspondiente determine

        # después si es simplemente un archivo corrupto.

        return False





    except OSError as exc:



        raise ReportParseError(

            f"No fue posible abrir '{path.name}' "

            "para verificar cifrado."

        ) from exc





    except Exception as exc:



        # Si msoffcrypto no reconoce el formato, no asumimos

        # automáticamente que esté cifrado.

        logger.debug(

            "msoffcrypto no pudo determinar cifrado de "

            "'%s': %s",

            path.name,

            type(exc).__name__,

        )



        return False





# ============================================================

# UTILIDADES PARA STREAMS

# ============================================================



def _rewind(

    source: OfficeSource,

) -> None:



    if isinstance(

        source,

        io.BytesIO,

    ):

        source.seek(

            0

        )





# ============================================================

# XLSX

# ============================================================



def _validate_xlsx_container(

    source: OfficeSource,

    config: ParserConfig,

) -> None:

    """

    Valida el XLSX ya descifrado, si aplicaba.



    Protege contra:

    - ZIP inválido;

    - ZIP bomb;

    - paths internos peligrosos;

    - relación de compresión excesiva.

    """



    _rewind(

        source

    )





    if not zipfile.is_zipfile(

        source

    ):



        _rewind(

            source

        )



        raise ReportParseError(

            "El contenido XLSX no es un ZIP válido. "

            "Puede estar corrupto, usar una contraseña "

            "incorrecta o tener un formato incompatible."

        )





    _rewind(

        source

    )





    try:



        with zipfile.ZipFile(

            source,

            "r",

        ) as archive:



            infos = (

                archive.infolist()

            )



            total_uncompressed = 0

            total_compressed = 0





            for info in infos:



                internal_path = (

                    PurePosixPath(

                        info.filename

                    )

                )



                if (

                    internal_path.is_absolute()

                    or ".." in internal_path.parts

                ):



                    raise ReportParseError(

                        "El XLSX contiene rutas internas inseguras."

                    )





                total_uncompressed += (

                    info.file_size

                )





                total_compressed += max(

                    info.compress_size,

                    1,

                )





            if (

                total_uncompressed

                > config.max_xlsx_uncompressed_bytes

            ):



                raise ReportParseError(

                    "El contenido descomprimido del XLSX "

                    "supera el límite permitido."

                )





            ratio = (

                total_uncompressed

                / max(

                    total_compressed,

                    1,

                )

            )





            if (

                ratio

                > config.max_zip_ratio

            ):



                raise ReportParseError(

                    "Relación de compresión XLSX sospechosa "

                    f"({ratio:.2f}:1)."

                )





    except zipfile.BadZipFile as exc:



        raise ReportParseError(

            "Archivo XLSX corrupto."

        ) from exc





    finally:



        _rewind(

            source

        )





def _parse_xlsx(

    *,

    source: OfficeSource,

    display_name: str,

    config: ParserConfig,
    entity: str = "",

) -> dict[str, ParsedTable]:



    _validate_xlsx_container(

        source,

        config,

    )



    _rewind(

        source

    )



    try:

        workbook = load_workbook(

            filename=source,

            read_only=True,

            data_only=True,

        )



    except Exception as exc:

        raise ReportParseError(

            f"No fue posible abrir el XLSX "

            f"'{display_name}' después del "

            "preprocesamiento."

        ) from exc



    try:

        if (

            len(workbook.sheetnames)

            > config.max_sheets_per_workbook

        ):

            raise ReportParseError(

                "El libro supera el número "

                "máximo de hojas permitido."

            )



        tables: dict[

            str,

            ParsedTable,

        ] = {}



        sheet_errors: list[str] = []



        for sheet_name in (

            workbook.sheetnames

        ):

            worksheet = workbook[

                sheet_name

            ]



            try:

                if hasattr(

                    worksheet,

                    "reset_dimensions",

                ):

                    worksheet.reset_dimensions()



                physical_rows: list[

                    tuple[

                        int,

                        list[Any],

                    ]

                ] = []



                for (

                    excel_row_number,

                    row,

                ) in enumerate(

                    worksheet.iter_rows(

                        values_only=True

                    ),

                    start=1,

                ):

                    values = (

                        _trim_trailing_empty_values(

                            row

                        )

                    )



                    if (

                        not values

                        or _is_empty_row(

                            values

                        )

                    ):

                        continue



                    if (

                        len(values)

                        > config.max_columns_per_table

                    ):

                        raise ReportParseError(

                            f"La hoja "

                            f"'{sheet_name}' contiene "

                            f"{len(values)} columnas "

                            "reales y supera el límite."

                        )



                    physical_rows.append(

                        (

                            excel_row_number,

                            list(values),

                        )

                    )



                    # Margen para:

                    # encabezados, títulos y filas

                    # estructurales adicionales.

                    if (

                        len(physical_rows)

                        >

                        (

                            config.max_rows_per_table

                            + 50

                        )

                    ):

                        raise ReportParseError(

                            f"La hoja '{sheet_name}' "

                            "supera el límite máximo "

                            "de filas analizables."

                        )



                table = _build_parsed_table(

                    sheet_name=sheet_name,

                    physical_rows=physical_rows,

                    config=config,
                    entity=entity,

                )



                if table is None:

                    continue



                tables[

                    sheet_name

                ] = table



                logger.info(

                    "Hoja '%s' procesada: "

                    "%s registros financieros "

                    "x %s columnas. "

                    "Encabezado fila=%s | "

                    "filas estructurales=%s | "

                    "advertencias=%s",

                    sheet_name,

                    table.row_count,

                    table.column_count,

                    table.source_header_row_number,

                    len(

                        table.structural_rows

                    ),

                    len(

                        table.warnings

                    ),

                )



            except ReportParseError as exc:

                sheet_errors.append(

                    f"{sheet_name}: {exc}"

                )



                logger.warning(

                    "Hoja '%s' omitida: %s",

                    sheet_name,

                    exc,

                )



            except Exception as exc:

                sheet_errors.append(

                    f"{sheet_name}: "

                    f"{type(exc).__name__}: "

                    f"{exc}"

                )



                logger.warning(

                    "Hoja '%s' omitida por error "

                    "de lectura: %s: %s",

                    sheet_name,

                    type(exc).__name__,

                    exc,

                )



        if not tables:

            if sheet_errors:

                logger.error(

                    "Ninguna hoja válida encontrada "

                    "en '%s'. Hojas con error: %s",

                    display_name,

                    len(sheet_errors),

                )



                raise ReportParseError(

                    f"El archivo '{display_name}' "

                    "no contiene ninguna hoja con "

                    "registros de datos útiles."

                )



        return tables



    finally:

        workbook.close()





# ============================================================

# XLS

# ============================================================



def _parse_xls(

    *,

    source: OfficeSource,

    display_name: str,

    config: ParserConfig,

) -> dict[str, ParsedTable]:

    """

    xlrd acepta ruta para archivos normales.



    Para contenido descifrado en BytesIO utiliza file_contents,

    manteniendo el flujo de nuestra aplicación completamente

    en memoria.

    """



    try:



        if isinstance(

            source,

            io.BytesIO,

        ):



            source.seek(

                0

            )



            workbook = xlrd.open_workbook(

                file_contents=source.getvalue(),

                on_demand=True,

            )



        else:



            workbook = xlrd.open_workbook(

                filename=str(

                    source

                ),

                on_demand=True,

            )





    except Exception as exc:



        raise ReportParseError(

            f"No fue posible abrir el XLS "

            f"'{display_name}' después del "

            "preprocesamiento."

        ) from exc





    try:



        if (

            workbook.nsheets

            > config.max_sheets_per_workbook

        ):



            raise ReportParseError(

                "El XLS supera el número máximo "

                "de hojas permitido."

            )





        tables: dict[str, ParsedTable] = {}





        for sheet_name in workbook.sheet_names():



            sheet = workbook.sheet_by_name(

                sheet_name

            )





            if (

                sheet.nrows

                > config.max_rows_per_table + 1

            ):



                raise ReportParseError(

                    f"La hoja '{sheet_name}' supera "

                    "el límite de filas."

                )





            if (

                sheet.ncols

                > config.max_columns_per_table

            ):



                raise ReportParseError(

                    f"La hoja '{sheet_name}' supera "

                    "el límite de columnas."

                )





            rows: list[list[Any]] = []





            for row_index in range(

                sheet.nrows

            ):



                row: list[Any] = []





                for column_index in range(

                    sheet.ncols

                ):



                    cell = sheet.cell(

                        row_index,

                        column_index,

                    )





                    row.append(

                        _convert_xls_cell(

                            workbook,

                            cell,

                        )

                    )





                rows.append(

                    row

                )





            dataframe = (

                _rows_to_dataframe(

                    rows

                )

            )





            if dataframe.empty:

                continue





            tables[sheet_name] = (

                ParsedTable(

                    name=sheet_name,

                    dataframe=dataframe,

                )

            )





        return tables





    finally:



        workbook.release_resources()





def _convert_xls_cell(

    workbook,

    cell,

) -> Any:



    if (

        cell.ctype

        == xlrd.XL_CELL_EMPTY

    ):

        return None





    if (

        cell.ctype

        == xlrd.XL_CELL_BOOLEAN

    ):

        return bool(

            cell.value

        )





    if (

        cell.ctype

        == xlrd.XL_CELL_ERROR

    ):

        return None





    if (

        cell.ctype

        == xlrd.XL_CELL_DATE

    ):



        try:



            return xlrd.xldate_as_datetime(

                cell.value,

                workbook.datemode,

            )



        except (

            ValueError,

            OverflowError,

        ):



            return cell.value





    return cell.value





# ============================================================

# CSV

# ============================================================



def _parse_csv(

    path: Path,

    config: ParserConfig,

) -> dict[str, ParsedTable]:



    encoding = (

        _detect_csv_encoding(

            path

        )

    )





    delimiter = (

        _detect_csv_delimiter(

            path,

            encoding,

        )

    )





    csv.field_size_limit(

        CSV_MAX_FIELD_SIZE

    )





    rows: list[list[Any]] = []





    try:



        with path.open(

            "r",

            encoding=encoding,

            newline="",

        ) as file_handle:



            reader = csv.reader(

                file_handle,

                delimiter=delimiter,

            )





            for row_number, row in enumerate(

                reader,

                start=1,

            ):



                if (

                    row_number

                    > config.max_rows_per_table + 1

                ):



                    raise ReportParseError(

                        "El CSV supera el límite "

                        "máximo de filas."

                    )





                if (

                    len(row)

                    > config.max_columns_per_table

                ):



                    raise ReportParseError(

                        "El CSV supera el límite "

                        "máximo de columnas."

                    )





                rows.append(

                    row

                )





    except ReportParseError:

        raise





    except (

        csv.Error,

        UnicodeError,

        OSError,

    ) as exc:



        raise ReportParseError(

            f"No fue posible leer el CSV: {exc}"

        ) from exc





    dataframe = (

        _rows_to_dataframe(

            rows

        )

    )





    if dataframe.empty:

        return {}





    return {

        "CSV": ParsedTable(

            name="CSV",

            dataframe=dataframe,

        )

    }





def _normalize_header_text(

    value: Any,

) -> str:

    if value is None:

        return ""



    text = unicodedata.normalize(

        "NFKD",

        str(value).strip(),

    )



    text = "".join(

        character

        for character in text

        if not unicodedata.combining(character)

    )



    text = text.lower()



    text = re.sub(

        r"[^a-z0-9]+",

        "_",

        text,

    )



    return text.strip("_")





def _looks_like_data_value(

    value: Any,

) -> bool:

    """

    Detecta valores que se parecen más a datos de negocio

    que a encabezados.

    """



    if value is None:

        return False



    if isinstance(

        value,

        (

            int,

            float,

            date,

            datetime,

            time,

        ),

    ):

        return True



    if not isinstance(value, str):

        return False



    text = value.strip()



    if not text:

        return False



    # número escrito como texto

    if re.fullmatch(

        r"[-+]?[0-9.,]+",

        text,

    ):

        return True



    # hora

    if re.fullmatch(

        r"\d{1,2}:\d{2}(:\d{2})?",

        text,

    ):

        return True



    # fecha habitual

    if re.fullmatch(

        r"\d{1,4}[/-]\d{1,2}[/-]\d{1,4}",

        text,

    ):

        return True



    return False





def _header_hint_ratio(

    values: list[Any],

) -> float:

    useful = [

        value

        for value in values

        if not _is_empty_value(value)

    ]



    if not useful:

        return 0.0



    hits = 0



    for value in useful:

        normalized = _normalize_header_text(

            value

        )



        tokens = set(

            token

            for token in normalized.split("_")

            if token

        )



        if tokens.intersection(

            HEADER_HINTS

        ):

            hits += 1



    return hits / len(useful)



def _header_like_score(

    primary_header: list[Any],

    candidate_row: list[Any],

) -> float:

    """

    Devuelve un valor entre 0 y 1.



    Cuanto más cerca de 1, más probable es que candidate_row

    sea otro encabezado y no una operación financiera.



    Importante:

    una puntuación intermedia NO elimina la fila.

    """



    primary_values = [

        value

        for value in primary_header

        if not _is_empty_value(value)

    ]



    candidate_values = [

        value

        for value in candidate_row

        if not _is_empty_value(value)

    ]



    if len(candidate_values) < 3:

        return 0.0



    text_count = sum(

        1

        for value in candidate_values

        if isinstance(value, str)

    )



    text_ratio = (

        text_count

        / len(candidate_values)

    )



    data_like_count = sum(

        1

        for value in candidate_values

        if _looks_like_data_value(value)

    )



    data_like_ratio = (

        data_like_count

        / len(candidate_values)

    )



    if primary_values:

        width_ratio = (

            min(

                len(primary_values),

                len(candidate_values),

            )

            /

            max(

                len(primary_values),

                len(candidate_values),

            )

        )

    else:

        width_ratio = 0.0



    comparable = min(

        len(primary_header),

        len(candidate_row),

    )



    exact_matches = 0



    for index in range(comparable):

        primary_text = _normalize_header_text(

            primary_header[index]

        )



        candidate_text = _normalize_header_text(

            candidate_row[index]

        )



        if (

            primary_text

            and candidate_text

            and primary_text == candidate_text

        ):

            exact_matches += 1



    exact_ratio = (

        exact_matches / comparable

        if comparable

        else 0.0

    )



    hint_ratio = _header_hint_ratio(

        candidate_row

    )



    score = (

        0.30 * text_ratio

        + 0.25 * width_ratio

        + 0.25 * max(

            hint_ratio,

            exact_ratio,

        )

        + 0.20 * (

            1.0 - data_like_ratio

        )

    )



    return max(

        0.0,

        min(

            score,

            1.0,

        ),

    )





def _primary_header_score(
    values: list[Any],
) -> float:
    useful = [
        value
        for value in values
        if not _is_empty_value(value)
    ]

    if len(useful) < 2:
        return 0.0

    text_ratio = (
        sum(
            1
            for value in useful
            if isinstance(value, str)
        )
        / len(useful)
    )

    data_ratio = (
        sum(
            1
            for value in useful
            if _looks_like_data_value(value)
        )
        / len(useful)
    )

    normalized = [
        _normalize_header_text(value)
        for value in useful
    ]

    unique_ratio = (
        len(set(normalized))
        / len(normalized)
    )

    hint_ratio = _header_hint_ratio(
        values
    )

    width_factor = min(
        len(useful) / 5,
        1.0,
    )

    score = (
        0.30 * text_ratio
        + 0.30 * hint_ratio
        + 0.20 * unique_ratio
        + 0.20 * width_factor
        - 0.30 * data_ratio
    )

    return max(
        0.0,
        min(
            score,
            1.0,
        ),
    )


def _choose_primary_header(
    physical_rows: list[
        tuple[int, list[Any]]
    ],
) -> int:
    """
    Retorna la posición del encabezado principal dentro de
    physical_rows.

    Es conservadora: solo abandona la primera fila útil si
    otra candidata de las primeras filas obtiene una
    puntuación claramente superior.
    """

    if not physical_rows:
        return 0

    scan_limit = min(
        len(physical_rows),
        8,
    )

    scores = [
        _primary_header_score(
            physical_rows[index][1]
        )
        for index in range(scan_limit)
    ]

    first_score = scores[0]

    best_position = max(
        range(len(scores)),
        key=scores.__getitem__,
    )

    best_score = scores[
        best_position
    ]

    if (
        best_position > 0
        and best_score >= 0.68
        and (
            best_score
            - first_score
        ) >= 0.15
    ):
        return best_position

    return 0


def _is_standard_english_secondary_header(primary_header: list[Any], row: list[Any]) -> bool:
    """Solo la firma exacta del encabezado bilingüe conocido de STANDARD."""
    expected = (
        ("mercado", "market"),
        ("hora_negociacion", "time_negotiation"),
        ("fecha_negociacion", "date_negotiation"),
        ("standard_chartered_comprador_vendedor", "standard_chartered_buy_sell_indicator"),
        ("compania_1_standard_chartered_entity", "party_1_standard_chartered_entity"),
    )
    if len(primary_header) < 5 or len(row) < 5:
        return False
    return tuple(
        (_normalize_header_text(primary_header[i]), _normalize_header_text(row[i]))
        for i in range(5)
    ) == expected


def _build_parsed_table(

    *,

    sheet_name: str,

    physical_rows: list[

        tuple[int, list[Any]]

    ],

    config: ParserConfig,
    entity: str = "",

) -> ParsedTable | None:

    """

    Convierte filas físicas del Excel en una tabla.



    Conserva:

    - encabezado original;

    - números físicos de fila;

    - datos fuente;

    - filas estructurales;

    - advertencias.



    No elimina columnas válidas solo porque todos

    sus datos estén vacíos.

    """



    if not physical_rows:

        return None



    header_position = (

        _choose_primary_header(

            physical_rows

        )

    )



    (

        header_row_number,

        header_values,

    ) = physical_rows[

        header_position

    ]



    structural_rows: list[

        dict[str, Any]

    ] = []



    table_warnings: list[str] = []



    # Lo que aparezca antes del encabezado se conserva

    # como estructura del documento.

    for (

        row_number,

        values,

    ) in physical_rows[

        :header_position

    ]:

        structural_rows.append(

            {

                "kind": "PREAMBLE",

                "source_row_number": row_number,

                "values": tuple(values),

            }

        )



    if header_position > 0:

        table_warnings.append(

            "PRIMARY_HEADER_NOT_FIRST_ROW"

        )



    data_candidates = physical_rows[

        header_position + 1:

    ]



    accepted_rows: list[

        tuple[int, list[Any]]

    ] = []



    row_warnings: dict[

        int,

        tuple[str, ...],

    ] = {}



    for (

        row_number,

        values,

    ) in data_candidates:



        if (entity == "STANDARD" and sheet_name.strip().lower() == "sheetone"
                and _is_standard_english_secondary_header(header_values, values)):
            structural_rows.append({
                "kind": "SECONDARY_HEADER_BILINGUAL_STANDARD",
                "source_row_number": row_number,
                "values": tuple(values),
            })
            logger.info("STANDARD: encabezado bilingüe excluido de operaciones, fila %s", row_number)
            continue

        score = _header_like_score(

            header_values,

            values,

        )



        # Alta certeza:

        # estructura, no operación.

        if score >= 0.88:

            structural_rows.append(

                {

                    "kind": "SECONDARY_HEADER",

                    "source_row_number": row_number,

                    "score": round(

                        score,

                        4,

                    ),

                    "values": tuple(values),

                }

            )



            logger.info(

                "Hoja '%s': fila estructural "

                "omitida como registro. "

                "Fila Excel=%s | score=%.2f",

                sheet_name,

                row_number,

                score,

            )



            continue



        # Hay sospecha, pero NO suficiente para borrar.

        if score >= 0.72:

            row_warnings[

                row_number

            ] = (

                "SUSPICIOUS_HEADER_LIKE_ROW",

            )



            table_warnings.append(

                "SUSPICIOUS_HEADER_LIKE_ROW"

            )



            logger.warning(

                "Hoja '%s': fila %s parece "

                "estructural (score=%.2f), "

                "pero se conservará como dato "

                "por seguridad.",

                sheet_name,

                row_number,

                score,

            )



        accepted_rows.append(

            (

                row_number,

                list(values),

            )

        )



    if not accepted_rows:

        logger.info(

            "Hoja '%s' omitida: "

            "contiene estructura pero no "

            "registros financieros.",

            sheet_name,

        )



        return None



    max_width = max(

        [

            len(header_values)

        ]

        + [

            len(values)

            for _, values

            in accepted_rows

        ]

    )



    padded_header = (

        list(header_values)

        + [None]

        * (

            max_width

            - len(header_values)

        )

    )



    padded_data = [

        (

            row_number,

            list(values)

            + [None]

            * (

                max_width

                - len(values)

            ),

        )

        for (

            row_number,

            values,

        )

        in accepted_rows

    ]



    # ----------------------------------------------------

    # COLUMNAS QUE REALMENTE EXISTEN

    #

    # Una columna con encabezado se conserva aunque todos

    # los valores estén vacíos.

    #

    # Solo se elimina una columna si:

    # - no tiene encabezado;

    # - Y tampoco tiene ningún dato.

    # ----------------------------------------------------



    column_indexes: list[int] = []



    for column_index in range(

        max_width

    ):

        header_value = padded_header[

            column_index

        ]



        has_data = any(

            not _is_empty_value(

                values[column_index]

            )

            for _, values in padded_data

        )



        if (

            _is_empty_value(

                header_value

            )

            and not has_data

        ):

            continue



        column_indexes.append(

            column_index

        )



    source_headers = tuple(

        padded_header[index]

        for index in column_indexes

    )



    source_row_numbers: list[int] = []

    source_rows: list[

        tuple[Any, ...]

    ] = []



    dataframe_rows: list[

        list[Any]

    ] = []



    for (

        row_number,

        values,

    ) in padded_data:



        selected_values = [

            values[index]

            for index in column_indexes

        ]



        normalized_values = [

            _normalize_value(value)

            for value in selected_values

        ]



        # Una fila que finalmente sea totalmente vacía

        # no representa un registro.

        if all(

            value is None

            for value

            in normalized_values

        ):

            continue



        source_row_numbers.append(

            row_number

        )



        source_rows.append(

            tuple(

                selected_values

            )

        )



        dataframe_rows.append(

            normalized_values

        )



        if (

            len(dataframe_rows)

            > config.max_rows_per_table

        ):

            raise ReportParseError(

                f"La hoja '{sheet_name}' "

                "supera el límite de registros."

            )



    if not dataframe_rows:

        return None



    if (

        len(source_headers)

        > config.max_columns_per_table

    ):

        raise ReportParseError(

            f"La hoja '{sheet_name}' "

            "supera el límite de columnas."

        )



    normalized_columns = (

        _normalize_columns(

            list(source_headers)

        )

    )



    dataframe = pd.DataFrame(

        dataframe_rows,

        columns=normalized_columns,

    )



    dataframe = (

        dataframe.convert_dtypes()

    )



    return ParsedTable(

        name=sheet_name,

        dataframe=dataframe,



        source_header_row_number=(

            header_row_number

        ),



        source_headers=source_headers,



        source_row_numbers=tuple(

            source_row_numbers

        ),



        source_rows=tuple(

            source_rows

        ),



        structural_rows=tuple(

            structural_rows

        ),



        warnings=tuple(

            dict.fromkeys(

                table_warnings

            )

        ),



        row_warnings=row_warnings,

    )



def _detect_csv_encoding(

    path: Path,

) -> str:



    try:



        sample = path.read_bytes()[

            :128 * 1024

        ]



    except OSError as exc:



        raise ReportParseError(

            f"No fue posible leer '{path.name}'."

        ) from exc





    if b"\x00" in sample:



        raise ReportParseError(

            "El supuesto CSV contiene bytes NUL; "

            "puede no ser un archivo de texto válido."

        )





    for encoding in CSV_ENCODINGS:



        try:



            sample.decode(

                encoding

            )



            return encoding





        except UnicodeDecodeError:

            continue





    raise ReportParseError(

        "No fue posible determinar "

        "la codificación del CSV."

    )





def _detect_csv_delimiter(

    path: Path,

    encoding: str,

) -> str:



    try:



        with path.open(

            "r",

            encoding=encoding,

            newline="",

        ) as file_handle:



            sample = file_handle.read(

                64 * 1024

            )





    except OSError as exc:



        raise ReportParseError(

            f"No fue posible leer '{path.name}'."

        ) from exc





    try:



        dialect = csv.Sniffer().sniff(

            sample,

            delimiters=",;\t|",

        )



        return dialect.delimiter





    except csv.Error:



        logger.warning(

            "No fue posible detectar el delimitador "

            "de '%s'. Se usará coma.",

            path.name,

        )



        return ","





# ============================================================

# NORMALIZACIÓN

# ============================================================



def _rows_to_dataframe(

    rows: list[list[Any]],

) -> pd.DataFrame:

    """

    Convierte las filas reales en un DataFrame normalizado.



    Reglas:

    - elimina filas completamente vacías;

    - elimina celdas vacías fantasma a la derecha;

    - conserva celdas vacías internas;

    - utiliza la primera fila útil como encabezado;

    - elimina columnas completamente vacías;

    - elimina filas de datos completamente vacías.

    """



    # --------------------------------------------------------

    # NORMALIZACIÓN DEFENSIVA

    #

    # Aunque _parse_xlsx ya limpia las filas, esta función

    # puede ser reutilizada por XLS/CSV.

    # --------------------------------------------------------



    clean_rows: list[list[Any]] = []



    for row in rows:



        trimmed_row = (

            _trim_trailing_empty_values(

                row

            )

        )



        if (

            not trimmed_row

            or _is_empty_row(

                trimmed_row

            )

        ):

            continue



        clean_rows.append(

            trimmed_row

        )



    if not clean_rows:

        return pd.DataFrame()



    # --------------------------------------------------------

    # Calcular ancho REAL.

    # --------------------------------------------------------



    max_width = max(

        len(row)

        for row in clean_rows

    )



    # --------------------------------------------------------

    # Homogeneizar longitudes.

    #

    # Los None agregados aquí son estructurales, no provienen

    # de estilos fantasma del Excel.

    # --------------------------------------------------------



    padded_rows = [



        list(row)

        + [None] * (

            max_width

            - len(row)

        )



        for row in clean_rows

    ]



    # --------------------------------------------------------

    # ENCABEZADO

    # --------------------------------------------------------



    raw_header = (

        padded_rows[0]

    )



    columns = (

        _normalize_columns(

            raw_header

        )

    )



    data_rows = (

        padded_rows[1:]

    )



    # Una hoja únicamente con encabezado produce DataFrame

    # vacío.

    if not data_rows:



        return pd.DataFrame(

            columns=columns

        )



    # --------------------------------------------------------

    # DATAFRAME

    # --------------------------------------------------------



    dataframe = pd.DataFrame(

        data_rows,

        columns=columns,

    )



    dataframe = dataframe.apply(

        lambda column: column.map(

            _normalize_value

        )

    )



    # --------------------------------------------------------

    # FILAS SIN DATOS

    #

    # Si Excel contiene:

    #

    # bordes

    # estilos

    # fórmulas vacías

    # espacios

    #

    # después de la normalización estas filas desaparecen.

    # --------------------------------------------------------



    dataframe = dataframe.dropna(

        axis=0,

        how="all",

    )



    # --------------------------------------------------------

    # COLUMNAS SIN DATOS

    #

    # Muy importante para las columnas fantasma Y/Z/etc.

    # --------------------------------------------------------



    dataframe = dataframe.dropna(

        axis=1,

        how="all",

    )



    dataframe = dataframe.reset_index(

        drop=True

    )



    return dataframe.convert_dtypes()





def _normalize_value(

    value: Any,

) -> Any:



    if value is None:

        return None





    if isinstance(

        value,

        str,

    ):



        normalized = (

            unicodedata.normalize(

                "NFKC",

                value,

            ).strip()

        )





        return (

            normalized

            if normalized

            else None

        )





    return value





def _is_empty_row(

    row: list[Any],

) -> bool:



    return all(



        value is None



        or (

            isinstance(

                value,

                str,

            )

            and not value.strip()

        )



        for value in row

    )



def _is_empty_value(

    value: Any,

) -> bool:

    """

    Determina si una celda debe considerarse vacía.



    Se consideran vacíos:

        None

        ""

        "   "

        tabs / saltos de línea sin contenido

    """



    if value is None:

        return True



    if isinstance(

        value,

        str,

    ):

        return not value.strip()



    return False



def _trim_trailing_empty_values(

    row,

) -> list[Any]:

    """

    Elimina únicamente celdas vacías situadas a la derecha.



    No elimina celdas vacías internas porque pueden representar

    una columna real del reporte.



    Ejemplo:



        ["TRD001", None, 1500, None, None, " "]



    se convierte en:



        ["TRD001", None, 1500]



    La celda None interna se conserva.

    """



    values = list(

        row

    )



    while (

        values

        and _is_empty_value(

            values[-1]

        )

    ):

        values.pop()



    return values





def _normalize_columns(

    values: list[Any],

) -> list[str]:



    result: list[str] = []



    counters: dict[str, int] = {}





    for index, value in enumerate(

        values,

        start=1,

    ):



        column = (

            _normalize_column_name(

                value,

                index,

            )

        )





        count = counters.get(

            column,

            0,

        )





        counters[column] = (

            count + 1

        )





        if count:



            column = (

                f"{column}_{count + 1}"

            )





        result.append(

            column

        )





    return result





def _normalize_column_name(

    value: Any,

    index: int,

) -> str:



    if value is None:



        return (

            f"column_{index}"

        )





    text = unicodedata.normalize(

        "NFKD",

        str(value).strip(),

    )





    text = "".join(

        char

        for char in text

        if not unicodedata.combining(

            char

        )

    )





    text = text.lower()





    text = re.sub(

        r"[^a-z0-9]+",

        "_",

        text,

    )





    text = text.strip(

        "_"

    )





    if not text:



        text = (

            f"column_{index}"

        )





    if text[0].isdigit():



        text = (

            f"col_{text}"

        )





    return text