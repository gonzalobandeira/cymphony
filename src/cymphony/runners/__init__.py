"""Agent runner implementations (spec §10).

Provider-agnostic runner interface:
  BaseAgentRunner defines the contract: _build_command, _build_env, and _parse_event.
  Each provider (Claude, Codex) implements these behind a common run_turn() method.
  The orchestrator uses create_runner() to get the right implementation from config.
"""

from __future__ import annotations

from ..models import AgentError, CodingAgentConfig, RunnerConfig
from .base import BaseAgentRunner, OnAgentEvent
from .claude import ClaudeAgentRunner, parse_claude_stream_event
from .codex import CodexAgentRunner, parse_codex_stream_event, extract_plan_todos_from_codex_event

__all__ = [
    "BaseAgentRunner",
    "ClaudeAgentRunner",
    "CodexAgentRunner",
    "OnAgentEvent",
    "create_agent_runner",
    "create_runner",
    "parse_claude_stream_event",
    "parse_codex_stream_event",
    "extract_plan_todos_from_codex_event",
]


_PROVIDERS: dict[str, type[BaseAgentRunner]] = {
    "claude": ClaudeAgentRunner,
    "codex": CodexAgentRunner,
}


def create_agent_runner(provider: str, config: RunnerConfig) -> BaseAgentRunner:
    """Factory that returns the correct runner for the configured provider."""
    runner_cls = _PROVIDERS.get(provider)
    if runner_cls is None:
        raise AgentError(
            "unknown_provider",
            f"Unknown agent provider {provider!r}. Supported: {', '.join(_PROVIDERS)}",
        )
    return runner_cls(config)


def create_runner(config: RunnerConfig | CodingAgentConfig) -> BaseAgentRunner:
    """Backward-compatible factory keyed off the config object."""
    provider = getattr(config, "provider", "claude")
    return create_agent_runner(provider, config)


# Backward-compatible aliases
AgentRunner = ClaudeAgentRunner
