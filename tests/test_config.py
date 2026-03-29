from __future__ import annotations

from cymphony.config import build_config, validate_dispatch_config
from cymphony.models import WorkflowDefinition


def _workflow(agent_overrides: dict | None = None) -> WorkflowDefinition:
    """Minimal workflow with sensible defaults for testing."""
    agent = {
        "max_concurrent_agents": 2,
        "max_turns": 5,
    }
    if agent_overrides:
        agent.update(agent_overrides)

    return WorkflowDefinition(
        config={
            "tracker": {
                "kind": "linear",
                "api_key": "lin_test_key",
                "project_slug": "test-project",
            },
            "agent": agent,
            "codex": {"command": "claude"},
        },
        prompt_template="Hello {{ issue.title }}",
    )


def test_build_config_defaults_provider_to_claude() -> None:
    config = build_config(_workflow())
    assert config.agent.provider == "claude"


def test_build_config_reads_provider_from_yaml() -> None:
    config = build_config(_workflow({"provider": "codex"}))
    assert config.agent.provider == "codex"


def test_build_config_normalizes_provider_to_lowercase() -> None:
    config = build_config(_workflow({"provider": "CLAUDE"}))
    assert config.agent.provider == "claude"


def test_validate_dispatch_config_accepts_claude_provider() -> None:
    config = build_config(_workflow({"provider": "claude"}))
    result = validate_dispatch_config(config)
    assert result.ok


def test_validate_dispatch_config_accepts_codex_provider() -> None:
    config = build_config(_workflow({"provider": "codex"}))
    result = validate_dispatch_config(config)
    assert result.ok


def test_validate_dispatch_config_rejects_invalid_provider() -> None:
    config = build_config(_workflow({"provider": "gpt4"}))
    result = validate_dispatch_config(config)
    assert not result.ok
    assert any("agent.provider" in e for e in result.errors)
