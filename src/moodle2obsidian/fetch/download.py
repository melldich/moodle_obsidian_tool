"""Download Moodle files: token only to the Moodle host, size limits, type checks, atomic writes."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from ..errors import M2OError
from ..moodle.client import MoodleClient, network_error
from ..safety import check_public_url

MAX_REDIRECTS = 3


class DownloadError(M2OError):
    pass


def download_file(
    client: MoodleClient,
    url: str,
    dest: Path,
    *,
    max_bytes: int,
    timeout: float,
    expected_mimetype: str = "",
) -> tuple[str, int]:
    """Download to dest (via dest.part); returns (sha256, size)."""
    current = client.authed_url(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        for _ in range(MAX_REDIRECTS + 1):
            client.throttle()
            with client.http.stream("GET", current, timeout=timeout) as response:
                if response.is_redirect:
                    current = _next_hop(client, current, response.headers.get("location", ""))
                    continue
                return _save(response, tmp, dest, max_bytes, expected_mimetype)
        raise DownloadError("Слишком много перенаправлений", "Too many redirects")
    except httpx.TransportError as exc:
        raise network_error(exc) from None
    finally:
        tmp.unlink(missing_ok=True)


def _next_hop(client: MoodleClient, current: str, location: str) -> str:
    target = urljoin(current, location)
    parts = urlsplit(target)
    if parts.hostname == client.host:
        if parts.scheme != "https":
            raise DownloadError("Перенаправление на http — отказ", "Redirect to plain http refused")
        return target
    # foreign host (e.g. S3 file storage): never forward the token, and never go to private networks
    if client.leaks_token(target):
        raise DownloadError("Перенаправление содержит токен — отказ", "Redirect would leak the token")
    check_public_url(target)
    return target


def _save(response: httpx.Response, tmp: Path, dest: Path, max_bytes: int, expected_mimetype: str) -> tuple[str, int]:
    if response.status_code != 200:
        raise DownloadError(f"HTTP {response.status_code}", f"HTTP {response.status_code}")
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        raise _too_big(max_bytes)
    ctype = response.headers.get("content-type", "").lower()
    if ctype.startswith("application/json") and not expected_mimetype.endswith("json"):
        error = _moodle_error(response)
        raise DownloadError(
            f"Moodle вернул ошибку вместо файла: {error}", f"Moodle returned an error instead of the file: {error}"
        )
    if ctype.startswith("text/html") and expected_mimetype and "html" not in expected_mimetype:
        raise DownloadError(
            "Сервер вернул HTML-страницу вместо файла", "Server returned an HTML page instead of the file"
        )

    digest = hashlib.sha256()
    size = 0
    with tmp.open("wb") as f:
        for chunk in response.iter_bytes(1 << 16):
            size += len(chunk)
            if size > max_bytes:
                raise _too_big(max_bytes)
            digest.update(chunk)
            f.write(chunk)
    os.replace(tmp, dest)
    return digest.hexdigest(), size


def _too_big(max_bytes: int) -> DownloadError:
    mb = max_bytes // (1024 * 1024)
    return DownloadError(f"Файл больше лимита {mb} МБ", f"File exceeds the {mb} MB limit")


def _moodle_error(response: httpx.Response) -> str:
    try:
        data = json.loads(next(response.iter_bytes(4096), b""))
    except ValueError:
        return "?"
    return str(data.get("errorcode") or data.get("error") or "?") if isinstance(data, dict) else "?"
