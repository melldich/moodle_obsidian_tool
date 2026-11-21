"""Moodle Web Services REST client: rate limiting, retries, and a strict same-host policy."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .. import __version__
from ..config import MoodleConfig
from ..errors import M2OError
from ..log import register_secret

log = logging.getLogger(__name__)
USER_AGENT = f"moodle2obsidian/{__version__} (+https://github.com/melldich/moodle_obsidian_tool)"


class MoodleError(M2OError):
    def __init__(self, errorcode: str, message: str) -> None:
        super().__init__(f"Ошибка Moodle [{errorcode}]: {message}", f"Moodle error [{errorcode}]: {message}")
        self.errorcode = errorcode


def new_http_client(timeout: float) -> httpx.Client:
    # follow_redirects=False: a redirect must never carry the token or cookies anywhere else
    return httpx.Client(timeout=timeout, follow_redirects=False, headers={"User-Agent": USER_AGENT})


def network_error(exc: Exception) -> M2OError:
    return M2OError(f"Сетевая ошибка: {type(exc).__name__}", f"Network error: {type(exc).__name__}")


def parse_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        raise M2OError(
            "Moodle вернул не JSON (страница Cloudflare, техработы или неверный адрес).",
            "Moodle returned non-JSON (Cloudflare page, maintenance or wrong URL).",
        ) from None


def flatten_params(params: Mapping[Any, Any], prefix: str = "") -> dict[str, str]:
    """Encode nested params the way Moodle REST expects: options[0][name]=x."""
    out: dict[str, str] = {}
    for key, value in params.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, Mapping):
            out.update(flatten_params(value, name))
        elif isinstance(value, list | tuple):
            out.update(flatten_params(dict(enumerate(value)), name))
        elif isinstance(value, bool):
            out[name] = "1" if value else "0"
        elif value is not None:
            out[name] = str(value)
    return out


class MoodleClient:
    def __init__(
        self,
        cfg: MoodleConfig,
        token: str,
        *,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        register_secret(token)
        self._cfg = cfg
        self._token = token
        self._http = http or new_http_client(cfg.timeout_sec)
        self._sleep = sleep
        self._min_interval = 1.0 / cfg.rate_limit_per_sec
        self._last_request = 0.0

    def __enter__(self) -> MoodleClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def call(self, wsfunction: str, **params: Any) -> Any:
        data = {
            "wstoken": self._token,  # POST body only, never in the URL
            "wsfunction": wsfunction,
            "moodlewsrestformat": "json",
            **flatten_params(params),
        }
        payload = parse_json(self._post(f"{self._cfg.url}/webservice/rest/server.php", data))
        if isinstance(payload, dict) and "exception" in payload:
            raise MoodleError(str(payload.get("errorcode", "unknown")), str(payload.get("message", "")))
        return payload

    def _post(self, url: str, data: dict[str, str]) -> httpx.Response:
        retries = self._cfg.max_retries
        for attempt in range(retries + 1):
            self._throttle()
            try:
                response = self._http.post(url, data=data)
            except httpx.TransportError as exc:
                if attempt == retries:
                    raise network_error(exc) from None
                log.warning("network error (%s), retry %d/%d", type(exc).__name__, attempt + 1, retries)
                self._sleep(2**attempt)
                continue
            if response.is_redirect:
                target = urlsplit(response.headers.get("location", "")).hostname or "?"
                raise M2OError(
                    f"Сервер перенаправил запрос на {target}; токен туда не отправлен. Проверьте moodle.url.",
                    f"Server redirected to {target}; the token was not sent there. Check moodle.url.",
                )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == retries:
                    break
                delay = _retry_after(response) or 2**attempt
                log.warning("HTTP %d, retry in %.0fs", response.status_code, delay)
                self._sleep(delay)
                continue
            if response.status_code != 200:
                break
            return response
        raise M2OError(f"Moodle ответил HTTP {response.status_code}", f"Moodle returned HTTP {response.status_code}")

    def _throttle(self) -> None:
        wait = self._last_request + self._min_interval - time.monotonic()
        if wait > 0:
            self._sleep(wait)
        self._last_request = time.monotonic()


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return min(float(response.headers["retry-after"]), 60.0)
    except (KeyError, ValueError):
        return None


_MOODLE_PAGE = re.compile(
    r"/(?:my|login|course|mod|user|admin|blocks|local|auth|calendar|grade|message)(?:/|$)|/index\.php$"
)


def normalize_site_url(raw: str) -> str:
    """Turn whatever the user pasted (…/my/, …/course/view.php?id=3) into the https site root."""
    raw = raw.strip()
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    if not parts.hostname:
        raise M2OError(f"Не похоже на адрес: {raw}", f"Not a URL: {raw}")
    path = parts.path
    if match := _MOODLE_PAGE.search(path):
        path = path[: match.start()]
    netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
    return urlunsplit(("https", netloc, path.rstrip("/"), "", ""))


def fetch_public_config(site_url: str, timeout: float = 30) -> dict[str, Any]:
    """tool_mobile_get_public_config — works without login; tells how users sign in."""
    body = [{"index": 0, "methodname": "tool_mobile_get_public_config", "args": {}}]
    with new_http_client(timeout) as http:
        try:
            response = http.post(f"{site_url}/lib/ajax/service-nologin.php", json=body)
        except httpx.TransportError as exc:
            raise network_error(exc) from None
    if response.status_code != 200:
        raise M2OError(
            f"{site_url} ответил HTTP {response.status_code} — это точно Moodle?",
            f"{site_url} returned HTTP {response.status_code} — is this a Moodle site?",
        )
    payload = parse_json(response)
    first = payload[0] if isinstance(payload, list) and payload else None
    data = first.get("data") if isinstance(first, dict) and not first.get("error") else None
    if not isinstance(data, dict):
        raise M2OError(
            "Сайт не отдал публичный конфиг Moodle (мобильный API выключен?).",
            "The site did not return Moodle's public config (mobile API disabled?).",
        )
    return data
