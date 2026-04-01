from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from cymphony.models import (
    HooksConfig,
    Issue,
    PollingConfig,
    PreflightConfig,
    QAReviewConfig,
    ReviewDecision,
    ReviewResult,
    ServerConfig,
    ServiceConfig,
    TrackerConfig,
    TransitionsConfig,
    WorkspaceConfig,
    AgentConfig,
    CodingAgentConfig,
)
from cymphony.services.linear_service import LinearService
from cymphony.services.workspace_service import WorkspaceService
from cymphony.workflows.qa_review import QAReviewWorkflow


def _build_issue() -> Issue:
    return Issue(
        id="issue-1",
        identifier="BAP-204",
        title="Refactor runtime",
        project_name=None,
        description="Do the refactor.",
        priority=2,
        state="QA Review",
        branch_name=None,
        url=None,
        labels=["cymphony"],
        blocked_by=[],
        comments=[],
        created_at=None,
        updated_at=None,
    )


def _build_config() -> ServiceConfig:
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
            failure=None,
            qa_review=QAReviewConfig(
                enabled=True,
                dispatch="QA Review",
                success="In Review",
                failure="To Do",
            ),
        ),
    )


def _build_workflow() -> QAReviewWorkflow:
    linear = LinearService(_build_config().tracker)
    workspaces = WorkspaceService(WorkspaceConfig(root="/tmp/cymphony-tests"))
    return QAReviewWorkflow(linear=linear, workspaces=workspaces)


def test_qa_workflow_uses_issue_scoped_root() -> None:
    workflow = _build_workflow()
    assert workflow.workspace_root_for(_build_issue()) == "/tmp/cymphony-tests/qa/BAP-204"


def test_qa_workflow_uses_canonical_issue_branch_name() -> None:
    workflow = _build_workflow()
    assert workflow.review_branch_name(_build_issue()) == "agent/bap-204"


def test_qa_workflow_uses_dedicated_qa_agent_when_configured() -> None:
    workflow = _build_workflow()
    config = _build_config()
    qa_agent = CodingAgentConfig(
        command="review-cli",
        turn_timeout_ms=200,
        read_timeout_ms=200,
        stall_timeout_ms=200,
        dangerously_skip_permissions=False,
        provider="codex",
    )
    config.transitions.qa_review.agent = qa_agent
    provider, runner_config = workflow.resolve_agent_runner(config)
    assert provider == "codex"
    assert runner_config is qa_agent


def test_qa_workflow_falls_back_to_main_agent_runner_when_no_qa_agent_is_configured() -> None:
    workflow = _build_workflow()
    config = _build_config()
    provider, runner_config = workflow.resolve_agent_runner(config)
    assert provider == config.agent.provider
    assert runner_config is config.runner


def test_qa_workflow_does_not_require_planning() -> None:
    workflow = _build_workflow()
    assert workflow.requires_planning() is False


def test_qa_workflow_workspace_paths_are_fresh_per_run() -> None:
    workflow = _build_workflow()
    issue = _build_issue()
    first = workflow.workspace_path_for(issue, run_id="run-a")
    second = workflow.workspace_path_for(issue, run_id="run-b")
    assert str(first) == "/tmp/cymphony-tests/qa/BAP-204/run-a"
    assert str(second) == "/tmp/cymphony-tests/qa/BAP-204/run-b"
    assert first != second


def test_qa_workflow_renders_review_prompt() -> None:
    from cymphony.models import WorkflowDefinition

    workflow = _build_workflow()
    rendered = workflow.build_review_prompt(
        WorkflowDefinition(config={}, prompt_template="unused"),
        _build_issue(),
    )
    assert "review mode" in rendered.lower()
    assert "REVIEW_RESULT.json" in rendered


def test_qa_workflow_resolves_pass_target() -> None:
    workflow = _build_workflow()
    target = workflow.resolve_decision_target(
        _build_config(),
        ReviewResult(decision=ReviewDecision.PASS, summary="Looks good"),
    )
    assert target == "In Review"


