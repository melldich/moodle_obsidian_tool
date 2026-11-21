from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from moodle2obsidian.auth.password import password_login
from moodle2obsidian.auth.sso import _is_logged_in_page, build_launch_url, parse_launch_redirect
from moodle2obsidian.config import MoodleConfig
from moodle2obsidian.errors import M2OError

WWWROOT = "https://moodle.test"
PASSPORT = "a1b2c3d4"
TOKEN = "0123456789abcdef0123456789abcdef"  # gitleaks:allow (fake test value)
PRIVATE = "fedcba9876543210fedcba9876543210"


def redirect(passport: str = PASSPORT, private: str | None = PRIVATE, scheme: str = "moodlemobile") -> str:
    sig = hashlib.md5((WWWROOT + passport).encode(), usedforsecurity=False).hexdigest()
    raw = ":::".join([sig, TOKEN, *([private] if private else [])])
    return f"{scheme}://token=" + base64.b64encode(raw.encode()).decode()


def parse(url: str) -> tuple[str, str | None]:
    tokens = parse_launch_redirect(url, wwwroot=WWWROOT, passport=PASSPORT, scheme="moodlemobile")
    return tokens.token, tokens.privatetoken


def test_parses_valid_redirect() -> None:
    assert parse(redirect()) == (TOKEN, PRIVATE)
    assert parse(redirect(private=None)) == (TOKEN, None)


def test_parses_intent_redirect() -> None:
    url = redirect().replace("moodlemobile://", "intent://") + "#Intent;scheme=moodlemobile;end"
    assert parse(url)[0] == TOKEN


def test_rejects_wrong_signature() -> None:
    with pytest.raises(M2OError, match="signature"):
        parse(redirect(passport="other"))


@pytest.mark.parametrize(
    "url", ["https://evil.test/?token=x", "moodlemobile://token=!!!notbase64", "moodlemobile://token="]
)
def test_rejects_garbage(url: str) -> None:
    with pytest.raises(M2OError):
        parse(url)


def test_tokens_repr_hides_values() -> None:
    tokens = parse_launch_redirect(redirect(), wwwroot=WWWROOT, passport=PASSPORT, scheme="moodlemobile")
    assert TOKEN not in repr(tokens)


def test_build_launch_url(moodle_cfg: MoodleConfig) -> None:
    url = build_launch_url(moodle_cfg, PASSPORT)
    assert url.startswith("https://moodle.test/admin/tool/mobile/launch.php?")
    query = parse_qs(urlsplit(url).query)
    assert query == {"service": ["moodle_mobile_app"], "passport": [PASSPORT], "urlscheme": ["moodlemobile"]}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://moodle.test/my/", True),
        ("https://moodle.test/login/index.php", False),
        ("https://moodle.test/admin/oauth2/callback.php?x", False),
        ("https://accounts.google.com/signin", False),
        ("https://moodle.test.evil.com/my/", False),
    ],
)
def test_is_logged_in_page(url: str, expected: bool) -> None:
    assert _is_logged_in_page(url, WWWROOT) is expected


@respx.mock
def test_password_login_success(moodle_cfg: MoodleConfig) -> None:
    route = respx.post("https://moodle.test/login/token.php").mock(
        return_value=httpx.Response(200, json={"token": TOKEN, "privatetoken": PRIVATE})
    )
    tokens = password_login(moodle_cfg, "student", "pa55word!")
    assert (tokens.token, tokens.privatetoken) == (TOKEN, PRIVATE)
    assert "pa55word" not in str(route.calls.last.request.url)


@respx.mock
def test_password_login_rejected(moodle_cfg: MoodleConfig) -> None:
    respx.post("https://moodle.test/login/token.php").mock(
        return_value=httpx.Response(200, json={"error": "Invalid login", "errorcode": "invalidlogin"})
    )
    with pytest.raises(M2OError, match="Invalid login"):
        password_login(moodle_cfg, "student", "wrong")
