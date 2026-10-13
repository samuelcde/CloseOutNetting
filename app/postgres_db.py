from __future__ import annotations

import hashlib
import math
import os

from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import psycopg

from dotenv import load_dotenv
from psycopg import Connection


load_dotenv()


# ============================================================
# CONFIGURACIÓN / CONEXIÓN
# ============================================================


def _required_env(
    name: str,
) -> str:

    value = os.getenv(
        name
    )

    if (
        value is None
        or not value.strip()
    ):

        raise RuntimeError(
            f"Falta configurar la variable "
            f"de entorno {name}."
        )

    return value.strip()


def get_connection() -> Connection:
    """
    Abre una conexión nueva a PostgreSQL.

    Las credenciales se obtienen desde .env.
    """

    return psycopg.connect(
        host=_required_env(
            "DATABASE_HOST"
        ),
        port=int(
            os.getenv(
                "DATABASE_PORT",
                "5432",
            )
        ),
        dbname=_required_env(
            "DATABASE_NAME"
        ),
        user=_required_env(
            "DATABASE_USER"
        ),
        password=_required_env(
            "DATABASE_PASSWORD"
        ),
        connect_timeout=10,
        application_name=(
            "auto-closeoutnetting"
        ),
    )


# ============================================================
# HASH FÍSICO
# ============================================================


def calculate_file_sha256(
    path: Path,
) -> str:

    digest = hashlib.sha256()

    with path.open(
        "rb"
    ) as file_handle:

        while True:

            block = file_handle.read(
                1024 * 1024
            )

            if not block:
                break

            digest.update(
                block
            )

    return digest.hexdigest()


# ============================================================
# CONVERSIÓN SEGURA A JSON
# ============================================================


def json_safe(
    value: Any,
) -> Any:
    """
    Convierte valores Python, Pandas y OpenPyXL
    a valores compatibles con JSON/JSONB.

    No interpreta financieramente los valores.
    """

    if value is None:
        return None

    try:

        if pd.isna(
            value
        ):
            return None

    except (
        TypeError,
        ValueError,
    ):
        pass

    if (
        hasattr(
            value,
            "item",
        )
        and not isinstance(
            value,
            (
                str,
                bytes,
                bytearray,
            ),
        )
    ):

        try:

            value = value.item()

        except (
            TypeError,
            ValueError,
        ):
            pass

    if isinstance(
        value,
        datetime,
    ):
        return value.isoformat()

    if isinstance(
        value,
        date,
    ):
        return value.isoformat()

    if isinstance(
        value,
        time,
    ):
        return value.isoformat()

    if isinstance(
        value,
        Decimal,
    ):
        return str(
            value
        )

    if isinstance(
        value,
        float,
    ):

        if not math.isfinite(
            value
        ):
            return str(
                value
            )

        return value

    if isinstance(
        value,
        (
            str,
            int,
            bool,
        ),
    ):
        return value

    if isinstance(
        value,
        bytes,
    ):
        return value.hex()

    if isinstance(
        value,
        dict,
    ):

        return {
            str(key): json_safe(
                item
            )
            for key, item
            in value.items()
        }

    if isinstance(
        value,
        (
            list,
            tuple,
            set,
        ),
    ):

        return [
            json_safe(
                item
            )
            for item in value
        ]

    return str(
        value
    )


# ============================================================
# INGESTION_FILES
# ============================================================


def create_ingestion(
    connection: Connection,
    *,
    entity: str,
    source_filename: str,
    source_path: str | None,
    message_id: str | None = None,
    attachment_id: str | None = None,
    file_sha256: str | None = None,
    content_sha256: str | None = None,
    size_bytes: int | None = None,
    status: str = "downloaded",
) -> int:
    """
    Crea o recupera el histórico del archivo recibido.

    message_id + attachment_id identifican de forma
    idempotente el adjunto proveniente de Microsoft Graph.
    """

    with connection.cursor() as cursor:

        cursor.execute(
            """
            INSERT INTO closenetting.ingestion_files (
                entity,
                source_filename,
                source_path,
                message_id,
                attachment_id,
                file_sha256,
                content_sha256,
                size_bytes,
                status
            )
            VALUES (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s
            )

            ON CONFLICT (
                message_id,
                attachment_id
            )

            DO UPDATE SET
                entity = EXCLUDED.entity,
                source_filename = EXCLUDED.source_filename,
                source_path = EXCLUDED.source_path,
                file_sha256 = EXCLUDED.file_sha256,
                content_sha256 = EXCLUDED.content_sha256,
                size_bytes = EXCLUDED.size_bytes,
                status = EXCLUDED.status,
                processed_at = CURRENT_TIMESTAMP

            RETURNING id
            """,
            (
                entity,
                source_filename,
                source_path,
                message_id,
                attachment_id,
                file_sha256,
                content_sha256,
                size_bytes,
                status,
            ),
        )

        row = cursor.fetchone()

    if row is None:

        raise RuntimeError(
            "PostgreSQL no devolvió "
            "el ingestion_id."
        )

    return int(
        row[0]
    )


def register_ingestion_file(
    *,
    entity: str,
    source_filename: str,
    source_path: str | Path | None,
    message_id: str | None,
    attachment_id: str | None,
    file_sha256: str | None,
    content_sha256: str | None,
    size_bytes: int | None,
) -> int:
    """
    Registra el archivo en la única tabla histórica
    ingestion_files.

    No almacena operaciones financieras.
    """

    normalized_path = (
        str(source_path)
        if source_path is not None
        else None
    )

    with get_connection() as connection:

        ingestion_id = create_ingestion(
            connection,
            entity=entity,
            source_filename=(
                source_filename
            ),
            source_path=(
                normalized_path
            ),
            message_id=(
                message_id
            ),
            attachment_id=(
                attachment_id
            ),
            file_sha256=(
                file_sha256
            ),
            content_sha256=(
                content_sha256
            ),
            size_bytes=(
                size_bytes
            ),
            status="downloaded",
        )

    return ingestion_id


def update_ingestion_status(
    connection: Connection,
    *,
    ingestion_id: int,
    status: str,
    content_sha256: str | None = None,
) -> None:
    """
    Actualiza el estado de una ingesta.

    Si se proporciona content_sha256 también actualiza
    el hash lógico del contenido.
    """

    with connection.cursor() as cursor:

        cursor.execute(
            """
            UPDATE closenetting.ingestion_files
            SET
                status = %s,
                content_sha256 = COALESCE(
                    %s,
                    content_sha256
                ),
                processed_at = CURRENT_TIMESTAMP
            WHERE id = %s
            RETURNING id
            """,
            (
                status,
                content_sha256,
                ingestion_id,
            ),
        )

        row = cursor.fetchone()

    if row is None:

        raise RuntimeError(
            f"No existe ingestion_id="
            f"{ingestion_id}."
        )


def set_ingestion_status(
    *,
    ingestion_id: int,
    status: str,
    content_sha256: str | None = None,
) -> None:
    """
    Versión de alto nivel para cambiar el estado
    sin administrar manualmente la conexión.
    """

    with get_connection() as connection:

        update_ingestion_status(
            connection,
            ingestion_id=(
                ingestion_id
            ),
            status=status,
            content_sha256=(
                content_sha256
            ),
        )