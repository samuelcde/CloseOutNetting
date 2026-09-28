from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from .exceptions import ConfigurationError


# ============================================================
# RUTAS BASE
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

ENV_FILE = BASE_DIR / ".env"
CONFIG_FILE = BASE_DIR / "config.json"

STATE_DIR = BASE_DIR / "state"
LOG_DIR = BASE_DIR / "logs"

STATE_DB = STATE_DIR / "automation_state.db"


# ============================================================
# VALIDACIONES
# ============================================================

ENTITY_NAME_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,63}$")


# ============================================================
# MODELOS DE CONFIGURACIÓN
# ============================================================

@dataclass(frozen=True, slots=True)
class Settings:
    tenant_id: str
    client_id: str
    client_secret: str
    mailbox_user: str
    download_dir: Path
    smtp: SmtpSettings

    # Contraseñas para reportes protegidos.
    # Se mantienen fuera de config.json.
    report_passwords: dict[str, tuple[str, ...]]

@dataclass(frozen=True, slots=True)
class ParserConfig:
    max_rows_per_table: int
    max_columns_per_table: int
    max_sheets_per_workbook: int
    max_xlsx_uncompressed_bytes: int
    max_zip_ratio: int

@dataclass(frozen=True, slots=True)
class OperationalConfig:
    poll_interval_minutes: int
    timezone: str
    business_day_mode: str
    late_arrival_grace_hours: int
    graph_page_size: int
    max_messages_per_cycle: int
    max_files_per_cycle: int
    max_attachment_bytes: int
    allowed_extensions: frozenset[str]
    default_entity: str
    entities: dict[str, str]
    parser: ParserConfig
    alert_recipients: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class SmtpSettings:
    host: str
    port: int
    user: str
    password: str
    use_tls: bool
    timeout_seconds: int = 30

# ============================================================
# VARIABLES DE ENTORNO
# ============================================================

def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise ConfigurationError(f"Falta la variable obligatoria '{name}'.")

    return value

def _required_int_env(name: str) -> int:
    raw = _required_env(name)

    try:
        value = int(raw)

    except ValueError as exc:
        raise ConfigurationError(f"La variable '{name}' debe ser un entero.") from exc

    if value <= 0:
        raise ConfigurationError(f"La variable '{name}' debe ser mayor que cero.")

    return value

def _boolean_env(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)

    if raw is None:
        return default

    value = (raw.strip().lower())

    if value in {"1", "true", "yes", "si", "sí", "on",}:
        return True

    if value in {"0", "false", "no", "off",}:
        return False

    raise ConfigurationError(f"La variable '{name}' debe contener " "true/false, yes/no o 1/0.")

def load_smtp_settings() -> SmtpSettings:
    """
    Carga la configuración SMTP desde .env.

    La contraseña nunca debe registrarse en logs.
    """

    return SmtpSettings(
        host=_required_env("SMTP_HOST"),

        port=_required_int_env("SMTP_PORT"),

        user=_required_env("SMTP_USER"),

        password=_required_env("SMTP_PASSWORD"),

        use_tls=_boolean_env("SMTP_USE_TLS", default=True),

        timeout_seconds=30,
    )

def load_report_passwords() -> dict[str, tuple[str, ...]]:
    """
    Carga contraseñas por entidad desde REPORT_PASSWORDS.

    Formato esperado:

    REPORT_PASSWORDS={
        "GS":["clave1","clave2"],
        "MORGAN_STANLEY":["clave3"],
        "CITI":[]
    }

    Las claves nunca se registran en logs.
    """

    load_dotenv(ENV_FILE, override=False)

    raw = os.getenv("REPORT_PASSWORDS", "").strip()

    if not raw:
        return {}

    try:
        parsed = json.loads(raw)

    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            "REPORT_PASSWORDS debe contener "
            "un objeto JSON válido."
        ) from exc

    if not isinstance(parsed, dict):
        raise ConfigurationError("REPORT_PASSWORDS debe ser un objeto JSON.")

    result: dict[str, tuple[str, ...]] = {}

    for entity, passwords in parsed.items():
        clean_entity = (str(entity).strip().upper())

        if not ENTITY_NAME_PATTERN.fullmatch(clean_entity):
            raise ConfigurationError(f"Entidad inválida en REPORT_PASSWORDS: " f"{entity!r}")

        if passwords is None:
            result[clean_entity] = ()
            continue

        if not isinstance(passwords, list):
            raise ConfigurationError(
                f"Las contraseñas de '{clean_entity}' "
                "deben ser una lista JSON."
            )

        clean_passwords: list[str] = []
        seen: set[str] = set()

        for value in passwords:
            password = str(value).strip()

            if not password:
                continue

            if password in seen:
                continue

            seen.add(password)

            clean_passwords.append(password)

        result[clean_entity] = tuple(clean_passwords)

    return result

