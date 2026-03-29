from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from aiohttp import web

from cymphony import server
from cymphony.models import BlockerRef, Issue
from cymphony.server import _build_operator_groups, _render_dashboard
from cymphony.workflow import load_workflow


def _issue(
    *,
    issue_id: str,
    identifier: str,
    state: str = "Todo",
    priority: int | None = 2,
    blocked_by: list[BlockerRef] | None = None,
    updated_at: datetime | None = None,
) -> Issue:
    return Issue(
        id=issue_id,
        identifier=identifier,
        title=f"Title for {identifier}",
        description=None,
        priority=priority,
        state=state,
        branch_name=None,
        url=f"https://linear.test/{identifier}",
        labels=[],
        blocked_by=blocked_by or [],
        comments=[],
        created_at=datetime(2026, 3, 28, 20, 0, tzinfo=timezone.utc),
        updated_at=updated_at or datetime(2026, 3, 28, 21, 0, tzinfo=timezone.utc),
    )


class _FakeOrchestrator:
    def __init__(self, snapshot: dict) -> None:
        self._snapshot = snapshot
        self.called: list[tuple[str, str | None]] = []

    def snapshot(self) -> dict:
        return self._snapshot

    def trigger_refresh(self) -> dict:
        self.called.append(("refresh", None))
        return {"ok": True, "action": "refresh", "scope": "global", "coalesced": False}

    def pause_dispatching(self) -> dict:
        self.called.append(("pause", None))
        return {"ok": True, "action": "pause_dispatching", "scope": "global"}

    def resume_dispatching(self) -> dict:
        self.called.append(("resume", None))
        return {"ok": True, "action": "resume_dispatching", "scope": "global"}

    async def shutdown_app(self) -> dict:
        self.called.append(("shutdown", None))
        return {"ok": True, "action": "shutdown_app", "scope": "global"}

    async def cancel_worker(self, identifier: str) -> dict:
        self.called.append(("cancel", identifier))
        return {"ok": True, "action": "cancel_worker", "issue_identifier": identifier}

    async def requeue_issue(self, identifier: str) -> dict:
        self.called.append(("requeue", identifier))
        return {"ok": True, "action": "requeue_issue", "issue_identifier": identifier}

    async def skip_issue(self, identifier: str) -> dict:
        self.called.append(("skip", identifier))
        return {"ok": True, "action": "skip_issue", "issue_identifier": identifier}


class _FakeRequest:
    def __init__(
        self,
        orchestrator: _FakeOrchestrator | None = None,
        identifier: str | None = None,
        *,
        app: dict | None = None,
        post_data: dict | None = None,
        query: dict | None = None,
    ) -> None:
        self.app = app or {"orchestrator": orchestrator}
        self.match_info = {}
        self._post_data = post_data or {}
        self.query = query or {}
        if identifier is not None:
            self.match_info["identifier"] = identifier

    async def post(self) -> dict:
        return self._post_data


def test_build_operator_groups_classifies_ready_waiting_blocked_and_recently_completed() -> None:
    snapshot = {
        "generated_at": "2026-03-28T21:05:00+00:00",
        "running": [
            {
                "issue_id": "issue-running",
                "issue_identifier": "BAP-100",
                "state": "Todo",
                "run_status": "StreamingTurn",
            }
        ],
        "retrying": [
            {
                "issue_id": "issue-retrying",
                "issue_identifier": "BAP-101",
                "attempt": 2,
                "due_at": "2026-03-28T21:06:00+00:00",
                "error": "agent crashed",
            }
        ],
        "codex_totals": {"total_tokens": 1200, "seconds_running": 45},
    }
    active_issues = [
        _issue(issue_id="issue-running", identifier="BAP-100", priority=1),
        _issue(issue_id="issue-ready", identifier="BAP-102", priority=1),
        _issue(issue_id="issue-waiting", identifier="BAP-103", priority=2),
        _issue(
            issue_id="issue-blocked",
            identifier="BAP-104",
            priority=1,
            blocked_by=[BlockerRef(id="2", identifier="BAP-099", state="In Progress")],
        ),
    ]
    completed_issues = [
        _issue(
            issue_id="issue-done",
            identifier="BAP-105",
            state="Done",
            updated_at=datetime(2026, 3, 28, 21, 4, tzinfo=timezone.utc),
        )
    ]

    groups = _build_operator_groups(
        snapshot,
        active_issues,
        completed_issues,
        max_concurrent_agents=2,
        max_concurrent_agents_by_state={},
        active_states=["Todo", "In Progress"],
        terminal_states=["Done"],
    )

    assert [item["identifier"] for item in groups["ready"]] == ["BAP-102"]
    assert [item["identifier"] for item in groups["waiting"]] == ["BAP-103"]
    assert groups["waiting"][0]["reason"] == "Waiting for global capacity"
    assert [item["identifier"] for item in groups["blocked"]] == ["BAP-104"]
    assert groups["blocked"][0]["reason"] == "Waiting on BAP-099"
    assert [item["identifier"] for item in groups["recently_completed"]] == ["BAP-105"]
    assert groups["summary"]["needs_attention"] == 2


