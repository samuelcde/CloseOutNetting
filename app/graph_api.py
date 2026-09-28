from __future__ import annotations

import hashlib
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .exceptions import AttachmentTooLargeError, GraphApiError

logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

REQUEST_TIMEOUT = (5, 30)


@dataclass(frozen=True, slots=True)
class MessageWindow:
    target_date: date

    start_local: datetime
    end_local: datetime

    start_utc: datetime
    end_utc: datetime

def _compute_file_sha256(path: Path) -> str:
    """Calcula el hash SHA-256 de un archivo en disco en bloques de 64 KB."""
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(64 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()

class GraphClient:
    def __init__(self, access_token: str, mailbox_user: str) -> None:
        self.mailbox_user = mailbox_user

        retry = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
        )

        adapter = HTTPAdapter(max_retries=retry)
        self.session = requests.Session()
        self.session.mount("https://", adapter)
        self.session.headers.update(
            {"Authorization": f"Bearer {access_token}", "Accept": "application/json",}
        )

    def close(self) -> None:
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def _mailbox(self) -> str:
        return quote(self.mailbox_user, safe="@.")

    def list_messages_for_previous_business_day(
        self,
        *,
        timezone_name: str,
        late_arrival_grace_hours: int,
        page_size: int,
        max_messages: int,
    ) -> list[dict[str, Any]]:
        """
        Obtiene los mensajes cuya fecha de recepción pertenece
        a la ventana del día hábil anterior.

        Incluye:
            - día completo;
            - tolerancia para llegadas tardías;
            - paginación mediante @odata.nextLink;
            - límite defensivo max_messages.

        max_messages NO define qué correos corresponden al día.
        Solo funciona como fusible de seguridad.
        """

        window = build_previous_business_day_window(
            timezone_name=timezone_name,
            late_arrival_grace_hours=late_arrival_grace_hours,
        )

        start_graph = _graph_datetime(window.start_utc)

        end_graph = _graph_datetime(window.end_utc)

        logger.info(
            "Consultando correos del día hábil objetivo: %s",
            window.target_date.isoformat(),
        )

        logger.info(
            "Ventana local: %s -> %s | Zona: %s",
            window.start_local.isoformat(),
            window.end_local.isoformat(),
            timezone_name,
        )

        logger.info("Ventana Microsoft Graph UTC: %s -> %s", start_graph, end_graph)

        url = (f"{GRAPH_BASE_URL}" f"/users/{self._mailbox()}" f"/mailFolders/inbox/messages")

        params: dict[str, Any] | None = {
            "$top": page_size,
            "$select": ("id," "subject," "receivedDateTime," "from," "hasAttachments"),
            "$filter": (
                f"receivedDateTime ge {start_graph} " f"and receivedDateTime lt {end_graph}"
            ),
        }

        messages: list[dict[str, Any]] = []

        seen_next_links: set[str] = set()

        page_number = 0

        while url:
            page_number += 1

            try:
                response = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)

            except requests.RequestException as exc:
                raise GraphApiError(
                    "Error consultando mensajes " f"en Microsoft Graph: {exc}"
                ) from exc

            self._raise_for_graph_error(response)

            try:
                payload = response.json()

            except ValueError as exc:
                raise GraphApiError(
                    "Microsoft Graph devolvió " "una respuesta JSON inválida."
                ) from exc

            page_messages = payload.get("value")

            if not isinstance(page_messages, list):
                raise GraphApiError("Microsoft Graph no devolvió " "la colección esperada 'value'.")

            logger.info("Página Graph %s: %s mensaje(s).", page_number, len(page_messages))

            # ====================================================
            # TOPE DEFENSIVO
            # ====================================================

            remaining_capacity = max_messages - len(messages)

            if remaining_capacity <= 0:
                break

            messages.extend(page_messages[:remaining_capacity])

            if len(messages) >= max_messages:
                next_link = payload.get("@odata.nextLink")

                if next_link:
                    logger.warning(
                        "Se alcanzó el límite defensivo de "
                        "%s mensajes. Existen más resultados "
                        "en Microsoft Graph.",
                        max_messages,
                    )

                break

            # ====================================================
            # PAGINACIÓN
            # ====================================================

            next_link = payload.get("@odata.nextLink")

            if not next_link:
                break

            if not isinstance(next_link, str):
                raise GraphApiError("Microsoft Graph devolvió un " "@odata.nextLink inválido.")

            # Protección frente a un nextLink repetido.
            if next_link in seen_next_links:
                raise GraphApiError("Microsoft Graph devolvió un " "@odata.nextLink repetido.")

            seen_next_links.add(next_link)

            # nextLink ya contiene todos los query parameters.
            url = next_link
            params = None

        logger.info(
            "Total de mensajes encontrados para " "%s: %s",
            window.target_date.isoformat(),
            len(messages),
        )

        return messages

    def list_attachments(self, message_id: str) -> list[dict[str, Any]]:
        message = quote(message_id, safe="")
        url = f"{GRAPH_BASE_URL}/users/{self._mailbox()}/messages/{message}/attachments"
        params = {"$select": "id,name,size,isInline,contentType"}

        response = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        self._raise_for_graph_error(response)
        return response.json().get("value", [])

    def download_attachment(
        self,
        *,
        message_id: str,
        attachment_id: str,
        destination: Path,
        max_bytes: int,
    ) -> tuple[int, str]:
        message = quote(message_id, safe="")
        attachment = quote(attachment_id, safe="")
        url = (
            f"{GRAPH_BASE_URL}"
            f"/users/{self._mailbox()}"
            f"/messages/{message}"
            f"/attachments/{attachment}"
            f"/$value"
        )

        response = self.session.get(url, timeout=REQUEST_TIMEOUT, stream=True)
        self._raise_for_graph_error(response)

        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                announced_size = int(content_length)
            except ValueError:
                announced_size = 0

            if announced_size > max_bytes:
                response.close()
                raise AttachmentTooLargeError(f"El servidor anunció {announced_size} bytes.")

        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")

        total = 0
        digest = hashlib.sha256()

        try:
            with temporary.open("xb") as file_handle:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise AttachmentTooLargeError(
                            "El adjunto superó el límite durante la descarga."
                        )
                    digest.update(chunk)
                    file_handle.write(chunk)

            incoming_hash = digest.hexdigest()

            # --------------------------------------------------------
            # VALIDACIÓN POR HASH (EVITA DUPLICADOS)
            # --------------------------------------------------------
            if destination.exists():
                existing_hash = _compute_file_sha256(destination)

                # CASO 1: Contenido idéntico
                if existing_hash == incoming_hash:
                    temporary.unlink(missing_ok=True)
                    logger.info(
                        "El archivo '%s' ya existe con contenido idéntico "
                        "(SHA-256: %s). Se omite duplicado.",
                        destination.name,
                        incoming_hash[:12],
                    )
                    return (total, incoming_hash)

                # CASO 2: Mismo nombre pero contenido nuevo/corregido
                clean_id = quote(attachment_id[:8], safe="")
                versioned_destination = destination.with_name(
                    f"{destination.stem}__{clean_id}{destination.suffix}"
                )
                os.replace(temporary, versioned_destination)
                logger.warning(
                    "El archivo '%s' ya existe pero con contenido DIFERENTE. Guardado como '%s'.",
                    destination.name,
                    versioned_destination.name,
                )
                return (total, incoming_hash)

            # CASO 3: Archivo nuevo
            os.replace(temporary, destination)

        except Exception:
            temporary.unlink(missing_ok=True)
            raise

        finally:
            response.close()

        return (total, incoming_hash)

    @staticmethod
    def _raise_for_graph_error(response: requests.Response) -> None:
        if response.ok:
            return

        try:
            payload = response.json()
            graph_error = payload.get("error", {})
            detail = graph_error.get("code", "") + ": " + graph_error.get("message", "")
        except ValueError:
            detail = response.text[:500]

        raise GraphApiError(f"Microsoft Graph HTTP {response.status_code}: {detail}")

