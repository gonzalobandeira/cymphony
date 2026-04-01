from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cymphony.models import (
    AgentConfig,
    Comment,
    HooksConfig,
    Issue,
    PollingConfig,
    PreflightConfig,
    QAReviewConfig,
    ServerConfig,
    ServiceConfig,
    TrackerConfig,
    TransitionsConfig,
    WorkflowDefinition,
    WorkspaceConfig,
    CodingAgentConfig,
)
from cymphony.services.linear_service import LinearService
from cymphony.services.pr_service import PRService
from cymphony.services.workspace_service import WorkspaceService
from cymphony.workflows.execution import ExecutionWorkflow


def _build_issue() -> Issue:
    return Issue(
        id="issue-1",
        identifier="BAP-204",
        title="Refactor runtime",
        project_name=None,
        description="Do the refactor.",
        priority=2,
        state="To Do",
        branch_name=None,
        url=None,
        labels=["cymphony"],
        blocked_by=[],
        comments=[
            Comment(author="QA", body="Please cover retry behavior.", created_at=None),
        ],
        created_at=None,
        updated_at=None,
    )


def _build_linear_service() -> LinearService:
    return LinearService(
        TrackerConfig(
            kind="linear",
            endpoint="https://example.test/graphql",
            api_key="test-key",
            project_slug="proj",
            active_states=["To Do", "In Progress", "QA Review"],
            terminal_states=["Done"],
            assignee=None,
        )
    )


def _build_workflow() -> ExecutionWorkflow:
    return ExecutionWorkflow(
        linear=_build_linear_service(),
        workspaces=WorkspaceService(WorkspaceConfig(root="/tmp/cymphony-tests")),
        prs=PRService(),
    )


