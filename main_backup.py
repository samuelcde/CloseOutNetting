from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import msal
import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import base64
import re
from pathlib import Path
import sqlite3


# ============================================================
# CONSTANTES
# ============================================================
BASE_DIR = Path(__file__).resolve().parent

ENV_FILE = BASE_DIR / ".env"
STATE_DB = BASE_DIR / "automation_state.db"

load_dotenv(ENV_FILE)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

GRAPH_SCOPES = [
    "https://graph.microsoft.com/.default"
]

REQUEST_TIMEOUT = (5, 30)  # conexión, lectura


STATE_DB = Path("automation_state.db")

ALLOWED_EXTENSIONS = {
    ".xlsx",
    ".xls",
    ".csv",
}

ENTITY_BY_DOMAIN = {
    "citi.com": "CITI",
    "gs.com": "GS",
    "morganstanley.com": "MORGAN_STANLEY",
    # Agregaremos las demás entidades aquí a medida que
    # confirmemos sus dominios.
}

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

LOG_FILE = LOG_DIR / "auto-closenetting.log"

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(
            LOG_FILE,
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)

logger = logging.getLogger(__name__)


# ============================================================
# EXCEPCIONES
# ============================================================

class ConfigurationError(RuntimeError):
    """Error en la configuración local de la aplicación."""


class AuthenticationError(RuntimeError):
    """Error obteniendo el token desde Microsoft Entra ID."""


class GraphApiError(RuntimeError):
    """Error realizando una petición contra Microsoft Graph."""


# ============================================================
# CONFIGURACIÓN
# ============================================================

@dataclass(frozen=True, slots=True)
class Settings:
    tenant_id: str
    client_id: str
    client_secret: str
    mailbox_user: str
    download_dir: Path
    max_messages: int
    max_files_per_run: int


def get_required_env(name: str) -> str:
    """
    Obtiene una variable de entorno obligatoria.

    Nunca imprime su valor para evitar exposición accidental
    de información sensible.
    """
    value = os.getenv(name, "").strip()

    if not value:
        raise ConfigurationError(
            f"La variable de entorno '{name}' no está configurada."
        )

    return value


def load_settings() -> Settings:
    """
    Carga la configuración desde el archivo .env.
    """

    return Settings(
        tenant_id=get_required_env("TENANT_ID"),
        client_id=get_required_env("CLIENT_ID"),
        client_secret=get_required_env("CLIENT_SECRET"),
        mailbox_user=get_required_env("MAILBOX_USER"),

        download_dir=Path(
            os.getenv(
                "DOWNLOAD_DIR",
                "downloads",
            ).strip()
        ),

        max_messages=get_positive_int_env(
            "MAX_MESSAGES",
            10,
        ),

        max_files_per_run=get_positive_int_env(
            "MAX_FILES_PER_RUN",
            20,
        ),
    )

def validate_download_directory(
    download_dir: Path,
) -> None:
    """
    Verifica que la carpeta exista y tenga permisos de escritura.
    """
    try:
        download_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        test_file = download_dir / ".write_test"

        test_file.write_text(
            "test",
            encoding="utf-8",
        )

        test_file.unlink()

        logger.info(
            "Directorio de descarga accesible: %s",
            download_dir,
        )

    except OSError as exc:
        raise ConfigurationError(
            f"No hay acceso de escritura al directorio "
            f"'{download_dir}': {exc}"
        ) from exc

def get_positive_int_env(
    name: str,
    default: int,
) -> int:
    raw = os.getenv(name, str(default)).strip()

    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(
            f"La variable '{name}' debe ser un número entero."
        ) from exc

    if value <= 0:
        raise ConfigurationError(
            f"La variable '{name}' debe ser mayor que cero."
        )

    return value
# ============================================================
# AUTENTICACIÓN MICROSOFT ENTRA ID
# ============================================================

