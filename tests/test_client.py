from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest
import respx

from moodle2obsidian.config import MoodleConfig
from moodle2obsidian.errors import M2OError
from moodle2obsidian.moodle.client import (
    MoodleClient,
    MoodleError,
    fetch_public_config,
    flatten_params,
    normalize_site_url,
)

WS = "https://moodle.test/webservice/rest/server.php"
TOKEN = "a" * 32


def client(cfg: MoodleConfig) -> MoodleClient:
    return MoodleClient(cfg, TOKEN, sleep=lambda _: None)


@respx.mock
def test_call_posts_token_in_body_and_flattens_params(moodle_cfg: MoodleConfig) -> None:
    route = respx.post(WS).mock(return_value=httpx.Response(200, json=[{"id": 5}]))
    with client(moodle_cfg) as c:
        result = c.call("core_course_get_contents", courseid=5, options=[{"name": "excludemodules", "value": True}])
    assert result == [{"id": 5}]
    request = route.calls.last.request
    body = parse_qs(request.content.decode())
    assert body["wsfunction"] == ["core_course_get_contents"]
    assert body["courseid"] == ["5"]
    assert body["options[0][name]"] == ["excludemodules"]
    assert body["options[0][value]"] == ["1"]
    assert TOKEN not in str(request.url)
    assert request.headers["user-agent"].startswith("moodle2obsidian/")


@respx.mock
def test_moodle_exception_raises(moodle_cfg: MoodleConfig) -> None:
    respx.post(WS).mock(
        return_value=httpx.Response(
            200,
            json={"exception": "moodle_exception", "errorcode": "invalidtoken", "message": "Invalid token"},
        )
    )
    with client(moodle_cfg) as c, pytest.raises(MoodleError) as err:
        c.call("core_webservice_get_site_info")
    assert err.value.errorcode == "invalidtoken"


@respx.mock
def test_redirect_is_not_followed(moodle_cfg: MoodleConfig) -> None:
    respx.post(WS).mock(return_value=httpx.Response(303, headers={"location": "https://evil.test/steal"}))
    evil = respx.route(host="evil.test").mock(return_value=httpx.Response(200))
    with client(moodle_cfg) as c, pytest.raises(M2OError, match=r"evil.test"):
        c.call("core_webservice_get_site_info")
    assert not evil.called
    assert respx.calls.call_count == 1


@respx.mock
def test_retries_on_5xx(moodle_cfg: MoodleConfig) -> None:
    route = respx.post(WS).mock(side_effect=[httpx.Response(503), httpx.Response(200, json={"ok": 1})])
    with client(moodle_cfg) as c:
        assert c.call("x") == {"ok": 1}
    assert route.call_count == 2


@respx.mock
def test_gives_up_after_retries(moodle_cfg: MoodleConfig) -> None:
    route = respx.post(WS).mock(side_effect=httpx.ConnectError("boom"))
    with client(moodle_cfg) as c, pytest.raises(M2OError, match="ConnectError"):
        c.call("x")
    assert route.call_count == moodle_cfg.max_retries + 1


@respx.mock
def test_non_json_response(moodle_cfg: MoodleConfig) -> None:
    respx.post(WS).mock(return_value=httpx.Response(200, text="<html>Cloudflare</html>"))
    with client(moodle_cfg) as c, pytest.raises(M2OError, match="JSON"):
        c.call("x")


def test_flatten_params() -> None:
    assert flatten_params({"a": [1, {"b": False}], "c": None, "d": "x"}) == {
        "a[0]": "1",
        "a[1][b]": "0",
        "d": "x",
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://teaching.kse.org.ua/my/", "https://teaching.kse.org.ua"),
        ("moodle.example.edu/moodle/course/view.php?id=3", "https://moodle.example.edu/moodle"),
        ("http://x.edu/login/index.php", "https://x.edu"),
        ("https://x.edu/mymoodle/", "https://x.edu/mymoodle"),
        ("https://user:pw@x.edu:8443/", "https://x.edu:8443"),
    ],
)
def test_normalize_site_url(raw: str, expected: str) -> None:
    assert normalize_site_url(raw) == expected


@respx.mock
def test_fetch_public_config() -> None:
    respx.post("https://moodle.test/lib/ajax/service-nologin.php").mock(
        return_value=httpx.Response(
            200, json=[{"error": False, "data": {"wwwroot": "https://moodle.test", "identityproviders": []}}]
        )
    )
    assert fetch_public_config("https://moodle.test")["wwwroot"] == "https://moodle.test"


@respx.mock
def test_fetch_public_config_error() -> None:
    respx.post("https://moodle.test/lib/ajax/service-nologin.php").mock(
        return_value=httpx.Response(200, json=[{"error": True, "exception": {}}])
    )
    with pytest.raises(M2OError):
        fetch_public_config("https://moodle.test")
