"""Tests for repo preflight checks."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from cymphony.config import build_config
from cymphony.models import PreflightConfig, WorkflowDefinition
from cymphony.preflight import (
    PreflightResult,
    check_base_branch,
    check_clean_worktree,
    check_env_var,
    check_git_remote,
    check_git_repo,
    check_required_cli,
    run_preflight_checks,
)


# ---------------------------------------------------------------------------
# Individual check functions
# ---------------------------------------------------------------------------


def test_check_required_cli_found() -> None:
    result = check_required_cli("git")
    assert result.ok
    assert result.name == "cli:git"


def test_check_required_cli_missing() -> None:
    result = check_required_cli("nonexistent_cli_xyz_123")
    assert not result.ok
    assert "not found on PATH" in result.message


def test_check_env_var_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_PREFLIGHT_VAR", "hello")
    result = check_env_var("TEST_PREFLIGHT_VAR")
    assert result.ok
    assert result.name == "env:TEST_PREFLIGHT_VAR"


def test_check_env_var_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_PREFLIGHT_VAR", "")
    result = check_env_var("TEST_PREFLIGHT_VAR")
    assert not result.ok
    assert "not set or empty" in result.message


def test_check_env_var_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_PREFLIGHT_VAR", raising=False)
    result = check_env_var("TEST_PREFLIGHT_VAR")
    assert not result.ok


def test_check_git_repo_exists(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    result = check_git_repo(str(tmp_path))
    assert result.ok


def test_check_git_repo_missing(tmp_path: Path) -> None:
    result = check_git_repo(str(tmp_path))
    assert not result.ok
    assert "No .git directory" in result.message


@pytest.mark.asyncio
async def test_check_git_remote_present(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.com/repo.git"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    result = await check_git_remote(str(tmp_path))
    assert result.ok
    assert "origin" in result.message


@pytest.mark.asyncio
async def test_check_git_remote_missing(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    result = await check_git_remote(str(tmp_path))
    assert not result.ok
    assert "No git remotes" in result.message


@pytest.mark.asyncio
async def test_check_base_branch_exists(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "README").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    result = await check_base_branch(str(tmp_path), "main")
    assert result.ok


@pytest.mark.asyncio
async def test_check_base_branch_missing(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "README").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    result = await check_base_branch(str(tmp_path), "nonexistent-branch")
    assert not result.ok
    assert "not found" in result.message


@pytest.mark.asyncio
async def test_check_clean_worktree_clean(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "README").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    result = await check_clean_worktree(str(tmp_path))
    assert result.ok


@pytest.mark.asyncio
async def test_check_clean_worktree_dirty(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "README").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    # Make dirty
    (tmp_path / "dirty_file.txt").write_text("uncommitted")
    result = await check_clean_worktree(str(tmp_path))
    assert not result.ok
    assert "uncommitted" in result.message


# ---------------------------------------------------------------------------
# run_preflight_checks() integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_preflight_all_pass(tmp_path: Path) -> None:
    """All checks pass for a valid git repo with required CLIs."""
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.com/repo.git"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    (tmp_path / "README").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    config = PreflightConfig(
        enabled=True,
        required_clis=["git"],
        required_env_vars=[],
        expect_clean_worktree=False,
        base_branch="main",
    )
    result = await run_preflight_checks(config, workspace_path=str(tmp_path))
    assert result.ok
    assert len(result.errors) == 0


@pytest.mark.asyncio
async def test_run_preflight_missing_cli() -> None:
    """Preflight fails when a required CLI is missing."""
    config = PreflightConfig(
        enabled=True,
        required_clis=["nonexistent_tool_abc"],
        required_env_vars=[],
        expect_clean_worktree=False,
        base_branch="main",
    )
    result = await run_preflight_checks(config, workspace_path=None)
    assert not result.ok
    assert any(c.name == "cli:nonexistent_tool_abc" for c in result.errors)


@pytest.mark.asyncio
async def test_run_preflight_missing_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Preflight fails when a required env var is missing."""
    monkeypatch.delenv("MISSING_VAR_XYZ", raising=False)
    config = PreflightConfig(
        enabled=True,
        required_clis=[],
        required_env_vars=["MISSING_VAR_XYZ"],
        expect_clean_worktree=False,
        base_branch="main",
    )
    result = await run_preflight_checks(config, workspace_path=None)
    assert not result.ok
    assert any(c.name == "env:MISSING_VAR_XYZ" for c in result.errors)


