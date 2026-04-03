from __future__ import annotations

from cymphony.config import build_config, validate_dispatch_config
from cymphony.models import QAReviewConfig, WorkflowDefinition


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
            "runner": {"command": "claude"},
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
    wf = WorkflowDefinition(
        config={
            "tracker": {"kind": "linear", "api_key": "k", "project_slug": "p"},
            "agent": {"max_concurrent_agents": 1, "max_turns": 1, "provider": "codex"},
            "runner": {},
        },
        prompt_template="test",
    )
    config = build_config(wf)
    result = validate_dispatch_config(config)
    assert result.ok


def test_validate_dispatch_config_rejects_invalid_provider() -> None:
    config = build_config(_workflow({"provider": "gpt4"}))
    result = validate_dispatch_config(config)
    assert not result.ok
    assert any("agent.provider" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Provider-aware runner command defaults
# ---------------------------------------------------------------------------

def test_runner_command_defaults_to_claude_for_claude_provider() -> None:
    config = build_config(_workflow({"provider": "claude"}))
    assert config.runner.command == "claude"


def test_runner_command_defaults_to_codex_for_codex_provider() -> None:
    wf = WorkflowDefinition(
        config={
            "tracker": {"kind": "linear", "api_key": "k", "project_slug": "p"},
            "agent": {"max_concurrent_agents": 1, "max_turns": 1, "provider": "codex"},
            "runner": {},
        },
        prompt_template="test",
    )
    config = build_config(wf)
    assert config.runner.command == "codex"


def test_runner_command_explicit_override_respected() -> None:
    wf = WorkflowDefinition(
        config={
            "tracker": {"kind": "linear", "api_key": "k", "project_slug": "p"},
            "agent": {"max_concurrent_agents": 1, "max_turns": 1, "provider": "codex"},
            "runner": {"command": "/usr/local/bin/my-codex"},
        },
        prompt_template="test",
    )
    config = build_config(wf)
    assert config.runner.command == "/usr/local/bin/my-codex"


def test_legacy_codex_yaml_key_is_ignored() -> None:
    wf = WorkflowDefinition(
        config={
            "tracker": {"kind": "linear", "api_key": "k", "project_slug": "p"},
            "agent": {"max_concurrent_agents": 1, "max_turns": 1},
            "codex": {"command": "claude", "turn_timeout_ms": 999},
        },
        prompt_template="test",
    )
    config = build_config(wf)
    assert config.runner.command == "claude"
    assert config.runner.turn_timeout_ms != 999


def test_runner_command_blank_string_resolves_to_provider_default() -> None:
    """An explicit empty-string command should resolve to the provider default."""
    wf = WorkflowDefinition(
        config={
            "tracker": {"kind": "linear", "api_key": "k", "project_slug": "p"},
            "agent": {"max_concurrent_agents": 1, "max_turns": 1, "provider": "codex"},
            "runner": {"command": ""},
        },
        prompt_template="test",
    )
    config = build_config(wf)
    assert config.runner.command == "codex"


def test_validate_dispatch_config_rejects_obvious_provider_command_mismatch() -> None:
    wf = WorkflowDefinition(
        config={
            "tracker": {"kind": "linear", "api_key": "k", "project_slug": "p"},
            "agent": {"max_concurrent_agents": 1, "max_turns": 1, "provider": "claude"},
            "runner": {"command": "codex"},
        },
        prompt_template="test",
    )
    config = build_config(wf)
    result = validate_dispatch_config(config)
    assert not result.ok
    assert any("runner.command does not match agent.provider" in e for e in result.errors)


def test_validate_dispatch_config_allows_custom_same_provider_command_path() -> None:
    wf = WorkflowDefinition(
        config={
            "tracker": {"kind": "linear", "api_key": "k", "project_slug": "p"},
            "agent": {"max_concurrent_agents": 1, "max_turns": 1, "provider": "codex"},
            "runner": {"command": "/usr/local/bin/my-codex"},
        },
        prompt_template="test",
    )
    config = build_config(wf)
    result = validate_dispatch_config(config)
    assert result.ok


def test_runner_config_has_no_provider_field() -> None:
    """RunnerConfig should not carry a provider field — provider lives on AgentConfig."""
    from cymphony.models import RunnerConfig
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(RunnerConfig)}
    assert "provider" not in field_names


