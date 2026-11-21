from __future__ import annotations

import hashlib
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from moodle2obsidian.config import MoodleConfig
from moodle2obsidian.errors import M2OError
from moodle2obsidian.fetch.download import download_file
from moodle2obsidian.moodle.client import MoodleClient

TOKEN = "b" * 32
FILE_URL = "https://moodle.test/webservice/pluginfile.php/1/mod_resource/content/1/a.pdf"


@pytest.fixture
def client(moodle_cfg: MoodleConfig) -> MoodleClient:
    return MoodleClient(moodle_cfg, TOKEN, sleep=lambda _: None)


def get(client: MoodleClient, dest: Path, url: str = FILE_URL, **kwargs: Any) -> tuple[str, int]:
    return download_file(client, url, dest, max_bytes=kwargs.pop("max_bytes", 1000), timeout=5, **kwargs)


def _resolve_to(monkeypatch: pytest.MonkeyPatch, address: str) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))],
    )


@respx.mock
def test_downloads_with_token_and_hashes(client: MoodleClient, tmp_path: Path) -> None:
    route = respx.get(url__startswith=FILE_URL).mock(return_value=httpx.Response(200, content=b"%PDF-1"))
    sha, size = get(client, tmp_path / "a.pdf", expected_mimetype="application/pdf")
    assert (tmp_path / "a.pdf").read_bytes() == b"%PDF-1"
    assert (sha, size) == (hashlib.sha256(b"%PDF-1").hexdigest(), 6)
    assert route.calls.last.request.url.params["token"] == TOKEN
    assert not list(tmp_path.glob("*.part"))


def test_refuses_foreign_host(client: MoodleClient, tmp_path: Path) -> None:
    with pytest.raises(M2OError, match="not on the Moodle host"):
        get(client, tmp_path / "x", url="https://evil.test/file.pdf")
    with pytest.raises(M2OError):
        get(client, tmp_path / "x", url="http://moodle.test/file.pdf")


@respx.mock
def test_size_limit_from_header(client: MoodleClient, tmp_path: Path) -> None:
    respx.get(url__startswith=FILE_URL).mock(return_value=httpx.Response(200, content=b"x" * 50))
    with pytest.raises(M2OError, match="limit"):
        get(client, tmp_path / "a.pdf", max_bytes=10)
    assert not (tmp_path / "a.pdf").exists()


@respx.mock
def test_size_limit_while_streaming(client: MoodleClient, tmp_path: Path) -> None:
    respx.get(url__startswith=FILE_URL).mock(return_value=httpx.Response(200, content=iter([b"x" * 8] * 5)))
    with pytest.raises(M2OError, match="limit"):
        get(client, tmp_path / "a.pdf", max_bytes=20)
    assert not list(tmp_path.iterdir())


@respx.mock
def test_moodle_json_error(client: MoodleClient, tmp_path: Path) -> None:
    respx.get(url__startswith=FILE_URL).mock(
        return_value=httpx.Response(200, json={"error": "Invalid token", "errorcode": "invalidtoken"})
    )
    with pytest.raises(M2OError, match="invalidtoken"):
        get(client, tmp_path / "a.pdf", expected_mimetype="application/pdf")


@respx.mock
def test_html_instead_of_pdf(client: MoodleClient, tmp_path: Path) -> None:
    respx.get(url__startswith=FILE_URL).mock(
        return_value=httpx.Response(200, html="<html>login</html>", headers={"content-type": "text/html"})
    )
    with pytest.raises(M2OError, match="HTML"):
        get(client, tmp_path / "a.pdf", expected_mimetype="application/pdf")


@respx.mock
def test_redirect_to_private_network_refused(
    client: MoodleClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _resolve_to(monkeypatch, "10.0.0.5")
    respx.get(url__startswith=FILE_URL).mock(
        return_value=httpx.Response(302, headers={"location": "https://internal.test/a.pdf"})
    )
    internal = respx.get(host="internal.test").mock(return_value=httpx.Response(200, content=b"secret"))
    with pytest.raises(M2OError, match="private"):
        get(client, tmp_path / "a.pdf")
    assert not internal.called


@respx.mock
def test_redirect_to_cdn_drops_token(client: MoodleClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _resolve_to(monkeypatch, "93.184.216.34")
    respx.get(url__startswith=FILE_URL).mock(
        return_value=httpx.Response(302, headers={"location": "https://cdn.test/blob?sig=1"})
    )
    cdn = respx.get(host="cdn.test").mock(return_value=httpx.Response(200, content=b"data"))
    get(client, tmp_path / "a.pdf")
    assert TOKEN not in str(cdn.calls.last.request.url)
    assert (tmp_path / "a.pdf").read_bytes() == b"data"


@respx.mock
def test_redirect_echoing_token_refused(client: MoodleClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _resolve_to(monkeypatch, "93.184.216.34")
    respx.get(url__startswith=FILE_URL).mock(
        return_value=httpx.Response(302, headers={"location": f"https://cdn.test/x?t={TOKEN}"})
    )
    with pytest.raises(M2OError, match="leak"):
        get(client, tmp_path / "a.pdf")
