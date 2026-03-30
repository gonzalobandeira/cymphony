from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cymphony.models import (
    AgentConfig,
    CodingAgentConfig,
    HooksConfig,
    Issue,
    PollingConfig,
    PreflightConfig,
    RetryEntry,
    ServerConfig,
    ServiceConfig,
    SkippedEntry,
    TrackerConfig,
    WorkflowDefinition,
    WorkspaceConfig,
)
from cymphony.orchestrator import Orchestrator
from cymphony.linear import LinearClient
from cymphony.state import StateManager, _STATE_VERSION


def _make_state_manager(tmp_path: Path) -> StateManager:
    return StateManager(tmp_path / ".cymphony_state.json")


def _make_retry_entry(
    issue_id: str = "issue-1",
    identifier: str = "BAP-151",
    attempt: int = 2,
    error: str | None = "some error",
) -> RetryEntry:
    return RetryEntry(
        issue_id=issue_id,
        identifier=identifier,
        attempt=attempt,
        due_at_ms=12345.0,
        error=error,
        state="Todo",
        run_status="Failed",
        session_id="session-abc",
        turn_count=3,
        last_event="turn_completed",
        last_message="Working on it",
        last_event_at=datetime(2026, 3, 28, 12, 0, 0, tzinfo=timezone.utc),
        workspace_path="/tmp/ws/BAP-151",
        tokens={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        started_at=datetime(2026, 3, 28, 11, 0, 0, tzinfo=timezone.utc),
        retry_attempt=1,
        plan_comment_id="comment-1",
        latest_plan="**Agent Plan**\n- [ ] Step 1",
        recent_events=[{"event": "turn_completed", "timestamp": "2026-03-28T12:00:00+00:00"}],
        issue_title="Fix the bug",
        issue_url="https://linear.app/proj/issue/BAP-151",
        issue_description="Description here",
        issue_labels=["bug"],
        issue_comments=[{"author": "alice", "body": "please fix", "created_at": None}],
        qa_review_bounce_count=3,
    )


def _make_skipped_entry(
    issue_id: str = "issue-2",
    identifier: str = "BAP-152",
) -> SkippedEntry:
    return SkippedEntry(
        issue_id=issue_id,
        identifier=identifier,
        created_at=datetime(2026, 3, 28, 10, 0, 0, tzinfo=timezone.utc),
        reason="operator_skip",
    )


# ---------------------------------------------------------------------------
# StateManager unit tests
# ---------------------------------------------------------------------------


class TestStateManagerSaveLoad:
    def test_round_trip(self, tmp_path: Path) -> None:
        sm = _make_state_manager(tmp_path)
        retry = _make_retry_entry()
        skipped = _make_skipped_entry()

        sm.save(
            retry_attempts={"issue-1": retry},
            qa_review_bounces={"issue-1": 2},
            qa_review_comment_ids={"issue-1": "comment-99"},
            skipped={"issue-2": skipped},
            dispatch_paused=True,
        )

        retries, qa_bounces, qa_comment_ids, skips, paused = sm.restore()

        assert len(retries) == 1
        assert "issue-1" in retries
        r = retries["issue-1"]
        assert r.identifier == "BAP-151"
        assert r.attempt == 2
        assert r.error == "some error"
        assert r.state == "Todo"
        assert r.session_id == "session-abc"
        assert r.turn_count == 3
        assert r.tokens == {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}
        assert r.issue_title == "Fix the bug"
        assert r.issue_labels == ["bug"]
        assert r.due_at_ms == 0.0  # Recomputed on restore
        assert r.qa_review_bounce_count == 3

        assert qa_bounces == {"issue-1": 2}
        assert qa_comment_ids == {"issue-1": "comment-99"}

        assert len(skips) == 1
        assert "issue-2" in skips
        s = skips["issue-2"]
        assert s.identifier == "BAP-152"
        assert s.reason == "operator_skip"

        assert paused is True

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        sm = _make_state_manager(tmp_path)
        retries, qa_bounces, qa_comment_ids, skips, paused = sm.restore()
        assert retries == {}
        assert qa_bounces == {}
        assert qa_comment_ids == {}
        assert skips == {}
        assert paused is False

    def test_corrupt_json_returns_empty(self, tmp_path: Path) -> None:
        sm = _make_state_manager(tmp_path)
        sm.path.write_text("not valid json {{{", encoding="utf-8")
        retries, qa_bounces, qa_comment_ids, skips, paused = sm.restore()
        assert retries == {}
        assert qa_bounces == {}
        assert qa_comment_ids == {}
        assert skips == {}
        assert paused is False

    def test_wrong_version_returns_empty(self, tmp_path: Path) -> None:
        sm = _make_state_manager(tmp_path)
        sm.path.write_text(
            json.dumps({"version": 999, "retry_attempts": {}, "skipped": {}}),
            encoding="utf-8",
        )
        retries, qa_bounces, qa_comment_ids, skips, paused = sm.restore()
        assert retries == {}
        assert qa_bounces == {}
        assert qa_comment_ids == {}
        assert skips == {}
        assert paused is False

    def test_not_a_dict_returns_empty(self, tmp_path: Path) -> None:
        sm = _make_state_manager(tmp_path)
        sm.path.write_text('"just a string"', encoding="utf-8")
        retries, qa_bounces, qa_comment_ids, skips, paused = sm.restore()
        assert retries == {}
        assert qa_bounces == {}
        assert qa_comment_ids == {}

    def test_clear_removes_file(self, tmp_path: Path) -> None:
        sm = _make_state_manager(tmp_path)
        sm.save(
            retry_attempts={},
            qa_review_bounces={},
            qa_review_comment_ids={},
            skipped={},
            dispatch_paused=False,
        )
        assert sm.path.exists()
        sm.clear()
        assert not sm.path.exists()

    def test_clear_noop_when_no_file(self, tmp_path: Path) -> None:
        sm = _make_state_manager(tmp_path)
        sm.clear()  # Should not raise

    def test_save_creates_parent_directories(self, tmp_path: Path) -> None:
        sm = StateManager(tmp_path / "deep" / "nested" / "state.json")
        sm.save(
            retry_attempts={},
            qa_review_bounces={},
            qa_review_comment_ids={},
            skipped={},
            dispatch_paused=False,
        )
        assert sm.path.exists()

    def test_malformed_retry_entry_skipped(self, tmp_path: Path) -> None:
        sm = _make_state_manager(tmp_path)
        data = {
            "version": _STATE_VERSION,
            "saved_at": "2026-03-28T12:00:00+00:00",
            "retry_attempts": {
                "good": {
                    "issue_id": "good",
                    "identifier": "BAP-1",
                    "attempt": 1,
                    "error": None,
                },
                "bad": {"missing": "required_fields"},
            },
            "skipped": {},
        }
        sm.path.write_text(json.dumps(data), encoding="utf-8")
        retries, qa_bounces, qa_comment_ids, skips, paused = sm.restore()
        assert "good" in retries
        assert "bad" not in retries
        assert qa_bounces == {}
        assert qa_comment_ids == {}

    def test_empty_state_round_trip(self, tmp_path: Path) -> None:
        sm = _make_state_manager(tmp_path)
        sm.save(
            retry_attempts={},
            qa_review_bounces={},
            qa_review_comment_ids={},
            skipped={},
            dispatch_paused=False,
        )
        retries, qa_bounces, qa_comment_ids, skips, paused = sm.restore()
        assert retries == {}
        assert qa_bounces == {}
        assert qa_comment_ids == {}
        assert skips == {}
        assert paused is False

    def test_continuation_retry_round_trip(self, tmp_path: Path) -> None:
        """Continuation retries (error=None) should round-trip correctly."""
        sm = _make_state_manager(tmp_path)
        retry = _make_retry_entry(error=None)
        sm.save(
            retry_attempts={"issue-1": retry},
            qa_review_bounces={},
            qa_review_comment_ids={},
            skipped={},
            dispatch_paused=False,
        )
        retries, _, _, _, _ = sm.restore()
        assert retries["issue-1"].error is None


# ---------------------------------------------------------------------------
# Orchestrator integration tests for state persistence
# ---------------------------------------------------------------------------


def _build_orchestrator(tmp_path: Path) -> Orchestrator:
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
        workspace=WorkspaceConfig(root=str(tmp_path)),
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
            enabled=True,
            required_clis=["git"],
            required_env_vars=[],
            expect_clean_worktree=False,
            base_branch="main",
        ),
    )
    workflow = WorkflowDefinition(config={}, prompt_template="")
    return Orchestrator(Path("WORKFLOW.md"), config, workflow)


