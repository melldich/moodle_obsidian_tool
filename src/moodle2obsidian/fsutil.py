"""Private file helpers: secrets/state files 0600, their directories 0700."""

from __future__ import annotations

import os
from pathlib import Path


def chmod_private(path: Path, mode: int) -> None:
    # On Windows chmod only toggles read-only; files live in per-user %LOCALAPPDATA% protected by ACLs.
    if os.name != "nt":
        os.chmod(path, mode)


def secure_mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    chmod_private(path, 0o700)
    return path


def secure_write_text(path: Path, text: str) -> None:
    secure_mkdir(path.parent)
    if path.exists():
        chmod_private(path, 0o600)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    chmod_private(path, 0o600)
