from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from moodle2obsidian.config import LLMConfig
from moodle2obsidian.llm import LLMError
from moodle2obsidian.llm.anthropic_api import AnthropicBackend
from moodle2obsidian.llm.claude_code import ClaudeCodeBackend

SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}


def fake_run(stdout: dict[str, Any] | str, calls: list[dict[str, Any]]) -> Any:
    def run(command: list[str], **kwargs: Any) -> Any:
        calls.append({"command": command, **kwargs})
        body = stdout if isinstance(stdout, str) else json.dumps(stdout)
        return SimpleNamespace(returncode=0, stdout=body.encode(), stderr=b"")

    return run


def test_claude_code_is_sandboxed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    reply = {
        "is_error": False,
        "structured_output": {"summary": "ok"},
        "usage": {"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 7},
        "total_cost_usd": 0.01,
    }
    monkeypatch.setattr(subprocess, "run", fake_run(reply, calls))
    backend = ClaudeCodeBackend(LLMConfig(), executable="claude")
    result = backend.complete_json("SYSTEM", "<materials>secret lecture</materials>", SCHEMA)

    assert result.data == {"summary": "ok"}
    assert (result.input_tokens, result.output_tokens, result.cost_usd) == (15, 7, 0.01)
    command = calls[0]["command"]
    assert command[command.index("--tools") + 1] == ""  # no tools at all
    assert "--strict-mcp-config" in command and "--no-session-persistence" in command
    assert command[command.index("--setting-sources") + 1] == "project"
    assert not any("dangerously" in part for part in command)
    assert calls[0]["input"] == b"<materials>secret lecture</materials>"  # material via stdin, not argv
    assert "secret lecture" not in " ".join(command)
    assert calls[0]["cwd"]  # isolated temp working dir
    assert json.loads(command[command.index("--json-schema") + 1]) == SCHEMA


def test_claude_code_result_text_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    reply = {"is_error": False, "result": '{"summary": "from text"}', "usage": {}}
    monkeypatch.setattr(subprocess, "run", fake_run(reply, []))
    assert ClaudeCodeBackend(LLMConfig(), executable="claude").complete_json("s", "u", SCHEMA).data == {
        "summary": "from text"
    }


def test_claude_code_not_logged_in_is_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    reply = {"is_error": True, "result": "Not logged in · Please run /login"}
    monkeypatch.setattr(subprocess, "run", fake_run(reply, []))
    with pytest.raises(LLMError) as err:
        ClaudeCodeBackend(LLMConfig(), executable="claude").complete_json("s", "u", SCHEMA)
    assert err.value.fatal


def test_claude_code_garbage_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", fake_run("not json", []))
    with pytest.raises(LLMError) as err:
        ClaudeCodeBackend(LLMConfig(), executable="claude").complete_json("s", "u", SCHEMA)
    assert not err.value.fatal


def test_claude_code_missing_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(LLMError) as err:
        ClaudeCodeBackend(LLMConfig())
    assert err.value.fatal


class FakeMessages:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return self.response


def fake_client(stop_reason: str = "end_turn", text: str = '{"summary": "ok"}') -> tuple[Any, FakeMessages]:
    response = SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=100, cache_creation_input_tokens=0, cache_read_input_tokens=20, output_tokens=30
        ),
    )
    messages = FakeMessages(response)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def test_anthropic_request_shape() -> None:
    client, messages = fake_client()
    result = AnthropicBackend(LLMConfig(backend="anthropic"), client=client).complete_json("SYS", "USER", SCHEMA)
    assert result.data == {"summary": "ok"}
    assert (result.input_tokens, result.output_tokens) == (120, 30)
    sent = messages.kwargs
    assert sent["model"] == "claude-opus-5-5"
    assert sent["system"] == "SYS" and sent["messages"] == [{"role": "user", "content": "USER"}]
    assert sent["output_config"] == {"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}}
    assert sent["fallbacks"] == "default" and sent["betas"] == ["server-side-fallback-2026-07-01"]


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_anthropic_bad_stop_reasons(stop_reason: str) -> None:
    client, _ = fake_client(stop_reason=stop_reason)
    with pytest.raises(LLMError):
        AnthropicBackend(LLMConfig(backend="anthropic"), client=client).complete_json("s", "u", SCHEMA)
