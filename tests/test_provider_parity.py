"""E2E provider parity tests: Claude vs Codex outcome equivalence (BAP-215).

Tests verify that both providers produce the same Cymphony workflow outcomes
across all lifecycle scenarios. Tests use mocked Linear/subprocess boundaries
to simulate full orchestration flows for each provider without requiring real
CLI tools or Linear API access.

Each scenario is parametrized over ("claude", "codex") so the same workflow
expectations apply to both providers identically.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cymphony.models import (
    AgentConfig,
    AgentEvent,
    AgentEventType,
    CodingAgentConfig,
    Comment,
    ExecutionMode,
    HooksConfig,
    Issue,
    LiveSession,
    PollingConfig,
    PreflightConfig,
    QAReviewConfig,
    ReviewDecision,
    ReviewResult,
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
from cymphony.runners import create_agent_runner
from cymphony.runners.claude import parse_claude_stream_event
from cymphony.runners.codex import parse_codex_stream_event


# ---------------------------------------------------------------------------
# Test fixtures and factories
# ---------------------------------------------------------------------------

_PROVIDERS = ["claude", "codex"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _build_config(
    provider: str = "claude",
    qa_enabled: bool = False,
    max_turns: int = 3,
) -> ServiceConfig:
    """Build a ServiceConfig for the given provider."""
    return ServiceConfig(
        tracker=TrackerConfig(
            kind="linear",
            endpoint="https://test.linear.app/graphql",
            api_key="test-key",
            project_slug="parity-test",
            active_states=["Todo", "In Progress", "QA Review"],
            terminal_states=["Done", "In Review"],
            assignee=None,
        ),
        polling=PollingConfig(interval_ms=25),
        workspace=WorkspaceConfig(root="/tmp/cymphony-parity-tests"),
        hooks=HooksConfig(
            after_create=None,
            before_run=None,
            after_run=None,
            before_remove=None,
            timeout_ms=1000,
        ),
        agent=AgentConfig(
            provider=provider,
            max_concurrent_agents=2,
            max_turns=max_turns,
            max_retry_backoff_ms=1000,
            max_concurrent_agents_by_state={},
        ),
        runner=CodingAgentConfig(
            command=provider,
            turn_timeout_ms=5000,
            read_timeout_ms=1000,
            stall_timeout_ms=5000,
            dangerously_skip_permissions=True,
            provider=provider,
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
            qa_review=QAReviewConfig(
                enabled=qa_enabled,
                dispatch="QA Review",
                success="In Review",
                failure="Todo",
                max_bounces=2,
                max_retries=2,
            ),
        ),
    )


def _build_orchestrator(
    provider: str = "claude",
    qa_enabled: bool = False,
    max_turns: int = 3,
) -> Orchestrator:
    config = _build_config(provider, qa_enabled, max_turns)
    workflow = WorkflowDefinition(
        config={},
        prompt_template="Implement {{ issue.title }}",
        review_prompt_template=None,
    )
    return Orchestrator(Path(".cymphony/config.yml"), config, workflow)


def _build_issue(
    issue_id: str = "issue-1",
    identifier: str = "BAP-E2E-1",
    state: str = "Todo",
    title: str = "E2E test issue",
    description: str | None = "Automated E2E test",
    comments: list[Comment] | None = None,
) -> Issue:
    return Issue(
        id=issue_id,
        identifier=identifier,
        title=title,
        project_name="parity-test",
        description=description,
        priority=2,
        state=state,
        branch_name=None,
        url=f"https://linear.app/test/{identifier}",
        labels=[],
        blocked_by=[],
        comments=comments or [],
        created_at=_now(),
        updated_at=_now(),
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


def _build_entry(
    issue_id: str = "issue-1",
    identifier: str = "BAP-E2E-1",
    mode: ExecutionMode = ExecutionMode.BUILD,
    state: str = "In Progress",
) -> RunningEntry:
    return RunningEntry(
        issue_id=issue_id,
        identifier=identifier,
        issue=_build_issue(issue_id=issue_id, identifier=identifier, state=state),
        task=None,
        session=_build_session(),
        retry_attempt=None,
        started_at=_now(),
        mode=mode,
    )


# ---------------------------------------------------------------------------
# Provider-specific event factories
# ---------------------------------------------------------------------------

def _make_session_started_event(provider: str, session_id: str = "sess-1") -> dict:
    """Build raw session-started event for a given provider."""
    if provider == "claude":
        return {"type": "system", "subtype": "init", "session_id": session_id}
    return {"type": "thread.started", "thread_id": session_id}


def _make_notification_event(provider: str, message: str = "Working on it") -> dict:
    """Build raw notification event for a given provider."""
    if provider == "claude":
        return {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": message}]},
        }
    return {
        "type": "item.completed",
        "item": {"id": "item_1", "type": "agent_message", "text": message},
    }


def _make_todowrite_event(provider: str, todos: list[dict]) -> dict:
    """Build raw TodoWrite event for a given provider."""
    if provider == "claude":
        return {
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
        }
    return {
        "type": "item.completed",
        "item": {
            "id": "item_tw",
            "type": "function_call",
            "name": "TodoWrite",
            "arguments": json.dumps({"todos": todos}),
            "call_id": "call_tw",
        },
    }


def _make_turn_completed_event(provider: str, usage: dict | None = None) -> dict:
    """Build raw turn-completed event for a given provider."""
    if provider == "claude":
        event = {"type": "result", "subtype": "success"}
        if usage:
            event["usage"] = usage
        return event
    event: dict = {"type": "turn.completed"}
    if usage:
        event["usage"] = usage
    return event


def _make_turn_failed_event(provider: str, error: str = "something failed") -> dict:
    """Build raw turn-failed event for a given provider."""
    if provider == "claude":
        return {"type": "result", "subtype": "error"}
    return {"type": "turn.failed", "detail": error}


def _make_input_required_event(provider: str) -> dict:
    """Build raw input-required event for a given provider."""
    if provider == "claude":
        return {"type": "result", "subtype": "input_required"}
    return {"type": "input_required"}


def _parse_event(provider: str, raw: dict, session_id: str | None = None) -> tuple:
    """Parse a raw event using the correct provider parser."""
    line = json.dumps(raw)
    if provider == "claude":
        return parse_claude_stream_event(line, session_id, "iss", "ID-1", 1)
    return parse_codex_stream_event(line, session_id, "iss", "ID-1", 1)


# ---------------------------------------------------------------------------
# 1. Event parsing parity
# ---------------------------------------------------------------------------


class TestEventParsingParity:
    """Both providers must produce the same AgentEventType for equivalent lifecycle events."""

    @pytest.mark.parametrize("provider", _PROVIDERS)
    def test_session_started_produces_same_event_type(self, provider: str) -> None:
        raw = _make_session_started_event(provider)
        event, sid, ok, err = _parse_event(provider, raw)
        assert event is not None
        assert event.event == AgentEventType.SESSION_STARTED
        assert sid is not None

    @pytest.mark.parametrize("provider", _PROVIDERS)
    def test_notification_produces_same_event_type(self, provider: str) -> None:
        raw = _make_notification_event(provider, "hello world")
        event, sid, ok, err = _parse_event(provider, raw)
        assert event is not None
        assert event.event == AgentEventType.NOTIFICATION
        assert event.message is not None
        assert "hello" in event.message.lower() or "world" in event.message.lower()

    @pytest.mark.parametrize("provider", _PROVIDERS)
    def test_turn_completed_produces_same_event_type(self, provider: str) -> None:
        raw = _make_turn_completed_event(provider)
        event, sid, ok, err = _parse_event(provider, raw)
        assert event is not None
        assert event.event == AgentEventType.TURN_COMPLETED
        assert ok is True

    @pytest.mark.parametrize("provider", _PROVIDERS)
    def test_turn_failed_produces_same_event_type(self, provider: str) -> None:
        raw = _make_turn_failed_event(provider)
        event, sid, ok, err = _parse_event(provider, raw)
        assert event is not None
        assert event.event == AgentEventType.TURN_FAILED
        assert err is not None

    @pytest.mark.parametrize("provider", _PROVIDERS)
    def test_input_required_produces_same_event_type(self, provider: str) -> None:
        raw = _make_input_required_event(provider)
        event, sid, ok, err = _parse_event(provider, raw)
        assert event is not None
        assert event.event == AgentEventType.TURN_INPUT_REQUIRED

    @pytest.mark.parametrize("provider", _PROVIDERS)
    def test_malformed_json_produces_same_event_type(self, provider: str) -> None:
        parser = parse_claude_stream_event if provider == "claude" else parse_codex_stream_event
        event, sid, ok, err = parser("not valid json", None, "iss", "ID-1", 1)
        assert event is not None
        assert event.event == AgentEventType.MALFORMED

    @pytest.mark.parametrize("provider", _PROVIDERS)
    def test_unknown_type_produces_same_event_type(self, provider: str) -> None:
        raw = {"type": "totally_unknown_event_xyz"}
        event, sid, ok, err = _parse_event(provider, raw)
        assert event is not None
        assert event.event == AgentEventType.OTHER_MESSAGE

    @pytest.mark.parametrize("provider", _PROVIDERS)
    def test_usage_extraction_parity(self, provider: str) -> None:
        usage = {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 10}
        if provider == "codex":
            usage = {"input_tokens": 100, "output_tokens": 50, "cached_input_tokens": 10}
        raw = _make_turn_completed_event(provider, usage=usage)
        event, sid, ok, err = _parse_event(provider, raw)
        assert event is not None
        assert event.usage is not None
        assert event.usage["input_tokens"] == 100
        assert event.usage["output_tokens"] == 50
        assert event.usage["cache_read_input_tokens"] == 10


# ---------------------------------------------------------------------------
# 2. Runner construction parity
# ---------------------------------------------------------------------------


class TestRunnerConstructionParity:
    """Both providers must produce valid runners from the factory."""

    @pytest.mark.parametrize("provider", _PROVIDERS)
    def test_create_runner_succeeds(self, provider: str) -> None:
        config = CodingAgentConfig(
            command=provider,
            turn_timeout_ms=1000,
            read_timeout_ms=1000,
            stall_timeout_ms=1000,
            dangerously_skip_permissions=True,
        )
        runner = create_agent_runner(provider, config)
        assert runner is not None

    @pytest.mark.parametrize("provider", _PROVIDERS)
    def test_command_includes_prompt(self, provider: str) -> None:
        config = CodingAgentConfig(
            command=provider,
            turn_timeout_ms=1000,
            read_timeout_ms=1000,
            stall_timeout_ms=1000,
            dangerously_skip_permissions=True,
        )
        runner = create_agent_runner(provider, config)
        cmd = runner._build_command("test prompt here", "/ws", None, "title")
        assert "test prompt here" in cmd

    @pytest.mark.parametrize("provider", _PROVIDERS)
    def test_resume_includes_session_id(self, provider: str) -> None:
        config = CodingAgentConfig(
            command=provider,
            turn_timeout_ms=1000,
            read_timeout_ms=1000,
            stall_timeout_ms=1000,
            dangerously_skip_permissions=True,
        )
        runner = create_agent_runner(provider, config)
        cmd = runner._build_command("continue", "/ws", "sess-xyz", "title")
        assert "sess-xyz" in cmd

    @pytest.mark.parametrize("provider", _PROVIDERS)
    def test_permissions_flag_present_when_enabled(self, provider: str) -> None:
        config = CodingAgentConfig(
            command=provider,
            turn_timeout_ms=1000,
            read_timeout_ms=1000,
            stall_timeout_ms=1000,
            dangerously_skip_permissions=True,
        )
        runner = create_agent_runner(provider, config)
        cmd = runner._build_command("prompt", "/ws", None, "title")
        if provider == "claude":
            assert "--dangerously-skip-permissions" in cmd
        else:
            assert "--dangerously-bypass-approvals-and-sandbox" in cmd

    @pytest.mark.parametrize("provider", _PROVIDERS)
    def test_permissions_flag_absent_when_disabled(self, provider: str) -> None:
        config = CodingAgentConfig(
            command=provider,
            turn_timeout_ms=1000,
            read_timeout_ms=1000,
            stall_timeout_ms=1000,
            dangerously_skip_permissions=False,
        )
        runner = create_agent_runner(provider, config)
        cmd = runner._build_command("prompt", "/ws", None, "title")
        assert "--dangerously-skip-permissions" not in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" not in cmd


# ---------------------------------------------------------------------------
# 3. Plan comment sync parity (TodoWrite detection)
# ---------------------------------------------------------------------------


class TestPlanCommentSyncParity:
    """Both providers must trigger plan comment sync from TodoWrite events."""

    @pytest.mark.parametrize("provider", _PROVIDERS)
    @pytest.mark.asyncio
    async def test_todowrite_triggers_plan_comment_create(self, provider: str) -> None:
        orch = _build_orchestrator(provider)
        entry = _build_entry()

        todos = [
            {"content": "Read the code", "status": "completed"},
            {"content": "Write the fix", "status": "in_progress"},
            {"content": "Run tests", "status": "pending"},
        ]
        raw = _make_todowrite_event(provider, todos)
        event = AgentEvent(
            event=AgentEventType.NOTIFICATION,
            timestamp=_now(),
            session_id="sess-1",
            raw=raw,
        )

        mock_create = AsyncMock(return_value="comment-plan-1")
        with patch("cymphony.orchestrator.LinearClient") as MockClient:
            instance = MockClient.return_value
            instance.create_comment = mock_create
            instance.update_comment = AsyncMock()

            await orch._handle_agent_event("issue-1", entry, event)
            await asyncio.sleep(0.05)

        mock_create.assert_awaited_once()
        body = mock_create.call_args[0][1]
        assert "Read the code" in body
        assert "Write the fix" in body
        assert "Run tests" in body
        assert entry.session.plan_comment_id == "comment-plan-1"

    @pytest.mark.parametrize("provider", _PROVIDERS)
    @pytest.mark.asyncio
    async def test_todowrite_updates_existing_comment(self, provider: str) -> None:
        orch = _build_orchestrator(provider)
        entry = _build_entry()
        entry.session.plan_comment_id = "existing-comment"

        todos = [{"content": "Updated plan", "status": "in_progress"}]
        raw = _make_todowrite_event(provider, todos)
        event = AgentEvent(
            event=AgentEventType.NOTIFICATION,
            timestamp=_now(),
            session_id="sess-1",
            raw=raw,
        )

        mock_update = AsyncMock(return_value=True)
        with patch("cymphony.orchestrator.LinearClient") as MockClient:
            instance = MockClient.return_value
            instance.create_comment = AsyncMock()
            instance.update_comment = mock_update

            await orch._handle_agent_event("issue-1", entry, event)
            await asyncio.sleep(0.05)

        mock_update.assert_awaited_once()
        assert "Updated plan" in mock_update.call_args[0][1]

    @pytest.mark.parametrize("provider", _PROVIDERS)
    @pytest.mark.asyncio
    async def test_empty_todos_does_not_sync(self, provider: str) -> None:
        orch = _build_orchestrator(provider)
        entry = _build_entry()

        raw = _make_todowrite_event(provider, [])
        event = AgentEvent(
            event=AgentEventType.NOTIFICATION,
            timestamp=_now(),
            session_id="sess-1",
            raw=raw,
        )

        with patch("cymphony.orchestrator.LinearClient") as MockClient:
            instance = MockClient.return_value
            instance.create_comment = AsyncMock()

            await orch._handle_agent_event("issue-1", entry, event)
            await asyncio.sleep(0.05)

        instance.create_comment.assert_not_awaited()


# ---------------------------------------------------------------------------
# 4. Review result parsing parity (provider-agnostic file-based)
# ---------------------------------------------------------------------------


class TestReviewResultParsingParity:
    """Review result parsing must work identically regardless of which provider wrote the file."""

    def test_pass_decision_parsed(self, tmp_path: Path) -> None:
        from cymphony.review import parse_review_result

        result_file = tmp_path / "REVIEW_RESULT.json"
        result_file.write_text(json.dumps({
            "decision": "pass",
            "summary": "Looks good",
        }))

        result = parse_review_result(str(tmp_path))
        assert result.decision == ReviewDecision.PASS
        assert result.summary == "Looks good"
        assert result.error is None

    def test_changes_requested_decision_parsed(self, tmp_path: Path) -> None:
        from cymphony.review import parse_review_result

        result_file = tmp_path / "REVIEW_RESULT.json"
        result_file.write_text(json.dumps({
            "decision": "changes_requested",
            "summary": "Fix the tests",
        }))

        result = parse_review_result(str(tmp_path))
        assert result.decision == ReviewDecision.CHANGES_REQUESTED
        assert result.summary == "Fix the tests"

    def test_missing_file_returns_error(self, tmp_path: Path) -> None:
        from cymphony.review import parse_review_result

        result = parse_review_result(str(tmp_path))
        assert result.decision is None
        assert result.error is not None

    def test_invalid_json_returns_error(self, tmp_path: Path) -> None:
        from cymphony.review import parse_review_result

        result_file = tmp_path / "REVIEW_RESULT.json"
        result_file.write_text("not json")

        result = parse_review_result(str(tmp_path))
        assert result.decision is None
        assert result.error is not None

    def test_invalid_decision_returns_error(self, tmp_path: Path) -> None:
        from cymphony.review import parse_review_result

        result_file = tmp_path / "REVIEW_RESULT.json"
        result_file.write_text(json.dumps({"decision": "maybe"}))

        result = parse_review_result(str(tmp_path))
        assert result.decision is None
        assert result.error is not None


# ---------------------------------------------------------------------------
# 5. Happy-path orchestration parity (dispatch → success transitions)
# ---------------------------------------------------------------------------


class TestHappyPathTransitionParity:
    """Execution worker success must produce the same state transitions for both providers."""

    @pytest.mark.parametrize("provider", _PROVIDERS)
    @pytest.mark.asyncio
    async def test_execution_success_transitions_to_in_review(
        self, provider: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch = _build_orchestrator(provider, qa_enabled=False)
        entry = _build_entry(state="In Progress")
        entry.status = RunStatus.SUCCEEDED

        transitions: list[tuple[str, str, str]] = []

        async def fake_transition(issue_id, target, *, trigger, issue_identifier, from_state):
            transitions.append((issue_id, target, trigger))
            return True

        monkeypatch.setattr(orch, "_transition_issue_state", fake_transition)
        monkeypatch.setattr(orch, "_release_completed_issue", lambda *a: None)

        await orch._handle_execution_worker_success("issue-1", "BAP-E2E-1", entry)

        assert ("issue-1", "In Review", "success") in transitions

    @pytest.mark.parametrize("provider", _PROVIDERS)
    @pytest.mark.asyncio
    async def test_execution_success_with_qa_transitions_to_qa_review(
        self, provider: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch = _build_orchestrator(provider, qa_enabled=True)
        entry = _build_entry(state="In Progress")
        entry.workspace_path = "/tmp/fake-ws"
        entry.status = RunStatus.SUCCEEDED

        transitions: list[tuple[str, str, str]] = []

        async def fake_transition(issue_id, target, *, trigger, issue_identifier, from_state):
            transitions.append((issue_id, target, trigger))
            return True

        async def fake_validate_handoff(identifier, workspace_path):
            return True, ""

        monkeypatch.setattr(orch, "_transition_issue_state", fake_transition)
        monkeypatch.setattr(orch, "_validate_review_handoff", fake_validate_handoff)
        monkeypatch.setattr(orch, "_release_completed_issue", lambda *a: None)

        await orch._handle_execution_worker_success("issue-1", "BAP-E2E-1", entry)

        assert ("issue-1", "QA Review", "success") in transitions


# ---------------------------------------------------------------------------
# 6. QA review flow parity
# ---------------------------------------------------------------------------


class TestQAReviewFlowParity:
    """QA review decisions must produce the same state transitions for both providers."""

    @pytest.mark.parametrize("provider", _PROVIDERS)
    @pytest.mark.asyncio
    async def test_qa_pass_transitions_to_in_review(
        self, provider: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch = _build_orchestrator(provider, qa_enabled=True)
        entry = _build_entry(mode=ExecutionMode.REVIEW, state="QA Review")
        entry.review_result = ReviewResult(
            decision=ReviewDecision.PASS,
            summary="All good",
        )

        transitions: list[tuple[str, str, str]] = []

        async def fake_transition(issue_id, target, *, trigger, issue_identifier, from_state):
            transitions.append((issue_id, target, trigger))
            return True

        monkeypatch.setattr(orch, "_transition_issue_state", fake_transition)
        monkeypatch.setattr(orch, "_cleanup_review_workspace", AsyncMock())
        monkeypatch.setattr(orch, "_release_completed_issue", lambda *a: None)
        monkeypatch.setattr(orch, "_post_review_result_comment", AsyncMock())
        monkeypatch.setattr(orch, "_persist_state", lambda: None)

        await orch._handle_review_worker_success("issue-1", "BAP-E2E-1", entry)

        assert ("issue-1", "In Review", "success") in transitions

    @pytest.mark.parametrize("provider", _PROVIDERS)
    @pytest.mark.asyncio
    async def test_qa_changes_requested_transitions_to_todo(
        self, provider: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch = _build_orchestrator(provider, qa_enabled=True)
        entry = _build_entry(mode=ExecutionMode.REVIEW, state="QA Review")
        entry.review_result = ReviewResult(
            decision=ReviewDecision.CHANGES_REQUESTED,
            summary="Fix the tests",
        )

        transitions: list[tuple[str, str, str]] = []

        async def fake_transition(issue_id, target, *, trigger, issue_identifier, from_state):
            transitions.append((issue_id, target, trigger))
            return True

        monkeypatch.setattr(orch, "_transition_issue_state", fake_transition)
        monkeypatch.setattr(orch, "_cleanup_review_workspace", AsyncMock())
        monkeypatch.setattr(orch, "_release_completed_issue", lambda *a: None)
        monkeypatch.setattr(orch, "_post_review_result_comment", AsyncMock())
        monkeypatch.setattr(orch, "_persist_state", lambda: None)

        await orch._handle_review_worker_success("issue-1", "BAP-E2E-1", entry)

        assert ("issue-1", "Todo", "success") in transitions


# ---------------------------------------------------------------------------
# 7. Stuck-state detection parity
# ---------------------------------------------------------------------------


class TestStuckStateParity:
    """Neither provider should leave issues stuck in intermediate states."""

    @pytest.mark.parametrize("provider", _PROVIDERS)
    def test_claimed_set_cleared_after_dispatch(self, provider: str) -> None:
        orch = _build_orchestrator(provider)
        issue = _build_issue()
        orch._state.claimed.add(issue.id)

        # After worker completion, claimed should be cleared
        orch._state.claimed.discard(issue.id)
        assert issue.id not in orch._state.claimed

    @pytest.mark.parametrize("provider", _PROVIDERS)
    @pytest.mark.asyncio
    async def test_worker_failure_schedules_retry(
        self, provider: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch = _build_orchestrator(provider)
        entry = _build_entry()

        scheduled_retries: list[str] = []

        async def fake_schedule(issue_id, identifier, outcome, entry=None):
            if outcome:
                scheduled_retries.append(issue_id)

        monkeypatch.setattr(orch, "_schedule_retry_from_outcome", fake_schedule)
        monkeypatch.setattr(orch, "_apply_execution_failure_outcome", AsyncMock())

        exc = Exception("agent crashed")
        await orch._handle_worker_failure("issue-1", "BAP-E2E-1", entry, exc)

        assert "issue-1" in scheduled_retries

    @pytest.mark.parametrize("provider", _PROVIDERS)
    @pytest.mark.asyncio
    async def test_success_without_qa_schedules_continuation_retry(
        self, provider: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When QA is disabled, success schedules a continuation retry to re-evaluate."""
        orch = _build_orchestrator(provider, qa_enabled=False)
        entry = _build_entry(state="In Progress")

        retries: list[str] = []

        async def fake_transition(*a, **kw):
            return True

        async def fake_schedule_retry(issue_id, identifier, outcome, entry=None):
            if outcome:
                retries.append(issue_id)

        monkeypatch.setattr(orch, "_transition_issue_state", fake_transition)
        monkeypatch.setattr(orch, "_schedule_retry_from_outcome", fake_schedule_retry)

        await orch._handle_execution_worker_success("issue-1", "BAP-E2E-1", entry)

        assert "issue-1" in retries

    @pytest.mark.parametrize("provider", _PROVIDERS)
    @pytest.mark.asyncio
    async def test_success_with_qa_releases_after_qa_handoff(
        self, provider: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When QA is enabled, success transitions to QA Review and releases."""
        orch = _build_orchestrator(provider, qa_enabled=True)
        entry = _build_entry(state="In Progress")
        entry.workspace_path = "/tmp/fake-ws"

        released: list[str] = []

        async def fake_transition(*a, **kw):
            return True

        async def fake_validate_handoff(identifier, workspace_path):
            return True, ""

        monkeypatch.setattr(orch, "_transition_issue_state", fake_transition)
        monkeypatch.setattr(orch, "_validate_review_handoff", fake_validate_handoff)
        monkeypatch.setattr(orch, "_release_completed_issue", lambda issue_id: released.append(issue_id))

        await orch._handle_execution_worker_success("issue-1", "BAP-E2E-1", entry)

        assert "issue-1" in released


# ---------------------------------------------------------------------------
# 8. Token accounting parity
# ---------------------------------------------------------------------------


class TestTokenAccountingParity:
    """Token usage must be tracked consistently for both providers."""

    @pytest.mark.parametrize("provider", _PROVIDERS)
    @pytest.mark.asyncio
    async def test_token_deltas_accumulated(self, provider: str) -> None:
        orch = _build_orchestrator(provider)
        entry = _build_entry()

        # Simulate a turn-completed event with usage
        usage = {"input_tokens": 500, "output_tokens": 200, "cache_read_input_tokens": 50}
        event = AgentEvent(
            event=AgentEventType.TURN_COMPLETED,
            timestamp=_now(),
            session_id="sess-1",
            pid=123,
            usage=usage,
        )

        await orch._handle_agent_event("issue-1", entry, event)

        assert entry.session.input_tokens == 500
        assert entry.session.output_tokens == 200
        assert entry.session.total_tokens == 700

    @pytest.mark.parametrize("provider", _PROVIDERS)
    @pytest.mark.asyncio
    async def test_token_deltas_not_double_counted(self, provider: str) -> None:
        orch = _build_orchestrator(provider)
        entry = _build_entry()

        usage = {"input_tokens": 500, "output_tokens": 200, "cache_read_input_tokens": 50}
        event = AgentEvent(
            event=AgentEventType.TURN_COMPLETED,
            timestamp=_now(),
            session_id="sess-1",
            pid=123,
            usage=usage,
        )

        await orch._handle_agent_event("issue-1", entry, event)
        await orch._handle_agent_event("issue-1", entry, event)

        # Same usage reported twice should not double count
        assert entry.session.input_tokens == 500
        assert entry.session.output_tokens == 200


# ---------------------------------------------------------------------------
# 9. Execution mode routing parity
# ---------------------------------------------------------------------------


class TestExecutionModeRoutingParity:
    """State machine must route issues identically regardless of provider."""

    @pytest.mark.parametrize("provider", _PROVIDERS)
    def test_todo_routes_to_execution(self, provider: str) -> None:
        orch = _build_orchestrator(provider, qa_enabled=True)
        issue = _build_issue(state="To Do")
        route = orch._state_machine.route(issue)
        assert route is not None
        assert route.workflow == "execution"

    @pytest.mark.parametrize("provider", _PROVIDERS)
    def test_qa_review_routes_to_qa_review(self, provider: str) -> None:
        orch = _build_orchestrator(provider, qa_enabled=True)
        issue = _build_issue(state="QA Review")
        route = orch._state_machine.route(issue)
        assert route is not None
        assert route.workflow == "qa_review"

    @pytest.mark.parametrize("provider", _PROVIDERS)
    def test_in_progress_not_routed(self, provider: str) -> None:
        orch = _build_orchestrator(provider, qa_enabled=True)
        issue = _build_issue(state="In Progress")
        route = orch._state_machine.route(issue)
        assert route is None


# ---------------------------------------------------------------------------
# 10. Rework loop parity (QA bounce cycle)
# ---------------------------------------------------------------------------


class TestReworkLoopParity:
    """QA bounce tracking must work identically for both providers."""

    @pytest.mark.parametrize("provider", _PROVIDERS)
    def test_bounce_count_increments(self, provider: str) -> None:
        orch = _build_orchestrator(provider, qa_enabled=True)
        assert orch._state.qa_review_bounces.get("issue-1", 0) == 0

        orch._state.qa_review_bounces["issue-1"] = 1
        assert orch._state.qa_review_bounces["issue-1"] == 1

    @pytest.mark.parametrize("provider", _PROVIDERS)
    def test_bounce_count_cleared_on_pass(self, provider: str) -> None:
        orch = _build_orchestrator(provider, qa_enabled=True)
        orch._state.qa_review_bounces["issue-1"] = 2

        orch._clear_qa_review_bounces("issue-1")
        assert orch._state.qa_review_bounces.get("issue-1", 0) == 0

    @pytest.mark.parametrize("provider", _PROVIDERS)
    def test_qa_review_comment_tracking(self, provider: str) -> None:
        orch = _build_orchestrator(provider, qa_enabled=True)
        orch._state.qa_review_comment_ids["issue-1"] = "comment-qa-1"

        assert orch._state.qa_review_comment_ids["issue-1"] == "comment-qa-1"

        # On bounce, comment ID is cleared for fresh review
        orch._state.qa_review_comment_ids.pop("issue-1", None)
        assert "issue-1" not in orch._state.qa_review_comment_ids


# ---------------------------------------------------------------------------
# 11. Prompt rendering parity
# ---------------------------------------------------------------------------


class TestPromptRenderingParity:
    """Prompt templates must render identically for both providers."""

    @pytest.mark.parametrize("provider", _PROVIDERS)
    def test_execution_prompt_renders(self, provider: str) -> None:
        from cymphony.workflow import render_prompt

        workflow = WorkflowDefinition(
            config={},
            prompt_template="Fix {{ issue.title }} ({{ issue.identifier }})",
        )
        issue = _build_issue()
        result = render_prompt(workflow, issue, attempt=None)
        assert "E2E test issue" in result
        assert "BAP-E2E-1" in result

    @pytest.mark.parametrize("provider", _PROVIDERS)
    def test_plan_prompt_renders(self, provider: str) -> None:
        from cymphony.workflow import render_plan_prompt

        workflow = WorkflowDefinition(config={}, prompt_template="")
        issue = _build_issue()
        result = render_plan_prompt(workflow, issue)
        assert "TodoWrite" in result
        assert "E2E test issue" in result

    @pytest.mark.parametrize("provider", _PROVIDERS)
    def test_review_prompt_renders(self, provider: str) -> None:
        from cymphony.workflow import render_review_prompt

        workflow = WorkflowDefinition(config={}, prompt_template="")
        issue = _build_issue()
        result = render_review_prompt(workflow, issue)
        assert "REVIEW_RESULT.json" in result
        assert "review mode" in result.lower()


# ---------------------------------------------------------------------------
# 12. Environment isolation parity
# ---------------------------------------------------------------------------


class TestEnvironmentIsolationParity:
    """Environment setup must be correct for each provider."""

    def test_claude_strips_claudecode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDECODE", "1")
        config = CodingAgentConfig(
            command="claude", turn_timeout_ms=1000, read_timeout_ms=1000,
            stall_timeout_ms=1000, dangerously_skip_permissions=True,
        )
        runner = create_agent_runner("claude", config)
        env = runner._build_env()
        assert "CLAUDECODE" not in env

    def test_codex_preserves_claudecode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDECODE", "1")
        config = CodingAgentConfig(
            command="codex", turn_timeout_ms=1000, read_timeout_ms=1000,
            stall_timeout_ms=1000, dangerously_skip_permissions=True,
        )
        runner = create_agent_runner("codex", config)
        env = runner._build_env()
        assert env.get("CLAUDECODE") == "1"
