"""SSO login (Google, Microsoft, CAS, SAML): the user signs in in a visible browser, then we get a
token the way the Moodle mobile app does: admin/tool/mobile/launch.php → <scheme>://token=<base64>.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlencode, urlsplit

from ..config import MoodleConfig
from ..errors import M2OError
from ..fsutil import secure_write_text
from ..log import register_secret
from . import Tokens

if TYPE_CHECKING:
    from playwright.sync_api import Page, Request, Response

log = logging.getLogger(__name__)
_TOKEN_RE = re.compile(r"[A-Za-z0-9]{16,128}")
# pages we pass through while signing in; landing anywhere else on the site means "logged in"
_AUTH_PATHS = ("/login/", "/oauth2/", "/auth/", "/admin/tool/mobile/")


def build_launch_url(cfg: MoodleConfig, passport: str) -> str:
    query = urlencode({"service": cfg.service, "passport": passport, "urlscheme": cfg.url_scheme})
    return f"{cfg.url}/admin/tool/mobile/launch.php?{query}"


def parse_launch_redirect(url: str, *, wwwroot: str, passport: str, scheme: str) -> Tokens:
    if url.startswith("intent://"):  # Android-style redirect some Moodle versions emit
        url = f"{scheme}://" + url[len("intent://") :]
    prefix = f"{scheme}://token="
    if not url.startswith(prefix):
        raise _bad("неожиданный адрес перенаправления", "unexpected redirect target")
    payload = unquote(url[len(prefix) :]).split("#", 1)[0].strip()
    try:
        decoded = base64.b64decode(payload + "=" * (-len(payload) % 4), validate=True).decode("ascii")
    except ValueError:
        raise _bad("это не base64", "not base64") from None
    parts = decoded.split(":::")
    # Moodle signs the payload with md5(wwwroot + passport); this is its protocol, not our crypto choice
    expected = hashlib.md5((wwwroot + passport).encode(), usedforsecurity=False).hexdigest()
    if len(parts) < 2 or not hmac.compare_digest(parts[0], expected):
        raise _bad("подпись не совпала", "signature mismatch")
    if not _TOKEN_RE.fullmatch(parts[1]):
        raise _bad("неверный формат токена", "malformed token")
    private = parts[2] if len(parts) > 2 and _TOKEN_RE.fullmatch(parts[2]) else None
    register_secret(parts[1])
    register_secret(private)
    return Tokens(parts[1], private)


def _bad(ru: str, en: str) -> M2OError:
    return M2OError(f"Не удалось разобрать токен из launch.php: {ru}", f"Could not parse launch.php token: {en}")


def _is_logged_in_page(url: str, site_url: str) -> bool:
    if not url.startswith(site_url + "/") and url != site_url:
        return False
    path = urlsplit(url).path
    return not any(part in path for part in _AUTH_PATHS)


def sso_login(cfg: MoodleConfig, cookies_path: Path, timeout_sec: float = 600) -> Tokens | None:
    """Return tokens, or None if only a browser session (cookies) could be saved."""
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise M2OError("Playwright не установлен", "Playwright is not installed") from None

    passport = secrets.token_hex(8)
    launch_url = build_launch_url(cfg, passport)
    prefixes = (f"{cfg.url_scheme}://", "intent://")
    captured: list[str] = []

    def on_request(request: Request) -> None:
        if request.url.startswith(prefixes):
            captured.append(request.url)

    def on_response(response: Response) -> None:
        location = response.headers.get("location", "")
        if location.startswith(prefixes):
            captured.append(location)

    def goto(page: Page, url: str) -> None:
        with suppress(PlaywrightError):  # navigation to the custom scheme aborts — that's expected
            page.goto(url)

    cookies: list[Any] = []
    with sync_playwright() as pw:
        channel = None if cfg.browser_channel == "chromium" else cfg.browser_channel
        try:
            browser = pw.chromium.launch(headless=False, channel=channel)
        except PlaywrightError:
            raise M2OError(
                f"Не удалось запустить браузер «{cfg.browser_channel}». Установите Chrome/Edge "
                "или выполните `uv run playwright install chromium` и задайте moodle.browser_channel: chromium",
                f"Could not start browser '{cfg.browser_channel}'. Install Chrome/Edge, or run "
                "`uv run playwright install chromium` and set moodle.browser_channel: chromium",
            ) from None
        context = browser.new_context()  # fresh profile: nothing of the user's Google session is kept
        context.on("request", on_request)
        context.on("response", on_response)
        page = context.new_page()
        goto(page, launch_url)
        relaunched = False
        deadline = time.monotonic() + timeout_sec
        while not captured and time.monotonic() < deadline and browser.is_connected():
            try:
                page.wait_for_timeout(500)
                current = page.url
            except PlaywrightError:
                break  # the user closed the window
            if not relaunched and _is_logged_in_page(current, cfg.url):
                # SSO sometimes lands on /my/ instead of returning to launch.php
                relaunched = True
                goto(page, launch_url)
        if not captured:
            with suppress(PlaywrightError):
                cookies = list(context.cookies([cfg.url]))  # Moodle host only, never Google cookies
        with suppress(PlaywrightError):
            browser.close()

    if captured:
        return parse_launch_redirect(captured[0], wwwroot=cfg.url, passport=passport, scheme=cfg.url_scheme)
    if any(str(c.get("name", "")).startswith("MoodleSession") for c in cookies):
        for cookie in cookies:
            register_secret(str(cookie.get("value", "")))
        secure_write_text(cookies_path, json.dumps(cookies, indent=2))
        log.info("saved Moodle session cookies to %s", cookies_path)
        return None
    raise M2OError(
        "Токен не получен: истекло время ожидания или окно браузера закрыто.",
        "No token received: timed out or the browser window was closed.",
    )
