"""Anthropic API backend (pay-per-token). Structured JSON output; server-side fallback on model refusals."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from ..auth.store import get_secret
from . import LLMError, LLMResult

if TYPE_CHECKING:
    from ..config import LLMConfig

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicBackend:
    name = "anthropic"

    def __init__(self, cfg: LLMConfig, client: Any | None = None) -> None:
        self.cfg = cfg
        if client is None:
            try:
                import anthropic
            except ImportError:
                raise LLMError(
                    "Не установлен пакет anthropic: `uv sync --extra anthropic`.",
                    "The anthropic package is not installed: `uv sync --extra anthropic`.",
                    fatal=True,
                ) from None
            key = get_secret("anthropic_api_key")  # keyring / .env; otherwise the SDK's own credential lookup
            client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        self.client = client

    def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> LLMResult:
        try:
            response = self.client.beta.messages.create(
                model=self.cfg.model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": self.cfg.effort, "format": {"type": "json_schema", "schema": schema}},
                betas=[FALLBACK_BETA],
                fallbacks="default",  # a policy decline is retried on a fallback model inside the same call
            )
        except Exception as exc:  # mapped to retryable / fatal below; the SDK already retried 429/5xx
            raise _map_error(exc) from None
        if response.stop_reason == "refusal":
            raise LLMError("модель отказалась обрабатывать эту тему", "the model declined this topic")
        if response.stop_reason == "max_tokens":
            raise LLMError("ответ модели не поместился в лимит", "the model response hit max_tokens")
        text = next((block.text for block in response.content if block.type == "text"), "")
        try:
            data = json.loads(text)
        except ValueError:
            raise LLMError("ответ модели не JSON", "the model response is not JSON") from None
        usage = response.usage
        tokens_in = sum(
            int(getattr(usage, key, 0) or 0)
            for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )
        return LLMResult(data, tokens_in, int(usage.output_tokens or 0))


def _map_error(exc: Exception) -> LLMError:
    try:
        import anthropic
    except ImportError:  # only when a fake client is injected in tests
        return LLMError(f"ошибка API: {type(exc).__name__}", f"API error: {type(exc).__name__}")
    name = type(exc).__name__
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return LLMError(
            "Anthropic API: неверный ключ или нет доступа (m2o login не нужен — задайте ключ ANTHROPIC_API_KEY)",
            "Anthropic API: invalid key or no access (set ANTHROPIC_API_KEY)",
            fatal=True,
        )
    if isinstance(exc, anthropic.RateLimitError):
        return LLMError(
            "Anthropic API: лимит запросов, продолжим в следующий раз", "Anthropic API: rate limited", fatal=True
        )
    if isinstance(exc, anthropic.APIStatusError):
        return LLMError(f"Anthropic API: HTTP {exc.status_code}", f"Anthropic API: HTTP {exc.status_code}")
    return LLMError(f"Anthropic API: {name}", f"Anthropic API: {name}")
