from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cymphony import server
from cymphony.models import BlockerRef, Issue
from cymphony.server import _build_operator_groups, _render_dashboard


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
    assert "network blip" in html
    assert "Waiting Reasons (2)" in html
    assert "Blocked by dependency" in html
    assert "BAP-170 (In Progress)" in html
    assert "in 15s" in html
    assert "Recent Problems (1)" in html
    assert "tracker.project_slug is required" in html
