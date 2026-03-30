from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cymphony.linear import LinearClient, _normalize_issue
from cymphony.models import (
    AgentConfig,
    BlockerRef,
    CodingAgentConfig,
    Issue,
    LiveSession,
    PollingConfig,
    PreflightConfig,
    ProblemRecord,
    RetryEntry,
    RunningEntry,
    RunStatus,
    ServerConfig,
    ServiceConfig,
    TrackerConfig,
    TransitionsConfig,
    WorkflowDefinition,
    WorkspaceConfig,
    HooksConfig,
)
from cymphony.orchestrator import Orchestrator
from cymphony.workspace import WorkspaceManager


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


def test_should_dispatch_blocks_in_progress_issue_with_unresolved_dependencies() -> None:
    orchestrator = _build_orchestrator()
    issue = _build_issue(issue_id="issue-2", identifier="BAN-217", state="In Progress")
    issue.blocked_by = [
        BlockerRef(id="blocker-1", identifier="BAN-196", state="Todo"),
        BlockerRef(id="blocker-2", identifier="BAN-216", state="In Progress"),
    ]

    assert orchestrator._should_dispatch(issue) is False


def test_maybe_transition_blocked_issue_uses_configured_state(monkeypatch: pytest.MonkeyPatch) -> None:
    orchestrator = _build_orchestrator()
    orchestrator._config.transitions = TransitionsConfig(blocked="Blocked")
    issue = _build_issue(issue_id="issue-2", identifier="BAN-217", state="Todo")
    issue.blocked_by = [
        BlockerRef(id="blocker-1", identifier="BAN-215", state="In Progress"),
    ]
    transitions: list[tuple[str, str]] = []

    monkeypatch.setattr(
        orchestrator,
        "_transition_issue_state_background",
        lambda issue_id, state_name, **kw: transitions.append((issue_id, state_name)),
    )

    orchestrator._maybe_transition_blocked_issue(issue)

    assert transitions == [(issue.id, "Blocked")]


def test_maybe_transition_blocked_issue_skips_when_already_in_blocked_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = _build_orchestrator()
    orchestrator._config.transitions = TransitionsConfig(blocked="Blocked")
    issue = _build_issue(issue_id="issue-2", identifier="BAN-217", state="Blocked")
    issue.blocked_by = [
        BlockerRef(id="blocker-1", identifier="BAN-215", state="In Progress"),
    ]
    transitions: list[tuple[str, str]] = []

    monkeypatch.setattr(
        orchestrator,
        "_transition_issue_state_background",
        lambda issue_id, state_name, **kw: transitions.append((issue_id, state_name)),
    )

    orchestrator._maybe_transition_blocked_issue(issue)

    assert transitions == []


@pytest.mark.asyncio
async def test_request_immediate_poll_coalesces_while_tick_is_running(monkeypatch: pytest.MonkeyPatch) -> None:
    orchestrator = _build_orchestrator()
    started = asyncio.Event()
    release = asyncio.Event()
    runs = 0
    max_concurrency = 0
    active_runs = 0

    async def fake_tick_once() -> None:
        nonlocal runs, max_concurrency, active_runs
        runs += 1
        active_runs += 1
        max_concurrency = max(max_concurrency, active_runs)
        started.set()
        if runs == 1:
            await release.wait()
        active_runs -= 1

    monkeypatch.setattr(orchestrator, "_tick_once", fake_tick_once)
    monkeypatch.setattr(orchestrator, "_schedule_tick", lambda delay_ms=None: None)

    orchestrator._start_tick_task()
    await started.wait()
    tick_task = orchestrator._tick_task

    assert orchestrator.request_immediate_poll() is False
    assert orchestrator.request_immediate_poll() is True

    release.set()
    assert tick_task is not None
    await tick_task

    assert runs == 2
    assert max_concurrency == 1


