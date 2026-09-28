from __future__ import annotations

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import load_operational_config, load_settings, prepare_local_directories
from app.db import initialize_database, list_without_content_sha256, update_content_sha256
from app.exceptions import ReportParseError
from app.fingerprint import build_report_content_sha256
from app.parser import parse_report


logger = logging.getLogger("backfill_content_fingerprints")


def configure_console_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    )

    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False


def main() -> int:
    configure_console_logging()
    prepare_local_directories()

    settings = load_settings()
    config = load_operational_config()
    initialize_database()

    pending = list_without_content_sha256()

    total = len(pending)
    updated = 0
    missing = 0
    failed = 0

    logger.info("Registros sin content_sha256 detectados: %s", total)

    for index, row in enumerate(pending, start=1):
        path = Path(row.local_path or "")

        logger.info(
            "[%s/%s] %s | %s",
            index,
            total,
            row.entity,
            row.filename,
        )

        if not path.is_file():
            missing += 1
            logger.warning("Archivo no encontrado: %s", path)
            continue

        try:
            report = parse_report(
                path,
                config.parser,
                entity=row.entity,
                report_passwords=settings.report_passwords,
            )
            content_sha256 = build_report_content_sha256(report)
        except ReportParseError as exc:
            failed += 1
            logger.error("No fue posible parsear '%s': %s", path, exc)
            continue
        except Exception:
            failed += 1
            logger.exception("Error inesperado procesando '%s'.", path)
            continue

        update_content_sha256(
            message_id=row.message_id,
            attachment_id=row.attachment_id,
            content_sha256=content_sha256,
        )
        updated += 1

        logger.info(
            "Fingerprint actualizado | Content SHA-256: %s",
            content_sha256[:16],
        )

    logger.info("============================================================")
    logger.info("RESUMEN BACKFILL DE FINGERPRINTS")
    logger.info("============================================================")
    logger.info("Registros detectados: %s", total)
    logger.info("Actualizados: %s", updated)
    logger.info("Archivos no encontrados: %s", missing)
    logger.info("Fallidos: %s", failed)
    logger.info("============================================================")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