def load_settings() -> Settings:
    """
    Carga la configuración sensible desde .env.
    """

    load_dotenv(ENV_FILE, override=False)

    return Settings(
        tenant_id=_required_env("TENANT_ID"),

        client_id=_required_env("CLIENT_ID"),

        client_secret=_required_env("CLIENT_SECRET"),

        mailbox_user=_required_env("MAILBOX_USER"),

        download_dir=Path(_required_env("DOWNLOAD_DIR")),

        report_passwords=(load_report_passwords()),

        smtp=load_smtp_settings(),
    )

# ============================================================
# CONFIGURACIÓN OPERATIVA
# ============================================================

def _positive_int(value, field: str) -> int:
    try:
        parsed = int(value)

    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"'{field}' debe ser entero.") from exc

    if parsed <= 0:
        raise ConfigurationError(f"'{field}' debe ser mayor que cero.")

    return parsed

def _validate_entity_name(value: str) -> str:
    entity = str(value).strip().upper()

    if not ENTITY_NAME_PATTERN.fullmatch(entity):
        raise ConfigurationError(f"Nombre de entidad inválido: {value!r}")

    return entity

def load_operational_config() -> OperationalConfig:
    """
    Carga y valida config.json.

    La selección de correos se realizará posteriormente
    por día hábil anterior, no por los últimos N mensajes.

    Los límites de mensajes y archivos se mantienen como
    controles defensivos de seguridad.
    """

    # ========================================================
    # ARCHIVO JSON
    # ========================================================

    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))

    except FileNotFoundError as exc:
        raise ConfigurationError(f"No existe {CONFIG_FILE}") from exc

    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"config.json inválido: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigurationError("config.json debe contener un objeto JSON.")

    # ========================================================
    # INTERVALO DEL MONITOR
    # ========================================================

    poll_interval_minutes = _positive_int(
        raw.get("poll_interval_minutes", 720),
        "poll_interval_minutes",
    )

    # ========================================================
    # REGLA TEMPORAL
    # ========================================================

    timezone_name = str(raw.get("timezone", "America/Bogota")).strip()

    if not timezone_name:
        raise ConfigurationError("'timezone' no puede estar vacío.")

    business_day_mode = str(raw.get("business_day_mode", "previous_business_day")).strip().lower()

    if business_day_mode != "previous_business_day":
        raise ConfigurationError("'business_day_mode' debe ser " "'previous_business_day'.")

    late_arrival_grace_hours = (
        _non_negative_int(raw.get("late_arrival_grace_hours", 12), "late_arrival_grace_hours")
    )

    # Límite defensivo:
    # evitar configurar accidentalmente varios días de
    # tolerancia y ampliar demasiado la consulta.
    if late_arrival_grace_hours > 48:
        raise ConfigurationError("'late_arrival_grace_hours' " "no puede ser mayor que 48.")

    # ========================================================
    # MICROSOFT GRAPH
    # ========================================================

    graph_page_size = _positive_int(raw.get("graph_page_size", 50), "graph_page_size")

    if graph_page_size > 500:
        raise ConfigurationError("'graph_page_size' no puede " "ser mayor que 500.")

    max_messages_per_cycle = (
        _positive_int(raw.get("max_messages_per_cycle", 500), "max_messages_per_cycle")
    )

    if max_messages_per_cycle < graph_page_size:
        raise ConfigurationError(
            "'max_messages_per_cycle' debe "
            "ser mayor o igual que "
            "'graph_page_size'."
        )

    max_files_per_cycle = (
        _positive_int(raw.get("max_files_per_cycle", 100), "max_files_per_cycle")
    )

    # ========================================================
    # TAMAÑO DE ADJUNTOS
    # ========================================================

    max_attachment_mb = (_positive_int(raw.get("max_attachment_mb", 25), "max_attachment_mb"))

    max_attachment_bytes = max_attachment_mb * 1024 * 1024

    # ========================================================
    # EXTENSIONES
    # ========================================================

    raw_extensions = raw.get("allowed_extensions", [])

    if not isinstance(raw_extensions, list):
        raise ConfigurationError("'allowed_extensions' debe " "ser una lista JSON.")

    allowed_extensions: set[str] = set()

    for value in raw_extensions:
        extension = str(value).strip().lower()

        if not extension:
            continue

        if not extension.startswith("."):
            extension = f".{extension}"

        allowed_extensions.add(extension)

    if not allowed_extensions:
        raise ConfigurationError("Debe existir al menos una " "extensión permitida.")

    # ========================================================
    # ENTIDAD POR DEFECTO
    # ========================================================

    default_entity = (_validate_entity_name(raw.get("default_entity", "OTROS")))

    # ========================================================
    # ENTIDADES
    # ========================================================

    raw_entities = raw.get("entities", {})

    if not isinstance(raw_entities, dict):
        raise ConfigurationError("'entities' debe ser un objeto JSON.")

    entities: dict[str, str] = {}

    for domain, entity in raw_entities.items():
        clean_domain = str(domain).strip().lower()

        if not clean_domain:
            raise ConfigurationError("Se encontró un dominio vacío " "en 'entities'.")

        if (
            "/" in clean_domain
            or "\\" in clean_domain
            or "@" in clean_domain
            or " " in clean_domain
        ):
            raise ConfigurationError(f"Dominio inválido: {domain!r}")

        entities[clean_domain] = _validate_entity_name(entity)

    # ========================================================
    # DESTINATARIOS DE ALERTAS
    # ========================================================

    raw_recipients = raw.get("alert_recipients", [])

    if not isinstance(raw_recipients, list):
        raise ConfigurationError("'alert_recipients' debe ser " "una lista JSON.")

    alert_recipients: list[str] = []
    seen_recipients: set[str] = set()

    for value in raw_recipients:
        recipient = str(value).strip()

        if not recipient:
            continue

        if "\r" in recipient or "\n" in recipient:
            raise ConfigurationError("Destinatario de alerta inválido.")

        if ("@" not in recipient or recipient.startswith("@") or recipient.endswith("@")):
            raise ConfigurationError(f"Correo de alerta inválido: " f"{recipient!r}")

        normalized = (recipient.casefold())

        if normalized in seen_recipients:
            continue

        seen_recipients.add(normalized)

        alert_recipients.append(recipient)

    if not alert_recipients:
        raise ConfigurationError("Debe existir al menos un " "correo en 'alert_recipients'.")

    # ========================================================
    # PARSER
    # ========================================================

    parser_raw = raw.get("parser", {})

    if not isinstance(parser_raw, dict):
        raise ConfigurationError("'parser' debe ser un objeto JSON.")

    parser_config = ParserConfig(

        max_rows_per_table=_positive_int(
            parser_raw.get("max_rows_per_table", 100000),
            "parser.max_rows_per_table",
        ),

        max_columns_per_table=_positive_int(
            parser_raw.get("max_columns_per_table", 200),
            "parser.max_columns_per_table",
        ),

        max_sheets_per_workbook=_positive_int(
            parser_raw.get("max_sheets_per_workbook", 20),
            "parser.max_sheets_per_workbook",
        ),

        max_xlsx_uncompressed_bytes=(
            _positive_int(
                parser_raw.get("max_xlsx_uncompressed_mb", 150),
                "parser.max_xlsx_uncompressed_mb",
            )
            * 1024
            * 1024
        ),

        max_zip_ratio=_positive_int(parser_raw.get("max_zip_ratio", 100), "parser.max_zip_ratio"),
    )

    # ========================================================
    # CONFIGURACIÓN FINAL
    # ========================================================

    return OperationalConfig(

        poll_interval_minutes=(poll_interval_minutes),

        timezone=timezone_name,

        business_day_mode=(business_day_mode),

        late_arrival_grace_hours=(late_arrival_grace_hours),

        graph_page_size=(graph_page_size),

        max_messages_per_cycle=(max_messages_per_cycle),

        max_files_per_cycle=(max_files_per_cycle),

        max_attachment_bytes=(max_attachment_bytes),

        allowed_extensions=frozenset(allowed_extensions),

        default_entity=(default_entity),

        entities=entities,

        alert_recipients=tuple(alert_recipients),

        parser=parser_config,
    )

