"""Tests for Codex planning, plan extraction, and plan syncing (BAP-201).

Covers:
  - Markdown checklist parsing (_parse_markdown_checklist)
  - Codex event → todos extraction (extract_plan_todos_from_codex_event)
  - Orchestrator plan sync for Codex events (create + update)
  - Plan prompt rendering for both providers
  - Worker continuation after planning turn (Codex)
  - Regression: Claude planning still works unchanged
"""

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
    BlockerRef,
    CodingAgentConfig,
    Comment,
    ExecutionMode,
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
from cymphony.runners import create_agent_runner
from cymphony.runners.codex import (
    extract_plan_todos_from_codex_event,
    _parse_markdown_checklist,
)
from cymphony.workflow import render_plan_prompt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_orchestrator(provider: str = "codex") -> Orchestrator:
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
            provider=provider,
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


def _build_session() -> LiveSession:
    return LiveSession(
        session_id="sess-codex",
        pid=5678,
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


def _build_issue(issue_id: str = "issue-codex") -> Issue:
    return Issue(
        id=issue_id,
        identifier="BAP-201",
        title="Test Codex issue",
        project_name=None,
        description="Make planning provider-agnostic",
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


def _build_entry(issue_id: str = "issue-codex") -> RunningEntry:
    return RunningEntry(
        issue_id=issue_id,
        identifier="BAP-201",
        issue=_build_issue(issue_id),
        task=None,
        session=_build_session(),
        retry_attempt=None,
        started_at=datetime.now(timezone.utc),
    )


def _make_codex_plan_event(text: str) -> AgentEvent:
    """Build an AgentEvent with a Codex item.completed agent_message containing plan text."""
    return AgentEvent(
        event=AgentEventType.NOTIFICATION,
        timestamp=datetime.now(timezone.utc),
        session_id="sess-codex",
        raw={
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": text,
            },
        },
    )


# ---------------------------------------------------------------------------
# _parse_markdown_checklist tests
# ---------------------------------------------------------------------------

class TestParseMarkdownChecklist:
    def test_basic_pending_items(self) -> None:
        text = "- [ ] Step one\n- [ ] Step two\n- [ ] Step three"
        result = _parse_markdown_checklist(text)
        assert result is not None
        assert len(result) == 3
        assert all(t["status"] == "pending" for t in result)
        assert result[0]["content"] == "Step one"
        assert result[2]["content"] == "Step three"

    def test_completed_items(self) -> None:
        text = "- [x] Done task\n- [X] Also done"
        result = _parse_markdown_checklist(text)
        assert result is not None
        assert len(result) == 2
        assert all(t["status"] == "completed" for t in result)

    def test_in_progress_items(self) -> None:
        text = "- [ ] 🔄 Running task *(in progress)*"
        result = _parse_markdown_checklist(text)
        assert result is not None
        assert len(result) == 1
        assert result[0]["status"] == "in_progress"
        assert result[0]["content"] == "Running task"

    def test_mixed_statuses(self) -> None:
        text = (
            "- [x] Read the code\n"
            "- [ ] 🔄 Implement changes *(in progress)*\n"
            "- [ ] Write tests\n"
        )
        result = _parse_markdown_checklist(text)
        assert result is not None
        assert len(result) == 3
        assert result[0]["status"] == "completed"
        assert result[1]["status"] == "in_progress"
        assert result[2]["status"] == "pending"

    def test_asterisk_bullets(self) -> None:
        text = "* [ ] Using asterisk"
        result = _parse_markdown_checklist(text)
        assert result is not None
        assert len(result) == 1
        assert result[0]["content"] == "Using asterisk"

    def test_no_checklist_returns_none(self) -> None:
        text = "This is just regular text without any checklist."
        assert _parse_markdown_checklist(text) is None

    def test_empty_string_returns_none(self) -> None:
        assert _parse_markdown_checklist("") is None

    def test_checklist_with_surrounding_text(self) -> None:
        text = "Here is my plan:\n\n- [ ] First\n- [ ] Second\n\nDone!"
        result = _parse_markdown_checklist(text)
        assert result is not None
        assert len(result) == 2


# ---------------------------------------------------------------------------
# extract_plan_todos_from_codex_event tests
# ---------------------------------------------------------------------------

class TestExtractPlanTodosFromCodexEvent:
    def test_valid_agent_message_with_checklist(self) -> None:
        raw = {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "- [ ] Step A\n- [ ] Step B",
            },
        }
        result = extract_plan_todos_from_codex_event(raw)
        assert result is not None
        assert len(result) == 2

    def test_non_item_completed_returns_none(self) -> None:
        raw = {"type": "turn.completed", "item": {"type": "agent_message", "text": "- [ ] X"}}
        assert extract_plan_todos_from_codex_event(raw) is None

    def test_non_agent_message_returns_none(self) -> None:
        raw = {"type": "item.completed", "item": {"type": "command_execution", "text": "- [ ] X"}}
        assert extract_plan_todos_from_codex_event(raw) is None

    def test_agent_message_without_checklist_returns_none(self) -> None:
        raw = {"type": "item.completed", "item": {"type": "agent_message", "text": "No plan here."}}
        assert extract_plan_todos_from_codex_event(raw) is None

    def test_missing_item_returns_none(self) -> None:
        raw = {"type": "item.completed"}
        assert extract_plan_todos_from_codex_event(raw) is None


