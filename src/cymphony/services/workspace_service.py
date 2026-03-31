"""Execution and QA worktree lifecycle helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from ..models import Issue, WorkspaceConfig
from ..workspace import sanitize_workspace_key


@dataclass(frozen=True)
class WorkspacePaths:
    """Paths used by the refactored workflow services."""

    execution_root: Path
    qa_root: Path


class WorkspaceService:
    """Manage execution and QA worktree locations.

    Execution worktrees are persistent per issue.
    QA worktrees are created fresh for each run.
    """

    def __init__(self, config: WorkspaceConfig) -> None:
        self._root = Path(config.root)

    def paths(self) -> WorkspacePaths:
        """Return the configured workspace roots."""
        return WorkspacePaths(
            execution_root=self._root,
            qa_root=self._root / "qa",
        )

    def execution_path_for(self, issue: Issue) -> Path:
        """Return the persistent execution worktree path for an issue."""
        return self.paths().execution_root / issue.identifier

    def execution_path_for_identifier(self, identifier: str) -> Path:
        """Return the persistent execution worktree path for an issue identifier."""
        return self.paths().execution_root / sanitize_workspace_key(identifier)

    def fresh_qa_path_for(self, issue: Issue, run_id: str | None = None) -> Path:
        """Return a fresh QA worktree path for a review run."""
        resolved_run_id = run_id or uuid4().hex
        return self.paths().qa_root / issue.identifier / resolved_run_id

    def qa_issue_root_for(self, issue: Issue) -> Path:
        """Return the QA workspace root for an issue."""
        return self.paths().qa_root / issue.identifier
