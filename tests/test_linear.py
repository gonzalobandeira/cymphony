from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from cymphony.linear import LinearClient, _normalize_issue_minimal
from cymphony.models import TrackerConfig


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


@pytest.mark.asyncio
async def test_fetch_project_team_ids_paginates_all_project_issues() -> None:
    client = LinearClient(
        TrackerConfig(
            kind="linear",
            endpoint="https://example.test/graphql",
            api_key="test-key",
            project_slug="proj",
            active_states=["Todo"],
            terminal_states=["Done"],
            assignee=None,
        )
    )
    responses = [
        {
            "issues": {
                "nodes": [{"team": {"id": "team-1"}}],
                "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
            }
        },
        {
            "issues": {
                "nodes": [{"team": {"id": "team-2"}}, {"team": {"id": "team-1"}}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        },
    ]

    with patch.object(client, "_request", AsyncMock(side_effect=responses)) as request_mock:
        team_ids = await client.fetch_project_team_ids()

    assert team_ids == ["team-1", "team-2"]
    assert request_mock.await_count == 2


def _make_client() -> LinearClient:
    return LinearClient(
        TrackerConfig(
            kind="linear",
            endpoint="https://example.test/graphql",
            api_key="test-key",
            project_slug="proj",
            active_states=["Todo"],
            terminal_states=["Done"],
            assignee=None,
        )
    )


@pytest.mark.asyncio
async def test_fetch_projects_returns_sorted_list() -> None:
    client = _make_client()
    response = {
        "projects": {
            "nodes": [
                {"id": "p2", "name": "Zeta", "slugId": "zeta-abc"},
                {"id": "p1", "name": "Alpha", "slugId": "alpha-def"},
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }
    with patch.object(client, "_request", AsyncMock(return_value=response)):
        projects = await client.fetch_projects()

    assert len(projects) == 2
    assert projects[0]["name"] == "Alpha"
    assert projects[0]["slugId"] == "alpha-def"
    assert projects[1]["name"] == "Zeta"


@pytest.mark.asyncio
async def test_fetch_projects_paginates() -> None:
    client = _make_client()
    responses = [
        {
            "projects": {
                "nodes": [{"id": "p1", "name": "A", "slugId": "a-1"}],
                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
            }
        },
        {
            "projects": {
                "nodes": [{"id": "p2", "name": "B", "slugId": "b-2"}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        },
    ]
    with patch.object(client, "_request", AsyncMock(side_effect=responses)) as mock:
        projects = await client.fetch_projects()

    assert len(projects) == 2
    assert mock.await_count == 2


@pytest.mark.asyncio
async def test_fetch_projects_skips_nodes_without_slug() -> None:
    client = _make_client()
    response = {
        "projects": {
            "nodes": [
                {"id": "p1", "name": "Good", "slugId": "good-1"},
                {"id": "p2", "name": "Bad", "slugId": ""},
                {"id": "", "name": "NoId", "slugId": "noid-1"},
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }
    with patch.object(client, "_request", AsyncMock(return_value=response)):
        projects = await client.fetch_projects()

    assert len(projects) == 1
    assert projects[0]["slugId"] == "good-1"


@pytest.mark.asyncio
async def test_fetch_members_returns_sorted_list() -> None:
    client = _make_client()
    response = {
        "users": {
            "nodes": [
                {"id": "u2", "displayName": "Zara"},
                {"id": "u1", "displayName": "Alice"},
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }
    with patch.object(client, "_request", AsyncMock(return_value=response)):
        members = await client.fetch_members()

    assert len(members) == 2
    assert members[0]["displayName"] == "Alice"
    assert members[1]["displayName"] == "Zara"


@pytest.mark.asyncio
async def test_fetch_members_skips_empty_names() -> None:
    client = _make_client()
    response = {
        "users": {
            "nodes": [
                {"id": "u1", "displayName": "Bob"},
                {"id": "u2", "displayName": ""},
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }
    with patch.object(client, "_request", AsyncMock(return_value=response)):
        members = await client.fetch_members()

    assert len(members) == 1
    assert members[0]["displayName"] == "Bob"


@pytest.mark.asyncio
async def test_fetch_all_workflow_state_names_deduplicates_and_sorts() -> None:
    client = _make_client()
    response = {
        "workflowStates": {
            "nodes": [
                {"name": "Done"},
                {"name": "Todo"},
                {"name": "In Progress"},
                {"name": "Done"},  # duplicate
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }
    with patch.object(client, "_request", AsyncMock(return_value=response)):
        states = await client.fetch_all_workflow_state_names()

    assert states == ["Done", "In Progress", "Todo"]


@pytest.mark.asyncio
async def test_fetch_all_workflow_state_names_paginates() -> None:
    client = _make_client()
    responses = [
        {
            "workflowStates": {
                "nodes": [{"name": "Todo"}],
                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
            }
        },
        {
            "workflowStates": {
                "nodes": [{"name": "Done"}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        },
    ]
    with patch.object(client, "_request", AsyncMock(side_effect=responses)) as mock:
        states = await client.fetch_all_workflow_state_names()

    assert states == ["Done", "Todo"]
    assert mock.await_count == 2
