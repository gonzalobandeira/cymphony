"""Orchestrator: poll loop, dispatch, reconciliation, retry (spec §7, §8, §14, §16)."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .agent import create_agent_runner
from .config import ServiceConfig, build_config, validate_dispatch_config
from .linear import LinearClient
from .logging_ import issue_log, session_log
from .models import (
    AgentEvent,
    AgentEventType,
    AgentError,
    ControlAction,
    CodexTotals,
    ExecutionMode,
    Issue,
    LiveSession,
    OrchestratorState,
    ProblemRecord,
    ReviewDecision,
    ReviewResult,
    RetryEntry,
    RunningEntry,
    RunStatus,
    SkippedEntry,
    TransitionRecord,
    WorkflowDefinition,
    WorkflowError,
)
from .preflight import PreflightResult, run_preflight_checks
from .review import is_review_result_missing, parse_review_result
from .services import LinearService, PRService, WorkspaceService
from .state_machine import IssueStateMachine
from .state import StateManager
from .workflow import WorkflowWatcher, load_workflow
from .workspace import WorkspaceManager
from .workflows import ExecutionWorkflow, QAReviewWorkflow
from .workflows.execution import ExecutionFailureOutcome, ExecutionRetryOutcome
from .workflows.qa_review import QAReviewCompletionOutcome

logger = logging.getLogger(__name__)

_MAX_RECENT_EVENTS = 12


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _monotonic_ms() -> float:
    return time.monotonic() * 1000.0


# Continuation retry delay after clean exit (spec §8.4)
_CONTINUATION_RETRY_DELAY_MS = 1000.0
_MAX_RECENT_PROBLEMS = 25
_CONTROL_HISTORY_LIMIT = 50
_MAX_TRANSITION_HISTORY = 50
_DEFAULT_BASE_BRANCH = "main"


class Orchestrator:
    """Main orchestrator daemon (spec §7, §8, §16)."""

    def __init__(
        self,
        workflow_path: Path,
        config: ServiceConfig,
        workflow: WorkflowDefinition,
    ) -> None:
        self._workflow_path = workflow_path
        self._config = config
        self._workflow = workflow
        self._state = OrchestratorState(
            poll_interval_ms=config.polling.interval_ms,
            max_concurrent_agents=config.agent.max_concurrent_agents,
        )
        self._state_manager = StateManager(
            Path(config.workspace.root) / ".cymphony_state.json"
        )
        self._observers: list[Any] = []
        self._server: Any = None
        self._shutdown_event = asyncio.Event()
        self._state_id_cache: dict[tuple[str, str], str] = {}  # (team_id, state_name) → Linear state ID
        self._tick_task: asyncio.Task[None] | None = None
        self._tick_handle: asyncio.TimerHandle | None = None
        self._tick_due_at_ms: float | None = None
        self._tick_rerun_requested: bool = False
        self._plan_comment_locks: dict[str, asyncio.Lock] = {}
        self._state_machine = self._build_state_machine(config)
        self._linear_service = LinearService(
            config.tracker,
            client_factory=lambda: LinearClient(config.tracker),
        )
        self._workspace_service = WorkspaceService(config.workspace)
        self._execution_workflow = ExecutionWorkflow(
            linear=self._linear_service,
            workspaces=self._workspace_service,
            prs=PRService(),
        )
        self._qa_review_workflow = QAReviewWorkflow(
            linear=self._linear_service,
            workspaces=self._workspace_service,
        )

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def _build_state_machine(self, config: ServiceConfig) -> IssueStateMachine:
        """Create the issue state machine from current configuration."""
        qa_state = config.transitions.qa_review.dispatch or "QA Review"
        return IssueStateMachine(
            execution_state="To Do",
            qa_review_state=qa_state,
        )

    async def run(self) -> None:
        """Start the orchestrator event loop (spec §16.1)."""
        loop = asyncio.get_event_loop()

        # Start workflow file watcher for dynamic reload (spec §6.2)
        watcher = WorkflowWatcher(
            self._workflow_path,
            on_change=self._on_workflow_change,
            loop=loop,
        )
        watcher.start()

        # Start optional HTTP server
        if self._config.server.port is not None:
            from .server import start_server
            self._server = await start_server(
                self,
                self._config.server.port,
                self._workflow_path,
            )

        # Restore persisted state from previous run
        await self._restore_persisted_state()

        # Startup terminal workspace cleanup (spec §8.6)
        await self._startup_terminal_cleanup()

        # Validate configured transition targets against Linear workflow states
        await self._validate_transitions(fail_hard=True)

        # Schedule immediate first tick
        self._enqueue_tick(delay_ms=0.0)

        try:
            await self._shutdown_event.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            watcher.stop()
            if self._server:
                await self._server.cleanup()

    async def _on_workflow_change(self, new_workflow: WorkflowDefinition) -> None:
        """Apply updated workflow config dynamically (spec §6.2)."""
        try:
            new_config = build_config(new_workflow, self._config.server.port)
            previous_config = self._config
            previous_cache = dict(self._state_id_cache)

            # Validate transitions against Linear states before making the new
            # workflow active so a bad reload cannot leak into runtime behavior.
            self._config = new_config
            is_valid = await self._validate_transitions(fail_hard=False)
            if not is_valid:
                self._config = previous_config
                self._state_id_cache = previous_cache
                logger.warning(
                    "action=workflow_config_reapply_rejected "
                    "reason=invalid_transition_targets"
                )
                return

            self._workflow = new_workflow
            self._state.poll_interval_ms = new_config.polling.interval_ms
            self._state.max_concurrent_agents = new_config.agent.max_concurrent_agents
            self._state_machine = self._build_state_machine(new_config)
            self._linear_service = LinearService(
                new_config.tracker,
                client_factory=lambda: LinearClient(new_config.tracker),
            )
            self._workspace_service = WorkspaceService(new_config.workspace)
            self._execution_workflow = ExecutionWorkflow(
                linear=self._linear_service,
                workspaces=self._workspace_service,
                prs=PRService(),
            )
            self._qa_review_workflow = QAReviewWorkflow(
                linear=self._linear_service,
                workspaces=self._workspace_service,
            )
            logger.info("action=workflow_config_reapplied")
        except Exception as exc:
            self._record_problem(
                kind="workflow_reload_failed",
                severity="error",
                summary="Workflow reload failed",
                detail=str(exc),
            )
            logger.error(f"action=workflow_reapply_failed error={exc}")

    async def _validate_transitions(self, *, fail_hard: bool = False) -> bool:
        """Validate configured transition targets against Linear workflow states.

        Fetches teams for the project and checks that each configured
        transition target exists as a workflow state on every team.

        When *fail_hard* is True (startup), raises ``WorkflowError`` on
        invalid targets.  When False (reload), logs warnings only.

        Returns True if all targets are valid.
        """
        transitions = self._config.transitions
        targets: dict[str, str] = {}
        for field in ("dispatch", "success", "failure", "blocked", "cancelled"):
            value = getattr(transitions, field)
            if value is not None:
                targets[field] = value

        # Include QA review targets when enabled
        if transitions.qa_review.enabled:
            for qa_field in ("dispatch", "success", "failure"):
                value = getattr(transitions.qa_review, qa_field)
                if value is not None:
                    targets[f"qa_review.{qa_field}"] = value

        if not targets:
            logger.info("action=validate_transitions result=skip reason=no_targets_configured")
            return True

        try:
            team_ids = await self._linear_service.fetch_project_team_ids()
        except Exception as exc:
            msg = f"Failed to fetch project teams for transition validation: {exc}"
            if fail_hard:
                raise WorkflowError("transition_validation_failed", msg) from exc
            logger.warning(f"action=validate_transitions_skipped reason=team_fetch_failed error={exc}")
            return False

        if not team_ids:
            msg = "No teams found for project — cannot validate transition targets"
            if fail_hard:
                raise WorkflowError("transition_validation_failed", msg)
            logger.warning(f"action=validate_transitions_skipped reason=no_teams_found")
            return False

        all_valid = True
        for team_id in team_ids:
            try:
                state_names = await self._linear_service.fetch_team_workflow_state_names(team_id)
            except Exception as exc:
                msg = f"Failed to fetch workflow states for team {team_id}: {exc}"
                if fail_hard:
                    raise WorkflowError("transition_validation_failed", msg) from exc
                logger.warning(f"action=validate_transitions_skipped team_id={team_id} error={exc}")
                all_valid = False
                continue

            available_lower = {s.lower() for s in state_names}

            for field, target in targets.items():
                if target.lower() not in available_lower:
                    all_valid = False
                    msg = (
                        f"Transition '{field}' targets state '{target}' "
                        f"which does not exist on team {team_id}. "
                        f"Available states: {sorted(state_names)}"
                    )
                    if fail_hard:
                        raise WorkflowError("invalid_transition_target", msg)
                    logger.warning(f"action=invalid_transition_target {msg}")

            # Pre-populate state ID cache for valid targets
            for field, target in targets.items():
                if target.lower() in available_lower:
                    # Resolve the actual state ID and cache it
                    state_id = await self._linear_service.fetch_team_workflow_state_id(team_id, target)
                    if state_id:
                        cache_key = (team_id, target.lower())
                        self._state_id_cache[cache_key] = state_id

        if all_valid:
            logger.info(
                f"action=validate_transitions result=ok "
                f"teams={len(team_ids)} targets={list(targets.keys())}"
            )

        return all_valid

    # ------------------------------------------------------------------
    # Startup cleanup
    # ------------------------------------------------------------------

    async def _startup_terminal_cleanup(self) -> None:
        """Remove workspaces for terminal-state issues on startup (spec §8.6)."""
        try:
            terminal_issues = await self._linear_service.fetch_issues_by_states(
                self._config.tracker.terminal_states
            )
            wm = WorkspaceManager(self._config)
            removed = 0
            for issue in terminal_issues:
                ws_path = wm.get_path(issue.identifier)
                if ws_path.exists():
                    await wm.remove_workspace(issue.identifier)
                    removed += 1
            logger.info(
                f"action=startup_terminal_cleanup "
                f"project_slug={self._config.tracker.project_slug} "
                f"states={self._config.tracker.terminal_states} "
                f"matched={len(terminal_issues)} removed={removed}"
            )
        except Exception as exc:
            logger.warning(
                f"action=startup_terminal_cleanup_failed "
                f"project_slug={self._config.tracker.project_slug} "
                f"error={exc} (continuing)"
            )

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _persist_state(self) -> None:
        """Save current runtime state to disk. Best-effort; never raises."""
        try:
            self._state_manager.save(
                retry_attempts=self._state.retry_attempts,
                qa_review_bounces=self._state.qa_review_bounces,
                qa_review_comment_ids=self._state.qa_review_comment_ids,
                skipped=self._state.skipped,
                dispatch_paused=self._state.dispatch_paused,
            )
        except Exception as exc:
            logger.warning(f"action=state_persist_failed error={exc}")

    async def _restore_persisted_state(self) -> None:
        """Restore persisted state and reconcile against current Linear state."""
        (
            retry_attempts,
            qa_review_bounces,
            qa_review_comment_ids,
            skipped,
            dispatch_paused,
        ) = self._state_manager.restore()

        if (
            not retry_attempts
            and not qa_review_bounces
            and not qa_review_comment_ids
            and not skipped
            and not dispatch_paused
        ):
            return

        # Reconcile: fetch current issue states from Linear to drop stale entries
        all_issue_ids = list(
            set(
                list(retry_attempts.keys())
                + list(qa_review_bounces.keys())
                + list(qa_review_comment_ids.keys())
                + list(skipped.keys())
            )
        )
        current_states: dict[str, str] | None = None
        try:
            issues = await self._linear_service.fetch_issue_states_by_ids(all_issue_ids)
            current_states = {i.id: i.state for i in issues}
        except Exception as exc:
            logger.warning(
                f"action=state_reconcile_fetch_failed error={exc} "
                "(restoring all persisted entries without reconciliation)"
            )
            # If we can't reach Linear, still restore what we have — the next
            # tick will reconcile naturally.

        terminal_lower = {s.lower() for s in self._config.tracker.terminal_states}
        wm = WorkspaceManager(self._config)

        # Reconcile retry attempts
        restored_retries = 0
        dropped_retries = 0
        for issue_id, entry in list(retry_attempts.items()):
            if current_states is not None:
                issue_state = current_states.get(issue_id)

                # Drop retries for issues in terminal states
                if issue_state is not None and issue_state.lower() in terminal_lower:
                    logger.info(
                        f"action=state_reconcile_drop_retry issue_id={issue_id} "
                        f"identifier={entry.identifier} reason=terminal_state "
                        f"state={issue_state}"
                    )
                    dropped_retries += 1
                    continue

                # Drop retries for issues no longer found in Linear
                if issue_id not in current_states:
                    logger.info(
                        f"action=state_reconcile_drop_retry issue_id={issue_id} "
                        f"identifier={entry.identifier} reason=not_found_in_linear"
                    )
                    dropped_retries += 1
                    continue

            # Restore: set due_at_ms to fire immediately on next tick
            entry.due_at_ms = _monotonic_ms()
            self._state.retry_attempts[issue_id] = entry
            self._state.claimed.add(issue_id)
            restored_retries += 1

        for issue_id, bounce_count in list(qa_review_bounces.items()):
            if current_states is not None:
                issue_state = current_states.get(issue_id)

                if issue_state is not None and issue_state.lower() in terminal_lower:
                    continue

                if issue_id not in current_states:
                    continue

            self._state.qa_review_bounces[issue_id] = bounce_count

        for issue_id, comment_id in list(qa_review_comment_ids.items()):
            if current_states is not None:
                issue_state = current_states.get(issue_id)

                if issue_state is not None and issue_state.lower() in terminal_lower:
                    continue

                if issue_id not in current_states:
                    continue

            self._state.qa_review_comment_ids[issue_id] = comment_id

        # Reconcile skipped entries
        restored_skipped = 0
        dropped_skipped = 0
        for issue_id, entry in list(skipped.items()):
            if current_states is not None:
                issue_state = current_states.get(issue_id)

                # Drop skips for terminal issues
                if issue_state is not None and issue_state.lower() in terminal_lower:
                    logger.info(
                        f"action=state_reconcile_drop_skip issue_id={issue_id} "
                        f"identifier={entry.identifier} reason=terminal_state "
                        f"state={issue_state}"
                    )
                    dropped_skipped += 1
                    continue

            self._state.skipped[issue_id] = entry
            restored_skipped += 1

        if dispatch_paused:
            self._state.dispatch_paused = True

        logger.info(
            f"action=state_reconcile_complete "
            f"restored_retries={restored_retries} dropped_retries={dropped_retries} "
            f"restored_skipped={restored_skipped} dropped_skipped={dropped_skipped} "
            f"dispatch_paused={dispatch_paused}"
        )

        # Schedule timers for restored retries
        for issue_id in list(self._state.retry_attempts.keys()):
            asyncio.get_event_loop().call_soon(
                lambda iid=issue_id: asyncio.create_task(self._on_retry_timer(iid)),
            )

    # ------------------------------------------------------------------
    # Poll tick
    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        """Run serialized ticks, coalescing any overlap into one follow-up pass."""
        normal_completion = False
        try:
            while True:
                self._tick_rerun_requested = False
                await self._tick_once()

                if self._tick_rerun_requested:
                    continue

                normal_completion = True
                break
        finally:
            rerun_requested = self._tick_rerun_requested
            self._tick_task = None
            if rerun_requested:
                self._enqueue_tick(delay_ms=0.0)
            elif normal_completion:
                self._schedule_tick()

    async def _tick_once(self) -> None:
        """One poll-and-dispatch tick (spec §8.1, §16.2)."""
        try:
            # 1. Reconcile active runs
            await self._reconcile_running_issues()

            # 2. Dispatch preflight validation
            validation = validate_dispatch_config(self._config)
            if not validation.ok:
                self._state.last_validation_errors = list(validation.errors)
                for err in validation.errors:
                    self._record_problem(
                        kind="invalid_config",
                        summary="Dispatch configuration is invalid",
                        detail=err,
                    )
                for err in validation.errors:
                    logger.error(
                        f"action=dispatch_validation_failed error={err!r}"
                    )
                return
            self._state.last_validation_errors = []

            # 2b. Repo preflight checks (CLIs, env vars)
            if self._config.preflight.enabled:
                preflight = await run_preflight_checks(
                    self._config.preflight, workspace_path=None,
                )
                if not preflight.ok:
                    error_dicts = [
                        {"name": c.name, "message": c.message}
                        for c in preflight.errors
                    ]
                    self._state.last_preflight_errors = error_dicts
                    for c in preflight.errors:
                        self._record_problem(
                            kind="preflight_failed",
                            summary=f"Preflight check '{c.name}' failed",
                            detail=c.message,
                        )
                        logger.error(
                            f"action=preflight_check_failed "
                            f"name={c.name} message={c.message!r}"
                        )
                    return
                self._state.last_preflight_errors = []

            # 3. Fetch candidate issues
            try:
                issues = await self._linear_service.fetch_candidate_issues()
            except Exception as exc:
                self._state.last_candidates = []
                self._record_problem(
                    kind="fetch_candidates_failed",
                    summary="Failed to refresh candidate issues",
                    detail=str(exc),
                )
                logger.error(f"action=fetch_candidates_failed error={exc}")
                return
            self._state.last_candidates = list(issues)

            # 4. Sort for dispatch (spec §8.2)
            sorted_issues = _sort_for_dispatch(issues)

            # 5. Dispatch eligible issues while slots remain
            for issue in sorted_issues:
                if not self._has_slots():
                    break
                if self._should_dispatch(issue):
                    await self._dispatch_issue(issue, attempt=None)

        except Exception as exc:
            logger.error(f"action=tick_error error={exc}", exc_info=True)
        finally:
            self._persist_state()

    def _schedule_tick(self, delay_ms: float | None = None) -> None:
        if delay_ms is None:
            delay_ms = float(self._state.poll_interval_ms)
        self._enqueue_tick(delay_ms)

    def _enqueue_tick(self, delay_ms: float) -> bool:
        """Queue the next tick without allowing concurrent tick execution."""
        if self._tick_task is not None and not self._tick_task.done():
            already_requested = self._tick_rerun_requested
            self._tick_rerun_requested = True
            return already_requested

        loop = asyncio.get_event_loop()
        due_at_ms = _monotonic_ms() + max(delay_ms, 0.0)

        if self._tick_handle is not None and not self._tick_handle.cancelled():
            existing_due_at_ms = self._tick_due_at_ms or due_at_ms
            if due_at_ms >= existing_due_at_ms:
                return True
            self._tick_handle.cancel()

        self._tick_due_at_ms = due_at_ms
        self._tick_handle = loop.call_later(
            max(delay_ms, 0.0) / 1000.0,
            self._start_tick_task,
        )
        return False

    def _start_tick_task(self) -> None:
        """Start one serialized tick runner."""
        self._tick_handle = None
        self._tick_due_at_ms = None

        if self._tick_task is not None and not self._tick_task.done():
            self._tick_rerun_requested = True
            return

        self._tick_task = asyncio.create_task(self._tick())

    def request_immediate_poll(self) -> bool:
        """Trigger an immediate poll tick (e.g. from HTTP POST /api/v1/refresh).

        Returns True if the request was coalesced (a poll was already pending),
        False if a new tick was scheduled.
        """
        if self._is_shutting_down():
            return True
        return self._enqueue_tick(delay_ms=0.0)

    def trigger_refresh(self) -> dict[str, Any]:
        """Queue an immediate refresh and record the operator action."""
        coalesced = self.request_immediate_poll()
        detail = "coalesced with pending tick" if coalesced else "tick queued"
        self._record_control(
            action="refresh",
            scope="global",
            outcome="accepted",
            detail=detail,
        )
        return {
            "ok": True,
            "action": "refresh",
            "scope": "global",
            "coalesced": coalesced,
            "detail": detail,
        }

    def pause_dispatching(self) -> dict[str, Any]:
        """Pause new dispatches while leaving active workers alone."""
        already_paused = self._state.dispatch_paused
        self._state.dispatch_paused = True
        outcome = "noop" if already_paused else "accepted"
        detail = "dispatching already paused" if already_paused else "dispatching paused"
        self._record_control(
            action="pause_dispatching",
            scope="global",
            outcome=outcome,
            detail=detail,
        )
        return {
            "ok": True,
            "action": "pause_dispatching",
            "scope": "global",
            "already_paused": already_paused,
            "detail": detail,
        }

    def resume_dispatching(self) -> dict[str, Any]:
        """Resume dispatching and immediately poll for eligible work."""
        was_paused = self._state.dispatch_paused
        self._state.dispatch_paused = False
        refresh = self.request_immediate_poll()
        outcome = "accepted" if was_paused else "noop"
        detail = (
            "dispatching resumed; refresh queued"
            if was_paused
            else "dispatching already active; refresh queued"
        )
        self._record_control(
            action="resume_dispatching",
            scope="global",
            outcome=outcome,
            detail=detail,
        )
        return {
            "ok": True,
            "action": "resume_dispatching",
            "scope": "global",
            "was_paused": was_paused,
            "refresh_coalesced": refresh,
            "detail": detail,
        }

    async def shutdown_app(self) -> dict[str, Any]:
        """Stop dispatching, cancel active work, and shut the orchestrator down."""
        already_requested = self._is_shutting_down()
        self._state.dispatch_paused = True
        self._state.shutdown_requested = True

        if self._tick_handle is not None and not self._tick_handle.cancelled():
            self._tick_handle.cancel()
        self._tick_handle = None
        self._tick_due_at_ms = None
        self._tick_rerun_requested = False
        self._state.retry_attempts.clear()

        running_issue_ids = list(self._state.running.keys())
        for issue_id in running_issue_ids:
            await self._terminate_running_issue(issue_id, cleanup_workspace=False)

        self._persist_state()
        self._shutdown_event.set()
        outcome = "noop" if already_requested else "accepted"
        detail = (
            "shutdown already requested"
            if already_requested
            else "shutdown requested; running workers cancelled"
        )
        self._record_control(
            action="shutdown_app",
            scope="global",
            outcome=outcome,
            detail=detail,
        )
        return {
            "ok": True,
            "action": "shutdown_app",
            "scope": "global",
            "already_requested": already_requested,
            "cancelled_workers": len(running_issue_ids),
            "detail": detail,
        }

    async def cancel_worker(self, identifier: str) -> dict[str, Any]:
        """Cancel a running worker without requeueing it automatically."""
        issue_identifier = identifier.upper()
        match = self._find_tracked_issue(issue_identifier)
        if not match or match["kind"] != "running":
            detail = "issue is not currently running"
            self._record_control(
                action="cancel_worker",
                scope="issue",
                outcome="rejected",
                issue_identifier=issue_identifier,
                detail=detail,
            )
            return {
                "ok": False,
                "action": "cancel_worker",
                "scope": "issue",
                "issue_identifier": issue_identifier,
                "detail": detail,
            }

        issue_id = str(match["issue_id"])
        await self._terminate_running_issue(issue_id, cleanup_workspace=False)
        self._state.claimed.discard(issue_id)
        self._state.completed.discard(issue_id)
        self._state.retry_attempts.pop(issue_id, None)
        detail = "worker cancelled and issue released"
        self._record_control(
            action="cancel_worker",
            scope="issue",
            outcome="accepted",
            issue_id=issue_id,
            issue_identifier=issue_identifier,
            detail=detail,
        )
        return {
            "ok": True,
            "action": "cancel_worker",
            "scope": "issue",
            "issue_id": issue_id,
            "issue_identifier": issue_identifier,
            "detail": detail,
        }

    async def requeue_issue(self, identifier: str) -> dict[str, Any]:
        """Release an issue from manual holds and ask the scheduler to pick it up again."""
        issue_identifier = identifier.upper()
        match = self._find_tracked_issue(issue_identifier)
        if not match:
            detail = "issue is not currently tracked"
            self._record_control(
                action="requeue_issue",
                scope="issue",
                outcome="rejected",
                issue_identifier=issue_identifier,
                detail=detail,
            )
            return {
                "ok": False,
                "action": "requeue_issue",
                "scope": "issue",
                "issue_identifier": issue_identifier,
                "detail": detail,
            }

        issue_id = str(match["issue_id"])
        if match["kind"] == "running":
            await self._terminate_running_issue(issue_id, cleanup_workspace=False)

        self._state.retry_attempts.pop(issue_id, None)
        self._state.skipped.pop(issue_id, None)
        self._state.claimed.discard(issue_id)
        self._state.completed.discard(issue_id)
        self._clear_qa_review_cycle_state(issue_id)
        self._persist_state()
        coalesced = self.request_immediate_poll()
        detail = "issue released for redispatch"
        self._record_control(
            action="requeue_issue",
            scope="issue",
            outcome="accepted",
            issue_id=issue_id,
            issue_identifier=issue_identifier,
            detail=detail,
        )
        return {
            "ok": True,
            "action": "requeue_issue",
            "scope": "issue",
            "issue_id": issue_id,
            "issue_identifier": issue_identifier,
            "refresh_coalesced": coalesced,
            "detail": detail,
        }

    async def skip_issue(self, identifier: str) -> dict[str, Any]:
        """Hold an issue out of dispatch until an operator requeues it."""
        issue_identifier = identifier.upper()
        match = self._find_tracked_issue(issue_identifier)
        if not match:
            detail = "issue is not currently tracked"
            self._record_control(
                action="skip_issue",
                scope="issue",
                outcome="rejected",
                issue_identifier=issue_identifier,
                detail=detail,
            )
            return {
                "ok": False,
                "action": "skip_issue",
                "scope": "issue",
                "issue_identifier": issue_identifier,
                "detail": detail,
            }

        issue_id = str(match["issue_id"])
        if match["kind"] == "running":
            await self._terminate_running_issue(issue_id, cleanup_workspace=False)

        self._state.retry_attempts.pop(issue_id, None)
        self._state.claimed.discard(issue_id)
        self._state.completed.discard(issue_id)
        self._state.skipped[issue_id] = SkippedEntry(
            issue_id=issue_id,
            identifier=issue_identifier,
            created_at=_now_utc(),
            reason="operator_skip",
        )
        self._persist_state()
        detail = "issue marked as skipped"
        self._record_control(
            action="skip_issue",
            scope="issue",
            outcome="accepted",
            issue_id=issue_id,
            issue_identifier=issue_identifier,
            detail=detail,
        )
        return {
            "ok": True,
            "action": "skip_issue",
            "scope": "issue",
            "issue_id": issue_id,
            "issue_identifier": issue_identifier,
            "detail": detail,
        }

    # ------------------------------------------------------------------
    # Reconciliation (spec §8.5)
    # ------------------------------------------------------------------

    async def _reconcile_running_issues(self) -> None:
        """Stall detection + tracker state refresh (spec §8.5, §16.3)."""
        # Part A: stall detection
        now_utc = _now_utc()
        stalled: list[tuple[str, int]] = []
        qa_agent_cfg = self._config.transitions.qa_review.agent
        for issue_id, entry in list(self._state.running.items()):
            stall_timeout_ms = self._config.runner.stall_timeout_ms
            if entry.mode == ExecutionMode.REVIEW and qa_agent_cfg is not None:
                stall_timeout_ms = qa_agent_cfg.stall_timeout_ms
            if stall_timeout_ms <= 0:
                continue

            last_event_ts = entry.session.last_event_timestamp
            ref = last_event_ts if last_event_ts else entry.started_at
            elapsed_ms = (now_utc - ref).total_seconds() * 1000.0

            if elapsed_ms > stall_timeout_ms:
                stalled.append((issue_id, stall_timeout_ms))

        for issue_id, stall_timeout_ms in stalled:
            entry = self._state.running.get(issue_id)
            if entry:
                entry.status = RunStatus.STALLED
                self._record_problem(
                    kind="stall_detected",
                    severity="warning",
                    summary=f"Worker stalled for {entry.identifier}",
                    detail=f"No agent events received within {stall_timeout_ms / 1000:.0f}s timeout.",
                    issue_id=issue_id,
                    issue_identifier=entry.identifier,
                )
                issue_log(
                    logger, logging.WARNING,
                    "agent_stall_detected",
                    issue_id, entry.identifier,
                    stall_timeout_ms=stall_timeout_ms,
                )
                await self._terminate_running_issue(issue_id, cleanup_workspace=False)
                await self._schedule_retry(
                    issue_id,
                    entry.identifier,
                    _next_attempt(entry.retry_attempt),
                    error="stall_timeout",
                )

        # Part B: tracker state refresh
        running_ids = list(self._state.running.keys())
        if not running_ids:
            return

        try:
            refreshed = await self._linear_service.fetch_issue_states_by_ids(running_ids)
        except Exception as exc:
            logger.debug(
                f"action=reconcile_state_refresh_failed error={exc} "
                f"(keeping workers running)"
            )
            return

        refreshed_by_id = {i.id: i for i in refreshed}
        active_lower = [s.lower() for s in self._config.tracker.active_states]
        terminal_lower = [s.lower() for s in self._config.tracker.terminal_states]

        for issue_id in list(self._state.running.keys()):
            refreshed_issue = refreshed_by_id.get(issue_id)
            if not refreshed_issue:
                continue

            state_lower = refreshed_issue.state.lower()
            if state_lower in terminal_lower:
                entry = self._state.running.get(issue_id)
                if entry:
                    issue_log(
                        logger, logging.INFO,
                        "reconcile_terminal_stop",
                        issue_id, entry.identifier,
                        state=refreshed_issue.state,
                    )
                await self._terminate_running_issue(issue_id, cleanup_workspace=True)
            elif state_lower in active_lower:
                if issue_id in self._state.running:
                    self._state.running[issue_id].issue = refreshed_issue
            else:
                entry = self._state.running.get(issue_id)
                if entry:
                    self._record_problem(
                        kind="inactive_state",
                        severity="warning",
                        summary=f"Issue moved to inactive state {refreshed_issue.state!r}",
                        detail="Work was stopped by reconciliation because the issue is no longer in an active workflow state.",
                        issue_id=issue_id,
                        issue_identifier=entry.identifier,
                    )
                    issue_log(
                        logger, logging.INFO,
                        "reconcile_inactive_stop",
                        issue_id, entry.identifier,
                        state=refreshed_issue.state,
                    )
                await self._terminate_running_issue(issue_id, cleanup_workspace=False)

    async def _terminate_running_issue(self, issue_id: str, cleanup_workspace: bool) -> None:
        """Cancel running task and optionally clean workspace."""
        entry = self._state.running.pop(issue_id, None)
        if not entry:
            return
        self._plan_comment_locks.pop(issue_id, None)

        if entry.task and not entry.task.done():
            entry.task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(entry.task), timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

        elapsed = (_now_utc() - entry.started_at).total_seconds()
        self._state.codex_totals.seconds_running += elapsed

        if cleanup_workspace:
            try:
                await self._cleanup_entry_workspace(entry)
            except Exception as exc:
                logger.warning(
                    f"action=workspace_cleanup_failed "
                    f"identifier={entry.identifier} error={exc}"
                )

    async def _cleanup_entry_workspace(self, entry: RunningEntry) -> None:
        """Clean up a running entry workspace using recorded workspace metadata."""
        if entry.workspace_root and entry.workspace_key:
            if (
                entry.mode == ExecutionMode.BUILD
                and entry.workspace_root == self._config.workspace.root
            ):
                wm = WorkspaceManager(self._config)
                await wm.remove_workspace(entry.workspace_key)
                return

            qa_manager = self._qa_review_workflow.create_workspace_manager(
                self._config,
                entry.issue,
            )
            await qa_manager.remove_workspace(entry.workspace_key)
            return

        if entry.workspace_path:
            shutil.rmtree(entry.workspace_path, ignore_errors=True)
            logger.info(
                "action=workspace_removed "
                f"identifier={entry.identifier} path={entry.workspace_path}"
            )

    async def _cleanup_review_workspace(self, entry: RunningEntry) -> None:
        """Clean up the ephemeral QA workspace for a review run."""
        if entry.mode != ExecutionMode.REVIEW:
            return
        await self._cleanup_entry_workspace(entry)

    def _detect_todo_write(self, raw: dict, issue_id: str, entry: RunningEntry) -> None:
        """Detect TodoWrite tool calls from either Claude or Codex raw events."""
        raw_type = raw.get("type", "")

        # Claude: {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "TodoWrite", "input": {...}}]}}
        if raw_type == "assistant":
            message = raw.get("message") or {}
            content = message.get("content") or []
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") == "TodoWrite"
                ):
                    todos = (block.get("input") or {}).get("todos") or []
                    if todos:
                        self._sync_todo_comment(issue_id, entry, todos)

        # Codex: {"type": "item.completed", "item": {"type": "function_call", "name": "TodoWrite", "arguments": "{...}"}}
        elif raw_type == "item.completed":
            item = raw.get("item") or {}
            if item.get("type") == "function_call" and item.get("name") == "TodoWrite":
                args_raw = item.get("arguments", "")
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                except (json.JSONDecodeError, TypeError):
                    return
                if isinstance(args, dict):
                    todos = args.get("todos") or []
                    if todos:
                        self._sync_todo_comment(issue_id, entry, todos)

    def _sync_todo_comment(self, issue_id: str, entry: RunningEntry, todos: list[dict]) -> None:
        """Fire-and-forget: sync TodoWrite todos to a Linear comment; never raises."""
        body = self._execution_workflow.render_plan_comment(todos)
        entry.session.latest_plan = body

        async def _do() -> None:
            lock = self._plan_comment_locks.setdefault(issue_id, asyncio.Lock())
            try:
                async with lock:
                    comment_id = entry.session.plan_comment_id
                    client = self._linear_service
                    if comment_id is None:
                        new_id = await client.create_comment(issue_id, body)
                        entry.session.plan_comment_id = new_id
                        logger.info(
                            f"action=plan_comment_created issue_id={issue_id} comment_id={new_id}"
                        )
                    else:
                        await client.update_comment(comment_id, body)
                        logger.info(
                            f"action=plan_comment_updated issue_id={issue_id} comment_id={comment_id}"
                        )
            except Exception as exc:
                logger.warning(
                    f"action=plan_comment_sync_failed issue_id={issue_id} error={exc}"
                )

        asyncio.create_task(_do())

    async def _post_review_result_comment(
        self,
        issue_id: str,
        identifier: str,
        result: ReviewResult,
    ) -> None:
        """Persist the QA review outcome to a Linear comment.

        Uses upsert semantics: if a review comment was already posted for this
        issue during the current review cycle, the existing comment is updated
        instead of creating a duplicate.  The comment ID is tracked in
        ``_state.qa_review_comment_ids`` and cleared when the issue transitions
        out of the QA review lane.
        """
        existing_comment_id = self._state.qa_review_comment_ids.get(issue_id)
        try:
            comment_id, created = await self._qa_review_workflow.publish_review_result_comment(
                issue_id,
                result,
                existing_comment_id=existing_comment_id,
            )
            self._state.qa_review_comment_ids[issue_id] = comment_id
            issue_log(
                logger,
                logging.INFO,
                "qa_review_comment_created" if created else "qa_review_comment_updated",
                issue_id,
                identifier,
                comment_id=comment_id,
            )
        except Exception as exc:
            self._record_problem(
                kind="qa_review_comment_failed",
                summary="Failed to publish QA review findings",
                detail=str(exc),
                issue_id=issue_id,
                issue_identifier=identifier,
            )
            issue_log(
                logger,
                logging.WARNING,
                "qa_review_comment_failed",
                issue_id,
                identifier,
                error=str(exc),
            )

    async def _transition_issue_state(
        self,
        issue_id: str,
        state_name: str,
        *,
        trigger: str = "unknown",
        issue_identifier: str | None = None,
        from_state: str | None = None,
        ) -> bool:
        """Move an issue to the named workflow state. Returns True on success."""
        identifier = issue_identifier or self._resolve_identifier(issue_id)
        current_state = (
            from_state if issue_identifier is not None or from_state is not None
            else self._resolve_current_state(issue_id)
        )
        try:
            team_id, state_id = await self._linear_service.resolve_issue_state_id(
                issue_id,
                state_name,
                state_id_cache=self._state_id_cache,
            )
            if not team_id:
                logger.warning(
                    f"action=state_transition_skipped issue_id={issue_id} "
                    f"state={state_name!r} reason=team_id_not_found"
                )
                self._record_transition(
                    issue_id, identifier, current_state, state_name, trigger, success=False,
                )
                return False

            if not state_id:
                logger.warning(
                    f"action=state_transition_skipped issue_id={issue_id} "
                    f"state={state_name!r} team_id={team_id} reason=state_id_not_found"
                )
                self._record_transition(
                    issue_id, identifier, current_state, state_name, trigger, success=False,
                )
                return False

            await self._linear_service.client.set_issue_state(issue_id, state_id)
            logger.info(
                f"action=issue_state_set issue_id={issue_id} state={state_name!r} "
                f"team_id={team_id}"
            )
            self._record_transition(
                issue_id, identifier, current_state, state_name, trigger, success=True,
            )
            return True
        except Exception as exc:
            self._record_problem(
                kind="transition_failed",
                summary=f"State transition to {state_name!r} failed",
                detail=str(exc),
                issue_id=issue_id,
            )
            logger.warning(
                f"action=state_transition_failed issue_id={issue_id} "
                f"state={state_name!r} error={exc}"
            )
            self._record_transition(
                issue_id, identifier, current_state, state_name, trigger, success=False,
            )
            return False

    def _transition_issue_state_background(
        self,
        issue_id: str,
        state_name: str,
        *,
        trigger: str = "unknown",
        issue_identifier: str | None = None,
        from_state: str | None = None,
    ) -> None:
        """Schedule a state transition without blocking the caller."""
        asyncio.create_task(
            self._transition_issue_state(
                issue_id,
                state_name,
                trigger=trigger,
                issue_identifier=issue_identifier,
                from_state=from_state,
            )
        )

    def _resolve_identifier(self, issue_id: str) -> str:
        """Best-effort lookup of issue identifier from running/retry state."""
        entry = self._state.running.get(issue_id)
        if entry:
            return entry.identifier
        retry = self._state.retry_attempts.get(issue_id)
        if retry:
            return retry.identifier
        return issue_id

    def _resolve_current_state(self, issue_id: str) -> str | None:
        """Best-effort lookup of the issue's current Linear state."""
        entry = self._state.running.get(issue_id)
        if entry:
            return entry.issue.state
        return None

    def _record_transition(
        self,
        issue_id: str,
        identifier: str,
        from_state: str | None,
        to_state: str,
        trigger: str,
        *,
        success: bool,
    ) -> None:
        self._state.transition_history.insert(
            0,
            TransitionRecord(
                timestamp=_now_utc(),
                issue_id=issue_id,
                issue_identifier=identifier,
                from_state=from_state,
                to_state=to_state,
                trigger=trigger,
                success=success,
            ),
        )
        del self._state.transition_history[_MAX_TRANSITION_HISTORY:]

    # ------------------------------------------------------------------
    # Dispatch (spec §8.2, §16.4)
    # ------------------------------------------------------------------

    def _has_slots(self) -> bool:
        if self._state.dispatch_paused or self._is_shutting_down():
            return False
        global_available = max(
            self._state.max_concurrent_agents - len(self._state.running), 0
        )
        return global_available > 0

    def _unresolved_blockers(self, issue: Issue) -> list[BlockerRef]:
        """Return blockers that are not yet in a terminal state."""
        terminal_lower = {s.lower() for s in self._config.tracker.terminal_states}
        return [
            blocker for blocker in issue.blocked_by
            if (blocker.state or "").lower() not in terminal_lower
        ]

    def _maybe_transition_blocked_issue(self, issue: Issue) -> None:
        """Apply the configured blocked transition when an issue is gated by dependencies."""
        blocked_state = self._config.transitions.resolve("blocked")
        if not blocked_state:
            return
        if issue.state.lower() == blocked_state.lower():
            return
        if self._unresolved_blockers(issue):
            self._transition_issue_state_background(issue.id, blocked_state, trigger="blocked")

    def _is_dispatch_eligible(self, issue: Issue) -> bool:
        """Check non-slot dispatch eligibility (spec §8.2)."""
        # Must have required fields
        if not (issue.id and issue.identifier and issue.title and issue.state):
            return False

        active_lower = [s.lower() for s in self._config.tracker.active_states]
        terminal_lower = [s.lower() for s in self._config.tracker.terminal_states]
        state_lower = issue.state.lower()

        if state_lower not in active_lower:
            return False
        if state_lower in terminal_lower:
            return False
        if issue.id in self._state.running:
            return False
        if issue.id in self._state.claimed:
            return False
        if issue.id in self._state.skipped:
            return False

        # Blocker check: do not dispatch issues with unresolved dependencies.
        if self._unresolved_blockers(issue):
            return False

        return True

    def _has_state_slot(self, issue: Issue) -> bool:
        """Check per-state slot availability for an issue."""
        state_lower = issue.state.lower()
        per_state = self._config.agent.max_concurrent_agents_by_state
        if state_lower not in per_state:
            return True

        state_count = sum(
            1 for e in self._state.running.values()
            if e.issue.state.lower() == state_lower
        )
        return state_count < per_state[state_lower]

    def _should_dispatch(self, issue: Issue) -> bool:
        """Check dispatch eligibility including slot availability (spec §8.2)."""
        return (
            self._is_dispatch_eligible(issue)
            and self._has_slots()
            and self._has_state_slot(issue)
        )

    def _is_continuation_retry(self, retry_entry: RetryEntry) -> bool:
        """Continuation retries come from a clean worker exit, not an error path."""
        return retry_entry.error is None

    def _resolve_execution_mode(self, issue: Issue) -> ExecutionMode:
        """Determine whether an issue should run in build or review mode."""
        if not self._config.transitions.qa_review.enabled:
            return ExecutionMode.BUILD
        route = self._state_machine.route(issue)
        if route and route.workflow == "qa_review":
            return ExecutionMode.REVIEW
        return ExecutionMode.BUILD

    def _workflow_for_mode(
        self,
        mode: ExecutionMode,
    ) -> ExecutionWorkflow | QAReviewWorkflow:
        """Return the workflow boundary object for a runtime mode."""
        if mode == ExecutionMode.REVIEW:
            return self._qa_review_workflow
        return self._execution_workflow

    async def _dispatch_issue(self, issue: Issue, attempt: int | None) -> None:
        """Claim and spawn worker for issue (spec §16.4)."""
        if self._is_shutting_down():
            return
        self._state.claimed.add(issue.id)
        self._state.completed.discard(issue.id)
        self._state.retry_attempts.pop(issue.id, None)

        mode = self._resolve_execution_mode(issue)

        session = LiveSession(
            session_id=None,
            pid=None,
            last_event=None,
            last_event_timestamp=None,
            last_message=None,
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            last_reported_input_tokens=0,
            last_reported_output_tokens=0,
            last_reported_total_tokens=0,
            turn_count=0,
        )
        entry = RunningEntry(
            issue_id=issue.id,
            identifier=issue.identifier,
            issue=issue,
            task=None,
            session=session,
            retry_attempt=attempt,
            started_at=_now_utc(),
            mode=mode,
            qa_review_bounce_count=self._state.qa_review_bounces.get(issue.id, 0),
        )
        self._state.running[issue.id] = entry

        task = asyncio.create_task(
            self._worker(issue, attempt, entry),
            name=f"worker-{issue.identifier}",
        )
        entry.task = task
        task.add_done_callback(
            lambda t: asyncio.create_task(
                self._on_worker_done(issue.id, issue.identifier, entry, t)
            )
        )

        issue_log(
            logger, logging.INFO,
            "issue_dispatched",
            issue.id, issue.identifier,
            attempt=attempt,
            mode=mode.value,
        )
        # Review-mode issues are already in the QA dispatch state; skip the
        # normal dispatch transition to avoid fighting with the review lane.
        if mode == ExecutionMode.BUILD:
            route = self._state_machine.route(issue)
            target = route.target_state if route and route.workflow == "execution" else self._config.transitions.resolve("dispatch")
            if target:
                self._transition_issue_state_background(
                    issue.id,
                    target,
                    trigger="dispatch",
                    issue_identifier=issue.identifier,
                    from_state=issue.state,
                )

    # ------------------------------------------------------------------
    # Worker (spec §16.5)
    # ------------------------------------------------------------------

    async def _worker(
        self,
        issue: Issue,
        attempt: int | None,
        entry: RunningEntry,
    ) -> None:
        """Run one agent session for an issue (workspace + hooks + turns)."""
        workflow_runtime = self._workflow_for_mode(entry.mode)
        provider, runner_config = workflow_runtime.resolve_agent_runner(self._config)
        agent = create_agent_runner(provider, runner_config)

        # Prepare workspace
        entry.status = RunStatus.PREPARING_WORKSPACE
        try:
            active_wm, workspace = await workflow_runtime.prepare_workspace_run(
                self._config,
                issue,
            )
        except Exception as exc:
            entry.status = RunStatus.FAILED
            raise AgentError("workspace_error", f"Workspace creation failed: {exc}") from exc
        entry.workspace_path = workspace.path
        entry.workspace_root = getattr(active_wm, "_root", None)
        entry.workspace_key = workspace.workspace_key

        # before_run hook
        try:
            await workflow_runtime.prepare_run(active_wm, workspace, issue)
        except Exception as exc:
            entry.status = RunStatus.FAILED
            await active_wm.run_after_run_hook(workspace)
            raise AgentError("before_run_hook_error", str(exc)) from exc

        # Workspace-level preflight checks (git repo state)
        if self._config.preflight.enabled:
            ws_preflight = await run_preflight_checks(
                self._config.preflight, workspace_path=workspace.path,
            )
            if not ws_preflight.ok:
                entry.status = RunStatus.FAILED
                errors = "; ".join(c.message for c in ws_preflight.errors)
                for c in ws_preflight.errors:
                    self._record_problem(
                        kind="preflight_failed",
                        summary=f"Workspace preflight '{c.name}' failed",
                        detail=c.message,
                        issue_id=issue.id,
                        issue_identifier=issue.identifier,
                    )
                    issue_log(
                        logger, logging.ERROR,
                        "workspace_preflight_failed",
                        issue.id, issue.identifier,
                        check=c.name, message=c.message,
                    )
                raise AgentError("preflight_failed", f"Workspace preflight failed: {errors}")

        max_turns = self._config.agent.max_turns
        session_id: str | None = None
        turn_number = 1

        async def on_event(event: AgentEvent) -> None:
            await self._handle_agent_event(issue.id, entry, event)

        try:
            # Planning turn (build mode only): agent produces a TodoWrite checklist.
            # Review mode skips planning — the review prompt is self-contained.
            if workflow_runtime.requires_planning():
                entry.session.plan_comment_id = None
                entry.status = RunStatus.PLANNING
                plan_prompt = self._execution_workflow.build_plan_prompt(
                    self._workflow, issue
                )
                issue_log(
                    logger, logging.INFO,
                    "planning_turn_start",
                    issue.id, issue.identifier,
                )
                title = f"{issue.identifier}: {issue.title}"
                session_id = await agent.run_turn(
                    workspace_path=workspace.path,
                    prompt=plan_prompt,
                    issue_id=issue.id,
                    issue_identifier=issue.identifier,
                    session_id=None,
                    title=title,
                    on_event=on_event,
                )
                issue_log(
                    logger, logging.INFO,
                    "planning_turn_completed",
                    issue.id, issue.identifier,
                    session_id=session_id,
                )
                entry.session.turn_count += 1
                turn_number += 1  # planning turn consumed one slot

            first_execution_turn = True
            while True:
                # Render full prompt on the first execution turn; continuation turns
                # send brief guidance so the original task description is not re-injected (spec §7.1).
                entry.status = RunStatus.BUILDING_PROMPT
                try:
                    prompt = workflow_runtime.build_turn_prompt(
                        self._workflow,
                        issue,
                        attempt,
                        first_turn=first_execution_turn,
                    )
                except WorkflowError as exc:
                    entry.status = RunStatus.FAILED
                    raise AgentError("prompt_error", str(exc)) from exc
                first_execution_turn = False

                entry.session.turn_count += 1
                issue_log(
                    logger, logging.INFO,
                    "turn_start",
                    issue.id, issue.identifier,
                    turn=turn_number,
                    max_turns=max_turns,
                    session_id=session_id,
                )

                entry.status = RunStatus.LAUNCHING_AGENT
                title = f"{issue.identifier}: {issue.title}"
                session_id = await agent.run_turn(
                    workspace_path=workspace.path,
                    prompt=prompt,
                    issue_id=issue.id,
                    issue_identifier=issue.identifier,
                    session_id=session_id,
                    title=title,
                    on_event=on_event,
                )
                entry.status = RunStatus.FINISHING

                # Check current issue state after turn
                try:
                    refreshed_issue = await workflow_runtime.refresh_issue(issue.id)
                    if refreshed_issue is not None:
                        issue = refreshed_issue
                    entry.issue = issue
                except Exception as exc:
                    raise AgentError("issue_state_refresh_error", str(exc)) from exc

                should_continue, stop_reason = workflow_runtime.should_continue(
                    issue,
                    active_states=self._config.tracker.active_states,
                    turn_number=turn_number,
                    max_turns=max_turns,
                    workspace_path=workspace.path,
                    last_message=entry.session.last_message,
                )
                if not should_continue:
                    if stop_reason == "inactive_state":
                        issue_log(
                            logger, logging.INFO,
                            "turn_loop_exit_inactive",
                            issue.id, issue.identifier,
                            state=issue.state,
                        )
                    elif stop_reason == "max_turns":
                        issue_log(
                            logger, logging.INFO,
                            "turn_loop_exit_max_turns",
                            issue.id, issue.identifier,
                            max_turns=max_turns,
                        )
                    elif stop_reason in {"task_complete", "review_complete", "review_result_ready"}:
                        issue_log(
                            logger, logging.INFO,
                            "turn_loop_exit_complete",
                            issue.id, issue.identifier,
                            reason=stop_reason,
                        )
                    break

                turn_number += 1
                # Use attempt=None for continuation turns (spec §7.1)
                attempt = None

            # Capture the review result before after_run hooks run. Hooks may
            # clean workspace files as part of post-processing.
            entry.review_result = workflow_runtime.capture_run_result(workspace.path)

        finally:
            # Run after_run hook as an independent task so that a concurrent
            # reconciler cancel() on this worker task cannot interrupt it.
            hook_task = asyncio.create_task(active_wm.run_after_run_hook(workspace))
            try:
                await asyncio.shield(hook_task)
            except asyncio.CancelledError:
                # Worker was cancelled while hook was running; hook_task
                # continues independently in the event loop.
                raise

    async def _handle_agent_event(
        self,
        issue_id: str,
        entry: RunningEntry,
        event: AgentEvent,
    ) -> None:
        """Update live session from an agent event (spec §13.5)."""
        session = entry.session
        session.last_event = event.event
        session.last_event_timestamp = event.timestamp

        # Advance status on first event from the agent process
        if event.event == AgentEventType.SESSION_STARTED:
            entry.status = RunStatus.INITIALIZING_SESSION
        elif event.event == AgentEventType.NOTIFICATION:
            entry.status = RunStatus.STREAMING_TURN

        if event.session_id:
            session.session_id = event.session_id
        if event.pid:
            session.pid = event.pid
        if event.message:
            session.last_message = event.message

        event_row = {
            "event": event.event.value,
            "timestamp": event.timestamp.isoformat(),
        }
        if event.message:
            event_row["message"] = event.message
        if event.usage:
            event_row["usage"] = {
                "input_tokens": event.usage.get("input_tokens", 0),
                "output_tokens": event.usage.get("output_tokens", 0),
                "cache_read_input_tokens": event.usage.get("cache_read_input_tokens", 0),
            }
        session.recent_events.append(event_row)
        if len(session.recent_events) > _MAX_RECENT_EVENTS:
            session.recent_events = session.recent_events[-_MAX_RECENT_EVENTS:]

        # Detect TodoWrite tool calls in raw events and sync to Linear.
        # Claude emits {"type": "assistant", "message": {"content": [{"type": "tool_use", ...}]}}
        # Codex emits {"type": "item.completed", "item": {"type": "function_call", "name": "...", "arguments": "..."}}
        raw = event.raw
        if raw:
            self._detect_todo_write(raw, issue_id, entry)

        # Token accounting (spec §13.5)
        if event.usage:
            inp = event.usage.get("input_tokens", 0)
            out = event.usage.get("output_tokens", 0)
            total = inp + out

            # Track deltas to avoid double-counting
            inp_delta = max(inp - session.last_reported_input_tokens, 0)
            out_delta = max(out - session.last_reported_output_tokens, 0)

            session.last_reported_input_tokens = max(inp, session.last_reported_input_tokens)
            session.last_reported_output_tokens = max(out, session.last_reported_output_tokens)
            session.last_reported_total_tokens = max(total, session.last_reported_total_tokens)

            session.input_tokens += inp_delta
            session.output_tokens += out_delta
            session.total_tokens += inp_delta + out_delta

            self._state.codex_totals.input_tokens += inp_delta
            self._state.codex_totals.output_tokens += out_delta
            self._state.codex_totals.total_tokens += inp_delta + out_delta

    # ------------------------------------------------------------------
    # Worker exit (spec §16.6)
    # ------------------------------------------------------------------

    async def _on_worker_done(
        self,
        issue_id: str,
        identifier: str,
        entry: RunningEntry,
        task: asyncio.Task,  # type: ignore[type-arg]
    ) -> None:
        """Handle worker task completion (spec §16.6)."""
        elapsed = (_now_utc() - entry.started_at).total_seconds()
        self._state.codex_totals.seconds_running += elapsed
        self._state.running.pop(issue_id, None)

        exc = task.exception() if not task.cancelled() else None

        if task.cancelled() or (exc and isinstance(exc, asyncio.CancelledError)):
            # Cancelled by reconciliation — do not retry
            entry.status = RunStatus.CANCELED
            issue_log(
                logger, logging.INFO,
                "worker_cancelled",
                issue_id, identifier,
            )
            target = self._config.transitions.resolve("cancelled")
            if target:
                self._transition_issue_state_background(
                    issue_id,
                    target,
                    trigger="cancelled",
                    issue_identifier=identifier,
                    from_state=entry.issue.state,
                )
            return

        # Resolve transitions based on execution mode: review-mode workers
        # use the qa_review sub-config, build-mode workers use top-level transitions.
        is_review = entry.mode == ExecutionMode.REVIEW

        if exc is None:
            await self._handle_worker_success(issue_id, identifier, entry)
        else:
            await self._handle_worker_failure(issue_id, identifier, entry, exc)

    async def _handle_worker_success(
        self,
        issue_id: str,
        identifier: str,
        entry: RunningEntry,
    ) -> None:
        """Handle normal worker completion."""
        entry.status = RunStatus.SUCCEEDED
        self._state.completed.add(issue_id)
        issue_log(
            logger, logging.INFO,
            "worker_exited_normal",
            issue_id, identifier,
            mode=entry.mode.value,
        )

        if entry.mode == ExecutionMode.REVIEW:
            await self._handle_review_worker_success(issue_id, identifier, entry)
            return

        await self._handle_execution_worker_success(issue_id, identifier, entry)

    async def _handle_execution_worker_success(
        self,
        issue_id: str,
        identifier: str,
        entry: RunningEntry,
    ) -> None:
        """Handle successful completion of an execution worker."""
        qa = self._config.transitions.qa_review
        if qa.enabled:
            review_ready, review_error = await self._validate_review_handoff(
                identifier,
                workspace_path=entry.workspace_path,
            )
            if not review_ready:
                await self._handle_execution_review_handoff_failure(
                    issue_id,
                    identifier,
                    entry,
                    review_error,
                )
                return

        execution_outcome = self._execution_workflow.resolve_success_outcome(self._config)
        await self._apply_worker_success_transition(
            issue_id,
            identifier,
            entry,
            execution_outcome.target,
        )
        if execution_outcome.schedule_continuation_retry:
            retry_outcome = self._execution_workflow.resolve_continuation_retry_outcome(
                delay_ms=_CONTINUATION_RETRY_DELAY_MS,
            )
            await self._schedule_retry_from_outcome(
                issue_id,
                identifier,
                retry_outcome,
                entry=entry,
            )
            return

        self._release_completed_issue(issue_id)

    async def _handle_execution_review_handoff_failure(
        self,
        issue_id: str,
        identifier: str,
        entry: RunningEntry,
        review_error: str,
    ) -> None:
        """Handle an execution run that did not produce a reviewable handoff."""
        entry.status = RunStatus.FAILED
        self._state.completed.discard(issue_id)
        self._record_problem(
            kind="qa_review_not_ready",
            summary="Build finished without a reviewable GitHub PR",
            detail=review_error,
            issue_id=issue_id,
            issue_identifier=identifier,
        )
        issue_log(
            logger, logging.WARNING,
            "qa_review_handoff_blocked",
            issue_id, identifier,
            error=review_error,
        )
        self._state.completed.discard(issue_id)
        failure_outcome = self._execution_workflow.resolve_failure_outcome(
            self._config,
            next_attempt=_next_attempt(entry.retry_attempt),
            error=review_error,
        )
        await self._apply_execution_failure_outcome(
            issue_id,
            identifier,
            entry,
            failure_outcome,
        )
        await self._schedule_retry_from_outcome(
            issue_id,
            identifier,
            failure_outcome.retry,
            entry=entry,
        )

    async def _handle_review_worker_success(
        self,
        issue_id: str,
        identifier: str,
        entry: RunningEntry,
    ) -> None:
        """Handle successful completion of a QA review worker."""
        review_result = self._resolve_review_result(issue_id, identifier, entry)
        entry.review_result = review_result
        await self._post_review_result_comment(issue_id, identifier, review_result)
        if review_result.decision is None:
            await self._cleanup_review_workspace(entry)
            await self._handle_review_retry_or_hold(
                issue_id,
                identifier,
                entry,
                attempt=_next_attempt(entry.retry_attempt),
                error=review_result.error or "qa review result could not be parsed",
            )
            return

        issue_log(
            logger, logging.INFO,
            "qa_review_decision",
            issue_id, identifier,
            decision=review_result.decision.value,
            summary=review_result.summary,
        )

        initial_outcome = self._qa_review_workflow.resolve_completion_outcome(
            self._config,
            review_result,
            transition_succeeded=False,
            current_bounce_count=entry.qa_review_bounce_count,
        )
        transition_succeeded = await self._apply_worker_success_transition(
            issue_id,
            identifier,
            entry,
            initial_outcome.target,
        )
        current_bounce_count = self._state.qa_review_bounces.get(
            issue_id,
            entry.qa_review_bounce_count,
        )
        completion_outcome = self._qa_review_workflow.resolve_completion_outcome(
            self._config,
            review_result,
            transition_succeeded=transition_succeeded,
            current_bounce_count=current_bounce_count,
        )
        await self._apply_review_completion_outcome(
            issue_id,
            identifier,
            entry,
            review_result,
            completion_outcome,
        )

    async def _apply_review_completion_outcome(
        self,
        issue_id: str,
        identifier: str,
        entry: RunningEntry,
        review_result: ReviewResult,
        completion_outcome: QAReviewCompletionOutcome,
    ) -> None:
        """Apply QA-review completion bookkeeping after transition resolution."""
        qa = self._config.transitions.qa_review
        if completion_outcome.clear_bounces:
            self._clear_qa_review_bounces(issue_id)
        elif completion_outcome.increment_bounce:
            bounce_count = self._increment_qa_review_bounces(issue_id)
            entry.qa_review_bounce_count = bounce_count
            self._state.qa_review_comment_ids.pop(issue_id, None)
            self._persist_state()

        if review_result.decision == ReviewDecision.CHANGES_REQUESTED:
            issue_log(
                logger, logging.WARNING,
                "qa_review_bounced",
                issue_id, identifier,
                bounce_count=entry.qa_review_bounce_count,
                max_bounces=qa.max_bounces,
            )

        if completion_outcome.hold_for_manual_intervention:
            hold_outcome = self._qa_review_workflow.resolve_bounce_limit_hold_outcome(
                bounce_count=entry.qa_review_bounce_count,
            )
            await self._apply_review_manual_hold(
                issue_id,
                identifier,
                entry,
                problem_kind="qa_review_bounce_limit_reached",
                summary=hold_outcome.summary,
                detail=hold_outcome.detail,
                reason=hold_outcome.reason,
                cleanup_workspace=completion_outcome.should_cleanup_workspace,
            )
            return

        if completion_outcome.should_cleanup_workspace:
            await self._cleanup_review_workspace(entry)

        self._release_completed_issue(issue_id)

    async def _apply_worker_success_transition(
        self,
        issue_id: str,
        identifier: str,
        entry: RunningEntry,
        target: str | None,
    ) -> bool:
        """Apply a success transition if a target exists and the issue is active."""
        if not target or not self._issue_is_in_active_state(entry.issue):
            return False
        return await self._transition_issue_state(
            issue_id,
            target,
            trigger="success",
            issue_identifier=identifier,
            from_state=entry.issue.state,
        )

    async def _handle_worker_failure(
        self,
        issue_id: str,
        identifier: str,
        entry: RunningEntry,
        exc: BaseException,
    ) -> None:
        """Handle abnormal worker completion."""
        if isinstance(exc, AgentError) and exc.code == "stall_timeout":
            entry.status = RunStatus.STALLED
        elif isinstance(exc, AgentError) and exc.code == "turn_timeout":
            entry.status = RunStatus.TIMED_OUT
        else:
            entry.status = RunStatus.FAILED

        next_attempt = _next_attempt(entry.retry_attempt)
        error_str = str(exc)[:200]
        issue_log(
            logger, logging.WARNING,
            "worker_exited_abnormal",
            issue_id, identifier,
            attempt=next_attempt,
            error=error_str,
            run_status=entry.status.value,
            mode=entry.mode.value,
        )
        error_code = getattr(exc, "code", "unknown")
        kind = "worker_failed"
        if error_code == "before_run_hook_error":
            kind = "hook_failed"
        elif error_code == "workspace_error":
            kind = "workspace_error"
        self._record_problem(
            kind=kind,
            severity="error",
            summary=f"Worker failed for {identifier}",
            detail=error_str,
            issue_id=issue_id,
            issue_identifier=identifier,
        )

        if entry.mode == ExecutionMode.REVIEW:
            await self._cleanup_review_workspace(entry)
            await self._handle_review_retry_or_hold(
                issue_id,
                identifier,
                entry,
                attempt=next_attempt,
                error=error_str,
            )
            return

        failure_outcome = self._execution_workflow.resolve_failure_outcome(
            self._config,
            next_attempt=next_attempt,
            error=error_str,
        )
        await self._apply_execution_failure_outcome(
            issue_id,
            identifier,
            entry,
            failure_outcome,
            background=True,
        )
        await self._schedule_retry_from_outcome(
            issue_id,
            identifier,
            failure_outcome.retry,
            entry=entry,
        )

    async def _apply_execution_failure_outcome(
        self,
        issue_id: str,
        identifier: str,
        entry: RunningEntry,
        failure_outcome: ExecutionFailureOutcome,
        *,
        background: bool = False,
    ) -> None:
        """Apply the transition side of an execution failure outcome."""
        target = failure_outcome.transition_target
        if not target:
            return
        if background:
            self._transition_issue_state_background(
                issue_id,
                target,
                trigger="failure",
                issue_identifier=identifier,
                from_state=entry.issue.state,
            )
            return
        if self._issue_is_in_active_state(entry.issue):
            await self._transition_issue_state(
                issue_id,
                target,
                trigger="failure",
                issue_identifier=identifier,
                from_state=entry.issue.state,
            )

    async def _schedule_retry_from_outcome(
        self,
        issue_id: str,
        identifier: str,
        retry_outcome: ExecutionRetryOutcome,
        *,
        entry: RunningEntry | None = None,
    ) -> None:
        """Schedule a retry using a workflow-owned retry outcome."""
        await self._schedule_retry(
            issue_id,
            identifier,
            attempt=retry_outcome.attempt,
            delay_ms=retry_outcome.delay_ms,
            error=retry_outcome.error,
            entry=entry,
        )

    async def _apply_review_manual_hold(
        self,
        issue_id: str,
        identifier: str,
        entry: RunningEntry,
        *,
        problem_kind: str,
        summary: str,
        detail: str,
        reason: str,
        cleanup_workspace: bool,
    ) -> None:
        """Record and apply a QA-review manual intervention hold."""
        self._record_problem(
            kind=problem_kind,
            summary=summary,
            detail=detail,
            issue_id=issue_id,
            issue_identifier=identifier,
        )
        self._hold_issue_for_manual_intervention(
            issue_id,
            identifier,
            reason=reason,
        )
        if cleanup_workspace:
            await self._cleanup_review_workspace(entry)

    def _issue_is_in_active_state(self, issue: Issue) -> bool:
        """Return whether an issue is currently in an active workflow state."""
        active_lower = [s.lower() for s in self._config.tracker.active_states]
        return issue.state.lower() in active_lower

    def _resolve_execution_workspace_path(
        self,
        identifier: str,
        recorded_workspace_path: str | None = None,
    ) -> Path:
        """Return the execution workspace path, preferring recorded runtime metadata."""
        if recorded_workspace_path:
            return Path(recorded_workspace_path)
        return self._execution_workflow.workspace_path_for_identifier(identifier)

    def _resolve_review_result(
        self,
        issue_id: str,
        identifier: str,
        entry: RunningEntry,
    ) -> ReviewResult:
        """Load the machine-readable QA review result for a completed review run."""
        if entry.review_result is not None:
            result = entry.review_result
        elif entry.workspace_path:
            result = self._qa_review_workflow.load_review_result(entry.workspace_path)
        else:
            result = ReviewResult(
                decision=None,
                error=(
                    "QA review workspace metadata missing; "
                    "cannot load REVIEW_RESULT.json from an isolated review run"
                ),
            )

        if result.decision is None:
            missing_result = is_review_result_missing(result.error)
            self._record_problem(
                kind="qa_review_result_missing" if missing_result else "qa_review_parse_error",
                summary=(
                    "QA review result file was not produced"
                    if missing_result
                    else "QA review result could not be parsed"
                ),
                detail=result.error or "unknown parse error",
                issue_id=issue_id,
                issue_identifier=identifier,
            )
            issue_log(
                logger, logging.WARNING,
                "qa_review_parse_failed",
                issue_id, identifier,
                error=result.error,
            )
        return result

    async def _handle_review_retry_or_hold(
        self,
        issue_id: str,
        identifier: str,
        entry: RunningEntry,
        *,
        attempt: int,
        error: str,
    ) -> None:
        """Retry reviewer failures in-place, or hold them for manual intervention."""
        qa = self._config.transitions.qa_review
        issue_log(
            logger, logging.WARNING,
            "qa_review_retry_considered",
            issue_id, identifier,
            attempt=attempt,
            max_retries=qa.max_retries,
            error=error,
            run_status=entry.status.value,
        )
        retry_outcome = self._qa_review_workflow.resolve_retry_outcome(
            self._config,
            attempt=attempt,
            error=error,
        )
        if retry_outcome.hold_for_manual_intervention:
            self._record_problem(
                kind="qa_review_retry_limit_reached",
                summary=retry_outcome.summary or "QA review retry limit reached",
                detail=retry_outcome.detail or error,
                issue_id=issue_id,
                issue_identifier=identifier,
            )
            self._hold_issue_for_manual_intervention(
                issue_id,
                identifier,
                reason=retry_outcome.reason or "qa_review_retry_limit",
            )
            return

        await self._schedule_retry(
            issue_id,
            identifier,
            attempt=retry_outcome.attempt or attempt,
            error=retry_outcome.error or error,
            entry=entry,
        )

    async def _validate_review_handoff(
        self,
        identifier: str,
        workspace_path: str | None = None,
    ) -> tuple[bool, str]:
        """Verify that build-mode work produced a reviewable PR before QA dispatch."""
        resolved_workspace_path = self._resolve_execution_workspace_path(
            identifier,
            recorded_workspace_path=workspace_path,
        )
        base_branch = self._config.preflight.base_branch or _DEFAULT_BASE_BRANCH
        return await self._execution_workflow.validate_review_handoff(
            resolved_workspace_path,
            base_branch=base_branch,
            run_command=self._run_workspace_command,
        )

    async def _run_workspace_command(
        self,
        workspace_path: Path,
        *args: str,
    ) -> tuple[int, str, str]:
        """Run a command inside a workspace and capture its output."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(workspace_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return 127, "", str(exc)

        stdout, stderr = await proc.communicate()
        return (
            proc.returncode or 0,
            (stdout or b"").decode(errors="replace").strip(),
            (stderr or b"").decode(errors="replace").strip(),
        )

    def _clear_qa_review_cycle_state(self, issue_id: str) -> None:
        self._state.qa_review_bounces.pop(issue_id, None)
        self._state.qa_review_comment_ids.pop(issue_id, None)

    def _clear_qa_review_bounces(self, issue_id: str) -> None:
        self._clear_qa_review_cycle_state(issue_id)
        self._persist_state()

    def _increment_qa_review_bounces(self, issue_id: str) -> int:
        next_count = self._state.qa_review_bounces.get(issue_id, 0) + 1
        self._state.qa_review_bounces[issue_id] = next_count
        self._persist_state()
        return next_count

    def _hold_issue_for_manual_intervention(
        self,
        issue_id: str,
        identifier: str,
        *,
        reason: str,
    ) -> None:
        self._state.retry_attempts.pop(issue_id, None)
        self._state.claimed.discard(issue_id)
        self._state.completed.discard(issue_id)
        self._state.qa_review_comment_ids.pop(issue_id, None)
        self._state.skipped[issue_id] = SkippedEntry(
            issue_id=issue_id,
            identifier=identifier,
            created_at=_now_utc(),
            reason=reason,
        )
        self._persist_state()

    # ------------------------------------------------------------------
    # Retry scheduling (spec §8.4)
    # ------------------------------------------------------------------

    async def _schedule_retry(
        self,
        issue_id: str,
        identifier: str,
        attempt: int,
        delay_ms: float | None = None,
        error: str | None = None,
        entry: RunningEntry | None = None,
    ) -> None:
        """Schedule a retry for an issue (spec §8.4)."""
        is_continuation = error is None

        if not is_continuation and attempt >= 3:
            self._record_problem(
                kind="retry_storm",
                severity="warning",
                summary=f"Retry storm for {identifier} (attempt {attempt})",
                detail=f"Issue has failed {attempt} times: {error or 'unknown error'}",
                issue_id=issue_id,
                issue_identifier=identifier,
            )

        # Cancel existing retry timer
        existing = self._state.retry_attempts.pop(issue_id, None)
        if existing:
            logger.debug(
                f"action=retry_cancelled_for_new "
                f"issue_id={issue_id} identifier={identifier}"
            )

        if delay_ms is None:
            delay_ms = _backoff_delay_ms(
                attempt, self._config.agent.max_retry_backoff_ms
            )

        due_at_ms = _monotonic_ms() + delay_ms
        retry_entry = RetryEntry(
            issue_id=issue_id,
            identifier=identifier,
            attempt=attempt,
            due_at_ms=due_at_ms,
            error=error,
        )
        if entry is not None:
            retry_entry.mode = entry.mode.value
            retry_entry.state = entry.issue.state
            retry_entry.run_status = entry.status.value
            retry_entry.session_id = entry.session.session_id
            retry_entry.turn_count = entry.session.turn_count
            retry_entry.last_event = (
                entry.session.last_event.value if entry.session.last_event else None
            )
            retry_entry.last_message = entry.session.last_message
            retry_entry.last_event_at = entry.session.last_event_timestamp
            retry_entry.workspace_path = entry.workspace_path
            retry_entry.tokens = {
                "input_tokens": entry.session.input_tokens,
                "output_tokens": entry.session.output_tokens,
                "total_tokens": entry.session.total_tokens,
            }
            retry_entry.started_at = entry.started_at
            retry_entry.retry_attempt = entry.retry_attempt
            retry_entry.plan_comment_id = entry.session.plan_comment_id
            retry_entry.latest_plan = entry.session.latest_plan
            retry_entry.recent_events = list(entry.session.recent_events)
            retry_entry.issue_title = entry.issue.title
            retry_entry.issue_url = entry.issue.url
            retry_entry.issue_description = entry.issue.description
            retry_entry.issue_labels = list(entry.issue.labels)
            retry_entry.issue_comments = [
                {
                    "author": comment.author,
                    "body": comment.body,
                    "created_at": comment.created_at.isoformat() if comment.created_at else None,
                }
                for comment in entry.issue.comments
            ]
            retry_entry.qa_review_bounce_count = entry.qa_review_bounce_count
        elif existing is not None:
            retry_entry.state = existing.state
            retry_entry.run_status = existing.run_status
            retry_entry.session_id = existing.session_id
            retry_entry.turn_count = existing.turn_count
            retry_entry.last_event = existing.last_event
            retry_entry.last_message = existing.last_message
            retry_entry.last_event_at = existing.last_event_at
            retry_entry.workspace_path = existing.workspace_path
            retry_entry.tokens = dict(existing.tokens)
            retry_entry.started_at = existing.started_at
            retry_entry.retry_attempt = existing.retry_attempt
            retry_entry.plan_comment_id = existing.plan_comment_id
            retry_entry.latest_plan = existing.latest_plan
            retry_entry.recent_events = list(existing.recent_events)
            retry_entry.issue_title = existing.issue_title
            retry_entry.issue_url = existing.issue_url
            retry_entry.issue_description = existing.issue_description
            retry_entry.issue_labels = list(existing.issue_labels)
            retry_entry.issue_comments = list(existing.issue_comments)
            retry_entry.qa_review_bounce_count = existing.qa_review_bounce_count
        self._state.retry_attempts[issue_id] = retry_entry

        if is_continuation:
            issue_log(
                logger, logging.INFO,
                "continuation_retry_scheduled",
                issue_id, identifier,
                attempt=attempt,
                delay_ms=delay_ms,
            )
        else:
            issue_log(
                logger, logging.INFO,
                "retry_scheduled",
                issue_id, identifier,
                attempt=attempt,
                delay_ms=delay_ms,
                error=error,
            )

        asyncio.get_event_loop().call_later(
            delay_ms / 1000.0,
            lambda: asyncio.create_task(self._on_retry_timer(issue_id)),
        )

        self._persist_state()

    async def _on_retry_timer(self, issue_id: str) -> None:
        """Handle retry timer firing (spec §16.6)."""
        if self._is_shutting_down():
            self._state.retry_attempts.pop(issue_id, None)
            return
        retry_entry = self._state.retry_attempts.pop(issue_id, None)
        if not retry_entry:
            return

        is_continuation = self._is_continuation_retry(retry_entry)

        try:
            candidates = await self._linear_service.fetch_candidate_issues()
        except Exception as exc:
            await self._handle_retry_poll_failure(issue_id, retry_entry, exc)
            return

        issue = next((i for i in candidates if i.id == issue_id), None)

        if issue is None:
            self._release_retry_bookkeeping(issue_id)
            self._log_retry_release(
                issue_id,
                retry_entry.identifier,
                is_continuation=is_continuation,
                issue_found=False,
            )
            return

        self._release_retry_bookkeeping(issue_id)

        if not self._is_dispatch_eligible(issue):
            self._maybe_transition_blocked_issue(issue)
            self._log_retry_release(
                issue_id,
                retry_entry.identifier,
                is_continuation=is_continuation,
                issue_found=True,
                state=issue.state,
            )
            return

        if not self._has_slots() or not self._has_state_slot(issue):
            await self._handle_retry_waiting_for_slot(
                issue_id,
                retry_entry,
                issue,
                is_continuation=is_continuation,
            )
            return

        await self._redispatch_retry_issue(
            issue_id,
            retry_entry,
            issue,
            is_continuation=is_continuation,
        )

    async def _handle_retry_poll_failure(
        self,
        issue_id: str,
        retry_entry: RetryEntry,
        exc: Exception,
    ) -> None:
        """Reschedule a retry after candidate polling fails."""
        logger.warning(
            f"action=retry_poll_failed "
            f"issue_id={issue_id} error={exc}"
        )
        retry_outcome = self._execution_workflow.resolve_retry_poll_failure_outcome(
            attempt=retry_entry.attempt,
        )
        await self._schedule_retry(
            issue_id,
            retry_entry.identifier,
            attempt=retry_outcome.attempt,
            delay_ms=retry_outcome.delay_ms,
            error=retry_outcome.error,
        )

    def _release_retry_bookkeeping(self, issue_id: str) -> None:
        """Clear guards so a retry can be evaluated or released cleanly."""
        self._state.claimed.discard(issue_id)
        self._state.completed.discard(issue_id)

    def _release_completed_issue(self, issue_id: str) -> None:
        """Release local claim/completion bookkeeping after a finished run."""
        self._release_retry_bookkeeping(issue_id)
        self._persist_state()

    def _log_retry_release(
        self,
        issue_id: str,
        identifier: str,
        *,
        is_continuation: bool,
        issue_found: bool,
        state: str | None = None,
    ) -> None:
        """Log that a retry was released without redispatch."""
        issue_log(
            logger, logging.INFO,
            self._execution_workflow.release_log_action(
                is_continuation=is_continuation,
                issue_found=issue_found,
            ),
            issue_id, identifier,
            state=state,
        )

    async def _handle_retry_waiting_for_slot(
        self,
        issue_id: str,
        retry_entry: RetryEntry,
        issue: Issue,
        *,
        is_continuation: bool,
    ) -> None:
        """Reschedule a retry when the orchestrator has no capacity."""
        retry_outcome = self._execution_workflow.resolve_slot_wait_retry_outcome(
            attempt=retry_entry.attempt,
            continuation_delay_ms=_CONTINUATION_RETRY_DELAY_MS,
            is_continuation=is_continuation,
        )
        log_kwargs: dict[str, Any] = {}
        if not is_continuation:
            log_kwargs["attempt"] = retry_outcome.attempt
        issue_log(
            logger, logging.INFO,
            self._execution_workflow.waiting_for_slot_log_action(
                is_continuation=is_continuation,
            ),
            issue_id, retry_entry.identifier,
            **log_kwargs,
        )
        await self._schedule_retry(
            issue_id,
            issue.identifier,
            attempt=retry_outcome.attempt,
            delay_ms=retry_outcome.delay_ms,
            error=retry_outcome.error,
        )

    async def _redispatch_retry_issue(
        self,
        issue_id: str,
        retry_entry: RetryEntry,
        issue: Issue,
        *,
        is_continuation: bool,
    ) -> None:
        """Redispatch an eligible retry candidate."""
        issue_log(
            logger, logging.INFO,
            self._execution_workflow.redispatch_log_action(
                is_continuation=is_continuation,
            ),
            issue_id, retry_entry.identifier,
            attempt=retry_entry.attempt,
        )
        await self._dispatch_issue(issue, attempt=retry_entry.attempt)

    # ------------------------------------------------------------------
    # Snapshot (for HTTP server)
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        """Return runtime snapshot for observability (spec §13.3)."""
        now = _now_utc()
        live_seconds = sum(
            (now - e.started_at).total_seconds()
            for e in self._state.running.values()
        )
        totals = self._state.codex_totals
        qa_review = self._config.transitions.qa_review
        qa_agent_cfg = qa_review.agent

        running_rows = []
        for entry in self._state.running.values():
            running_rows.append(self._snapshot_running_entry(entry))

        retrying_rows = []
        for issue_id, retry in self._state.retry_attempts.items():
            remaining_ms = max(retry.due_at_ms - _monotonic_ms(), 0)
            due_at = now + timedelta(milliseconds=remaining_ms)
            retrying_rows.append({
                "issue_id": issue_id,
                "issue_identifier": retry.identifier,
                "attempt": retry.attempt,
                "due_at": due_at.isoformat(),
                "error": retry.error,
                "mode": retry.mode,
                "state": retry.state,
                "run_status": retry.run_status,
                "session_id": retry.session_id,
                "turn_count": retry.turn_count,
                "last_event": retry.last_event,
                "last_message": retry.last_message or "",
                "last_event_at": retry.last_event_at.isoformat() if retry.last_event_at else None,
                "workspace_path": retry.workspace_path,
                "tokens": retry.tokens,
                "started_at": retry.started_at.isoformat() if retry.started_at else None,
                "retry_attempt": retry.retry_attempt,
                "plan_comment_id": retry.plan_comment_id,
                "latest_plan": retry.latest_plan,
                "recent_events": retry.recent_events,
                "issue_title": retry.issue_title,
                "issue_url": retry.issue_url,
                "issue_description": retry.issue_description,
                "issue_labels": retry.issue_labels,
                "issue_comments": retry.issue_comments,
                "qa_review_bounce_count": retry.qa_review_bounce_count,
            })

        waiting_rows = self._build_waiting_rows(now)
        problem_rows = [
            {
                "kind": problem.kind,
                "severity": problem.severity,
                "summary": problem.summary,
                "detail": problem.detail,
                "issue_id": problem.issue_id,
                "issue_identifier": problem.issue_identifier,
                "observed_at": problem.observed_at.isoformat(),
            }
            for problem in self._state.recent_problems
        ]
        skipped_rows = []
        for issue_id, skipped in self._state.skipped.items():
            skipped_rows.append({
                "issue_id": issue_id,
                "issue_identifier": skipped.identifier,
                "reason": skipped.reason,
                "created_at": skipped.created_at.isoformat(),
            })

        control_rows = []
        for action in self._state.control_actions:
            control_rows.append({
                "timestamp": action.timestamp.isoformat(),
                "action": action.action,
                "scope": action.scope,
                "outcome": action.outcome,
                "issue_id": action.issue_id,
                "issue_identifier": action.issue_identifier,
                "detail": action.detail,
            })

        return {
            "generated_at": now.isoformat(),
            "provider": self._config.agent.provider,
            "counts": {
                "running": len(running_rows),
                "retrying": len(retrying_rows),
                "waiting": len(waiting_rows),
                "problems": len(problem_rows),
                "skipped": len(skipped_rows),
            },
            "controls": {
                "dispatch_paused": self._state.dispatch_paused,
                "shutdown_requested": self._state.shutdown_requested,
                "recent_actions": control_rows,
            },
            "running": running_rows,
            "retrying": retrying_rows,
            "waiting": waiting_rows,
            "problems": problem_rows,
            "skipped": skipped_rows,
            "codex_totals": {
                "input_tokens": totals.input_tokens,
                "output_tokens": totals.output_tokens,
                "total_tokens": totals.total_tokens,
                "seconds_running": round(totals.seconds_running + live_seconds, 2),
            },
            "rate_limits": self._state.codex_rate_limits,
            "preflight_errors": list(self._state.last_preflight_errors),
            "validation_errors": list(self._state.last_validation_errors),
            "workflow_config": {
                "active_states": list(self._config.tracker.active_states),
                "terminal_states": list(self._config.tracker.terminal_states),
                "transitions": {
                    "dispatch": self._config.transitions.dispatch,
                    "success": self._config.transitions.success,
                    "failure": self._config.transitions.failure,
                    "blocked": self._config.transitions.blocked,
                    "cancelled": self._config.transitions.cancelled,
                    "qa_review": {
                        "enabled": qa_review.enabled,
                        "dispatch": qa_review.dispatch,
                        "success": qa_review.success,
                        "failure": qa_review.failure,
                        "max_bounces": qa_review.max_bounces,
                        "max_retries": qa_review.max_retries,
                        "agent": {
                            "provider": qa_agent_cfg.provider,
                            "command": qa_agent_cfg.command,
                            "turn_timeout_ms": qa_agent_cfg.turn_timeout_ms,
                            "read_timeout_ms": qa_agent_cfg.read_timeout_ms,
                            "stall_timeout_ms": qa_agent_cfg.stall_timeout_ms,
                            "dangerously_skip_permissions": qa_agent_cfg.dangerously_skip_permissions,
                        } if qa_agent_cfg is not None else None,
                    },
                },
            },
            "transition_history": [
                {
                    "timestamp": t.timestamp.isoformat(),
                    "issue_id": t.issue_id,
                    "issue_identifier": t.issue_identifier,
                    "from_state": t.from_state,
                    "to_state": t.to_state,
                    "trigger": t.trigger,
                    "success": t.success,
                }
                for t in self._state.transition_history
            ],
        }

    def _record_problem(
        self,
        *,
        kind: str,
        summary: str,
        detail: str,
        severity: str = "error",
        issue_id: str | None = None,
        issue_identifier: str | None = None,
    ) -> None:
        if issue_identifier is None and issue_id:
            entry = self._state.running.get(issue_id)
            if entry:
                issue_identifier = entry.identifier
            else:
                retry = self._state.retry_attempts.get(issue_id)
                if retry:
                    issue_identifier = retry.identifier

        self._state.recent_problems.insert(
            0,
            ProblemRecord(
                kind=kind,
                severity=severity,
                summary=summary,
                detail=detail,
                observed_at=_now_utc(),
                issue_id=issue_id,
                issue_identifier=issue_identifier,
            ),
        )
        del self._state.recent_problems[_MAX_RECENT_PROBLEMS:]

    def _build_waiting_rows(self, now: datetime) -> list[dict[str, Any]]:
        waiting_rows: list[dict[str, Any]] = []

        for issue in _sort_for_dispatch(self._state.last_candidates):
            if issue.id in self._state.running:
                continue

            waiting_row = self._build_waiting_row(issue)
            if waiting_row is not None:
                waiting_rows.append(waiting_row)

        for issue_id, retry in self._state.retry_attempts.items():
            remaining_ms = max(retry.due_at_ms - _monotonic_ms(), 0)
            due_at = now + timedelta(milliseconds=remaining_ms)
            waiting_rows.append(
                {
                    "issue_id": issue_id,
                    "issue_identifier": retry.identifier,
                    "state": None,
                    "kind": "waiting_for_retry",
                    "summary": "Waiting for retry timer",
                    "detail": retry.error or "Waiting for continuation retry",
                    "attempt": retry.attempt,
                    "due_at": due_at.isoformat(),
                }
            )

        waiting_rows.sort(
            key=lambda row: (row.get("issue_identifier") or "", row.get("kind") or "")
        )
        return waiting_rows

    def _build_waiting_row(self, issue: Issue) -> dict[str, Any] | None:
        terminal_lower = {s.lower() for s in self._config.tracker.terminal_states}
        active_lower = {s.lower() for s in self._config.tracker.active_states}
        state_lower = issue.state.lower()

        row = {
            "issue_id": issue.id,
            "issue_identifier": issue.identifier,
            "state": issue.state,
            "attempt": None,
            "due_at": None,
        }

        if not (issue.id and issue.identifier and issue.title and issue.state):
            return {
                **row,
                "kind": "invalid_issue",
                "summary": "Issue data is incomplete",
                "detail": "Required issue fields are missing, so dispatch is skipped.",
            }

        if issue.id in self._state.retry_attempts:
            return None

        if state_lower not in active_lower:
            return {
                **row,
                "kind": "inactive_state",
                "summary": f"Issue is in inactive state {issue.state!r}",
                "detail": "Dispatch only runs for configured active states.",
            }

        if state_lower in terminal_lower:
            return {
                **row,
                "kind": "terminal_state",
                "summary": f"Issue is in terminal state {issue.state!r}",
                "detail": "Terminal issues are not eligible for dispatch.",
            }

        if issue.id in self._state.claimed:
            return {
                **row,
                "kind": "claimed",
                "summary": "Issue is already claimed",
                "detail": "The orchestrator has already reserved this issue for work.",
            }

        blockers = self._unresolved_blockers(issue)
        if blockers:
                blocker_desc = ", ".join(
                    f"{blocker.identifier or blocker.id or 'unknown'} ({blocker.state or 'unknown'})"
                    for blocker in blockers
                )
                return {
                    **row,
                    "kind": "blocked_by_dependency",
                    "summary": "Blocked by dependency",
                    "detail": blocker_desc,
                }

        if self._state.dispatch_paused:
            return {
                **row,
                "kind": "dispatch_paused",
                "summary": "Dispatching is paused",
                "detail": "Resume dispatching to make this issue eligible again.",
            }

        if not self._has_slots():
            capacity = self._state.max_concurrent_agents
            in_use = len(self._state.running)
            return {
                **row,
                "kind": "no_slots_available",
                "summary": "No global orchestrator slots available",
                "detail": f"{in_use}/{capacity} slots are currently in use.",
            }

        if not self._has_state_slot(issue):
            state_key = issue.state.lower()
            capacity = self._config.agent.max_concurrent_agents_by_state.get(state_key)
            in_use = sum(
                1 for entry in self._state.running.values()
                if entry.issue.state.lower() == state_key
            )
            return {
                **row,
                "kind": "no_state_slots_available",
                "summary": f"No slots available for state {issue.state!r}",
                "detail": (
                    f"{in_use}/{capacity} slots are currently in use for this state."
                    if capacity is not None
                    else "The per-state concurrency limit is currently saturated."
                ),
            }

        return None

    def _find_tracked_issue(self, identifier: str) -> dict[str, str] | None:
        """Locate an issue in running, retry, or skipped collections by identifier."""
        target = identifier.upper()
        for issue_id, entry in self._state.running.items():
            if entry.identifier.upper() == target:
                return {"kind": "running", "issue_id": issue_id}
        for issue_id, retry in self._state.retry_attempts.items():
            if retry.identifier.upper() == target:
                return {"kind": "retrying", "issue_id": issue_id}
        for issue_id, skipped in self._state.skipped.items():
            if skipped.identifier.upper() == target:
                return {"kind": "skipped", "issue_id": issue_id}
        return None

    def _record_control(
        self,
        action: str,
        scope: str,
        outcome: str,
        issue_id: str | None = None,
        issue_identifier: str | None = None,
        detail: str | None = None,
    ) -> None:
        record = ControlAction(
            timestamp=_now_utc(),
            action=action,
            scope=scope,
            outcome=outcome,
            issue_id=issue_id,
            issue_identifier=issue_identifier,
            detail=detail,
        )
        self._state.control_actions.append(record)
        if len(self._state.control_actions) > _CONTROL_HISTORY_LIMIT:
            self._state.control_actions = self._state.control_actions[-_CONTROL_HISTORY_LIMIT:]

        logger.info(
            "action=operator_control "
            f"control_action={action} scope={scope} outcome={outcome} "
            f"issue_id={issue_id!r} issue_identifier={issue_identifier!r} detail={detail!r}"
        )

    def _is_shutting_down(self) -> bool:
        return self._state.shutdown_requested or self._shutdown_event.is_set()

    def _snapshot_running_entry(
        self,
        entry: RunningEntry,
    ) -> dict[str, Any]:
        s = entry.session
        return {
            "issue_id": entry.issue_id,
            "issue_identifier": entry.identifier,
            "issue_title": entry.issue.title,
            "issue_url": entry.issue.url,
            "issue_description": entry.issue.description,
            "issue_labels": list(entry.issue.labels),
            "issue_comments": [
                {
                    "author": comment.author,
                    "body": comment.body,
                    "created_at": comment.created_at.isoformat() if comment.created_at else None,
                }
                for comment in entry.issue.comments
            ],
            "state": entry.issue.state,
            "mode": entry.mode.value,
            "run_status": entry.status.value,
            "session_id": s.session_id,
            "turn_count": s.turn_count,
            "last_event": s.last_event.value if s.last_event else None,
            "last_message": s.last_message or "",
            "started_at": entry.started_at.isoformat(),
            "last_event_at": s.last_event_timestamp.isoformat()
            if s.last_event_timestamp else None,
            "retry_attempt": entry.retry_attempt,
            "qa_review_bounce_count": entry.qa_review_bounce_count,
            "workspace_path": entry.workspace_path,
            "plan_comment_id": s.plan_comment_id,
            "latest_plan": s.latest_plan,
            "recent_events": list(s.recent_events),
            "tokens": {
                "input_tokens": s.input_tokens,
                "output_tokens": s.output_tokens,
                "total_tokens": s.total_tokens,
            },
        }

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sort_for_dispatch(issues: list[Issue]) -> list[Issue]:
    """Sort issues for dispatch (spec §8.2): priority asc, then oldest first."""
    def _key(i: Issue):
        prio = i.priority if i.priority is not None else 9999
        created = i.created_at.timestamp() if i.created_at else float("inf")
        return (prio, created, i.identifier)

    return sorted(issues, key=_key)


def _next_attempt(current: int | None) -> int:
    if current is None:
        return 1
    return current + 1


def _backoff_delay_ms(attempt: int, max_backoff_ms: int) -> float:
    """Exponential backoff: min(10000 * 2^(attempt-1), max_backoff_ms) (spec §8.4)."""
    delay = 10_000.0 * (2 ** (attempt - 1))
    return min(delay, float(max_backoff_ms))