def test_build_operator_groups_respects_state_capacity_limits() -> None:
    snapshot = {
        "generated_at": "2026-03-28T21:05:00+00:00",
        "running": [
            {
                "issue_id": "issue-running",
                "issue_identifier": "BAP-100",
                "state": "Todo",
                "run_status": "StreamingTurn",
            }
        ],
        "retrying": [],
        "codex_totals": {},
    }
    active_issues = [
        _issue(issue_id="issue-ready", identifier="BAP-102", state="In Progress", priority=1),
        _issue(issue_id="issue-waiting", identifier="BAP-103", state="Todo", priority=2),
    ]

    groups = _build_operator_groups(
        snapshot,
        active_issues,
        [],
        max_concurrent_agents=3,
        max_concurrent_agents_by_state={"todo": 1},
        active_states=["Todo", "In Progress"],
        terminal_states=["Done"],
    )

    assert [item["identifier"] for item in groups["ready"]] == ["BAP-102"]
    assert [item["identifier"] for item in groups["waiting"]] == ["BAP-103"]
    assert groups["waiting"][0]["reason"] == "Waiting for Todo capacity"


def test_format_relative_due_formats_countdown_and_due_now() -> None:
    now = datetime(2026, 3, 28, 12, 0, 0, tzinfo=timezone.utc)

    assert server._format_relative_due("2026-03-28T12:01:30+00:00", now) == "1m 30s"
    assert server._format_relative_due("2026-03-28T12:00:00+00:00", now) == "Now"


