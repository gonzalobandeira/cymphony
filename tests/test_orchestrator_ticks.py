from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from cymphony.linear import LinearClient
from cymphony.models import (
    AgentConfig,
    CodingAgentConfig,
    Issue,
    PollingConfig,
    ServerConfig,
    ServiceConfig,
    TrackerConfig,
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


def _minimal_issue(identifier: str, *, issue_id: str = "issue-1", state: str = "Done") -> Issue:
    return Issue(
        id=issue_id,
        identifier=identifier,
        title=identifier,
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
        return [_minimal_issue("BAP-153"), _minimal_issue("MISSING-1", issue_id="issue-2")]

    monkeypatch.setattr(LinearClient, "fetch_issues_by_states", fake_fetch_issues_by_states)

    caplog.set_level(logging.INFO)
    await orchestrator._startup_terminal_cleanup()

    assert not wm.get_path("BAP-153").exists()
    assert other_path.exists()
    assert "project_slug=proj" in caplog.text
    assert "matched=2 removed=1" in caplog.text
