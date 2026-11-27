"""Claude Code backend (`claude -p`): uses the user's Claude subscription instead of API credits.

Hardened for untrusted input: no tools at all (no shell, file or web access), no MCP servers, no user
settings/hooks/plugins, an empty temporary working directory, no saved session, never
`--dangerously-skip-permissions`. The prompt goes through stdin (Windows command lines max out at ~32k chars).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from typing import TYPE_CHECKING, Any

from ..log import redact
from . import LLMError, LLMResult

if TYPE_CHECKING:
    from ..config import LLMConfig


class ClaudeCodeBackend:
    name = "claude_code"

    def __init__(self, cfg: LLMConfig, executable: str | None = None) -> None:
        self.cfg = cfg
        exe = executable or shutil.which("claude")
        if not exe:
            raise LLMError(
                "Не найден `claude` (Claude Code CLI). Установите его или задайте llm.backend: anthropic.",
                "`claude` (Claude Code CLI) not found. Install it or set llm.backend: anthropic.",
                fatal=True,
            )
        self.exe = exe

    def command(self, system: str, schema: dict[str, Any]) -> list[str]:
        return [
            self.exe,
            "-p",
            "--output-format",
            "json",
            "--tools",
            "",  # no built-in tools: nothing can run commands, touch files or go online
            "--strict-mcp-config",  # no MCP servers either
            "--setting-sources",
            "project",  # no user settings, hooks or plugins; the project dir is an empty temp dir
            "--no-session-persistence",
            "--permission-prompts",
            "none",
            "--model",
            self.cfg.model,
            "--system-prompt",
            system,
            "--json-schema",
            json.dumps(schema),
        ]

    def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> LLMResult:
        with tempfile.TemporaryDirectory(prefix="m2o-llm-") as workdir:
            try:
                proc = subprocess.run(  # noqa: S603 — fixed argv, no shell; the material goes via stdin
                    self.command(system, schema),
                    input=user.encode("utf-8"),
                    capture_output=True,
                    cwd=workdir,
                    timeout=self.cfg.timeout_sec,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise LLMError(
                    f"claude не ответил за {self.cfg.timeout_sec:.0f} с",
                    f"claude timed out after {self.cfg.timeout_sec:.0f}s",
                ) from None
        try:
            out = json.loads(proc.stdout.decode("utf-8", "replace"))
        except ValueError:
            tail = redact(proc.stderr.decode("utf-8", "replace").strip()[-300:])
            raise LLMError(
                f"claude вернул не JSON (код {proc.returncode}): {tail}",
                f"claude returned non-JSON (exit {proc.returncode}): {tail}",
            ) from None
        if not isinstance(out, dict):
            raise LLMError("claude вернул неожиданный ответ", "claude returned an unexpected response")
        if out.get("is_error"):
            message = redact(str(out.get("result") or out.get("terminal_reason") or "error"))[:300]
            fatal = any(word in message.lower() for word in ("login", "auth", "credit", "usage limit", "rate limit"))
            hint = " Выполните `claude` и `/login`." if "login" in message.lower() else ""
            raise LLMError(f"Claude Code: {message}.{hint}", f"Claude Code: {message}", fatal=fatal)
        data = out.get("structured_output")
        if data is None:
            try:
                data = json.loads(str(out.get("result") or ""))
            except ValueError:
                raise LLMError("ответ claude не по схеме", "claude response does not match the schema") from None
        usage = out.get("usage") or {}
        tokens_in = sum(
            int(usage.get(key) or 0)
            for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )
        cost = out.get("total_cost_usd")
        return LLMResult(data, tokens_in, int(usage.get("output_tokens") or 0), float(cost) if cost else None)
