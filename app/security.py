from __future__ import annotations

import re
import unicodedata

from pathlib import Path

from .exceptions import SecurityValidationError


WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
}


INVALID_WINDOWS_CHARS = re.compile(
    r'[<>:"/\\|?*\x00-\x1F]'
)


def sanitize_filename(
    raw_filename: str,
    max_length: int = 180,
) -> str:

    value = unicodedata.normalize(
        "NFKC",
        str(raw_filename),
    )

    # Elimina cualquier ruta enviada por el remitente.
    value = Path(value).name

    value = INVALID_WINDOWS_CHARS.sub(
        "_",
        value,
    )

    # Windows no acepta nombres acabados en punto/espacio.
    value = value.strip(
        " ."
    )

    if not value:
        raise SecurityValidationError(
            "Nombre de archivo vacío."
        )

    path = Path(value)

    stem = path.stem.strip(
        " ."
    )

    suffix = path.suffix.lower()

    if stem.upper() in WINDOWS_RESERVED_NAMES:
        stem = f"_{stem}"

    available = max_length - len(suffix)

    stem = stem[:available]

    filename = (
        f"{stem}{suffix}"
    )

    if filename in {".", ".."}:
        raise SecurityValidationError(
            "Nombre de archivo inseguro."
        )

    return filename


def build_safe_destination(
    download_root: Path,
    entity: str,
    filename: str,
    source_year: int,
) -> Path:

    root = download_root.resolve(strict=False)

    if not isinstance(source_year, int) or not 1900 <= source_year <= 2200:
        raise SecurityValidationError(
            f"Año de descarga inválido: {source_year!r}"
        )

    year_directory = root / str(source_year)

    entity_directory = (
        year_directory / entity
    ).resolve(strict=False)

    if not entity_directory.is_relative_to(root):
        raise SecurityValidationError(
            "Ruta de entidad fuera del directorio permitido."
        )

    entity_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    destination = (
        entity_directory / filename
    ).resolve(strict=False)

    if not destination.is_relative_to(entity_directory):
        raise SecurityValidationError(
            "Intento de Path Traversal detectado."
        )

    return destination


def avoid_file_collision(
    destination: Path,
    attachment_id: str,
) -> Path:

    if not destination.exists():
        return destination

    safe_id = re.sub(
        r"[^A-Za-z0-9]",
        "",
        attachment_id,
    )[:8]

    return destination.with_name(
        f"{destination.stem}"
        f"__{safe_id}"
        f"{destination.suffix}"
    )