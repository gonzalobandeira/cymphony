"""Tests for the Claude-specific runner implementation."""

from __future__ import annotations

import pytest

from cymphony.runners.claude import ClaudeAgentRunner, parse_claude_stream_event
from cymphony.models import AgentEventType, CodingAgentConfig


def _make_config() -> CodingAgentConfig:
    return CodingAgentConfig(
        command="claude",
        turn_timeout_ms=1000,
        read_timeout_ms=1000,
        stall_timeout_ms=1000,
        dangerously_skip_permissions=True,
        provider="claude",
    )


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------

class TestClaudeCommandBuilding:
    def test_initial_command(self) -> None:
        runner = ClaudeAgentRunner(_make_config())
        cmd = runner._build_command("do stuff", "/ws", None, "title")
        assert cmd[:6] == [
            "claude",
            "--output-format",
            "stream-json",
            "--verbose",
            "--print",
            "do stuff",
        ]
        assert "--resume" not in cmd
        assert "--dangerously-skip-permissions" in cmd

    def test_resume_command(self) -> None:
        runner = ClaudeAgentRunner(_make_config())
        cmd = runner._build_command("continue", "/ws", "sess-123", "title")
        assert "--resume" in cmd
        idx = cmd.index("--resume")
        assert cmd[idx + 1] == "sess-123"

    def test_no_skip_permissions(self) -> None:
        cfg = _make_config()
        cfg.dangerously_skip_permissions = False
        runner = ClaudeAgentRunner(cfg)
        cmd = runner._build_command("prompt", "/ws", None, "title")
        assert "--dangerously-skip-permissions" not in cmd

    def test_prompt_is_in_command(self) -> None:
        runner = ClaudeAgentRunner(_make_config())
        cmd = runner._build_command("fix the bug now", "/ws", None, "BAP-1")
        assert "fix the bug now" in cmd


# ---------------------------------------------------------------------------
# Environment handling
# ---------------------------------------------------------------------------

class TestClaudeEnvironment:
    def test_preserves_auth_vars(self, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("CLAUDE_API_KEY", "claude-api-key")

        env = ClaudeAgentRunner(_make_config())._build_env()

        assert env["ANTHROPIC_API_KEY"] == "sk-ant-test"
        assert env["CLAUDE_API_KEY"] == "claude-api-key"

    def test_strips_claudecode_sentinel(self, monkeypatch) -> None:
        monkeypatch.setenv("CLAUDECODE", "1")

        env = ClaudeAgentRunner(_make_config())._build_env()

        assert "CLAUDECODE" not in env

    def test_env_without_claudecode_is_fine(self, monkeypatch) -> None:
        monkeypatch.delenv("CLAUDECODE", raising=False)

        env = ClaudeAgentRunner(_make_config())._build_env()

        assert "CLAUDECODE" not in env


# ---------------------------------------------------------------------------
# Stream-json parsing
# ---------------------------------------------------------------------------

class TestClaudeStreamParsing:
    def test_parse_init_event(self) -> None:
        event, sid, ok, err = parse_claude_stream_event(
            '{"type": "system", "subtype": "init", "session_id": "abc"}',
            None, "iss", "ID-1", 1,
        )
        assert event is not None
        assert event.event == AgentEventType.SESSION_STARTED
        assert sid == "abc"
        assert ok is None

    def test_parse_result_success(self) -> None:
        event, sid, ok, err = parse_claude_stream_event(
            '{"type": "result", "subtype": "success", "usage": {"input_tokens": 10, "output_tokens": 5}}',
            "s1", "iss", "ID-1", 1,
        )
        assert event is not None
        assert event.event == AgentEventType.TURN_COMPLETED
        assert ok is True
        assert event.usage == {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 0,
        }

    def test_parse_result_error(self) -> None:
        event, sid, ok, err = parse_claude_stream_event(
            '{"type": "result", "subtype": "error_max_turns"}',
            "s1", "iss", "ID-1", 1,
        )
        assert event is not None
        assert event.event == AgentEventType.TURN_FAILED
        assert ok is None
        assert err is not None

    def test_parse_input_required(self) -> None:
        event, sid, ok, err = parse_claude_stream_event(
            '{"type": "result", "subtype": "input_required"}',
            "s1", "iss", "ID-1", 1,
        )
        assert event is not None
        assert event.event == AgentEventType.TURN_INPUT_REQUIRED

    def test_parse_user_input_variant(self) -> None:
        event, sid, ok, err = parse_claude_stream_event(
            '{"type": "result", "subtype": "user_input_needed"}',
            "s1", "iss", "ID-1", 1,
        )
        assert event is not None
        assert event.event == AgentEventType.TURN_INPUT_REQUIRED

    def test_parse_malformed(self) -> None:
        event, sid, ok, err = parse_claude_stream_event(
            "not json at all",
            "s1", "iss", "ID-1", 1,
        )
        assert event is not None
        assert event.event == AgentEventType.MALFORMED

    def test_parse_assistant_message(self) -> None:
        msg = '{"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}}'
        event, sid, ok, err = parse_claude_stream_event(msg, "s1", "iss", "ID-1", 1)
        assert event is not None
        assert event.event == AgentEventType.NOTIFICATION
        assert "hello" in (event.message or "")

    def test_parse_assistant_tool_use(self) -> None:
        msg = '{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "bash"}]}}'
        event, sid, ok, err = parse_claude_stream_event(msg, "s1", "iss", "ID-1", 1)
        assert event is not None
        assert event.event == AgentEventType.NOTIFICATION
        assert "[tool: bash]" in (event.message or "")

    def test_parse_unknown_type(self) -> None:
        event, sid, ok, err = parse_claude_stream_event(
            '{"type": "something_else"}',
            "s1", "iss", "ID-1", 1,
        )
        assert event is not None
        assert event.event == AgentEventType.OTHER_MESSAGE

    def test_parse_preserves_session_id(self) -> None:
        event, sid, ok, err = parse_claude_stream_event(
            '{"type": "result", "subtype": "success"}',
            "existing-session", "iss", "ID-1", 1,
        )
        assert event is not None
        assert event.session_id == "existing-session"
        assert sid is None  # no new session_id from result events

    def test_parse_success_without_usage(self) -> None:
        event, sid, ok, err = parse_claude_stream_event(
            '{"type": "result", "subtype": "success"}',
            "s1", "iss", "ID-1", 1,
        )
        assert event is not None
        assert event.event == AgentEventType.TURN_COMPLETED
        assert event.usage is None

    def test_parse_event_delegates_correctly(self) -> None:
        """ClaudeAgentRunner._parse_event delegates to parse_claude_stream_event."""
        runner = ClaudeAgentRunner(_make_config())
        event, sid, ok, err = runner._parse_event(
            '{"type": "system", "subtype": "init", "session_id": "s1"}',
            None, "iss-1", "ID-1", 42,
        )
        assert event is not None
        assert event.event == AgentEventType.SESSION_STARTED
        assert sid == "s1"