# ============================================================
# DIRECTORIOS
# ============================================================

def prepare_local_directories() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    LOG_DIR.mkdir(parents=True, exist_ok=True)

def validate_download_directory(download_dir: Path) -> None:
    """
    Verifica acceso real de escritura al destino UNC/local.
    """

    try:
        download_dir.mkdir(parents=True, exist_ok=True)

        test_file = download_dir / ".closenetting_write_test"

        test_file.write_text("ok", encoding="utf-8")

        test_file.unlink()

    except OSError as exc:
        raise ConfigurationError(
            f"No hay acceso de escritura a "
            f"'{download_dir}': {exc}"
        ) from exc

def _validate_timezone(value: object) -> str:
    timezone_name = str(value).strip()

    if not timezone_name:
        raise ConfigurationError("'timezone' no puede estar vacío.")

    try:
        ZoneInfo(timezone_name)

    except ZoneInfoNotFoundError as exc:
        raise ConfigurationError(f"Zona horaria inválida: {timezone_name!r}") from exc

    return timezone_name

def _non_negative_int(value, field: str) -> int:
    try:
        parsed = int(value)

    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"'{field}' debe ser un número entero.") from exc

    if parsed < 0:
        raise ConfigurationError(f"'{field}' no puede ser negativo.")

    return parsed