"""Execution workflow boundary."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Awaitable, Callable

from ..models import Issue, ReviewResult, Workspace
from ..config import ServiceConfig
from ..services.linear_service import LinearService
from ..services.pr_service import PRService
from ..services.workspace_service import WorkspaceService
from ..workflow import WorkflowDefinition, render_plan_prompt, render_prompt
from ..workspace import WorkspaceManager


@dataclass(frozen=True)
class ExecutionSuccessOutcome:
    """Workflow-owned decision for a successful execution run."""

    target: str | None
    schedule_continuation_retry: bool


@dataclass(frozen=True)
class ExecutionRetryOutcome:
    """Workflow-owned retry decision for execution runs."""

    attempt: int
    delay_ms: float | None
    error: str | None


@dataclass(frozen=True)
class ExecutionFailureOutcome:
    """Workflow-owned decision for a failed execution run."""

    transition_target: str | None
    retry: ExecutionRetryOutcome


@dataclass
class ExecutionWorkflow:
    """Own the implementation workflow for issues in ``To Do``."""

    linear: LinearService
    workspaces: WorkspaceService
    prs: PRService

    def resolve_agent_runner(self, config: ServiceConfig) -> tuple[str, object]:
        """Return the provider and runner config for execution work."""
        return config.agent.provider, config.runner

    def requires_planning(self) -> bool:
        """Execution runs always start with a planning phase."""
        return True

    def dispatch_target(self) -> str:
        """Return the state used when execution claims an issue."""
        return "In Progress"

    def resolve_success_target(self, config: ServiceConfig) -> str | None:
        """Return the state target after a successful execution run."""
        qa = config.transitions.qa_review
        if qa.enabled:
            return qa.dispatch
        return config.transitions.resolve("success")

    def resolve_success_outcome(self, config: ServiceConfig) -> ExecutionSuccessOutcome:
        """Return the workflow-owned outcome for a successful execution run."""
        return ExecutionSuccessOutcome(
            target=self.resolve_success_target(config),
            schedule_continuation_retry=True,
        )

    def resolve_failure_target(self, config: ServiceConfig) -> str | None:
        """Return the state target after a failed execution run."""
        return config.transitions.resolve("failure")

    def resolve_continuation_retry_outcome(
        self,
        *,
        delay_ms: float,
    ) -> ExecutionRetryOutcome:
        """Return the retry policy for a clean execution continuation."""
        return ExecutionRetryOutcome(
            attempt=1,
            delay_ms=delay_ms,
            error=None,
        )

    def resolve_failure_retry_outcome(
        self,
        *,
        next_attempt: int,
        error: str,
    ) -> ExecutionRetryOutcome:
        """Return the retry policy for an abnormal execution failure."""
        return ExecutionRetryOutcome(
            attempt=next_attempt,
            delay_ms=None,
            error=error,
        )

    def resolve_failure_outcome(
        self,
        config: ServiceConfig,
        *,
        next_attempt: int,
        error: str,
    ) -> ExecutionFailureOutcome:
        """Return the state-transition and retry policy for failed execution work."""
        return ExecutionFailureOutcome(
            transition_target=self.resolve_failure_target(config),
            retry=self.resolve_failure_retry_outcome(
                next_attempt=next_attempt,
                error=error,
            ),
        )

    def resolve_retry_poll_failure_outcome(
        self,
        *,
        attempt: int,
    ) -> ExecutionRetryOutcome:
        """Return the retry policy when polling candidates fails."""
        return ExecutionRetryOutcome(
            attempt=attempt + 1,
            delay_ms=None,
            error="retry poll failed",
        )

    def resolve_slot_wait_retry_outcome(
        self,
        *,
        attempt: int,
        continuation_delay_ms: float,
        is_continuation: bool,
    ) -> ExecutionRetryOutcome:
        """Return the retry policy when the orchestrator has no free slot."""
        if is_continuation:
            return ExecutionRetryOutcome(
                attempt=attempt,
                delay_ms=continuation_delay_ms,
                error=None,
            )
        return ExecutionRetryOutcome(
            attempt=attempt + 1,
            delay_ms=None,
            error="no available orchestrator slots",
        )

    def release_log_action(self, *, is_continuation: bool, issue_found: bool) -> str:
        """Return the action label used when a retry is released without dispatch."""
        if not issue_found:
            return "retry_claim_released_not_found"
        if is_continuation:
            return "continuation_retry_released_inactive"
        return "retry_claim_released_inactive"

    def waiting_for_slot_log_action(self, *, is_continuation: bool) -> str:
        """Return the action label used when a retry is waiting for capacity."""
        if is_continuation:
            return "continuation_retry_waiting_for_slot"
        return "retry_waiting_for_slot"

    def redispatch_log_action(self, *, is_continuation: bool) -> str:
        """Return the action label used when a retry is redispatched."""
        if is_continuation:
            return "continuation_retry_redispatching"
        return "retry_dispatching"

    def workspace_path_for(self, issue: Issue):
        """Return the execution workspace path for an issue."""
        return self.workspaces.execution_path_for(issue)

    def workspace_path_for_identifier(self, identifier: str) -> Path:
        """Return the execution workspace path for an identifier."""
        return self.workspaces.execution_path_for_identifier(identifier)

    def create_workspace_manager(self, config: ServiceConfig) -> WorkspaceManager:
        """Create the execution workspace manager."""
        return WorkspaceManager(config)

    async def prepare_workspace(
        self,
        manager: WorkspaceManager,
        issue: Issue,
    ) -> Workspace:
        """Create or reuse the execution workspace for an issue."""
        return await manager.create_for_issue(issue.identifier)

    async def prepare_workspace_run(
        self,
        config: ServiceConfig,
        issue: Issue,
    ) -> tuple[WorkspaceManager, Workspace]:
        """Create or reuse the execution workspace and return its manager."""
        manager = self.create_workspace_manager(config)
        workspace = await self.prepare_workspace(manager, issue)
        return manager, workspace

    async def prepare_run(
        self,
        manager: WorkspaceManager,
        workspace: Workspace,
    ) -> None:
        """Run execution pre-run workspace hooks."""
        await manager.run_before_run_hook(workspace)

    def build_plan_prompt(self, workflow: WorkflowDefinition, issue: Issue) -> str:
        """Render the planning prompt for execution runs."""
        return render_plan_prompt(workflow, issue)

    def build_execution_prompt(
        self,
        workflow: WorkflowDefinition,
        issue: Issue,
        attempt: int | None,
    ) -> str:
        """Render the implementation prompt for execution runs."""
        return render_prompt(workflow, issue, attempt)

    def continuation_prompt(self) -> str:
        """Return the prompt used for continuation turns."""
        return (
            "Continue working on the task only if implementation work remains. "
            "If the task is fully complete and ready for post-run handoff, "
            "reply with exactly `CYMPHONY_COMPLETE`."
        )

    def completion_marker(self) -> str:
        """Return the explicit completion marker for execution turns."""
        return "CYMPHONY_COMPLETE"

    def build_turn_prompt(
        self,
        workflow: WorkflowDefinition,
        issue: Issue,
        attempt: int | None,
        *,
        first_turn: bool,
    ) -> str:
        """Build the prompt for an execution turn."""
        if first_turn:
            return self.build_execution_prompt(workflow, issue, attempt)
        return self.continuation_prompt()

    async def refresh_issue(self, issue_id: str) -> Issue | None:
        """Refresh the issue record from Linear."""
        refreshed = await self.linear.fetch_issue_states_by_ids([issue_id])
        return refreshed[0] if refreshed else None

    async def validate_review_handoff(
        self,
        workspace_path: Path,
        *,
        base_branch: str,
        run_command: Callable[..., Awaitable[tuple[int, str, str]]],
    ) -> tuple[bool, str]:
        """Verify that execution produced a reviewable PR handoff."""
        if not workspace_path.exists():
            return False, f"Workspace path does not exist: {workspace_path}"

        resolved_base_branch = base_branch.strip() or "main"

        rc, stdout, stderr = await run_command(
            workspace_path,
            "git", "branch", "--show-current",
        )
        if rc != 0:
            return False, f"Failed to detect current branch: {stderr or stdout or 'unknown git error'}"
        branch = stdout.strip()
        if not branch:
            return False, "Workspace is not on a named branch after the build run"
        if branch == resolved_base_branch:
            return False, (
                f"Workspace is still on {resolved_base_branch!r}; "
                "after_run did not leave a review branch checked out"
            )

        rc, stdout, stderr = await run_command(
            workspace_path,
            "git", "status", "--porcelain",
        )
        if rc != 0:
            return False, f"Failed to inspect workspace status: {stderr or stdout or 'unknown git error'}"
        dirty_lines = [line for line in stdout.splitlines() if line.strip()]
        if dirty_lines:
            sample = "; ".join(dirty_lines[:5])
            return False, f"Workspace still has uncommitted changes after the build run: {sample}"

        rc, stdout, stderr = await run_command(
            workspace_path,
            "gh", "pr", "list",
            "--head", branch,
            "--json", "url,state",
            "--limit", "1",
        )
        if rc != 0:
            return False, (
                "Failed to confirm a GitHub PR for the review branch: "
                f"{stderr or stdout or 'unknown gh error'}"
            )

        try:
            prs = json.loads(stdout or "[]")
        except json.JSONDecodeError as exc:
            return False, f"Failed to parse GitHub PR lookup output: {exc}"

        if not prs:
            return False, f"No GitHub PR exists for branch {branch!r}"

        pr_url = str(prs[0].get("url") or "").strip()
        if not pr_url:
            return False, f"GitHub PR lookup for branch {branch!r} returned no URL"

        return True, pr_url

    def should_continue(
        self,
        issue: Issue,
        *,
        active_states: list[str],
        turn_number: int,
        max_turns: int,
        workspace_path: str | None = None,
        last_message: str | None = None,
    ) -> tuple[bool, str | None]:
        """Return whether execution should continue after a turn."""
        del workspace_path
        active_lower = {state.lower() for state in active_states}
        if issue.state.lower() not in active_lower:
            return False, "inactive_state"
        normalized_message = (last_message or "").strip()
        if normalized_message == self.completion_marker():
            return False, "task_complete"
        if turn_number >= max_turns:
            return False, "max_turns"
        return True, None

    def render_plan_comment(self, todos: list[dict]) -> str:
        """Render a TodoWrite checklist into the Linear plan-comment format."""
        lines = ["**Agent Plan**\n"]
        for todo in todos:
            content = todo.get("content", "")
            status = todo.get("status", "pending")
            if status == "completed":
                lines.append(f"- [x] {content}")
            elif status == "in_progress":
                lines.append(f"- [ ] 🔄 {content} *(in progress)*")
            else:
                lines.append(f"- [ ] {content}")
        return "\n".join(lines)

    def capture_run_result(self, workspace_path: str) -> ReviewResult | None:
        """Execution runs do not produce a QA review artifact."""
        del workspace_path
        return None

    async def run(self, issue: Issue) -> None:
        """Run the execution workflow for one issue.

        This is intentionally a placeholder seam. Behavior will migrate here
        from the legacy orchestrator in small slices.
        """
        raise NotImplementedError("Execution workflow migration not implemented yet")