@pytest.mark.asyncio
async def test_enqueue_tick_reschedules_to_earlier_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    orchestrator = _build_orchestrator()
    started = asyncio.Event()
    runs = 0

    async def fake_tick_once() -> None:
        nonlocal runs
        runs += 1
        started.set()

    monkeypatch.setattr(orchestrator, "_tick_once", fake_tick_once)
    monkeypatch.setattr(orchestrator, "_schedule_tick", lambda delay_ms=None: None)

    assert orchestrator._enqueue_tick(delay_ms=50.0) is False
    assert orchestrator.request_immediate_poll() is False

    await asyncio.wait_for(started.wait(), timeout=0.2)
    await asyncio.sleep(0)

    assert runs == 1


@pytest.mark.asyncio
async def test_schedule_tick_while_running_becomes_single_follow_up(monkeypatch: pytest.MonkeyPatch) -> None:
    orchestrator = _build_orchestrator()
    started = asyncio.Event()
    release = asyncio.Event()
    runs = 0
    max_concurrency = 0
    active_runs = 0

    async def fake_tick_once() -> None:
        nonlocal runs, max_concurrency, active_runs
        runs += 1
        active_runs += 1
        max_concurrency = max(max_concurrency, active_runs)
        started.set()
        if runs == 1:
            await release.wait()
        active_runs -= 1

    monkeypatch.setattr(orchestrator, "_tick_once", fake_tick_once)
    monkeypatch.setattr(orchestrator, "_schedule_tick", lambda delay_ms=None: None)

    orchestrator._start_tick_task()
    await started.wait()
    tick_task = orchestrator._tick_task

    assert orchestrator._enqueue_tick(delay_ms=5.0) is False
    assert orchestrator._enqueue_tick(delay_ms=5.0) is True

    release.set()
    assert tick_task is not None
    await tick_task

    assert runs == 2
    assert max_concurrency == 1


