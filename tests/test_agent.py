from __future__ import annotations

import pytest

from cymphony.agent import (
    BaseAgentRunner,
    ClaudeAgentRunner,
    CodexAgentRunner,
    create_runner,
    _parse_claude_stream_event,
)
from cymphony.models import AgentError, AgentEventType, CodingAgentConfig


def _make_config(provider: str = "claude", command: str = "claude") -> CodingAgentConfig:
    return CodingAgentConfig(
        command=command,
        turn_timeout_ms=1000,
        read_timeout_ms=1000,
        stall_timeout_ms=1000,
        dangerously_skip_permissions=True,
        provider=provider,
    )


# ---------------------------------------------------------------------------
# BaseAgentRunner is abstract
# ---------------------------------------------------------------------------

def test_base_runner_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        BaseAgentRunner(_make_config())  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# ClaudeAgentRunner
# ---------------------------------------------------------------------------

def test_claude_runner_build_command_initial() -> None:
    runner = ClaudeAgentRunner(_make_config())
    cmd = runner._build_command("do stuff", "/ws", None, "title")
    assert cmd[:6] == ["claude", "--output-format", "stream-json", "--verbose", "--print", "do stuff"]
    assert "--resume" not in cmd
    assert "--dangerously-skip-permissions" in cmd


def test_claude_runner_build_command_resume() -> None:
    runner = ClaudeAgentRunner(_make_config())
    cmd = runner._build_command("continue", "/ws", "sess-123", "title")
    assert "--resume" in cmd
    idx = cmd.index("--resume")
    assert cmd[idx + 1] == "sess-123"


def test_claude_runner_build_command_no_skip_permissions() -> None:
    cfg = _make_config()
    cfg.dangerously_skip_permissions = False
    runner = ClaudeAgentRunner(cfg)
    cmd = runner._build_command("prompt", "/ws", None, "title")
    assert "--dangerously-skip-permissions" not in cmd


def test_claude_runner_env_preserves_auth_vars(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("CLAUDE_API_KEY", "claude-api-key")

    env = ClaudeAgentRunner(_make_config())._build_env()

    assert env["ANTHROPIC_API_KEY"] == "sk-ant-test"
    assert env["CLAUDE_API_KEY"] == "claude-api-key"


def test_claude_runner_env_strips_claudecode_sentinel(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")

    env = ClaudeAgentRunner(_make_config())._build_env()

    assert "CLAUDECODE" not in env


def test_claude_parse_event_delegates_correctly() -> None:
    runner = ClaudeAgentRunner(_make_config())
    event, sid, ok, err = runner._parse_event(
        '{"type": "system", "subtype": "init", "session_id": "s1"}',
        None, "iss-1", "ID-1", 42,
    )
    assert event is not None
    assert event.event == AgentEventType.SESSION_STARTED
    assert sid == "s1"


# ---------------------------------------------------------------------------
# CodexAgentRunner
# ---------------------------------------------------------------------------

def test_codex_runner_build_command() -> None:
    runner = CodexAgentRunner(_make_config(provider="codex", command="codex"))
    cmd = runner._build_command("do stuff", "/ws", None, "title")
    assert cmd[0] == "codex"
    assert "--quiet" in cmd
    assert "--full-auto" in cmd


def test_codex_runner_build_command_no_full_auto() -> None:
    cfg = _make_config(provider="codex", command="codex")
    cfg.dangerously_skip_permissions = False
    runner = CodexAgentRunner(cfg)
    cmd = runner._build_command("prompt", "/ws", None, "title")
    assert "--full-auto" not in cmd


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def test_create_runner_claude() -> None:
    runner = create_runner(_make_config(provider="claude"))
    assert isinstance(runner, ClaudeAgentRunner)


def test_create_runner_codex() -> None:
    runner = create_runner(_make_config(provider="codex"))
    assert isinstance(runner, CodexAgentRunner)


def test_create_runner_unknown_raises() -> None:
    with pytest.raises(AgentError, match="Unknown agent provider"):
        create_runner(_make_config(provider="gemini"))


# ---------------------------------------------------------------------------
# Claude stream-json parser
# ---------------------------------------------------------------------------

def test_parse_init_event() -> None:
    event, sid, ok, err = _parse_claude_stream_event(
        '{"type": "system", "subtype": "init", "session_id": "abc"}',
        None, "iss", "ID-1", 1,
    )
    assert event is not None
    assert event.event == AgentEventType.SESSION_STARTED
    assert sid == "abc"
    assert ok is None


def test_parse_result_success() -> None:
    event, sid, ok, err = _parse_claude_stream_event(
        '{"type": "result", "subtype": "success", "usage": {"input_tokens": 10, "output_tokens": 5}}',
        "s1", "iss", "ID-1", 1,
    )
    assert event is not None
    assert event.event == AgentEventType.TURN_COMPLETED
    assert ok is True
    assert event.usage == {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0}


def test_parse_result_error() -> None:
    event, sid, ok, err = _parse_claude_stream_event(
        '{"type": "result", "subtype": "error_max_turns"}',
        "s1", "iss", "ID-1", 1,
    )
    assert event is not None
    assert event.event == AgentEventType.TURN_FAILED
    assert ok is None
    assert err is not None


def test_parse_input_required() -> None:
    event, sid, ok, err = _parse_claude_stream_event(
        '{"type": "result", "subtype": "input_required"}',
        "s1", "iss", "ID-1", 1,
    )
    assert event is not None
    assert event.event == AgentEventType.TURN_INPUT_REQUIRED


def test_parse_malformed() -> None:
    event, sid, ok, err = _parse_claude_stream_event(
        "not json at all",
        "s1", "iss", "ID-1", 1,
    )
    assert event is not None
    assert event.event == AgentEventType.MALFORMED


def test_parse_assistant_message() -> None:
    msg = '{"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}}'
    event, sid, ok, err = _parse_claude_stream_event(msg, "s1", "iss", "ID-1", 1)
    assert event is not None
    assert event.event == AgentEventType.NOTIFICATION
    assert "hello" in (event.message or "")


def test_parse_unknown_type() -> None:
    event, sid, ok, err = _parse_claude_stream_event(
        '{"type": "something_else"}',
        "s1", "iss", "ID-1", 1,
    )
    assert event is not None
    assert event.event == AgentEventType.OTHER_MESSAGE
