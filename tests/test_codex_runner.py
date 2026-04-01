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
    )


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------

class TestCodexCommandBuilding:
    def test_initial_command_structure(self) -> None:
        runner = CodexAgentRunner(_make_config())
        cmd = runner._build_command("do stuff", "/ws", None, "title")
        assert cmd[0] == "codex"
        assert cmd[1] == "exec"
        assert "--json" in cmd
        assert "do stuff" in cmd
        assert "resume" not in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd

    def test_resume_command(self) -> None:
        runner = CodexAgentRunner(_make_config())
        cmd = runner._build_command("fix the bug", "/tmp/ws", "sess-123", "BAP-1")
        assert cmd[:4] == ["codex", "exec", "resume", "sess-123"]
        assert "--json" in cmd

    def test_omits_resume_for_fresh_session(self) -> None:
        runner = CodexAgentRunner(_make_config())
        cmd = runner._build_command("fix the bug", "/tmp/ws", None, "BAP-1")
        assert "resume" not in cmd

    def test_no_approval_mode_when_permissions_not_skipped(self) -> None:
        cfg = _make_config()
        cfg.dangerously_skip_permissions = False
        runner = CodexAgentRunner(cfg)
        cmd = runner._build_command("prompt", "/ws", None, "title")
        assert "--dangerously-bypass-approvals-and-sandbox" not in cmd

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
    def test_parse_thread_started(self) -> None:
        event, sid, ok, err = parse_codex_stream_event(
            '{"type": "thread.started", "thread_id": "codex-s1"}',
            None, "iss", "ID-1", 1,
        )
        assert event is not None
        assert event.event == AgentEventType.SESSION_STARTED
        assert sid == "codex-s1"
        assert ok is None

    def test_parse_thread_started_uses_current_if_no_id(self) -> None:
        event, sid, ok, err = parse_codex_stream_event(
            '{"type": "thread.started"}',
            "existing-session", "iss", "ID-1", 1,
        )
        assert sid == "existing-session"

    def test_parse_agent_message_item(self) -> None:
        msg = '{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"hello world"}}'
        event, sid, ok, err = parse_codex_stream_event(msg, "s1", "iss", "ID-1", 1)
        assert event is not None
        assert event.event == AgentEventType.NOTIFICATION
        assert "hello world" in (event.message or "")

    def test_parse_command_execution_item(self) -> None:
        msg = (
            '{"type":"item.completed","item":{"id":"item_0","type":"command_execution",'
            '"command":"pytest","status":"completed","exit_code":0}}'
        )
        event, sid, ok, err = parse_codex_stream_event(msg, "s1", "iss", "ID-1", 1)
        assert event is not None
        assert event.event == AgentEventType.NOTIFICATION
        assert "pytest" in (event.message or "")
        assert "exit=0" in (event.message or "")

    def test_parse_function_call_item(self) -> None:
        msg = (
            '{"type":"item.completed","item":{"id":"item_2","type":"function_call",'
            '"name":"TodoWrite","arguments":"{\\"todos\\": []}","call_id":"call_1"}}'
        )
        event, sid, ok, err = parse_codex_stream_event(msg, "s1", "iss", "ID-1", 1)
        assert event is not None
        assert event.event == AgentEventType.NOTIFICATION
        assert "[tool: TodoWrite]" in (event.message or "")

    def test_parse_item_completed_ignores_unknown_item_types(self) -> None:
        msg = '{"type":"item.completed","item":{"id":"item_9","type":"reasoning"}}'
        event, sid, ok, err = parse_codex_stream_event(msg, "s1", "iss", "ID-1", 1)
        assert event is None

    def test_parse_turn_completed(self) -> None:
        msg = '{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":50,"cached_input_tokens":7}}'
        event, sid, ok, err = parse_codex_stream_event(msg, "s1", "iss", "ID-1", 1)
        assert event is not None
        assert event.event == AgentEventType.TURN_COMPLETED
        assert ok is True
        assert event.usage == {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 7,
        }

    def test_parse_turn_completed_without_usage(self) -> None:
        msg = '{"type":"turn.completed"}'
        event, sid, ok, err = parse_codex_stream_event(msg, "s1", "iss", "ID-1", 1)
        assert event is not None
        assert event.event == AgentEventType.TURN_COMPLETED
        assert ok is True
        assert event.usage is None

    def test_parse_turn_failed(self) -> None:
        msg = '{"type":"turn.failed","detail":"rate limit exceeded"}'
        event, sid, ok, err = parse_codex_stream_event(msg, "s1", "iss", "ID-1", 1)
        assert event is not None
        assert event.event == AgentEventType.TURN_FAILED
        assert err is not None
        assert "rate limit" in err

    def test_parse_error_with_error_field(self) -> None:
        msg = '{"type":"error","error":"something broke"}'
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
            '{"type":"turn.completed"}',
            "existing-session", "iss", "ID-1", 1,
        )
        assert event is not None
        assert event.session_id == "existing-session"
        assert sid is None

    def test_parse_event_delegates_correctly(self) -> None:
        """CodexAgentRunner._parse_event delegates to parse_codex_stream_event."""
        runner = CodexAgentRunner(_make_config())
        event, sid, ok, err = runner._parse_event(
            '{"type":"thread.started","thread_id":"s1"}',
            None, "iss-1", "ID-1", 42,
        )
        assert event is not None
        assert event.event == AgentEventType.SESSION_STARTED
        assert sid == "s1"

    def test_parse_agent_message_keeps_more_context(self) -> None:
        long_content = "x" * 1200
        msg = (
            '{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"'
            + long_content
            + '"}}'
        )
        event, sid, ok, err = parse_codex_stream_event(msg, "s1", "iss", "ID-1", 1)
        assert event is not None
        assert event.message == long_content


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
