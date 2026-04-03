"""QA review workflow boundary."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from ..models import Issue, ReviewDecision, ReviewResult, ServiceConfig, Workspace
from ..review import REVIEW_RESULT_FILENAME, parse_review_result
from ..services.linear_service import LinearService
from ..services.workspace_service import WorkspaceService
from ..workflow import WorkflowDefinition, render_review_prompt
from ..workspace import WorkspaceManager


@dataclass(frozen=True)
class QAReviewCompletionOutcome:
    """Workflow-owned decision for a completed QA review run."""

    target: str | None
    clear_bounces: bool = False
    increment_bounce: bool = False
    should_cleanup_workspace: bool = True
    hold_for_manual_intervention: bool = False


@dataclass(frozen=True)
class QAReviewRetryOutcome:
    """Workflow-owned decision for an abnormal QA review retry path."""

    hold_for_manual_intervention: bool
    attempt: int | None = None
    error: str | None = None
    summary: str | None = None
    detail: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class QAReviewHoldOutcome:
    """Workflow-owned decision for holding a QA review for manual intervention."""

    summary: str
    detail: str
    reason: str


@dataclass
class QAReviewWorkflow:
    """Own the isolated QA review workflow for issues in ``QA Review``."""

    linear: LinearService
    workspaces: WorkspaceService

    def resolve_agent_runner(self, config: ServiceConfig) -> tuple[str, object]:
        """Return the provider and runner config for QA review work."""
        qa_agent = config.transitions.qa_review.agent
        if qa_agent is not None:
            return qa_agent.provider, qa_agent
        return config.agent.provider, config.runner

    def requires_planning(self) -> bool:
        """QA review runs skip the execution planning turn."""
        return False

    def workspace_path_for(self, issue: Issue, run_id: str | None = None):
        """Return the clean QA workspace path for a review run."""
        return self.workspaces.fresh_qa_path_for(issue, run_id=run_id)

    def workspace_root_for(self, issue: Issue) -> str:
        """Return the QA workspace root for an issue."""
        return str(self.workspaces.qa_issue_root_for(issue))

    def workspace_key_for(self, run_id: str | None = None) -> str:
        """Return the QA workspace key for a review run."""
        return run_id or uuid4().hex

    def create_workspace_manager(
        self,
        config: ServiceConfig,
        issue: Issue,
    ) -> WorkspaceManager:
        """Create a workspace manager rooted at the issue-specific QA area."""
        qa_config = SimpleNamespace(
            workspace=SimpleNamespace(root=self.workspace_root_for(issue)),
            hooks=config.hooks,
        )
        return WorkspaceManager(qa_config)

    async def prepare_workspace(
        self,
        config: ServiceConfig,
        issue: Issue,
        run_id: str | None = None,
    ) -> tuple[WorkspaceManager, Workspace]:
        """Create a fresh QA workspace for this review run."""
        manager = self.create_workspace_manager(config, issue)
        workspace = await manager.create_for_issue(self.workspace_key_for(run_id))
        return manager, workspace

    async def prepare_workspace_run(
        self,
        config: ServiceConfig,
        issue: Issue,
    ) -> tuple[WorkspaceManager, Workspace]:
        """Create a fresh QA workspace and return its manager."""
        return await self.prepare_workspace(config, issue)

    async def prepare_run(
        self,
        manager: WorkspaceManager,
        workspace: Workspace,
        issue: Issue,
    ) -> None:
        """Run QA pre-run hooks inside the isolated review workspace."""
        await manager.run_before_run_hook(workspace)
        await self._checkout_review_branch(workspace.path, issue)

    def review_branch_name(self, issue: Issue) -> str:
        """Return the branch that QA should review for an issue."""
        if issue.branch_name and issue.branch_name.strip():
            return issue.branch_name.strip()
        return f"agent/{issue.identifier.lower()}"

    def review_branch_candidates(self, issue: Issue) -> list[str]:
        """Return remote branch candidates for a QA review run."""
        candidates: list[str] = []
        preferred = (issue.branch_name or "").strip()
        canonical = f"agent/{issue.identifier.lower()}"
        for branch_name in (preferred, canonical):
            if branch_name and branch_name not in candidates:
                candidates.append(branch_name)
        return candidates

    async def _checkout_review_branch(self, workspace_path: str, issue: Issue) -> None:
        """Check out the remote branch under review in a fresh QA workspace."""
        failures: list[tuple[str, str]] = []
        for branch_name in self.review_branch_candidates(issue):
            fetch = await asyncio.create_subprocess_exec(
                "git",
                "fetch",
                "origin",
                branch_name,
                cwd=workspace_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            fetch_stdout, fetch_stderr = await fetch.communicate()
            if fetch.returncode != 0:
                stderr_text = (fetch_stderr or b"").decode(errors="replace").strip()
                stdout_text = (fetch_stdout or b"").decode(errors="replace").strip()
                detail = stderr_text or stdout_text or "unknown git fetch failure"
                failures.append((branch_name, detail))
                continue

            checkout = await asyncio.create_subprocess_exec(
                "git",
                "checkout",
                "-B",
                branch_name,
                f"origin/{branch_name}",
                cwd=workspace_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            checkout_stdout, checkout_stderr = await checkout.communicate()
            if checkout.returncode == 0:
                return

            stderr_text = (checkout_stderr or b"").decode(errors="replace").strip()
            stdout_text = (checkout_stdout or b"").decode(errors="replace").strip()
            detail = stderr_text or stdout_text or "unknown git checkout failure"
            failures.append((branch_name, detail))

        attempted = ", ".join(f"{name!r}: {detail}" for name, detail in failures)
        raise RuntimeError(
            f"Failed to fetch or check out any review branch candidate: {attempted}"
        )

    def build_review_prompt(self, workflow: WorkflowDefinition, issue: Issue) -> str:
        """Render the QA review prompt."""
        return render_review_prompt(workflow, issue)

    def continuation_prompt(self) -> str:
        """Return the prompt used for review continuation turns."""
        return (
            "Continue the QA review only if review work remains. "
            "If the review is complete and REVIEW_RESULT.json is written, "
            "reply with exactly `CYMPHONY_REVIEW_COMPLETE`."
        )

    def completion_marker(self) -> str:
        """Return the explicit completion marker for review turns."""
        return "CYMPHONY_REVIEW_COMPLETE"

    def build_turn_prompt(
        self,
        workflow: WorkflowDefinition,
        issue: Issue,
        attempt: int | None = None,
        *,
        first_turn: bool,
    ) -> str:
        """Build the prompt for a QA review turn."""
        del attempt
        if first_turn:
            return self.build_review_prompt(workflow, issue)
        return self.continuation_prompt()

    async def refresh_issue(self, issue_id: str) -> Issue | None:
        """Refresh the issue record from Linear."""
        refreshed = await self.linear.fetch_issue_states_by_ids([issue_id])
        return refreshed[0] if refreshed else None

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
        """Return whether review should continue after a turn."""
        active_lower = {state.lower() for state in active_states}
        if issue.state.lower() not in active_lower:
            return False, "inactive_state"
        if workspace_path and (Path(workspace_path) / REVIEW_RESULT_FILENAME).exists():
            return False, "review_result_ready"
        normalized_message = (last_message or "").strip()
        if normalized_message == self.completion_marker():
            return False, "review_complete"
        if turn_number >= max_turns:
            return False, "max_turns"
        return True, None

    def load_review_result(self, workspace_path: str) -> ReviewResult:
        """Load the machine-readable QA review result from the review workspace."""
        return parse_review_result(workspace_path)

    def capture_run_result(self, workspace_path: str) -> ReviewResult:
        """Capture the review result before post-run hooks clean up files."""
        return self.load_review_result(workspace_path)

    def render_review_result_comment(self, result: ReviewResult) -> str:
        """Render a QA review verdict into a Linear comment body."""
        if result.decision == ReviewDecision.PASS:
            heading = "QA review passed"
        elif result.decision == ReviewDecision.CHANGES_REQUESTED:
            heading = "QA review requested changes"
        else:
            heading = "QA review result could not be applied"

        lines = [f"**{heading}**"]
        if result.decision is not None:
            lines.append(f"Decision: `{result.decision.value}`")

        summary = (result.summary or "").strip()
        error = (result.error or "").strip()
        raw_output = (result.raw_output or "").strip()

        if summary:
            lines.extend(["", summary])

        if error:
            lines.extend(
                [
                    "",
                    "Cymphony did not apply a QA transition because the review decision was invalid.",
                    f"Parse error: {error}",
                ]
            )

        if raw_output and result.decision is None:
            lines.extend(["", "Raw review output:", "```json", raw_output, "```"])

        return "\n".join(lines)

    async def publish_review_result_comment(
        self,
        issue_id: str,
        result: ReviewResult,
        *,
        existing_comment_id: str | None = None,
    ) -> tuple[str, bool]:
        """Create or update the QA review comment and return ``(comment_id, created)``."""
        body = self.render_review_result_comment(result)
        comment_id = existing_comment_id
        if comment_id:
            updated = await self.linear.update_comment(comment_id, body)
            if updated:
                return comment_id, False
            comment_id = None

        created_id = await self.linear.create_comment(issue_id, body)
        return created_id, True

    def resolve_decision_target(
        self,
        config: ServiceConfig,
        result: ReviewResult,
    ) -> str | None:
        """Return the state target for a QA review decision."""
        qa = config.transitions.qa_review
        if not qa.enabled or result.decision is None:
            return None
        if result.decision == ReviewDecision.PASS:
            return qa.success
        return qa.failure

    def resolve_completion_outcome(
        self,
        config: ServiceConfig,
        result: ReviewResult,
        *,
        transition_succeeded: bool,
        current_bounce_count: int,
    ) -> QAReviewCompletionOutcome:
        """Return the workflow-owned outcome for a completed QA review run."""
        target = self.resolve_decision_target(config, result)
        qa = config.transitions.qa_review

        if result.decision == ReviewDecision.PASS:
            return QAReviewCompletionOutcome(
                target=target,
                clear_bounces=transition_succeeded,
                should_cleanup_workspace=True,
            )

        increment_bounce = bool(
            result.decision == ReviewDecision.CHANGES_REQUESTED
            and transition_succeeded
            and target
            and target.lower() != (qa.dispatch or "").lower()
        )
        next_bounce_count = current_bounce_count + (1 if increment_bounce else 0)
        hold_for_manual = next_bounce_count > qa.max_bounces

        return QAReviewCompletionOutcome(
            target=target,
            increment_bounce=increment_bounce,
            should_cleanup_workspace=True,
            hold_for_manual_intervention=hold_for_manual,
        )

    def resolve_retry_outcome(
        self,
        config: ServiceConfig,
        *,
        attempt: int,
        error: str,
    ) -> QAReviewRetryOutcome:
        """Return whether QA should retry again or hold for manual intervention."""
        qa = config.transitions.qa_review
        if attempt > qa.max_retries:
            return QAReviewRetryOutcome(
                hold_for_manual_intervention=True,
                summary="QA review retry limit reached",
                detail=(
                    f"Reviewer run failed {attempt} times; "
                    "holding issue in QA review for manual intervention."
                ),
                reason="qa_review_retry_limit",
            )

        return QAReviewRetryOutcome(
            hold_for_manual_intervention=False,
            attempt=attempt,
            error=error,
        )

    def resolve_bounce_limit_hold_outcome(
        self,
        *,
        bounce_count: int,
    ) -> QAReviewHoldOutcome:
        """Return the manual-hold message after QA bounce limit is exceeded."""
        return QAReviewHoldOutcome(
            summary="QA review re-entry limit reached",
            detail=(
                f"Reached {bounce_count} QA review bounces; "
                "holding issue for manual intervention."
            ),
            reason="qa_review_bounce_limit",
        )

    async def run(self, issue: Issue) -> None:
        """Run the QA review workflow for one issue.

        This is intentionally a placeholder seam. Behavior will migrate here
        from the legacy orchestrator in small slices.
        """
        raise NotImplementedError("QA review workflow migration not implemented yet")
