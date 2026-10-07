from __future__ import annotations

import argparse

from app.business_normalizer import (
    normalize_smbc_forward,
)


def main() -> None:

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "ingestion_id",
        type=int,
    )

    args = parser.parse_args()

    print()
    print("=" * 70)
    print("NORMALIZACIÓN SMBC / FORWARD")
    print("=" * 70)

    print(
        f"Ingestion ID : "
        f"{args.ingestion_id}"
    )

    try:

        (
            normalized,
            warnings,
        ) = normalize_smbc_forward(
            args.ingestion_id
        )

    except Exception as exc:

        print()
        print(
            "[ERROR NORMALIZANDO]"
        )

        print(
            f"{type(exc).__name__}: "
            f"{exc}"
        )

        return

    print()
    print(
        f"Filas normalizadas : "
        f"{normalized}"
    )

    print(
        f"Con advertencias   : "
        f"{warnings}"
    )

    print()
    print(
        "[OK] Normalización finalizada."
    )


if __name__ == "__main__":
    main()