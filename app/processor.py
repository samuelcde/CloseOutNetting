from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

from .auth import get_access_token
from .business_normalizer import save_report_data
from .config import OperationalConfig, Settings
from .db import (
    find_by_content_sha256,
    find_by_file_sha256,
    is_processed,
    mark_processed,
)
from .entities import get_sender_address, resolve_entity
from .exceptions import (
    AttachmentTooLargeError,
    ReportParseError,
    SecurityValidationError,
)
from .fingerprint import build_report_content_sha256
from .graph_api import GraphClient, build_previous_business_day_window
from .notifier import DailyProcessSummary, EmailNotifier, NotificationError
from .parser import parse_report
from .postgres_db import register_ingestion_file, set_ingestion_status
from .security import avoid_file_collision, build_safe_destination, sanitize_filename


logger = logging.getLogger(__name__)


def _safe_notify(description: str, action: Callable[[], None]) -> None:
    """Envía una notificación sin detener el proceso si falla SMTP."""

    try:
        action()
    except NotificationError as exc:
        logger.error(
            "No fue posible enviar la notificación SMTP '%s': %s",
            description,
            exc,
        )
    except Exception:
        logger.exception(
            "Error inesperado enviando la notificación SMTP '%s'.",
            description,
        )


def _existing_local_path(value: str | None) -> Path | None:
    if not value:
        return None

    return Path(value)


def _resolve_final_destination(
    base_destination: Path,
    attachment_id: str,
    sha256: str,
) -> Path:
    candidate = avoid_file_collision(base_destination, attachment_id)

    if not candidate.exists():
        return candidate

    candidate = base_destination.with_name(
        f"{base_destination.stem}__{sha256[:12]}{base_destination.suffix}"
    )

    if not candidate.exists():
        return candidate

    return base_destination.with_name(
        f"{base_destination.stem}__{sha256[:12]}_{uuid.uuid4().hex[:8]}"
        f"{base_destination.suffix}"
    )


def _register_ingestion_outcome(
    *,
    entity: str,
    filename: str,
    source_path: str | Path | None,
    message_id: str,
    attachment_id: str,
    file_sha256: str,
    content_sha256: str | None,
    size_bytes: int,
    status: str,
) -> int:
    """Registra el archivo en ingestion_files y deja su estado final."""

    ingestion_id = register_ingestion_file(
        entity=entity,
        source_filename=filename,
        source_path=source_path,
        message_id=message_id,
        attachment_id=attachment_id,
        file_sha256=file_sha256,
        content_sha256=content_sha256,
        size_bytes=size_bytes,
    )

    set_ingestion_status(
        ingestion_id=ingestion_id,
        status=status,
        content_sha256=content_sha256,
    )

    return ingestion_id