# ---------------------------------------------------------------------------
# Transition config tests
# ---------------------------------------------------------------------------

def _workflow_with_transitions(transitions: dict | None = None) -> WorkflowDefinition:
    """Minimal workflow with optional transitions block."""
    cfg: dict = {
        "tracker": {
            "kind": "linear",
            "api_key": "lin_test_key",
            "project_slug": "test-project",
        },
        "agent": {"max_concurrent_agents": 2, "max_turns": 5},
        "runner": {"command": "claude"},
    }
    if transitions is not None:
        cfg["transitions"] = transitions
    return WorkflowDefinition(config=cfg, prompt_template="Hello {{ issue.title }}")


def test_transitions_defaults_when_omitted() -> None:
    config = build_config(_workflow_with_transitions())
    assert config.transitions.dispatch == "In Progress"
    assert config.transitions.success == "In Review"
    assert config.transitions.failure is None
    assert config.transitions.blocked is None
    assert config.transitions.cancelled is None


def test_transitions_defaults_when_empty_dict() -> None:
    config = build_config(_workflow_with_transitions({}))
    assert config.transitions.dispatch == "In Progress"
    assert config.transitions.success == "In Review"


def test_transitions_are_opinionated_defaults_even_when_custom_values_are_supplied() -> None:
    config = build_config(_workflow_with_transitions({
        "dispatch": "Working",
        "success": "Done",
        "failure": "Failed",
        "blocked": "On Hold",
        "cancelled": "Cancelled",
    }))
    assert config.transitions.dispatch == "In Progress"
    assert config.transitions.success == "In Review"
    assert config.transitions.failure is None
    assert config.transitions.blocked is None
    assert config.transitions.cancelled is None


def test_transitions_cannot_be_disabled_with_false() -> None:
    config = build_config(_workflow_with_transitions({
        "dispatch": False,
        "success": False,
    }))
    assert config.transitions.dispatch == "In Progress"
    assert config.transitions.success == "In Review"


def test_transitions_cannot_be_disabled_with_empty_string() -> None:
    config = build_config(_workflow_with_transitions({
        "dispatch": "",
        "success": "",
    }))
    assert config.transitions.dispatch == "In Progress"
    assert config.transitions.success == "In Review"


def test_transitions_cannot_be_disabled_with_null() -> None:
    config = build_config(_workflow_with_transitions({
        "dispatch": None,
        "success": None,
    }))
    assert config.transitions.dispatch == "In Progress"
    assert config.transitions.success == "In Review"


def test_transitions_ignore_partial_override() -> None:
    config = build_config(_workflow_with_transitions({
        "success": "Completed",
    }))
    assert config.transitions.dispatch == "In Progress"
    assert config.transitions.success == "In Review"
    assert config.transitions.failure is None


def test_transitions_resolve_returns_configured_state() -> None:
    from cymphony.models import TransitionsConfig
    t = TransitionsConfig(dispatch="Working", success="Done", failure="Failed")
    assert t.resolve("dispatch") == "Working"
    assert t.resolve("success") == "Done"
    assert t.resolve("failure") == "Failed"


def test_transitions_resolve_returns_none_for_unconfigured_event() -> None:
    from cymphony.models import TransitionsConfig
    t = TransitionsConfig()
    assert t.resolve("failure") is None
    assert t.resolve("blocked") is None
    assert t.resolve("cancelled") is None


def test_transitions_resolve_returns_none_for_unknown_event() -> None:
    from cymphony.models import TransitionsConfig
    t = TransitionsConfig()
    assert t.resolve("nonexistent") is None