def test_render_dashboard_shows_waiting_reasons_and_recent_problems(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 3, 28, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(server, "_now_utc", lambda: now)

    html = _render_dashboard(
        {
            "generated_at": now.isoformat(),
            "summary": {
                "running": 0,
                "retrying": 1,
                "ready": 0,
                "waiting": 0,
                "needs_attention": 1,
                "capacity_in_use": "0/2",
            },
            "totals": {},
            "controls": {
                "dispatch_paused": True,
                "shutdown_requested": False,
                "recent_actions": [
                    {
                        "timestamp": now.isoformat(),
                        "action": "pause_dispatching",
                        "scope": "global",
                        "outcome": "accepted",
                        "issue_identifier": None,
                        "detail": "dispatching paused",
                    }
                ],
            },
            "running": [],
            "retrying": [
                {
                    "issue_identifier": "BAP-154",
                    "attempt": 2,
                    "due_at": (now + timedelta(seconds=90)).isoformat(),
                    "error": "network blip",
                }
            ],
            "ready": [],
            "waiting": [],
            "blocked": [],
            "recently_completed": [],
            "skipped": [
                {
                    "issue_identifier": "BAP-155",
                    "reason": "operator_skip",
                    "created_at": now.isoformat(),
                }
            ],
            "waiting_reasons": [
                {
                    "issue_identifier": "BAP-171",
                    "summary": "Blocked by dependency",
                    "detail": "BAP-170 (In Progress)",
                    "due_at": None,
                },
                {
                    "issue_identifier": "BAP-172",
                    "summary": "Waiting for retry timer",
                    "detail": "network blip",
                    "due_at": (now + timedelta(seconds=15)).isoformat(),
                },
            ],
            "recent_problems": [
                {
                    "issue_identifier": "BAP-173",
                    "summary": "Dispatch configuration is invalid",
                    "detail": "tracker.project_slug is required",
                    "observed_at": now.isoformat(),
                }
            ],
        }
    )

    assert "BAP-154" in html
    assert "1m 30s" in html
    assert "Pause Dispatching" in html
    assert "Resume Dispatching" in html
    assert "Arm kill" in html
    assert "Kill App" in html
    assert "Paused" in html
    assert "BAP-155" in html
    assert "Waiting Reasons (2)" in html
    assert "Recent Problems (1)" in html
    assert "pause_dispatching" in html


@pytest.mark.asyncio
async def test_refresh_handler_delegates_to_orchestrator() -> None:
    orchestrator = _FakeOrchestrator(
        {"running": [], "retrying": [], "waiting": [], "problems": [], "skipped": [], "controls": {}, "codex_totals": {}}
    )

    response = await server._handle_refresh(_FakeRequest(orchestrator))

    assert response.status == 202
    assert orchestrator.called == [("refresh", None)]
    assert '"action": "refresh"' in response.text


@pytest.mark.asyncio
async def test_shutdown_handler_requires_confirm_switch() -> None:
    orchestrator = _FakeOrchestrator(
        {"running": [], "retrying": [], "waiting": [], "problems": [], "skipped": [], "controls": {}, "codex_totals": {}}
    )

    response = await server._handle_shutdown_app(_FakeRequest(orchestrator))

    assert response.status == 400
    assert orchestrator.called == []
    assert "confirm_kill must be enabled" in response.text


@pytest.mark.asyncio
async def test_shutdown_handler_delegates_to_orchestrator_when_switch_is_enabled() -> None:
    orchestrator = _FakeOrchestrator(
        {"running": [], "retrying": [], "waiting": [], "problems": [], "skipped": [], "controls": {}, "codex_totals": {}}
    )

    response = await server._handle_shutdown_app(
        _FakeRequest(orchestrator, post_data={"confirm_kill": "true"})
    )

    assert response.status == 202
    assert orchestrator.called == [("shutdown", None)]
    assert '"action": "shutdown_app"' in response.text


@pytest.mark.asyncio
async def test_issue_control_handlers_delegate_to_orchestrator() -> None:
    orchestrator = _FakeOrchestrator(
        {"running": [], "retrying": [], "waiting": [], "problems": [], "skipped": [], "controls": {}, "codex_totals": {}}
    )

    cancel_response = await server._handle_cancel_worker(_FakeRequest(orchestrator, "bap-172"))
    requeue_response = await server._handle_requeue_issue(_FakeRequest(orchestrator, "bap-172"))
    skip_response = await server._handle_skip_issue(_FakeRequest(orchestrator, "bap-172"))

    assert cancel_response.status == 202
    assert requeue_response.status == 202
    assert skip_response.status == 202
    assert orchestrator.called == [
        ("cancel", "bap-172"),
        ("requeue", "bap-172"),
        ("skip", "bap-172"),
    ]


@pytest.mark.asyncio
async def test_issue_endpoint_returns_rich_running_drilldown() -> None:
    request = _FakeRequest(
        _FakeOrchestrator(
            {
                "running": [
                    {
                        "issue_id": "issue-1",
                        "issue_identifier": "BAP-170",
                        "issue_title": "Per-issue drill-down",
                        "issue_url": "https://linear.app/bandeira/issue/BAP-170",
                        "issue_description": "Inspect runtime state",
                        "issue_labels": ["Feature"],
                        "issue_comments": [
                            {
                                "author": "Gonzalo",
                                "body": "Please add recent events",
                                "created_at": "2026-03-28T12:00:00+00:00",
                            }
                        ],
                        "state": "In Progress",
                        "run_status": "StreamingTurn",
                        "session_id": "sess-123",
                        "turn_count": 3,
                        "last_event": "notification",
                        "last_message": "Writing tests",
                        "started_at": "2026-03-28T11:00:00+00:00",
                        "last_event_at": "2026-03-28T11:05:00+00:00",
                        "retry_attempt": None,
                        "workspace_path": "/tmp/BAP-170",
                        "plan_comment_id": "comment-1",
                        "latest_plan": "**Agent Plan**\n- [ ] Add drill-down",
                        "recent_events": [
                            {
                                "event": "notification",
                                "timestamp": "2026-03-28T11:05:00+00:00",
                                "message": "Writing tests",
                            }
                        ],
                        "tokens": {
                            "input_tokens": 100,
                            "output_tokens": 50,
                            "total_tokens": 150,
                        },
                    }
                ],
                "retrying": [],
                "waiting": [],
                "problems": [],
                "skipped": [],
                "controls": {},
                "codex_totals": {},
            }
        ),
        identifier="bap-170",
    )

    response = await server._handle_issue(request)

    assert response.status == 200
    payload = response.text
    assert '"tracked": true' in payload
    assert '"status": "running"' in payload
    assert '"latest_plan": "**Agent Plan**\\n- [ ] Add drill-down"' in payload
    assert '"recent_events": [{"event": "notification"' in payload


def test_render_dashboard_shows_issue_drilldown_details() -> None:
    html = _render_dashboard(
        {
            "generated_at": "2026-03-28T12:00:00+00:00",
            "summary": {
                "running": 1,
                "retrying": 0,
                "ready": 0,
                "waiting": 0,
                "needs_attention": 0,
                "capacity_in_use": "1/2",
            },
            "totals": {},
            "controls": {"dispatch_paused": False, "shutdown_requested": False, "recent_actions": []},
            "running": [
                {
                    "issue_id": "issue-1",
                    "issue_identifier": "BAP-170",
                    "issue_title": "Per-issue drill-down",
                    "issue_url": "https://linear.app/bandeira/issue/BAP-170",
                    "issue_description": "Inspect runtime state",
                    "issue_labels": ["Feature"],
                    "issue_comments": [],
                    "state": "In Progress",
                    "run_status": "StreamingTurn",
                    "session_id": "sess-123",
                    "turn_count": 3,
                    "last_event": "notification",
                    "last_message": "Writing tests",
                    "started_at": "2026-03-28T11:00:00+00:00",
                    "last_event_at": "2026-03-28T11:05:00+00:00",
                    "retry_attempt": None,
                    "workspace_path": "/tmp/BAP-170",
                    "plan_comment_id": "comment-1",
                    "latest_plan": "**Agent Plan**\n- [ ] Add drill-down",
                    "recent_events": [
                        {
                            "event": "notification",
                            "timestamp": "2026-03-28T11:05:00+00:00",
                            "message": "Writing tests",
                        }
                    ],
                    "tokens": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "total_tokens": 150,
                    },
                }
            ],
            "retrying": [],
            "ready": [],
            "waiting": [],
            "blocked": [],
            "recently_completed": [],
            "waiting_reasons": [],
            "recent_problems": [],
            "skipped": [],
        }
    )

    assert "<details" in html
    assert "Recent Events" in html
    assert "Plan comment" in html
    assert "Writing tests" in html


