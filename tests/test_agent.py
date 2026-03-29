from __future__ import annotations

from cymphony.agent import (
    BaseAgentRunner,
    ClaudeAgentRunner,
    CodexAgentRunner,
    create_agent_runner,
)
from cymphony.models import CodingAgentConfig


def _build_config(command: str = "claude") -> CodingAgentConfig:
    return CodingAgentConfig(
        command=command,
        turn_timeout_ms=1000,
        read_timeout_ms=1000,
        stall_timeout_ms=1000,
        dangerously_skip_permissions=True,
    )


def _build_runner() -> ClaudeAgentRunner:
    return ClaudeAgentRunner(_build_config())


def test_claude_runner_env_preserves_auth_vars(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("CLAUDE_API_KEY", "claude-api-key")

    env = _build_runner()._build_env()

    assert env["ANTHROPIC_API_KEY"] == "sk-ant-test"
    assert env["CLAUDE_API_KEY"] == "claude-api-key"


def test_claude_runner_env_strips_claudecode_sentinel(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")

    env = _build_runner()._build_env()

    assert "CLAUDECODE" not in env


# --- Provider factory tests ---


def test_create_agent_runner_returns_claude_by_default() -> None:
    runner = create_agent_runner("claude", _build_config())
    assert isinstance(runner, ClaudeAgentRunner)


def test_create_agent_runner_returns_codex_for_codex_provider() -> None:
    runner = create_agent_runner("codex", _build_config("codex"))
    assert isinstance(runner, CodexAgentRunner)


def test_create_agent_runner_falls_back_to_claude_for_unknown() -> None:
    runner = create_agent_runner("unknown", _build_config())
    assert isinstance(runner, ClaudeAgentRunner)


# --- CodexAgentRunner tests ---


def test_codex_runner_does_not_strip_claudecode_sentinel(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    runner = CodexAgentRunner(_build_config("codex"))

    env = runner._build_env()

    assert env.get("CLAUDECODE") == "1"


def test_codex_runner_build_command_includes_resume_when_session_id_set() -> None:
    runner = CodexAgentRunner(_build_config("codex"))

    cmd = runner._build_command("fix the bug", "/tmp/ws", "sess-123", "BAP-1")

    assert "--resume" in cmd
    assert "sess-123" in cmd


def test_codex_runner_build_command_omits_resume_for_fresh_session() -> None:
    runner = CodexAgentRunner(_build_config("codex"))

    cmd = runner._build_command("fix the bug", "/tmp/ws", None, "BAP-1")

    assert "--resume" not in cmd