def get_access_token(settings: Settings) -> str:
    """
    Obtiene un access token mediante Client Credentials.

    El token representa a la aplicación, no a un usuario.
    """

    authority = (
        f"https://login.microsoftonline.com/{settings.tenant_id}"
    )

    logger.info("Autenticando aplicación contra Microsoft Entra ID...")

    app = msal.ConfidentialClientApplication(
        client_id=settings.client_id,
        authority=authority,
        client_credential=settings.client_secret,
    )

    result = app.acquire_token_for_client(
        scopes=GRAPH_SCOPES
    )

    access_token = result.get("access_token")

    if access_token:
        expires_in = result.get("expires_in")

        logger.info(
            "Autenticación completada correctamente. "
            "Token obtenido%s.",
            f" (expira en {expires_in} segundos)"
            if expires_in
            else "",
        )

        return access_token

    error = result.get("error", "unknown_error")
    description = result.get(
        "error_description",
        "Microsoft Entra ID no devolvió una descripción.",
    )
    correlation_id = result.get("correlation_id")

    message = (
        f"No fue posible obtener el token. "
        f"Error: {error}. "
        f"Descripción: {description}"
    )

    if correlation_id:
        message += f" Correlation ID: {correlation_id}"

    raise AuthenticationError(message)


# ============================================================
# CLIENTE HTTP
# ============================================================

def build_http_session() -> requests.Session:
    """
    Construye una sesión HTTP reutilizable con reintentos
    para errores transitorios.
    """

    retry_strategy = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1,
        status_forcelist=(
            429,
            500,
            502,
            503,
            504,
        ),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )

    adapter = HTTPAdapter(
        max_retries=retry_strategy
    )

    session = requests.Session()

    session.mount(
        "https://",
        adapter,
    )

    session.headers.update({
        "Accept": "application/json",
    })

    return session


# ============================================================
# MICROSOFT GRAPH
# ============================================================

def extract_graph_error(response: requests.Response) -> str:
    """
    Intenta obtener una descripción legible de un error
    devuelto por Microsoft Graph.
    """

    try:
        payload = response.json()

        graph_error = payload.get("error", {})

        code = graph_error.get(
            "code",
            "UnknownGraphError",
        )

        message = graph_error.get(
            "message",
            response.text,
        )

        return f"{code}: {message}"

    except ValueError:
        return response.text or "Respuesta sin contenido."


def list_messages(
    session: requests.Session,
    access_token: str,
    mailbox_user: str,
    top: int = 10,
) -> list[dict[str, Any]]:
    """
    Obtiene los últimos correos del buzón indicado.
    """

    mailbox = quote(
        mailbox_user,
        safe="@.",
    )

    url = (
        f"{GRAPH_BASE_URL}"
        f"/users/{mailbox}/messages"
    )

    headers = {
        "Authorization": f"Bearer {access_token}",
    }

    params = {
        "$top": top,
        "$select": (
            "id,"
            "subject,"
            "receivedDateTime,"
            "from,"
            "hasAttachments,"
            "isRead"
        ),
        "$orderby": "receivedDateTime desc",
    }

    logger.info(
        "Consultando últimos %s correos del buzón %s...",
        top,
        mailbox_user,
    )

    try:
        response = session.get(
            url,
            headers=headers,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )

    except requests.RequestException as exc:
        raise GraphApiError(
            f"Error de comunicación con Microsoft Graph: {exc}"
        ) from exc

    if not response.ok:
        graph_error = extract_graph_error(response)

        request_id = (
            response.headers.get("request-id")
            or response.headers.get("client-request-id")
        )

        message = (
            f"Microsoft Graph respondió HTTP "
            f"{response.status_code}: {graph_error}"
        )

        if request_id:
            message += f" | Request ID: {request_id}"

        raise GraphApiError(message)

    try:
        payload = response.json()
    except ValueError as exc:
        raise GraphApiError(
            "Microsoft Graph respondió con un JSON inválido."
        ) from exc

    messages = payload.get("value")

    if not isinstance(messages, list):
        raise GraphApiError(
            "La respuesta de Microsoft Graph no contiene "
            "la colección esperada 'value'."
        )

    return messages


