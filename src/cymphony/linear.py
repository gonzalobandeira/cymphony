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
      project { name }
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
      inverseRelations {
        nodes {
          type
          issue {
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
      project { name }
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
      inverseRelations {
        nodes {
          type
          issue {
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

# Project-scoped issues by state names (for startup terminal cleanup)
_PROJECT_ISSUES_BY_STATES_QUERY = """
query ProjectIssuesByStates($projectSlug: String!, $states: [String!]!, $after: String) {
  issues(
    first: %(page_size)d,
    after: $after,
    filter: {
      project: { slugId: { eq: $projectSlug } },
      state: { name: { in: $states } }
    }
  ) {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      id
      identifier
      title
      project { name }
      state { name }
      url
      updatedAt
    }
  }
}
""" % {"page_size": _PAGE_SIZE}

# Project-scoped teams (for transition validation)
_PROJECT_TEAMS_QUERY = """
query ProjectTeams($projectSlug: String!, $after: String) {
  issues(
    first: %(page_size)d,
    after: $after,
    filter: { project: { slugId: { eq: $projectSlug } } }
  ) {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      team { id }
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
      project { name }
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
      inverseRelations {
        nodes {
          type
          issue {
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
        """Fetch project-scoped issues in given states (spec §11.1.2)."""
        if not state_names:
            return []

        issues: list[Issue] = []
        after: str | None = None

        async with aiohttp.ClientSession(
            headers=self._headers(), timeout=_NETWORK_TIMEOUT
        ) as session:
            while True:
                variables: dict[str, Any] = {
                    "projectSlug": self._config.project_slug,
                    "states": state_names,
                }
                if after:
                    variables["after"] = after

                data = await self._request(
                    session,
                    _PROJECT_ISSUES_BY_STATES_QUERY,
                    variables,
                )

                page = data.get("issues") or {}
                nodes = page.get("nodes") or []
                page_info = page.get("pageInfo") or {}

                normalized = [_normalize_issue_minimal(n) for n in nodes if n]
                issues.extend(i for i in normalized if i is not None)

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

        logger.debug(
            "action=fetch_issues_by_states "
            f"project_slug={self._config.project_slug} count={len(issues)}"
        )
        return issues

    async def fetch_project_team_ids(self) -> list[str]:
        """Return all team IDs that have issues in the configured project."""
        team_ids: set[str] = set()
        after: str | None = None

        async with aiohttp.ClientSession(
            headers=self._headers(), timeout=_NETWORK_TIMEOUT
        ) as session:
            while True:
                variables: dict[str, Any] = {
                    "projectSlug": self._config.project_slug,
                }
                if after:
                    variables["after"] = after

                data = await self._request(session, _PROJECT_TEAMS_QUERY, variables)
                page = data.get("issues") or {}
                nodes = page.get("nodes") or []
                page_info = page.get("pageInfo") or {}

                for node in nodes:
                    tid = (node.get("team") or {}).get("id")
                    if tid:
                        team_ids.add(tid)

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

        return sorted(team_ids)

    async def fetch_team_workflow_state_names(self, team_id: str) -> list[str]:
        """Return all workflow state names for a Linear team."""
        query = """
query TeamWorkflowStates($teamId: ID!) {
  workflowStates(filter: { team: { id: { eq: $teamId } } }) {
    nodes { id name }
  }
}
"""
        async with aiohttp.ClientSession(
            headers=self._headers(), timeout=_NETWORK_TIMEOUT
        ) as session:
            data = await self._request(session, query, {"teamId": team_id})

        nodes = (data.get("workflowStates") or {}).get("nodes") or []
        return [n.get("name") for n in nodes if n.get("name")]

    async def fetch_issue_team_id(self, issue_id: str) -> str | None:
        """Return the owning Linear team ID for an issue."""
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
        return team_id

    async def fetch_team_workflow_state_id(self, team_id: str, state_name: str) -> str | None:
        """Return a workflow state ID by name for a specific Linear team."""
        states_query = """
query TeamWorkflowStates($teamId: ID!) {
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
            "action=workflow_states_fetched "
            f"team_id={team_id} count={len(nodes)} "
            f"names={[n.get('name') for n in nodes]}"
        )
        target = state_name.lower()
        match = next((n for n in nodes if n.get("name", "").lower() == target), None)
        return match["id"] if match else None

    async def fetch_workflow_state_ref(self, issue_id: str, state_name: str) -> tuple[str | None, str | None]:
        """Return ``(team_id, state_id)`` for a workflow state on the issue's owning team."""
        team_id = await self.fetch_issue_team_id(issue_id)
        if not team_id:
            return None, None

        state_id = await self.fetch_team_workflow_state_id(team_id, state_name)
        return team_id, state_id

    async def fetch_workflow_state_id(self, issue_id: str, state_name: str) -> str | None:
        """Return the workflow state ID for state_name on the team that owns the issue."""
        _, state_id = await self.fetch_workflow_state_ref(issue_id, state_name)
        return state_id

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
        if not success:
            raise TrackerError(
                "linear_issue_state_update_failed",
                f"issueUpdate returned success={success!r} for issue {issue_id}",
            )

    async def create_comment(self, issue_id: str, body: str) -> str:
        """Create a comment on an issue and return the comment ID."""
        mutation = """
mutation CommentCreate($issueId: String!, $body: String!) {
  commentCreate(input: { issueId: $issueId, body: $body }) {
    success
    comment { id }
  }
}
"""
        async with aiohttp.ClientSession(
            headers=self._headers(), timeout=_NETWORK_TIMEOUT
        ) as session:
            result = await self._request(session, mutation, {"issueId": issue_id, "body": body})

        comment_id = ((result.get("commentCreate") or {}).get("comment") or {}).get("id")
        if not comment_id:
            raise TrackerError(
                "linear_comment_create_failed",
                "commentCreate returned no comment ID",
            )
        return comment_id

    async def update_comment(self, comment_id: str, body: str) -> bool:
        """Update an existing comment body. Returns True on success."""
        mutation = """
mutation CommentUpdate($id: String!, $body: String!) {
  commentUpdate(id: $id, input: { body: $body }) {
    success
  }
}
"""
        async with aiohttp.ClientSession(
            headers=self._headers(), timeout=_NETWORK_TIMEOUT
        ) as session:
            result = await self._request(session, mutation, {"id": comment_id, "body": body})

        return bool((result.get("commentUpdate") or {}).get("success"))

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

    # Linear exposes blockers through inverseRelations. For a blocker edge,
    # inverseRelations[].issue is the upstream blocking issue and relatedIssue is self.
    relation_nodes = (node.get("inverseRelations") or {}).get("nodes") or []
    blocked_by = []
    for rel in relation_nodes:
        if rel.get("type") != "blocks":
            continue
        related = rel.get("issue") or {}
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
        project_name=((node.get("project") or {}).get("name")),
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
    """Normalize a minimal issue node (id, identifier, state, and optional enrichment fields)."""
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
        title=node.get("title") or "",
        project_name=((node.get("project") or {}).get("name")),
        description=None,
        priority=None,
        state=state,
        branch_name=None,
        url=node.get("url"),
        labels=[],
        blocked_by=[],
        comments=[],
        created_at=None,
        updated_at=_parse_dt(node.get("updatedAt")),
    )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
