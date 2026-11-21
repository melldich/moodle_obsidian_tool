"""Logging with secret redaction (messages, args, tracebacks) and file rotation."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.logging import RichHandler

from .fsutil import chmod_private, secure_mkdir

_KEYS = (
    r"(?:ws)?token|privatetoken|password|passwd|passport|secret|api[_-]?key"
    r"|access_token|refresh_token|sesskey|MoodleSession\w*"
)
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # key=value in URLs, form bodies, CLI args
    (re.compile(rf"(?i)\b({_KEYS})=([^&\s\"'<>]+)"), r"\1=***"),
    # "key": "value" in JSON / python reprs
    (re.compile(rf"(?i)([\"']({_KEYS})[\"']\s*:\s*)[\"'][^\"']*[\"']"), r'\1"***"'),
    # HTTP headers
    (
        re.compile(r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie|x-api-key)(\s*[:=]\s*)[^\r\n]+"),
        r"\1\2***",
    ),
    (re.compile(r"(?i)\bbearer\s+[\w.~+/-]+=*"), "Bearer ***"),
    # well-known API key formats (Anthropic, OpenAI, Groq, GitHub)
    (re.compile(r"\b(sk-ant-[\w-]{8,}|sk-[\w-]{20,}|gsk_\w{20,}|gh[pousr]_\w{20,})"), "***"),
]
_registered: set[str] = set()


def register_secret(value: str | None) -> None:
    """Redact this exact value everywhere it appears in logs from now on."""
    if value and len(value) >= 6:
        _registered.add(value)


def redact(text: str) -> str:
    for secret in sorted(_registered, key=len, reverse=True):
        text = text.replace(secret, "***")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def setup_logging(log_dir: Path | None, verbose: bool = False) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(logging.DEBUG)

    console = RichHandler(level=logging.DEBUG if verbose else logging.INFO, show_path=False, markup=False)
    handlers: list[logging.Handler] = [console]
    if log_dir is not None:
        secure_mkdir(log_dir)
        log_file = log_dir / "m2o.log"
        file_handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        file_handler.setLevel(logging.DEBUG)
        chmod_private(log_file, 0o600)
        handlers.append(file_handler)
    for handler in handlers:
        handler.addFilter(RedactingFilter())
        root.addHandler(handler)
    # httpx logs full request URLs at INFO; keep it quiet (redaction still applies if raised).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
