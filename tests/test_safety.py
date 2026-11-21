from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pytest

from moodle2obsidian.errors import M2OError
from moodle2obsidian.safety import check_public_url, safe_join, sanitize_filename, split_moodle_path


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("lecture.pdf", "lecture.pdf"),
        ("../../etc/passwd", "_.._etc_passwd"),
        ("..", "_"),
        ("", "_"),
        ("a\\b/c:d*e?f", "a_b_c_d_e_f"),
        ("CON", "_CON"),
        ("con.txt", "_con.txt"),
        ("LPT1.pdf", "_LPT1.pdf"),
        ("name. ", "name"),
        ("tab\there\x00", "tab_here_"),
        ("Cafe\u0301.pdf", "Café.pdf"),  # NFC normalization
    ],
)
def test_sanitize_filename(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_truncates_keeping_extension() -> None:
    name = sanitize_filename("x" * 300 + ".pdf", max_len=50)
    assert len(name) == 50
    assert name.endswith(".pdf")


def test_split_moodle_path() -> None:
    assert split_moodle_path("/../a/./b\\..\\c/") == ["a", "b", "c"]


def test_safe_join_stays_inside(tmp_path: Path) -> None:
    target = safe_join(tmp_path, "..", "../x", "/abs", "C:\\win")
    assert target.is_relative_to(tmp_path.resolve())


def test_safe_join_rejects_root(tmp_path: Path) -> None:
    with pytest.raises(M2OError):
        safe_join(tmp_path)


def _resolve_to(monkeypatch: pytest.MonkeyPatch, address: str) -> None:
    def fake(host: str, port: Any, *args: Any, **kwargs: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x.org/a", "data:text/html,hi", "javascript:alert(1)"])
def test_blocks_schemes(url: str) -> None:
    with pytest.raises(M2OError):
        check_public_url(url)


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "::ffff:127.0.0.1"]
)
def test_blocks_private_addresses(monkeypatch: pytest.MonkeyPatch, address: str) -> None:
    _resolve_to(monkeypatch, address)
    with pytest.raises(M2OError):
        check_public_url("https://looks-public.example/file")


def test_allows_public_address(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolve_to(monkeypatch, "93.184.216.34")
    check_public_url("https://example.org/video.mp4")
