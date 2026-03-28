from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cymphony.models import (
    AgentConfig,
    CodingAgentConfig,
    PollingConfig,
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
