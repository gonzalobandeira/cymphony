"""Agent runner implementations (spec §10).

Provider-agnostic runner interface:
  BaseAgentRunner defines the contract: _build_command, _build_env, and _parse_event.
  Each provider (Claude, Codex) implements these behind a common run_turn() method.
  The orchestrator uses create_runner() to get the right implementation from config.
"""

from __future__ import annotations

from ..models import AgentError, CodingAgentConfig
from .base import BaseAgentRunner, OnAgentEvent
from .claude import ClaudeAgentRunner, parse_claude_stream_event
from .codex import CodexAgentRunner

__all__ = [
    "BaseAgentRunner",
    "ClaudeAgentRunner",
    "CodexAgentRunner",
    "OnAgentEvent",
    "create_agent_runner",
    "create_runner",
    "parse_claude_stream_event",
]


_PROVIDERS: dict[str, type[BaseAgentRunner]] = {
    "claude": ClaudeAgentRunner,
    "codex": CodexAgentRunner,
}


def create_agent_runner(provider: str, config: CodingAgentConfig) -> BaseAgentRunner:
    """Factory that returns the correct runner for the configured provider."""
    runner_cls = _PROVIDERS.get(provider)
    if runner_cls is None:
        raise AgentError(
            "unknown_provider",
            f"Unknown agent provider {provider!r}. Supported: {', '.join(_PROVIDERS)}",
        )
    return runner_cls(config)


def create_runner(config: CodingAgentConfig) -> BaseAgentRunner:
    """Backward-compatible factory keyed off the coding-agent config."""
    return create_agent_runner(config.provider, config)


# Backward-compatible alias
AgentRunner = ClaudeAgentRunner