# ---------------------------------------------------------------------------
# render_plan_prompt provider-aware tests
# ---------------------------------------------------------------------------

class TestRenderPlanPrompt:
    def test_claude_prompt_mentions_todowrite(self) -> None:
        workflow = WorkflowDefinition(config={}, prompt_template="")
        issue = _build_issue()
        prompt = render_plan_prompt(workflow, issue, provider="claude")
        assert "TodoWrite" in prompt
        assert "markdown checklist" not in prompt.lower()

    def test_codex_prompt_mentions_markdown_checklist(self) -> None:
        workflow = WorkflowDefinition(config={}, prompt_template="")
        issue = _build_issue()
        prompt = render_plan_prompt(workflow, issue, provider="codex")
        assert "markdown checklist" in prompt.lower()
        assert "TodoWrite" not in prompt

    def test_default_provider_is_claude(self) -> None:
        workflow = WorkflowDefinition(config={}, prompt_template="")
        issue = _build_issue()
        prompt = render_plan_prompt(workflow, issue)
        assert "TodoWrite" in prompt

    def test_issue_details_rendered(self) -> None:
        workflow = WorkflowDefinition(config={}, prompt_template="")
        issue = _build_issue()
        for provider in ("claude", "codex"):
            prompt = render_plan_prompt(workflow, issue, provider=provider)
            assert "Test Codex issue" in prompt
            assert "Make planning provider-agnostic" in prompt
            assert "BAP-201" in prompt
            assert "In Progress" in prompt

    def test_labels_rendered(self) -> None:
        workflow = WorkflowDefinition(config={}, prompt_template="")
        issue = _build_issue()
        issue.labels = ["improvement", "backend"]
        prompt = render_plan_prompt(workflow, issue, provider="codex")
        assert "improvement" in prompt
        assert "backend" in prompt

    def test_blocked_by_rendered(self) -> None:
        workflow = WorkflowDefinition(config={}, prompt_template="")
        issue = _build_issue()
        issue.blocked_by = [BlockerRef(id="b1", identifier="BAP-100", state="Done")]
        prompt = render_plan_prompt(workflow, issue, provider="codex")
        assert "BAP-100" in prompt
        assert "Done" in prompt

    def test_comments_rendered(self) -> None:
        workflow = WorkflowDefinition(config={}, prompt_template="")
        issue = _build_issue()
        issue.comments = [Comment(author="Alice", body="Check the API first", created_at="2026-03-31")]
        prompt = render_plan_prompt(workflow, issue, provider="codex")
        assert "Alice" in prompt
        assert "Check the API first" in prompt


# ---------------------------------------------------------------------------
# Orchestrator plan sync for Codex events
# ---------------------------------------------------------------------------

