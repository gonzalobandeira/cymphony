"""Linear GraphQL adapter (spec §11)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import aiohttp

from .models import BlockerRef, Comment, Issue, TrackerConfig, TrackerError

logger = logging.getLogger(__name__)

_NETWORK_TIMEOUT = aiohttp.ClientTimeout(total=30)
_PAGE_SIZE = 50


# ---------------------------------------------------------------------------
# GraphQL queries
# ---------------------------------------------------------------------------

# Candidate issues query — active states + project slug, paginated
_CANDIDATE_ISSUES_QUERY = """
query CandidateIssues($projectSlug: String!, $states: [String!]!, $after: String, $assignee: String!) {
  issues(
    first: %(page_size)d,
    after: $after,
    filter: {
      project: { slugId: { eq: $projectSlug } },
      state: { name: { in: $states } },
      assignee: { displayName: { eqIgnoreCase: $assignee } }
    },
    orderBy: updatedAt
  ) {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      id
      identifier
      title
      description
      priority
      state { name }
      branchName
      url
      labels { nodes { name } }
      relations {
        nodes {
          type
          relatedIssue {
            id
            identifier
            state { name }
          }
        }
      }
      comments {
        nodes {
          user { name }
          body
          createdAt
        }
      }
      createdAt
      updatedAt
    }
  }
}
""" % {"page_size": _PAGE_SIZE}

_CANDIDATE_ISSUES_QUERY_NO_ASSIGNEE = """
query CandidateIssues($projectSlug: String!, $states: [String!]!, $after: String) {
  issues(
    first: %(page_size)d,
    after: $after,
    filter: {
      project: { slugId: { eq: $projectSlug } },
      state: { name: { in: $states } }
    },
    orderBy: updatedAt
  ) {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      id
      identifier
      title
      description
      priority
      state { name }
      branchName
      url
      labels { nodes { name } }
      relations {
        nodes {
          type
          relatedIssue {
            id
            identifier
            state { name }
          }
        }
      }
      comments {
        nodes {
          user { name }
          body
          createdAt
        }
      }
      createdAt
      updatedAt
    }
  }
}
""" % {"page_size": _PAGE_SIZE}

# Issues by state names (for startup terminal cleanup)
_ISSUES_BY_STATES_QUERY = """
query IssuesByStates($states: [String!]!) {
  issues(
    first: %(page_size)d,
    filter: { state: { name: { in: $states } } }
  ) {
    nodes {
      id
      identifier
      state { name }
    }
  }
}
""" % {"page_size": _PAGE_SIZE}

# Issue state refresh by IDs (for reconciliation)
_ISSUE_STATES_BY_IDS_QUERY = """
query IssueStatesByIds($ids: [ID!]!) {
  issues(
    first: 50,
    filter: { id: { in: $ids } }
  ) {
    nodes {
      id
      identifier
      title
      description
      priority
      state { name }
      branchName
      url
      labels { nodes { name } }
      relations {
        nodes {
          type
          relatedIssue {
            id
            identifier
            state { name }
          }
        }
      }
      comments {
        nodes {
          user { name }
          body
          createdAt
        }
      }
      createdAt
      updatedAt
    }
  }
}
"""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class LinearClient:
    """Async Linear GraphQL client (spec §11)."""

    def __init__(self, config: TrackerConfig) -> None:
        self._config = config

    async def _request(
        self,
        session: aiohttp.ClientSession,
        query: str,
        variables: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a single GraphQL request."""
        try:
            async with session.post(
                self._config.endpoint,
                json={"query": query, "variables": variables},
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise TrackerError(
                        "linear_api_status",
                        f"Linear API returned HTTP {resp.status}: {text[:200]}",
                    )
                body: dict[str, Any] = await resp.json()
        except aiohttp.ClientError as exc:
            raise TrackerError(
                "linear_api_request",
                f"Linear API request failed: {exc}",
            ) from exc

        if "errors" in body and body["errors"]:
            raise TrackerError(
                "linear_graphql_errors",
                f"Linear GraphQL errors: {body['errors']}",
            )

        if "data" not in body:
            raise TrackerError(
                "linear_unknown_payload",
                f"Unexpected Linear response shape: {str(body)[:200]}",
            )

        return body["data"]

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": self._config.api_key,
            "Content-Type": "application/json",
        }

    async def fetch_candidate_issues(self) -> list[Issue]:
        """Fetch issues in active states for the configured project (spec §11.1.1)."""
        issues: list[Issue] = []
        after: str | None = None

        assignee = self._config.assignee
        query = _CANDIDATE_ISSUES_QUERY if assignee else _CANDIDATE_ISSUES_QUERY_NO_ASSIGNEE

        async with aiohttp.ClientSession(
            headers=self._headers(), timeout=_NETWORK_TIMEOUT
        ) as session:
            while True:
                variables: dict[str, Any] = {
                    "projectSlug": self._config.project_slug,
                    "states": self._config.active_states,
                }
                if assignee:
                    variables["assignee"] = assignee
                if after:
                    variables["after"] = after

                data = await self._request(session, query, variables)

                page = data.get("issues") or {}
                nodes = page.get("nodes") or []
                page_info = page.get("pageInfo") or {}

                for node in nodes:
                    issue = _normalize_issue(node)
                    if issue:
                        issues.append(issue)

                has_next = page_info.get("hasNextPage", False)
                if not has_next:
                    break

                end_cursor = page_info.get("endCursor")
                if not end_cursor:
                    raise TrackerError(
                        "linear_missing_end_cursor",
                        "Linear pagination: hasNextPage=true but endCursor is missing",
                    )
                after = end_cursor

        logger.debug(f"action=fetch_candidate_issues count={len(issues)}")
        return issues

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        """Fetch issues in given states (for startup terminal cleanup, spec §11.1.2)."""
        if not state_names:
            return []

        async with aiohttp.ClientSession(
            headers=self._headers(), timeout=_NETWORK_TIMEOUT
        ) as session:
            data = await self._request(
                session,
                _ISSUES_BY_STATES_QUERY,
                {"states": state_names},
            )

        nodes = (data.get("issues") or {}).get("nodes") or []
        issues = [_normalize_issue_minimal(n) for n in nodes if n]
        return [i for i in issues if i is not None]

    async def fetch_workflow_state_id(self, issue_id: str, state_name: str) -> str | None:
        """Return the workflow state ID for state_name on the team that owns the issue."""
        # Step 1: get team ID from the issue
        team_query = """
query IssueTeam($issueId: String!) {
  issue(id: $issueId) {
    team { id }
  }
}
"""
        async with aiohttp.ClientSession(
            headers=self._headers(), timeout=_NETWORK_TIMEOUT
        ) as session:
            data = await self._request(session, team_query, {"issueId": issue_id})

        team_id = ((data.get("issue") or {}).get("team") or {}).get("id")
        if not team_id:
            logger.warning(
                f"action=fetch_workflow_state_id_no_team issue_id={issue_id}"
            )
            return None

        # Step 2: get all workflow states for this team via root-level query
        states_query = """
query TeamWorkflowStates($teamId: String!) {
  workflowStates(filter: { team: { id: { eq: $teamId } } }) {
    nodes { id name }
  }
}
"""
        async with aiohttp.ClientSession(
            headers=self._headers(), timeout=_NETWORK_TIMEOUT
        ) as session:
            data = await self._request(session, states_query, {"teamId": team_id})

        nodes = (data.get("workflowStates") or {}).get("nodes") or []
        logger.info(
            f"action=workflow_states_fetched issue_id={issue_id} "
            f"team_id={team_id} count={len(nodes)} "
            f"names={[n.get('name') for n in nodes]}"
        )
        target = state_name.lower()
        match = next((n for n in nodes if n.get("name", "").lower() == target), None)
        return match["id"] if match else None

    async def set_issue_state(self, issue_id: str, state_id: str) -> None:
        """Update the workflow state of an issue."""
        mutation = """
mutation IssueUpdate($id: String!, $stateId: String!) {
  issueUpdate(id: $id, input: { stateId: $stateId }) {
    success
  }
}
"""
        async with aiohttp.ClientSession(
            headers=self._headers(), timeout=_NETWORK_TIMEOUT
        ) as session:
            result = await self._request(session, mutation, {"id": issue_id, "stateId": state_id})
        success = (result.get("issueUpdate") or {}).get("success")
        logger.info(
            f"action=set_issue_state_result issue_id={issue_id} "
            f"state_id={state_id} success={success}"
        )

    async def create_comment(self, issue_id: str, body: str) -> None:
        """Post a comment on a Linear issue (fire-and-forget friendly)."""
        mutation = """
mutation CommentCreate($issueId: String!, $body: String!) {
  commentCreate(input: { issueId: $issueId, body: $body }) {
    success
  }
}
"""
        async with aiohttp.ClientSession(
            headers=self._headers(), timeout=_NETWORK_TIMEOUT
        ) as session:
            await self._request(session, mutation, {"issueId": issue_id, "body": body})

    async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        """Fetch current state for given issue IDs (for reconciliation, spec §11.1.3)."""
        if not issue_ids:
            return []

        async with aiohttp.ClientSession(
            headers=self._headers(), timeout=_NETWORK_TIMEOUT
        ) as session:
            data = await self._request(
                session,
                _ISSUE_STATES_BY_IDS_QUERY,
                {"ids": issue_ids},
            )

        nodes = (data.get("issues") or {}).get("nodes") or []
        issues = [_normalize_issue(n) for n in nodes if n and n.get("id")]
        return [i for i in issues if i is not None]