def process_cycle(settings: Settings, config: OperationalConfig) -> int:
    """
    Ejecuta un ciclo completo de Auto CloseNetting.

    El correo de resumen se intenta enviar siempre, incluso si no hay
    archivos nuevos o si se presentan incidencias durante el ciclo.
    """

    started_at = datetime.now()
    target_date = None

    messages_reviewed = 0
    downloaded = 0
    files_parsed = 0
    files_saved_to_database = 0
    duplicate_files = 0
    unknown_entity_files = 0
    oversized_files = 0
    parse_errors = 0

    cycle_incidents: list[str] = []
    cycle_error: Exception | None = None

    notifier = EmailNotifier(
        smtp_settings=settings.smtp,
        recipients=config.alert_recipients,
    )

    logger.info("============================================================")
    logger.info("===== INICIO CICLO AUTO CLOSENETTING =====")
    logger.info("============================================================")

    try:
        processing_window = build_previous_business_day_window(
            timezone_name=config.timezone,
            late_arrival_grace_hours=config.late_arrival_grace_hours,
        )

        target_date = processing_window.target_date
        logger.info("Fecha objetivo del ciclo: %s", target_date.isoformat())

        access_token = get_access_token(settings)

        with GraphClient(access_token, settings.mailbox_user) as graph:
            messages = graph.list_messages_for_previous_business_day(
                timezone_name=config.timezone,
                late_arrival_grace_hours=config.late_arrival_grace_hours,
                page_size=config.graph_page_size,
                max_messages=config.max_messages_per_cycle,
            )

            messages_reviewed = len(messages)
            logger.info("Mensajes obtenidos: %s", messages_reviewed)

            for message in messages:
                if downloaded >= config.max_files_per_cycle:
                    logger.info(
                        "Límite global de %s archivos alcanzado para este ciclo.",
                        config.max_files_per_cycle,
                    )
                    break

                if not message.get("hasAttachments", False):
                    continue

                message_id = message.get("id")
                if not message_id:
                    logger.warning("Mensaje sin ID. Se omite.")
                    continue

                sender_address = get_sender_address(message)
                entity = resolve_entity(message, config)
                is_unknown_entity = entity == config.default_entity

                attachments = graph.list_attachments(message_id)

                for attachment in attachments:
                    if downloaded >= config.max_files_per_cycle:
                        break

                    attachment_type = attachment.get("@odata.type", "")
                    if attachment_type != "#microsoft.graph.fileAttachment":
                        logger.info(
                            "Adjunto omitido porque no es fileAttachment: %s",
                            attachment_type,
                        )
                        continue

                    if attachment.get("isInline", False):
                        logger.info(
                            "Adjunto inline omitido: %s",
                            attachment.get("name", "(sin nombre)"),
                        )
                        continue

                    attachment_id = attachment.get("id")
                    if not attachment_id:
                        logger.warning("Adjunto sin ID. Se omite.")
                        continue

                    if is_processed(message_id, attachment_id):
                        logger.info(
                            "Adjunto ya procesado. Se omite: %s",
                            attachment.get("name", "(sin nombre)"),
                        )
                        continue

                    try:
                        filename = sanitize_filename(
                            attachment.get("name", "attachment.bin")
                        )
                    except SecurityValidationError as exc:
                        logger.warning("Adjunto rechazado por seguridad: %s", exc)
                        continue

                    extension = Path(filename).suffix.lower()
                    if extension not in config.allowed_extensions:
                        logger.info("Adjunto omitido por extensión: %s", filename)
                        continue

                    try:
                        announced_size = int(attachment.get("size") or 0)
                    except (TypeError, ValueError):
                        announced_size = 0

                    if announced_size > config.max_attachment_bytes:
                        oversized_files += 1
                        logger.warning(
                            "Adjunto rechazado por tamaño: %s | Entidad: %s | %s bytes",
                            filename,
                            entity,
                            announced_size,
                        )
                        cycle_incidents.append(
                            "ARCHIVO RECHAZADO POR TAMAÑO"
                            f" | Entidad: {entity}"
                            f" | Archivo: {filename}"
                            f" | Tamaño: {announced_size} bytes"
                            " | Motivo: supera el límite permitido"
                        )
                        continue

                    try:
                        base_destination = build_safe_destination(
                            settings.download_dir,
                            entity,
                            filename,
                        )
                        staging_destination = build_safe_destination(
                            settings.download_dir,
                            entity,
                            f".cln-{uuid.uuid4().hex}{extension}",
                        )
                    except SecurityValidationError as exc:
                        logger.warning(
                            "Adjunto rechazado por seguridad '%s': %s",
                            filename,
                            exc,
                        )
                        continue

                    try:
                        size_bytes, sha256 = graph.download_attachment(
                            message_id=message_id,
                            attachment_id=attachment_id,
                            destination=staging_destination,
                            max_bytes=config.max_attachment_bytes,
                        )
                    except AttachmentTooLargeError as exc:
                        oversized_files += 1
                        staging_destination.unlink(missing_ok=True)
                        logger.warning(
                            "Adjunto rechazado durante descarga por tamaño: "
                            "%s | Entidad: %s | %s",
                            filename,
                            entity,
                            exc,
                        )
                        cycle_incidents.append(
                            "ARCHIVO RECHAZADO POR TAMAÑO"
                            f" | Entidad: {entity}"
                            f" | Archivo: {filename}"
                            f" | Motivo: {exc}"
                        )
                        continue
                    except SecurityValidationError as exc:
                        staging_destination.unlink(missing_ok=True)
                        logger.warning(
                            "Adjunto rechazado por seguridad '%s': %s",
                            filename,
                            exc,
                        )
                        continue
                    except Exception:
                        staging_destination.unlink(missing_ok=True)
                        raise

                    downloaded += 1
                    logger.info(
                        "Descargado temporalmente [%s]: %s (%s bytes)",
                        entity,
                        filename,
                        size_bytes,
                    )

                    if is_unknown_entity:
                        unknown_entity_files += 1
                        logger.warning(
                            "Archivo recibido en OTROS: %s | Remitente: %s",
                            filename,
                            sender_address,
                        )
                        cycle_incidents.append(
                            "ENTIDAD DESCONOCIDA"
                            f" | Entidad: {entity}"
                            f" | Archivo: {filename}"
                            f" | Remitente: {sender_address}"
                            " | Motivo: dominio no configurado"
                        )

                    # ========================================================
                    # DUPLICADO BINARIO
                    # ========================================================

                    binary_match = find_by_file_sha256(entity, sha256)

                    if binary_match is not None:
                        duplicate_files += 1
                        staging_destination.unlink(missing_ok=True)

                        try:
                            _register_ingestion_outcome(
                                entity=entity,
                                filename=filename,
                                source_path=binary_match.local_path,
                                message_id=message_id,
                                attachment_id=attachment_id,
                                file_sha256=sha256,
                                content_sha256=binary_match.content_sha256,
                                size_bytes=size_bytes,
                                status="duplicate_binary",
                            )
                        except Exception as exc:
                            logger.exception(
                                "No fue posible registrar el duplicado binario "
                                "en PostgreSQL: %s",
                                filename,
                            )
                            cycle_incidents.append(
                                "ERROR BASE DE DATOS"
                                f" | Entidad: {entity}"
                                f" | Archivo: {filename}"
                                f" | Motivo: {type(exc).__name__}: {exc}"
                            )

                        mark_processed(
                            message_id=message_id,
                            attachment_id=attachment_id,
                            entity=entity,
                            filename=filename,
                            local_path=_existing_local_path(binary_match.local_path),
                            size_bytes=size_bytes,
                            sha256=sha256,
                            content_sha256=binary_match.content_sha256,
                            status="duplicate_binary",
                            duplicate_of_message_id=binary_match.message_id,
                            duplicate_of_attachment_id=binary_match.attachment_id,
                        )

                        logger.info(
                            "Duplicado binario omitido: %s | Entidad: %s | SHA-256: %s",
                            filename,
                            entity,
                            sha256[:16],
                        )
                        cycle_incidents.append(
                            "DUPLICADO OMITIDO"
                            f" | Entidad: {entity}"
                            f" | Archivo: {filename}"
                            " | Motivo: SHA-256 físico ya procesado"
                        )
                        continue

                    # ========================================================
                    # PARSEO
                    # ========================================================

                    try:
                        report = parse_report(
                            staging_destination,
                            config.parser,
                            entity=entity,
                            report_passwords=settings.report_passwords,
                        )
                    except ReportParseError as exc:
                        parse_errors += 1

                        final_destination = _resolve_final_destination(
                            base_destination,
                            attachment_id,
                            sha256,
                        )
                        os.replace(staging_destination, final_destination)

                        try:
                            _register_ingestion_outcome(
                                entity=entity,
                                filename=filename,
                                source_path=final_destination,
                                message_id=message_id,
                                attachment_id=attachment_id,
                                file_sha256=sha256,
                                content_sha256=None,
                                size_bytes=size_bytes,
                                status="parse_error",
                            )
                        except Exception as db_exc:
                            logger.exception(
                                "No fue posible registrar en PostgreSQL el archivo "
                                "con error de lectura: %s",
                                filename,
                            )
                            cycle_incidents.append(
                                "ERROR BASE DE DATOS"
                                f" | Entidad: {entity}"
                                f" | Archivo: {filename}"
                                f" | Motivo: {type(db_exc).__name__}: {db_exc}"
                            )

                        mark_processed(
                            message_id=message_id,
                            attachment_id=attachment_id,
                            entity=entity,
                            filename=filename,
                            local_path=final_destination,
                            size_bytes=size_bytes,
                            sha256=sha256,
                            content_sha256=None,
                            status="parse_error",
                        )

                        logger.error(
                            "Error de lectura del reporte '%s' | Entidad: %s | Error: %s",
                            filename,
                            entity,
                            exc,
                        )
                        cycle_incidents.append(
                            "ERROR DE LECTURA"
                            f" | Entidad: {entity}"
                            f" | Archivo: {filename}"
                            f" | Motivo: {exc}"
                        )
                        continue

                    files_parsed += 1
                    content_sha256 = build_report_content_sha256(report)

                    logger.info(
                        "Reporte interpretado correctamente: "
                        "%s | Entidad: %s | Tablas: %s | Registros: %s "
                        "| Content SHA-256: %s",
                        filename,
                        entity,
                        report.total_tables,
                        report.total_rows,
                        content_sha256[:16],
                    )

                    # ========================================================
                    # DUPLICADO LÓGICO
                    # ========================================================

                    content_match = find_by_content_sha256(entity, content_sha256)

                    if content_match is not None:
                        duplicate_files += 1
                        staging_destination.unlink(missing_ok=True)

                        try:
                            _register_ingestion_outcome(
                                entity=entity,
                                filename=filename,
                                source_path=content_match.local_path,
                                message_id=message_id,
                                attachment_id=attachment_id,
                                file_sha256=sha256,
                                content_sha256=content_sha256,
                                size_bytes=size_bytes,
                                status="duplicate_content",
                            )
                        except Exception as exc:
                            logger.exception(
                                "No fue posible registrar el duplicado lógico "
                                "en PostgreSQL: %s",
                                filename,
                            )
                            cycle_incidents.append(
                                "ERROR BASE DE DATOS"
                                f" | Entidad: {entity}"
                                f" | Archivo: {filename}"
                                f" | Motivo: {type(exc).__name__}: {exc}"
                            )

                        mark_processed(
                            message_id=message_id,
                            attachment_id=attachment_id,
                            entity=entity,
                            filename=filename,
                            local_path=_existing_local_path(content_match.local_path),
                            size_bytes=size_bytes,
                            sha256=sha256,
                            content_sha256=content_sha256,
                            status="duplicate_content",
                            duplicate_of_message_id=content_match.message_id,
                            duplicate_of_attachment_id=content_match.attachment_id,
                        )

                        logger.info(
                            "Duplicado lógico omitido después del parseo: "
                            "%s | Entidad: %s | Content SHA-256: %s",
                            filename,
                            entity,
                            content_sha256[:16],
                        )
                        cycle_incidents.append(
                            "DUPLICADO OMITIDO"
                            f" | Entidad: {entity}"
                            f" | Archivo: {filename}"
                            " | Motivo: contenido financiero ya procesado"
                        )
                        continue

                    # ========================================================
                    # ARCHIVO NUEVO: UBICACIÓN DEFINITIVA
                    # ========================================================

                    final_destination = _resolve_final_destination(
                        base_destination,
                        attachment_id,
                        sha256,
                    )
                    os.replace(staging_destination, final_destination)

                    # ========================================================
                    # POSTGRESQL: INGESTION + TABLA ENTIDAD/OPERACIÓN
                    # ========================================================

                    ingestion_id: int | None = None

                    try:
                        ingestion_id = register_ingestion_file(
                            entity=entity,
                            source_filename=filename,
                            source_path=final_destination,
                            message_id=message_id,
                            attachment_id=attachment_id,
                            file_sha256=sha256,
                            content_sha256=content_sha256,
                            size_bytes=size_bytes,
                        )

                        persistence = save_report_data(
                            ingestion_id=ingestion_id,
                            report=report,
                            entity=entity,
                        )

                        if persistence.complete:
                            files_saved_to_database += 1

                            logger.info(
                                "Archivo guardado en base de datos: "
                                "%s | ingestion_id=%s | filas=%s | warnings=%s",
                                filename,
                                ingestion_id,
                                persistence.rows_saved,
                                persistence.rows_with_warnings,
                            )
                        else:
                            pending_parts: list[str] = []

                            if persistence.unsupported_reports:
                                pending_parts.append(
                                    "sin soporte="
                                    + ", ".join(persistence.unsupported_reports)
                                )

                            if persistence.unclassified_sheets:
                                pending_parts.append(
                                    "sin clasificar="
                                    + ", ".join(persistence.unclassified_sheets)
                                )

                            pending_reason = (
                                " | ".join(pending_parts)
                                or "persistencia parcial"
                            )

                            logger.warning(
                                "Archivo registrado en PostgreSQL pero con "
                                "persistencia parcial: %s | ingestion_id=%s | %s",
                                filename,
                                ingestion_id,
                                pending_reason,
                            )

                            cycle_incidents.append(
                                "PERSISTENCIA PARCIAL"
                                f" | Entidad: {entity}"
                                f" | Archivo: {filename}"
                                f" | Ingestion ID: {ingestion_id}"
                                f" | Motivo: {pending_reason}"
                            )

                    except Exception as exc:
                        logger.exception(
                            "Error guardando el reporte en PostgreSQL: %s",
                            filename,
                        )

                        if ingestion_id is not None:
                            try:
                                set_ingestion_status(
                                    ingestion_id=ingestion_id,
                                    status="database_error",
                                    content_sha256=content_sha256,
                                )
                            except Exception:
                                logger.exception(
                                    "No fue posible actualizar a database_error "
                                    "la ingesta %s.",
                                    ingestion_id,
                                )

                        cycle_incidents.append(
                            "ERROR BASE DE DATOS"
                            f" | Entidad: {entity}"
                            f" | Archivo: {filename}"
                            f" | Motivo: {type(exc).__name__}: {exc}"
                        )

                        # El archivo ya quedó almacenado localmente. Se registra
                        # en SQLite como error para no volver a descargar el mismo
                        # adjunto en cada ciclo.
                        mark_processed(
                            message_id=message_id,
                            attachment_id=attachment_id,
                            entity=entity,
                            filename=filename,
                            local_path=final_destination,
                            size_bytes=size_bytes,
                            sha256=sha256,
                            content_sha256=content_sha256,
                            status="database_error",
                        )
                        continue

                    # ========================================================
                    # CONTROL OPERATIVO SQLITE
                    # ========================================================

                    mark_processed(
                        message_id=message_id,
                        attachment_id=attachment_id,
                        entity=entity,
                        filename=filename,
                        local_path=final_destination,
                        size_bytes=size_bytes,
                        sha256=sha256,
                        content_sha256=content_sha256,
                        status="processed",
                    )

                    logger.info(
                        "Archivo nuevo confirmado y almacenado [%s]: %s",
                        entity,
                        final_destination,
                    )

        logger.info("Procesamiento principal finalizado correctamente.")

    except Exception as exc:
        cycle_error = exc
        logger.exception("El ciclo finalizó con un error general.")
        cycle_incidents.append(
            "ERROR GENERAL DEL CICLO"
            f" | Tipo: {type(exc).__name__}"
            f" | Motivo: {exc}"
        )

    finally:
        finished_at = datetime.now()

        logger.info("Archivos descargados desde Graph API: %s", downloaded)
        logger.info("Archivos parseados correctamente: %s", files_parsed)
        logger.info("Guardados en base de datos: %s", files_saved_to_database)
        logger.info("Duplicados omitidos: %s", duplicate_files)
        logger.info("Archivos enviados a OTROS: %s", unknown_entity_files)
        logger.info("Archivos rechazados por tamaño: %s", oversized_files)
        logger.info("Errores de lectura: %s", parse_errors)

        summary = DailyProcessSummary(
            started_at=started_at,
            finished_at=finished_at,
            target_date=target_date,
            successful=cycle_error is None,
            messages_reviewed=messages_reviewed,
            files_downloaded=downloaded,
            files_parsed=files_parsed,
            files_saved_to_database=files_saved_to_database,
            duplicate_files=duplicate_files,
            unknown_entity_files=unknown_entity_files,
            oversized_files=oversized_files,
            parse_errors=parse_errors,
            details=tuple(cycle_incidents),
        )

        logger.info("Enviando resumen operativo del ciclo por SMTP...")

        _safe_notify(
            "resumen operativo del ciclo",
            lambda: notifier.notify_daily_summary(summary),
        )

        logger.info("============================================================")
        logger.info("===== FIN CICLO AUTO CLOSENETTING =====")
        logger.info("============================================================")

    if cycle_error is not None:
        raise cycle_error

    return downloaded