@pytest.mark.asyncio
async def test_run_preflight_no_git_repo(tmp_path: Path) -> None:
    """Preflight detects missing git repo in workspace."""
    config = PreflightConfig(
        enabled=True,
        required_clis=[],
        required_env_vars=[],
        expect_clean_worktree=False,
        base_branch="main",
    )
    result = await run_preflight_checks(config, workspace_path=str(tmp_path))
    assert not result.ok
    assert any(c.name == "git_repo" for c in result.errors)


@pytest.mark.asyncio
async def test_run_preflight_dirty_worktree(tmp_path: Path) -> None:
    """Preflight detects dirty worktree when expect_clean_worktree is set."""
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.com/repo.git"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    (tmp_path / "README").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "dirty.txt").write_text("uncommitted")

    config = PreflightConfig(
        enabled=True,
        required_clis=[],
        required_env_vars=[],
        expect_clean_worktree=True,
        base_branch="main",
    )
    result = await run_preflight_checks(config, workspace_path=str(tmp_path))
    assert not result.ok
    assert any(c.name == "clean_worktree" for c in result.errors)


@pytest.mark.asyncio
async def test_run_preflight_multiple_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multiple preflight failures are all collected."""
    monkeypatch.delenv("MISSING_VAR_1", raising=False)
    monkeypatch.delenv("MISSING_VAR_2", raising=False)
    config = PreflightConfig(
        enabled=True,
        required_clis=["nonexistent_cli_1"],
        required_env_vars=["MISSING_VAR_1", "MISSING_VAR_2"],
        expect_clean_worktree=False,
        base_branch="main",
    )
    result = await run_preflight_checks(config, workspace_path=None)
    assert not result.ok
    assert len(result.errors) == 3


@pytest.mark.asyncio
async def test_run_preflight_skips_git_checks_without_workspace() -> None:
    """When workspace_path is None, only CLI/env checks run."""
    config = PreflightConfig(
        enabled=True,
        required_clis=["git"],
        required_env_vars=[],
        expect_clean_worktree=True,
        base_branch="main",
    )
    result = await run_preflight_checks(config, workspace_path=None)
    assert result.ok
    # Only the CLI check should be present, no git checks
    assert len(result.checks) == 1
    assert result.checks[0].name == "cli:git"


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_build_config_preflight_defaults() -> None:
    """Preflight config has sensible defaults when not specified."""
    workflow = WorkflowDefinition(
        config={
            "tracker": {"kind": "linear", "api_key": "key", "project_slug": "proj"},
            "codex": {"command": "claude"},
        },
        prompt_template="",
    )
    config = build_config(workflow)
    assert config.preflight.enabled is True
    assert "git" in config.preflight.required_clis
    assert config.preflight.base_branch == "main"
    assert config.preflight.expect_clean_worktree is False


def test_build_config_preflight_custom() -> None:
    """Preflight config is parsed from YAML."""
    workflow = WorkflowDefinition(
        config={
            "tracker": {"kind": "linear", "api_key": "key", "project_slug": "proj"},
            "codex": {"command": "claude"},
            "preflight": {
                "enabled": False,
                "required_clis": ["git", "gh", "claude"],
                "required_env_vars": ["ANTHROPIC_API_KEY"],
                "expect_clean_worktree": True,
                "base_branch": "develop",
            },
        },
        prompt_template="",
    )
    config = build_config(workflow)
    assert config.preflight.enabled is False
    assert config.preflight.required_clis == ["git", "gh", "claude"]
    assert config.preflight.required_env_vars == ["ANTHROPIC_API_KEY"]
    assert config.preflight.expect_clean_worktree is True
    assert config.preflight.base_branch == "develop"


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_tick_blocks_on_preflight_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Orchestrator _tick_once stops dispatch when preflight fails."""
    from cymphony.models import (
        AgentConfig,
        CodingAgentConfig,
        HooksConfig,
        PollingConfig,
        ServerConfig,
        ServiceConfig,
        TrackerConfig,
        WorkspaceConfig,
    )
    from cymphony.orchestrator import Orchestrator

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
            after_create=None, before_run=None, after_run=None,
            before_remove=None, timeout_ms=1000,
        ),
        agent=AgentConfig(
            max_concurrent_agents=1, max_turns=1,
            max_retry_backoff_ms=1000, max_concurrent_agents_by_state={},
        ),
        runner=CodingAgentConfig(
            command="codex", turn_timeout_ms=1000,
            stall_timeout_ms=1000, dangerously_skip_permissions=True,
        ),
        server=ServerConfig(port=None),
        preflight=PreflightConfig(
            enabled=True,
            required_clis=["nonexistent_cli_for_test"],
            required_env_vars=[],
            expect_clean_worktree=False,
            base_branch="main",
        ),
    )
    workflow = WorkflowDefinition(config={}, prompt_template="")
    orch = Orchestrator(Path("WORKFLOW.md"), config, workflow)

    # Stub out reconcile and fetch so only preflight matters
    async def noop_reconcile() -> None:
        pass

    fetch_called = False

    async def fake_fetch(*a: object, **kw: object) -> list:
        nonlocal fetch_called
        fetch_called = True
        return []

    monkeypatch.setattr(orch, "_reconcile_running_issues", noop_reconcile)
    from cymphony import linear
    monkeypatch.setattr(linear.LinearClient, "fetch_candidate_issues", fake_fetch)

    await orch._tick_once()

    # Preflight should have blocked — fetch_candidates should NOT have been called
    assert not fetch_called
    assert len(orch._state.last_preflight_errors) > 0
    assert orch._state.last_preflight_errors[0]["name"] == "cli:nonexistent_cli_for_test"


