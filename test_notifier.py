from __future__ import annotations

import sys

from app.config import (
    load_operational_config,
    load_settings,
)

from app.notifier import (
    EmailNotifier,
    NotificationError,
)


def main() -> int:

    print()
    print("=" * 70)
    print("PRUEBA SMTP - AUTO CLOSENETTING")
    print("=" * 70)
    print()

    try:

        settings = load_settings()

        config = (
            load_operational_config()
        )

        print(
            f"SMTP Host : "
            f"{settings.smtp.host}"
        )

        print(
            f"SMTP Port : "
            f"{settings.smtp.port}"
        )

        print(
            f"TLS       : "
            f"{'Sí' if settings.smtp.use_tls else 'No'}"
        )

        print(
            f"Destinos  : "
            f"{len(config.alert_recipients)}"
        )

        # Nunca mostramos password.

        notifier = EmailNotifier(
            smtp_settings=settings.smtp,
            recipients=(
                config.alert_recipients
            ),
        )

        print()
        print(
            "Intentando enviar correo de prueba..."
        )
        print()

        notifier.send_test()

        print(
            "[OK] Correo enviado correctamente."
        )

        print()
        print(
            "Revise la bandeja de entrada "
            "de los destinatarios configurados."
        )

        return 0


    except NotificationError as exc:

        print()
        print(
            f"[ERROR SMTP] {exc}"
        )

        return 2


    except Exception as exc:

        print()
        print(
            f"[ERROR CONFIGURACIÓN] "
            f"{type(exc).__name__}: {exc}"
        )

        return 1


if __name__ == "__main__":
    sys.exit(
        main()
    )