def _get_previous_business_date(current_date: date) -> date:
    """
    Obtiene el día hábil anterior.

    Reglas:
        lunes     -> viernes
        martes    -> lunes
        miércoles -> martes
        jueves    -> miércoles
        viernes   -> jueves
        sábado    -> viernes
        domingo   -> viernes

    Por ahora se consideran hábiles únicamente
    lunes a viernes. No se evalúan festivos.
    """

    weekday = current_date.weekday()

    if weekday == 0:
        days_back = 3

    elif weekday == 5:
        days_back = 1

    elif weekday == 6:
        days_back = 2

    else:
        days_back = 1

    return current_date - timedelta(days=days_back)

def build_previous_business_day_window(
    *,
    timezone_name: str,
    late_arrival_grace_hours: int,
    now: datetime | None = None,
) -> MessageWindow:
    """
    Construye la ventana temporal correspondiente al
    día hábil anterior.

    Ejemplo ejecutándose un lunes:

        Viernes 00:00 America/Bogota
            hasta
        Sábado 12:00 America/Bogota

    si late_arrival_grace_hours = 12.

    La ventana nunca se extiende más allá del momento
    real de ejecución.
    """

    local_timezone = ZoneInfo(timezone_name)

    if now is None:
        now_local = datetime.now(local_timezone)

    else:
        if now.tzinfo is None:
            raise ValueError("'now' debe contener información " "de zona horaria.")

        now_local = now.astimezone(local_timezone)

    target_date = _get_previous_business_date(now_local.date())

    start_local = datetime.combine(target_date, time.min, tzinfo=local_timezone)

    next_calendar_day = target_date + timedelta(days=1)

    nominal_end_local = datetime.combine(
        next_calendar_day,
        time.min,
        tzinfo=local_timezone,
    ) + timedelta(hours=late_arrival_grace_hours)

    # Nunca consultar fechas futuras.
    end_local = min(nominal_end_local, now_local)

    start_utc = start_local.astimezone(timezone.utc)

    end_utc = end_local.astimezone(timezone.utc)

    return MessageWindow(
        target_date=target_date,
        start_local=start_local,
        end_local=end_local,
        start_utc=start_utc,
        end_utc=end_utc,
    )

def _graph_datetime(value: datetime) -> str:
    """
    Convierte datetime timezone-aware al formato UTC
    utilizado en los filtros de Microsoft Graph.

    Ejemplo:
        2026-09-18T05:00:00Z
    """

    return (value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