def test_qa_workflow_resolves_changes_requested_target() -> None:
    workflow = _build_workflow()
    target = workflow.resolve_decision_target(
        _build_config(),
        ReviewResult(decision=ReviewDecision.CHANGES_REQUESTED, summary="Needs tests"),
    )
    assert target == "To Do"


def test_qa_workflow_resolves_no_target_for_invalid_review_result() -> None:
    workflow = _build_workflow()
    target = workflow.resolve_decision_target(
        _build_config(),
        ReviewResult(decision=None, error="bad result"),
    )
    assert target is None


def test_qa_workflow_completion_outcome_clears_bounces_on_pass_after_transition() -> None:
    workflow = _build_workflow()
    outcome = workflow.resolve_completion_outcome(
        _build_config(),
        ReviewResult(decision=ReviewDecision.PASS, summary="Looks good"),
        transition_succeeded=True,
        current_bounce_count=2,
    )
    assert outcome.target == "In Review"
    assert outcome.clear_bounces is True
    assert outcome.increment_bounce is False
    assert outcome.hold_for_manual_intervention is False


def test_qa_workflow_completion_outcome_increments_bounce_on_changes_requested() -> None:
    workflow = _build_workflow()
    outcome = workflow.resolve_completion_outcome(
        _build_config(),
        ReviewResult(
            decision=ReviewDecision.CHANGES_REQUESTED,
            summary="Needs tests",
        ),
        transition_succeeded=True,
        current_bounce_count=0,
    )
    assert outcome.target == "To Do"
    assert outcome.clear_bounces is False
    assert outcome.increment_bounce is True
    assert outcome.hold_for_manual_intervention is False


def test_qa_workflow_completion_outcome_holds_when_bounce_limit_exceeded() -> None:
    workflow = _build_workflow()
    config = _build_config()
    config.transitions.qa_review.max_bounces = 1
    outcome = workflow.resolve_completion_outcome(
        config,
        ReviewResult(
            decision=ReviewDecision.CHANGES_REQUESTED,
            summary="Still broken",
        ),
        transition_succeeded=True,
        current_bounce_count=1,
    )
    assert outcome.increment_bounce is True
    assert outcome.hold_for_manual_intervention is True


def test_qa_workflow_retry_outcome_retries_while_under_limit() -> None:
    workflow = _build_workflow()
    outcome = workflow.resolve_retry_outcome(
        _build_config(),
        attempt=1,
        error="review agent crashed",
    )
    assert outcome.hold_for_manual_intervention is False
    assert outcome.attempt == 1
    assert outcome.error == "review agent crashed"


def test_qa_workflow_retry_outcome_holds_when_retry_limit_exceeded() -> None:
    workflow = _build_workflow()
    config = _build_config()
    config.transitions.qa_review.max_retries = 1
    outcome = workflow.resolve_retry_outcome(
        config,
        attempt=2,
        error="review agent crashed",
    )
    assert outcome.hold_for_manual_intervention is True
    assert outcome.reason == "qa_review_retry_limit"
    assert outcome.summary == "QA review retry limit reached"
    assert "holding issue in QA review" in (outcome.detail or "")


def test_qa_workflow_bounce_limit_hold_outcome_has_manual_reason() -> None:
    workflow = _build_workflow()
    outcome = workflow.resolve_bounce_limit_hold_outcome(bounce_count=3)
    assert outcome.summary == "QA review re-entry limit reached"
    assert outcome.reason == "qa_review_bounce_limit"
    assert "Reached 3 QA review bounces" in outcome.detail


def test_qa_workflow_build_turn_prompt_uses_initial_prompt_on_first_turn() -> None:
    from cymphony.models import WorkflowDefinition

    workflow = _build_workflow()
    rendered = workflow.build_turn_prompt(
        WorkflowDefinition(config={}, prompt_template="unused"),
        _build_issue(),
        attempt=3,
        first_turn=True,
    )
    assert "REVIEW_RESULT.json" in rendered