# ============================================================
# PRESENTACIÓN DE RESULTADOS
# ============================================================

def show_messages(
    messages: list[dict[str, Any]]
) -> None:

    if not messages:
        logger.info(
            "El buzón no devolvió mensajes."
        )
        return

    print()
    print("=" * 80)
    print("CORREOS ENCONTRADOS")
    print("=" * 80)

    for number, message in enumerate(
        messages,
        start=1,
    ):
        sender = (
            message.get("from", {})
            .get("emailAddress", {})
        )

        sender_name = sender.get(
            "name",
            "Desconocido",
        )

        sender_address = sender.get(
            "address",
            "Desconocido",
        )

        subject = (
            message.get("subject")
            or "(Sin asunto)"
        )

        received = (
            message.get("receivedDateTime")
            or "Desconocida"
        )

        has_attachments = message.get(
            "hasAttachments",
            False,
        )

        is_read = message.get(
            "isRead",
            False,
        )

        print()
        print(f"[{number}] {subject}")
        print(
            f"    Remitente : "
            f"{sender_name} <{sender_address}>"
        )
        print(
            f"    Recibido  : {received}"
        )
        print(
            f"    Adjuntos  : "
            f"{'Sí' if has_attachments else 'No'}"
        )
        print(
            f"    Leído     : "
            f"{'Sí' if is_read else 'No'}"
        )

    print()
    print("=" * 80)

def sanitize_filename(filename: str) -> str:
    """
    Limpia el nombre recibido desde el correo para evitar
    rutas inválidas o path traversal.
    """
    filename = Path(filename).name

    filename = re.sub(
        r'[<>:"/\\|?*\x00-\x1F]',
        "_",
        filename,
    )

    return filename.strip() or "attachment.bin"

def get_sender_address(
    message: dict[str, Any]
) -> str:
    """
    Obtiene y normaliza el correo del remitente.
    """

    sender = (
        message.get("from", {})
        .get("emailAddress", {})
        .get("address", "")
    )

    return str(sender).strip().lower()


def get_entity_from_message(
    message: dict[str, Any]
) -> str:
    """
    Determina la entidad a partir del dominio del remitente.

    Ejemplo:
        usuario@citi.com -> CITI
        usuario@gs.com   -> GS
    """

    sender_address = get_sender_address(message)

    if "@" not in sender_address:
        logger.warning(
            "No fue posible determinar el dominio del remitente: %s",
            sender_address or "(vacío)",
        )
        return "OTROS"

    domain = sender_address.rsplit("@", 1)[1]

    for configured_domain, entity in ENTITY_BY_DOMAIN.items():

        configured_domain = configured_domain.lower()

        if (
            domain == configured_domain
            or domain.endswith(f".{configured_domain}")
        ):
            return entity

    logger.warning(
        "Entidad no configurada para el remitente %s",
        sender_address,
    )

    return "OTROS"

def initialize_state_db() -> None:
    """
    Inicializa la base de datos local utilizada para controlar
    qué adjuntos ya fueron procesados.
    """

    with sqlite3.connect(STATE_DB) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_attachments (
                message_id TEXT NOT NULL,
                attachment_id TEXT NOT NULL,
                entity TEXT NOT NULL,
                filename TEXT NOT NULL,
                processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

                PRIMARY KEY (message_id, attachment_id)
            )
            """
        )

        connection.commit()


def is_attachment_processed(
    message_id: str,
    attachment_id: str,
) -> bool:
    """
    Indica si el adjunto ya fue procesado anteriormente.
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
            (
                message_id,
                attachment_id,
            ),
        ).fetchone()

    return row is not None


def mark_attachment_processed(
    message_id: str,
    attachment_id: str,
    entity: str,
    filename: str,
) -> None:
    """
    Registra que un adjunto fue procesado correctamente.
    """

    with sqlite3.connect(STATE_DB) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO processed_attachments (
                message_id,
                attachment_id,
                entity,
                filename
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                message_id,
                attachment_id,
                entity,
                filename,
            ),
        )

        connection.commit()