class TestCodexPlanSync:
    @pytest.mark.asyncio
    async def test_codex_plan_creates_comment(self) -> None:
        orch = _build_orchestrator(provider="codex")
        entry = _build_entry()
        assert entry.session.plan_comment_id is None

        event = _make_codex_plan_event("- [ ] Implement feature\n- [ ] Write tests")

        with patch("cymphony.orchestrator.LinearClient") as MockClient:
            instance = MockClient.return_value
            instance.create_comment = AsyncMock(return_value="comment-codex-1")
            instance.update_comment = AsyncMock(return_value=True)

            await orch._handle_agent_event("issue-codex", entry, event)
            await asyncio.sleep(0.05)

        instance.create_comment.assert_awaited_once()
        body = instance.create_comment.call_args[0][1]
        assert "Implement feature" in body
        assert "Write tests" in body
        assert entry.session.plan_comment_id == "comment-codex-1"

    @pytest.mark.asyncio
    async def test_codex_plan_updates_existing_comment(self) -> None:
        orch = _build_orchestrator(provider="codex")
        entry = _build_entry()
        entry.session.plan_comment_id = "comment-existing"

        event = _make_codex_plan_event("- [x] Implement feature\n- [ ] Write tests")

        with patch("cymphony.orchestrator.LinearClient") as MockClient:
            instance = MockClient.return_value
            instance.create_comment = AsyncMock()
            instance.update_comment = AsyncMock(return_value=True)

            await orch._handle_agent_event("issue-codex", entry, event)
            await asyncio.sleep(0.05)

        instance.update_comment.assert_awaited_once()
        instance.create_comment.assert_not_awaited()
        assert entry.session.plan_comment_id == "comment-existing"

    @pytest.mark.asyncio
    async def test_codex_plan_stores_latest_plan(self) -> None:
        orch = _build_orchestrator(provider="codex")
        entry = _build_entry()
        entry.session.plan_comment_id = "comment-x"

        event = _make_codex_plan_event("- [ ] Important step")

        with patch("cymphony.orchestrator.LinearClient") as MockClient:
            instance = MockClient.return_value
            instance.update_comment = AsyncMock(return_value=True)

            await orch._handle_agent_event("issue-codex", entry, event)
            await asyncio.sleep(0.05)

        assert entry.session.latest_plan is not None
        assert "Important step" in entry.session.latest_plan

    @pytest.mark.asyncio
    async def test_codex_non_plan_message_not_synced(self) -> None:
        orch = _build_orchestrator(provider="codex")
        entry = _build_entry()

        event = _make_codex_plan_event("I'm going to start working on the task now.")

        with patch("cymphony.orchestrator.LinearClient") as MockClient:
            instance = MockClient.return_value
            instance.create_comment = AsyncMock()

            await orch._handle_agent_event("issue-codex", entry, event)
            await asyncio.sleep(0.05)

        instance.create_comment.assert_not_awaited()
        assert entry.session.plan_comment_id is None

    @pytest.mark.asyncio
    async def test_codex_concurrent_plan_events_no_duplicate_comment(self) -> None:
        orch = _build_orchestrator(provider="codex")
        entry = _build_entry()

        first_event = _make_codex_plan_event("- [ ] Step 1")
        second_event = _make_codex_plan_event("- [x] Step 1")

        create_started = asyncio.Event()
        allow_finish = asyncio.Event()

        async def _slow_create(issue_id: str, body: str) -> str:
            create_started.set()
            await allow_finish.wait()
            return "comment-new"

        with patch("cymphony.orchestrator.LinearClient") as MockClient:
            instance = MockClient.return_value
            instance.create_comment = AsyncMock(side_effect=_slow_create)
            instance.update_comment = AsyncMock(return_value=True)

            await orch._handle_agent_event("issue-codex", entry, first_event)
            await create_started.wait()

            await orch._handle_agent_event("issue-codex", entry, second_event)
            allow_finish.set()
            await asyncio.sleep(0.05)

        instance.create_comment.assert_awaited_once()
        instance.update_comment.assert_awaited_once()
        assert entry.session.plan_comment_id == "comment-new"


# ---------------------------------------------------------------------------
# Regression: Claude planning still works
# ---------------------------------------------------------------------------

class TestClaudePlanSyncRegression:
    @pytest.mark.asyncio
    async def test_claude_todowrite_still_creates_comment(self) -> None:
        orch = _build_orchestrator(provider="claude")
        entry = _build_entry()

        event = AgentEvent(
            event=AgentEventType.NOTIFICATION,
            timestamp=datetime.now(timezone.utc),
            session_id="sess-1",
            raw={
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "TodoWrite",
                            "input": {
                                "todos": [
                                    {"content": "Claude step", "status": "pending"},
                                ]
                            },
                        }
                    ]
                },
            },
        )

        with patch("cymphony.orchestrator.LinearClient") as MockClient:
            instance = MockClient.return_value
            instance.create_comment = AsyncMock(return_value="comment-claude")

            await orch._handle_agent_event("issue-codex", entry, event)
            await asyncio.sleep(0.05)

        instance.create_comment.assert_awaited_once()
        body = instance.create_comment.call_args[0][1]
        assert "Claude step" in body
        assert entry.session.plan_comment_id == "comment-claude"