def _build_issue(
    issue_id: str = "issue-1",
    identifier: str = "BAP-151",
    state: str = "Todo",
) -> Issue:
    return Issue(
        id=issue_id,
        identifier=identifier,
        title=identifier,
        project_name=None,
        description=None,
        priority=None,
        state=state,
        branch_name=None,
        url=None,
        labels=[],
        blocked_by=[],
        comments=[],
        created_at=None,
        updated_at=None,
    )


class TestOrchestratorPersistence:
    def test_persist_state_writes_file(self, tmp_path: Path) -> None:
        orch = _build_orchestrator(tmp_path)
        orch._state.skipped["issue-1"] = _make_skipped_entry(issue_id="issue-1")
        orch._persist_state()

        assert orch._state_manager.path.exists()
        _, _, _, skips, _ = orch._state_manager.restore()
        assert "issue-1" in skips

    def test_skip_issue_persists_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orch = _build_orchestrator(tmp_path)
        issue = _build_issue()

        # Put issue in retry so skip can find it
        orch._state.retry_attempts[issue.id] = RetryEntry(
            issue_id=issue.id,
            identifier=issue.identifier,
            attempt=1,
            due_at_ms=0.0,
            error="boom",
        )

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(orch.skip_issue(issue.identifier))
        finally:
            loop.close()

        _, _, _, skips, _ = orch._state_manager.restore()
        assert issue.id in skips

    def test_requeue_issue_persists_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orch = _build_orchestrator(tmp_path)
        issue = _build_issue()

        orch._state.skipped[issue.id] = _make_skipped_entry(issue_id=issue.id)
        orch._state.qa_review_bounces[issue.id] = 2
        orch._state.qa_review_comment_ids[issue.id] = "comment-5"

        # Monkeypatch request_immediate_poll to avoid needing an event loop
        monkeypatch.setattr(orch, "request_immediate_poll", lambda: False)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(orch.requeue_issue(issue.identifier))
        finally:
            loop.close()

        _, qa_bounces, qa_comment_ids, skips, _ = orch._state_manager.restore()
        assert issue.id not in skips
        assert issue.id not in qa_bounces
        assert issue.id not in qa_comment_ids