# ---------------------------------------------------------------------------
# QA review config tests (BAP-193)
# ---------------------------------------------------------------------------

def test_qa_review_defaults_disabled_when_omitted() -> None:
    config = build_config(_workflow_with_transitions())
    qa = config.transitions.qa_review
    assert qa.enabled is False
    assert qa.dispatch == "QA Review"
    assert qa.success == "In Review"
    assert qa.failure == "Todo"
    assert qa.max_bounces == 2
    assert qa.max_retries == 2


def test_qa_review_defaults_disabled_when_empty_transitions() -> None:
    config = build_config(_workflow_with_transitions({}))
    assert config.transitions.qa_review.enabled is False


def test_qa_review_shorthand_true() -> None:
    config = build_config(_workflow_with_transitions({
        "qa_review": True,
    }))
    qa = config.transitions.qa_review
    assert qa.enabled is True
    assert qa.dispatch == "QA Review"
    assert qa.success == "In Review"
    assert qa.failure == "Todo"
    assert "QA Review" in config.tracker.active_states


def test_qa_review_enabled_with_defaults() -> None:
    config = build_config(_workflow_with_transitions({
        "qa_review": {"enabled": True},
    }))
    qa = config.transitions.qa_review
    assert qa.enabled is True
    assert qa.dispatch == "QA Review"
    assert qa.success == "In Review"
    assert qa.failure == "Todo"
    assert qa.max_bounces == 2
    assert qa.max_retries == 2


def test_qa_review_custom_retry_settings_are_respected_but_states_remain_fixed() -> None:
    config = build_config(_workflow_with_transitions({
        "qa_review": {
            "enabled": True,
            "max_bounces": 4,
            "max_retries": 3,
        },
    }))
    qa = config.transitions.qa_review
    assert qa.enabled is True
    assert qa.dispatch == "QA Review"
    assert qa.success == "In Review"
    assert qa.failure == "Todo"
    assert qa.max_bounces == 4
    assert qa.max_retries == 3
    assert "QA Review" in config.tracker.active_states


def test_qa_review_disable_individual_transitions() -> None:
    config = build_config(_workflow_with_transitions({
        "qa_review": {
            "enabled": True,
            "failure": None,
        },
    }))
    qa = config.transitions.qa_review
    assert qa.enabled is True
    assert qa.dispatch == "QA Review"
    assert qa.failure is None


def test_qa_review_does_not_change_fixed_main_transitions() -> None:
    config = build_config(_workflow_with_transitions({
        "dispatch": "Working",
        "success": "Done",
        "qa_review": {"enabled": True},
    }))
    assert config.transitions.dispatch == "In Progress"
    assert config.transitions.success == "In Review"
    assert config.transitions.qa_review.enabled is True


def test_qa_review_validation_rejects_enabled_without_dispatch() -> None:
    config = build_config(_workflow_with_transitions({
        "qa_review": {
            "enabled": True,
            "dispatch": None,
        },
    }))
    result = validate_dispatch_config(config)
    assert not result.ok
    assert any("qa_review.dispatch" in e for e in result.errors)


def test_qa_review_validation_passes_when_disabled() -> None:
    config = build_config(_workflow_with_transitions({
        "qa_review": {
            "enabled": False,
            "dispatch": None,
        },
    }))
    result = validate_dispatch_config(config)
    assert result.ok


def test_qa_review_backward_compat_legacy_workflow() -> None:
    """A workflow with no qa_review config behaves exactly as before."""
    config = build_config(_workflow_with_transitions({
        "dispatch": "In Progress",
        "success": "In Review",
    }))
    assert config.transitions.dispatch == "In Progress"
    assert config.transitions.success == "In Review"
    assert config.transitions.qa_review.enabled is False


def test_qa_review_disabled_does_not_extend_active_states() -> None:
    config = build_config(_workflow_with_transitions({
        "qa_review": {"enabled": False, "dispatch": "QA Review"},
    }))
    assert "QA Review" not in config.tracker.active_states


