import logging

import msal

from .config import Settings
from .exceptions import AuthenticationError


logger = logging.getLogger(__name__)

GRAPH_SCOPES = [
    "https://graph.microsoft.com/.default"
]


def get_access_token(
    settings: Settings,
) -> str:

    authority = (
        "https://login.microsoftonline.com/"
        f"{settings.tenant_id}"
    )

    logger.info(
        "Autenticando aplicación contra Microsoft Entra ID..."
    )

    app = msal.ConfidentialClientApplication(
        client_id=settings.client_id,
        authority=authority,
        client_credential=settings.client_secret,
    )

    result = app.acquire_token_for_client(
        scopes=GRAPH_SCOPES
    )

    token = result.get(
        "access_token"
    )

    if token:
        logger.info(
            "Autenticación completada correctamente."
        )

        return token

    raise AuthenticationError(
        "Microsoft Entra ID rechazó la autenticación: "
        f"{result.get('error', 'unknown_error')} - "
        f"{result.get('error_description', 'Sin descripción')}"
    )