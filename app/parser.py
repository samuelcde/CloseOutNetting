from __future__ import annotations

import csv
import io
import logging
import re
import unicodedata
import zipfile

from dataclasses import dataclass
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
    Tabla normalizada obtenida desde una hoja Excel o CSV.
    """

    name: str
    dataframe: pd.DataFrame

    @property
    def row_count(self) -> int:
        return len(
            self.dataframe.index
        )

    @property
    def column_count(self) -> int:
        return len(
            self.dataframe.columns
        )

    def records(
        self,
    ) -> list[dict[str, Any]]:
        """
        Convierte a registros aptos para una futura inserción
        en base de datos.

        NaN / NaT / pd.NA -> None
        """

        safe_df = (
            self.dataframe
            .astype(object)
        )

        safe_df = safe_df.where(
            pd.notna(
                safe_df
            ),
            None,
        )

        return safe_df.to_dict(
            orient="records"
        )


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
) -> dict[str, ParsedTable]:
    """
    Lee un XLSX validando únicamente las filas y columnas
    que contienen información real.

    No utiliza worksheet.max_row/max_column como criterio
    de rechazo, ya que Excel puede reportar dimensiones
    fantasma debido a estilos, bordes o formatos aplicados
    sobre filas/columnas vacías.

    Una hoja inválida o sin registros no invalida el libro
    completo si existe al menos otra hoja con datos útiles.
    """

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

        # ----------------------------------------------------
        # El límite de hojas sí puede validarse directamente.
        # ----------------------------------------------------

        if (
            len(workbook.sheetnames)
            > config.max_sheets_per_workbook
        ):
            raise ReportParseError(
                "El libro supera el número máximo "
                "de hojas permitido."
            )

        tables: dict[str, ParsedTable] = {}

        sheet_errors: list[str] = []

        # ----------------------------------------------------
        # Procesar cada hoja independientemente.
        # ----------------------------------------------------

        for sheet_name in workbook.sheetnames:

            worksheet = workbook[
                sheet_name
            ]

            try:

                # ------------------------------------------------
                # IMPORTANTE:
                #
                # En modo read_only openpyxl puede confiar en las
                # dimensiones declaradas dentro del XML.
                #
                # Algunos archivos reales declaran:
                #
                # A1:XFD1048576
                #
                # simplemente porque existen estilos/bordes.
                #
                # reset_dimensions() obliga a ignorar ese dato
                # pre-calculado y evita utilizarlo como límite
                # lógico de contenido.
                # ------------------------------------------------

                if hasattr(
                    worksheet,
                    "reset_dimensions",
                ):
                    worksheet.reset_dimensions()

                rows: list[list[Any]] = []

                header_found = False

                real_data_rows = 0

                # ------------------------------------------------
                # Iteración real del contenido.
                # ------------------------------------------------

                for row in worksheet.iter_rows(
                    values_only=True
                ):

                    # Eliminar None / espacios únicamente del
                    # extremo derecho.
                    #
                    # Ejemplo:
                    #
                    # ["A", "B", None, None, " ", None]
                    #
                    # pasa a:
                    #
                    # ["A", "B"]
                    #
                    values = _trim_trailing_empty_values(
                        row
                    )

                    # ------------------------------------------------
                    # Filas completamente vacías no existen para
                    # efectos del parser.
                    # ------------------------------------------------

                    if (
                        not values
                        or _is_empty_row(
                            values
                        )
                    ):
                        continue

                    # ------------------------------------------------
                    # El límite de columnas se aplica únicamente
                    # después de quitar columnas fantasma a la
                    # derecha.
                    # ------------------------------------------------

                    real_column_count = len(
                        values
                    )

                    if (
                        real_column_count
                        > config.max_columns_per_table
                    ):
                        raise ReportParseError(
                            f"La hoja '{sheet_name}' "
                            f"contiene {real_column_count} "
                            "columnas reales y supera el "
                            f"límite de "
                            f"{config.max_columns_per_table}."
                        )

                    # ------------------------------------------------
                    # La primera fila REAL se considera encabezado.
                    #
                    # No cuenta para max_rows_per_table.
                    # ------------------------------------------------

                    if not header_found:

                        rows.append(
                            values
                        )

                        header_found = True

                        continue

                    # ------------------------------------------------
                    # A partir de aquí únicamente contamos filas
                    # reales de datos.
                    # ------------------------------------------------

                    real_data_rows += 1

                    if (
                        real_data_rows
                        > config.max_rows_per_table
                    ):
                        raise ReportParseError(
                            f"La hoja '{sheet_name}' "
                            "supera el límite de "
                            f"{config.max_rows_per_table} "
                            "filas reales de datos."
                        )

                    rows.append(
                        values
                    )

                # ------------------------------------------------
                # HOJA COMPLETAMENTE VACÍA
                # ------------------------------------------------

                if not header_found:

                    logger.info(
                        "Hoja '%s' omitida: "
                        "no contiene registros de datos.",
                        sheet_name,
                    )

                    continue

                # ------------------------------------------------
                # HEADER-ONLY
                #
                # Encontramos una fila de encabezados pero ninguna
                # fila real de transacciones.
                # ------------------------------------------------

                if real_data_rows == 0:

                    logger.info(
                        "Hoja '%s' omitida: "
                        "no contiene registros de datos.",
                        sheet_name,
                    )

                    continue

                # ------------------------------------------------
                # NORMALIZACIÓN
                # ------------------------------------------------

                dataframe = (
                    _rows_to_dataframe(
                        rows
                    )
                )

                # Después de normalizar podría ocurrir que todas
                # las filas de datos sean realmente vacías.
                if dataframe.empty:

                    logger.info(
                        "Hoja '%s' omitida: "
                        "no contiene registros de datos.",
                        sheet_name,
                    )

                    continue

                # ------------------------------------------------
                # Defensa adicional:
                # validar dimensiones REALES del DataFrame.
                # ------------------------------------------------

                if (
                    len(dataframe.index)
                    > config.max_rows_per_table
                ):
                    raise ReportParseError(
                        f"La hoja '{sheet_name}' "
                        "supera el límite de filas "
                        "reales permitido."
                    )

                if (
                    len(dataframe.columns)
                    > config.max_columns_per_table
                ):
                    raise ReportParseError(
                        f"La hoja '{sheet_name}' "
                        "supera el límite de columnas "
                        "reales permitido."
                    )

                # ------------------------------------------------
                # HOJA VÁLIDA
                # ------------------------------------------------

                tables[
                    sheet_name
                ] = ParsedTable(
                    name=sheet_name,
                    dataframe=dataframe,
                )

                logger.info(
                    "Hoja '%s' procesada: "
                    "%s registros x %s columnas.",
                    sheet_name,
                    len(dataframe.index),
                    len(dataframe.columns),
                )

            # ----------------------------------------------------
            # Un problema en UNA hoja no invalida automáticamente
            # el workbook completo.
            # ----------------------------------------------------

            except ReportParseError as exc:

                sheet_errors.append(
                    f"{sheet_name}: {exc}"
                )

                logger.warning(
                    "Hoja '%s' omitida: %s",
                    sheet_name,
                    exc,
                )

                continue

            except Exception as exc:

                sheet_errors.append(
                    f"{sheet_name}: "
                    f"{type(exc).__name__}: {exc}"
                )

                logger.warning(
                    "Hoja '%s' omitida por error "
                    "de lectura: %s: %s",
                    sheet_name,
                    type(exc).__name__,
                    exc,
                )

                continue

        # --------------------------------------------------------
        # REGLA GLOBAL
        #
        # El libro únicamente falla si NINGUNA hoja produjo una
        # tabla útil.
        # --------------------------------------------------------

        if not tables:

            if sheet_errors:

                logger.error(
                    "Ninguna hoja válida encontrada en '%s'. "
                    "Hojas con error: %s",
                    display_name,
                    len(sheet_errors),
                )

            raise ReportParseError(
                f"El archivo '{display_name}' no contiene "
                "ninguna hoja con registros de datos útiles."
            )

        # --------------------------------------------------------
        # Si tenemos al menos una hoja válida, el archivo completo
        # se considera procesable aunque otras pestañas hayan sido
        # omitidas.
        # --------------------------------------------------------

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