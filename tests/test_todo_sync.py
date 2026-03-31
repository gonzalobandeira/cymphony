"""Tests for TodoWrite → Linear comment sync (BAP-70)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from cymphony.models import (
    AgentConfig,
    AgentEvent,
    AgentEventType,
    CodingAgentConfig,
    Issue,
    LiveSession,
    PollingConfig,
    PreflightConfig,
    RunningEntry,
    RunStatus,
    ServerConfig,
    ServiceConfig,
    TrackerConfig,
    WorkflowDefinition,
    WorkspaceConfig,
    HooksConfig,
)
from cymphony.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_orchestrator() -> Orchestrator:
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
        runner=CodingAgentConfig(
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
    )
    workflow = WorkflowDefinition(config={}, prompt_template="")
    return Orchestrator(Path("WORKFLOW.md"), config, workflow)


def _build_issue(issue_id: str = "issue-1") -> Issue:
    return Issue(
        id=issue_id,
        identifier="BAP-70",
        title="Test issue",
        project_name=None,
        description=None,
        priority=None,
        state="In Progress",
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
        session_id="sess-1",
        pid=1234,
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


def _build_entry(issue_id: str = "issue-1") -> RunningEntry:
    return RunningEntry(
        issue_id=issue_id,
        identifier="BAP-70",
        issue=_build_issue(issue_id),
        task=None,
        session=_build_session(),
        retry_attempt=None,
        started_at=datetime.now(timezone.utc),
    )


def _make_todowrite_event(todos: list[dict]) -> AgentEvent:
    """Build an AgentEvent whose raw payload contains a TodoWrite tool_use block."""
    return AgentEvent(
        event=AgentEventType.OTHER_MESSAGE,
        timestamp=datetime.now(timezone.utc),
        session_id="sess-1",
        raw={
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "TodoWrite",
                        "input": {"todos": todos},
                    }
                ]
            },
        },
    )


# ---------------------------------------------------------------------------
# _render_todo_checklist tests
# ---------------------------------------------------------------------------

class TestRenderTodoChecklist:
    def test_completed_items(self) -> None:
        orch = _build_orchestrator()
        todos = [{"content": "Set up project", "status": "completed"}]
        result = orch._render_todo_checklist(todos)
        assert "- [x] Set up project" in result

    def test_in_progress_items(self) -> None:
        orch = _build_orchestrator()
        todos = [{"content": "Write tests", "status": "in_progress"}]
        result = orch._render_todo_checklist(todos)
        assert "- [ ] 🔄 Write tests *(in progress)*" in result

    def test_pending_items(self) -> None:
        orch = _build_orchestrator()
        todos = [{"content": "Deploy", "status": "pending"}]
        result = orch._render_todo_checklist(todos)
        assert "- [ ] Deploy" in result
        assert "🔄" not in result
        assert "in progress" not in result

    def test_mixed_statuses(self) -> None:
        orch = _build_orchestrator()
        todos = [
            {"content": "Step 1", "status": "completed"},
            {"content": "Step 2", "status": "in_progress"},
            {"content": "Step 3", "status": "pending"},
        ]
        result = orch._render_todo_checklist(todos)
        lines = result.split("\n")
        # First line is the heading
        assert "**Agent Plan**" in lines[0]
        assert "- [x] Step 1" in result
        assert "- [ ] 🔄 Step 2 *(in progress)*" in result
        assert "- [ ] Step 3" in result

    def test_empty_todos(self) -> None:
        orch = _build_orchestrator()
        result = orch._render_todo_checklist([])
        assert "**Agent Plan**" in result

    def test_missing_status_defaults_to_pending(self) -> None:
        orch = _build_orchestrator()
        todos = [{"content": "Unknown status"}]
        result = orch._render_todo_checklist(todos)
        assert "- [ ] Unknown status" in result
        assert "[x]" not in result


# ---------------------------------------------------------------------------
# TodoWrite event detection + comment sync tests
# ---------------------------------------------------------------------------

class TestTodoWriteSync:
    @pytest.mark.asyncio
    async def test_first_todowrite_creates_comment(self) -> None:
        orch = _build_orchestrator()
        entry = _build_entry()
        assert entry.session.plan_comment_id is None

        todos = [{"content": "Do the thing", "status": "pending"}]
        event = _make_todowrite_event(todos)

        mock_create = AsyncMock(return_value="comment-abc")
        mock_update = AsyncMock(return_value=True)

        with (
            patch("cymphony.orchestrator.LinearClient") as MockClient,
        ):
            instance = MockClient.return_value
            instance.create_comment = mock_create
            instance.update_comment = mock_update

            await orch._handle_agent_event("issue-1", entry, event)
            # Let fire-and-forget task complete
            await asyncio.sleep(0.05)

        mock_create.assert_awaited_once()
        call_args = mock_create.call_args
        assert call_args[0][0] == "issue-1"
        assert "Do the thing" in call_args[0][1]
        mock_update.assert_not_awaited()
        assert entry.session.plan_comment_id == "comment-abc"

    @pytest.mark.asyncio
    async def test_subsequent_todowrite_updates_comment(self) -> None:
        orch = _build_orchestrator()
        entry = _build_entry()
        entry.session.plan_comment_id = "comment-existing"

        todos = [
            {"content": "Step 1", "status": "completed"},
            {"content": "Step 2", "status": "in_progress"},
        ]
        event = _make_todowrite_event(todos)

        mock_create = AsyncMock(return_value="comment-new")
        mock_update = AsyncMock(return_value=True)

        with patch("cymphony.orchestrator.LinearClient") as MockClient:
            instance = MockClient.return_value
            instance.create_comment = mock_create
            instance.update_comment = mock_update

            await orch._handle_agent_event("issue-1", entry, event)
            await asyncio.sleep(0.05)

        mock_update.assert_awaited_once()
        call_args = mock_update.call_args
        assert call_args[0][0] == "comment-existing"
        assert "Step 1" in call_args[0][1]
        mock_create.assert_not_awaited()
        # comment ID should not change
        assert entry.session.plan_comment_id == "comment-existing"

    @pytest.mark.asyncio
    async def test_no_comment_without_todowrite(self) -> None:
        orch = _build_orchestrator()
        entry = _build_entry()

        # An assistant event with no TodoWrite block
        event = AgentEvent(
            event=AgentEventType.OTHER_MESSAGE,
            timestamp=datetime.now(timezone.utc),
            session_id="sess-1",
            raw={
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Hello world"}
                    ]
                },
            },
        )

        with patch("cymphony.orchestrator.LinearClient") as MockClient:
            instance = MockClient.return_value
            instance.create_comment = AsyncMock()
            instance.update_comment = AsyncMock()

            await orch._handle_agent_event("issue-1", entry, event)
            await asyncio.sleep(0.05)

        instance.create_comment.assert_not_awaited()
        instance.update_comment.assert_not_awaited()
        assert entry.session.plan_comment_id is None

    @pytest.mark.asyncio
    async def test_non_assistant_event_ignored(self) -> None:
        orch = _build_orchestrator()
        entry = _build_entry()

        event = AgentEvent(
            event=AgentEventType.OTHER_MESSAGE,
            timestamp=datetime.now(timezone.utc),
            session_id="sess-1",
            raw={"type": "system", "data": "something"},
        )

        with patch("cymphony.orchestrator.LinearClient") as MockClient:
            instance = MockClient.return_value
            instance.create_comment = AsyncMock()
            instance.update_comment = AsyncMock()

            await orch._handle_agent_event("issue-1", entry, event)
            await asyncio.sleep(0.05)

        instance.create_comment.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_api_error_does_not_crash_worker(self) -> None:
        orch = _build_orchestrator()
        entry = _build_entry()

        todos = [{"content": "Exploding task", "status": "pending"}]
        event = _make_todowrite_event(todos)

        with patch("cymphony.orchestrator.LinearClient") as MockClient:
            instance = MockClient.return_value
            instance.create_comment = AsyncMock(side_effect=RuntimeError("API down"))

            # Should not raise
            await orch._handle_agent_event("issue-1", entry, event)
            await asyncio.sleep(0.05)

        # comment_id should remain None since create failed
        assert entry.session.plan_comment_id is None

    @pytest.mark.asyncio
    async def test_latest_plan_stored_on_session(self) -> None:
        orch = _build_orchestrator()
        entry = _build_entry()
        entry.session.plan_comment_id = "comment-x"

        todos = [{"content": "Important step", "status": "in_progress"}]
        event = _make_todowrite_event(todos)

        with patch("cymphony.orchestrator.LinearClient") as MockClient:
            instance = MockClient.return_value
            instance.update_comment = AsyncMock(return_value=True)

            await orch._handle_agent_event("issue-1", entry, event)
            await asyncio.sleep(0.05)

        assert entry.session.latest_plan is not None
        assert "Important step" in entry.session.latest_plan

    @pytest.mark.asyncio
    async def test_empty_todos_array_not_synced(self) -> None:
        orch = _build_orchestrator()
        entry = _build_entry()

        # TodoWrite with empty todos array
        event = _make_todowrite_event([])

        with patch("cymphony.orchestrator.LinearClient") as MockClient:
            instance = MockClient.return_value
            instance.create_comment = AsyncMock()

            await orch._handle_agent_event("issue-1", entry, event)
            await asyncio.sleep(0.05)

        instance.create_comment.assert_not_awaited()