@pytest.mark.asyncio
async def test_orchestrator_tick_passes_preflight_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Orchestrator proceeds past preflight when preflight.enabled=False."""
    from cymphony.models import (
        AgentConfig,
        CodingAgentConfig,
        HooksConfig,
        PollingConfig,
        ServerConfig,
        ServiceConfig,
        TrackerConfig,
        WorkspaceConfig,
    )
    from cymphony.orchestrator import Orchestrator

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
            after_create=None, before_run=None, after_run=None,
            before_remove=None, timeout_ms=1000,
        ),
        agent=AgentConfig(
            max_concurrent_agents=1, max_turns=1,
            max_retry_backoff_ms=1000, max_concurrent_agents_by_state={},
        ),
        runner=CodingAgentConfig(
            command="codex", turn_timeout_ms=1000,
            stall_timeout_ms=1000, dangerously_skip_permissions=True,
        ),
        server=ServerConfig(port=None),
        preflight=PreflightConfig(
            enabled=False,
            required_clis=["nonexistent_cli_for_test"],
            required_env_vars=[],
            expect_clean_worktree=False,
            base_branch="main",
        ),
    )
    workflow = WorkflowDefinition(config={}, prompt_template="")
    orch = Orchestrator(Path("WORKFLOW.md"), config, workflow)

    async def noop_reconcile() -> None:
        pass

    fetch_called = False

    async def fake_fetch(*a: object, **kw: object) -> list:
        nonlocal fetch_called
        fetch_called = True
        return []

    monkeypatch.setattr(orch, "_reconcile_running_issues", noop_reconcile)
    from cymphony import linear
    monkeypatch.setattr(linear.LinearClient, "fetch_candidate_issues", fake_fetch)

    await orch._tick_once()

    # With preflight disabled, fetch should have been called
    assert fetch_called


@pytest.mark.asyncio
async def test_snapshot_includes_preflight_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Snapshot includes preflight_errors field."""
    from cymphony.models import (
        AgentConfig,
        CodingAgentConfig,
        HooksConfig,
        PollingConfig,
        ServerConfig,
        ServiceConfig,
        TrackerConfig,
        WorkspaceConfig,
    )
    from cymphony.orchestrator import Orchestrator

    config = ServiceConfig(
        tracker=TrackerConfig(
            kind="linear",
            endpoint="https://example.test/graphql",
            api_key="test-key",
            project_slug="proj",
            active_states=["Todo"],
            terminal_states=["Done"],
            assignee=None,
        ),
        polling=PollingConfig(interval_ms=25),
        workspace=WorkspaceConfig(root="/tmp/cymphony-tests"),
        hooks=HooksConfig(
            after_create=None, before_run=None, after_run=None,
            before_remove=None, timeout_ms=1000,
        ),
        agent=AgentConfig(
            max_concurrent_agents=1, max_turns=1,
            max_retry_backoff_ms=1000, max_concurrent_agents_by_state={},
        ),
        runner=CodingAgentConfig(
            command="codex", turn_timeout_ms=1000,
            stall_timeout_ms=1000, dangerously_skip_permissions=True,
        ),
        server=ServerConfig(port=None),
        preflight=PreflightConfig(
            enabled=False,
            required_clis=[],
            required_env_vars=[],
            expect_clean_worktree=False,
            base_branch="main",
        ),
    )
    workflow = WorkflowDefinition(config={}, prompt_template="")
    orch = Orchestrator(Path("WORKFLOW.md"), config, workflow)

    snap = orch.snapshot()
    assert "preflight_errors" in snap
    assert "validation_errors" in snap
    assert snap["preflight_errors"] == []
    assert snap["validation_errors"] == []
