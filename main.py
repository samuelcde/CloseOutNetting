from __future__ import annotations

import argparse
import logging
import sys
import time

from app.config import (
    STATE_DIR,
    load_operational_config,
    load_settings,
    prepare_local_directories,
    validate_download_directory,
)
from app.db import initialize_database
from app.instance_lock import SingleInstanceLock
from app.logging_setup import configure_logging
from app.processor import process_cycle


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SET ICAP - Auto CloseNetting"
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help="Ejecuta un único ciclo y finaliza.",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    prepare_local_directories()
    configure_logging()

    logger = logging.getLogger(__name__)

    try:
        settings = load_settings()

        validate_download_directory(
            settings.download_dir
        )

        initialize_database()

        lock_file = (
            STATE_DIR
            / "auto-closenetting.lock"
        )

        with SingleInstanceLock(lock_file):

            logger.info(
                "============================================="
            )
            logger.info(
                "AUTO CLOSENETTING INICIADO"
            )
            logger.info(
                "Modo: %s",
                "UNA SOLA EJECUCIÓN"
                if args.once
                else "MONITOREO CONTINUO",
            )
            logger.info(
                "============================================="
            )

            while True:

                try:
                    config = load_operational_config()

                    process_cycle(
                        settings,
                        config,
                    )

                    # Modo controlado para pruebas.
                    if args.once:
                        logger.info(
                            "Ciclo único finalizado correctamente."
                        )
                        return 0

                    wait_seconds = (
                        config.poll_interval_minutes
                        * 60
                    )

                    logger.info(
                        "Próxima revisión en %s minutos.",
                        config.poll_interval_minutes,
                    )

                    logger.info(
                        "---------------------------------------------"
                    )

                    time.sleep(
                        wait_seconds
                    )

                except KeyboardInterrupt:
                    logger.info(
                        "Proceso detenido manualmente."
                    )
                    return 0

                except Exception:
                    logger.exception(
                        "Error durante el ciclo."
                    )

                    # En modo de prueba queremos ver el error
                    # y terminar, no esperar 5 minutos.
                    if args.once:
                        return 1

                    logger.info(
                        "Nuevo intento en 5 minutos."
                    )

                    time.sleep(300)

    except Exception:
        logger.exception(
            "No fue posible iniciar Auto CloseNetting."
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())