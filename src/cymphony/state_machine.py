"""Workflow selection and allowed Linear state transitions."""

from __future__ import annotations

from dataclasses import dataclass

from .models import Issue

@dataclass(frozen=True)
class WorkflowRoute:
    """Resolved workflow routing decision for an issue."""

    workflow: str
    target_state: str


@dataclass(frozen=True)
class IssueStateMachine:
    """Select the workflow for an issue based on its current Linear state."""

    execution_state: str = "Todo"
    qa_review_state: str = "QA Review"

    def route(self, issue: Issue) -> WorkflowRoute | None:
        """Return the workflow route for an issue, or ``None`` if not eligible."""
        normalized_state = issue.state.strip().lower()
        if normalized_state == self.execution_state.strip().lower():
            return WorkflowRoute(workflow="execution", target_state="In Progress")
        if normalized_state == self.qa_review_state.strip().lower():
            return WorkflowRoute(workflow="qa_review", target_state="QA Review")
        return None
