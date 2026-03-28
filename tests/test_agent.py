from __future__ import annotations

from cymphony.agent import ClaudeAgentRunner
from cymphony.models import CodingAgentConfig


def _build_runner() -> ClaudeAgentRunner:
    return ClaudeAgentRunner(
        CodingAgentConfig(
            command="claude",
            turn_timeout_ms=1000,
            read_timeout_ms=1000,
            stall_timeout_ms=1000,
            dangerously_skip_permissions=True,
        )
    )


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
