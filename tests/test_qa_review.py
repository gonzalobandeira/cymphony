"""Tests for the QA Review execution path (BAP-194).

Covers:
- ExecutionMode resolution based on issue state
- Review-mode dispatch (skips dispatch transition)
- Review prompt rendering in the worker
- Mode-aware success/failure transitions in _on_worker_done
- Mode field in snapshot output
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from cymphony.models import (
    AgentConfig,
    CodingAgentConfig,
    ExecutionMode,
    HooksConfig,
    Issue,
    LiveSession,
    PollingConfig,
    PreflightConfig,
    QAReviewConfig,
    RetryEntry,
    RunningEntry,
    RunStatus,
    ServerConfig,
    ServiceConfig,
    TrackerConfig,
    TransitionsConfig,
    WorkflowDefinition,
    WorkspaceConfig,
)
from cymphony.orchestrator import Orchestrator
from cymphony.review import REVIEW_RESULT_FILENAME
from cymphony.workflow import render_review_prompt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_config(
    qa_enabled: bool = True,
    active_states: list[str] | None = None,
) -> ServiceConfig:
    if active_states is None:
        active_states = ["Todo", "In Progress", "QA Review"]
    return ServiceConfig(
        tracker=TrackerConfig(
            kind="linear",
            endpoint="https://example.test/graphql",
            api_key="test-key",
            project_slug="proj",
            active_states=active_states,
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
        coding_agent=CodingAgentConfig(
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
                enabled=qa_enabled,
                dispatch="QA Review",
                success="In Review",
                failure="Todo",
            ),
        ),
    )


def _build_orchestrator(qa_enabled: bool = True) -> Orchestrator:
    config = _build_config(qa_enabled=qa_enabled)
    workflow = WorkflowDefinition(config={}, prompt_template="Build prompt for {{ issue.title }}")
    return Orchestrator(Path("WORKFLOW.md"), config, workflow)


def _build_issue(
    issue_id: str = "issue-1",
    identifier: str = "BAP-200",
    state: str = "Todo",
) -> Issue:
    return Issue(
        id=issue_id,
        identifier=identifier,
        title="Test issue",
        project_name=None,
        description="Some description",
        priority=2,
        state=state,
        branch_name=None,
        url=None,
        labels=[],
        blocked_by=[],
        comments=[],
        created_at=None,
        updated_at=None,
    )


def _build_session() -> LiveSession:
    return LiveSession(
        session_id=None,
        pid=None,
        last_event=None,
        last_event_timestamp=None,
        last_message=None,
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        last_reported_input_tokens=0,
        last_reported_output_tokens=0,
        last_reported_total_tokens=0,
        turn_count=0,
    )


def _build_running_entry(
    issue: Issue,
    mode: ExecutionMode = ExecutionMode.BUILD,
) -> RunningEntry:
    return RunningEntry(
        issue_id=issue.id,
        identifier=issue.identifier,
        issue=issue,
        task=None,
        session=_build_session(),
        retry_attempt=None,
        started_at=datetime.now(timezone.utc),
        mode=mode,
    )


def _write_review_result(orch: Orchestrator, identifier: str, decision: str, summary: str) -> None:
    workspace_dir = Path(orch._config.workspace.root) / identifier
    workspace_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / REVIEW_RESULT_FILENAME).write_text(
        json.dumps({"decision": decision, "summary": summary}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# _resolve_execution_mode
# ---------------------------------------------------------------------------

class TestResolveExecutionMode:
    def test_todo_issue_resolves_to_build(self) -> None:
        orch = _build_orchestrator()
        issue = _build_issue(state="Todo")
        assert orch._resolve_execution_mode(issue) == ExecutionMode.BUILD

    def test_in_progress_issue_resolves_to_build(self) -> None:
        orch = _build_orchestrator()
        issue = _build_issue(state="In Progress")
        assert orch._resolve_execution_mode(issue) == ExecutionMode.BUILD

    def test_qa_review_issue_resolves_to_review(self) -> None:
        orch = _build_orchestrator()
        issue = _build_issue(state="QA Review")
        assert orch._resolve_execution_mode(issue) == ExecutionMode.REVIEW

    def test_qa_review_case_insensitive(self) -> None:
        orch = _build_orchestrator()
        issue = _build_issue(state="qa review")
        assert orch._resolve_execution_mode(issue) == ExecutionMode.REVIEW

    def test_qa_review_disabled_resolves_to_build(self) -> None:
        orch = _build_orchestrator(qa_enabled=False)
        issue = _build_issue(state="QA Review")
        assert orch._resolve_execution_mode(issue) == ExecutionMode.BUILD


# ---------------------------------------------------------------------------
# Dispatch: mode is set on RunningEntry
# ---------------------------------------------------------------------------

class TestDispatchMode:
    @pytest.mark.asyncio
    async def test_dispatch_sets_review_mode_for_qa_review_issue(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch = _build_orchestrator()
        issue = _build_issue(state="QA Review")

        # Stub _worker so dispatch doesn't actually run an agent
        async def fake_worker(i, a, e):
            pass

        monkeypatch.setattr(orch, "_worker", fake_worker)
        monkeypatch.setattr(
            orch, "_transition_issue_state_background",
            lambda issue_id, state_name, **kwargs: None,
        )

        await orch._dispatch_issue(issue, attempt=None)

        entry = orch._state.running.get(issue.id)
        assert entry is not None
        assert entry.mode == ExecutionMode.REVIEW

    @pytest.mark.asyncio
    async def test_dispatch_sets_build_mode_for_todo_issue(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch = _build_orchestrator()
        issue = _build_issue(state="Todo")

        async def fake_worker(i, a, e):
            pass

        monkeypatch.setattr(orch, "_worker", fake_worker)
        transitions: list[tuple[str, str]] = []
        monkeypatch.setattr(
            orch, "_transition_issue_state_background",
            lambda issue_id, state_name, **kwargs: transitions.append((issue_id, state_name)),
        )

        await orch._dispatch_issue(issue, attempt=None)

        entry = orch._state.running.get(issue.id)
        assert entry is not None
        assert entry.mode == ExecutionMode.BUILD
        # Build mode issues get the dispatch transition
        assert transitions == [(issue.id, "In Progress")]

    @pytest.mark.asyncio
    async def test_dispatch_skips_transition_for_review_mode(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch = _build_orchestrator()
        issue = _build_issue(state="QA Review")

        async def fake_worker(i, a, e):
            pass

        monkeypatch.setattr(orch, "_worker", fake_worker)
        transitions: list[tuple[str, str]] = []
        monkeypatch.setattr(
            orch, "_transition_issue_state_background",
            lambda issue_id, state_name, **kwargs: transitions.append((issue_id, state_name)),
        )

        await orch._dispatch_issue(issue, attempt=None)

        # Review mode should NOT trigger dispatch transition
        assert transitions == []


# ---------------------------------------------------------------------------
# Worker exit: mode-aware transitions
# ---------------------------------------------------------------------------

class TestWorkerDoneTransitions:
    @pytest.mark.asyncio
    async def test_review_success_uses_qa_review_success_target(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch = _build_orchestrator()
        issue = _build_issue(state="QA Review")
        entry = _build_running_entry(issue, mode=ExecutionMode.REVIEW)
        orch._state.running[issue.id] = entry

        # Create a done task that succeeded
        async def noop():
            pass

        task = asyncio.ensure_future(noop())
        await task  # let it complete
        entry.task = task
        _write_review_result(orch, issue.identifier, "pass", "Looks good")

        transitions: list[tuple[str, str]] = []
        monkeypatch.setattr(
            orch, "_transition_issue_state",
            AsyncMock(side_effect=lambda iid, state, **kwargs: transitions.append((iid, state))),
        )
        monkeypatch.setattr(
            orch, "_schedule_retry",
            AsyncMock(),
        )

        await orch._on_worker_done(issue.id, issue.identifier, entry, task)

        # Should use qa_review.success = "In Review"
        assert transitions == [(issue.id, "In Review")]

    @pytest.mark.asyncio
    async def test_review_process_failure_uses_qa_review_failure_target(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch = _build_orchestrator()
        issue = _build_issue(state="QA Review")
        entry = _build_running_entry(issue, mode=ExecutionMode.REVIEW)
        orch._state.running[issue.id] = entry

        # Create a task that raises an exception
        async def failing():
            raise RuntimeError("agent crashed")

        task = asyncio.ensure_future(failing())
        try:
            await task
        except RuntimeError:
            pass
        entry.task = task

        transitions: list[tuple[str, str]] = []
        monkeypatch.setattr(
            orch, "_transition_issue_state_background",
            lambda iid, state, **kwargs: transitions.append((iid, state)),
        )
        monkeypatch.setattr(
            orch, "_schedule_retry",
            AsyncMock(),
        )

        await orch._on_worker_done(issue.id, issue.identifier, entry, task)

        # Should use qa_review.failure = "Todo"
        assert transitions == [(issue.id, "Todo")]

    @pytest.mark.asyncio
    async def test_review_changes_requested_uses_qa_review_failure_target(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch = _build_orchestrator()
        issue = _build_issue(state="QA Review")
        entry = _build_running_entry(issue, mode=ExecutionMode.REVIEW)
        orch._state.running[issue.id] = entry

        async def noop():
            pass

        task = asyncio.ensure_future(noop())
        await task
        entry.task = task
        _write_review_result(orch, issue.identifier, "changes_requested", "Needs tests")

        transitions: list[tuple[str, str]] = []
        monkeypatch.setattr(
            orch, "_transition_issue_state",
            AsyncMock(side_effect=lambda iid, state, **kwargs: transitions.append((iid, state))),
        )
        monkeypatch.setattr(
            orch, "_schedule_retry",
            AsyncMock(),
        )

        await orch._on_worker_done(issue.id, issue.identifier, entry, task)

        assert transitions == [(issue.id, "Todo")]

    @pytest.mark.asyncio
    async def test_review_missing_result_file_falls_back_to_failure_target(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch = _build_orchestrator()
        issue = _build_issue(state="QA Review", identifier="BAP-201")
        entry = _build_running_entry(issue, mode=ExecutionMode.REVIEW)
        orch._state.running[issue.id] = entry

        async def noop():
            pass

        task = asyncio.ensure_future(noop())
        await task
        entry.task = task

        transitions: list[tuple[str, str]] = []
        monkeypatch.setattr(
            orch, "_transition_issue_state",
            AsyncMock(side_effect=lambda iid, state, **kwargs: transitions.append((iid, state))),
        )
        monkeypatch.setattr(
            orch, "_schedule_retry",
            AsyncMock(),
        )

        await orch._on_worker_done(issue.id, issue.identifier, entry, task)

        assert transitions == [(issue.id, "Todo")]
        assert len(orch._state.recent_problems) == 1
        assert orch._state.recent_problems[0].kind == "qa_review_parse_error"

    @pytest.mark.asyncio
    async def test_build_success_uses_top_level_success_target(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch = _build_orchestrator()
        issue = _build_issue(state="In Progress")
        entry = _build_running_entry(issue, mode=ExecutionMode.BUILD)
        orch._state.running[issue.id] = entry

        async def noop():
            pass

        task = asyncio.ensure_future(noop())
        await task
        entry.task = task

        transitions: list[tuple[str, str]] = []
        monkeypatch.setattr(
            orch, "_transition_issue_state",
            AsyncMock(side_effect=lambda iid, state, **kwargs: transitions.append((iid, state))),
        )
        monkeypatch.setattr(
            orch, "_schedule_retry",
            AsyncMock(),
        )

        await orch._on_worker_done(issue.id, issue.identifier, entry, task)

        # Should use top-level success = "In Review"
        assert transitions == [(issue.id, "In Review")]


# ---------------------------------------------------------------------------
# Review prompt rendering
# ---------------------------------------------------------------------------

class TestRenderReviewPrompt:
    def test_default_review_prompt_contains_review_mode(self) -> None:
        workflow = WorkflowDefinition(config={}, prompt_template="")
        issue = _build_issue(state="QA Review")
        prompt = render_review_prompt(workflow, issue)
        assert "review mode" in prompt.lower()
        assert "Test issue" in prompt
        assert "REVIEW_RESULT.json" in prompt

    def test_custom_review_prompt_from_config(self) -> None:
        workflow = WorkflowDefinition(
            config={"review_prompt": "Custom review for {{ issue.title }}"},
            prompt_template="",
        )
        issue = _build_issue()
        prompt = render_review_prompt(workflow, issue)
        assert "Custom review for Test issue" in prompt
        assert "REVIEW_RESULT.json" in prompt


# ---------------------------------------------------------------------------
# Snapshot includes mode
# ---------------------------------------------------------------------------

class TestSnapshotMode:
    def test_running_entry_snapshot_includes_mode(self) -> None:
        orch = _build_orchestrator()
        issue = _build_issue(state="QA Review")
        entry = _build_running_entry(issue, mode=ExecutionMode.REVIEW)
        orch._state.running[issue.id] = entry

        snapshot = orch.snapshot()
        assert len(snapshot["running"]) == 1
        row = snapshot["running"][0]
        assert row["mode"] == "review"

    def test_running_entry_snapshot_build_mode(self) -> None:
        orch = _build_orchestrator()
        issue = _build_issue(state="In Progress")
        entry = _build_running_entry(issue, mode=ExecutionMode.BUILD)
        orch._state.running[issue.id] = entry

        snapshot = orch.snapshot()
        row = snapshot["running"][0]
        assert row["mode"] == "build"

    def test_retry_entry_snapshot_includes_mode(self) -> None:
        orch = _build_orchestrator()
        retry = RetryEntry(
            issue_id="issue-1",
            identifier="BAP-200",
            attempt=1,
            due_at_ms=0.0,
            error=None,
            mode="review",
        )
        orch._state.retry_attempts["issue-1"] = retry
        orch._state.claimed.add("issue-1")

        snapshot = orch.snapshot()
        assert len(snapshot["retrying"]) == 1
        row = snapshot["retrying"][0]
        assert row["mode"] == "review"


# ---------------------------------------------------------------------------
# ExecutionMode enum
# ---------------------------------------------------------------------------

class TestExecutionModeEnum:
    def test_build_value(self) -> None:
        assert ExecutionMode.BUILD.value == "build"

    def test_review_value(self) -> None:
        assert ExecutionMode.REVIEW.value == "review"

    def test_default_on_running_entry(self) -> None:
        issue = _build_issue()
        entry = RunningEntry(
            issue_id=issue.id,
            identifier=issue.identifier,
            issue=issue,
            task=None,
            session=_build_session(),
            retry_attempt=None,
            started_at=datetime.now(timezone.utc),
        )
        assert entry.mode == ExecutionMode.BUILD