# ---------------------------------------------------------------------------
# Normalization helpers (spec §11.3)
# ---------------------------------------------------------------------------

def _normalize_issue(node: dict[str, Any]) -> Issue | None:
    """Normalize a full issue node from Linear into a domain Issue."""
    if not node:
        return None

    issue_id = node.get("id")
    identifier = node.get("identifier")
    title = node.get("title")
    state_obj = node.get("state") or {}
    state = state_obj.get("name") or ""

    if not (issue_id and identifier and title and state):
        return None

    # labels → lowercase
    label_nodes = (node.get("labels") or {}).get("nodes") or []
    labels = [str(ln.get("name", "")).lower() for ln in label_nodes if ln.get("name")]

    # blocked_by from relations of type "blocks" (filter client-side; API has no filter arg)
    relation_nodes = (node.get("relations") or {}).get("nodes") or []
    blocked_by = []
    for rel in relation_nodes:
        if rel.get("type") != "blocks":
            continue
        related = rel.get("relatedIssue") or {}
        blocker_state_obj = related.get("state") or {}
        blocked_by.append(BlockerRef(
            id=related.get("id"),
            identifier=related.get("identifier"),
            state=blocker_state_obj.get("name"),
        ))

    # priority → int or null
    priority_raw = node.get("priority")
    try:
        priority: int | None = int(priority_raw) if priority_raw is not None else None
    except (TypeError, ValueError):
        priority = None

    # comments
    comment_nodes = (node.get("comments") or {}).get("nodes") or []
    comments = [
        Comment(
            author=str((cn.get("user") or {}).get("name") or "Unknown"),
            body=str(cn.get("body") or ""),
            created_at=_parse_dt(cn.get("createdAt")),
        )
        for cn in comment_nodes
    ]

    return Issue(
        id=issue_id,
        identifier=identifier,
        title=title,
        description=node.get("description"),
        priority=priority,
        state=state,
        branch_name=node.get("branchName"),
        url=node.get("url"),
        labels=labels,
        blocked_by=blocked_by,
        comments=comments,
        created_at=_parse_dt(node.get("createdAt")),
        updated_at=_parse_dt(node.get("updatedAt")),
    )


def _normalize_issue_minimal(node: dict[str, Any]) -> Issue | None:
    """Normalize a minimal issue node (id, identifier, state only)."""
    if not node:
        return None
    issue_id = node.get("id")
    identifier = node.get("identifier")
    state_obj = node.get("state") or {}
    state = state_obj.get("name") or ""
    if not (issue_id and identifier):
        return None
    return Issue(
        id=issue_id,
        identifier=identifier,
        title="",
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


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