@pytest.mark.asyncio
async def test_fetch_issues_by_states_scopes_requests_to_configured_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = LinearClient(_build_orchestrator()._config.tracker)
    captured_variables: list[dict[str, object]] = []

    class FakeSession:
        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    async def fake_request(self, session, query: str, variables: dict[str, object]) -> dict[str, object]:
        del self, session
        captured_variables.append(variables)
        assert "project: { slugId: { eq: $projectSlug } }" in query
        return {
            "issues": {
                "nodes": [
                    {"id": "1", "identifier": "BAP-153", "state": {"name": "Done"}}
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        }

    monkeypatch.setattr("cymphony.linear.aiohttp.ClientSession", lambda **kwargs: FakeSession())
    monkeypatch.setattr(LinearClient, "_request", fake_request)

    issues = await client.fetch_issues_by_states(["Done"])

    assert [issue.identifier for issue in issues] == ["BAP-153"]
    assert captured_variables == [{"projectSlug": "proj", "states": ["Done"]}]

def test_normalize_issue_reads_project_name() -> None:
    issue = _normalize_issue(
        {
            "id": "issue-148",
            "identifier": "BAP-148",
            "title": "Provider abstraction",
            "project": {"name": "Bandeira"},
            "description": None,
            "priority": 2,
            "state": {"name": "Todo"},
            "branchName": None,
            "url": "https://linear.app/bandeira/issue/BAP-148",
            "labels": {"nodes": []},
            "relations": {"nodes": []},
            "inverseRelations": {"nodes": []},
            "comments": {"nodes": []},
            "createdAt": "2026-03-28T19:11:32.256Z",
            "updatedAt": "2026-03-29T09:01:32.404Z",
        }
    )

    assert issue is not None
    assert issue.project_name == "Bandeira"


def test_normalize_issue_uses_only_blocked_by_relations_for_blockers() -> None:
    issue = _normalize_issue(
        {
            "id": "issue-148",
            "identifier": "BAP-148",
            "title": "Provider abstraction",
            "description": None,
            "priority": 2,
            "state": {"name": "Todo"},
            "branchName": None,
            "url": "https://linear.app/bandeira/issue/BAP-148",
            "labels": {"nodes": []},
            "relations": {
                "nodes": [
                    {
                        "type": "blocks",
                        "relatedIssue": {
                            "id": "issue-149",
                            "identifier": "BAP-149",
                            "state": {"name": "Todo"},
                        },
                    },
                ]
            },
            "inverseRelations": {
                "nodes": [
                    {
                        "type": "blocks",
                        "issue": {
                            "id": "issue-150",
                            "identifier": "BAP-150",
                            "state": {"name": "In Progress"},
                        },
                    }
                ]
            },
            "comments": {"nodes": []},
            "createdAt": "2026-03-28T19:11:32.256Z",
            "updatedAt": "2026-03-29T09:01:32.404Z",
        }
    )

    assert issue is not None
    assert [blocker.identifier for blocker in issue.blocked_by] == ["BAP-150"]
@pytest.mark.asyncio
async def test_startup_terminal_cleanup_removes_only_matching_workspaces_and_logs_project_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    orchestrator = _build_orchestrator()
    orchestrator._config.workspace.root = str(tmp_path)
    wm = WorkspaceManager(orchestrator._config)

    await wm.create_for_issue("BAP-153")
    other_path = tmp_path / "OTHER-1"
    other_path.mkdir()

    async def fake_fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        del self
        assert state_names == ["Done"]
        return [_build_issue(identifier="BAP-153", state="Done"), _build_issue(issue_id="issue-2", identifier="MISSING-1", state="Done")]

    monkeypatch.setattr(LinearClient, "fetch_issues_by_states", fake_fetch_issues_by_states)

    caplog.set_level(logging.INFO)
    await orchestrator._startup_terminal_cleanup()

    assert not wm.get_path("BAP-153").exists()
    assert other_path.exists()
    assert "project_slug=proj" in caplog.text
    assert "matched=2 removed=1" in caplog.text


async def test_continuation_retry_timer_redispatches_after_clearing_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = _build_orchestrator()
    issue = _build_issue()
    dispatched: list[tuple[str, int | None]] = []

    class FakeLinearClient:
        def __init__(self, tracker_config: object) -> None:
            self.tracker_config = tracker_config

        async def fetch_candidate_issues(self) -> list[Issue]:
            return [issue]

    async def fake_dispatch(candidate: Issue, attempt: int | None) -> None:
        dispatched.append((candidate.id, attempt))

    monkeypatch.setattr("cymphony.orchestrator.LinearClient", FakeLinearClient)
    monkeypatch.setattr(orchestrator, "_dispatch_issue", fake_dispatch)

    orchestrator._state.claimed.add(issue.id)
    orchestrator._state.completed.add(issue.id)
    orchestrator._state.retry_attempts[issue.id] = RetryEntry(
        issue_id=issue.id,
        identifier=issue.identifier,
        attempt=1,
        due_at_ms=0.0,
        error=None,
    )

    await orchestrator._on_retry_timer(issue.id)

    assert dispatched == [(issue.id, 1)]
    assert issue.id not in orchestrator._state.claimed
    assert issue.id not in orchestrator._state.completed


@pytest.mark.asyncio
async def test_continuation_retry_timer_reschedules_when_slots_are_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = _build_orchestrator()
    issue = _build_issue()
    scheduled: list[tuple[str, str, int, float | None, str | None]] = []

    class FakeLinearClient:
        def __init__(self, tracker_config: object) -> None:
            self.tracker_config = tracker_config

        async def fetch_candidate_issues(self) -> list[Issue]:
            return [issue]

    async def fake_schedule_retry(
        issue_id: str,
        identifier: str,
        attempt: int,
        delay_ms: float | None = None,
        error: str | None = None,
    ) -> None:
        scheduled.append((issue_id, identifier, attempt, delay_ms, error))

    async def fail_dispatch(candidate: Issue, attempt: int | None) -> None:
        raise AssertionError("dispatch should not be called when slots are unavailable")

    monkeypatch.setattr("cymphony.orchestrator.LinearClient", FakeLinearClient)
    monkeypatch.setattr(orchestrator, "_schedule_retry", fake_schedule_retry)
    monkeypatch.setattr(orchestrator, "_dispatch_issue", fail_dispatch)

    orchestrator._state.max_concurrent_agents = 0
    orchestrator._state.claimed.add(issue.id)
    orchestrator._state.completed.add(issue.id)
    orchestrator._state.retry_attempts[issue.id] = RetryEntry(
        issue_id=issue.id,
        identifier=issue.identifier,
        attempt=1,
        due_at_ms=0.0,
        error=None,
    )

    await orchestrator._on_retry_timer(issue.id)

    assert scheduled == [(issue.id, issue.identifier, 1, 1000.0, None)]
    assert issue.id not in orchestrator._state.claimed
    assert issue.id not in orchestrator._state.completed


@pytest.mark.asyncio
async def test_schedule_retry_uses_continuation_log_action_for_clean_exit(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    orchestrator = _build_orchestrator()

    class FakeLoop:
        def call_later(self, delay: float, callback: object) -> None:
            self.delay = delay
            self.callback = callback

    fake_loop = FakeLoop()
    monkeypatch.setattr("cymphony.orchestrator.asyncio.get_event_loop", lambda: fake_loop)

    with caplog.at_level("INFO"):
        await orchestrator._schedule_retry(
            issue_id="issue-1",
            identifier="BAP-151",
            attempt=1,
            delay_ms=1000.0,
            error=None,
        )

    assert "action=continuation_retry_scheduled" in caplog.text
    assert "action=retry_scheduled" not in caplog.text


def test_snapshot_includes_waiting_reasons_and_recent_problems(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = _build_orchestrator()
    blocker = BlockerRef(id="blocker-1", identifier="BAP-170", state="In Progress")
    blocked_issue = _build_issue(issue_id="issue-2", identifier="BAP-171", state="Todo")
    blocked_issue.blocked_by = [blocker]
    slotted_issue = _build_issue(issue_id="issue-3", identifier="BAP-172", state="In Progress")
    retry_issue = _build_issue(issue_id="issue-4", identifier="BAP-173", state="Todo")

    orchestrator._state.last_candidates = [blocked_issue, slotted_issue, retry_issue]
    orchestrator._state.running["issue-1"] = RunningEntry(
        issue_id="issue-1",
        identifier="BAP-169",
        issue=_build_issue(issue_id="issue-1", identifier="BAP-169", state="In Progress"),
        task=None,
        session=LiveSession(
            session_id=None,
            pid=None,
            last_event=None,
            last_event_timestamp=None,
            last_message="",
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            last_reported_input_tokens=0,
            last_reported_output_tokens=0,
            last_reported_total_tokens=0,
            turn_count=0,
        ),
        retry_attempt=None,
        started_at=datetime(2026, 3, 28, 12, 0, 0, tzinfo=timezone.utc),
        status=RunStatus.STREAMING_TURN,
    )
    orchestrator._state.retry_attempts[retry_issue.id] = RetryEntry(
        issue_id=retry_issue.id,
        identifier=retry_issue.identifier,
        attempt=2,
        due_at_ms=5_000.0,
        error="network blip",
    )
    orchestrator._state.recent_problems.append(
        ProblemRecord(
            kind="invalid_config",
            summary="Dispatch configuration is invalid",
            detail="tracker.project_slug is required",
            observed_at=datetime(2026, 3, 28, 12, 0, 0, tzinfo=timezone.utc),
        )
    )

    monkeypatch.setattr("cymphony.orchestrator._monotonic_ms", lambda: 4_000.0)
    monkeypatch.setattr("cymphony.orchestrator._now_utc", lambda: datetime(2026, 3, 28, 12, 0, 0, tzinfo=timezone.utc))

    snapshot = orchestrator.snapshot()

    waiting_by_issue = {
        row["issue_identifier"]: row
        for row in snapshot["waiting"]
    }
    assert waiting_by_issue["BAP-171"]["kind"] == "blocked_by_dependency"
    assert waiting_by_issue["BAP-171"]["detail"] == "BAP-170 (In Progress)"
    assert waiting_by_issue["BAP-172"]["kind"] == "no_slots_available"
    assert waiting_by_issue["BAP-173"]["kind"] == "waiting_for_retry"
    assert snapshot["problems"][0]["kind"] == "invalid_config"
    assert snapshot["counts"]["waiting"] == 3
    assert snapshot["counts"]["problems"] == 1


def test_snapshot_reports_paused_dispatch_as_distinct_waiting_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = _build_orchestrator()
    paused_issue = _build_issue(issue_id="issue-2", identifier="BAP-171", state="In Progress")
    orchestrator._state.last_candidates = [paused_issue]
    orchestrator._state.dispatch_paused = True

    monkeypatch.setattr(
        "cymphony.orchestrator._now_utc",
        lambda: datetime(2026, 3, 28, 12, 0, 0, tzinfo=timezone.utc),
    )

    snapshot = orchestrator.snapshot()

    assert snapshot["counts"]["waiting"] == 1
    assert snapshot["waiting"][0]["issue_identifier"] == "BAP-171"
    assert snapshot["waiting"][0]["kind"] == "dispatch_paused"
    assert snapshot["waiting"][0]["summary"] == "Dispatching is paused"


def test_pause_dispatching_blocks_slot_availability_and_resume_requeues_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    orchestrator = _build_orchestrator()
    refresh_calls: list[float] = []

    monkeypatch.setattr(orchestrator, "_enqueue_tick", lambda delay_ms: refresh_calls.append(delay_ms) or False)

    pause_result = orchestrator.pause_dispatching()
    resume_result = orchestrator.resume_dispatching()

    assert pause_result["ok"] is True
    assert orchestrator._state.dispatch_paused is False
    assert resume_result["was_paused"] is True
    assert refresh_calls == [0.0]
    assert orchestrator._has_slots() is True
    assert [a.action for a in orchestrator._state.control_actions[-2:]] == [
        "pause_dispatching",
        "resume_dispatching",
    ]


@pytest.mark.asyncio
async def test_skip_issue_marks_issue_as_skipped_and_requeue_clears_it(monkeypatch: pytest.MonkeyPatch) -> None:
    orchestrator = _build_orchestrator()
    issue = _build_issue()
    refresh_calls: list[float] = []

    monkeypatch.setattr(orchestrator, "_enqueue_tick", lambda delay_ms: refresh_calls.append(delay_ms) or False)

    orchestrator._state.retry_attempts[issue.id] = RetryEntry(
        issue_id=issue.id,
        identifier=issue.identifier,
        attempt=1,
        due_at_ms=0.0,
        error="boom",
    )

    skip_result = await orchestrator.skip_issue(issue.identifier)

    assert skip_result["ok"] is True
    assert issue.id in orchestrator._state.skipped
    assert issue.id not in orchestrator._state.retry_attempts
    assert orchestrator._is_dispatch_eligible(issue) is False

    requeue_result = await orchestrator.requeue_issue(issue.identifier)

    assert requeue_result["ok"] is True
    assert issue.id not in orchestrator._state.skipped
    assert refresh_calls == [0.0]


@pytest.mark.asyncio
async def test_cancel_worker_releases_running_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    orchestrator = _build_orchestrator()
    issue = _build_issue()
    terminated: list[tuple[str, bool]] = []

    async def fake_terminate(issue_id: str, cleanup_workspace: bool) -> None:
        terminated.append((issue_id, cleanup_workspace))
        orchestrator._state.running.pop(issue_id, None)

    monkeypatch.setattr(orchestrator, "_terminate_running_issue", fake_terminate)

    orchestrator._state.running[issue.id] = RunningEntry(
        issue_id=issue.id,
        identifier=issue.identifier,
        issue=issue,
        task=None,
        session=LiveSession(
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
        ),
        retry_attempt=None,
        started_at=datetime.now(timezone.utc),
        status=RunStatus.STREAMING_TURN,
    )
    orchestrator._state.claimed.add(issue.id)
    orchestrator._state.completed.add(issue.id)
    orchestrator._state.retry_attempts[issue.id] = RetryEntry(
        issue_id=issue.id,
        identifier=issue.identifier,
        attempt=1,
        due_at_ms=0.0,
        error="old",
    )

    result = await orchestrator.cancel_worker(issue.identifier)

    assert result["ok"] is True
    assert terminated == [(issue.id, False)]
    assert issue.id not in orchestrator._state.claimed
    assert issue.id not in orchestrator._state.completed
    assert issue.id not in orchestrator._state.retry_attempts


@pytest.mark.asyncio
async def test_transition_state_cache_is_scoped_by_team(monkeypatch: pytest.MonkeyPatch) -> None:
    orchestrator = _build_orchestrator()
    state_calls: list[tuple[str, str]] = []
    set_calls: list[tuple[str, str]] = []

    class FakeLinearClient:
        def __init__(self, tracker_config: object) -> None:
            self.tracker_config = tracker_config

        async def fetch_issue_team_id(self, issue_id: str) -> str | None:
            return {
                "issue-a": "team-a",
                "issue-b": "team-b",
            }.get(issue_id)

        async def fetch_team_workflow_state_id(self, team_id: str, state_name: str) -> str | None:
            state_calls.append((team_id, state_name))
            return {
                ("team-a", "In Review"): "state-a-review",
                ("team-b", "In Review"): "state-b-review",
            }.get((team_id, state_name))

        async def set_issue_state(self, issue_id: str, state_id: str) -> None:
            set_calls.append((issue_id, state_id))

    monkeypatch.setattr("cymphony.orchestrator.LinearClient", FakeLinearClient)

    await orchestrator._transition_issue_state("issue-a", "In Review")
    await orchestrator._transition_issue_state("issue-b", "In Review")
    await orchestrator._transition_issue_state("issue-a", "In Review")

    assert state_calls == [
        ("team-a", "In Review"),
        ("team-b", "In Review"),
    ]
    assert set_calls == [
        ("issue-a", "state-a-review"),
        ("issue-b", "state-b-review"),
        ("issue-a", "state-a-review"),
    ]
    assert orchestrator._state_id_cache == {
        ("team-a", "in review"): "state-a-review",
        ("team-b", "in review"): "state-b-review",
    }


@pytest.mark.asyncio
async def test_on_worker_done_waits_for_in_review_transition_before_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = _build_orchestrator()
    issue = _build_issue(state="In Progress")
    entry = RunningEntry(
        issue_id=issue.id,
        identifier=issue.identifier,
        issue=issue,
        task=None,
        session=LiveSession(
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
        ),
        retry_attempt=None,
        started_at=datetime.now(timezone.utc),
    )
    events: list[str] = []

    async def fake_transition(issue_id: str, state_name: str, **kw: object) -> bool:
        events.append(f"transition:{issue_id}:{state_name}")
        return True

    async def fake_schedule_retry(
        issue_id: str,
        identifier: str,
        attempt: int,
        delay_ms: float | None = None,
        error: str | None = None,
        entry: RunningEntry | None = None,
    ) -> None:
        events.append(f"retry:{issue_id}:{attempt}")

    async def fake_worker() -> None:
        return None

    monkeypatch.setattr(orchestrator, "_transition_issue_state", fake_transition)
    monkeypatch.setattr(orchestrator, "_schedule_retry", fake_schedule_retry)

    task = asyncio.create_task(fake_worker())
    await task
    await orchestrator._on_worker_done(issue.id, issue.identifier, entry, task)

    assert events == [
        f"transition:{issue.id}:In Review",
        f"retry:{issue.id}:1",
    ]


@pytest.mark.asyncio
async def test_on_worker_done_preserves_transition_metadata_after_running_entry_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = _build_orchestrator()
    issue = _build_issue(state="In Progress")
    entry = RunningEntry(
        issue_id=issue.id,
        identifier=issue.identifier,
        issue=issue,
        task=None,
        session=LiveSession(
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
        ),
        retry_attempt=None,
        started_at=datetime.now(timezone.utc),
    )
    orchestrator._state.running[issue.id] = entry

    class FakeLinearClient:
        def __init__(self, _config: object) -> None:
            pass

        async def fetch_issue_team_id(self, issue_id: str) -> str:
            assert issue_id == issue.id
            return "team-1"

        async def fetch_team_workflow_state_id(self, team_id: str, state_name: str) -> str:
            assert team_id == "team-1"
            assert state_name == "In Review"
            return "state-1"

        async def set_issue_state(self, issue_id: str, state_id: str) -> None:
            assert issue_id == issue.id
            assert state_id == "state-1"

    async def fake_schedule_retry(
        issue_id: str,
        identifier: str,
        attempt: int,
        delay_ms: float | None = None,
        error: str | None = None,
        entry: RunningEntry | None = None,
    ) -> None:
        return None

    async def fake_worker() -> None:
        return None

    monkeypatch.setattr("cymphony.orchestrator.LinearClient", FakeLinearClient)
    monkeypatch.setattr(orchestrator, "_schedule_retry", fake_schedule_retry)

    task = asyncio.create_task(fake_worker())
    await task
    await orchestrator._on_worker_done(issue.id, issue.identifier, entry, task)

    assert len(orchestrator._state.transition_history) == 1
    record = orchestrator._state.transition_history[0]
    assert record.issue_identifier == issue.identifier
    assert record.from_state == "In Progress"
    assert record.to_state == "In Review"
    assert record.trigger == "success"
    assert record.success is True


@pytest.mark.asyncio
async def test_on_worker_done_uses_custom_success_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the orchestrator honours a non-default success transition."""
    orchestrator = _build_orchestrator()
    orchestrator._config.transitions = TransitionsConfig(success="Completed")
    issue = _build_issue(state="In Progress")
    entry = RunningEntry(
        issue_id=issue.id,
        identifier=issue.identifier,
        issue=issue,
        task=None,
        session=LiveSession(
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
        ),
        retry_attempt=None,
        started_at=datetime.now(timezone.utc),
    )
    events: list[str] = []

    async def fake_transition(issue_id: str, state_name: str, **kw: object) -> bool:
        events.append(f"transition:{issue_id}:{state_name}")
        return True

    async def fake_schedule_retry(
        issue_id: str,
        identifier: str,
        attempt: int,
        delay_ms: float | None = None,
        error: str | None = None,
        entry: RunningEntry | None = None,
    ) -> None:
        events.append(f"retry:{issue_id}:{attempt}")

    async def fake_worker() -> None:
        return None

    monkeypatch.setattr(orchestrator, "_transition_issue_state", fake_transition)
    monkeypatch.setattr(orchestrator, "_schedule_retry", fake_schedule_retry)

    task = asyncio.create_task(fake_worker())
    await task
    await orchestrator._on_worker_done(issue.id, issue.identifier, entry, task)

    assert events == [
        f"transition:{issue.id}:Completed",
        f"retry:{issue.id}:1",
    ]


@pytest.mark.asyncio
async def test_on_worker_done_skips_transition_when_success_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When success transition is disabled, no state change should occur."""
    orchestrator = _build_orchestrator()
    orchestrator._config.transitions = TransitionsConfig(success=None)
    issue = _build_issue(state="In Progress")
    entry = RunningEntry(
        issue_id=issue.id,
        identifier=issue.identifier,
        issue=issue,
        task=None,
        session=LiveSession(
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
        ),
        retry_attempt=None,
        started_at=datetime.now(timezone.utc),
    )
    events: list[str] = []

    async def fake_transition(issue_id: str, state_name: str, **kw: object) -> bool:
        events.append(f"transition:{issue_id}:{state_name}")
        return True

    async def fake_schedule_retry(
        issue_id: str,
        identifier: str,
        attempt: int,
        delay_ms: float | None = None,
        error: str | None = None,
        entry: RunningEntry | None = None,
    ) -> None:
        events.append(f"retry:{issue_id}:{attempt}")

    async def fake_worker() -> None:
        return None

    monkeypatch.setattr(orchestrator, "_transition_issue_state", fake_transition)
    monkeypatch.setattr(orchestrator, "_schedule_retry", fake_schedule_retry)

    task = asyncio.create_task(fake_worker())
    await task
    await orchestrator._on_worker_done(issue.id, issue.identifier, entry, task)

    # No transition event — only the retry
    assert events == [
        f"retry:{issue.id}:1",
    ]


@pytest.mark.asyncio
async def test_retry_releases_blocked_issue_and_applies_blocked_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = _build_orchestrator()
    orchestrator._config.transitions = TransitionsConfig(blocked="Blocked")
    issue = _build_issue(state="Todo")
    issue.blocked_by = [
        BlockerRef(id="blocker-1", identifier="BAP-170", state="In Progress"),
    ]
    orchestrator._state.claimed.add(issue.id)
    orchestrator._state.completed.add(issue.id)
    retry_entry = RetryEntry(
        issue_id=issue.id,
        identifier=issue.identifier,
        attempt=1,
        due_at_ms=0.0,
        error=None,
    )
    orchestrator._state.retry_attempts[issue.id] = retry_entry
    transitions: list[tuple[str, str]] = []

    class FakeLinearClient:
        def __init__(self, tracker_config: TrackerConfig) -> None:
            pass

        async def fetch_candidate_issues(self) -> list[Issue]:
            return [issue]

    monkeypatch.setattr("cymphony.orchestrator.LinearClient", FakeLinearClient)
    monkeypatch.setattr(
        orchestrator,
        "_transition_issue_state_background",
        lambda issue_id, state_name, **kw: transitions.append((issue_id, state_name)),
    )

    await orchestrator._on_retry_timer(issue.id)

    assert transitions == [(issue.id, "Blocked")]
    assert issue.id not in orchestrator._state.claimed
    assert issue.id not in orchestrator._state.completed


@pytest.mark.asyncio
async def test_dispatch_skips_transition_when_dispatch_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When dispatch transition is disabled, no state change should occur on dispatch."""
    orchestrator = _build_orchestrator()
    orchestrator._config.transitions = TransitionsConfig(dispatch=None)
    issue = _build_issue()
    transitions: list[tuple[str, str]] = []

    monkeypatch.setattr(
        orchestrator,
        "_transition_issue_state_background",
        lambda issue_id, state_name, **kw: transitions.append((issue_id, state_name)),
    )

    async def fake_worker(issue, attempt, entry):
        return None

    monkeypatch.setattr(orchestrator, "_worker", fake_worker)

    await orchestrator._dispatch_issue(issue, attempt=None)

    assert transitions == []


@pytest.mark.asyncio
async def test_dispatch_uses_custom_dispatch_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the orchestrator honours a non-default dispatch transition."""
    orchestrator = _build_orchestrator()
    orchestrator._config.transitions = TransitionsConfig(dispatch="Working")
    issue = _build_issue()
    transitions: list[tuple[str, str]] = []

    monkeypatch.setattr(
        orchestrator,
        "_transition_issue_state_background",
        lambda issue_id, state_name, **kw: transitions.append((issue_id, state_name)),
    )

    async def fake_worker(issue, attempt, entry):
        return None

    monkeypatch.setattr(orchestrator, "_worker", fake_worker)

    await orchestrator._dispatch_issue(issue, attempt=None)

    assert transitions == [(issue.id, "Working")]


def test_snapshot_includes_workflow_config_and_transition_history() -> None:
    orchestrator = _build_orchestrator()
    orchestrator._config.transitions = TransitionsConfig(
        dispatch="In Progress",
        success="In Review",
        failure="Failed",
        blocked=None,
        cancelled=None,
    )

    # Record a transition manually
    orchestrator._record_transition(
        "issue-1", "BAP-100", "Todo", "In Progress", "dispatch", success=True,
    )

    snap = orchestrator.snapshot()

    assert "workflow_config" in snap
    wc = snap["workflow_config"]
    assert wc["active_states"] == ["Todo", "In Progress"]
    assert wc["terminal_states"] == ["Done"]
    assert wc["transitions"]["dispatch"] == "In Progress"
    assert wc["transitions"]["success"] == "In Review"
    assert wc["transitions"]["failure"] == "Failed"
    assert wc["transitions"]["blocked"] is None

    assert "transition_history" in snap
    assert len(snap["transition_history"]) == 1
    t = snap["transition_history"][0]
    assert t["issue_identifier"] == "BAP-100"
    assert t["from_state"] == "Todo"
    assert t["to_state"] == "In Progress"
    assert t["trigger"] == "dispatch"
    assert t["success"] is True


def test_record_transition_caps_history_size() -> None:
    orchestrator = _build_orchestrator()
    for i in range(60):
        orchestrator._record_transition(
            f"issue-{i}", f"BAP-{i}", "Todo", "In Progress", "dispatch", success=True,
        )

    assert len(orchestrator._state.transition_history) == 50
    assert orchestrator._state.transition_history[0].issue_identifier == "BAP-59"
