"""Private file helpers: secrets/state files 0600, their directories 0700."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .errors import M2OError


def chmod_private(path: Path, mode: int) -> None:
    # On Windows chmod only toggles read-only; files live in per-user %LOCALAPPDATA% protected by ACLs.
    if os.name != "nt":
        os.chmod(path, mode)


def secure_mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    chmod_private(path, 0o700)
    return path


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    """One sync at a time. The OS releases the lock even if the process crashes (no stale lock files)."""
    secure_mkdir(path.parent)
    with path.open("a+b") as f:
        try:
            if sys.platform == "win32":
                import msvcrt

                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise M2OError(
                "Другой `m2o sync` уже запущен — дождитесь его окончания.",
                "Another `m2o sync` is already running — wait for it to finish.",
            ) from None
        yield


def secure_write_text(path: Path, text: str) -> None:
    secure_mkdir(path.parent)
    if path.exists():
        chmod_private(path, 0o600)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    chmod_private(path, 0o600)
