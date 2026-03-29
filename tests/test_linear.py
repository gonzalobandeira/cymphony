from __future__ import annotations

from datetime import datetime, timezone

from cymphony.linear import _normalize_issue_minimal


def test_normalize_issue_minimal_populates_enriched_fields() -> None:
    node = {
        "id": "abc-123",
        "identifier": "BAP-300",
        "title": "Fix the widget",
        "project": {"name": "Bandeira"},
        "state": {"name": "Done"},
        "url": "https://linear.test/BAP-300",
        "updatedAt": "2026-03-28T15:30:00.000Z",
    }
    issue = _normalize_issue_minimal(node)
    assert issue is not None
    assert issue.id == "abc-123"
    assert issue.identifier == "BAP-300"
    assert issue.title == "Fix the widget"
    assert issue.project_name == "Bandeira"
    assert issue.state == "Done"
    assert issue.url == "https://linear.test/BAP-300"
    assert issue.updated_at == datetime(2026, 3, 28, 15, 30, tzinfo=timezone.utc)


def test_normalize_issue_minimal_handles_missing_optional_fields() -> None:
    node = {
        "id": "abc-456",
        "identifier": "BAP-301",
        "state": {"name": "Cancelled"},
    }
    issue = _normalize_issue_minimal(node)
    assert issue is not None
    assert issue.title == ""
    assert issue.project_name is None
    assert issue.url is None
    assert issue.updated_at is None