# ---------------------------------------------------------------------------
# QA agent config tests (BAP-199)
# ---------------------------------------------------------------------------

def test_qa_agent_defaults_to_none_when_omitted() -> None:
    """No qa_review.agent block → QAReviewConfig.agent is None."""
    config = build_config(_workflow_with_transitions({
        "qa_review": {"enabled": True},
    }))
    assert config.transitions.qa_review.agent is None


def test_qa_agent_explicit_override() -> None:
    """Explicit qa_review.agent block creates a CodingAgentConfig."""
    config = build_config(_workflow_with_transitions({
        "qa_review": {
            "enabled": True,
            "agent": {
                "provider": "codex",
                "command": "codex-cli",
                "turn_timeout_ms": 999,
                "stall_timeout_ms": 555,
            },
        },
    }))
    qa_agent = config.transitions.qa_review.agent
    assert qa_agent is not None
    assert qa_agent.provider == "codex"
    assert qa_agent.command == "codex-cli"
    assert qa_agent.turn_timeout_ms == 999
    assert qa_agent.stall_timeout_ms == 555


def test_qa_agent_inherits_defaults_from_main_runner() -> None:
    """Fields omitted in qa_review.agent inherit from the main runner config."""
    config = build_config(_workflow_with_transitions({
        "qa_review": {
            "enabled": True,
            "agent": {
                "provider": "claude",
            },
        },
    }))
    qa_agent = config.transitions.qa_review.agent
    assert qa_agent is not None
    assert qa_agent.provider == "claude"
    # command should inherit from the main runner default ("claude")
    assert qa_agent.command == "claude"
    # timeouts inherit from main runner defaults
    assert qa_agent.turn_timeout_ms == config.runner.turn_timeout_ms
    assert qa_agent.stall_timeout_ms == config.runner.stall_timeout_ms


def test_qa_agent_validation_rejects_invalid_provider() -> None:
    config = build_config(_workflow_with_transitions({
        "qa_review": {
            "enabled": True,
            "agent": {"provider": "gpt4"},
        },
    }))
    result = validate_dispatch_config(config)
    assert not result.ok
    assert any("qa_review.agent.provider" in e for e in result.errors)


def test_qa_agent_empty_command_inherits_from_main() -> None:
    """An empty command in qa_review.agent falls back to the main runner command."""
    config = build_config(_workflow_with_transitions({
        "qa_review": {
            "enabled": True,
            "agent": {"command": ""},
        },
    }))
    qa_agent = config.transitions.qa_review.agent
    assert qa_agent is not None
    assert qa_agent.command == config.runner.command
    result = validate_dispatch_config(config)
    assert result.ok


def test_qa_agent_validation_passes_with_valid_config() -> None:
    config = build_config(_workflow_with_transitions({
        "qa_review": {
            "enabled": True,
            "agent": {
                "provider": "claude",
                "command": "claude",
            },
        },
    }))
    result = validate_dispatch_config(config)
    assert result.ok


def test_qa_agent_validation_rejects_provider_command_mismatch() -> None:
    config = build_config(_workflow_with_transitions({
        "qa_review": {
            "enabled": True,
            "agent": {
                "provider": "codex",
                "command": "claude",
            },
        },
    }))
    result = validate_dispatch_config(config)
    assert not result.ok
    assert any(
        "transitions.qa_review.agent.command does not match" in e
        for e in result.errors
    )


def test_qa_agent_backward_compat_no_agent_block() -> None:
    """Legacy workflow without qa_review.agent works exactly as before."""
    config = build_config(_workflow_with_transitions({
        "qa_review": {"enabled": True, "dispatch": "QA Review"},
    }))
    assert config.transitions.qa_review.agent is None
    assert config.transitions.qa_review.enabled is True
    assert config.transitions.qa_review.dispatch == "QA Review"