def test_qa_workflow_build_turn_prompt_uses_review_continuation_after_first_turn() -> None:
    from cymphony.models import WorkflowDefinition

    workflow = _build_workflow()
    rendered = workflow.build_turn_prompt(
        WorkflowDefinition(config={}, prompt_template="unused"),
        _build_issue(),
        attempt=None,
        first_turn=False,
    )
    assert "CYMPHONY_REVIEW_COMPLETE" in rendered


def test_qa_workflow_stops_when_issue_inactive() -> None:
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


def test_qa_workflow_stops_at_max_turns() -> None:
    workflow = _build_workflow()
    should_continue, reason = workflow.should_continue(
        _build_issue(),
        active_states=["To Do", "In Progress", "QA Review"],
        turn_number=5,
        max_turns=5,
    )
    assert should_continue is False
    assert reason == "max_turns"


def test_qa_workflow_continues_when_issue_active_and_under_limit() -> None:
    workflow = _build_workflow()
    should_continue, reason = workflow.should_continue(
        _build_issue(),
        active_states=["To Do", "In Progress", "QA Review"],
        turn_number=2,
        max_turns=5,
        workspace_path=None,
    )
    assert should_continue is True
    assert reason is None


def test_qa_workflow_stops_when_review_result_exists(tmp_path: Path) -> None:
    workflow = _build_workflow()
    workspace_path = tmp_path / "qa-run"
    workspace_path.mkdir()
    (workspace_path / "REVIEW_RESULT.json").write_text(
        json.dumps({"decision": "pass"}),
        encoding="utf-8",
    )
    should_continue, reason = workflow.should_continue(
        _build_issue(),
        active_states=["To Do", "In Progress", "QA Review"],
        turn_number=2,
        max_turns=5,
        workspace_path=str(workspace_path),
    )
    assert should_continue is False
    assert reason == "review_result_ready"


@pytest.mark.asyncio
async def test_qa_workflow_prepare_workspace_uses_fresh_run_key() -> None:
    workflow = _build_workflow()
    config = _build_config()
    issue = _build_issue()
    manager, workspace = await workflow.prepare_workspace(config, issue, run_id="run-123")
    assert workspace.workspace_key == "run-123"
    assert workspace.path.endswith("/qa/BAP-204/run-123")
    assert getattr(manager, "_root") == "/tmp/cymphony-tests/qa/BAP-204"


@pytest.mark.asyncio
async def test_qa_workflow_prepare_workspace_run_returns_manager_and_workspace() -> None:
    with TemporaryDirectory() as tmp:
        config = _build_config()
        config.workspace = WorkspaceConfig(root=tmp)
        workflow = QAReviewWorkflow(
            linear=LinearService(config.tracker),
            workspaces=WorkspaceService(config.workspace),
        )
        manager, workspace = await workflow.prepare_workspace_run(config, _build_issue())
        assert Path(workspace.path).exists()
        assert str(Path(workspace.path)).startswith(str((Path(tmp) / "qa" / "BAP-204").resolve()))
        assert manager is not None


@pytest.mark.asyncio
async def test_qa_workflow_prepare_run_uses_before_run_hook_and_checks_out_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _build_workflow()
    issue = _build_issue()
    issue.branch_name = "agent/bap-204"
    workspace = type("Workspace", (), {"path": "/tmp/ws/BAP-204"})()
    calls: list[tuple[str, str]] = []

    class FakeManager:
        async def run_before_run_hook(self, ws):
            calls.append(("before_run", ws.path))

    async def fake_checkout(workspace_path: str, review_issue: Issue) -> None:
        calls.append(("checkout", workspace_path))
        assert review_issue is issue

    monkeypatch.setattr(workflow, "_checkout_review_branch", fake_checkout)

    await workflow.prepare_run(FakeManager(), workspace, issue)

    assert calls == [
        ("before_run", "/tmp/ws/BAP-204"),
        ("checkout", "/tmp/ws/BAP-204"),
    ]