class TestOrchestratorRestore:
    @pytest.mark.asyncio
    async def test_restore_retries_reconciles_terminal_issues(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orch = _build_orchestrator(tmp_path)

        # Pre-persist state with two retries
        retry_active = _make_retry_entry(issue_id="issue-active", identifier="BAP-1")
        retry_terminal = _make_retry_entry(issue_id="issue-terminal", identifier="BAP-2")
        orch._state_manager.save(
            retry_attempts={
                "issue-active": retry_active,
                "issue-terminal": retry_terminal,
            },
            qa_review_bounces={"issue-active": 1, "issue-terminal": 3},
            qa_review_comment_ids={"issue-active": "comment-1", "issue-terminal": "comment-2"},
            skipped={},
            dispatch_paused=False,
        )

        # Mock Linear to return issue states
        class FakeLinearClient:
            def __init__(self, tracker_config: object) -> None:
                pass

            async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
                return [
                    _build_issue(issue_id="issue-active", identifier="BAP-1", state="Todo"),
                    _build_issue(issue_id="issue-terminal", identifier="BAP-2", state="Done"),
                ]

        monkeypatch.setattr("cymphony.orchestrator.LinearClient", FakeLinearClient)

        # Prevent retry timers from actually dispatching
        async def noop_retry_timer(issue_id: str) -> None:
            pass
        monkeypatch.setattr(orch, "_on_retry_timer", noop_retry_timer)

        await orch._restore_persisted_state()

        # Active issue should be restored
        assert "issue-active" in orch._state.retry_attempts
        assert "issue-active" in orch._state.claimed
        assert orch._state.qa_review_bounces == {"issue-active": 1}
        assert orch._state.qa_review_comment_ids == {"issue-active": "comment-1"}

        # Terminal issue should be dropped
        assert "issue-terminal" not in orch._state.retry_attempts
        assert "issue-terminal" not in orch._state.claimed

    @pytest.mark.asyncio
    async def test_restore_skipped_reconciles_terminal_issues(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orch = _build_orchestrator(tmp_path)

        skip_active = _make_skipped_entry(issue_id="issue-active", identifier="BAP-1")
        skip_terminal = _make_skipped_entry(issue_id="issue-terminal", identifier="BAP-2")
        orch._state_manager.save(
            retry_attempts={},
            qa_review_bounces={},
            qa_review_comment_ids={},
            skipped={
                "issue-active": skip_active,
                "issue-terminal": skip_terminal,
            },
            dispatch_paused=False,
        )

        class FakeLinearClient:
            def __init__(self, tracker_config: object) -> None:
                pass

            async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
                return [
                    _build_issue(issue_id="issue-active", identifier="BAP-1", state="In Progress"),
                    _build_issue(issue_id="issue-terminal", identifier="BAP-2", state="Done"),
                ]

        monkeypatch.setattr("cymphony.orchestrator.LinearClient", FakeLinearClient)

        await orch._restore_persisted_state()

        assert "issue-active" in orch._state.skipped
        assert "issue-terminal" not in orch._state.skipped

    @pytest.mark.asyncio
    async def test_restore_dispatch_paused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orch = _build_orchestrator(tmp_path)

        orch._state_manager.save(
            retry_attempts={},
            qa_review_bounces={},
            qa_review_comment_ids={},
            skipped={},
            dispatch_paused=True,
        )

        class FakeLinearClient:
            def __init__(self, tracker_config: object) -> None:
                pass

            async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
                return []

        monkeypatch.setattr("cymphony.orchestrator.LinearClient", FakeLinearClient)

        await orch._restore_persisted_state()

        assert orch._state.dispatch_paused is True

    @pytest.mark.asyncio
    async def test_restore_with_no_state_file_is_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orch = _build_orchestrator(tmp_path)
        await orch._restore_persisted_state()

        assert len(orch._state.retry_attempts) == 0
        assert len(orch._state.skipped) == 0
        assert orch._state.dispatch_paused is False

    @pytest.mark.asyncio
    async def test_restore_survives_linear_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If Linear is unreachable during restore, all entries are kept."""
        orch = _build_orchestrator(tmp_path)

        retry = _make_retry_entry(issue_id="issue-1", identifier="BAP-1")
        skip = _make_skipped_entry(issue_id="issue-2", identifier="BAP-2")
        orch._state_manager.save(
            retry_attempts={"issue-1": retry},
            qa_review_bounces={},
            qa_review_comment_ids={},
            skipped={"issue-2": skip},
            dispatch_paused=False,
        )

        class FakeLinearClient:
            def __init__(self, tracker_config: object) -> None:
                pass

            async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
                raise ConnectionError("Linear is down")

        monkeypatch.setattr("cymphony.orchestrator.LinearClient", FakeLinearClient)

        async def noop_retry_timer(issue_id: str) -> None:
            pass
        monkeypatch.setattr(orch, "_on_retry_timer", noop_retry_timer)

        await orch._restore_persisted_state()

        # All entries preserved when Linear is unreachable
        assert "issue-1" in orch._state.retry_attempts
        assert "issue-2" in orch._state.skipped

    @pytest.mark.asyncio
    async def test_restart_round_trip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulate a full restart: save state, create new orchestrator, restore."""
        orch1 = _build_orchestrator(tmp_path)

        # Simulate some runtime state
        orch1._state.retry_attempts["issue-1"] = _make_retry_entry(
            issue_id="issue-1", identifier="BAP-1", attempt=3
        )
        orch1._state.qa_review_bounces["issue-1"] = 2
        orch1._state.qa_review_comment_ids["issue-1"] = "comment-7"
        orch1._state.skipped["issue-2"] = _make_skipped_entry(
            issue_id="issue-2", identifier="BAP-2"
        )
        orch1._state.dispatch_paused = True
        orch1._persist_state()

        # Create a new orchestrator (simulating restart)
        orch2 = _build_orchestrator(tmp_path)

        class FakeLinearClient:
            def __init__(self, tracker_config: object) -> None:
                pass

            async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
                return [
                    _build_issue(issue_id="issue-1", identifier="BAP-1", state="Todo"),
                    _build_issue(issue_id="issue-2", identifier="BAP-2", state="In Progress"),
                ]

        monkeypatch.setattr("cymphony.orchestrator.LinearClient", FakeLinearClient)

        async def noop_retry_timer(issue_id: str) -> None:
            pass
        monkeypatch.setattr(orch2, "_on_retry_timer", noop_retry_timer)

        await orch2._restore_persisted_state()

        assert "issue-1" in orch2._state.retry_attempts
        assert orch2._state.retry_attempts["issue-1"].attempt == 3
        assert orch2._state.qa_review_bounces == {"issue-1": 2}
        assert orch2._state.qa_review_comment_ids == {"issue-1": "comment-7"}
        assert "issue-2" in orch2._state.skipped
        assert orch2._state.dispatch_paused is True

    @pytest.mark.asyncio
    async def test_restore_drops_retries_for_issues_not_in_linear(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orch = _build_orchestrator(tmp_path)

        retry = _make_retry_entry(issue_id="issue-gone", identifier="BAP-GONE")
        orch._state_manager.save(
            retry_attempts={"issue-gone": retry},
            qa_review_bounces={"issue-gone": 2},
            qa_review_comment_ids={"issue-gone": "comment-gone"},
            skipped={},
            dispatch_paused=False,
        )

        class FakeLinearClient:
            def __init__(self, tracker_config: object) -> None:
                pass

            async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
                return []  # Issue not found

        monkeypatch.setattr("cymphony.orchestrator.LinearClient", FakeLinearClient)

        async def noop_retry_timer(issue_id: str) -> None:
            pass
        monkeypatch.setattr(orch, "_on_retry_timer", noop_retry_timer)

        await orch._restore_persisted_state()

        assert "issue-gone" not in orch._state.retry_attempts
        assert "issue-gone" not in orch._state.qa_review_bounces
        assert "issue-gone" not in orch._state.qa_review_comment_ids
