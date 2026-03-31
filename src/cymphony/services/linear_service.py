"""Workflow-oriented Linear operations."""

from __future__ import annotations

from collections.abc import Callable
from typing import MutableMapping

from ..linear import LinearClient
from ..models import Issue, TrackerConfig


class LinearService:
    """Wrap the lower-level Linear client with workflow-specific operations."""

    def __init__(
        self,
        tracker_config: TrackerConfig,
        client_factory: Callable[[], LinearClient] | None = None,
    ) -> None:
        self._tracker_config = tracker_config
        self._client_factory = client_factory or (lambda: LinearClient(tracker_config))

    @property
    def client(self) -> LinearClient:
        """Return a freshly constructed client while migration is in progress."""
        return self._client_factory()

    async def fetch_candidate_issues(self) -> list[Issue]:
        """Return issues eligible for automated processing."""
        return await self.client.fetch_candidate_issues()

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        """Return issues currently in the given states."""
        return await self.client.fetch_issues_by_states(state_names)

    async def fetch_project_team_ids(self) -> list[str]:
        """Return team ids associated with the configured project."""
        return await self.client.fetch_project_team_ids()

    async def fetch_team_workflow_state_names(self, team_id: str) -> list[str]:
        """Return workflow state names for a team."""
        return await self.client.fetch_team_workflow_state_names(team_id)

    async def fetch_team_workflow_state_id(self, team_id: str, state_name: str) -> str | None:
        """Return a workflow state id for a given team/state name pair."""
        return await self.client.fetch_team_workflow_state_id(team_id, state_name)

    async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        """Return current issue records for a set of ids."""
        return await self.client.fetch_issue_states_by_ids(issue_ids)

    async def fetch_issue_team_id(self, issue_id: str) -> str | None:
        """Return the owning team id for an issue."""
        return await self.client.fetch_issue_team_id(issue_id)

    async def resolve_issue_state_id(
        self,
        issue_id: str,
        state_name: str,
        *,
        state_id_cache: MutableMapping[tuple[str, str], str] | None = None,
    ) -> tuple[str | None, str | None]:
        """Resolve ``(team_id, state_id)`` for an issue and workflow state name.

        When ``state_id_cache`` is provided, it is used and updated with keys of
        the form ``(team_id, state_name.lower())``.
        """
        team_id = await self.fetch_issue_team_id(issue_id)
        if not team_id:
            return None, None

        cache_key = (team_id, state_name.lower())
        state_id = state_id_cache.get(cache_key) if state_id_cache is not None else None
        if not state_id:
            state_id = await self.fetch_team_workflow_state_id(team_id, state_name)
            if state_id and state_id_cache is not None:
                state_id_cache[cache_key] = state_id
        return team_id, state_id

    async def set_issue_state_by_name(self, issue_id: str, state_name: str) -> tuple[str | None, str | None]:
        """Resolve and set an issue state by workflow state name.

        Returns ``(team_id, state_id)`` when successful, or ``(team_id, None)``
        if the state could not be resolved for the owning team.
        """
        team_id = await self.fetch_issue_team_id(issue_id)
        if not team_id:
            return None, None
        state_id = await self.fetch_team_workflow_state_id(team_id, state_name)
        if not state_id:
            return team_id, None
        await self.client.set_issue_state(issue_id, state_id)
        return team_id, state_id

    async def transition_issue_state(
        self,
        issue_id: str,
        state_name: str,
        *,
        state_id_cache: MutableMapping[tuple[str, str], str] | None = None,
    ) -> tuple[str | None, str | None]:
        """Resolve and apply an issue state transition by state name."""
        team_id, state_id = await self.resolve_issue_state_id(
            issue_id,
            state_name,
            state_id_cache=state_id_cache,
        )
        if team_id and state_id:
            await self.client.set_issue_state(issue_id, state_id)
        return team_id, state_id

    async def create_comment(self, issue_id: str, body: str) -> str:
        """Create a Linear comment for an issue."""
        return await self.client.create_comment(issue_id, body)

    async def update_comment(self, comment_id: str, body: str) -> bool:
        """Update an existing Linear comment."""
        return await self.client.update_comment(comment_id, body)
