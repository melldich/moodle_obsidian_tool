"""LLM backends. Course materials are untrusted: callers wrap them as data, backends run without any tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from ..errors import M2OError

if TYPE_CHECKING:
    from ..config import LLMConfig


@dataclass
class LLMResult:
    data: Any
    input_tokens: int
    output_tokens: int
    cost_usd: float | None = None


class LLMError(M2OError):
    """fatal=True stops the whole run (not logged in, no CLI, rate limit); otherwise only this topic fails."""

    def __init__(self, ru: str, en: str, *, fatal: bool = False) -> None:
        super().__init__(ru, en)
        self.fatal = fatal


class Backend(Protocol):
    name: str

    def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> LLMResult: ...


def make_backend(cfg: LLMConfig) -> Backend:
    if cfg.backend == "anthropic":
        from .anthropic_api import AnthropicBackend

        return AnthropicBackend(cfg)
    from .claude_code import ClaudeCodeBackend

    return ClaudeCodeBackend(cfg)
