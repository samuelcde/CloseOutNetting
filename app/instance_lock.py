from __future__ import annotations

import msvcrt

from pathlib import Path


class SingleInstanceLock:

    def __init__(
        self,
        lock_file: Path,
    ) -> None:

        self.lock_file = lock_file
        self.handle = None

    def __enter__(self):

        self.lock_file.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.handle = self.lock_file.open(
            "a+b"
        )

        if self.lock_file.stat().st_size == 0:
            self.handle.write(
                b"0"
            )

            self.handle.flush()

        self.handle.seek(0)

        try:
            msvcrt.locking(
                self.handle.fileno(),
                msvcrt.LK_NBLCK,
                1,
            )

        except OSError as exc:

            self.handle.close()

            raise RuntimeError(
                "Ya existe otra instancia "
                "de Auto CloseNetting en ejecución."
            ) from exc

        return self

    def __exit__(
        self,
        exc_type,
        exc_value,
        traceback,
    ):

        if self.handle:

            self.handle.seek(0)

            try:
                msvcrt.locking(
                    self.handle.fileno(),
                    msvcrt.LK_UNLCK,
                    1,
                )

            finally:
                self.handle.close()