def list_attachments(
    session: requests.Session,
    access_token: str,
    mailbox_user: str,
    message_id: str,
) -> list[dict[str, Any]]:
    """
    Lista los adjuntos asociados a un mensaje.
    """

    mailbox = quote(
        mailbox_user,
        safe="@.",
    )

    message = quote(
        message_id,
        safe="",
    )

    url = (
        f"{GRAPH_BASE_URL}"
        f"/users/{mailbox}"
        f"/messages/{message}"
        f"/attachments"
    )

    headers = {
        "Authorization": f"Bearer {access_token}",
    }

    try:
        response = session.get(
            url,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )

    except requests.RequestException as exc:
        raise GraphApiError(
            f"Error consultando adjuntos: {exc}"
        ) from exc

    if not response.ok:
        raise GraphApiError(
            f"Error obteniendo adjuntos. "
            f"HTTP {response.status_code}: "
            f"{extract_graph_error(response)}"
        )

    payload = response.json()

    attachments = payload.get("value", [])

    if not isinstance(attachments, list):
        raise GraphApiError(
            "Microsoft Graph devolvió una estructura "
            "de adjuntos inesperada."
        )

    return attachments


def get_attachment(
    session: requests.Session,
    access_token: str,
    mailbox_user: str,
    message_id: str,
    attachment_id: str,
) -> dict[str, Any]:
    """
    Obtiene el contenido completo de un adjunto.
    """

    mailbox = quote(
        mailbox_user,
        safe="@.",
    )

    message = quote(
        message_id,
        safe="",
    )

    attachment = quote(
        attachment_id,
        safe="",
    )

    url = (
        f"{GRAPH_BASE_URL}"
        f"/users/{mailbox}"
        f"/messages/{message}"
        f"/attachments/{attachment}"
    )

    headers = {
        "Authorization": f"Bearer {access_token}",
    }

    try:
        response = session.get(
            url,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )

    except requests.RequestException as exc:
        raise GraphApiError(
            f"Error descargando adjunto: {exc}"
        ) from exc

    if not response.ok:
        raise GraphApiError(
            f"Error descargando adjunto. "
            f"HTTP {response.status_code}: "
            f"{extract_graph_error(response)}"
        )

    return response.json()


def download_message_attachments(
    session: requests.Session,
    access_token: str,
    mailbox_user: str,
    message: dict[str, Any],
    download_dir: Path,
    max_files: int,
) -> list[Path]:
    """
    Descarga únicamente adjuntos Excel y CSV.
    """

    message_id = message.get("id")

    if not message_id:
        logger.warning(
            "Mensaje sin ID. Se omite."
        )
        return []

    subject = (
        message.get("subject")
        or "(Sin asunto)"
    )

    attachments = list_attachments(
        session=session,
        access_token=access_token,
        mailbox_user=mailbox_user,
        message_id=message_id,
    )

    if not attachments:
        return []

    # Creamos una carpeta y Determinar la entidad a partir del remitente.
    entity = get_entity_from_message(message)

    message_directory = (
        download_dir / entity
    )


    downloaded_files: list[Path] = []

    for attachment in attachments:

        if len(downloaded_files) >= max_files:
            logger.info(
                "Límite de archivos alcanzado para esta ejecución."
            )
            break

        attachment_type = attachment.get(
            "@odata.type",
            "",
        )

        if attachment_type != "#microsoft.graph.fileAttachment":
            logger.info(
                "Adjunto omitido porque no es un archivo: %s",
                attachment_type,
            )
            continue

        if attachment.get("isInline", False):
            logger.info(
                "Adjunto inline omitido: %s",
                attachment.get("name"),
            )
            continue

        filename = sanitize_filename(
            attachment.get("name", "attachment.bin")
        )

        extension = Path(
            filename
        ).suffix.lower()

        if extension not in ALLOWED_EXTENSIONS:
            logger.info(
                "Adjunto omitido por extensión: %s",
                filename,
            )
            continue

        attachment_id = attachment.get("id")

        if not attachment_id:
            logger.warning(
                "Adjunto sin ID: %s",
                filename,
            )
            continue

        if is_attachment_processed(
            message_id=message_id,
            attachment_id=attachment_id,
            ):

            logger.info(
                "Adjunto ya procesado. Se omite: '%s' | Entidad: %s",
                filename,
                entity,
            )
            continue

        logger.info(
            "Descargando adjunto '%s' | Entidad: %s | Correo: '%s'...",
            filename,
            entity,
            subject,
        )

        attachment_data = get_attachment(
            session=session,
            access_token=access_token,
            mailbox_user=mailbox_user,
            message_id=message_id,
            attachment_id=attachment_id,
        )

        content_bytes = attachment_data.get(
            "contentBytes"
        )

        if not content_bytes:
            logger.warning(
                "El adjunto '%s' no contiene contentBytes.",
                filename,
            )
            continue

        try:
            binary_content = base64.b64decode(
                content_bytes,
                validate=True,
            )

        except (ValueError, TypeError) as exc:
            logger.error(
                "No fue posible decodificar '%s': %s",
                filename,
                exc,
            )
            continue

        message_directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        destination = (
             message_directory / filename
        )

        destination.write_bytes(
            binary_content
        )

        mark_attachment_processed(
            message_id=message_id,
            attachment_id=attachment_id,
            entity=entity,
            filename=filename,
        )

        downloaded_files.append(
            destination
        )

        logger.info(
            "Archivo descargado correctamente: %s (%s bytes)",
            destination,
            len(binary_content),
        )

    return downloaded_files




