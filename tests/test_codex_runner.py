"""Tests for the Codex-specific runner implementation."""

from __future__ import annotations

from cymphony.runners.codex import CodexAgentRunner, parse_codex_stream_event
from cymphony.models import AgentEventType, CodingAgentConfig


def _make_config() -> CodingAgentConfig:
    return CodingAgentConfig(
        command="codex",
        turn_timeout_ms=1000,
        read_timeout_ms=1000,
        stall_timeout_ms=1000,
        dangerously_skip_permissions=True,
        provider="codex",
    )


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------

class TestCodexCommandBuilding:
    def test_initial_command_structure(self) -> None:
        runner = CodexAgentRunner(_make_config())
        cmd = runner._build_command("do stuff", "/ws", None, "title")
        assert cmd[0] == "codex"
        assert "--approval-mode" in cmd
        assert "full-auto" in cmd
        assert "--quiet" in cmd
        assert "do stuff" in cmd
        assert "--resume" not in cmd

    def test_resume_command(self) -> None:
        runner = CodexAgentRunner(_make_config())
        cmd = runner._build_command("fix the bug", "/tmp/ws", "sess-123", "BAP-1")
        assert "--resume" in cmd
        idx = cmd.index("--resume")
        assert cmd[idx + 1] == "sess-123"

    def test_omits_resume_for_fresh_session(self) -> None:
        runner = CodexAgentRunner(_make_config())
        cmd = runner._build_command("fix the bug", "/tmp/ws", None, "BAP-1")
        assert "--resume" not in cmd

    def test_no_approval_mode_when_permissions_not_skipped(self) -> None:
        cfg = _make_config()
        cfg.dangerously_skip_permissions = False
        runner = CodexAgentRunner(cfg)
        cmd = runner._build_command("prompt", "/ws", None, "title")
        assert "--approval-mode" not in cmd
        assert "full-auto" not in cmd

    def test_prompt_is_in_command(self) -> None:
        runner = CodexAgentRunner(_make_config())
        cmd = runner._build_command("fix the bug now", "/ws", None, "BAP-1")
        assert "fix the bug now" in cmd


# ---------------------------------------------------------------------------
# Environment handling
# ---------------------------------------------------------------------------

