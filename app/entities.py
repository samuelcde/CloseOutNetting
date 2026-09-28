import logging

from typing import Any

from .config import OperationalConfig


logger = logging.getLogger(__name__)


def get_sender_address(
    message: dict[str, Any],
) -> str:

    return str(
        message
        .get("from", {})
        .get("emailAddress", {})
        .get("address", "")
    ).strip().lower()


def resolve_entity(
    message: dict[str, Any],
    config: OperationalConfig,
) -> str:

    sender = get_sender_address(
        message
    )

    if "@" not in sender:
        logger.warning(
            "Remitente inválido. "
            "Se asignará a %s.",
            config.default_entity,
        )

        return config.default_entity

    domain = sender.rsplit(
        "@",
        1,
    )[1]

    for configured_domain, entity in (
        config.entities.items()
    ):

        if (
            domain == configured_domain
            or domain.endswith(
                f".{configured_domain}"
            )
        ):
            return entity

    logger.warning(
        "Dominio no configurado '%s'. "
        "Se almacenará en %s.",
        domain,
        config.default_entity,
    )

    return config.default_entity