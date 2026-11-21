"""Server data is untrusted: sanitize file names/paths and refuse unsafe URLs."""

from __future__ import annotations

import ipaddress
import re
import socket
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

from .errors import M2OError

_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_BAD_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')
ALLOWED_SCHEMES = {"http", "https"}


def sanitize_filename(name: str, max_len: int = 120) -> str:
    """One safe path component: no separators, no traversal, no Windows-reserved names."""
    name = unicodedata.normalize("NFC", name)
    name = _BAD_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        return "_"
    if name.split(".")[0].strip().upper() in _RESERVED:
        name = "_" + name
    if len(name) > max_len:
        stem, dot, ext = name.rpartition(".")
        suffix = f".{ext}" if dot and stem and len(ext) <= 10 else ""
        name = name[: max_len - len(suffix)].rstrip(" .") + suffix
    return name or "_"


def split_moodle_path(filepath: str) -> list[str]:
    """Moodle filepath like '/sub/dir/' → ['sub', 'dir'], dropping '.', '..' and empty parts."""
    return [p for p in filepath.replace("\\", "/").split("/") if p not in ("", ".", "..")]


def safe_join(base: Path, *parts: str) -> Path:
    """Join sanitized components under base; the result must stay inside base."""
    root = base.resolve()
    target = root.joinpath(*(sanitize_filename(p) for p in parts)).resolve()
    if target == root or not target.is_relative_to(root):
        raise M2OError(f"Небезопасный путь: {target}", f"Unsafe path: {target}")
    return target


def check_public_url(url: str) -> None:
    """Allow only http(s) URLs that resolve to public addresses (SSRF guard)."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES or not parts.hostname:
        raise M2OError(f"Запрещённая ссылка: {scheme or '?'}://", f"Blocked URL scheme: {scheme or '?'}://")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if scheme == "https" else 80))
    except (socket.gaierror, UnicodeError):
        raise M2OError(f"Не удалось разрешить {parts.hostname}", f"Cannot resolve {parts.hostname}") from None
    # shortcut: checks DNS once; a rebinding host could still flip later. Pin the IP if this ever matters.
    for info in infos:
        ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise M2OError(
                f"Ссылка ведёт во внутреннюю сеть ({parts.hostname}) — пропускаю",
                f"URL points to a private/local address ({parts.hostname}) — skipped",
            )