@pytest.mark.asyncio
async def test_setup_get_renders_setup_form_with_error(tmp_path: Path) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    request = _FakeRequest(
        app={
            "orchestrator": None,
            "workflow_path": workflow_path,
            "setup_mode": True,
            "setup_error": "tracker.project_slug is required",
        }
    )

    response = await server._handle_setup_get(request)

    assert response.status == 200
    assert "Set Up Cymphony" in response.text
    assert "tracker.project_slug is required" in response.text
    assert str(workflow_path) in response.text


@pytest.mark.asyncio
async def test_setup_post_writes_workflow_file(tmp_path: Path) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    request = _FakeRequest(
        app={
            "orchestrator": None,
            "workflow_path": workflow_path,
            "setup_mode": True,
            "setup_error": None,
        },
        post_data={
            "tracker_api_key": "$LINEAR_API_KEY",
            "project_slug": "cymphony-b2a8d0064141",
            "assignee": "gonzalobandeira",
            "active_states": "Todo, In Progress",
            "terminal_states": "Done, Cancelled",
            "poll_interval_ms": "30000",
            "workspace_root": "~/cymphony-workspaces",
            "max_concurrent_agents": "3",
            "max_turns": "20",
            "max_retry_backoff_ms": "300000",
            "command": "claude",
            "turn_timeout_ms": "3600000",
            "stall_timeout_ms": "300000",
            "dangerously_skip_permissions": "1",
            "after_create": "git clone git@github.com:org/repo.git .",
            "before_run": "git fetch origin",
            "after_run": "git status",
            "before_remove": "",
            "hooks_timeout_ms": "120000",
            "server_port": "8080",
            "prompt_template": "You are working on {{ issue.identifier }}.",
        },
    )

    with pytest.raises(web.HTTPFound) as exc_info:
        await server._handle_setup_post(request)

    assert exc_info.value.location == "/setup?saved=1"
    saved = load_workflow(workflow_path)
    assert saved.config["tracker"]["project_slug"] == "cymphony-b2a8d0064141"
    assert saved.config["tracker"]["assignee"] == "gonzalobandeira"
    assert saved.config["codex"]["command"] == "claude"
    assert saved.prompt_template == "You are working on {{ issue.identifier }}."


@pytest.mark.asyncio
async def test_settings_get_redirects_to_setup_when_in_setup_mode(tmp_path: Path) -> None:
    request = _FakeRequest(
        app={
            "orchestrator": None,
            "workflow_path": tmp_path / "WORKFLOW.md",
            "setup_mode": True,
            "setup_error": None,
        }
    )

    with pytest.raises(web.HTTPFound) as exc_info:
        await server._handle_settings_get(request)

    assert exc_info.value.location == "/setup"