@pytest.mark.asyncio
async def test_qa_workflow_refresh_issue_returns_first_issue() -> None:
    workflow = _build_workflow()
    issue = _build_issue()

    class FakeLinear:
        async def fetch_issue_states_by_ids(self, issue_ids: list[str]):
            assert issue_ids == ["issue-1"]
            return [issue]

    workflow.linear = FakeLinear()  # type: ignore[assignment]
    refreshed = await workflow.refresh_issue("issue-1")
    assert refreshed is issue


def test_qa_workflow_load_review_result_from_workspace(tmp_path: Path) -> None:
    workflow = _build_workflow()
    result_path = tmp_path / "REVIEW_RESULT.json"
    result_path.write_text(
        json.dumps({"decision": "pass", "summary": "Looks good"}),
        encoding="utf-8",
    )
    result = workflow.load_review_result(str(tmp_path))
    assert result.decision is not None
    assert result.decision.value == "pass"
    assert result.summary == "Looks good"


def test_qa_workflow_capture_run_result_matches_loaded_result(tmp_path: Path) -> None:
    workflow = _build_workflow()
    result_path = tmp_path / "REVIEW_RESULT.json"
    result_path.write_text(
        json.dumps({"decision": "pass", "summary": "Looks good"}),
        encoding="utf-8",
    )
    result = workflow.capture_run_result(str(tmp_path))
    assert result.decision is not None
    assert result.decision.value == "pass"


@pytest.mark.asyncio
async def test_qa_workflow_publish_review_result_comment_creates_when_missing() -> None:
    workflow = _build_workflow()
    calls: list[tuple[str, str]] = []

    class FakeLinear:
        async def create_comment(self, issue_id: str, body: str) -> str:
            calls.append((issue_id, body))
            return "comment-1"

        async def update_comment(self, comment_id: str, body: str) -> bool:
            raise AssertionError("update should not be called")

    workflow.linear = FakeLinear()  # type: ignore[assignment]
    comment_id, created = await workflow.publish_review_result_comment(
        "issue-1",
        result=ReviewResult(decision=ReviewDecision.PASS, summary="Looks good"),
    )
    assert comment_id == "comment-1"
    assert created is True
    assert calls and calls[0][0] == "issue-1"
    assert "**QA review passed**" in calls[0][1]


@pytest.mark.asyncio
async def test_qa_workflow_publish_review_result_comment_updates_existing_when_possible() -> None:
    workflow = _build_workflow()
    updated: list[tuple[str, str]] = []

    class FakeLinear:
        async def create_comment(self, issue_id: str, body: str) -> str:
            raise AssertionError("create should not be called")

        async def update_comment(self, comment_id: str, body: str) -> bool:
            updated.append((comment_id, body))
            return True

    workflow.linear = FakeLinear()  # type: ignore[assignment]
    comment_id, created = await workflow.publish_review_result_comment(
        "issue-1",
        result=ReviewResult(
            decision=ReviewDecision.CHANGES_REQUESTED,
            summary="Needs tests",
        ),
        existing_comment_id="comment-9",
    )
    assert comment_id == "comment-9"
    assert created is False
    assert updated == [("comment-9", updated[0][1])]
    assert "**QA review requested changes**" in updated[0][1]


@pytest.mark.asyncio
async def test_qa_workflow_publish_review_result_comment_falls_back_to_create() -> None:
    workflow = _build_workflow()
    created_calls: list[tuple[str, str]] = []
    updated_calls: list[tuple[str, str]] = []

    class FakeLinear:
        async def create_comment(self, issue_id: str, body: str) -> str:
            created_calls.append((issue_id, body))
            return "comment-new"

        async def update_comment(self, comment_id: str, body: str) -> bool:
            updated_calls.append((comment_id, body))
            return False

    workflow.linear = FakeLinear()  # type: ignore[assignment]
    comment_id, created = await workflow.publish_review_result_comment(
        "issue-1",
        result=ReviewResult(decision=ReviewDecision.PASS, summary="OK"),
        existing_comment_id="comment-old",
    )
    assert updated_calls == [("comment-old", updated_calls[0][1])]
    assert created_calls == [("issue-1", created_calls[0][1])]
    assert comment_id == "comment-new"
    assert created is True
