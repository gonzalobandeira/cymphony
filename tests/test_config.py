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
        "codex": {"command": "claude"},
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


def test_transitions_custom_values() -> None:
    config = build_config(_workflow_with_transitions({
        "dispatch": "Working",
        "success": "Done",
        "failure": "Failed",
        "blocked": "On Hold",
        "cancelled": "Cancelled",
    }))
    assert config.transitions.dispatch == "Working"
    assert config.transitions.success == "Done"
    assert config.transitions.failure == "Failed"
    assert config.transitions.blocked == "On Hold"
    assert config.transitions.cancelled == "Cancelled"


def test_transitions_disable_with_false() -> None:
    config = build_config(_workflow_with_transitions({
        "dispatch": False,
        "success": False,
    }))
    assert config.transitions.dispatch is None
    assert config.transitions.success is None


def test_transitions_disable_with_empty_string() -> None:
    config = build_config(_workflow_with_transitions({
        "dispatch": "",
        "success": "",
    }))
    assert config.transitions.dispatch is None
    assert config.transitions.success is None


def test_transitions_disable_with_null() -> None:
    config = build_config(_workflow_with_transitions({
        "dispatch": None,
        "success": None,
    }))
    assert config.transitions.dispatch is None
    assert config.transitions.success is None


def test_transitions_partial_override() -> None:
    config = build_config(_workflow_with_transitions({
        "success": "Completed",
    }))
    assert config.transitions.dispatch == "In Progress"
    assert config.transitions.success == "Completed"
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


def test_qa_review_custom_states() -> None:
    config = build_config(_workflow_with_transitions({
        "qa_review": {
            "enabled": True,
            "dispatch": "QA",
            "success": "Ready to Merge",
            "failure": "Needs Work",
        },
    }))
    qa = config.transitions.qa_review
    assert qa.enabled is True
    assert qa.dispatch == "QA"
    assert qa.success == "Ready to Merge"
    assert qa.failure == "Needs Work"
    assert "QA" in config.tracker.active_states


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


def test_qa_review_does_not_affect_main_transitions() -> None:
    config = build_config(_workflow_with_transitions({
        "dispatch": "Working",
        "success": "Done",
        "qa_review": {"enabled": True},
    }))
    assert config.transitions.dispatch == "Working"
    assert config.transitions.success == "Done"
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