# ============================================================
# MAIN
# ============================================================

def main() -> int:
    try:
        logger.info(
            "Iniciando prueba de Microsoft Graph..."
        )

        settings = load_settings()
        
        # VALIDACIÓN AÑADIDA: Detiene la ejecución si no hay red o permisos
        validate_download_directory(
            settings.download_dir
        )

        initialize_state_db()

        access_token = get_access_token(
            settings
        )

        with build_http_session() as session:

            # ------------------------------------------------
            # 1. Consultar mensajes
            # ------------------------------------------------
            messages = list_messages(
                session=session,
                access_token=access_token,
                mailbox_user=settings.mailbox_user,
                top=settings.max_messages,
            )

            # ------------------------------------------------
            # 2. Mostrar mensajes encontrados
            # ------------------------------------------------
            show_messages(messages)

            # ------------------------------------------------
            # 3. Descargar adjuntos Excel / CSV
            # ------------------------------------------------
            total_downloaded = 0

            for message in messages:

                if total_downloaded >= settings.max_files_per_run:
                    logger.info(
                        "Límite máximo de %s archivos alcanzado.",
                        settings.max_files_per_run,
                    )
                    break

                if not message.get(
                    "hasAttachments",
                    False,
                ):
                    continue

                remaining_files = (
                    settings.max_files_per_run
                    - total_downloaded
                )

                files = download_message_attachments(
                    session=session,
                    access_token=access_token,
                    mailbox_user=settings.mailbox_user,
                    message=message,
                    download_dir=settings.download_dir,
                    max_files=remaining_files,
                )

                total_downloaded += len(files)

            logger.info(
                "Total de archivos Excel/CSV descargados: %s",
                total_downloaded,
            )

        logger.info(
            "Prueba finalizada correctamente."
        )

        return 0

    except ConfigurationError as exc:
        logger.error(
            "Error de configuración: %s",
            exc,
        )
        return 1

    except AuthenticationError as exc:
        logger.error(
            "Error de autenticación: %s",
            exc,
        )
        return 2

    except GraphApiError as exc:
        logger.error(
            "Error de Microsoft Graph: %s",
            exc,
        )
        return 3

    except KeyboardInterrupt:
        logger.warning(
            "Ejecución cancelada manualmente."
        )
        return 130

    except Exception:
        logger.exception(
            "Se produjo un error no controlado."
        )
        return 99


if __name__ == "__main__":
    sys.exit(main())