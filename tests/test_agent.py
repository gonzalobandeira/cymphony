"""Tests for the agent runner interface, factory, and backward-compatible re-exports.

Claude-specific tests live in test_claude_runner.py.
Codex-specific tests live in test_codex_runner.py.
"""

from __future__ import annotations

import pytest

from cymphony.models import AgentError, CodingAgentConfig


def _make_config(
    provider: str = "claude",
    command: str = "claude",
) -> CodingAgentConfig:
    return CodingAgentConfig(
        command=command,
        turn_timeout_ms=1000,
        read_timeout_ms=1000,
        stall_timeout_ms=1000,
        dangerously_skip_permissions=True,
        provider=provider,
    )


# ---------------------------------------------------------------------------
# Base runner contract
# ---------------------------------------------------------------------------

def test_base_runner_cannot_be_instantiated() -> None:
    from cymphony.runners import BaseAgentRunner

    with pytest.raises(TypeError):
        BaseAgentRunner(_make_config())  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def test_create_runner_claude() -> None:
    from cymphony.runners import ClaudeAgentRunner, create_runner

    runner = create_runner(_make_config(provider="claude"))
    assert isinstance(runner, ClaudeAgentRunner)


def test_create_runner_codex() -> None:
    from cymphony.runners import CodexAgentRunner, create_runner

    runner = create_runner(_make_config(provider="codex", command="codex"))
    assert isinstance(runner, CodexAgentRunner)


def test_create_runner_unknown_raises() -> None:
    from cymphony.runners import create_runner

    with pytest.raises(AgentError, match="Unknown agent provider"):
        create_runner(_make_config(provider="gemini"))


def test_create_agent_runner_returns_claude() -> None:
    from cymphony.runners import ClaudeAgentRunner, create_agent_runner

    runner = create_agent_runner("claude", _make_config())
    assert isinstance(runner, ClaudeAgentRunner)


def test_create_agent_runner_returns_codex() -> None:
    from cymphony.runners import CodexAgentRunner, create_agent_runner

    runner = create_agent_runner(
        "codex",
        _make_config(provider="codex", command="codex"),
    )
    assert isinstance(runner, CodexAgentRunner)


def test_create_agent_runner_unknown_raises() -> None:
    from cymphony.runners import create_agent_runner

    with pytest.raises(AgentError, match="Unknown agent provider"):
        create_agent_runner("unknown", _make_config())


# ---------------------------------------------------------------------------
# Backward-compatible re-exports from cymphony.agent
# ---------------------------------------------------------------------------

def test_agent_module_reexports_base_runner() -> None:
    from cymphony.agent import BaseAgentRunner
    from cymphony.runners.base import BaseAgentRunner as DirectBase

    assert BaseAgentRunner is DirectBase


def test_agent_module_reexports_claude_runner() -> None:
    from cymphony.agent import ClaudeAgentRunner
    from cymphony.runners.claude import ClaudeAgentRunner as DirectClaude

    assert ClaudeAgentRunner is DirectClaude


def test_agent_module_reexports_codex_runner() -> None:
    from cymphony.agent import CodexAgentRunner
    from cymphony.runners.codex import CodexAgentRunner as DirectCodex

    assert CodexAgentRunner is DirectCodex


def test_agent_module_reexports_factories() -> None:
    from cymphony.agent import create_agent_runner, create_runner
    from cymphony.runners import (
        create_agent_runner as direct_car,
        create_runner as direct_cr,
    )

    assert create_agent_runner is direct_car
    assert create_runner is direct_cr


def test_agent_module_reexports_parse_function() -> None:
    from cymphony.agent import _parse_claude_stream_event
    from cymphony.runners.claude import parse_claude_stream_event

    assert _parse_claude_stream_event is parse_claude_stream_event


def test_agent_module_reexports_agent_runner_alias() -> None:
    from cymphony.agent import AgentRunner
    from cymphony.runners.claude import ClaudeAgentRunner

    assert AgentRunner is ClaudeAgentRunner
