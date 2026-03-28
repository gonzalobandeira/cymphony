from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cymphony.models import (
    AgentConfig,
    CodingAgentConfig,
    Issue,
    PollingConfig,
    RetryEntry,
    ServerConfig,
    ServiceConfig,
    TrackerConfig,
    WorkflowDefinition,
    WorkspaceConfig,
    HooksConfig,
)
from cymphony.orchestrator import Orchestrator


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
        coding_agent=CodingAgentConfig(
            command="codex",
            turn_timeout_ms=1000,
            read_timeout_ms=1000,
            stall_timeout_ms=1000,
            dangerously_skip_permissions=True,
        ),
        server=ServerConfig(port=None),
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
        title="Test issue",
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

    orchestrator._transition_issue_state("issue-a", "In Review")
    orchestrator._transition_issue_state("issue-b", "In Review")
    orchestrator._transition_issue_state("issue-a", "In Review")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

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
