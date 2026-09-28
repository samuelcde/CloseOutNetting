from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .config import STATE_DB


@dataclass(frozen=True, slots=True)
class ProcessedAttachmentMatch:
    message_id: str
    attachment_id: str
    entity: str
    filename: str
    local_path: str | None
    size_bytes: int | None
    sha256: str | None
    content_sha256: str | None
    status: str
    processed_at: str


def initialize_database() -> None:
    with sqlite3.connect(STATE_DB) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_attachments (
                message_id TEXT NOT NULL,
                attachment_id TEXT NOT NULL,
                entity TEXT NOT NULL,
                filename TEXT NOT NULL,
                local_path TEXT,
                size_bytes INTEGER,
                sha256 TEXT,
                content_sha256 TEXT,
                status TEXT NOT NULL DEFAULT 'processed',
                duplicate_of_message_id TEXT,
                duplicate_of_attachment_id TEXT,
                processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (message_id, attachment_id)
            )
            """
        )

        existing_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(processed_attachments)")
        }

        optional_columns = {
            "local_path": "TEXT",
            "size_bytes": "INTEGER",
            "sha256": "TEXT",
            "content_sha256": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'processed'",
            "duplicate_of_message_id": "TEXT",
            "duplicate_of_attachment_id": "TEXT",
        }

        for name, sql_type in optional_columns.items():
            if name not in existing_columns:
                connection.execute(
                    f"""
                    ALTER TABLE processed_attachments
                    ADD COLUMN {name} {sql_type}
                    """
                )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_processed_entity_sha256
            ON processed_attachments (entity, sha256)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_processed_entity_content_sha256
            ON processed_attachments (entity, content_sha256)
            """
        )
        connection.commit()


def is_processed(message_id: str, attachment_id: str) -> bool:
    """
    Indica si este adjunto de Microsoft Graph ya fue visto anteriormente.

    La existencia del par (message_id, attachment_id) es suficiente
    para impedir una nueva descarga, independientemente del estado:

    - processed
    - duplicate_binary
    - duplicate_content
    - parse_error

    Los archivos con parse_error podrán reprocesarse posteriormente
    desde su copia local, sin volver a descargarlos desde Graph.
    """

    with sqlite3.connect(STATE_DB) as connection:
        row = connection.execute(
            """
            SELECT 1
            FROM processed_attachments
            WHERE message_id = ?
              AND attachment_id = ?
            LIMIT 1
            """,
            (message_id, attachment_id),
        ).fetchone()

    return row is not None


def _find_match(column: str, entity: str, value: str) -> ProcessedAttachmentMatch | None:
    if not value:
        return None

    if column not in {"sha256", "content_sha256"}:
        raise ValueError("Columna de búsqueda no permitida.")

    with sqlite3.connect(STATE_DB) as connection:
        row = connection.execute(
            f"""
            SELECT
                message_id,
                attachment_id,
                entity,
                filename,
                local_path,
                size_bytes,
                sha256,
                content_sha256,
                COALESCE(status, 'processed'),
                processed_at
            FROM processed_attachments
            WHERE entity = ?
              AND {column} = ?
              AND COALESCE(status, 'processed') IN (
                  'processed',
                  'duplicate_binary',
                  'duplicate_content'
              )
            ORDER BY
                CASE
                    WHEN COALESCE(status, 'processed') = 'processed' THEN 0
                    ELSE 1
                END,
                processed_at ASC
            LIMIT 1
            """,
            (entity, value),
        ).fetchone()

    return ProcessedAttachmentMatch(*row) if row is not None else None


def find_by_file_sha256(entity: str, sha256: str) -> ProcessedAttachmentMatch | None:
    return _find_match("sha256", entity, sha256)


def find_by_content_sha256(
    entity: str,
    content_sha256: str,
) -> ProcessedAttachmentMatch | None:
    return _find_match("content_sha256", entity, content_sha256)


def mark_processed(
    *,
    message_id: str,
    attachment_id: str,
    entity: str,
    filename: str,
    local_path: Path | None,
    size_bytes: int,
    sha256: str,
    content_sha256: str | None = None,
    status: str = "processed",
    duplicate_of_message_id: str | None = None,
    duplicate_of_attachment_id: str | None = None,
) -> None:
    """
    Inserta o actualiza el estado del adjunto.

    El UPSERT permite que un registro que antes quedó en parse_error
    pase posteriormente a processed/duplicate tras corregir la causa.
    """
    with sqlite3.connect(STATE_DB) as connection:
        connection.execute(
            """
            INSERT INTO processed_attachments (
                message_id,
                attachment_id,
                entity,
                filename,
                local_path,
                size_bytes,
                sha256,
                content_sha256,
                status,
                duplicate_of_message_id,
                duplicate_of_attachment_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id, attachment_id) DO UPDATE SET
                entity = excluded.entity,
                filename = excluded.filename,
                local_path = excluded.local_path,
                size_bytes = excluded.size_bytes,
                sha256 = excluded.sha256,
                content_sha256 = excluded.content_sha256,
                status = excluded.status,
                duplicate_of_message_id = excluded.duplicate_of_message_id,
                duplicate_of_attachment_id = excluded.duplicate_of_attachment_id,
                processed_at = CURRENT_TIMESTAMP
            """,
            (
                message_id,
                attachment_id,
                entity,
                filename,
                str(local_path) if local_path is not None else None,
                size_bytes,
                sha256,
                content_sha256,
                status,
                duplicate_of_message_id,
                duplicate_of_attachment_id,
            ),
        )
        connection.commit()


def update_content_sha256(
    *,
    message_id: str,
    attachment_id: str,
    content_sha256: str,
) -> None:
    with sqlite3.connect(STATE_DB) as connection:
        connection.execute(
            """
            UPDATE processed_attachments
            SET content_sha256 = ?
            WHERE message_id = ?
              AND attachment_id = ?
            """,
            (content_sha256, message_id, attachment_id),
        )
        connection.commit()


def list_without_content_sha256() -> list[ProcessedAttachmentMatch]:
    with sqlite3.connect(STATE_DB) as connection:
        rows = connection.execute(
            """
            SELECT
                message_id,
                attachment_id,
                entity,
                filename,
                local_path,
                size_bytes,
                sha256,
                content_sha256,
                COALESCE(status, 'processed'),
                processed_at
            FROM processed_attachments
            WHERE content_sha256 IS NULL
              AND local_path IS NOT NULL
            ORDER BY processed_at ASC
            """
        ).fetchall()

    return [ProcessedAttachmentMatch(*row) for row in rows]
