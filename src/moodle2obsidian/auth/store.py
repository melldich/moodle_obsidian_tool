"""Secret storage: system keyring first, `.env` (0600) in the config dir as a fallback."""

from __future__ import annotations

import logging
import os
import re
from contextlib import suppress
from pathlib import Path

import keyring
from keyring.errors import KeyringError

from ..config import config_dir
from ..fsutil import secure_write_text
from ..log import register_secret

log = logging.getLogger(__name__)
SERVICE = "moodle2obsidian"


def env_var_name(key: str) -> str:
    return "M2O_" + re.sub(r"\W", "_", key).upper()


def env_file() -> Path:
    return config_dir() / ".env"


def _keyring_usable() -> bool:
    try:
        # fail/null backends report priority < 1
        return float(getattr(keyring.get_keyring(), "priority", 0)) >= 1
    except Exception:  # broken keyring setups raise anything
        return False


def get_secret(key: str) -> str | None:
    value = _lookup(key)
    register_secret(value)
    return value


def _lookup(key: str) -> str | None:
    for name in dict.fromkeys((env_var_name(key), env_var_name(key.split("@")[0]))):
        if value := os.environ.get(name):
            return value
    if _keyring_usable():
        try:
            if value := keyring.get_password(SERVICE, key):
                return value
        except KeyringError as exc:
            log.warning("keyring read failed (%s)", type(exc).__name__)
    return _read_env_file().get(env_var_name(key))


def set_secret(key: str, value: str) -> str:
    """Store a secret; returns a human-readable description of where it went."""
    register_secret(value)
    if _keyring_usable():
        try:
            keyring.set_password(SERVICE, key, value)
            return "system keyring"
        except KeyringError as exc:
            log.warning("keyring write failed (%s), falling back to .env", type(exc).__name__)
    data = _read_env_file()
    data[env_var_name(key)] = value
    _write_env_file(data)
    return str(env_file())


def delete_secret(key: str) -> None:
    if _keyring_usable():
        with suppress(KeyringError):
            keyring.delete_password(SERVICE, key)
    data = _read_env_file()
    if data.pop(env_var_name(key), None) is not None:
        _write_env_file(data)


def _read_env_file() -> dict[str, str]:
    path = env_file()
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        out[name.strip()] = value.strip().strip("\"'")
    return out


def _write_env_file(data: dict[str, str]) -> None:
    body = "".join(f"{name}={value}\n" for name, value in data.items())
    secure_write_text(env_file(), "# moodle2obsidian secrets — never commit this file\n" + body)
