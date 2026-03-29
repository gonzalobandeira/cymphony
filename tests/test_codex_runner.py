"""Tests for the Codex-specific runner implementation."""

from __future__ import annotations

from cymphony.runners.codex import CodexAgentRunner
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


def test_codex_build_command() -> None:
    runner = CodexAgentRunner(_make_config())
    cmd = runner._build_command("do stuff", "/ws", None, "title")
    assert cmd[0] == "codex"
    assert "--output-format" in cmd
    assert "--dangerously-skip-permissions" in cmd


def test_codex_does_not_strip_claudecode_sentinel(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    runner = CodexAgentRunner(_make_config())

    env = runner._build_env()

    assert env.get("CLAUDECODE") == "1"


def test_codex_build_command_includes_resume() -> None:
    runner = CodexAgentRunner(_make_config())

    cmd = runner._build_command("fix the bug", "/tmp/ws", "sess-123", "BAP-1")

    assert "--resume" in cmd
    idx = cmd.index("--resume")
    assert cmd[idx + 1] == "sess-123"


def test_codex_build_command_omits_resume_for_fresh_session() -> None:
    runner = CodexAgentRunner(_make_config())

    cmd = runner._build_command("fix the bug", "/tmp/ws", None, "BAP-1")

    assert "--resume" not in cmd


def test_codex_build_command_no_skip_permissions() -> None:
    cfg = _make_config()
    cfg.dangerously_skip_permissions = False
    runner = CodexAgentRunner(cfg)
    cmd = runner._build_command("prompt", "/ws", None, "title")
    assert "--dangerously-skip-permissions" not in cmd


def test_codex_parse_event_handles_init() -> None:
    runner = CodexAgentRunner(_make_config())
    event, sid, ok, err = runner._parse_event(
        '{"type": "system", "subtype": "init", "session_id": "s1"}',
        None, "iss-1", "ID-1", 42,
    )
    assert event is not None
    assert event.event == AgentEventType.SESSION_STARTED
    assert sid == "s1"