def _build_service_config(*, qa_enabled: bool) -> ServiceConfig:
    return ServiceConfig(
        tracker=TrackerConfig(
            kind="linear",
            endpoint="https://example.test/graphql",
            api_key="test-key",
            project_slug="proj",
            active_states=["To Do", "In Progress", "QA Review"],
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
            max_concurrent_agents=2,
            max_turns=5,
            max_retry_backoff_ms=1000,
            max_concurrent_agents_by_state={},
        ),
        runner=CodingAgentConfig(
            command="claude",
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
        transitions=TransitionsConfig(
            dispatch="In Progress",
            success="In Review",
            failure="Todo",
            qa_review=QAReviewConfig(
                enabled=qa_enabled,
                dispatch="QA Review",
                success="In Review",
                failure="To Do",
            ),
        ),
    )


def test_execution_workflow_uses_legacy_compatible_workspace_path() -> None:
    workflow = _build_workflow()
    path = workflow.workspace_path_for(_build_issue())
    assert str(path) == "/tmp/cymphony-tests/BAP-204"


def test_execution_workflow_uses_main_agent_runner_config() -> None:
    workflow = _build_workflow()
    config = _build_service_config(qa_enabled=True)
    provider, runner_config = workflow.resolve_agent_runner(config)
    assert provider == config.agent.provider
    assert runner_config is config.runner


def test_execution_workflow_requires_planning() -> None:
    workflow = _build_workflow()
    assert workflow.requires_planning() is True


def test_execution_workflow_success_target_uses_qa_dispatch_when_enabled() -> None:
    workflow = _build_workflow()
    assert workflow.resolve_success_target(_build_service_config(qa_enabled=True)) == "QA Review"


def test_execution_workflow_success_target_uses_normal_success_when_qa_disabled() -> None:
    workflow = _build_workflow()
    assert workflow.resolve_success_target(_build_service_config(qa_enabled=False)) == "In Review"


def test_execution_workflow_failure_target_uses_failure_transition() -> None:
    workflow = _build_workflow()
    assert workflow.resolve_failure_target(_build_service_config(qa_enabled=True)) == "Todo"


def test_execution_workflow_failure_outcome_includes_transition_and_retry() -> None:
    workflow = _build_workflow()
    outcome = workflow.resolve_failure_outcome(
        _build_service_config(qa_enabled=True),
        next_attempt=2,
        error="agent crashed",
    )
    assert outcome.transition_target == "Todo"
    assert outcome.retry.attempt == 2
    assert outcome.retry.delay_ms is None
    assert outcome.retry.error == "agent crashed"


def test_execution_workflow_success_outcome_skips_continuation_retry_when_qa_enabled() -> None:
    workflow = _build_workflow()
    outcome = workflow.resolve_success_outcome(_build_service_config(qa_enabled=True))
    assert outcome.target == "QA Review"
    assert outcome.schedule_continuation_retry is False


def test_execution_workflow_success_outcome_requests_continuation_retry_without_qa() -> None:
    workflow = _build_workflow()
    outcome = workflow.resolve_success_outcome(_build_service_config(qa_enabled=False))
    assert outcome.target == "In Review"
    assert outcome.schedule_continuation_retry is True


def test_execution_workflow_continuation_retry_outcome_uses_clean_retry() -> None:
    workflow = _build_workflow()
    outcome = workflow.resolve_continuation_retry_outcome(delay_ms=1000.0)
    assert outcome.attempt == 1
    assert outcome.delay_ms == 1000.0
    assert outcome.error is None


def test_execution_workflow_failure_retry_outcome_keeps_error() -> None:
    workflow = _build_workflow()
    outcome = workflow.resolve_failure_retry_outcome(
        next_attempt=2,
        error="agent crashed",
    )
    assert outcome.attempt == 2
    assert outcome.delay_ms is None
    assert outcome.error == "agent crashed"


def test_execution_workflow_retry_poll_failure_outcome_increments_attempt() -> None:
    workflow = _build_workflow()
    outcome = workflow.resolve_retry_poll_failure_outcome(attempt=2)
    assert outcome.attempt == 3
    assert outcome.delay_ms is None
    assert outcome.error == "retry poll failed"


def test_execution_workflow_slot_wait_retry_outcome_preserves_continuation_attempt() -> None:
    workflow = _build_workflow()
    outcome = workflow.resolve_slot_wait_retry_outcome(
        attempt=1,
        continuation_delay_ms=1000.0,
        is_continuation=True,
    )
    assert outcome.attempt == 1
    assert outcome.delay_ms == 1000.0
    assert outcome.error is None


def test_execution_workflow_slot_wait_retry_outcome_increments_failure_attempt() -> None:
    workflow = _build_workflow()
    outcome = workflow.resolve_slot_wait_retry_outcome(
        attempt=2,
        continuation_delay_ms=1000.0,
        is_continuation=False,
    )
    assert outcome.attempt == 3
    assert outcome.delay_ms is None
    assert outcome.error == "no available orchestrator slots"


def test_execution_workflow_release_log_action_varies_by_context() -> None:
    workflow = _build_workflow()
    assert workflow.release_log_action(is_continuation=True, issue_found=False) == "retry_claim_released_not_found"
    assert workflow.release_log_action(is_continuation=True, issue_found=True) == "continuation_retry_released_inactive"
    assert workflow.release_log_action(is_continuation=False, issue_found=True) == "retry_claim_released_inactive"


def test_execution_workflow_retry_timer_log_actions() -> None:
    workflow = _build_workflow()
    assert workflow.waiting_for_slot_log_action(is_continuation=True) == "continuation_retry_waiting_for_slot"
    assert workflow.waiting_for_slot_log_action(is_continuation=False) == "retry_waiting_for_slot"
    assert workflow.redispatch_log_action(is_continuation=True) == "continuation_retry_redispatching"
    assert workflow.redispatch_log_action(is_continuation=False) == "retry_dispatching"


def test_execution_workflow_renders_plan_prompt() -> None:
    workflow = _build_workflow()
    issue = _build_issue()
    rendered = workflow.build_plan_prompt(
        WorkflowDefinition(config={}, prompt_template="unused"),
        issue,
    )
    assert "PLANNING ONLY" in rendered
    assert issue.title in rendered


def test_execution_workflow_renders_execution_prompt() -> None:
    workflow = _build_workflow()
    issue = _build_issue()
    rendered = workflow.build_execution_prompt(
        WorkflowDefinition(
            config={},
            prompt_template="Issue {{ issue.identifier }} attempt {{ attempt }}",
        ),
        issue,
        attempt=2,
    )
    assert rendered == "Issue BAP-204 attempt 2"


def test_execution_workflow_build_turn_prompt_uses_initial_prompt_on_first_turn() -> None:
    workflow = _build_workflow()
    issue = _build_issue()
    rendered = workflow.build_turn_prompt(
        WorkflowDefinition(
            config={},
            prompt_template="Issue {{ issue.identifier }} attempt {{ attempt }}",
        ),
        issue,
        attempt=2,
        first_turn=True,
    )
    assert rendered == "Issue BAP-204 attempt 2"


def test_execution_workflow_build_turn_prompt_uses_continuation_after_first_turn() -> None:
    workflow = _build_workflow()
    rendered = workflow.build_turn_prompt(
        WorkflowDefinition(config={}, prompt_template="unused"),
        _build_issue(),
        attempt=None,
        first_turn=False,
    )
    assert "CYMPHONY_COMPLETE" in rendered


def test_execution_workflow_renders_plan_comment() -> None:
    workflow = _build_workflow()
    body = workflow.render_plan_comment(
        [
            {"content": "Inspect orchestrator", "status": "completed"},
            {"content": "Extract execution flow", "status": "in_progress"},
            {"content": "Add tests", "status": "pending"},
        ]
    )
    assert "**Agent Plan**" in body
    assert "- [x] Inspect orchestrator" in body
    assert "- [ ] 🔄 Extract execution flow *(in progress)*" in body
    assert "- [ ] Add tests" in body


def test_execution_workflow_capture_run_result_returns_none() -> None:
    workflow = _build_workflow()
    assert workflow.capture_run_result("/tmp/ws/BAP-204") is None


def test_execution_workflow_stops_when_issue_inactive() -> None:
    workflow = _build_workflow()
    issue = _build_issue()
    issue.state = "Done"
    should_continue, reason = workflow.should_continue(
        issue,
        active_states=["To Do", "In Progress", "QA Review"],
        turn_number=1,
        max_turns=5,
    )
    assert should_continue is False
    assert reason == "inactive_state"


def test_execution_workflow_stops_at_max_turns() -> None:
    workflow = _build_workflow()
    should_continue, reason = workflow.should_continue(
        _build_issue(),
        active_states=["To Do", "In Progress", "QA Review"],
        turn_number=5,
        max_turns=5,
    )
    assert should_continue is False
    assert reason == "max_turns"


def test_execution_workflow_stops_when_agent_reports_completion() -> None:
    workflow = _build_workflow()
    should_continue, reason = workflow.should_continue(
        _build_issue(),
        active_states=["To Do", "In Progress", "QA Review"],
        turn_number=2,
        max_turns=5,
        last_message="CYMPHONY_COMPLETE",
    )
    assert should_continue is False
    assert reason == "task_complete"


def test_execution_workflow_continues_when_issue_active_and_under_limit() -> None:
    workflow = _build_workflow()
    should_continue, reason = workflow.should_continue(
        _build_issue(),
        active_states=["To Do", "In Progress", "QA Review"],
        turn_number=2,
        max_turns=5,
        last_message=None,
    )
    assert should_continue is True
    assert reason is None


@pytest.mark.asyncio
async def test_execution_workflow_prepare_workspace_uses_workspace_manager() -> None:
    workflow = _build_workflow()
    issue = _build_issue()
    captured: list[str] = []

    class FakeManager:
        async def create_for_issue(self, identifier: str):
            captured.append(identifier)
            return SimpleNamespace(path=f"/tmp/ws/{identifier}")

    workspace = await workflow.prepare_workspace(FakeManager(), issue)
    assert captured == ["BAP-204"]
    assert workspace.path == "/tmp/ws/BAP-204"


@pytest.mark.asyncio
async def test_execution_workflow_prepare_workspace_run_returns_manager_and_workspace() -> None:
    workflow = _build_workflow()
    issue = _build_issue()
    captured: list[str] = []

    class FakeManager:
        async def create_for_issue(self, identifier: str):
            captured.append(identifier)
            return SimpleNamespace(path=f"/tmp/ws/{identifier}")

    workflow.create_workspace_manager = lambda config: FakeManager()  # type: ignore[method-assign]
    manager, workspace = await workflow.prepare_workspace_run(
        _build_service_config(qa_enabled=True),
        issue,
    )
    assert captured == ["BAP-204"]
    assert isinstance(manager, FakeManager)
    assert workspace.path == "/tmp/ws/BAP-204"


@pytest.mark.asyncio
async def test_execution_workflow_prepare_run_uses_before_run_hook() -> None:
    workflow = _build_workflow()
    calls: list[str] = []
    workspace = SimpleNamespace(path="/tmp/ws/BAP-204")

    class FakeManager:
        async def run_before_run_hook(self, ws):
            calls.append(ws.path)

    await workflow.prepare_run(FakeManager(), workspace, _build_issue())
    assert calls == ["/tmp/ws/BAP-204"]


@pytest.mark.asyncio
async def test_execution_workflow_refresh_issue_returns_first_issue() -> None:
    workflow = _build_workflow()
    issue = _build_issue()

    class FakeLinear:
        async def fetch_issue_states_by_ids(self, issue_ids: list[str]):
            assert issue_ids == ["issue-1"]
            return [issue]

    workflow.linear = FakeLinear()  # type: ignore[assignment]
    refreshed = await workflow.refresh_issue("issue-1")
    assert refreshed is issue


@pytest.mark.asyncio
async def test_execution_workflow_validate_review_handoff_requires_existing_workspace(
    tmp_path: Path,
) -> None:
    workflow = _build_workflow()

    async def fake_run_command(*args, **kwargs):
        raise AssertionError("run_command should not be called")

    ok, detail = await workflow.validate_review_handoff(
        tmp_path / "missing",
        base_branch="main",
        run_command=fake_run_command,
    )
    assert ok is False
    assert "does not exist" in detail


@pytest.mark.asyncio
async def test_execution_workflow_validate_review_handoff_rejects_base_branch(
    tmp_path: Path,
) -> None:
    workflow = _build_workflow()
    tmp_path.mkdir(exist_ok=True)

    async def fake_run_command(workspace_path: Path, *args: str):
        if args[:3] == ("git", "branch", "--show-current"):
            return 0, "main\n", ""
        raise AssertionError(f"unexpected command: {args}")

    ok, detail = await workflow.validate_review_handoff(
        tmp_path,
        base_branch="main",
        run_command=fake_run_command,
    )
    assert ok is False
    assert "still on 'main'" in detail


@pytest.mark.asyncio
async def test_execution_workflow_validate_review_handoff_accepts_reviewable_pr(
    tmp_path: Path,
) -> None:
    workflow = _build_workflow()
    tmp_path.mkdir(exist_ok=True)

    async def fake_run_command(workspace_path: Path, *args: str):
        if args[:3] == ("git", "branch", "--show-current"):
            return 0, "agent/bap-204\n", ""
        if args[:3] == ("git", "status", "--porcelain"):
            return 0, "", ""
        if args[:3] == ("gh", "pr", "list"):
            return 0, json.dumps([{"url": "https://example.test/pr/1", "state": "OPEN"}]), ""
        raise AssertionError(f"unexpected command: {args}")

    ok, detail = await workflow.validate_review_handoff(
        tmp_path,
        base_branch="main",
        run_command=fake_run_command,
    )
    assert ok is True
    assert detail == "https://example.test/pr/1"