class TestCodexEnvironment:
    def test_preserves_openai_api_key(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
        runner = CodexAgentRunner(_make_config())
        env = runner._build_env()
        assert env["OPENAI_API_KEY"] == "sk-test-key"

    def test_does_not_strip_claudecode_sentinel(self, monkeypatch) -> None:
        monkeypatch.setenv("CLAUDECODE", "1")
        runner = CodexAgentRunner(_make_config())
        env = runner._build_env()
        assert env.get("CLAUDECODE") == "1"


# ---------------------------------------------------------------------------
# Codex event parsing
# ---------------------------------------------------------------------------

class TestCodexStreamParsing:
    def test_parse_session_created(self) -> None:
        event, sid, ok, err = parse_codex_stream_event(
            '{"type": "session.created", "session": {"id": "codex-s1"}}',
            None, "iss", "ID-1", 1,
        )
        assert event is not None
        assert event.event == AgentEventType.SESSION_STARTED
        assert sid == "codex-s1"
        assert ok is None

    def test_parse_session_created_fallback_session_id(self) -> None:
        event, sid, ok, err = parse_codex_stream_event(
            '{"type": "session.created", "session_id": "fallback-id"}',
            None, "iss", "ID-1", 1,
        )
        assert sid == "fallback-id"

    def test_parse_session_created_uses_current_if_no_id(self) -> None:
        event, sid, ok, err = parse_codex_stream_event(
            '{"type": "session.created"}',
            "existing-session", "iss", "ID-1", 1,
        )
        assert sid == "existing-session"

    def test_parse_assistant_message_string_content(self) -> None:
        msg = '{"type": "message", "role": "assistant", "content": "hello world"}'
        event, sid, ok, err = parse_codex_stream_event(msg, "s1", "iss", "ID-1", 1)
        assert event is not None
        assert event.event == AgentEventType.NOTIFICATION
        assert "hello world" in (event.message or "")

    def test_parse_assistant_message_list_content(self) -> None:
        msg = '{"type": "message", "role": "assistant", "content": [{"type": "text", "text": "thinking..."}]}'
        event, sid, ok, err = parse_codex_stream_event(msg, "s1", "iss", "ID-1", 1)
        assert event is not None
        assert event.event == AgentEventType.NOTIFICATION
        assert "thinking" in (event.message or "")

    def test_parse_system_message_ignored(self) -> None:
        msg = '{"type": "message", "role": "system", "content": "system init"}'
        event, sid, ok, err = parse_codex_stream_event(msg, "s1", "iss", "ID-1", 1)
        assert event is None

    def test_parse_function_call(self) -> None:
        msg = '{"type": "function_call", "name": "bash"}'
        event, sid, ok, err = parse_codex_stream_event(msg, "s1", "iss", "ID-1", 1)
        assert event is not None
        assert event.event == AgentEventType.NOTIFICATION
        assert "[tool: bash]" in (event.message or "")

    def test_parse_completed(self) -> None:
        msg = '{"type": "completed", "usage": {"input_tokens": 100, "output_tokens": 50}}'
        event, sid, ok, err = parse_codex_stream_event(msg, "s1", "iss", "ID-1", 1)
        assert event is not None
        assert event.event == AgentEventType.TURN_COMPLETED
        assert ok is True
        assert event.usage == {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 0,
        }

    def test_parse_success_type(self) -> None:
        msg = '{"type": "success"}'
        event, sid, ok, err = parse_codex_stream_event(msg, "s1", "iss", "ID-1", 1)
        assert event is not None
        assert event.event == AgentEventType.TURN_COMPLETED
        assert ok is True

    def test_parse_completed_without_usage(self) -> None:
        msg = '{"type": "completed"}'
        event, sid, ok, err = parse_codex_stream_event(msg, "s1", "iss", "ID-1", 1)
        assert event is not None
        assert event.event == AgentEventType.TURN_COMPLETED
        assert event.usage is None

    def test_parse_error(self) -> None:
        msg = '{"type": "error", "message": "rate limit exceeded"}'
        event, sid, ok, err = parse_codex_stream_event(msg, "s1", "iss", "ID-1", 1)
        assert event is not None
        assert event.event == AgentEventType.TURN_FAILED
        assert err is not None
        assert "rate limit" in err

    def test_parse_error_with_error_field(self) -> None:
        msg = '{"type": "error", "error": "something broke"}'
        event, sid, ok, err = parse_codex_stream_event(msg, "s1", "iss", "ID-1", 1)
        assert event is not None
        assert event.event == AgentEventType.TURN_FAILED
        assert "something broke" in (err or "")

    def test_parse_error_no_message(self) -> None:
        msg = '{"type": "error"}'
        event, sid, ok, err = parse_codex_stream_event(msg, "s1", "iss", "ID-1", 1)
        assert event is not None
        assert event.event == AgentEventType.TURN_FAILED
        assert err == "codex error"

    def test_parse_input_required(self) -> None:
        msg = '{"type": "input_required"}'
        event, sid, ok, err = parse_codex_stream_event(msg, "s1", "iss", "ID-1", 1)
        assert event is not None
        assert event.event == AgentEventType.TURN_INPUT_REQUIRED

    def test_parse_malformed_json(self) -> None:
        event, sid, ok, err = parse_codex_stream_event(
            "not json at all",
            "s1", "iss", "ID-1", 1,
        )
        assert event is not None
        assert event.event == AgentEventType.MALFORMED

    def test_parse_unknown_type(self) -> None:
        event, sid, ok, err = parse_codex_stream_event(
            '{"type": "something_else", "data": 42}',
            "s1", "iss", "ID-1", 1,
        )
        assert event is not None
        assert event.event == AgentEventType.OTHER_MESSAGE

    def test_parse_preserves_session_id(self) -> None:
        event, sid, ok, err = parse_codex_stream_event(
            '{"type": "completed"}',
            "existing-session", "iss", "ID-1", 1,
        )
        assert event is not None
        assert event.session_id == "existing-session"
        assert sid is None  # no new session_id from completed events

    def test_parse_event_delegates_correctly(self) -> None:
        """CodexAgentRunner._parse_event delegates to parse_codex_stream_event."""
        runner = CodexAgentRunner(_make_config())
        event, sid, ok, err = runner._parse_event(
            '{"type": "session.created", "session": {"id": "s1"}}',
            None, "iss-1", "ID-1", 42,
        )
        assert event is not None
        assert event.event == AgentEventType.SESSION_STARTED
        assert sid == "s1"

    def test_parse_message_content_truncation(self) -> None:
        long_content = "x" * 500
        msg = f'{{"type": "message", "role": "assistant", "content": "{long_content}"}}'
        event, sid, ok, err = parse_codex_stream_event(msg, "s1", "iss", "ID-1", 1)
        assert event is not None
        assert len(event.message or "") <= 300

    def test_parse_function_call_unknown_name(self) -> None:
        msg = '{"type": "function_call"}'
        event, sid, ok, err = parse_codex_stream_event(msg, "s1", "iss", "ID-1", 1)
        assert event is not None
        assert "[tool: ?]" in (event.message or "")


# ---------------------------------------------------------------------------
# Re-export verification
# ---------------------------------------------------------------------------

def test_codex_parser_reexported_from_runners() -> None:
    from cymphony.runners import parse_codex_stream_event as from_runners
    from cymphony.runners.codex import parse_codex_stream_event as direct

    assert from_runners is direct


def test_codex_parser_reexported_from_agent() -> None:
    from cymphony.agent import parse_codex_stream_event as from_agent
    from cymphony.runners.codex import parse_codex_stream_event as direct

    assert from_agent is direct
