"""Branch and pull request operations for execution workflow."""

from __future__ import annotations


class PRService:
    """Owns branch, push, and PR lifecycle policy.

    This starts as a placeholder boundary. The current implementation still
    relies heavily on hooks and legacy orchestration logic.
    """

    def issue_branch_name(self, issue_identifier: str) -> str:
        """Return the canonical branch name for an issue."""
        return f"agent/{issue_identifier.lower()}"

