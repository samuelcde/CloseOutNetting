from __future__ import annotations

import hashlib
import math
import os

from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg

from dotenv import load_dotenv
from psycopg import Connection


load_dotenv()


# ============================================================
# CONFIGURACIÓN
# ============================================================


def _required_env(name: str) -> str:
    value = os.getenv(name)

    if value is None or not value.strip():
        raise RuntimeError(
            f"Falta configurar la variable de entorno {name}."
        )

    return value.strip()


def get_connection() -> Connection:
    """Abre una conexión nueva a PostgreSQL usando .env."""

    return psycopg.connect(
        host=_required_env("DATABASE_HOST"),
        port=int(os.getenv("DATABASE_PORT", "5432")),
        dbname=_required_env("DATABASE_NAME"),
        user=_required_env("DATABASE_USER"),
        password=_required_env("DATABASE_PASSWORD"),
        connect_timeout=10,
        application_name="auto-closeoutnetting",
    )


# ============================================================
# HASH FÍSICO
# ============================================================


def calculate_file_sha256(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file_handle:
        while chunk := file_handle.read(64 * 1024):
            digest.update(chunk)

    return digest.hexdigest()


# ============================================================
# JSON SAFE
# ============================================================


def json_safe(value: Any) -> Any:
    """
    Convierte valores provenientes de Excel/parser a tipos que
    psycopg puede serializar de manera segura en JSONB.
    """

    if value is None:
        return None

    item_method = getattr(value, "item", None)
    if callable(item_method):
        try:
            converted = item_method()
            if converted is not value:
                return json_safe(converted)
        except (TypeError, ValueError):
            pass

    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, date):
        return value.isoformat()

    if isinstance(value, time):
        return value.isoformat()

    if isinstance(value, Decimal):
        return str(value)

    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)

        return value

    if isinstance(value, (str, int, bool)):
        return value

    if isinstance(value, bytes):
        return value.hex()

    if isinstance(value, dict):
        return {
            str(key): json_safe(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [
            json_safe(item)
            for item in value
        ]

    return str(value)


# ============================================================
# INGESTION_FILES
# ============================================================


def _normalize_source_year(source_year: int | None) -> int | None:
    if source_year is None:
        return None

    normalized = int(source_year)

    if normalized < 1900 or normalized > 2200:
        raise ValueError(
            f"source_year fuera de rango: {normalized}"
        )

    return normalized


def create_ingestion(
    connection: Connection,
    *,
    entity: str,
    source_filename: str,
    source_path: str | None = None,
    source_year: int | None = None,
    file_sha256: str | None = None,
    content_sha256: str | None = None,
    size_bytes: int | None = None,
    received_at: datetime | None = None,
    status: str = "downloaded",
) -> int:
    """
    Registra el intento de ingesta.

    received_at:
        Fecha/hora real de llegada del correo.

    processed_at:
        Permanece NULL mientras el archivo no haya finalizado
        completamente su persistencia en PostgreSQL.

    La fila de ingestion_files se conserva aunque el proceso falle.
    """

    normalized_year = _normalize_source_year(source_year)

    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO closenetting.ingestion_files (
                entity,
                source_year,
                source_filename,
                source_path,
                file_sha256,
                content_sha256,
                size_bytes,
                status,
                received_at,
                processed_at
            )
            VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, NULL
            )
            RETURNING id
            """,
            (
                entity,
                normalized_year,
                source_filename,
                source_path,
                file_sha256,
                content_sha256,
                size_bytes,
                status,
                received_at,
            ),
        )

        row = cursor.fetchone()

        if row is None:
            raise RuntimeError(
                "PostgreSQL no devolvió el ingestion_id."
            )

        return int(row[0])


def register_ingestion_attempt(
    *,
    entity: str,
    source_filename: str,
    source_path: str | None = None,
    source_year: int | None = None,
    file_sha256: str | None = None,
    content_sha256: str | None = None,
    size_bytes: int | None = None,
    received_at: datetime | None = None,
    status: str = "downloaded",
) -> int:
    """
    Crea y confirma la fila de ingestion_files antes del parseo.

    Esto permite conservar el intento aunque después ocurra parse_error,
    duplicado, error de base de datos o una persistencia incompleta.
    """

    with get_connection() as connection:
        return create_ingestion(
            connection,
            entity=entity,
            source_filename=source_filename,
            source_path=source_path,
            source_year=source_year,
            file_sha256=file_sha256,
            content_sha256=content_sha256,
            size_bytes=size_bytes,
            received_at=received_at,
            status=status,
        )


def set_ingestion_source_path(
    *,
    ingestion_id: int,
    source_path: str,
) -> None:
    """Registra la ubicación definitiva de un archivo.

    No modifica estado, hashes ni filas de operaciones.
    """
    if not source_path or not source_path.strip():
        raise ValueError("source_path no puede estar vacío")

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE closenetting.ingestion_files
                SET source_path = %s
                WHERE id = %s
                """,
                (source_path, ingestion_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"No se actualizó source_path "
                    f"de ingestion_id={ingestion_id}"
                )


def update_ingestion_status(
    connection: Connection,
    *,
    ingestion_id: int,
    status: str,
    content_sha256: str | None = None,
) -> None:
    """
    Actualiza estado y hash lógico sin modificar la fecha de finalización.
    """

    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE closenetting.ingestion_files
            SET
                status = %s,
                content_sha256 = COALESCE(%s, content_sha256)
            WHERE id = %s
            """,
            (
                status,
                content_sha256,
                ingestion_id,
            ),
        )

        if cursor.rowcount != 1:
            raise RuntimeError(
                f"No fue posible actualizar ingestion_id={ingestion_id}."
            )


def set_ingestion_status(
    *,
    ingestion_id: int,
    status: str,
    content_sha256: str | None = None,
) -> None:
    """Versión autónoma de update_ingestion_status()."""

    with get_connection() as connection:
        update_ingestion_status(
            connection,
            ingestion_id=ingestion_id,
            status=status,
            content_sha256=content_sha256,
        )


def finalize_ingestion(
    connection: Connection,
    *,
    ingestion_id: int,
    status: str,
    content_sha256: str | None = None,
) -> None:
    """
    Marca como finalizado el archivo y fija processed_at.

    Esta función solo debe llamarse cuando la persistencia completa
    en las tablas de negocio terminó correctamente.
    """

    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE closenetting.ingestion_files
            SET
                status = %s,
                content_sha256 = COALESCE(%s, content_sha256),
                processed_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (
                status,
                content_sha256,
                ingestion_id,
            ),
        )

        if cursor.rowcount != 1:
            raise RuntimeError(
                f"No fue posible finalizar ingestion_id={ingestion_id}."
            )


def find_ingestion_by_hash(
    connection: Connection,
    *,
    entity: str,
    file_sha256: str | None = None,
    content_sha256: str | None = None,
) -> tuple[int, str] | None:
    """
    Busca un archivo ya persistido en PostgreSQL por hash físico o lógico.

    Solo considera ingestas completadas (processed_at IS NOT NULL).
    Esto sirve como segunda barrera de deduplicación, especialmente útil
    si en el futuro el proyecto o PostgreSQL se trasladan a otra máquina.
    """

    conditions: list[str] = []
    params: list[Any] = [entity]

    if file_sha256:
        conditions.append("file_sha256 = %s")
        params.append(file_sha256)

    if content_sha256:
        conditions.append("content_sha256 = %s")
        params.append(content_sha256)

    if not conditions:
        return None

    where_hash = " OR ".join(conditions)

    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT id, status
            FROM closenetting.ingestion_files
            WHERE entity = %s
              AND processed_at IS NOT NULL
              AND ({where_hash})
            ORDER BY id
            LIMIT 1
            """,
            tuple(params),
        )

        row = cursor.fetchone()

    if row is None:
        return None

    return int(row[0]), str(row[1])


def find_persisted_ingestion_by_hash(
    *,
    entity: str,
    file_sha256: str | None = None,
    content_sha256: str | None = None,
) -> tuple[int, str] | None:
    """Versión autónoma de find_ingestion_by_hash."""

    with get_connection() as connection:
        return find_ingestion_by_hash(
            connection,
            entity=entity,
            file_sha256=file_sha256,
            content_sha256=content_sha256,
        )
