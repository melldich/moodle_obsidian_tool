from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from moodle2obsidian.log import RedactingFilter, redact, register_secret, setup_logging


@pytest.fixture
def capture() -> Iterator[tuple[logging.Logger, io.StringIO]]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("test.redaction")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    yield logger, stream
    logger.handlers = []


@pytest.mark.parametrize(
    ("message", "secret"),
    [
        (
            "GET https://m.test/webservice/pluginfile.php/1/a.pdf?token=abcdef123456&forcedownload=1",
            "abcdef123456",
        ),
        (
            "body wsfunction=x&wstoken=0123456789abcdef0123456789abcdef",  # gitleaks:allow
            "0123456789abcdef0123456789abcdef",
        ),
        (
            "redirect to moodlemobile://token=ZmFrZTo6OmZha2V0b2tlbg==",  # gitleaks:allow
            "ZmFrZTo6OmZha2V0b2tlbg==",
        ),
        ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
        ("Cookie: MoodleSession=s3cr3tsession; other=1", "s3cr3tsession"),
        ("{'password': 'hunter2hunter2', 'user': 'x'}", "hunter2hunter2"),
        ('{"token": "jsontoken123456"}', "jsontoken123456"),
        ("key sk-ant-api03-AAAAAAAAAAAAAAAAAAAA used", "sk-ant-api03-AAAAAAAAAAAAAAAAAAAA"),
        ("groq gsk_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123", "gsk_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"),
    ],
)
def test_redacts_messages(capture: tuple[logging.Logger, io.StringIO], message: str, secret: str) -> None:
    logger, stream = capture
    logger.info(message)
    assert secret not in stream.getvalue()
    assert "***" in stream.getvalue()


def test_redacts_args(capture: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = capture
    logger.info("fetching %s", "https://m.test/file.php?token=argsecret999")
    assert "argsecret999" not in stream.getvalue()


def test_redacts_httpx_exception_traceback(capture: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = capture
    request = httpx.Request("GET", "https://m.test/webservice/pluginfile.php/1/f.pdf?token=TRACEBACKsecret42")
    try:
        httpx.Response(404, request=request).raise_for_status()
    except httpx.HTTPStatusError:
        logger.exception("download failed")
    out = stream.getvalue()
    assert "HTTPStatusError" in out
    assert "TRACEBACKsecret42" not in out


def test_registered_secret_is_redacted_anywhere() -> None:
    register_secret("zzRegisteredValue42")
    assert redact("prefix zzRegisteredValue42 suffix") == "prefix *** suffix"


def test_short_values_are_not_registered() -> None:
    register_secret("abc")
    assert redact("abc") == "abc"


def test_setup_logging_file_is_redacted(tmp_path: Path) -> None:
    setup_logging(tmp_path, verbose=False)
    try:
        logging.getLogger("moodle2obsidian.test").warning("wstoken=%s", "fileSecretValue77")
        for handler in logging.getLogger().handlers:
            handler.flush()
        content = (tmp_path / "m2o.log").read_text(encoding="utf-8")
        assert "wstoken=***" in content
        assert "fileSecretValue77" not in content
    finally:
        for handler in list(logging.getLogger().handlers):
            logging.getLogger().removeHandler(handler)
            handler.close()
