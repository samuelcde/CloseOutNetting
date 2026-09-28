from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from .config import LOG_DIR

LOG_FILE = LOG_DIR / "closenetting.log"
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(message)s"


def _dated_log_namer(default_name: str) -> str:
    """Convierte closenetting.log.YYYY-MM-DD en closenetting_YYYY-MM-DD.log."""
    path = Path(default_name)
    prefix = f"{LOG_FILE.name}."
    if not path.name.startswith(prefix):
        return default_name

    date_suffix = path.name[len(prefix):]
    return str(path.with_name(f"{LOG_FILE.stem}_{date_suffix}{LOG_FILE.suffix}"))


def configure_logging() -> None:
    """Configura consola y rotación diaria de logs con retención de 30 días."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(LOG_FORMAT)

    file_handler = TimedRotatingFileHandler(
        LOG_FILE,
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
        utc=False,
    )
    file_handler.suffix = "%Y-%m-%d"
    file_handler.namer = _dated_log_namer
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
