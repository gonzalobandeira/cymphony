"""Tests for workflow transition validation against Linear workflow states (BAP-166)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from cymphony.models import (
    AgentConfig,
    CodingAgentConfig,
    HooksConfig,
    PollingConfig,
    PreflightConfig,
    ServerConfig,
    ServiceConfig,
    TrackerConfig,
    TransitionsConfig,
    WorkflowDefinition,
    WorkflowError,
    WorkspaceConfig,
)
from cymphony.orchestrator import Orchestrator


def _build_orchestrator(transitions: TransitionsConfig | None = None) -> Orchestrator:
    config = ServiceConfig(
        tracker=TrackerConfig(
            kind="linear",
            endpoint="https://example.test/graphql",
            api_key="test-key",
            project_slug="proj",
            active_states=["Todo", "In Progress"],
            terminal_states=["Done"],
            assignee=None,
        ),
        polling=PollingConfig(interval_ms=25),
        workspace=WorkspaceConfig(root="/tmp/cymphony-tests"),
        hooks=HooksConfig(
            after_create=None,
            before_run=None,
            after_run=None,
            before_remove=None,
            timeout_ms=1000,
        ),
        agent=AgentConfig(
            max_concurrent_agents=1,
            max_turns=1,
            max_retry_backoff_ms=1000,
            max_concurrent_agents_by_state={},
        ),
        coding_agent=CodingAgentConfig(
            command="codex",
            turn_timeout_ms=1000,
            read_timeout_ms=1000,
            stall_timeout_ms=1000,
            dangerously_skip_permissions=True,
        ),
        server=ServerConfig(port=None),
        preflight=PreflightConfig(
            enabled=False,
            required_clis=[],
            required_env_vars=[],
            expect_clean_worktree=False,
            base_branch="main",
        ),
        transitions=transitions or TransitionsConfig(),
    )
    workflow = WorkflowDefinition(config={}, prompt_template="")
    return Orchestrator(Path("WORKFLOW.md"), config, workflow)


# ---------------------------------------------------------------------------
# Valid configurations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_transitions_all_valid() -> None:
    """All configured targets exist on the team — validation passes."""
    orch = _build_orchestrator(TransitionsConfig(
        dispatch="In Progress",
        success="In Review",
    ))

    with patch("cymphony.orchestrator.LinearClient") as MockClient:
        client = MockClient.return_value
        client.fetch_project_team_ids = AsyncMock(return_value=["team-1"])
        client.fetch_team_workflow_state_names = AsyncMock(
            return_value=["Todo", "In Progress", "In Review", "Done"]
        )
        client.fetch_team_workflow_state_id = AsyncMock(return_value="state-id-1")

        result = await orch._validate_transitions(fail_hard=False)

    assert result is True


@pytest.mark.asyncio
async def test_validate_transitions_case_insensitive() -> None:
    """State name matching is case-insensitive."""
    orch = _build_orchestrator(TransitionsConfig(
        dispatch="in progress",
        success="IN REVIEW",
    ))

    with patch("cymphony.orchestrator.LinearClient") as MockClient:
        client = MockClient.return_value
        client.fetch_project_team_ids = AsyncMock(return_value=["team-1"])
        client.fetch_team_workflow_state_names = AsyncMock(
            return_value=["Todo", "In Progress", "In Review", "Done"]
        )
        client.fetch_team_workflow_state_id = AsyncMock(return_value="state-id-1")

        result = await orch._validate_transitions(fail_hard=False)

    assert result is True


@pytest.mark.asyncio
async def test_validate_transitions_no_targets_configured() -> None:
    """All transitions disabled (None) — skip validation."""
    orch = _build_orchestrator(TransitionsConfig(
        dispatch=None,
        success=None,
        failure=None,
        blocked=None,
        cancelled=None,
    ))

    result = await orch._validate_transitions(fail_hard=False)
    assert result is True


# ---------------------------------------------------------------------------
# Invalid configurations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_transitions_invalid_target_fail_hard() -> None:
    """Invalid target with fail_hard=True raises WorkflowError."""
    orch = _build_orchestrator(TransitionsConfig(
        dispatch="In Progress",
        success="Nonexistent State",
    ))

    with patch("cymphony.orchestrator.LinearClient") as MockClient:
        client = MockClient.return_value
        client.fetch_project_team_ids = AsyncMock(return_value=["team-1"])
        client.fetch_team_workflow_state_names = AsyncMock(
            return_value=["Todo", "In Progress", "In Review", "Done"]
        )

        with pytest.raises(WorkflowError, match="Nonexistent State"):
            await orch._validate_transitions(fail_hard=True)


@pytest.mark.asyncio
async def test_validate_transitions_invalid_target_warn_only() -> None:
    """Invalid target with fail_hard=False returns False (warns only)."""
    orch = _build_orchestrator(TransitionsConfig(
        dispatch="In Progress",
        success="Nonexistent State",
    ))

    with patch("cymphony.orchestrator.LinearClient") as MockClient:
        client = MockClient.return_value
        client.fetch_project_team_ids = AsyncMock(return_value=["team-1"])
        client.fetch_team_workflow_state_names = AsyncMock(
            return_value=["Todo", "In Progress", "In Review", "Done"]
        )
        client.fetch_team_workflow_state_id = AsyncMock(return_value="state-id-1")

        result = await orch._validate_transitions(fail_hard=False)

    assert result is False


@pytest.mark.asyncio
async def test_validate_transitions_failure_target_invalid() -> None:
    """Invalid failure transition target is caught."""
    orch = _build_orchestrator(TransitionsConfig(
        dispatch="In Progress",
        success="In Review",
        failure="Borked",
    ))

    with patch("cymphony.orchestrator.LinearClient") as MockClient:
        client = MockClient.return_value
        client.fetch_project_team_ids = AsyncMock(return_value=["team-1"])
        client.fetch_team_workflow_state_names = AsyncMock(
            return_value=["Todo", "In Progress", "In Review", "Done"]
        )

        with pytest.raises(WorkflowError, match="Borked"):
            await orch._validate_transitions(fail_hard=True)


# ---------------------------------------------------------------------------
# Multi-team environments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_transitions_multi_team_all_valid() -> None:
    """Targets valid on all teams — validation passes."""
    orch = _build_orchestrator(TransitionsConfig(
        dispatch="In Progress",
        success="In Review",
    ))

    with patch("cymphony.orchestrator.LinearClient") as MockClient:
        client = MockClient.return_value
        client.fetch_project_team_ids = AsyncMock(return_value=["team-1", "team-2"])
        client.fetch_team_workflow_state_names = AsyncMock(
            return_value=["Todo", "In Progress", "In Review", "Done"]
        )
        client.fetch_team_workflow_state_id = AsyncMock(return_value="state-id-1")

        result = await orch._validate_transitions(fail_hard=False)

    assert result is True
    # Should have been called for both teams
    assert client.fetch_team_workflow_state_names.call_count == 2


@pytest.mark.asyncio
async def test_validate_transitions_multi_team_missing_on_one() -> None:
    """Target missing on one team — validation fails."""
    orch = _build_orchestrator(TransitionsConfig(
        dispatch="In Progress",
        success="In Review",
    ))

    async def _state_names(team_id: str) -> list[str]:
        if team_id == "team-1":
            return ["Todo", "In Progress", "In Review", "Done"]
        return ["Backlog", "In Progress", "Done"]  # No "In Review"

    with patch("cymphony.orchestrator.LinearClient") as MockClient:
        client = MockClient.return_value
        client.fetch_project_team_ids = AsyncMock(return_value=["team-1", "team-2"])
        client.fetch_team_workflow_state_names = AsyncMock(side_effect=_state_names)
        client.fetch_team_workflow_state_id = AsyncMock(return_value="state-id-1")

        with pytest.raises(WorkflowError, match="In Review"):
            await orch._validate_transitions(fail_hard=True)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_transitions_team_fetch_fails_hard() -> None:
    """Team fetch failure with fail_hard raises WorkflowError."""
    orch = _build_orchestrator()

    with patch("cymphony.orchestrator.LinearClient") as MockClient:
        client = MockClient.return_value
        client.fetch_project_team_ids = AsyncMock(side_effect=Exception("API error"))

        with pytest.raises(WorkflowError, match="Failed to fetch project teams"):
            await orch._validate_transitions(fail_hard=True)


@pytest.mark.asyncio
async def test_validate_transitions_team_fetch_fails_soft() -> None:
    """Team fetch failure with fail_hard=False returns False."""
    orch = _build_orchestrator()

    with patch("cymphony.orchestrator.LinearClient") as MockClient:
        client = MockClient.return_value
        client.fetch_project_team_ids = AsyncMock(side_effect=Exception("API error"))

        result = await orch._validate_transitions(fail_hard=False)

    assert result is False


@pytest.mark.asyncio
async def test_validate_transitions_no_teams_found_hard() -> None:
    """No teams found with fail_hard raises WorkflowError."""
    orch = _build_orchestrator()

    with patch("cymphony.orchestrator.LinearClient") as MockClient:
        client = MockClient.return_value
        client.fetch_project_team_ids = AsyncMock(return_value=[])

        with pytest.raises(WorkflowError, match="No teams found"):
            await orch._validate_transitions(fail_hard=True)


@pytest.mark.asyncio
async def test_validate_transitions_populates_state_id_cache() -> None:
    """Valid transitions pre-populate the state ID cache."""
    orch = _build_orchestrator(TransitionsConfig(
        dispatch="In Progress",
        success="In Review",
    ))

    with patch("cymphony.orchestrator.LinearClient") as MockClient:
        client = MockClient.return_value
        client.fetch_project_team_ids = AsyncMock(return_value=["team-1"])
        client.fetch_team_workflow_state_names = AsyncMock(
            return_value=["Todo", "In Progress", "In Review", "Done"]
        )

        async def _resolve(team_id: str, name: str) -> str:
            return f"state-{name.lower().replace(' ', '-')}"

        client.fetch_team_workflow_state_id = AsyncMock(side_effect=_resolve)

        await orch._validate_transitions(fail_hard=False)

    assert ("team-1", "in progress") in orch._state_id_cache
    assert ("team-1", "in review") in orch._state_id_cache
    assert orch._state_id_cache[("team-1", "in progress")] == "state-in-progress"
    assert orch._state_id_cache[("team-1", "in review")] == "state-in-review"


# ---------------------------------------------------------------------------
# Reload integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_reload_validates_transitions() -> None:
    """Workflow reload triggers transition validation (warn-only)."""
    orch = _build_orchestrator()

    new_workflow = WorkflowDefinition(
        config={
            "tracker": {
                "kind": "linear",
                "api_key": "test-key",
                "project_slug": "proj",
                "active_states": ["Todo", "In Progress"],
                "terminal_states": ["Done"],
            },
            "workspace": {"root": "/tmp/cymphony-tests"},
            "transitions": {
                "dispatch": "In Progress",
                "success": "Bad State",
            },
        },
        prompt_template="test",
    )

    validate_calls: list[dict[str, bool]] = []

    async def fake_validate(*, fail_hard: bool = False) -> bool:
        validate_calls.append({"fail_hard": fail_hard})
        return False

    orch._validate_transitions = fake_validate  # type: ignore[assignment]

    await orch._on_workflow_change(new_workflow)

    # Validation was called with fail_hard=False (warn only on reload)
    assert len(validate_calls) == 1
    assert validate_calls[0]["fail_hard"] is False


@pytest.mark.asyncio
async def test_workflow_reload_rejects_invalid_transition_config() -> None:
    """Invalid reloads should not replace the active config."""
    orch = _build_orchestrator(TransitionsConfig(dispatch="In Progress"))
    original_config = orch._config
    original_workflow = orch._workflow
    orch._state_id_cache[("team-1", "in progress")] = "state-1"

    new_workflow = WorkflowDefinition(
        config={
            "tracker": {
                "kind": "linear",
                "api_key": "test-key",
                "project_slug": "proj",
                "active_states": ["Todo", "In Progress"],
                "terminal_states": ["Done"],
            },
            "workspace": {"root": "/tmp/cymphony-tests"},
            "transitions": {
                "dispatch": "Bad State",
            },
        },
        prompt_template="invalid",
    )

    async def fake_validate(*, fail_hard: bool = False) -> bool:
        assert fail_hard is False
        return False

    orch._validate_transitions = fake_validate  # type: ignore[assignment]

    await orch._on_workflow_change(new_workflow)

    assert orch._config is original_config
    assert orch._workflow is original_workflow
    assert orch._state_id_cache == {("team-1", "in progress"): "state-1"}