# ---------------------------------------------------------------------------
# Worker continuation: planning turn → execution turns (Codex)
# ---------------------------------------------------------------------------

class TestCodexWorkerContinuation:
    """Verify that a Codex worker runs a planning turn followed by execution turns."""

    @staticmethod
    def _patch_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
        """Stub workspace creation and hooks."""
        from cymphony.models import Workspace

        async def fake_create_for_issue(self, identifier):
            return Workspace(path="/tmp/fake-codex", workspace_key="fake", created_now=True)

        async def fake_hook(self, workspace):
            pass

        monkeypatch.setattr(
            "cymphony.workspace.WorkspaceManager.create_for_issue",
            fake_create_for_issue,
        )
        monkeypatch.setattr(
            "cymphony.workspace.WorkspaceManager.run_before_run_hook",
            fake_hook,
        )
        monkeypatch.setattr(
            "cymphony.workspace.WorkspaceManager.run_after_run_hook",
            fake_hook,
        )

    @staticmethod
    def _patch_linear_terminal(monkeypatch: pytest.MonkeyPatch, issue: Issue) -> None:
        """After the execution turn, report the issue as terminal so the loop exits."""
        async def fake_fetch_issue_states(self, ids):
            return [Issue(
                id=issue.id, identifier=issue.identifier,
                title=issue.title, project_name=None,
                description=issue.description, priority=None,
                state="Done",
                branch_name=None, url=None,
                labels=[], blocked_by=[], comments=[],
                created_at=None, updated_at=None,
            )]

        monkeypatch.setattr(
            "cymphony.linear.LinearClient.fetch_issue_states_by_ids",
            fake_fetch_issue_states,
        )

    @pytest.mark.asyncio
    async def test_codex_worker_runs_plan_then_execution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The worker should call run_turn twice: once for planning, once for execution."""
        orch = _build_orchestrator(provider="codex")
        orch._config.agent.max_turns = 3  # planning + at least one execution
        issue = _build_issue()

        prompts_received: list[str] = []
        call_count = 0

        original_run_turn = AsyncMock(return_value="session-1")

        async def tracking_run_turn(**kwargs) -> str:
            nonlocal call_count
            call_count += 1
            prompts_received.append(kwargs.get("prompt", ""))
            return f"session-{call_count}"

        mock_runner = AsyncMock()
        mock_runner.run_turn = tracking_run_turn

        monkeypatch.setattr("cymphony.orchestrator.create_agent_runner", lambda p, c: mock_runner)

        self._patch_workspace(monkeypatch)
        self._patch_linear_terminal(monkeypatch, issue)

        entry = _build_entry()
        try:
            await orch._worker(issue, attempt=None, entry=entry)
        except Exception:
            pass

        # Planning turn + at least one execution turn
        assert call_count >= 2, f"Expected ≥2 run_turn calls, got {call_count}"

        # First call should be the planning prompt (Codex-style)
        assert "markdown checklist" in prompts_received[0].lower()
        assert "TodoWrite" not in prompts_received[0]

        # Second call should be the execution prompt (not the plan prompt)
        assert "markdown checklist" not in prompts_received[1].lower()

    @pytest.mark.asyncio
    async def test_codex_planning_turn_sets_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The entry status should transition through PLANNING during the plan turn."""
        orch = _build_orchestrator(provider="codex")
        orch._config.agent.max_turns = 3
        issue = _build_issue()
        statuses_seen: list[RunStatus] = []

        entry = _build_entry()

        async def tracking_run_turn(**kwargs) -> str:
            statuses_seen.append(entry.status)
            return "session-1"

        mock_runner = AsyncMock()
        mock_runner.run_turn = tracking_run_turn
        monkeypatch.setattr("cymphony.orchestrator.create_agent_runner", lambda p, c: mock_runner)

        self._patch_workspace(monkeypatch)
        self._patch_linear_terminal(monkeypatch, issue)

        try:
            await orch._worker(issue, attempt=None, entry=entry)
        except Exception:
            pass

        # First run_turn call should have PLANNING status
        assert RunStatus.PLANNING in statuses_seen
